import asyncio

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

import app.main as main_module
from app.api.deps import get_db
from app.core.client_ip import client_ip
from app.core.config import Settings
from app.core.rate_limit import (
    InMemoryRateLimiter,
    RateLimitBackendError,
    RedisRateLimiter,
)
from app.db import Base
from app.main import app
from app.main import settings as app_settings


def make_request(headers=None, client=("192.0.2.10", 12345)):
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [
                (name.lower().encode(), value.encode()) for name, value in (headers or {}).items()
            ],
            "client": client,
            "server": ("testserver", 80),
        }
    )


def test_client_ip_uses_rightmost_validated_forwarded_address():
    request = make_request({"X-Forwarded-For": "198.51.100.20, 203.0.113.7, 198.51.100.10"})

    assert client_ip(request, trusted_hops=1) == "198.51.100.10"
    assert client_ip(request, trusted_hops=2) == "203.0.113.7"


def test_client_ip_with_zero_trusted_hops_ignores_forwarded_header():
    request = make_request({"X-Forwarded-For": "198.51.100.10"})

    assert client_ip(request, trusted_hops=0) == "192.0.2.10"


def test_client_ip_falls_back_for_malformed_forwarded_header():
    request = make_request({"X-Forwarded-For": "198.51.100.10, not-an-ip"})

    assert client_ip(request, trusted_hops=1) == "192.0.2.10"


def test_client_ip_accepts_ipv6_forwarded_addresses():
    request = make_request({"X-Forwarded-For": "2001:db8::1, 2001:db8::2"})

    assert client_ip(request, trusted_hops=1) == "2001:db8::2"


def test_client_ip_falls_back_when_forwarded_header_has_too_few_hops():
    request = make_request({"X-Forwarded-For": "198.51.100.10"})

    assert client_ip(request, trusted_hops=2) == "192.0.2.10"


@pytest.fixture
def security_client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, class_=Session, expire_on_commit=False)

    def override_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    previous_limiter = getattr(app.state, "rate_limiter", None)
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    if previous_limiter is not None:
        app.state.rate_limiter = previous_limiter
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_auth_rate_limit_returns_429_with_retry_after(security_client):
    responses = [
        security_client.post("/v1/auth/login", json={}, headers={"Idempotency-Key": str(index)})
        for index in range(11)
    ]
    assert all(response.status_code == 422 for response in responses[:10])
    limited = responses[10]
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    assert limited.json() == {"detail": "Rate limit exceeded"}


def test_auth_rate_limits_forwarded_client_ips_independently(security_client, monkeypatch):
    monkeypatch.setattr(
        main_module,
        "settings",
        app_settings.model_copy(update={"trusted_proxy_hops": 1}),
    )

    def login(forwarded_for, request_id):
        return security_client.post(
            "/v1/auth/login",
            json={},
            headers={
                "Idempotency-Key": request_id,
                "X-Forwarded-For": forwarded_for,
            },
        )

    client_a = "198.51.100.10"
    client_b = "198.51.100.20"

    for index in range(app_settings.auth_rate_limit_per_minute):
        assert login(client_a, f"client-a-{index}").status_code == 422

    assert login(client_b, "client-b-0").status_code == 422
    spoofed_a = f"{client_b}, 203.0.113.99, {client_a}"
    assert login(spoofed_a, "client-a-spoofed").status_code == 429

    for index in range(1, app_settings.auth_rate_limit_per_minute):
        assert login(client_b, f"client-b-{index}").status_code == 422
    assert login(client_b, "client-b-limited").status_code == 429


def test_booking_writes_have_an_independent_rate_limit(security_client):
    responses = [
        security_client.post(
            "/v1/consultations",
            json={"availability_slot_id": "00000000-0000-0000-0000-000000000001"},
            headers={"Idempotency-Key": str(index)},
        )
        for index in range(app_settings.booking_rate_limit_per_minute + 1)
    ]
    assert all(response.status_code != 429 for response in responses[:-1])
    assert responses[-1].status_code == 429
    assert int(responses[-1].headers["Retry-After"]) > 0


def test_large_request_is_rejected_and_security_headers_are_set(security_client):
    oversized = security_client.post(
        "/v1/auth/login",
        content=b"x" * (1_048_577),
        headers={"Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    assert oversized.headers["X-Content-Type-Options"] == "nosniff"
    assert oversized.headers["X-Frame-Options"] == "DENY"
    assert oversized.headers["Referrer-Policy"] == "no-referrer"
    assert oversized.headers["X-Request-ID"]

    unauthorized = security_client.get("/v1/auth/me")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["Cache-Control"] == "no-store, private"


def test_protected_endpoint_fails_closed_when_rate_limit_backend_is_unavailable(
    security_client,
):
    class UnavailableLimiter:
        async def check(self, *_):
            raise RateLimitBackendError("test backend unavailable")

    app.state.rate_limiter = UnavailableLimiter()
    response = security_client.post(
        "/v1/auth/login",
        json={},
        headers={"Idempotency-Key": "backend-outage"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Request protection is temporarily unavailable"}
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_in_memory_limiter_and_redis_backend_failure_are_explicit():
    limiter = InMemoryRateLimiter()

    async def exercise_in_memory():
        assert await limiter.check("test", 1) is None
        assert await limiter.check("test", 1) > 0

    asyncio.run(exercise_in_memory())

    class UnavailableRedis:
        async def eval(self, *_):
            raise RedisConnectionError("test backend unavailable")

    async def exercise_redis_failure():
        with pytest.raises(RateLimitBackendError):
            await RedisRateLimiter(UnavailableRedis()).check("test", 1)

    asyncio.run(exercise_redis_failure())


def test_production_settings_require_distributed_rate_limit_and_trusted_host_config():
    base = {
        "app_env": "production",
        "database_url": "postgresql+psycopg://user:password@db.example/amrutam",
        "jwt_secret": "production-jwt-secret-is-random-and-unique",
        "data_encryption_key": "production-encryption-key-is-random-and-unique",
        "payment_webhook_secret": "production-payment-secret-is-random-and-unique",
    }
    with pytest.raises(ValueError, match="REDIS_URL is required"):
        Settings(**base, trusted_proxy_hops=1)
    with pytest.raises(ValueError, match="TRUSTED_HOSTS"):
        Settings(
            **base,
            redis_url="rediss://redis.example:6379/0",
            trusted_proxy_hops=1,
        )
    production_settings = {
        **base,
        "redis_url": "rediss://redis.example:6379/0",
        "trusted_hosts": "api.example.com,admin.example.com",
        "trusted_proxy_hops": 1,
    }
    with pytest.raises(ValueError, match="TRUSTED_PROXY_HOPS"):
        Settings(**{**production_settings, "trusted_proxy_hops": 0})
    settings = Settings(**production_settings)
    assert settings.trusted_host_list == ["api.example.com", "admin.example.com"]
