"""
Distributed URL Shortener Service
==================================
A scalable URL shortener built with FastAPI, PostgreSQL, and Redis.
Features consistent hashing, caching with circuit breaker, rate limiting,
and structured error handling.
"""

import hashlib
import os
import time
import string
import random
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, HttpUrl, Field
import asyncpg
import redis.asyncio as aioredis


# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("url-shortener")


# ─── Configuration ───────────────────────────────────────────────────────────

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shortener:shortener@localhost:5432/shortener")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
SHORT_CODE_LENGTH = 7
RATE_LIMIT_MAX_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60
CACHE_TTL_SECONDS = 3600

# Circuit breaker settings
REDIS_TIMEOUT_SECONDS = 1          # Fast timeout — don't block on Redis failures
CIRCUIT_BREAKER_THRESHOLD = 3      # Open circuit after 3 consecutive failures
CIRCUIT_BREAKER_RESET_SECONDS = 30 # Try Redis again after 30 seconds


# ─── Models ──────────────────────────────────────────────────────────────────

class URLCreateRequest(BaseModel):
    url: HttpUrl = Field(..., description="The original URL to shorten")
    custom_code: Optional[str] = Field(
        None, min_length=3, max_length=20, pattern=r"^[a-zA-Z0-9_-]+$",
        description="Optional custom short code"
    )

class URLCreateResponse(BaseModel):
    short_code: str
    short_url: str
    original_url: str
    created_at: str

class URLStatsResponse(BaseModel):
    short_code: str
    original_url: str
    click_count: int
    created_at: str
    last_accessed: Optional[str]

class HealthResponse(BaseModel):
    status: str
    database: str
    cache: str
    circuit_breaker: str
    timestamp: str


# ─── Circuit Breaker ─────────────────────────────────────────────────────────

