# HTTP security controls

- Auth, booking/lifecycle write, and payment-webhook requests are rate-limited
  per client peer address. Production uses Redis and an atomic increment/expiry
  script shared across application replicas. Redis errors fail closed with
  `503`; `/health/ready` also fails while Redis is unavailable. Development and
  tests may use a bounded process-local limiter, which is not suitable for
  production.
- Authentication, booking, and webhook rate limits are independently
  configurable with `AUTH_RATE_LIMIT_PER_MINUTE`,
  `BOOKING_RATE_LIMIT_PER_MINUTE`, and `WEBHOOK_RATE_LIMIT_PER_MINUTE`. Rejected
  requests return `429` and `Retry-After`.
- Production configuration requires `REDIS_URL` and an explicit comma-separated
  `TRUSTED_HOSTS` allow-list. Do not trust client-supplied forwarding headers.
  When deployed behind a proxy, configure the ASGI server to accept forwarded
  client addresses only from the known proxy network; otherwise limits apply to
  the direct peer address.
- Requests are capped at 1 MiB before JSON parsing. Domain schemas apply tighter
  field-level bounds. Security response headers include `nosniff`, frame
  denial, a restrictive referrer policy, and disabled camera/microphone/location
  permissions. Authentication and prescription responses are marked
  `Cache-Control: no-store, private`.
- TLS termination, network policy, WAF/DDoS protection, and secret rotation are
  deployment controls and must be configured at the ingress/platform layer.
