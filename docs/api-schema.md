# REST API contract

The complete OpenAPI 3.1 document is [openapi.json](./openapi.json), generated
from FastAPI request/response models and route declarations. It documents the
versioned public API, request/response fields, validation constraints, bearer
authentication, and headers. `/health/live`, `/health/ready`, and `/metrics`
are operational endpoints and intentionally excluded from the public schema;
restrict readiness and metrics access at the network layer.

The document is generated rather than hand-maintained. Regenerate it whenever
routes, models, or request headers change:

```sh
APP_ENV=test \
JWT_SECRET='test-jwt-secret-long-enough-for-local-tests' \
DATA_ENCRYPTION_KEY='test-encryption-secret-long-enough-for-tests' \
PAYMENT_WEBHOOK_SECRET='test-payment-webhook-secret-long-enough' \
.venv/bin/python -c \
'import json; from app.main import app; open("docs/openapi.json", "w").write(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")'
```

The schema describes transport validation; authorization details that depend on
the authenticated database role and resource ownership are summarized here:

| API area | Access |
|---|---|
| `POST /v1/auth/register`, `/login`, `/refresh` | Public; registration creates patients only |
| `POST /v1/auth/logout`, `/mfa/setup`, `/mfa/enable`, `GET /v1/auth/me` | Authenticated user; MFA enablement requires password/code verification |
| `GET /v1/doctors` | Public directory; excludes inactive, unlisted, or deleted doctors |
| `/v1/doctors/me/profile` and `/slots` | Authenticated doctor; profile and slots are scoped to the caller |
| `POST /v1/consultations` | Patient; transactionally reserves a slot and starts payment/booking workflow |
| `GET /v1/consultations` and `/{consultation_id}` | Patient or assigned doctor, scoped to owned/assigned records |
| `POST /v1/consultations/{consultation_id}/start`, `/complete` | Assigned doctor; verified MFA and valid lifecycle transition |
| `POST /v1/consultations/{consultation_id}/cancel` | Patient, assigned doctor, or admin, subject to lifecycle rules |
| `POST` and `GET /v1/consultations/{consultation_id}/prescription` | Assigned doctor writes; patient or assigned doctor reads; verified MFA required |
| `POST /v1/webhooks/payments` | Payment provider only; timestamped HMAC signature required |
| `/v1/admin/analytics/*` | Admin role; verified MFA required |

All retryable writes require `Idempotency-Key`; reusing a key with a different
request body returns `409 Conflict`. Payment webhooks instead deduplicate on the
provider event ID and require `X-Webhook-Timestamp` and
`X-Webhook-Signature`. Common responses include `401` for missing/invalid
authentication or signature, `403` for role/MFA denial, `404` for inaccessible
resources, `409` for state/concurrency/idempotency conflicts, `422` for
validation failures, `429` for rate limits, and `503` when request protection
is unavailable. Error response details remain those emitted by each endpoint
and FastAPI validation; no separate public error envelope is currently
standardized.
