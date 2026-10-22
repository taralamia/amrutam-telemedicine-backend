from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.db import Base
from app.main import app
from app.models import AvailabilitySlot, Doctor, Profile, Role, SlotStatus, User


@pytest.fixture
def doctor_search_api():
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
            entries = [
                {
                    "email": "search-cardiology@example.com",
                    "name": "Dr. Asha",
                    "specialty": "Cardiology",
                    "languages": ["English", "Hindi"],
                    "location": "Mumbai",
                    "rating": Decimal("4.80"),
                    "fee": Decimal("900.00"),
                    "listed": True,
                    "active": True,
                    "slot": now + timedelta(hours=2),
                },
                {
                    "email": "search-dermatology@example.com",
                    "name": "Dr. Bilal",
                    "specialty": "Dermatology",
                    "languages": ["English", "Urdu"],
                    "location": "Delhi",
                    "rating": Decimal("4.50"),
                    "fee": Decimal("500.00"),
                    "listed": True,
                    "active": True,
                    "slot": now + timedelta(days=1),
                },
                {
                    "email": "search-unlisted@example.com",
                    "name": "Dr. Hidden",
                    "specialty": "Cardiology",
                    "languages": ["English"],
                    "location": "Mumbai",
                    "rating": Decimal("5.00"),
                    "fee": Decimal("100.00"),
                    "listed": False,
                    "active": True,
                    "slot": now + timedelta(hours=1),
                },
                {
                    "email": "search-inactive@example.com",
                    "name": "Dr. Inactive",
                    "specialty": "Cardiology",
                    "languages": ["English"],
                    "location": "Mumbai",
                    "rating": Decimal("5.00"),
                    "fee": Decimal("100.00"),
                    "listed": True,
                    "active": False,
                    "slot": now + timedelta(hours=1),
                },
                {
                    "email": "search-unavailable@example.com",
                    "name": "Dr. No Slots",
                    "specialty": "Cardiology",
                    "languages": ["English"],
                    "location": "Mumbai",
                    "rating": Decimal("4.90"),
                    "fee": Decimal("700.00"),
                    "listed": True,
                    "active": True,
                    "slot": None,
                },
            ]
            for entry in entries:
                user = User(
                    email=entry["email"],
                    password_hash="not-used",
                    role=Role.doctor,
                    is_active=entry["active"],
                )
                session.add(user)
                session.flush()
                doctor = Doctor(
                    user_id=user.id,
                    specialty=entry["specialty"],
                    qualifications="MD",
                    languages=entry["languages"],
                    location=entry["location"],
                    rating=entry["rating"],
                    consultation_fee=entry["fee"],
                    is_listed=entry["listed"],
                )
                session.add_all([Profile(user_id=user.id, full_name=entry["name"]), doctor])
                session.flush()
                if entry["slot"] is not None:
                    session.add(
                        AvailabilitySlot(
                            doctor_id=doctor.id,
                            starts_at=entry["slot"],
                            ends_at=entry["slot"] + timedelta(minutes=30),
                            status=SlotStatus.available,
                        )
                    )
            deleted_user = User(
                email="search-deleted@example.com",
                password_hash="not-used",
                role=Role.doctor,
                is_active=True,
            )
            session.add(deleted_user)
            session.flush()
            session.add(Profile(user_id=deleted_user.id, full_name="Dr. Deleted"))
            session.add(
                Doctor(
                    user_id=deleted_user.id,
                    specialty="Cardiology",
                    qualifications="MD",
                    languages=["English"],
                    location="Mumbai",
                    rating=Decimal("5.00"),
                    consultation_fee=Decimal("100.00"),
                    is_listed=True,
                    deleted_at=now,
                )
            )
        yield client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _names(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["display_name"] for item in response.json()]


def test_search_filters_by_specialty_language_location_rating_and_fee(doctor_search_api):
    results = doctor_search_api.get(
        "/v1/doctors",
        params={
            "specialty": "cardio",
            "language": "hInDi",
            "location": "mumbai",
            "min_rating": "4.7",
            "max_fee": "1000",
        },
    )
    assert _names(results) == ["Dr. Asha"]
    result = results.json()[0]
    assert result["next_available_at"] is not None
    assert result["currency"] == "INR"


def test_search_availability_window_and_sorting_are_stable(doctor_search_api):
    available = doctor_search_api.get(
        "/v1/doctors",
        params={"available_only": "true", "sort_by": "availability", "limit": "1"},
    )
    assert _names(available) == ["Dr. Asha"]

    without_slots = doctor_search_api.get(
        "/v1/doctors",
        params={"specialty": "Cardiology", "available_only": "true"},
    )
    assert _names(without_slots) == ["Dr. Asha"]

    low_fee_first = doctor_search_api.get("/v1/doctors", params={"sort_by": "fee", "limit": "2"})
    assert _names(low_fee_first) == ["Dr. Bilal", "Dr. No Slots"]


def test_search_excludes_unlisted_inactive_and_soft_deleted_doctors(doctor_search_api):
    response = doctor_search_api.get("/v1/doctors", params={"specialty": "Cardiology"})
    names = _names(response)
    assert names == ["Dr. No Slots", "Dr. Asha"]
    assert "Dr. Hidden" not in names
    assert "Dr. Inactive" not in names
    assert "Dr. Deleted" not in names


@pytest.mark.parametrize(
    "params",
    [
        {"available_from": "2030-01-01T10:00:00"},
        {"available_from": "2030-01-02T00:00:00Z", "available_to": "2030-01-01T00:00:00Z"},
        {"sort_by": "password_hash"},
        {"limit": "101"},
    ],
)
def test_search_rejects_invalid_filters(doctor_search_api, params):
    assert doctor_search_api.get("/v1/doctors", params=params).status_code == 422
