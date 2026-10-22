"""Add optimistic doctor profile version.

Revision ID: 0003_doctor_profile_version
Revises: 0002_auth_mfa_sessions
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import inspect

revision = "0003_doctor_profile_version"
down_revision = "0002_auth_mfa_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("doctors")}
    if "version" in columns:
        return
    op.add_column("doctors", sa.Column("version", sa.Integer(), server_default="1", nullable=False))
    if connection.dialect.name == "postgresql":
        op.create_check_constraint("ck_doctors_version_positive", "doctors", "version >= 1")


def downgrade() -> None:
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    inspector = inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("doctors")}
    if "version" not in columns:
        return
    if connection.dialect.name == "postgresql":
        constraints = {item["name"] for item in inspector.get_check_constraints("doctors")}
        if "ck_doctors_version_positive" in constraints:
            op.drop_constraint("ck_doctors_version_positive", "doctors", type_="check")
    op.drop_column("doctors", "version")
