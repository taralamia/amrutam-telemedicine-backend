# Amrutam Telemedicine Backend

FastAPI REST backend for patient, doctor, and admin workflows: authentication
and MFA, doctor profiles and availability, transactional consultation booking,
payment webhooks, consultation lifecycle, encrypted prescriptions, doctor
search, append-only audit records, and admin analytics.

The repository is a modular monolith backed by PostgreSQL. It uses Redis for
shared production rate limits, a transactional outbox for external side effects,
Alembic for schema migrations, OpenTelemetry/Prometheus instrumentation, Docker
for packaging, and an AWS Terraform reference deployment. Prescription content
and other designated sensitive fields are encrypted at the application layer
with AES-GCM before database storage.

> **Production readiness:** This codebase establishes the application and
> deployment patterns, but does not prove the targets of 100,000 consultations
> per day, p95 <200 ms reads/<500 ms writes, or 99.95% availability. Run
> workload tests and operational recovery exercises in the target AWS region.
> A real payment/notification provider adapter and deployed outbox consumer
> must be selected, implemented, and tested before accepting real payments.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/) for reproducible Python dependency management
- Docker Engine with the Compose plugin (optional, for local PostgreSQL/Redis)
- PostgreSQL for production; SQLite is suitable only for development/unit tests
- Redis for production rate limiting

## Local setup

### Run with local SQLite

```sh
uv sync --locked --extra dev
cp .env.example .env
```

Replace the placeholder values for `JWT_SECRET`,
`DATA_ENCRYPTION_KEY`, and `PAYMENT_WEBHOOK_SECRET` with different random
values at least 32 characters long. Keep `.env` local and never commit it.

```sh
DATABASE_URL='sqlite+pysqlite:///./amrutam.db' uv run alembic upgrade head
DATABASE_URL='sqlite+pysqlite:///./amrutam.db' uv run uvicorn app.main:app --reload
```

API: <http://127.0.0.1:8000>. Interactive docs: `/docs`; ReDoc: `/redoc`;
generated OpenAPI contract: [docs/openapi.json](docs/openapi.json).

### Run with PostgreSQL and Redis in Docker Compose

Compose supplies local-only PostgreSQL and Redis credentials and overrides the
URLs from `.env`. Start the services, migrate, and run the API:

```sh
cp .env.example .env
# Change the local secret placeholders in .env before starting.
docker compose up --build -d postgres redis
docker compose --profile tools run --rm migrate
docker compose up --build -d api
docker compose ps
curl --fail http://localhost:8000/health/ready
```

Stop the containers with `docker compose down`; persistent development data
remains in named volumes. `docker compose down -v` deletes those volumes and
their local database/cache data.

### Development-only sample users

Seed data is disabled unless `APP_ENV=development`, and requires a local-only
password of at least 12 characters:

```sh
SEED_PASSWORD='use-a-local-only-password' uv run python -m scripts.seed_development
```

The sample doctor is unlisted by default. Do not use these accounts or secrets
outside a local development environment. Provision doctor/admin identities
through an authenticated operator process with
`uv run python -m scripts.provision_user`; require MFA before privileged use.

## Configuration

All settings are environment variables; `.env` is read for local development.
Production secrets belong in a secret manager (the AWS reference uses Secrets
Manager), not in source control, container images, CI logs, or command history.

| Variable | Purpose |
|---|---|
| `APP_ENV` | Environment name; use `production` for production validation |
| `APP_NAME` | FastAPI service title |
| `DATABASE_URL` | SQLAlchemy URL; production must use `postgresql+psycopg://` |
| `REDIS_URL` | Shared Redis limiter; mandatory in production (`rediss://` recommended) |
| `TRUSTED_HOSTS` | Comma-separated production host allow-list; wildcard rejected in production |
| `TRUSTED_PROXY_HOPS` | Number of trusted proxy hops used to select the client IP from `X-Forwarded-For` (0–5; production requires at least 1) |
| `JWT_SECRET` | Unique random signing key, minimum 32 characters |
| `JWT_ISSUER` | Expected JWT issuer |
| `ACCESS_TOKEN_MINUTES` | Access-token lifetime (1–60 minutes) |
| `REFRESH_TOKEN_DAYS` | Refresh-session lifetime (1–365 days) |
| `DATA_ENCRYPTION_KEY` | Unique random key for encrypted identity, prescription, and replay data |
| `PAYMENT_WEBHOOK_SECRET` | Unique webhook HMAC key; mandatory in production |
| `AUTH_RATE_LIMIT_PER_MINUTE` | Auth request limit per client IP |
| `BOOKING_RATE_LIMIT_PER_MINUTE` | Consultation lifecycle write limit |
| `WEBHOOK_RATE_LIMIT_PER_MINUTE` | Payment webhook limit |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |
| `OTEL_SERVICE_NAME` | OpenTelemetry service identity |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Optional OTLP/gRPC collector endpoint |
| `OTEL_TRACE_SAMPLE_RATIO` | Trace sampling ratio from 0 to 1 |
| `SEED_PASSWORD` | Development-only seed account password; never set in production |

