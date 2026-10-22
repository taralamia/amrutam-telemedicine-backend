import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.core.config import get_settings
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
    Profile,
    Role,
    SlotStatus,
    User,
)


@pytest.fixture
def booking_api():
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
                email="booking-patient@example.com",
                password_hash=hash_password("patient password long enough"),
                role=Role.patient,
            )
            doctor_user = User(
                email="booking-doctor@example.com",
                password_hash=hash_password("doctor password long enough"),
                role=Role.doctor,
            )
            session.add_all([patient, doctor_user])
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
            session.add_all(
                [
                    Profile(user_id=patient.id, full_name="Patient"),
                    Profile(user_id=doctor_user.id, full_name="Doctor"),
                    doctor,
                ]
            )
            session.flush()
            slot = AvailabilitySlot(
                doctor_id=doctor.id,
                starts_at=datetime.now(UTC) + timedelta(days=2),
                ends_at=datetime.now(UTC) + timedelta(days=2, minutes=30),
                status=SlotStatus.available,
            )
            session.add(slot)
            session.flush()
            token = create_access_token(patient, mfa_verified=False)
            slot_id = slot.id
        yield {
            "client": client,
            "session_factory": session_factory,
            "token": token,
            "slot_id": slot_id,
        }
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _signed_webhook(client, body: dict, *, signature_valid: bool = True):
    raw = json.dumps(body, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    secret = get_settings().payment_webhook_secret.get_secret_value().encode()
    signed_content = timestamp.encode() + b"." + raw
    signature = hmac.new(secret, signed_content, hashlib.sha256).hexdigest()
    if not signature_valid:
        signature = "0" * 64
    return client.post(
        "/v1/webhooks/payments",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature": signature,
        },
    )


def _book(client, token: str, slot_id: str, key: str):
    return client.post(
        "/v1/consultations",
        json={"availability_slot_id": slot_id},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )


def _event(booking: dict, event_id: str, event_status: str) -> dict:
    return {
        "provider_event_id": event_id,
        "payment_id": booking["payment_id"],
        "provider_reference": "provider-intent-1",
        "status": event_status,
        "amount": booking["amount"],
        "currency": booking["currency"],
    }


def test_booking_is_idempotent_and_signed_payment_confirms_atomically(booking_api):
    client = booking_api["client"]
    token = booking_api["token"]
    slot_id = str(booking_api["slot_id"])

    booked = _book(client, token, slot_id, "booking-attempt-1")
    assert booked.status_code == 201
    result = booked.json()
    assert result["status"] == "pending_payment"
    assert result["payment_status"] == "pending"
    assert _book(client, token, slot_id, "booking-attempt-1").json() == result
    assert _book(client, token, slot_id, "booking-attempt-2").status_code == 409

    with booking_api["session_factory"]() as session:
        slot = session.get(AvailabilitySlot, booking_api["slot_id"])
        assert slot.status == SlotStatus.held
        assert len(list(session.scalars(select(Consultation)))) == 1
        assert len(list(session.scalars(select(OutboxEvent)))) == 1

    event = _event(result, "provider-event-success-1", "succeeded")
    assert _signed_webhook(client, event, signature_valid=False).status_code == 401
    accepted = _signed_webhook(client, event)
    assert accepted.status_code == 200
    assert _signed_webhook(client, event).json() == accepted.json()
    conflicting_replay = event | {"amount": "1.00"}
    assert _signed_webhook(client, conflicting_replay).status_code == 409

    with booking_api["session_factory"]() as session:
        consultation = session.get(Consultation, UUID(result["consultation_id"]))
        payment = session.get(Payment, UUID(result["payment_id"]))
        slot = session.get(AvailabilitySlot, booking_api["slot_id"])
        assert consultation.status == ConsultationStatus.scheduled
        assert payment.status == PaymentStatus.succeeded
        assert slot.status == SlotStatus.booked
        assert len(list(session.scalars(select(AuditLog)))) == 2
        assert len(list(session.scalars(select(OutboxEvent)))) == 2


def test_failed_payment_releases_slot_and_late_success_does_not_steal_rebooking(booking_api):
    client = booking_api["client"]
    token = booking_api["token"]
    slot_id = str(booking_api["slot_id"])
    first = _book(client, token, slot_id, "booking-before-failure").json()

    failed_event = _event(first, "provider-event-failed-1", "failed")
    assert _signed_webhook(client, failed_event).status_code == 200
    with booking_api["session_factory"]() as session:
        assert session.get(AvailabilitySlot, booking_api["slot_id"]).status == SlotStatus.available
        assert (
            session.get(Consultation, UUID(first["consultation_id"])).status
            == ConsultationStatus.payment_failed
        )

    second_response = _book(client, token, slot_id, "booking-after-failure")
    assert second_response.status_code == 201
    second = second_response.json()
    late_success = _event(first, "provider-event-late-success-1", "succeeded")
    assert _signed_webhook(client, late_success).status_code == 200

    with booking_api["session_factory"]() as session:
        slot = session.get(AvailabilitySlot, booking_api["slot_id"])
        assert slot.status == SlotStatus.held
        assert (
            session.get(Consultation, UUID(second["consultation_id"])).status
            == ConsultationStatus.pending_payment
        )
        refund_events = list(
            session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == "payment.refund.requested")
            )
        )
        assert len(refund_events) == 1


def test_webhook_rejects_amount_mismatch_without_recording_payment(booking_api):
    result = _book(
        booking_api["client"],
        booking_api["token"],
        str(booking_api["slot_id"]),
        "booking-amount-check",
    ).json()
    event = _event(result, "provider-event-wrong-amount", "succeeded")
    event["amount"] = "1.00"
    assert _signed_webhook(booking_api["client"], event).status_code == 409

    with booking_api["session_factory"]() as session:
        assert session.get(Payment, UUID(result["payment_id"])).status == PaymentStatus.pending
        assert (
            session.scalar(
                select(OutboxEvent).where(OutboxEvent.event_type == "consultation.scheduled")
            )
            is None
        )


def test_refund_event_is_terminal_even_if_success_arrives_later(booking_api):
    result = _book(
        booking_api["client"],
        booking_api["token"],
        str(booking_api["slot_id"]),
        "booking-refund-order",
    ).json()

    refunded = _event(result, "provider-event-refunded-first", "refunded")
    assert _signed_webhook(booking_api["client"], refunded).status_code == 200
    late_success = _event(result, "provider-event-success-after-refund", "succeeded")
    assert _signed_webhook(booking_api["client"], late_success).status_code == 200

    with booking_api["session_factory"]() as session:
        assert session.get(Payment, UUID(result["payment_id"])).status == PaymentStatus.refunded
        assert (
            session.get(Consultation, UUID(result["consultation_id"])).status
            == ConsultationStatus.cancelled
        )
        assert session.get(AvailabilitySlot, booking_api["slot_id"]).status == SlotStatus.available
