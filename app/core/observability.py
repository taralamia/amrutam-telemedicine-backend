import logging
from threading import Lock

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app.core.config import get_settings
from app.db import engine

logger = logging.getLogger("amrutam.observability")
_setup_lock = Lock()
_configured = False

_otel_meter = metrics.get_meter("amrutam.http")
_otel_http_requests = _otel_meter.create_counter(
    "amrutam.http.requests",
    description="Completed HTTP requests",
)
_otel_http_request_duration = _otel_meter.create_histogram(
    "amrutam.http.request.duration",
    unit="s",
    description="HTTP request duration in seconds",
)

HTTP_REQUESTS = Counter(
    "amrutam_http_requests_total",
    "Completed HTTP requests",
    ("method", "route", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "amrutam_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1, 2.5, 5, 10),
)


def configure_observability(app) -> None:
    global _configured
    with _setup_lock:
        if _configured:
            return
        settings = get_settings()
        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "deployment.environment": settings.app_env,
            }
        )

        trace_provider = TracerProvider(
            resource=resource,
            sampler=TraceIdRatioBased(settings.otel_trace_sample_ratio),
        )
        metric_readers = []
        if settings.otel_exporter_otlp_endpoint:
            trace_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
            )
            metric_readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=settings.otel_exporter_otlp_endpoint),
                    export_interval_millis=15_000,
                )
            )
        trace.set_tracer_provider(trace_provider)
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=metric_readers))

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="health/live,health/ready,metrics",
        )
        SQLAlchemyInstrumentor().instrument(engine=engine)
        RedisInstrumentor().instrument()
        _configured = True
        logger.info(
            "observability_configured",
            extra={
                "service_name": settings.otel_service_name,
                "otlp_export_enabled": bool(settings.otel_exporter_otlp_endpoint),
            },
        )


def prometheus_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


def record_http_request(method: str, route: str, status: int, duration_seconds: float) -> None:
    if route == "/metrics":
        return
    attributes = {
        "http.request.method": method,
        "http.route": route,
        "http.response.status_code": status,
    }
    _otel_http_requests.add(1, attributes)
    _otel_http_request_duration.record(duration_seconds, attributes)
    HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
    HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(duration_seconds)
