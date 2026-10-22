from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.core.security import create_access_token
from app.db import Base
from app.main import app
from app.models import (
    AvailabilitySlot,
    Consultation,
    ConsultationStatus,
    Doctor,
    Payment,
    PaymentStatus,
    Profile,
    Role,
    SlotStatus,
    User,
)


@pytest.fixture
def analytics_api():
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
        now = datetime.now(UTC)
        with session_factory.begin() as session:
            admin = User(
                email="analytics-admin@example.com",
                password_hash="not-used",
                role=Role.admin,
                mfa_enabled=True,
            )
            patient = User(
                email="analytics-patient@example.com",
                password_hash="not-used",
                role=Role.patient,
                mfa_enabled=True,
            )
            doctor_user = User(
                email="analytics-doctor@example.com",
                password_hash="not-used",
                role=Role.doctor,
                mfa_enabled=True,
            )
            session.add_all([admin, patient, doctor_user])
            session.flush()
            doctor = Doctor(
                user_id=doctor_user.id,
                specialty="Cardiology",
                qualifications="MD",
                languages=["English"],
                location="Mumbai",
                consultation_fee=Decimal("500.00"),
                is_listed=True,
            )
            session.add_all(
                [
                    Profile(user_id=patient.id, full_name="Patient"),
                    Profile(user_id=doctor_user.id, full_name="Dr. Analytics"),
                    doctor,
                ]
            )
            session.flush()

            fixtures = [
                (
                    ConsultationStatus.completed,
                    SlotStatus.booked,
                    30,
                    PaymentStatus.succeeded,
                    "1000",
                ),
                (
                    ConsultationStatus.cancelled,
                    SlotStatus.available,
                    45,
                    PaymentStatus.refunded,
                    "500",
                ),
                (
                    ConsultationStatus.pending_payment,
                    SlotStatus.held,
                    60,
                    PaymentStatus.pending,
                    "200",
                ),
            ]
            for index, (consult_status, slot_status, minutes, payment_status, amount) in enumerate(
                fixtures
            ):
                starts_at = now - timedelta(days=2, hours=index)
                slot = AvailabilitySlot(
                    doctor_id=doctor.id,
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(minutes=minutes),
                    status=slot_status,
                )
                session.add(slot)
                session.flush()
                consultation = Consultation(
                    patient_id=patient.id,
                    doctor_id=doctor.id,
                    availability_slot_id=slot.id,
                    status=consult_status,
                    starts_at=starts_at,
                )
                session.add(consultation)
                session.flush()
                session.add(
                    Payment(
                        consultation_id=consultation.id,
                        amount=Decimal(amount),
                        currency="INR",
                        status=payment_status,
                    )
                )

            ids = {"admin": admin.id, "patient": patient.id, "doctor": doctor.id}
            tokens = {
                "admin": create_access_token(admin, mfa_verified=True),
                "admin_without_mfa": create_access_token(admin, mfa_verified=False),
                "patient": create_access_token(patient, mfa_verified=True),
            }
        yield {
            "client": client,
            "session_factory": session_factory,
            "ids": ids,
            "tokens": tokens,
            "start": now - timedelta(days=7),
            "end": now + timedelta(days=1),
        }
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _params(analytics_api, **overrides) -> dict[str, str]:
    params = {
        "from_date": analytics_api["start"].isoformat(),
        "to_date": analytics_api["end"].isoformat(),
    }
    params.update(overrides)
    return params


def test_admin_overview_aggregates_consultations_revenue_and_utilization(analytics_api):
    response = analytics_api["client"].get(
        "/v1/admin/analytics/overview",
        headers=_headers(analytics_api["tokens"]["admin"]),
        params=_params(analytics_api),
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["consultations"]["total"] == 3
    assert result["consultations"]["by_status"]["completed"] == 1
    assert result["consultations"]["by_status"]["cancelled"] == 1
    assert result["consultations"]["by_status"]["pending_payment"] == 1
    assert result["revenue"] == [
        {
            "currency": "INR",
            "captured_amount": "1500.00",
            "refunded_amount": "500.00",
            "net_amount": "1000.00",
        }
    ]
    assert result["utilization"]["offered_minutes"] == "135.00"
    assert result["utilization"]["utilized_minutes"] == "30.00"
    assert result["utilization"]["utilization_percent"] == "22.22"


def test_doctor_performance_aggregates_are_bounded_and_currency_safe(analytics_api):
    response = analytics_api["client"].get(
        "/v1/admin/analytics/doctor-performance",
        headers=_headers(analytics_api["tokens"]["admin"]),
        params=_params(analytics_api, limit=1),
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1
    doctor = response.json()["items"][0]
    assert doctor["doctor_id"] == str(analytics_api["ids"]["doctor"])
    assert doctor["display_name"] == "Dr. Analytics"
    assert doctor["consultations"] == 3
    assert doctor["completed"] == 1
    assert doctor["cancelled"] == 1
    assert doctor["captured_amount"] == "1500.00"
    assert doctor["refunded_amount"] == "500.00"
    assert doctor["net_amount"] == "1000.00"
    assert doctor["currency"] == "INR"


@pytest.mark.parametrize(
    "token_name,expected_status",
    [
        ("patient", 403),
        ("admin_without_mfa", 403),
    ],
)
def test_analytics_requires_admin_role_and_verified_mfa(analytics_api, token_name, expected_status):
    response = analytics_api["client"].get(
        "/v1/admin/analytics/overview",
        headers=_headers(analytics_api["tokens"][token_name]),
        params=_params(analytics_api),
    )
    assert response.status_code == expected_status


@pytest.mark.parametrize(
    "overrides",
    [
        {"from_date": "2025-01-01T00:00:00"},
        {"from_date": "2026-01-02T00:00:00Z", "to_date": "2026-01-01T00:00:00Z"},
        {"from_date": "2024-01-01T00:00:00Z", "to_date": "2025-02-02T00:00:00Z"},
    ],
)
def test_analytics_rejects_invalid_or_excessive_date_windows(analytics_api, overrides):
    response = analytics_api["client"].get(
        "/v1/admin/analytics/overview",
        headers=_headers(analytics_api["tokens"]["admin"]),
        params=_params(analytics_api, **overrides),
    )
    assert response.status_code == 422
