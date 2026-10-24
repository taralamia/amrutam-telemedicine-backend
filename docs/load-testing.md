# Reproducible API load test

This k6 test measures the real `GET /v1/doctors` and authenticated
`POST /v1/consultations` routes against PostgreSQL. It also sends 100 concurrent
bookings for one slot and requires exactly one `201` and 99 `409` responses.
The test uses `APP_ENV=development` data and must not be run against production.

The defaults are 150 doctor reads/second and 10 consultation bookings/second
for two minutes. The write rate is about 10 times the average 100,000
consultations/day rate: `100,000 / 86,400 seconds/day = 1.16 consultations/s`,
so 10 bookings/s is about 8.6 times that average (roughly 10x). This is a
repeatable stress setting, not an assertion that traffic is uniform.

## Requirements and run

Use a shell with `uv`, Docker, and k6 installed ([k6 installation guide](https://grafana.com/docs/k6/latest/set-up/install-k6/)).
Run commands from the repository root. PostgreSQL 16 is started as a disposable
local container:

```sh
docker run --name amrutam-loadtest-postgres \
  -e POSTGRES_DB=amrutam_loadtest \
  -e POSTGRES_USER=amrutam \
  -e POSTGRES_PASSWORD=amrutam-local-only \
  -p 5433:5432 \
  --detach postgres:16
```

In the same shell, set the development database and local-only application
secrets, wait for PostgreSQL, and apply the current migration head:

```sh
export APP_ENV=development
export DATABASE_URL=postgresql+psycopg://amrutam:amrutam-local-only@localhost:5433/amrutam_loadtest
export JWT_SECRET=local-loadtest-jwt-secret-change-before-use-001
export DATA_ENCRYPTION_KEY=local-loadtest-encryption-secret-002
export PAYMENT_WEBHOOK_SECRET=local-loadtest-payment-secret-003
export REDIS_URL=
export TRUSTED_HOSTS=*
export ACCESS_TOKEN_MINUTES=60
export AUTH_RATE_LIMIT_PER_MINUTE=10
export BOOKING_RATE_LIMIT_PER_MINUTE=5000

until docker exec amrutam-loadtest-postgres pg_isready -U amrutam -d amrutam_loadtest; do
  sleep 1
done
uv run --locked alembic upgrade head
```

The current configuration validates `ACCESS_TOKEN_MINUTES` from 1 through 60,
`AUTH_RATE_LIMIT_PER_MINUTE` from 1 through 1,000, and
`BOOKING_RATE_LIMIT_PER_MINUTE` from 1 through 5,000. The settings above use
the booking cap so the intended test load is not masked by application
throttling. Authentication is not exercised by k6, so its limit remains at the
default of 10/minute (also within its validated cap). The app uses the
development-only in-memory limiter (`REDIS_URL` is empty).

Run the PostgreSQL integration tests against this migrated database. They must
not skip:

```sh
export POSTGRES_TEST_DATABASE_URL="$DATABASE_URL"
uv run --locked pytest -q
```

In one terminal, start one API process:

```sh
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

In another terminal, repeat the environment settings, then seed and run k6:

```sh
export APP_ENV=development
export DATABASE_URL=postgresql+psycopg://amrutam:amrutam-local-only@localhost:5433/amrutam_loadtest
export JWT_SECRET=local-loadtest-jwt-secret-change-before-use-001
export DATA_ENCRYPTION_KEY=local-loadtest-encryption-secret-002
export PAYMENT_WEBHOOK_SECRET=local-loadtest-payment-secret-003
export REDIS_URL=
export TRUSTED_HOSTS=*
export ACCESS_TOKEN_MINUTES=60
export AUTH_RATE_LIMIT_PER_MINUTE=10
export BOOKING_RATE_LIMIT_PER_MINUTE=5000

uv run --locked python -m loadtest.seed_loadtest
k6 run \
  -e BASE_URL=http://127.0.0.1:8000 \
  -e READS_PER_SECOND=150 \
  -e WRITES_PER_SECOND=10 \
  -e DURATION=2m \
  loadtest/k6_script.js
```

The script thresholds reads at p95 below 200 ms and writes at p95 below 500 ms.
It counts any read response other than `200`, any normal write response other
than `201`, and any contention response other than `201`/`409` as an unexpected
error; the global rate must remain below 1%. It separately requires exactly
one contention `201` and 99 contention `409`s. Each regular write has a unique
idempotency key and uses a different seeded slot. The token file contains
development credentials: it is git-ignored and created with owner-only
permissions.

The rates and duration can be changed with `READS_PER_SECOND`,
`WRITES_PER_SECOND`, `DURATION`, or `BASE_URL`. The seed provides 10,000 regular
slots, so raise that number by reseeding/replenishing before asking the test to
write more slots. The booking limiter must be raised only as far as the
validated cap of 5,000/minute; requests exceeding that capacity will be
reported as failures.

## Cleanup

Stop the API with Ctrl-C. Then remove the disposable database container and
local token file:

```sh
docker rm --force amrutam-loadtest-postgres
rm loadtest/tokens.json
```

The seed is safe to re-run: it reuses its stable doctor and patient accounts,
keeps already-booked slots intact, replenishes each doctor's future available
regular-slot pool to 200, and reuses an available distant-future contention
slot (or creates a replacement if the previous one was booked). It overwrites
the local token file with fresh tokens. Removing the PostgreSQL container
removes all seeded data and load-generated consultations.

## Results

The following is the final actual default-rate run on this development
machine. It failed the read/write latency, unexpected-error, and
dropped-iteration thresholds, so it is evidence that this setup did **not**
meet the targets. The contention assertion passed. Requests exhausted the
API's SQLAlchemy `QueuePool` (configured in the application with pool size 10
and max overflow 20), then timed out after 30 seconds; the k6 client also
timed out requests at 60 seconds. The test dropped 15,631 scheduled iterations
because the server could not keep up.

| Run | Reads p95 | Writes p95 | Unexpected error rate | Contention result | Machine / CPU / RAM | Versions (OS, Python, PostgreSQL, Docker, uv, k6) | Database settings |
|-----|-----------|-----------|-----------------------|-------------------|---------------------|---------------------------------------------------|-------------------|
| 150 reads/s, 10 writes/s, 2m (failed) | 1m0s (failed; threshold <200ms) | 1m0s (failed; threshold <500ms) | 90.39% (failed; threshold <1%) | 1 × 201, 99 × 409 (passed) | 11th Gen Intel Core i7-1195G7 @ 2.90GHz, 8 logical CPUs, 15 GiB RAM | Zorin OS 18.1 (Linux 7.0.0-38), Python 3.13.15, PostgreSQL 16.15, Docker 29.9.0, uv 0.12.17, k6 v2.3.0 | shared_buffers=128MB, max_connections=100, work_mem=4MB, effective_cache_size=4GB |

Machine: x86_64 Linux 7.0.0-38-generic, 8 logical CPUs, 15.39 GiB RAM;
PostgreSQL ran in Docker with no container CPU/memory limits, mapped to host
port 5433. The host already had an unrelated PostgreSQL service on port 5432.
The API was started as one uvicorn worker with
`APP_ENV=development`, `ACCESS_TOKEN_MINUTES=60`,
`AUTH_RATE_LIMIT_PER_MINUTE=10`, `BOOKING_RATE_LIMIT_PER_MINUTE=5000`,
`REDIS_URL` empty, and `TRUSTED_HOSTS=*`.

Exact k6 command used:

```text
k6 run loadtest/k6_script.js
```

Full k6 summary:

The printed summary below is from the actual run, including the
`dropped_iterations` threshold.

```text
  █ THRESHOLDS

    contention_conflicts
    ✓ 'count==99' count=99

    contention_successes
    ✓ 'count==1' count=1

    dropped_iterations
    ✗ 'count==0' count=15631

    read_latency
    ✗ 'p(95)<200' p(95)=1m0s

    unexpected_error_rate
    ✗ 'rate<0.01' rate=90.39%

    write_latency
    ✗ 'p(95)<500' p(95)=1m0s

  █ TOTAL RESULTS

    checks_total.......: 2967   19.778513/s
    checks_succeeded...: 9.60%  285 out of 2967
    checks_failed......: 90.39% 2682 out of 2967

    ✓ contention returns exactly the allowed status codes
    ✗ consultation booking returns 201
      ↳  2% — ✓ 10 / ✗ 352
    ✗ doctor search returns 200
      ↳  6% — ✓ 175 / ✗ 2330

    CUSTOM
    contention_conflicts...........: 99     0.65995/s
    contention_successes...........: 1      0.006666/s
    dropped_iterations.............: 15631  104.198832/s
    read_latency...................: avg=55.97s min=447.89ms med=1m0s max=1m0s p(90)=1m0s p(95)=1m0s
    unexpected_error_rate..........: 90.39% 2682 out of 2967
    write_latency..................: avg=58.47s min=863.86ms med=1m0s max=1m0s p(90)=1m0s p(95)=1m0s

    HTTP
    http_req_duration..............: avg=54.42s min=390.47ms med=1m0s max=1m0s p(90)=1m0s p(95)=1m0s
      { expected_response:true }...: avg=6.84s min=390.47ms med=1.49s max=33.17s p(90)=31s p(95)=31.45s
    http_req_failed................: 93.73% 2781 out of 2967
    http_reqs......................: 2967   19.778513/s

    EXECUTION
    iteration_duration.............: avg=54.42s min=391.23ms med=1m0s max=1m0s p(90)=1m0s p(95)=1m0s
    iterations.....................: 2967   19.778513/s
    vus............................: 724 min=0 max=1700
    vus_max........................: 1800 min=106 max=1800

    NETWORK
    data_received..................: 680 kB 4.5 kB/s
    data_sent......................: 802 kB 5.3 kB/s

    running (2m30.0s), 0000/1800 VUs, 2967 complete and 704 interrupted iterations
    contention ✓ [ 100% ] 100 VUs 01.5s/30s 100/100 iters, 1 per VU
    reads      ✓ [ 100% ] 0655/1500 VUs 2m0s 150.00 iters/s
    writes     ✓ [ 100% ] 049/200 VUs 2m0s 10.00 iters/s
    thresholds on metrics 'dropped_iterations, read_latency, unexpected_error_rate, write_latency' have been crossed
```

The application log reported
`sqlalchemy.exc.TimeoutError: QueuePool limit of size 10 overflow 20 reached`.
The first follow-up should inspect query/connection checkout time and session
release under concurrency, then right-size the SQLAlchemy pool and uvicorn
worker count against PostgreSQL's `max_connections` (100 here); avoid increasing
pool limits blindly. Re-run the same k6 command after any fix. A startup warning
also reported that this environment's SQLAlchemy 2.1.6 is outside the installed
OpenTelemetry SQLAlchemy instrumentor's supported `<2.1.0` range, so SQLAlchemy
instrumentation was not active during this run; align those dependency versions
before relying on its traces.

An additional retry made without restarting the already-saturated API process
returned 0 successful contention responses and 100% failed checks. Stop and
restart uvicorn, then reseed to refresh the contention slot and tokens, before
repeating a run after pool exhaustion.

One laptop and one uvicorn process are not evidence of 99.95% availability or
production capacity. The payment worker is not covered by this test; bookings
create payment records/outbox events, but payment processing is not exercised.