class RedisCircuitBreaker:
    """
    Circuit breaker for Redis operations.

    After CIRCUIT_BREAKER_THRESHOLD consecutive failures, the circuit opens
    and all Redis operations are skipped (returning None) for
    CIRCUIT_BREAKER_RESET_SECONDS. After that, a single probe request is
    allowed through. If it succeeds, the circuit closes. If it fails,
    the circuit stays open for another reset period.
    """

    def __init__(self, threshold: int = 3, reset_seconds: int = 30):
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.failure_count = 0
        self.last_failure_time: float = 0
        self.state = "closed"  # closed, open, half-open

    def record_success(self):
        self.failure_count = 0
        if self.state != "closed":
            logger.info("Circuit breaker CLOSED — Redis is back")
        self.state = "closed"

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.threshold:
            if self.state != "open":
                logger.warning(
                    f"Circuit breaker OPEN — Redis failed {self.failure_count} times consecutively. "
                    f"Falling back to PostgreSQL-only mode for {self.reset_seconds}s."
                )
            self.state = "open"

    def should_allow_request(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            elapsed = time.time() - self.last_failure_time
            if elapsed >= self.reset_seconds:
                self.state = "half-open"
                logger.info("Circuit breaker HALF-OPEN — probing Redis")
                return True
            return False
        # half-open: allow one request through
        return True


# ─── Database ────────────────────────────────────────────────────────────────

db_pool: Optional[asyncpg.Pool] = None
redis_client: Optional[aioredis.Redis] = None
circuit_breaker = RedisCircuitBreaker(
    threshold=CIRCUIT_BREAKER_THRESHOLD,
    reset_seconds=CIRCUIT_BREAKER_RESET_SECONDS
)


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id SERIAL PRIMARY KEY,
                short_code VARCHAR(20) UNIQUE NOT NULL,
                original_url TEXT NOT NULL,
                click_count INTEGER DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                last_accessed TIMESTAMP WITH TIME ZONE
            );
            CREATE INDEX IF NOT EXISTS idx_short_code ON urls(short_code);
        """)


async def init_redis():
    global redis_client
    redis_client = aioredis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_timeout=REDIS_TIMEOUT_SECONDS,       # 1 second timeout
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS, # 1 second connect timeout
        retry_on_timeout=False                        # Don't retry — fail fast
    )


async def close_db():
    if db_pool:
        await db_pool.close()
    if redis_client:
        await redis_client.close()


# ─── Safe Redis Operations ───────────────────────────────────────────────────

async def safe_redis_get(key: str) -> Optional[str]:
    """Get from Redis with circuit breaker protection."""
    if not redis_client or not circuit_breaker.should_allow_request():
        return None
    try:
        result = await redis_client.get(key)
        circuit_breaker.record_success()
        return result
    except Exception as e:
        circuit_breaker.record_failure()
        logger.debug(f"Redis GET failed for '{key}': {e}")
        return None


async def safe_redis_setex(key: str, ttl: int, value: str) -> bool:
    """Set in Redis with circuit breaker protection."""
    if not redis_client or not circuit_breaker.should_allow_request():
        return False
    try:
        await redis_client.setex(key, ttl, value)
        circuit_breaker.record_success()
        return True
    except Exception as e:
        circuit_breaker.record_failure()
        logger.debug(f"Redis SETEX failed for '{key}': {e}")
        return False


async def safe_redis_delete(key: str) -> bool:
    """Delete from Redis with circuit breaker protection."""
    if not redis_client or not circuit_breaker.should_allow_request():
        return False
    try:
        await redis_client.delete(key)
        circuit_breaker.record_success()
        return True
    except Exception as e:
        circuit_breaker.record_failure()
        logger.debug(f"Redis DELETE failed for '{key}': {e}")
        return False


# ─── Application Lifecycle ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_redis()
    yield
    await close_db()


app = FastAPI(
    title="URL Shortener",
    description=(
        "A scalable URL shortener with Redis caching (circuit breaker protected), "
        "rate limiting, and PostgreSQL persistence."
    ),
    version="1.1.0",
    lifespan=lifespan,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def generate_short_code(url: str) -> str:
    """Generate a short code using consistent hashing (SHA-256 + base62)."""
    salt = "".join(random.choices(string.ascii_letters + string.digits, k=8))
    hash_input = f"{url}{salt}{time.time_ns()}"
    hash_digest = hashlib.sha256(hash_input.encode()).hexdigest()
    num = int(hash_digest[:12], 16)
    chars = string.ascii_letters + string.digits
    code = []
    while num and len(code) < SHORT_CODE_LENGTH:
        code.append(chars[num % 62])
        num //= 62
    return "".join(code).ljust(SHORT_CODE_LENGTH, "a")


async def rate_limit(request: Request):
    """Rate limiting using Redis with circuit breaker fallback."""
    if not redis_client or not circuit_breaker.should_allow_request():
        return  # If Redis is down, skip rate limiting (graceful degradation)

    client_ip = request.client.host if request.client else "unknown"
    key = f"rate_limit:{client_ip}"

    try:
        current = await redis_client.get(key)
        if current and int(current) >= RATE_LIMIT_MAX_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded. Max {RATE_LIMIT_MAX_REQUESTS} requests per {RATE_LIMIT_WINDOW_SECONDS}s."
            )
        pipe = redis_client.pipeline()
        pipe.incr(key)
        pipe.expire(key, RATE_LIMIT_WINDOW_SECONDS)
        await pipe.execute()
        circuit_breaker.record_success()
    except HTTPException:
        raise
    except Exception:
        circuit_breaker.record_failure()
        # Rate limiting unavailable — allow the request through


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["System"])
async def health_check():
    """Check service health including circuit breaker state."""
    db_status = "unhealthy"
    cache_status = "unhealthy"

    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "healthy"
    except Exception:
        pass

    try:
        if circuit_breaker.should_allow_request():
            await redis_client.ping()
            cache_status = "healthy"
            circuit_breaker.record_success()
        else:
            cache_status = "bypassed (circuit open)"
    except Exception:
        circuit_breaker.record_failure()
        cache_status = "unhealthy"

    status = "healthy" if db_status == "healthy" else "degraded"

    return HealthResponse(
        status=status,
        database=db_status,
        cache=cache_status,
        circuit_breaker=circuit_breaker.state,
        timestamp=datetime.now(timezone.utc).isoformat()
    )


@app.post("/shorten", response_model=URLCreateResponse, status_code=201,
          tags=["URLs"], dependencies=[Depends(rate_limit)])
async def create_short_url(payload: URLCreateRequest):
    """Create a shortened URL with optional custom code."""
    original_url = str(payload.url)
    short_code = payload.custom_code if payload.custom_code else generate_short_code(original_url)

    try:
        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT short_code, original_url FROM urls WHERE short_code = $1", short_code
            )
            if existing:
                if existing["original_url"] == original_url:
                    return URLCreateResponse(
                        short_code=short_code, short_url=f"{BASE_URL}/{short_code}",
                        original_url=original_url,
                        created_at=datetime.now(timezone.utc).isoformat()
                    )
                elif payload.custom_code:
                    raise HTTPException(status_code=409, detail=f"Custom code '{short_code}' is already taken.")
                else:
                    short_code = generate_short_code(original_url + str(time.time_ns()))

            row = await conn.fetchrow(
                "INSERT INTO urls (short_code, original_url) VALUES ($1, $2) RETURNING short_code, created_at",
                short_code, original_url
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    # Cache in Redis (non-blocking, circuit breaker protected)
    await safe_redis_setex(f"url:{short_code}", CACHE_TTL_SECONDS, original_url)

    return URLCreateResponse(
        short_code=row["short_code"], short_url=f"{BASE_URL}/{row['short_code']}",
        original_url=original_url, created_at=row["created_at"].isoformat()
    )


@app.get("/{short_code}", tags=["URLs"], dependencies=[Depends(rate_limit)])
async def redirect_to_url(short_code: str):
    """Redirect short code to original URL. Cache-first with circuit breaker fallback to DB."""

    # Try cache first (circuit breaker protected)
    cached_url = await safe_redis_get(f"url:{short_code}")
    if cached_url:
        # Update click count in DB
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE urls SET click_count = click_count + 1, last_accessed = NOW() WHERE short_code = $1",
                    short_code
                )
        except Exception:
            pass  # Click tracking is non-critical
        return RedirectResponse(url=cached_url, status_code=307)

    # Fallback to database
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE urls SET click_count = click_count + 1, last_accessed = NOW() "
                "WHERE short_code = $1 RETURNING original_url",
                short_code
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    if not row:
        raise HTTPException(status_code=404, detail=f"Short code '{short_code}' not found.")

    # Cache for future requests (non-blocking, circuit breaker protected)
    await safe_redis_setex(f"url:{short_code}", CACHE_TTL_SECONDS, row["original_url"])

    return RedirectResponse(url=row["original_url"], status_code=307)


@app.get("/stats/{short_code}", response_model=URLStatsResponse,
         tags=["URLs"], dependencies=[Depends(rate_limit)])
async def get_url_stats(short_code: str):
    """Get click statistics for a shortened URL."""
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT short_code, original_url, click_count, created_at, last_accessed "
                "FROM urls WHERE short_code = $1", short_code
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    if not row:
        raise HTTPException(status_code=404, detail=f"Short code '{short_code}' not found.")

    return URLStatsResponse(
        short_code=row["short_code"], original_url=row["original_url"],
        click_count=row["click_count"], created_at=row["created_at"].isoformat(),
        last_accessed=row["last_accessed"].isoformat() if row["last_accessed"] else None
    )


@app.delete("/urls/{short_code}", status_code=204, tags=["URLs"], dependencies=[Depends(rate_limit)])
async def delete_url(short_code: str):
    """Delete a shortened URL and evict from cache."""
    try:
        async with db_pool.acquire() as conn:
            result = await conn.execute("DELETE FROM urls WHERE short_code = $1", short_code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail=f"Short code '{short_code}' not found.")

    await safe_redis_delete(f"url:{short_code}")


# ─── Error Handlers ──────────────────────────────────────────────────────────

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "client_error" if exc.status_code < 500 else "server_error",
                 "detail": exc.detail, "status_code": exc.status_code}
    )
