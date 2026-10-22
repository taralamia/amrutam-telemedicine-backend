# Observability

## Signals and setup

- Structured JSON application logs include timestamp, severity, logger, request
  ID, normalized route, HTTP method, status, and elapsed request metrics.
  Sensitive request/response bodies, authorization headers, tokens, and
  prescription content are not logged. When a span is active, the JSON formatter
  adds trace and span IDs.
- OpenTelemetry instruments FastAPI server requests, SQLAlchemy database calls,
  and Redis calls. Trace sampling is controlled by
  `OTEL_TRACE_SAMPLE_RATIO` (0.1 default). Set `OTEL_SERVICE_NAME` and
  `OTEL_EXPORTER_OTLP_ENDPOINT` to export traces and periodic metrics to an
  OTLP/gRPC collector (for example, `http://otel-collector:4317`). Without an
  endpoint, tracing remains locally instrumented but is not exported.
- Prometheus HTTP counters and duration histograms are exposed at `/metrics`.
  Labels use normalized route templates, method, and status to limit cardinality.
  Infrastructure should scrape this endpoint only over a private network; do
  not expose it through public ingress.
- `/health/live` checks process liveness. `/health/ready` verifies PostgreSQL
  and the configured rate-limit backend.

## Alerts and operational thresholds

Create dashboards/alerts for request rate and p95 latency by route, 5xx rate,
database pool saturation/slow queries, Redis availability, outbox age/retries,
and application availability. Trace sampling can be increased during incident
investigation; avoid unbounded labels containing user, consultation, payment,
or other PHI/PII identifiers.

Instrumentation is not a substitute for service-side monitoring: deploy a
Prometheus-compatible scraper and OTLP collector, configure retention and
access controls, and test alert delivery as part of the deployment runbook.
