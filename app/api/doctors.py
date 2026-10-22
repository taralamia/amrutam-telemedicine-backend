from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import DbSession, require_mfa, require_roles
from app.core.audit import record_audit
from app.core.auth_idempotency import execute_auth_write
from app.models import AvailabilitySlot, Doctor, Role, SlotStatus, User
from app.schemas.doctor import (
    AvailabilitySlotCreate,
    AvailabilitySlotOut,
    AvailabilitySlotRemove,
    AvailabilitySlotUpdate,
    DoctorOwnProfileOut,
    DoctorProfileCreate,
    DoctorProfileUpdate,
)

router = APIRouter(prefix="/doctors/me", tags=["doctor availability"])
DoctorUser = Annotated[User, Depends(require_roles(Role.doctor))]


def _profile_response(doctor: Doctor) -> dict:
    return {
        "id": doctor.id,
        "user_id": doctor.user_id,
        "specialty": doctor.specialty,
        "qualifications": doctor.qualifications,
        "languages": doctor.languages,
        "location": doctor.location,
        "consultation_fee": doctor.consultation_fee,
        "currency": doctor.currency,
        "rating": doctor.rating,
        "rating_count": doctor.rating_count,
        "is_listed": doctor.is_listed,
        "version": doctor.version,
    }


def _record(
    request: Request,
    session: Session,
    user: User,
    action: str,
    entity_type: str,
    entity_id: UUID | str,
    details: dict | None = None,
) -> None:
    record_audit(
        session,
        actor_id=user.id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        request_id=getattr(request.state, "request_id", None),
    )


def _doctor_profile(session, user_id: UUID) -> Doctor:
    doctor = session.scalar(
        select(Doctor).where(Doctor.user_id == user_id, Doctor.deleted_at.is_(None))
    )
    if doctor is None:
        raise HTTPException(status_code=404, detail="Doctor profile not found")
    return doctor


