"""Enforce append-only audit records in every supported database.

Revision ID: 0006_audit_immutable
Revises: 0005_doctor_search_index
"""

from alembic import context, op

revision = "0006_audit_immutable"
down_revision = "0005_doctor_search_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = context.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION reject_audit_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'audit_logs is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute("DROP TRIGGER IF EXISTS audit_logs_immutable ON audit_logs")
        op.execute(
            """
            CREATE TRIGGER audit_logs_immutable
            BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_logs_immutable_update
            BEFORE UPDATE ON audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS audit_logs_immutable_delete
            BEFORE DELETE ON audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs is append-only');
            END
            """
        )
    else:
        raise RuntimeError(f"Audit immutability unsupported for dialect {dialect!r}")


def downgrade() -> None:
    dialect = context.get_context().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_logs_immutable ON audit_logs")
        op.execute("DROP FUNCTION IF EXISTS reject_audit_mutation()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS audit_logs_immutable_update")
        op.execute("DROP TRIGGER IF EXISTS audit_logs_immutable_delete")
    else:
        raise RuntimeError(f"Audit immutability unsupported for dialect {dialect!r}")