For a starting template, see [.env.example](.env.example). Secret rotation for
data-encryption keys requires an explicit re-encryption strategy; replacing the
key alone makes existing encrypted fields unreadable.

## Database and migrations

Apply migrations using a deployment/migration identity with DDL permissions:

```sh
uv run alembic upgrade head
uv run alembic current
```

Create a migration only after reviewing the model and migration impact:

```sh
uv run alembic revision --autogenerate -m "describe schema change"
```

Review generated SQL, constraints, indexes, downgrade behavior, and lock impact
before merging. Production deployment runs migrations once before app rollout;
use backward-compatible expand/migrate/contract changes. Do not run migrations
from every API process or use the runtime application's database role for DDL.
PostgreSQL provides the production booking and audit guarantees; SQLite tests do
not exercise every PostgreSQL constraint.

## API and lifecycle

- OpenAPI contract: [docs/openapi.json](docs/openapi.json)
- API contract and access summary: [docs/api-schema.md](docs/api-schema.md)
- Auth, registration, token rotation, and MFA: [docs/authentication.md](docs/authentication.md)
- Doctor profile and availability: [docs/doctor-availability.md](docs/doctor-availability.md)
- Booking and payment state machine: [docs/booking-and-payments.md](docs/booking-and-payments.md)
- Consultation and prescription lifecycle: [docs/consultation-lifecycle.md](docs/consultation-lifecycle.md)

All retryable writes require an `Idempotency-Key`. Payment provider events
instead use a verified timestamped signature and provider event ID. `/health/live`
is process liveness; `/health/ready` checks the database and rate-limit backend.
`/metrics` is an internal Prometheus endpoint and must not be publicly exposed.
The current AWS reference ALB forwards API paths by default; configure a
private scrape path or an ingress rule/WAF restriction for `/metrics` before
production exposure.

## Tests and code quality

```sh
uv run --locked ruff check app tests migrations
uv run --locked ruff format --check app tests migrations
uv run --locked pytest -q
```

Most tests use in-memory SQLite. The PostgreSQL race test is skipped unless
`POSTGRES_TEST_DATABASE_URL` points at a disposable PostgreSQL database already
migrated to the current Alembic head. Example:

```sh
docker compose up -d postgres
APP_ENV=test \
DATABASE_URL='postgresql+psycopg://amrutam:amrutam-local-only@localhost:5432/amrutam' \
JWT_SECRET='test-jwt-secret-long-enough-for-local-tests' \
DATA_ENCRYPTION_KEY='test-encryption-secret-long-enough-for-tests' \
PAYMENT_WEBHOOK_SECRET='test-payment-webhook-secret-long-enough' \
uv run alembic upgrade head
POSTGRES_TEST_DATABASE_URL='postgresql+psycopg://amrutam:amrutam-local-only@localhost:5432/amrutam' \
uv run pytest -q
```

Use only a disposable test database: integration fixtures create test users and
do not clean the migrated schema. CI creates a fresh PostgreSQL 16 service,
migrates it, and runs the same integration coverage. CI also lints, checks
formatting, runs tests, and builds the container.

## Deployment and operations

- [AWS deployment and release pipeline](docs/deployment.md)
- [Operational runbooks](docs/operational-runbooks.md)
- [Architecture and scaling decisions](docs/architecture.md)
- [Security checklist and threat model](docs/security-threat-model.md)
- [Database partitioning strategy](docs/database-partitioning.md)
- [Observability and alert recommendations](docs/observability.md)

The Terraform in `infra/terraform` is a reviewable AWS reference, not a
zero-configuration production deployment. Bootstrap a secure remote Terraform
state backend, review region/residency, costs, IAM, backup retention, and quotas,
then review the plan before applying it. See the runbooks for deployment,
backup/restore, rollback, incident, and scaling procedures.
