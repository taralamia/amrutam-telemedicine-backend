from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.core.security import create_access_token, hash_password
from app.db import Base
from app.main import app
from app.models import (
    AuditLog,
    AvailabilitySlot,
    Consultation,
    ConsultationStatus,
    Doctor,
    OutboxEvent,
    Payment,
    PaymentStatus,
    Prescription,
    Profile,
    Role,
    SlotStatus,
    User,
)


@pytest.fixture
def consultation_api():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, class_=Session, expire_on_commit=False)

    def override_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        with session_factory.begin() as session:
            patient = User(
                email="lifecycle-patient@example.com",
                password_hash=hash_password("patient password long enough"),
                role=Role.patient,
                mfa_enabled=True,
            )
            doctor_user = User(
                email="lifecycle-doctor@example.com",
                password_hash=hash_password("doctor password long enough"),
                role=Role.doctor,
                mfa_enabled=True,
            )
            stranger = User(
                email="lifecycle-stranger@example.com",
                password_hash=hash_password("stranger password long enough"),
                role=Role.patient,
                mfa_enabled=True,
            )
            session.add_all([patient, doctor_user, stranger])
            session.flush()
            doctor = Doctor(
                user_id=doctor_user.id,
                specialty="General Medicine",
                qualifications="MD",
                languages=["English"],
                consultation_fee=Decimal("500.00"),
                is_listed=True,
            )
            session.add_all(
                [
                    Profile(user_id=patient.id, full_name="Patient"),
                    Profile(user_id=doctor_user.id, full_name="Doctor"),
                    Profile(user_id=stranger.id, full_name="Stranger"),
                    doctor,
                ]
            )
            session.flush()
            slot = AvailabilitySlot(
                doctor_id=doctor.id,
                starts_at=datetime.now(UTC) - timedelta(minutes=5),
                ends_at=datetime.now(UTC) + timedelta(minutes=25),
                status=SlotStatus.booked,
            )
            session.add(slot)
            session.flush()
            consultation = Consultation(
                patient_id=patient.id,
                doctor_id=doctor.id,
                availability_slot_id=slot.id,
                status=ConsultationStatus.scheduled,
                starts_at=slot.starts_at,
            )
            session.add(consultation)
            session.flush()
            session.add(
                Payment(
                    consultation_id=consultation.id,
                    amount=Decimal("500.00"),
                    currency="INR",
                    status=PaymentStatus.succeeded,
                    provider_reference="lifecycle-payment-ref",
                )
            )
            session.flush()
            ids = {
                "patient_id": patient.id,
                "doctor_id": doctor.id,
                "stranger_id": stranger.id,
                "slot_id": slot.id,
                "consultation_id": consultation.id,
            }
            tokens = {
                "patient": create_access_token(patient, mfa_verified=True),
                "doctor": create_access_token(doctor_user, mfa_verified=True),
                "doctor_without_mfa": create_access_token(doctor_user, mfa_verified=False),
                "stranger": create_access_token(stranger, mfa_verified=True),
            }
        yield {"client": client, "session_factory": session_factory, "ids": ids, "tokens": tokens}
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}"}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def test_doctor_lifecycle_transitions_are_idempotent_and_audited(consultation_api):
    client = consultation_api["client"]
    consultation_id = str(consultation_api["ids"]["consultation_id"])
    doctor_token = consultation_api["tokens"]["doctor"]

    started = client.post(
        f"/v1/consultations/{consultation_id}/start",
        headers=_headers(doctor_token, "start-consult-1"),
    )
    assert started.status_code == 200
    assert started.json()["status"] == "in_progress"
    assert (
        client.post(
            f"/v1/consultations/{consultation_id}/start",
            headers=_headers(doctor_token, "start-consult-1"),
        ).json()
        == started.json()
    )
    completed = client.post(
        f"/v1/consultations/{consultation_id}/complete",
        headers=_headers(doctor_token, "complete-consult-1"),
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"

    with consultation_api["session_factory"]() as session:
        stored = session.get(Consultation, consultation_api["ids"]["consultation_id"])
        assert stored.status == ConsultationStatus.completed
        assert stored.version == 3
        assert len(list(session.scalars(select(AuditLog)))) == 2
        assert len(list(session.scalars(select(OutboxEvent)))) == 2


def test_only_participants_can_read_and_prescription_requires_completed_visit_and_mfa(
    consultation_api,
):
    client = consultation_api["client"]
    consultation_id = str(consultation_api["ids"]["consultation_id"])
    patient_token = consultation_api["tokens"]["patient"]
    doctor_token = consultation_api["tokens"]["doctor"]

    assert (
        client.get(
            f"/v1/consultations/{consultation_id}",
            headers=_headers(consultation_api["tokens"]["stranger"]),
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/consultations/{consultation_id}/prescription",
            json={"content": "Medication plan"},
            headers=_headers(doctor_token, "prescription-too-early"),
        ).status_code
        == 409
    )
    assert (
        client.get(
            f"/v1/consultations/{consultation_id}/prescription",
            headers=_headers(patient_token),
        ).status_code
        == 404
    )

    assert (
        client.post(
            f"/v1/consultations/{consultation_id}/start",
            headers=_headers(consultation_api["tokens"]["doctor_without_mfa"], "start-no-mfa"),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/v1/consultations/{consultation_id}/start",
            headers=_headers(doctor_token, "start-before-prescription"),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/v1/consultations/{consultation_id}/complete",
            headers=_headers(doctor_token, "complete-before-prescription"),
        ).status_code
        == 200
    )

    no_mfa = client.post(
        f"/v1/consultations/{consultation_id}/prescription",
        json={"content": "Medication plan"},
        headers=_headers(consultation_api["tokens"]["doctor_without_mfa"], "prescription-no-mfa"),
    )
    assert no_mfa.status_code == 403

    created = client.post(
        f"/v1/consultations/{consultation_id}/prescription",
        json={"content": "  Take medicine after meals  "},
        headers=_headers(doctor_token, "prescription-create-1"),
    )
    assert created.status_code == 201
    assert created.json()["content"] == "Take medicine after meals"
    assert (
        client.post(
            f"/v1/consultations/{consultation_id}/prescription",
            json={"content": "Take medicine after meals"},
            headers=_headers(doctor_token, "prescription-create-1"),
        ).json()
        == created.json()
    )
    assert (
        client.get(
            f"/v1/consultations/{consultation_id}/prescription",
            headers=_headers(patient_token),
        ).json()["content"]
        == "Take medicine after meals"
    )
    assert (
        client.get(
            f"/v1/consultations/{consultation_id}/prescription",
            headers=_headers(consultation_api["tokens"]["doctor_without_mfa"]),
        ).status_code
        == 403
    )

    with consultation_api["session_factory"]() as session:
        prescription = session.scalar(select(Prescription))
        assert prescription.content_encrypted == "Take medicine after meals"
        audit = list(session.scalars(select(AuditLog)))
        assert any(item.action == "prescription.accessed" for item in audit)
        assert all("Take medicine after meals" not in str(item.details) for item in audit)


def test_cancellation_releases_slot_and_queues_refund_for_paid_booking(consultation_api):
    client = consultation_api["client"]
    consultation_id = str(consultation_api["ids"]["consultation_id"])
    future_start = datetime.now(UTC) + timedelta(days=1)
    with consultation_api["session_factory"].begin() as session:
        consultation = session.get(Consultation, consultation_api["ids"]["consultation_id"])
        slot = session.get(AvailabilitySlot, consultation_api["ids"]["slot_id"])
        consultation.starts_at = future_start
        slot.starts_at = future_start
        slot.ends_at = future_start + timedelta(minutes=30)
    response = client.post(
        f"/v1/consultations/{consultation_id}/cancel",
        headers=_headers(consultation_api["tokens"]["patient"], "cancel-consult-1"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    with consultation_api["session_factory"]() as session:
        consultation = session.get(Consultation, consultation_api["ids"]["consultation_id"])
        slot = session.get(AvailabilitySlot, consultation_api["ids"]["slot_id"])
        refund = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "payment.refund.requested")
        )
        assert consultation.status == ConsultationStatus.cancelled
        assert slot.status == SlotStatus.available
        assert refund is not None
        assert len(list(session.scalars(select(AuditLog)))) == 1


def test_patient_cannot_cancel_another_users_consultation(consultation_api):
    consultation_id = str(consultation_api["ids"]["consultation_id"])
    response = consultation_api["client"].post(
        f"/v1/consultations/{consultation_id}/cancel",
        headers=_headers(consultation_api["tokens"]["stranger"], "unauthorized-cancel"),
    )
    assert response.status_code == 404
