# Append-only audit logging

Audit records are written in the same database transaction as the state change
they describe. This keeps booking, payment webhook transitions, cancellations,
consultation lifecycle changes, prescription creation/access, and doctor
availability/profile changes from committing without their corresponding audit
entry. System-originated payment webhooks use a null actor ID and record provider
event identifiers and state/amount metadata; prescription text and other
unnecessary medical content must never be copied into audit details.

Migration `0006_immutable_audit_triggers` installs database triggers that reject
updates and deletes on `audit_logs` for both PostgreSQL and SQLite. The database
trigger protects against accidental ORM/raw-SQL mutation; production DB roles
should additionally grant the application role `INSERT` and `SELECT` on the
table while withholding `UPDATE`, `DELETE`, `TRUNCATE`, ownership, and DDL
privileges. Migration/operations credentials must be separate from the
application credentials. PostgreSQL superusers/owners can bypass ordinary role
boundaries, so privileged access requires controlled break-glass procedures and
separate infrastructure-level logging.

Audit records are retained as compliance evidence. Do not apply application
soft-delete, routine cleanup, or retention jobs to this table. For volume growth,
export/replicate audit history to a separately controlled immutable archive
before considering partitioning or retention changes; any such policy requires
compliance approval and jurisdiction-specific retention rules.
