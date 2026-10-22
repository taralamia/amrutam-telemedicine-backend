from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.core.security import create_access_token, hash_password
from app.db import Base
from app.main import app
from app.models import (
    AuditLog,
    AvailabilitySlot,
    Doctor,
    Profile,
    Role,
    SlotStatus,
    User,
)


@pytest.fixture
def doctor_api():
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
            doctor_user = User(
                email="doctor@example.com",
                password_hash=hash_password("doctor password long enough"),
                role=Role.doctor,
                mfa_enabled=True,
            )
            patient = User(
                email="patient@example.com",
                password_hash=hash_password("patient password long enough"),
                role=Role.patient,
            )
            session.add_all([doctor_user, patient])
            session.flush()
            session.add_all(
                [
                    Profile(user_id=doctor_user.id, full_name="Doctor One"),
                    Profile(user_id=patient.id, full_name="Patient One"),
                ]
            )
            doctor_user_id = doctor_user.id
            patient_token = create_access_token(patient, mfa_verified=True)
            doctor_token = create_access_token(doctor_user, mfa_verified=True)
        yield {
            "client": client,
            "session_factory": session_factory,
            "doctor_user_id": doctor_user_id,
            "doctor_token": doctor_token,
            "patient_token": patient_token,
        }
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def auth_headers(token: str, key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def future_slot(offset_hours: int = 1) -> dict:
    start = (datetime.now(UTC) + timedelta(days=3, hours=offset_hours)).replace(
        minute=0, second=0, microsecond=0
    )
    return {
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(minutes=30)).isoformat(),
    }


def create_doctor_profile(client, token: str, key: str = "profile-for-slots") -> dict:
    response = client.post(
        "/v1/doctors/me/profile",
        json={
            "specialty": "General Medicine",
            "qualifications": "MD",
            "languages": ["English"],
            "consultation_fee": "500.00",
            "currency": "INR",
        },
        headers=auth_headers(token, key),
    )
    assert response.status_code == 201
    return response.json()


