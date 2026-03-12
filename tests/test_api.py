"""
Test Suite for URL Shortener
=============================
Unit and integration tests covering all API endpoints.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport

from app.main import app, generate_short_code


class MockAsyncContextManager:
    def __init__(self, conn):
        self.conn = conn
    async def __aenter__(self):
        return self.conn
    async def __aexit__(self, *args):
        pass


@pytest.fixture
def mock_conn():
    return AsyncMock()


@pytest.fixture
def mock_redis():
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)
    client.setex = AsyncMock()
    client.delete = AsyncMock()
    client.ping = AsyncMock()
    pipe = AsyncMock()
    pipe.incr = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[1, True])
    client.pipeline = MagicMock(return_value=pipe)
    return client


@pytest.fixture
async def client(mock_conn, mock_redis):
    import app.main as m
    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=MockAsyncContextManager(mock_conn))
    m.db_pool = mock_pool
    m.redis_client = mock_redis
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, mock_conn, mock_redis


class TestShortCodeGeneration:
    def test_generates_correct_length(self):
        assert len(generate_short_code("https://example.com")) == 7

    def test_generates_alphanumeric(self):
        assert generate_short_code("https://example.com").isalnum()

    def test_different_urls_different_codes(self):
        assert generate_short_code("https://a.com") != generate_short_code("https://b.com")

    def test_same_url_different_codes_due_to_salt(self):
        assert generate_short_code("https://a.com") != generate_short_code("https://a.com")


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self, client):
        ac, conn, r = client
        conn.fetchval = AsyncMock(return_value=1)
        r.ping = AsyncMock()
        resp = await ac.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_degraded_db(self, client):
        ac, conn, r = client
        conn.fetchval = AsyncMock(side_effect=Exception("down"))
        r.ping = AsyncMock()
        resp = await ac.get("/health")
        assert resp.json()["database"] == "unhealthy"


class TestCreateShortURL:
    @pytest.mark.asyncio
    async def test_success(self, client):
        ac, conn, r = client
        r.get = AsyncMock(return_value=None)
        ts = MagicMock(); ts.isoformat = MagicMock(return_value="2026-01-01T00:00:00+00:00")
        conn.fetchrow = AsyncMock(side_effect=[None, {"short_code": "abc1234", "created_at": ts}])
        resp = await ac.post("/shorten", json={"url": "https://example.com"})
        assert resp.status_code == 201
        assert "short_code" in resp.json()

    @pytest.mark.asyncio
    async def test_custom_code(self, client):
        ac, conn, r = client
        r.get = AsyncMock(return_value=None)
        ts = MagicMock(); ts.isoformat = MagicMock(return_value="2026-01-01T00:00:00+00:00")
        conn.fetchrow = AsyncMock(side_effect=[None, {"short_code": "my-link", "created_at": ts}])
        resp = await ac.post("/shorten", json={"url": "https://example.com", "custom_code": "my-link"})
        assert resp.status_code == 201
        assert resp.json()["short_code"] == "my-link"

    @pytest.mark.asyncio
    async def test_custom_code_conflict(self, client):
        ac, conn, r = client
        r.get = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(return_value={"short_code": "taken", "original_url": "https://other.com"})
        resp = await ac.post("/shorten", json={"url": "https://example.com", "custom_code": "taken"})
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_invalid_url(self, client):
        ac, conn, r = client
        resp = await ac.post("/shorten", json={"url": "not-a-url"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_existing(self, client):
        ac, conn, r = client
        r.get = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(return_value={"short_code": "exists1", "original_url": "https://example.com/"})
        resp = await ac.post("/shorten", json={"url": "https://example.com"})
        assert resp.status_code == 201
        assert resp.json()["original_url"] == "https://example.com/"


class TestRedirect:
    @pytest.mark.asyncio
    async def test_from_cache(self, client):
        ac, conn, r = client
        r.get = AsyncMock(side_effect=[None, "https://example.com"])
        conn.execute = AsyncMock()
        resp = await ac.get("/abc1234", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_from_db(self, client):
        ac, conn, r = client
        r.get = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(return_value={"original_url": "https://google.com"})
        resp = await ac.get("/xyz7890", follow_redirects=False)
        assert resp.status_code == 307

    @pytest.mark.asyncio
    async def test_not_found(self, client):
        ac, conn, r = client
        r.get = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(return_value=None)
        resp = await ac.get("/nonexist", follow_redirects=False)
        assert resp.status_code == 404


class TestStats:
    @pytest.mark.asyncio
    async def test_success(self, client):
        ac, conn, r = client
        r.get = AsyncMock(return_value=None)
        ts = MagicMock(); ts.isoformat = MagicMock(return_value="2026-01-01T00:00:00+00:00")
        la = MagicMock(); la.isoformat = MagicMock(return_value="2026-01-02T00:00:00+00:00")
        conn.fetchrow = AsyncMock(return_value={
            "short_code": "abc1234", "original_url": "https://example.com",
            "click_count": 42, "created_at": ts, "last_accessed": la
        })
        resp = await ac.get("/stats/abc1234")
        assert resp.status_code == 200
        assert resp.json()["click_count"] == 42

    @pytest.mark.asyncio
    async def test_not_found(self, client):
        ac, conn, r = client
        r.get = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(return_value=None)
        resp = await ac.get("/stats/nonexist")
        assert resp.status_code == 404


class TestDelete:
    @pytest.mark.asyncio
    async def test_success(self, client):
        ac, conn, r = client
        r.get = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="DELETE 1")
        resp = await ac.delete("/urls/abc1234")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_not_found(self, client):
        ac, conn, r = client
        r.get = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value="DELETE 0")
        resp = await ac.delete("/urls/nonexist")
        assert resp.status_code == 404
