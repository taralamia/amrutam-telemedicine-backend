# Security checklist and threat model

## Scope and trust boundaries

In-scope components are the REST API, application identity, PostgreSQL, Redis,
the payment webhook boundary, outbox integrations, CI/CD, AWS infrastructure,
telemetry, and backup/state storage. Clients and third-party providers are
untrusted. Database and cache networks are private; only the ALB accepts public
traffic. CI deployment uses GitHub OIDC rather than long-lived AWS access keys.
The model assumes production uses TLS, managed encryption keys, restricted
operator access, and a supported AWS region approved for the data residency
policy.

### Data classification

| Class | Examples | Required handling |
|---|---|---|
| Public | Listed doctor specialty, location, languages, rating | Publish only while doctor and account are active/listed; avoid exposing internal identifiers beyond API need |
| PII | Email, name, phone, birth date, account/session identifiers | Least-privilege access; transport and storage encryption; no credential/token logging |
| PHI | Consultation participation/status, prescription content, medical audit metadata | Role- and participant-scoped access; MFA for prescription access; encrypted fields/storage; no response caching or telemetry/log content |
| Payment-sensitive | Amount, currency, provider reference/event, webhook signature | Never store card data; validate signature/freshness and amount/currency; restrict secrets and audit state changes |
| Secrets | JWT/data encryption/webhook keys, DB/Redis credentials, Terraform state | Secret manager, encrypted restricted state, rotation and access audit; never source-control or image-bake values |

## Threats, controls, and residual risks

| Threat | Impact | Current controls | Residual work |
|---|---|---|---|
| Credential stuffing or account takeover | PII/PHI disclosure, fraudulent booking | Argon2 password hashing, short-lived JWT, refresh-token family rotation/reuse revocation, auth rate limit, MFA enrollment hooks | Add MFA recovery/rotation policy, abuse detection and alerting; evaluate phishing-resistant MFA for admins |
| Broken object-level authorization | Cross-patient consultation or prescription access | API scopes consultation reads to patient/assigned doctor; prescription read is participant-scoped and requires verified MFA | Add recurring authorization matrix tests and external penetration testing |
| Concurrent double booking | Duplicate consultation/charge | PostgreSQL conditional versioned slot update, row locking, unique active consultation index, idempotency records | Run production-like contention/load tests and alert on booking conflicts |
| Duplicate/replayed payment webhook | Duplicate state transition or refund | HMAC over exact raw body, timestamp tolerance, constant-time signature check, event-ID deduplication, amount/currency checks | Select provider, document key rotation, verify provider event canonicalization and reconciliation |
| Provider success after cancellation/failure | Charge without valid consultation | Late success recorded; refund request outboxed; no slot reclaim | Implement provider-specific refund/reconciliation worker and operational exception queue |
| Idempotency key collision or changed request | Replayed or incorrect write | Scope+key uniqueness, canonical request hash, saved response replay, mismatch returns conflict | Define retention/expiry policy for idempotency registries before cleanup |
| SQL/state tampering or audit deletion | Loss of medical/financial evidence | Audit entry shares transaction with state change; DB triggers reject update/delete; separate application vs migration roles required | Enforce production grants that deny UPDATE/DELETE/TRUNCATE/ownership; export immutable archive; monitor privileged access |
| Request flooding/body abuse | Service exhaustion | Redis atomic rate limits fail closed, request size cap, trusted-host validation, private DB/cache subnets, ECS scaling | Configure AWS WAF/DDoS protections, tune per-account/IP limits, test load shedding and quotas |
| PHI leakage through logs/telemetry | Regulatory exposure | Structured logs omit bodies/tokens, route-template metric labels, trace sampling, no prescription body in audit/outbox | Verify collector redaction, access/retention, metrics scrape privacy, and third-party data processing agreements |
| Supply-chain or deployment compromise | Malicious image or secret exposure | Locked Python dependencies, CI tests, ECR immutable tags and scanning, GitHub OIDC branch trust, least-privilege deploy role | Pin Actions to reviewed commit SHAs, enable dependency/image vulnerability gating and provenance/signing, restrict workflow changes |
| Region loss, ransomware, operator error | Unavailability or permanent data loss | RDS Multi-AZ, encrypted backups/PITR, deletion protection, versioned remote Terraform state | Define RPO/RTO, test restore/DR, cross-region backup policy if permitted by residency, validate audit archive recovery |
| Secret disclosure from Terraform state | Credential compromise | Secrets generated outside source and stored in Secrets Manager; remote backend documented as encrypted/restricted | Bootstrap hardened backend before apply; restrict state principals; rotate credentials after suspected exposure |

## Security checklist for production

### Identity and authorization

- [x] Passwords use a memory-hard hash; access tokens are signed and short lived.
- [x] Refresh-token rotation detects reuse and revokes the family.
- [x] Server-side role checks and consultation participant checks are enforced.
- [x] MFA is available and required for protected prescription reads/writes.
- [ ] Define MFA enrollment, recovery, device change, revocation, and admin step-up policies.
- [ ] Add account lockout/credential-stuffing signals, breach response, and admin access reviews.

### Write integrity and audit

- [x] Retryable writes accept idempotency keys; conflicting key reuse is rejected.
- [x] Booking uniqueness and slot concurrency are enforced in PostgreSQL.
- [x] Payment and medical transitions write append-only audit records transactionally.
- [x] Payment callbacks validate HMAC signature, timestamp, amount, currency, and duplicate event IDs.
- [ ] Use separate runtime and migration database roles; grant runtime audit INSERT/SELECT only.
- [ ] Implement and exercise real gateway idempotency, refund handling, reconciliation, and key rotation.

### Data, infrastructure, and operations

- [x] PII/PHI fields are encrypted in application columns; managed DB/cache encryption and TLS are configured in the AWS reference.
- [x] Production requires PostgreSQL, shared Redis rate limiting, and explicit trusted hosts.
- [x] Container runs as non-root, drops capabilities, and has a read-only root filesystem.
- [x] Deployment uses a private application/data tier, HTTPS ingress, managed secrets, and OIDC federation.
- [ ] Select and document approved residency region, retention periods, lawful basis, and deletion/subject-access process.
- [ ] Validate KMS key ownership/rotation and encryption in transit to each managed service and telemetry collector.
- [ ] Configure WAF, private metrics scraping, least-privilege AWS operator roles, and break-glass alerts.
- [ ] Set and test backup RPO/RTO, restore, regional DR, incident response, and audit archive procedures.
- [ ] Load test p95/availability objectives and define SLO dashboards, paging, and error budgets.

## Incident response priorities

For suspected account compromise, revoke refresh-token families, disable the
account, preserve relevant immutable audit evidence, and investigate access
without copying PHI into incident tickets. For payment compromise, disable
provider webhook/credential access, preserve event IDs and financial metadata,
reconcile provider settlement, and use the refund workflow. For encryption key
or Terraform-state exposure, restrict access immediately, rotate dependent
credentials/keys using a staged plan, assess encrypted backups and log exposure,
and preserve cloud audit evidence. Follow jurisdiction-specific breach
notification obligations and the organization's approved incident commander
process.
