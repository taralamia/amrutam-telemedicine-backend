import logging

from fastapi.testclient import TestClient

from app.main import app


def test_prometheus_endpoint_exposes_bounded_route_metrics():
    with TestClient(app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        metrics = client.get("/metrics")

    assert metrics.status_code == 200
    assert "amrutam_http_requests_total" in metrics.text
    assert 'route="/health/live"' in metrics.text
    assert 'status="200"' in metrics.text
    assert "/v1/consultations/" not in metrics.text


def test_structured_logs_attach_trace_identifiers_when_span_is_active():
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    from app.core.logging import JsonFormatter

    span = NonRecordingSpan(
        SpanContext(
            trace_id=0x1234567890ABCDEF1234567890ABCDEF,
            span_id=0x1234567890ABCDEF,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    )
    token = trace.context_api.attach(trace.set_span_in_context(span))
    try:
        record = logging.LogRecord(
            name="test",
            level=20,
            pathname=__file__,
            lineno=1,
            msg="safe event",
            args=(),
            exc_info=None,
        )
        formatted = JsonFormatter().format(record)
    finally:
        trace.context_api.detach(token)

    assert '"trace_id":"1234567890abcdef1234567890abcdef"' in formatted
    assert '"span_id":"1234567890abcdef"' in formatted
