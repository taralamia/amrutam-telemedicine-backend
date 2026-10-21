import os
from datetime import UTC, datetime, timedelta

from pwdlib import PasswordHash
from sqlalchemy import select

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import AvailabilitySlot, Doctor, Profile, Role, SlotStatus, User

hasher = PasswordHash.recommended()


def main() -> None:
    if get_settings().app_env != "development":
        raise SystemExit("Development seed data is disabled outside APP_ENV=development")
    password = os.environ.get("SEED_PASSWORD", "")
    if len(password) < 12:
        raise SystemExit("Set SEED_PASSWORD to a local-only password of at least 12 characters")

    with SessionLocal.begin() as session:
        patient = session.scalar(select(User).where(User.email == "demo-patient@local.test"))
        if patient is None:
            patient = User(
                email="demo-patient@local.test",
                password_hash=hasher.hash(password),
                role=Role.patient,
            )
            session.add(patient)
            session.flush()
            session.add(Profile(user_id=patient.id, full_name="Demo Patient"))

        doctor_user = session.scalar(select(User).where(User.email == "demo-doctor@local.test"))
        if doctor_user is None:
            doctor_user = User(
                email="demo-doctor@local.test",
                password_hash=hasher.hash(password),
                role=Role.doctor,
            )
            session.add(doctor_user)
            session.flush()
            session.add(Profile(user_id=doctor_user.id, full_name="Demo Doctor"))
            doctor = Doctor(
                user_id=doctor_user.id,
                specialty="General Medicine",
                qualifications="Development-only sample",
                languages=["English"],
                location="Mumbai",
                consultation_fee=500,
                is_listed=False,
            )
            session.add(doctor)
            session.flush()
            starts_at = datetime.now(UTC) + timedelta(days=1)
            starts_at = starts_at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            session.add(
                AvailabilitySlot(
                    doctor_id=doctor.id,
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(minutes=30),
                    status=SlotStatus.available,
                )
            )
    print("Development seed accounts are ready; both use SEED_PASSWORD.")


if __name__ == "__main__":
    main()
