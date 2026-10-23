"""Encrypt prescription content already stored in the database.

Revision ID: 0007_encrypt_prescriptions
Revises: 0006_audit_immutable
"""

from collections.abc import Callable

from alembic import op
from sqlalchemy import MetaData, Table, bindparam, select

from app.core.encryption import decrypt_text, encrypt_text

revision = "0007_encrypt_prescriptions"
down_revision = "0006_audit_immutable"
branch_labels = None
depends_on = None

BATCH_SIZE = 500


def upgrade() -> None:
    _transform_prescription_content(encrypt_text)


def downgrade() -> None:
    _transform_prescription_content(decrypt_text)


def _transform_prescription_content(transform: Callable[[str], str]) -> None:
    connection = op.get_bind()
    prescriptions = Table("prescriptions", MetaData(), autoload_with=connection)
    last_id = None
    update_statement = (
        prescriptions.update()
        .where(prescriptions.c.id == bindparam("_target_id"))
        .values(content_encrypted=bindparam("_new_content"))
    )

    while True:
        statement = select(prescriptions.c.id, prescriptions.c.content_encrypted).order_by(
            prescriptions.c.id
        )
        if last_id is not None:
            statement = statement.where(prescriptions.c.id > last_id)
        rows = connection.execute(statement.limit(BATCH_SIZE)).all()
        if not rows:
            break

        connection.execute(
            update_statement,
            [
                {"_target_id": row.id, "_new_content": transform(row.content_encrypted)}
                for row in rows
            ],
        )
        last_id = rows[-1].id
