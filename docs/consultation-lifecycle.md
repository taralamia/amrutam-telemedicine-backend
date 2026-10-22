# Consultation lifecycle and prescriptions

## Lifecycle API

- `GET /v1/consultations` lists the authenticated patient's or doctor's own
  consultations. Results are bounded and paginated.
- `GET /v1/consultations/{id}` returns a consultation only to its patient or
  assigned doctor.
- `POST /v1/consultations/{id}/start` and `/complete` are doctor-only,
  idempotent state transitions.
- `POST /v1/consultations/{id}/cancel` allows the patient, assigned doctor, or
  admin to cancel pending-payment or scheduled consultations. All state-changing
  routes require `Idempotency-Key`.
- `POST /v1/consultations/{id}/prescription` lets the assigned doctor create the
  single prescription for a completed consultation.
- `GET /v1/consultations/{id}/prescription` allows the patient or assigned doctor
  to read the prescription after verifying MFA.

The lifecycle permits only `scheduled -> in_progress -> completed`. Start is
rejected before the server-side scheduled time. Only pending-payment and
scheduled consultations can be cancelled; completed consultations and their
prescriptions are retained. Optimistic version predicates and database row locks
guard transitions against concurrent updates.

Prescription content uses the encrypted database column and is never copied into
audit records or outbox payloads. The unique consultation constraint prevents a
second prescription. Prescription creation is audited and writes a notification
event in the same transaction; every successful prescription read is separately
audited without logging its content. Cancelling a paid, future consultation
queues a refund request; cancelling a pending-payment consultation releases the
slot, so a later payment success is handled by the payment webhook's refund path.
