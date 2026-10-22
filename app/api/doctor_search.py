from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import exists, func, select

from app.api.deps import DbSession
from app.models import AvailabilitySlot, Doctor, Profile, SlotStatus, User
from app.schemas.doctor_search import DoctorSearchResult

router = APIRouter(prefix="/doctors", tags=["doctor search"])


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@router.get("", response_model=list[DoctorSearchResult])
def search_doctors(
    session: DbSession,
    specialty: Annotated[str | None, Query(min_length=2, max_length=120)] = None,
    location: Annotated[str | None, Query(min_length=2, max_length=160)] = None,
    language: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    min_rating: Annotated[float | None, Query(ge=0, le=5)] = None,
    max_fee: Annotated[float | None, Query(ge=0)] = None,
    available_only: bool = False,
    available_from: datetime | None = None,
    available_to: datetime | None = None,
    sort_by: Annotated[str, Query(pattern="^(rating|fee|availability)$")] = "rating",
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> list[DoctorSearchResult]:
    if available_from is not None and (
        available_from.tzinfo is None or available_from.utcoffset() is None
    ):
        raise HTTPException(status_code=422, detail="available_from must include a timezone")
    if available_to is not None and (
        available_to.tzinfo is None or available_to.utcoffset() is None
    ):
        raise HTTPException(status_code=422, detail="available_to must include a timezone")
    if available_from is not None and available_to is not None:
        if available_from.astimezone(UTC) >= available_to.astimezone(UTC):
            raise HTTPException(status_code=422, detail="available_to must be after available_from")

    now = datetime.now(UTC)
    available_slot = select(AvailabilitySlot.id).where(
        AvailabilitySlot.doctor_id == Doctor.id,
        AvailabilitySlot.status == SlotStatus.available,
        AvailabilitySlot.deleted_at.is_(None),
        AvailabilitySlot.starts_at > now,
    )
    if available_from is not None:
        available_slot = available_slot.where(
            AvailabilitySlot.starts_at >= available_from.astimezone(UTC)
        )
    if available_to is not None:
        available_slot = available_slot.where(
            AvailabilitySlot.starts_at <= available_to.astimezone(UTC)
        )
    availability_exists = exists(available_slot)

    next_availability = (
        select(AvailabilitySlot.starts_at)
        .where(
            AvailabilitySlot.doctor_id == Doctor.id,
            AvailabilitySlot.status == SlotStatus.available,
            AvailabilitySlot.deleted_at.is_(None),
            AvailabilitySlot.starts_at > now,
        )
        .order_by(AvailabilitySlot.starts_at)
        .limit(1)
        .correlate(Doctor)
        .scalar_subquery()
    )

    statement = (
        select(Doctor, Profile.full_name, next_availability.label("next_available_at"))
        .join(User, User.id == Doctor.user_id)
        .join(Profile, Profile.user_id == User.id)
        .where(
            Doctor.is_listed.is_(True),
            Doctor.deleted_at.is_(None),
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    if specialty is not None:
        statement = statement.where(Doctor.specialty.ilike(f"%{specialty.strip()}%"))
    if location is not None:
        statement = statement.where(Doctor.location.ilike(f"%{location.strip()}%"))
    if min_rating is not None:
        statement = statement.where(Doctor.rating >= min_rating)
    if max_fee is not None:
        statement = statement.where(Doctor.consultation_fee <= max_fee)
    if available_only or available_from is not None or available_to is not None:
        statement = statement.where(availability_exists)
    if language is not None:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = statement.where(Doctor.languages.contains([language.strip()]))
        elif dialect == "sqlite":
            language_values = (
                func.json_each(Doctor.languages).table_valued("value").alias("doctor_language")
            )
            statement = statement.where(
                exists(
                    select(1)
                    .select_from(language_values)
                    .where(func.lower(language_values.c.value) == language.strip().lower())
                    .correlate(Doctor)
                )
            )
        else:
            raise RuntimeError(f"Doctor language search unsupported for dialect {dialect!r}")

    if sort_by == "fee":
        statement = statement.order_by(Doctor.consultation_fee, Doctor.id)
    elif sort_by == "availability":
        statement = statement.order_by(next_availability.asc().nulls_last(), Doctor.id)
    else:
        statement = statement.order_by(Doctor.rating.desc(), Doctor.id)

    rows = session.execute(statement.limit(limit).offset(offset)).all()
    return [
        DoctorSearchResult(
            doctor_id=doctor.id,
            display_name=display_name,
            specialty=doctor.specialty,
            qualifications=doctor.qualifications,
            languages=doctor.languages,
            location=doctor.location,
            rating=doctor.rating,
            rating_count=doctor.rating_count,
            consultation_fee=doctor.consultation_fee,
            currency=doctor.currency,
            next_available_at=_utc(next_available_at),
        )
        for doctor, display_name, next_available_at in rows
    ]
