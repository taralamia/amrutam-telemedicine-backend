"""Add MFA enrollment state and refresh token families.

Revision ID: 0002_auth_mfa_sessions
Revises: 0001_initial_schema
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy import inspect

revision = "0002_auth_mfa_sessions"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    inspector = inspect(connection)
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "mfa_pending_secret_encrypted" not in user_columns:
        op.add_column(
            "users",
            sa.Column("mfa_pending_secret_encrypted", sa.Text(), nullable=True),
        )

    refresh_columns = {column["name"] for column in inspector.get_columns("refresh_sessions")}
    if "token_family_id" not in refresh_columns:
        op.add_column(
            "refresh_sessions",
            sa.Column("token_family_id", sa.Uuid(), nullable=True),
        )
        op.execute("UPDATE refresh_sessions SET token_family_id = id")
        op.alter_column("refresh_sessions", "token_family_id", nullable=False)

    index_names = {index["name"] for index in inspect(connection).get_indexes("refresh_sessions")}
    if "ix_refresh_sessions_family" not in index_names:
        op.create_index(
            "ix_refresh_sessions_family",
            "refresh_sessions",
            ["token_family_id", "revoked_at"],
        )

    if "auth_idempotency_records" not in inspect(connection).get_table_names():
        op.create_table(
            "auth_idempotency_records",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("scope", sa.String(length=100), nullable=False),
            sa.Column("key", sa.String(length=255), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("response_encrypted", sa.Text(), nullable=True),
            sa.Column("response_status", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("scope", "key", name="uq_auth_idempotency_scope_key"),
        )


def downgrade() -> None:
    connection = op.get_bind()
    inspector = inspect(connection)
    if "auth_idempotency_records" in inspector.get_table_names():
        op.drop_table("auth_idempotency_records")
    index_names = {index["name"] for index in inspect(connection).get_indexes("refresh_sessions")}
    if "ix_refresh_sessions_family" in index_names:
        op.drop_index("ix_refresh_sessions_family", table_name="refresh_sessions")
    refresh_columns = {
        column["name"] for column in inspect(connection).get_columns("refresh_sessions")
    }
    if "token_family_id" in refresh_columns:
        op.drop_column("refresh_sessions", "token_family_id")
    user_columns = {column["name"] for column in inspect(connection).get_columns("users")}
    if "mfa_pending_secret_encrypted" in user_columns:
        op.drop_column("users", "mfa_pending_secret_encrypted")