def test_profile_create_and_retry_does_not_duplicate_profile(doctor_api):
    client = doctor_api["client"]
    token = doctor_api["doctor_token"]
    created = client.post(
        "/v1/doctors/me/profile",
        json={
            "specialty": "Cardiology",
            "qualifications": "MD",
            "languages": ["English", "Hindi", " english "],
            "location": "Mumbai",
            "consultation_fee": "750.00",
            "currency": "INR",
        },
        headers=auth_headers(token, "doctor-profile-create-1"),
    )
    assert created.status_code == 201
    assert created.json()["specialty"] == "Cardiology"
    assert created.json()["is_listed"] is False
    assert created.json()["languages"] == ["English", "Hindi"]

    retry = client.post(
        "/v1/doctors/me/profile",
        json={
            "specialty": "Cardiology",
            "qualifications": "MD",
            "languages": ["English", "Hindi", " english "],
            "location": "Mumbai",
            "consultation_fee": "750.00",
            "currency": "INR",
        },
        headers=auth_headers(token, "doctor-profile-create-1"),
    )
    assert retry.status_code == 201
    assert retry.json()["id"] == created.json()["id"]
    with doctor_api["session_factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Doctor)) == 1


def test_profile_update_requires_current_version_and_unlists_on_directory_change(doctor_api):
    client = doctor_api["client"]
    token = doctor_api["doctor_token"]
    profile_id = create_doctor_profile(client, token)["id"]

    updated = client.patch(
        "/v1/doctors/me/profile",
        json={"version": 1, "specialty": "Neurology"},
        headers=auth_headers(token, "doctor-profile-update-1"),
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["is_listed"] is False
    assert updated.json()["id"] == profile_id

    stale = client.patch(
        "/v1/doctors/me/profile",
        json={"version": 1, "location": "Delhi"},
        headers=auth_headers(token, "doctor-profile-update-stale"),
    )
    assert stale.status_code == 409

    replay = client.patch(
        "/v1/doctors/me/profile",
        json={"version": 1, "specialty": "Neurology"},
        headers=auth_headers(token, "doctor-profile-update-1"),
    )
    assert replay.status_code == 200
    assert replay.json()["version"] == 2


def test_profile_update_rejects_null_required_fields_but_can_clear_location(doctor_api):
    client = doctor_api["client"]
    token = doctor_api["doctor_token"]
    profile = create_doctor_profile(client, token)

    rejected = client.patch(
        "/v1/doctors/me/profile",
        json={"version": profile["version"], "specialty": None},
        headers=auth_headers(token, "profile-null-specialty"),
    )
    assert rejected.status_code == 422

    cleared = client.patch(
        "/v1/doctors/me/profile",
        json={"version": profile["version"], "location": None},
        headers=auth_headers(token, "profile-clear-location"),
    )
    assert cleared.status_code == 200
    assert cleared.json()["location"] is None


def test_slots_are_idempotent_owner_scoped_and_optimistically_updated(doctor_api):
    client = doctor_api["client"]
    token = doctor_api["doctor_token"]
    create_doctor_profile(client, token)
    body = future_slot()
    headers = auth_headers(token, "slot-create-1")
    created = client.post("/v1/doctors/me/slots", json=body, headers=headers)
    assert created.status_code == 201
    slot = created.json()

    replay = client.post("/v1/doctors/me/slots", json=body, headers=headers)
    assert replay.status_code == 201
    assert replay.json()["id"] == slot["id"]

    listing = client.get(
        "/v1/doctors/me/slots",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == [slot["id"]]

    changed = client.patch(
        f"/v1/doctors/me/slots/{slot['id']}",
        json={"version": 1, "available": False},
        headers=auth_headers(token, "slot-update-1"),
    )
    assert changed.status_code == 200
    assert changed.json()["status"] == SlotStatus.unavailable.value
    assert changed.json()["version"] == 2

    stale = client.patch(
        f"/v1/doctors/me/slots/{slot['id']}",
        json={"version": 1, "available": True},
        headers=auth_headers(token, "slot-update-stale"),
    )
    assert stale.status_code == 409

    with doctor_api["session_factory"]() as session:
        assert session.scalar(select(func.count()).select_from(AuditLog)) == 3


def test_slot_requires_timezone_and_future_time(doctor_api):
    client = doctor_api["client"]
    token = doctor_api["doctor_token"]
    create_doctor_profile(client, token)
    no_timezone = {
        "starts_at": "2035-01-01T10:00:00",
        "ends_at": "2035-01-01T10:30:00",
    }
    response = client.post(
        "/v1/doctors/me/slots",
        json=no_timezone,
        headers=auth_headers(token, "slot-no-timezone"),
    )
    assert response.status_code == 422

    past = datetime.now(UTC) - timedelta(hours=2)
    response = client.post(
        "/v1/doctors/me/slots",
        json={"starts_at": past.isoformat(), "ends_at": (past + timedelta(minutes=30)).isoformat()},
        headers=auth_headers(token, "slot-past"),
    )
    assert response.status_code == 422


def test_patient_cannot_manage_doctor_slots_and_booked_slots_cannot_be_removed(doctor_api):
    client = doctor_api["client"]
    patient_headers = auth_headers(doctor_api["patient_token"], "patient-slot-create")
    response = client.post(
        "/v1/doctors/me/slots",
        json=future_slot(),
        headers=patient_headers,
    )
    assert response.status_code == 403

    with doctor_api["session_factory"].begin() as session:
        doctor = Doctor(
            user_id=doctor_api["doctor_user_id"],
            specialty="General Medicine",
            consultation_fee=Decimal("500.00"),
        )
        session.add(doctor)
        session.flush()
        doctor_id = doctor.id
        slot = AvailabilitySlot(
            id=UUID("00000000-0000-0000-0000-000000000123"),
            doctor_id=doctor_id,
            starts_at=datetime.now(UTC) + timedelta(days=4),
            ends_at=datetime.now(UTC) + timedelta(days=4, minutes=30),
            status=SlotStatus.booked,
            version=3,
        )
        session.add(slot)

    removed = client.request(
        "DELETE",
        f"/v1/doctors/me/slots/{slot.id}",
        json={"version": 3},
        headers=auth_headers(doctor_api["doctor_token"], "booked-slot-remove"),
    )
    assert removed.status_code == 409


def test_slot_can_be_soft_deleted_and_never_appears_in_doctor_schedule(doctor_api):
    client = doctor_api["client"]
    token = doctor_api["doctor_token"]
    create_doctor_profile(client, token)
    created = client.post(
        "/v1/doctors/me/slots",
        json=future_slot(),
        headers=auth_headers(token, "slot-soft-delete-create"),
    )
    assert created.status_code == 201
    slot_id = created.json()["id"]

    removed = client.request(
        "DELETE",
        f"/v1/doctors/me/slots/{slot_id}",
        json={"version": 1},
        headers=auth_headers(token, "slot-soft-delete"),
    )
    assert removed.status_code == 204
    listed = client.get(
        "/v1/doctors/me/slots",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.status_code == 200
    assert listed.json() == []
