import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

SPECIALTIES = (
    "General Medicine",
    "Dermatology",
    "Cardiology",
    "Pediatrics",
    "Orthopedics",
)
LANGUAGES = ("English", "Hindi", "Bengali", "Tamil", "Marathi")
LOCATIONS = ("Mumbai", "Delhi", "Kolkata", "Chennai", "Pune")
DOCTOR_COUNT = 50
PATIENT_COUNT = 500
SLOTS_PER_DOCTOR = 200
CONTENTION_HORIZON_DAYS = 300


def main() -> None:
    if os.environ.get("APP_ENV") != "development":
        raise SystemExit("Load-test seed data is disabled unless APP_ENV=development")

    from sqlalchemy import select

    from app.core.security import create_access_token, hash_password
    from app.db import SessionLocal
    from app.models import AvailabilitySlot, Doctor, Profile, Role, SlotStatus, User

    password_hash = hash_password(secrets.token_urlsafe(32))
    now = datetime.now(UTC)
    regular_start = (now + timedelta(days=14)).replace(
        minute=((now + timedelta(days=14)).minute // 30) * 30,
        second=0,
        microsecond=0,
    )
    if regular_start <= now + timedelta(days=14):
        regular_start += timedelta(minutes=30)

    with SessionLocal.begin() as session:
        doctors = []
        for index in range(DOCTOR_COUNT):
            email = f"loadtest-doctor-{index:03}@loadtest.local"
            user = session.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(email=email, password_hash=password_hash, role=Role.doctor)
                session.add(user)
                session.flush()
            user.role = Role.doctor
            user.is_active = True
            user.deleted_at = None

            profile = session.get(Profile, user.id)
            if profile is None:
                profile = Profile(user_id=user.id, full_name=f"Load Test Doctor {index + 1}")
                session.add(profile)
            else:
                profile.full_name = f"Load Test Doctor {index + 1}"

            doctor = session.scalar(select(Doctor).where(Doctor.user_id == user.id))
            if doctor is None:
                doctor = Doctor(
                    user_id=user.id,
                    specialty=SPECIALTIES[index % len(SPECIALTIES)],
                    qualifications="Load-test profile",
                    languages=[LANGUAGES[index % len(LANGUAGES)]],
                    location=LOCATIONS[index % len(LOCATIONS)],
                    consultation_fee=500,
                    is_listed=True,
                )
                session.add(doctor)
                session.flush()
            else:
                doctor.specialty = SPECIALTIES[index % len(SPECIALTIES)]
                doctor.qualifications = "Load-test profile"
                doctor.languages = [LANGUAGES[index % len(LANGUAGES)]]
                doctor.location = LOCATIONS[index % len(LOCATIONS)]
                doctor.consultation_fee = 500
                doctor.is_listed = True
                doctor.deleted_at = None
            doctors.append(doctor)

        patients = []
        for index in range(PATIENT_COUNT):
            email = f"loadtest-patient-{index:03}@loadtest.local"
            user = session.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(email=email, password_hash=password_hash, role=Role.patient)
                session.add(user)
                session.flush()
            user.role = Role.patient
            user.is_active = True
            user.deleted_at = None

            profile = session.get(Profile, user.id)
            if profile is None:
                profile = Profile(user_id=user.id, full_name=f"Load Test Patient {index + 1}")
                session.add(profile)
            else:
                profile.full_name = f"Load Test Patient {index + 1}"
            patients.append(user)

        session.flush()

        regular_slots = []
        contention_slot = None
        contention_cutoff = now + timedelta(days=CONTENTION_HORIZON_DAYS)
        for doctor_index, doctor in enumerate(doctors):
            existing_slots = session.scalars(
                select(AvailabilitySlot)
                .where(AvailabilitySlot.doctor_id == doctor.id)
                .order_by(AvailabilitySlot.starts_at)
            ).all()
            occupied_starts = {slot.starts_at for slot in existing_slots}
            available_slots = [
                slot
                for slot in existing_slots
                if slot.status == SlotStatus.available
                and slot.deleted_at is None
                and slot.starts_at > now
                and slot.starts_at < contention_cutoff
            ][:SLOTS_PER_DOCTOR]

            candidate = regular_start
            while len(available_slots) < SLOTS_PER_DOCTOR:
                if candidate not in occupied_starts:
                    slot = AvailabilitySlot(
                        doctor_id=doctor.id,
                        starts_at=candidate,
                        ends_at=candidate + timedelta(minutes=30),
                        status=SlotStatus.available,
                    )
                    session.add(slot)
                    available_slots.append(slot)
                    occupied_starts.add(candidate)
                candidate += timedelta(minutes=30)
            regular_slots.extend(available_slots)

            if doctor_index == 0:
                available_contention_slots = [
                    slot
                    for slot in existing_slots
                    if slot.status == SlotStatus.available
                    and slot.deleted_at is None
                    and slot.starts_at > contention_cutoff
                ]
                if available_contention_slots:
                    contention_slot = available_contention_slots[0]
                else:
                    contention_start = now + timedelta(days=365)
                    contention_start = contention_start.replace(
                        minute=(contention_start.minute // 30) * 30,
                        second=0,
                        microsecond=0,
                    )
                    while contention_start in occupied_starts:
                        contention_start += timedelta(minutes=30)
                    contention_slot = AvailabilitySlot(
                        doctor_id=doctor.id,
                        starts_at=contention_start,
                        ends_at=contention_start + timedelta(minutes=30),
                        status=SlotStatus.available,
                    )
                    session.add(contention_slot)

        session.flush()
        token_data = {
            "base_url": "http://127.0.0.1:8000",
            "patients": [
                {"email": patient.email, "token": create_access_token(patient, mfa_verified=False)}
                for patient in patients
            ],
            "slot_ids": [str(slot.id) for slot in regular_slots],
            "contention_slot_id": str(contention_slot.id),
        }

    token_path = Path(__file__).with_name("tokens.json")
    token_path.write_text(json.dumps(token_data, indent=2) + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    print(
        f"Ready: {DOCTOR_COUNT} listed doctors, {len(regular_slots)} available "
        f"regular slots, {PATIENT_COUNT} patient tokens, and contention slot "
        f"{contention_slot.id}. Wrote {token_path}."
    )


if __name__ == "__main__":
    main()
