# Operational runbooks

These procedures are starting points for an operator with approved AWS access.
Adapt commands to the account, region, residency rules, database class, and
incident policy. Use a change ticket and peer review for production actions.
Never put credentials, PHI, prescription text, access tokens, or raw webhook
bodies in tickets or terminal transcripts.

## 1. Release and migration

1. Confirm required CI checks pass and the release is an approved commit on
   protected `master`. Review migration SQL and confirm compatibility with both
   currently running and candidate application versions.
2. Confirm PostgreSQL automated backups/PITR are healthy, the latest recovery
   point is acceptable, and there is enough database/storage headroom.
3. Confirm the GitHub OIDC trust and required repository variables are set as
   documented in [deployment.md](./deployment.md). Do not create long-lived AWS
   access keys for CI.
4. Merge the approved commit. The serialized `master` release publishes an
   immutable image, applies Alembic migrations as a one-off task, updates ECS,
   and waits for service stability.
5. Observe the Actions run, ECS deployment, ALB target health, `/health/ready`,
   error rate, latency, database/Redis health, and CloudWatch logs. Compare
   latency and 5xx rates with the pre-deploy baseline.
6. If migration fails, do not update the application service. Preserve logs,
   assess whether the migration was transactional/partially applied, and use a
   reviewed forward-repair or recovery plan; do not blindly downgrade.
7. If the new application is unhealthy but the schema remains backward
   compatible, redeploy a previous immutable image. The CI workflow currently
   deploys the commit SHA associated with a push; perform a controlled
   operator-approved task-definition rollback or revert/release procedure.
   Database rollback is not automatic.

## 2. Database backup and point-in-time restore

### Verify protection

- RDS automated backup retention is 14 days in the reference Terraform.
- Confirm automated backup status, latest restorable time, encryption, and
  deletion protection regularly. Configure AWS backup/cross-region retention
  separately if required and allowed by data-residency policy.
- Redis is a rate-limit/cache service, not a source of medical or payment
  truth. Its 7-day snapshots help operational recovery but do not replace the
  PostgreSQL backup.
- Terraform state contains generated credentials. Confirm its S3 bucket
  versioning, encryption, public-access block, lock file, and least-privilege
  access; verify that state recovery procedures are tested.

Example read-only health inspection (replace identifiers with approved values):

```sh
aws rds describe-db-instances \
  --db-instance-identifier amrutam-production-postgres \
  --query 'DBInstances[0].{Status:DBInstanceStatus,Encrypted:StorageEncrypted,Backups:BackupRetentionPeriod,Latest:LatestRestorableTime,DeletionProtection:DeletionProtection}'
```

### Restore without overwriting the source

1. Declare the incident and recovery point with the incident commander and data
   owner. Record the desired recovery timestamp in UTC and the approved RPO.
2. Restore to a **new isolated DB instance identifier**, never over the source:

   ```sh
   aws rds restore-db-instance-to-point-in-time \
     --source-db-instance-identifier amrutam-production-postgres \
     --target-db-instance-identifier amrutam-production-restore-YYYYMMDD \
     --restore-time REPLACE_WITH_UTC_RESTORE_TIMESTAMP \
     --db-instance-class db.r6g.large \
     --db-subnet-group-name amrutam-production-db \
     --vpc-security-group-ids sg-REPLACE_WITH_APPROVED_DATABASE_SG \
     --no-publicly-accessible
   ```

   Substitute a real incident timestamp, target class, subnet group, and
   approved security group; do not run the example literally.
3. Wait for the restored DB to become available. Restrict access to recovery
   operators and application tasks in a recovery environment. Confirm encryption
   and network policy before connecting.
4. Validate Alembic version, key tables/counts, a small set of approved
   integrity checks, audit trigger presence, recent booking/payment states,
   and sampled application behavior. Avoid querying or exporting unnecessary
   PHI. Reconcile payment-provider events and outbox work after the recovery
   point before reopening writes.
5. Obtain data owner approval before changing the application secret/endpoint.
   Deploy the recovery endpoint in a controlled cutover, monitor, and retain
   the original DB for investigation until the incident commander approves
   disposition.
6. Document actual recovery time/data loss, close the incident, and update RPO,
   RTO, backup, and restore tests.

RDS point-in-time restore creates a new instance; networking, parameter groups,
credentials, DNS/secrets, and application cutover require deliberate review.
This repository does not automate failover to a restored instance.