@router.post(
    "/profile",
    response_model=DoctorOwnProfileOut,
    status_code=status.HTTP_201_CREATED,
)
def create_profile(
    body: DoctorProfileCreate,
    request: Request,
    session: DbSession,
    user: DoctorUser,
    _: Annotated[None, Depends(require_mfa)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation() -> tuple[int, dict]:
        existing = session.scalar(select(Doctor.id).where(Doctor.user_id == user.id))
        if existing is not None:
            raise HTTPException(status_code=409, detail="Doctor profile already exists")
        doctor = Doctor(
            user_id=user.id,
            specialty=body.specialty,
            qualifications=body.qualifications,
            languages=body.languages,
            location=body.location,
            consultation_fee=body.consultation_fee,
            currency=body.currency,
            is_listed=False,
        )
        session.add(doctor)
        session.flush()
        _record(
            request,
            session,
            user,
            "doctor.profile_created",
            "doctor",
            doctor.id,
            {"public_listing": False},
        )
        return status.HTTP_201_CREATED, _profile_response(doctor)

    _, response, _ = execute_auth_write(
        session,
        scope=f"doctor-profile-create:{user.id}",
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=operation,
    )
    return response


@router.get("/profile", response_model=DoctorOwnProfileOut)
def get_profile(session: DbSession, user: DoctorUser) -> Doctor:
    return _doctor_profile(session, user.id)


@router.patch("/profile", response_model=DoctorOwnProfileOut)
def update_profile(
    body: DoctorProfileUpdate,
    request: Request,
    session: DbSession,
    user: DoctorUser,
    _: Annotated[None, Depends(require_mfa)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation() -> tuple[int, dict]:
        doctor = _doctor_profile(session, user.id)
        changes = body.model_dump(exclude_unset=True, exclude={"version"})
        if any(key in changes for key in ("specialty", "qualifications", "languages", "location")):
            changes["is_listed"] = False
        changes["version"] = Doctor.version + 1
        result = session.execute(
            update(Doctor)
            .where(
                Doctor.id == doctor.id,
                Doctor.user_id == user.id,
                Doctor.version == body.version,
                Doctor.deleted_at.is_(None),
            )
            .values(**changes)
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="Doctor profile version is stale")
        session.flush()
        session.expire_all()
        updated = _doctor_profile(session, user.id)
        _record(
            request,
            session,
            user,
            "doctor.profile_updated",
            "doctor",
            updated.id,
            {"updated_fields": sorted(key for key in changes if key != "version")},
        )
        return status.HTTP_200_OK, _profile_response(updated)

    _, response, _ = execute_auth_write(
        session,
        scope=f"doctor-profile-update:{user.id}",
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=operation,
    )
    return response


@router.post("/slots", response_model=AvailabilitySlotOut, status_code=status.HTTP_201_CREATED)
def create_slot(
    body: AvailabilitySlotCreate,
    request: Request,
    session: DbSession,
    user: DoctorUser,
    _: Annotated[None, Depends(require_mfa)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation() -> tuple[int, dict]:
        doctor = _doctor_profile(session, user.id)
        starts_at = body.starts_at.astimezone(UTC)
        ends_at = body.ends_at.astimezone(UTC)
        if starts_at <= datetime.now(UTC):
            raise HTTPException(status_code=422, detail="Availability must start in the future")
        slot = AvailabilitySlot(
            doctor_id=doctor.id,
            starts_at=starts_at,
            ends_at=ends_at,
            status=SlotStatus.available,
        )
        session.add(slot)
        session.flush()
        _record(
            request,
            session,
            user,
            "availability_slot.created",
            "availability_slot",
            slot.id,
            {
                "doctor_id": str(doctor.id),
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
            },
        )
        return status.HTTP_201_CREATED, {
            "id": slot.id,
            "doctor_id": slot.doctor_id,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "status": slot.status.value,
            "version": slot.version,
        }

    _, response, _ = execute_auth_write(
        session,
        scope=f"doctor-slot-create:{user.id}",
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=operation,
    )
    return response


@router.get("/slots", response_model=list[AvailabilitySlotOut])
def list_slots(
    session: DbSession,
    user: DoctorUser,
    starts_after: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> list[AvailabilitySlot]:
    doctor = _doctor_profile(session, user.id)
    query = select(AvailabilitySlot).where(
        AvailabilitySlot.doctor_id == doctor.id,
        AvailabilitySlot.deleted_at.is_(None),
    )
    if starts_after is not None:
        if starts_after.tzinfo is None or starts_after.utcoffset() is None:
            raise HTTPException(status_code=422, detail="starts_after must include a timezone")
        query = query.where(AvailabilitySlot.starts_at >= starts_after.astimezone(UTC))
    return list(
        session.scalars(
            query.order_by(AvailabilitySlot.starts_at, AvailabilitySlot.id)
            .limit(limit)
            .offset(offset)
        )
    )


@router.patch("/slots/{slot_id}", response_model=AvailabilitySlotOut)
def update_slot_availability(
    slot_id: UUID,
    body: AvailabilitySlotUpdate,
    request: Request,
    session: DbSession,
    user: DoctorUser,
    _: Annotated[None, Depends(require_mfa)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation() -> tuple[int, dict]:
        doctor = _doctor_profile(session, user.id)
        next_status = SlotStatus.available if body.available else SlotStatus.unavailable
        result = session.execute(
            update(AvailabilitySlot)
            .where(
                AvailabilitySlot.id == slot_id,
                AvailabilitySlot.doctor_id == doctor.id,
                AvailabilitySlot.version == body.version,
                AvailabilitySlot.status.in_([SlotStatus.available, SlotStatus.unavailable]),
                AvailabilitySlot.deleted_at.is_(None),
            )
            .values(status=next_status, version=AvailabilitySlot.version + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail="Slot is missing, reserved, removed, or has a stale version",
            )
        session.flush()
        slot = session.scalar(
            select(AvailabilitySlot).where(
                AvailabilitySlot.id == slot_id,
                AvailabilitySlot.doctor_id == doctor.id,
            )
        )
        _record(
            request,
            session,
            user,
            "availability_slot.availability_changed",
            "availability_slot",
            slot.id,
            {"available": body.available, "version": slot.version},
        )
        return status.HTTP_200_OK, {
            "id": slot.id,
            "doctor_id": slot.doctor_id,
            "starts_at": slot.starts_at,
            "ends_at": slot.ends_at,
            "status": slot.status.value,
            "version": slot.version,
        }

    _, response, _ = execute_auth_write(
        session,
        scope=f"doctor-slot-update:{user.id}",
        key=idempotency_key,
        request_body={"slot_id": str(slot_id), **body.model_dump(mode="json")},
        operation=operation,
    )
    return response


@router.delete("/slots/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_slot(
    slot_id: UUID,
    body: AvailabilitySlotRemove,
    request: Request,
    session: DbSession,
    user: DoctorUser,
    _: Annotated[None, Depends(require_mfa)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> Response:
    def operation() -> tuple[int, dict]:
        doctor = _doctor_profile(session, user.id)
        now = datetime.now(UTC)
        result = session.execute(
            update(AvailabilitySlot)
            .where(
                AvailabilitySlot.id == slot_id,
                AvailabilitySlot.doctor_id == doctor.id,
                AvailabilitySlot.version == body.version,
                AvailabilitySlot.status.in_([SlotStatus.available, SlotStatus.unavailable]),
                AvailabilitySlot.deleted_at.is_(None),
            )
            .values(
                status=SlotStatus.unavailable,
                deleted_at=now,
                version=AvailabilitySlot.version + 1,
            )
        )
        if result.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail="Slot is missing, reserved, removed, or has a stale version",
            )
        _record(
            request,
            session,
            user,
            "availability_slot.removed",
            "availability_slot",
            slot_id,
            {"version": body.version},
        )
        return status.HTTP_204_NO_CONTENT, {"removed": True}

    execute_auth_write(
        session,
        scope=f"doctor-slot-remove:{user.id}",
        key=idempotency_key,
        request_body={"slot_id": str(slot_id), **body.model_dump(mode="json")},
        operation=operation,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
