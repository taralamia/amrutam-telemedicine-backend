# HTTP security controls

- Auth, booking/lifecycle write, and payment-webhook requests are rate-limited
  per client IP. `TRUSTED_PROXY_HOPS` controls how the application selects that
  IP: `0` uses the socket peer and ignores `X-Forwarded-For`; values 1–5 parse
  and validate the header and select the configured entry from the right.
  Missing, malformed, or too-short headers fall back to the socket peer.
  Production uses Redis and an atomic increment/expiry script shared across
  application replicas. Redis errors fail closed with
  `503`; `/health/ready` also fails while Redis is unavailable. Development and
  tests may use a bounded process-local limiter, which is not suitable for
  production.
- Authentication, booking, and webhook rate limits are independently
  configurable with `AUTH_RATE_LIMIT_PER_MINUTE`,
  `BOOKING_RATE_LIMIT_PER_MINUTE`, and `WEBHOOK_RATE_LIMIT_PER_MINUTE`. Rejected
  requests return `429` and `Retry-After`.
- Production configuration requires `REDIS_URL`, an explicit comma-separated
  `TRUSTED_HOSTS` allow-list, and `TRUSTED_PROXY_HOPS` of at least 1. The AWS
  deployment assumes the ALB appends the connecting client IP to the
  right-hand end of `X-Forwarded-For`, so it sets `TRUSTED_PROXY_HOPS=1`.
  Forwarded headers are used only for rate-limit identity; the application
  trusts them only under this configured proxy-hop assumption. Keep ECS tasks
  reachable only from the ALB, and do not increase the hop count unless every
  added proxy is trusted and appends its peer address to the header.
- Requests are capped at 1 MiB before JSON parsing. Domain schemas apply tighter
  field-level bounds. Security response headers include `nosniff`, frame
  denial, a restrictive referrer policy, and disabled camera/microphone/location
  permissions. Authentication and prescription responses are marked
  `Cache-Control: no-store, private`.
- TLS termination, network policy, WAF/DDoS protection, and secret rotation are
  deployment controls and must be configured at the ingress/platform layer.
