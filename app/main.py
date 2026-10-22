import hashlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.admin_analytics import router as admin_analytics_router
from app.api.auth import router as auth_router
from app.api.bookings import router as booking_router
from app.api.consultations import router as consultation_router
from app.api.doctor_search import router as doctor_search_router
from app.api.doctors import router as doctor_router
from app.core.config import get_settings
from app.core.http_security import MaxRequestBodySizeMiddleware, SecurityHeadersMiddleware
from app.core.logging import configure_logging
from app.core.observability import configure_observability, prometheus_payload, record_http_request
from app.core.rate_limit import (
    InMemoryRateLimiter,
    RateLimitBackendError,
    RedisRateLimiter,
)
from app.db import check_database

settings = get_settings()
configure_logging()
logger = logging.getLogger("amrutam.http")


@asynccontextmanager
async def lifespan(application: FastAPI):
    configure_observability(application)
    if settings.redis_url:
        redis_client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            health_check_interval=30,
        )
        limiter = RedisRateLimiter(redis_client)
    else:
        redis_client = None
        limiter = InMemoryRateLimiter()
    application.state.rate_limiter = limiter
    logger.info("application_started")
    try:
        yield
    finally:
        if redis_client is not None:
            await redis_client.aclose()
        logger.info("application_stopped")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Amrutam telemedicine REST API",
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list or ["*"])
app.add_middleware(MaxRequestBodySizeMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(auth_router, prefix="/v1")
app.include_router(admin_analytics_router, prefix="/v1")
app.include_router(doctor_router, prefix="/v1")
app.include_router(doctor_search_router, prefix="/v1")
app.include_router(booking_router, prefix="/v1")
app.include_router(consultation_router, prefix="/v1")


@app.middleware("http")
async def request_context(request: Request, call_next):
    supplied_id = request.headers.get("X-Request-ID")
    try:
        request_id = str(uuid.UUID(supplied_id)) if supplied_id else str(uuid.uuid4())
    except ValueError:
        request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    started_at = time.perf_counter()
    response = await _rate_limit_response(request)
    try:
        if response is None:
            response = await call_next(request)
    except Exception:
        route = request.scope.get("route")
        record_http_request(
            request.method,
            getattr(route, "path", "unmatched"),
            500,
            time.perf_counter() - started_at,
        )
        raise
    _apply_response_security_headers(response, request.url.path)
    response.headers["X-Request-ID"] = request_id
    route = request.scope.get("route")
    route_path = getattr(route, "path", "unmatched")
    record_http_request(
        request.method,
        route_path,
        response.status_code,
        time.perf_counter() - started_at,
    )
    logger.info(
        json.dumps(
            {
                "event": "http_request",
                "request_id": request_id,
                "method": request.method,
                "path": route.path if route else "unmatched",
                "status_code": response.status_code,
            },
            separators=(",", ":"),
        )
    )
    return response


def _apply_response_security_headers(response: Response, path: str) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if path.startswith("/v1/auth/") or path.endswith("/prescription"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"


async def _rate_limit_response(request: Request) -> Response | None:
    method = request.method.upper()
    path = request.url.path
    if path.startswith("/v1/auth/"):
        category = "auth"
        limit = settings.auth_rate_limit_per_minute
    elif path == "/v1/webhooks/payments":
        category = "payment-webhook"
        limit = settings.webhook_rate_limit_per_minute
    elif path.startswith("/v1/consultations") and method in {"POST", "PUT", "PATCH", "DELETE"}:
        category = "booking"
        limit = settings.booking_rate_limit_per_minute
    else:
        return None

    peer = request.client.host if request.client is not None else "unknown"
    peer_hash = hashlib.sha256(peer.encode("utf-8")).hexdigest()
    key = f"rate-limit:{category}:{peer_hash}"

    try:
        retry_after = await request.app.state.rate_limiter.check(key, limit)
    except RateLimitBackendError:
        logger.error("rate_limit_backend_unavailable", extra={"category": category})
        return JSONResponse(
            status_code=503,
            content={"detail": "Request protection is temporarily unavailable"},
        )
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded"},
            headers={"Retry-After": str(retry_after)},
        )
    return None


@app.get("/health/live", include_in_schema=False)
def liveness():
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint():
    payload, content_type = prometheus_payload()
    return Response(content=payload, media_type=content_type)


@app.get("/health/ready", include_in_schema=False)
async def readiness():
    try:
        check_database()
    except SQLAlchemyError as exc:
        logger.error("database_readiness_failed", exc_info=exc)
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    try:
        if not await app.state.rate_limiter.ping():
            raise RateLimitBackendError("Rate-limit backend ping returned false")
    except RateLimitBackendError as exc:
        logger.error("rate_limit_readiness_failed", exc_info=exc)
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return {"status": "ready"}
