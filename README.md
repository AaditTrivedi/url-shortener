# Distributed URL Shortener

A scalable URL shortener service built with **FastAPI**, **PostgreSQL**, and **Redis**, containerized with **Docker** and automated with **GitHub Actions** CI/CD.

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│              │     │              │     │              │
│    Client    │────▶│   FastAPI    │────▶│  PostgreSQL  │
│              │     │   (App)      │     │  (Storage)   │
└──────────────┘     │              │     └──────────────┘
                     │   ┌──────┐   │
                     │   │ Rate │   │
                     │   │Limit │   │
                     │   └──────┘   │
                     │              │
                     │      │       │
                     └──────┼───────┘
                            │
                     ┌──────▼───────┐
                     │              │
                     │    Redis     │
                     │   (Cache)    │
                     └──────────────┘
```

**Request Flow:**
1. Client sends a request to the FastAPI application
2. Rate limiter checks Redis for request count per IP
3. For **redirects**: Redis cache is checked first (fast path), PostgreSQL on cache miss (slow path)
4. For **URL creation**: Short code is generated via consistent hashing (SHA-256 + base62), stored in PostgreSQL, cached in Redis
5. Click counts are incremented on every redirect

## Features

- **Consistent Hashing** — SHA-256 based short code generation with base62 encoding and collision handling
- **Redis Caching** — Cache-first reads with configurable TTL, automatic cache population on miss
- **Rate Limiting** — Sliding window counter per IP using Redis (60 req/min default)
- **Custom Short Codes** — Optional user-defined aliases with conflict detection
- **Click Analytics** — Per-URL click count and last-accessed timestamp
- **Health Checks** — `/health` endpoint reporting database and cache status
- **Structured Errors** — Consistent JSON error responses with status codes
- **Containerized** — Docker + Docker Compose for one-command local deployment
- **CI/CD** — GitHub Actions running tests and building Docker images on every PR
- **OpenAPI Docs** — Auto-generated Swagger UI at `/docs`

## Tech Stack

| Component | Technology |
|-----------|-----------|
| API Framework | FastAPI (async) |
| Database | PostgreSQL 16 |
| Cache | Redis 7 |
| Containerization | Docker + Docker Compose |
| CI/CD | GitHub Actions |
| Testing | pytest + pytest-asyncio + httpx |
| Language | Python 3.12 |

## Quick Start

### Prerequisites
- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)

### Run with Docker Compose (recommended)

```bash
# Clone the repository
git clone https://github.com/aadittrivedi/url-shortener.git
cd url-shortener

# Start all services
docker compose up --build

# The API is now running at http://localhost:8000
# Swagger docs at http://localhost:8000/docs
```

### Run locally (without Docker)

```bash
# Install dependencies
pip install -r requirements.txt

# Start PostgreSQL and Redis (must be running separately)
# Set environment variables
export DATABASE_URL=postgresql://shortener:shortener@localhost:5432/shortener
export REDIS_URL=redis://localhost:6379/0

# Run the app
uvicorn app.main:app --reload
```

## API Reference

### Create Short URL

```bash
POST /shorten

# Basic usage
curl -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'

# With custom code
curl -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "custom_code": "my-link"}'
```

**Response (201):**
```json
{
  "short_code": "aBc1234",
  "short_url": "http://localhost:8000/aBc1234",
  "original_url": "https://example.com",
  "created_at": "2026-01-15T10:30:00+00:00"
}
```

### Redirect

```bash
GET /{short_code}

curl -L http://localhost:8000/aBc1234
# → 307 Redirect to https://example.com
```

### Get Stats

```bash
GET /stats/{short_code}

curl http://localhost:8000/stats/aBc1234
```

**Response (200):**
```json
{
  "short_code": "aBc1234",
  "original_url": "https://example.com",
  "click_count": 42,
  "created_at": "2026-01-15T10:30:00+00:00",
  "last_accessed": "2026-01-16T08:15:00+00:00"
}
```

### Delete URL

```bash
DELETE /urls/{short_code}

curl -X DELETE http://localhost:8000/urls/aBc1234
# → 204 No Content
```

### Health Check

```bash
GET /health

curl http://localhost:8000/health
```

**Response (200):**
```json
{
  "status": "healthy",
  "database": "healthy",
  "cache": "healthy",
  "timestamp": "2026-01-15T10:30:00+00:00"
}
```

## Running Tests

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ -v --tb=short
```

## Project Structure

```
url-shortener/
├── app/
│   ├── __init__.py
│   └── main.py              # FastAPI application, routes, models
├── tests/
│   ├── __init__.py
│   └── test_api.py           # Unit + integration tests
├── .github/
│   └── workflows/
│       └── ci.yml            # GitHub Actions CI/CD pipeline
├── Dockerfile                # Container image definition
├── docker-compose.yml        # Multi-service local deployment
├── requirements.txt          # Python dependencies
├── .gitignore
└── README.md
```

## Design Decisions

- **Consistent hashing with salt**: Each URL gets a unique short code even if the same URL is submitted multiple times. SHA-256 provides uniform distribution, and the random salt prevents predictability.
- **Cache-first reads**: Redirects check Redis before PostgreSQL. On a cache miss, the result is cached for subsequent requests. This achieves high cache hit rates under typical access patterns.
- **Sliding window rate limiting**: Redis-backed per-IP counters with TTL expiry. Simpler and more memory-efficient than token bucket for this use case.
- **Async throughout**: FastAPI + asyncpg + redis.asyncio for non-blocking I/O. The application can handle thousands of concurrent connections on a single process.
- **Graceful degradation**: If Redis is unavailable, the app falls back to PostgreSQL-only mode. Cache operations are wrapped in try/except so the service stays operational.

## License

MIT
