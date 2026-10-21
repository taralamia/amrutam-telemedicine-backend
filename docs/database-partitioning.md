# Database schema and partitioning plan

## Initial schema

The first Alembic revision creates users, profiles, doctors, availability slots, consultations, prescriptions, payments, payment webhook events, audit logs, idempotency records, refresh sessions, and transactional outbox events. Models define foreign keys, uniqueness, query indexes, status enums, soft-delete timestamps, lifecycle versions, amount/rating/time checks, and JSONB for PostgreSQL. The migration adds a PostgreSQL GiST exclusion constraint so active availability intervals for one doctor cannot overlap, plus an append-only audit trigger. SQLite supports local schema and model tests but does not provide PostgreSQL-equivalent exclusion or audit-trigger guarantees.

Deploy migrations once from a migration identity with DDL privileges. Application connections must not own these tables or have schema/trigger privileges. Keep audit data free of prescription content and unnecessary PII.

## Volume strategy

100,000 consultations/day is approximately 36.5 million rows/year before associated events. Do not begin by sharding: partitioning and read replicas keep booking transactions local and operational behavior straightforward. Introduce monthly UTC range partitions for consultations, payments, and high-volume audit/event history before sustained row counts make vacuum, index maintenance, or retention impractical. This initial schema remains unpartitioned to avoid premature partitioned foreign-key and uniqueness complexity.

Before partition conversion:

1. Confirm row growth, retention, query plans, backup/restore times, and whether compliance requires indefinite audit retention.
2. Partition by `created_at` (UTC month); retain a default partition and alert when it contains rows.
3. Preserve global uniqueness/idempotency in unpartitioned registries keyed by consultation/payment identity and keep immutable audit archive references. PostgreSQL partitioned unique constraints must include partition keys, so do not assume a UUID or provider reference remains globally unique after partitioning.
4. Use an online expand/backfill/dual-write/cutover migration, validate row counts and references, then detach/archive expired partitions according to residency and retention policy.
5. Test month-boundary events, late webhook delivery, partition creation failure, and point-in-time restore before enabling the cutover.

Only consider sharding after measured primary saturation or a hard data-residency boundary. Route by residency/tenant (or stable patient home region), keep a patient's booking and payment records in the same shard, and explicitly accept that cross-shard analytics become asynchronous. Do not use Redis distributed locks for slot correctness; PostgreSQL conditional writes, uniqueness and range exclusion own that invariant.
