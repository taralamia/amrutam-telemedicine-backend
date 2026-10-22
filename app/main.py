import json
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import router as auth_router
from app.api.bookings import router as booking_router
from app.api.consultations import router as consultation_router
from app.api.doctors import router as doctor_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db import check_database

settings = get_settings()
configure_logging()
logger = logging.getLogger("amrutam.http")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("application_started")
    yield
    logger.info("application_stopped")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Amrutam telemedicine REST API",
    lifespan=lifespan,
)
app.include_router(auth_router, prefix="/v1")
app.include_router(doctor_router, prefix="/v1")
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
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    route = request.scope.get("route")
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


@app.get("/health/live", include_in_schema=False)
def liveness():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
def readiness():
    try:
        check_database()
    except SQLAlchemyError as exc:
        logger.error("database_readiness_failed", exc_info=exc)
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return {"status": "ready"}
