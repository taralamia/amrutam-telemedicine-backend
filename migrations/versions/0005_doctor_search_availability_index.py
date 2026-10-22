"""Index upcoming available slots for directory search.

Revision ID: 0005_doctor_search_availability_index
Revises: 0004_rebookable_consultation_slots
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import inspect

revision = "0005_doctor_search_availability_index"
down_revision = "0004_rebookable_consultation_slots"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_slots_available_start_doctor"
INDEX_PREDICATE = "status = 'available' AND deleted_at IS NULL"


def upgrade() -> None:
    if context.is_offline_mode():
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
            "ON availability_slots (starts_at, doctor_id) "
            f"WHERE {INDEX_PREDICATE}"
        )
        return

    indexes = {item["name"] for item in inspect(op.get_bind()).get_indexes("availability_slots")}
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "availability_slots",
            ["starts_at", "doctor_id"],
            postgresql_where=sa.text(INDEX_PREDICATE),
            sqlite_where=sa.text(INDEX_PREDICATE),
        )


def downgrade() -> None:
    if context.is_offline_mode():
        op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
        return

    indexes = {item["name"] for item in inspect(op.get_bind()).get_indexes("availability_slots")}
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="availability_slots")
