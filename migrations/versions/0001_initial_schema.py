"""Create initial telemedicine data model.

Revision ID: 0001_initial_schema
Revises:
"""

from alembic import op

from app import models  # noqa: F401
from app.db import Base

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    Base.metadata.create_all(bind=bind)

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            ALTER TABLE availability_slots
            ADD CONSTRAINT ex_doctor_slots_no_overlap
            EXCLUDE USING gist (
                doctor_id WITH =,
                tstzrange(starts_at, ends_at, '[)') WITH &&
            ) WHERE (deleted_at IS NULL)
            """
        )
        op.execute(
            """
            CREATE FUNCTION reject_audit_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'audit_logs is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER audit_logs_immutable
            BEFORE UPDATE OR DELETE ON audit_logs
            FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_logs_immutable ON audit_logs")
        op.execute("DROP FUNCTION IF EXISTS reject_audit_mutation()")
        op.execute(
            "ALTER TABLE availability_slots DROP CONSTRAINT IF EXISTS ex_doctor_slots_no_overlap"
        )
    Base.metadata.drop_all(bind=bind)
