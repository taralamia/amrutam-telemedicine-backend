"""Allow a released availability slot to be booked again.

Revision ID: 0004_rebook_slots
Revises: 0003_doctor_profile_version
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import inspect

revision = "0004_rebook_slots"
down_revision = "0003_doctor_profile_version"
branch_labels = None
depends_on = None

ACTIVE_SLOT_PREDICATE = "status NOT IN ('cancelled', 'payment_failed')"


def upgrade() -> None:
    if context.is_offline_mode():
        if context.get_context().dialect.name == "postgresql":
            op.execute("ALTER TABLE consultations DROP CONSTRAINT IF EXISTS uq_consultations_slot")
            op.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_consultations_slot_active "
                "ON consultations (availability_slot_id) "
                f"WHERE {ACTIVE_SLOT_PREDICATE}"
            )
        return

    connection = op.get_bind()
    inspector = inspect(connection)
    unique_constraints = {
        item["name"] for item in inspector.get_unique_constraints("consultations")
    }
    indexes = {item["name"] for item in inspector.get_indexes("consultations")}

    if "uq_consultations_slot" in unique_constraints:
        if connection.dialect.name == "sqlite":
            with op.batch_alter_table("consultations") as batch:
                batch.drop_constraint("uq_consultations_slot", type_="unique")
        else:
            op.drop_constraint("uq_consultations_slot", "consultations", type_="unique")

    if "uq_consultations_slot_active" not in indexes:
        op.create_index(
            "uq_consultations_slot_active",
            "consultations",
            ["availability_slot_id"],
            unique=True,
            postgresql_where=sa.text(ACTIVE_SLOT_PREDICATE),
            sqlite_where=sa.text(ACTIVE_SLOT_PREDICATE),
        )


def downgrade() -> None:
    if context.is_offline_mode():
        if context.get_context().dialect.name == "postgresql":
            op.execute("DROP INDEX IF EXISTS uq_consultations_slot_active")
            op.execute(
                "ALTER TABLE consultations "
                "ADD CONSTRAINT uq_consultations_slot UNIQUE (availability_slot_id)"
            )
        return

    connection = op.get_bind()
    inspector = inspect(connection)
    indexes = {item["name"] for item in inspector.get_indexes("consultations")}
    duplicate = connection.execute(
        sa.text(
            "SELECT availability_slot_id FROM consultations "
            "GROUP BY availability_slot_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "Cannot restore unique consultation slots while a slot has multiple consultations"
        )

    if "uq_consultations_slot_active" in indexes:
        op.drop_index("uq_consultations_slot_active", table_name="consultations")

    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("consultations") as batch:
            batch.create_unique_constraint("uq_consultations_slot", ["availability_slot_id"])
    else:
        op.create_unique_constraint(
            "uq_consultations_slot", "consultations", ["availability_slot_id"]
        )