## 3. Availability, latency, or database saturation

1. Page/incident thresholds should be defined by the service owner. Check
   `/health/ready`, ALB healthy targets, ECS desired/running task counts, RDS
   CPU/connections/storage/replica lag, Redis health, and 5xx/p95 metrics.
2. Use normalized route and trace IDs to isolate latency; do not add user,
   patient, consultation, or payment identifiers as metric labels.
3. If only the API tier is saturated and downstream capacity is healthy, scale
   ECS tasks within the reviewed maximum. Do not increase DB connections or
   task count beyond the database's tested connection budget.
4. If PostgreSQL is saturated, inspect slow query plans, locks, pool pressure,
   and storage/vacuum before scaling the API. Avoid destructive query
   termination or schema changes without incident approval.
5. If Redis is unavailable, production rate-limited requests fail closed and
   readiness becomes unhealthy by design. Restore Redis service or fail over;
   do not silently switch to per-process in-memory rate limiting.
6. If the database is unavailable, do not bypass transactional booking
   guarantees. Preserve API error context, allow managed failover/recovery, and
   validate readiness before resuming writes.

## 4. Payment or outbox incident

The outbox processing module exists, but no worker/provider adapter is deployed
by the current Terraform. Treat payment intent creation, refunds, notification,
and reconciliation as unavailable until those integrations are implemented,
configured, and operationally owned.

For a suspected duplicate/missing charge or stuck payment:

1. Pause the affected provider integration using its approved control; do not
   edit consultation/payment rows directly.
2. Correlate provider event IDs and payment IDs with internal status and audit
   metadata. Avoid putting card data or webhook payloads in logs/tickets.
3. Verify webhook signatures, duplicate-event handling, amount/currency
   matching, and whether a late success correctly generated a refund request.
4. Reconcile against the provider's authoritative settlement API/dashboard.
   Execute refunds through the provider's idempotent API using the stable event
   or refund key; record the provider reference through the approved workflow.
5. Resume processing only after retry behavior and reconciliation are verified.
   Alert finance/clinical operations for unresolved cases.

## 5. Security incident and secret rotation

1. Declare an incident and restrict access to the affected account, secret,
   deployment role, or endpoint. Preserve CloudTrail, CI, load balancer, and
   application evidence under the retention policy.
2. For account compromise, disable the account and revoke refresh sessions
   using the approved admin process. Preserve audit rows.
3. For a leaked JWT or webhook key, provision replacement material in the
   secret manager, coordinate provider/application rollout, verify new
   credentials, then revoke old material. JWT signing-key rotation may invalidate
   outstanding access tokens; encryption-key rotation requires versioned
   decrypt/re-encrypt handling and must not be done by simply replacing
   `DATA_ENCRYPTION_KEY`.
4. For Terraform state exposure, restrict the state bucket and role immediately,
   inspect access logs, rotate all credentials represented in the state, and
   verify the replacement state version and recovery plan.
5. Assess PHI/PII access and notification obligations with privacy/legal owners.
   Do not copy medical records into incident channels.

## 6. Capacity, scaling, and change planning

- Use representative load tests to establish per-route p50/p95/p99, error rate,
  connection pool use, DB CPU/IOPS, Redis rate, and ECS scaling lag. Test
  booking contention, payment webhook bursts, and popular-doctor reads.
- Maintain headroom for peak traffic and an AZ/task failure. Scale stateless
  API tasks only when the database/cache can support the additional load.
- Monitor consultation/payment/outbox/audit row growth, index bloat, vacuum,
  backup time, and restore duration. Partition only after measuring operational
  need and completing the documented online migration plan in
  [database-partitioning.md](./database-partitioning.md).
- Keep booking correctness on PostgreSQL primary transactions. Read replicas
  may serve explicitly stale-tolerant reads only; never make a booking decision
  from a replica or cache.
- Review autoscaling thresholds, DB/cache class, retention, residency,
  Terraform plans, and monthly cost after load or policy changes. Terraform
  values are starting configuration, not validated capacity recommendations.

## 7. Recovery exercise cadence

At least quarterly, or on a compliance-required schedule, exercise a database
point-in-time restore in an isolated environment; measure RPO/RTO; test
application secret/endpoint cutover; validate backup encryption and access;
test a prior-image deployment; and simulate Redis unavailability and a payment
provider timeout. Record outcomes, owners, and remediation dates without
including PHI.
