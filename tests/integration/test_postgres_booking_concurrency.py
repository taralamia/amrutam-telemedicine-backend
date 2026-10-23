"""PostgreSQL tests use a disposable database migrated to the current Alembic head."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_db
from app.core.security import create_access_token, hash_password
from app.db import Base
from app.main import app
from app.models import (
    AuditLog,
    AvailabilitySlot,
    Consultation,
    Doctor,
    OutboxEvent,
    Payment,
    Role,
    SlotStatus,
    User,
)


@pytest.fixture
def postgres_booking_api():
    database_url = os.getenv("POSTGRES_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is required for PostgreSQL integration tests")

    engine = create_engine(database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        migration = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert migration == "0006_audit_immutable"

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, class_=Session, expire_on_commit=False)

    def override_db():
        with session_factory() as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        suffix = uuid4().hex
        with session_factory.begin() as session:
            patients = [
                User(
                    email=f"concurrent-patient-{suffix}-{index}@example.com",
                    password_hash=hash_password("patient password long enough"),
                    role=Role.patient,
                )
                for index in range(2)
            ]
            doctor_user = User(
                email=f"concurrent-doctor-{suffix}@example.com",
                password_hash=hash_password("doctor password long enough"),
                role=Role.doctor,
            )
            session.add_all([*patients, doctor_user])
            session.flush()
            doctor = Doctor(
                user_id=doctor_user.id,
                specialty="General Medicine",
                qualifications="MD",
                languages=["English"],
                consultation_fee=Decimal("750.00"),
                currency="INR",
                is_listed=True,
            )
            session.add(doctor)
            session.flush()
            slot = AvailabilitySlot(
                doctor_id=doctor.id,
                starts_at=datetime.now(UTC) + timedelta(days=2),
                ends_at=datetime.now(UTC) + timedelta(days=2, minutes=30),
                status=SlotStatus.available,
            )
            session.add(slot)
            session.flush()
            patient_data = [
                (str(slot.id), create_access_token(patient, mfa_verified=False))
                for patient in patients
            ]
            slot_id = slot.id

        try:
            yield {
                "client": client,
                "session_factory": session_factory,
                "slot_id": slot_id,
                "patients": patient_data,
            }
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous_overrides)
            engine.dispose()


def test_only_one_patient_can_book_a_slot_concurrently(postgres_booking_api):
    client = postgres_booking_api["client"]
    slot_id = postgres_booking_api["slot_id"]
    patients = postgres_booking_api["patients"]
    start_requests = Barrier(2)

    def book(patient_index: int):
        body_slot_id, token = patients[patient_index]
        start_requests.wait(timeout=10)
        return client.post(
            "/v1/consultations",
            json={"availability_slot_id": body_slot_id},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"concurrent-booking-{patient_index}",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(book, range(2)))

    assert sorted(response.status_code for response in responses) == [201, 409]
    booking = next(response.json() for response in responses if response.status_code == 201)
    with postgres_booking_api["session_factory"]() as session:
        slot = session.get(AvailabilitySlot, slot_id)
        assert slot.status == SlotStatus.held
        assert (
            session.scalar(
                select(func.count())
                .select_from(Consultation)
                .where(Consultation.availability_slot_id == slot_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Payment)
                .join(Consultation)
                .where(Consultation.availability_slot_id == slot_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "consultation.booked",
                    AuditLog.entity_id == booking["consultation_id"],
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.event_type == "payment.intent.requested",
                    OutboxEvent.aggregate_id == booking["payment_id"],
                )
            )
            == 1
        )
