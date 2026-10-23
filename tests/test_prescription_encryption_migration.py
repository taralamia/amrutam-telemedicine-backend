import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from app.core.encryption import decrypt_text

MIGRATION_PATH = (
    Path(__file__).parents[1] / "migrations" / "versions" / "0007_encrypt_existing_prescriptions.py"
)
MIGRATION_SPEC = importlib.util.spec_from_file_location(
    "migration_0007_encrypt_existing_prescriptions", MIGRATION_PATH
)
MIGRATION = importlib.util.module_from_spec(MIGRATION_SPEC)
MIGRATION_SPEC.loader.exec_module(MIGRATION)


def test_prescription_migration_encrypts_and_downgrades_in_batches():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE prescriptions "
                    "(id TEXT PRIMARY KEY, content_encrypted TEXT NOT NULL)"
                )
            )
            connection.execute(
                text("INSERT INTO prescriptions (id, content_encrypted) VALUES (:id, :content)"),
                [
                    {"id": f"{index:04d}", "content": f"medical text {index}"}
                    for index in range(MIGRATION.BATCH_SIZE * 2 + 3)
                ],
            )

            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.upgrade()

            encrypted_rows = connection.execute(
                text("SELECT id, content_encrypted FROM prescriptions ORDER BY id")
            ).all()
            assert len(encrypted_rows) == MIGRATION.BATCH_SIZE * 2 + 3
            assert all(value.startswith("v1:") for _, value in encrypted_rows)
            assert [decrypt_text(value) for _, value in encrypted_rows] == [
                f"medical text {index}" for index in range(len(encrypted_rows))
            ]

            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.downgrade()

            plaintext_rows = connection.execute(
                text("SELECT id, content_encrypted FROM prescriptions ORDER BY id")
            ).all()
            assert plaintext_rows == [
                (f"{index:04d}", f"medical text {index}") for index in range(len(plaintext_rows))
            ]
    finally:
        engine.dispose()
