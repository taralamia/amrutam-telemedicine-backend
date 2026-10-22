# Booking and payment flow

## API

- `POST /v1/consultations` (patient bearer token, `Idempotency-Key`) reserves one
  available future slot. The response includes the consultation and payment IDs,
  amount, currency, and current lifecycle states.
- `POST /v1/webhooks/payments` receives signed provider status events. Configure
  `PAYMENT_WEBHOOK_SECRET`; the provider sends `X-Webhook-Timestamp` and
  `X-Webhook-Signature`, where the signature is lowercase hex HMAC-SHA256 over
  `<unix-timestamp>.<exact-raw-request-body>`. An optional `sha256=` prefix is
  accepted. Events outside the five-minute clock-skew window are rejected.

Every booking and webhook event is idempotent. A key reused with a different body
returns `409`. Webhook event IDs are the idempotency keys; duplicate events replay
the original response. Payment event amount and currency must match the persisted
payment before state can change.

## Transaction boundaries and failure behavior

The booking transaction locks and compare-and-swaps the slot, creates the
consultation and payment, appends an audit record, and writes a durable outbox
event. Any failure rolls all of those changes back. PostgreSQL row locks and the
partial unique index on active consultation states prevent two patients from
reserving the same slot. A slot released after a failed payment can be booked
again; prior failed/cancelled consultations remain for history and auditability.
Zero-fee consultations are confirmed in the same transaction without creating a
payment-provider intent.

Paid bookings remain `pending_payment` with the slot held until a verified
provider event arrives. A successful event confirms the booking only while the
consultation is still pending, the slot is still held, and the doctor remains
active and listed. If those conditions no longer hold, payment is recorded and a
refund request is put in the outbox; a late success never revives a failed or
cancelled consultation. A failed event marks the consultation `payment_failed`
and releases the slot unless another active consultation already owns it. Stale
failure events cannot revert a successful payment.

The outbox is committed with the medical and payment state and is the boundary
for payment-intent creation, refund requests, and booking notifications. The
worker claims batches with row locks and a lease, then retries failures using
capped exponential backoff with jitter. Consumers must use the outbox event ID
as their provider idempotency key; delivery is at-least-once. Provider-specific
credentials, reconciliation APIs, and notification providers are deployment
integrations and must be wired into the worker before enabling real payments.
They are intentionally not fabricated before a gateway is selected. The API
will not report a booking as paid until a verified webhook confirms the payment.

## Sequence

```mermaid
sequenceDiagram
    participant P as Patient
    participant API as API
    participant DB as PostgreSQL
    participant W as Outbox worker
    participant G as Payment gateway

    P->>API: POST consultation + Idempotency-Key
    API->>DB: lock slot, validate doctor, compare-and-swap slot
    API->>DB: insert consultation + payment + audit + payment.intent.requested
    DB-->>API: commit atomically
    API-->>P: pending_payment + payment ID
    W->>DB: claim durable payment-intent event
    W->>G: create intent (provider idempotency key)
    G-->>P: provider checkout flow
    G->>API: signed payment event
    API->>DB: verify, dedupe, update payment/consultation/slot + audit/outbox
    DB-->>API: commit atomically
    API-->>G: accepted

    par Concurrent booking attempt
        P->>API: book same slot with a different key
        API->>DB: conditional update / active-slot uniqueness check
        DB-->>API: conflict (409); no second consultation
    and Provider failure
        G->>API: signed failed event
        API->>DB: fail payment, fail consultation, release slot
    end

    Note over API,DB: A late success after release is recorded and refunded; it never rebooks the slot.
```
