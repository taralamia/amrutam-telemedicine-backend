from app import models  # noqa: F401
from app.db import Base


def test_core_and_reliability_tables_are_in_metadata():
    expected = {
        "users",
        "profiles",
        "doctors",
        "availability_slots",
        "consultations",
        "prescriptions",
        "payments",
        "audit_logs",
        "idempotency_records",
        "payment_webhook_events",
        "refresh_sessions",
        "outbox_events",
    }
    assert expected.issubset(Base.metadata.tables)


def test_booking_and_medical_uniqueness_constraints_exist():
    assert "uq_consultations_slot" in {
        constraint.name for constraint in Base.metadata.tables["consultations"].constraints
    }
    assert "uq_prescriptions_consultation" in {
        constraint.name for constraint in Base.metadata.tables["prescriptions"].constraints
    }
    assert "uq_payments_consultation" in {
        constraint.name for constraint in Base.metadata.tables["payments"].constraints
    }


def test_slots_include_search_index_and_lifecycle_version():
    slot_table = Base.metadata.tables["availability_slots"]
    columns = {column.name for column in slot_table.columns}
    assert {"status", "version", "deleted_at", "starts_at", "ends_at"} <= columns
    index_columns = {tuple(column.name for column in index.columns) for index in slot_table.indexes}
    assert ("doctor_id", "status", "starts_at") in index_columns
