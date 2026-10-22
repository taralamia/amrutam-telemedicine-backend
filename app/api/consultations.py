from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import DbSession, require_mfa, require_roles
from app.core.audit import record_audit
from app.core.auth_idempotency import execute_auth_write
from app.models import (
    AvailabilitySlot,
    Consultation,
    ConsultationStatus,
    Doctor,
    OutboxEvent,
    Payment,
    PaymentStatus,
    Prescription,
    Role,
    SlotStatus,
    User,
)
from app.schemas.consultation import (
    ConsultationOut,
    ConsultationStatusOut,
    PrescriptionCreate,
    PrescriptionOut,
)

router = APIRouter(prefix="/consultations", tags=["consultations"])
DoctorUser = Annotated[User, Depends(require_roles(Role.doctor))]
PatientOrDoctor = Annotated[User, Depends(require_roles(Role.patient, Role.doctor))]
ACTIVE_CONSULTATION_STATUSES = (
    ConsultationStatus.pending_payment,
    ConsultationStatus.scheduled,
    ConsultationStatus.in_progress,
    ConsultationStatus.completed,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _doctor_id(session: Session, user_id: UUID) -> UUID:
    doctor_id = session.scalar(
        select(Doctor.id).where(Doctor.user_id == user_id, Doctor.deleted_at.is_(None))
    )
    if doctor_id is None:
        raise HTTPException(status_code=403, detail="Active doctor profile required")
    return doctor_id


def _consultation_for_actor(session: Session, consultation_id: UUID, actor: User) -> Consultation:
    statement = select(Consultation).where(Consultation.id == consultation_id)
    if actor.role == Role.patient:
        statement = statement.where(Consultation.patient_id == actor.id)
    elif actor.role == Role.doctor:
        statement = statement.where(Consultation.doctor_id == _doctor_id(session, actor.id))
    consultation = session.scalar(statement)
    if consultation is None:
        raise HTTPException(status_code=404, detail="Consultation not found")
    return consultation


def _audit(
    session: Session,
    request: Request,
    actor: User,
    *,
    action: str,
    entity_type: str,
    entity_id: UUID,
    details: dict,
) -> None:
    record_audit(
        session,
        actor_id=actor.id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        request_id=getattr(request.state, "request_id", None),
    )


def _transition(
    session: Session,
    request: Request,
    actor: User,
    consultation_id: UUID,
    *,
    expected_status: ConsultationStatus,
    target_status: ConsultationStatus,
    action: str,
) -> dict:
    consultation = session.scalar(
        select(Consultation).where(Consultation.id == consultation_id).with_for_update()
    )
    if consultation is None:
        raise HTTPException(status_code=404, detail="Consultation not found")
    if consultation.doctor_id != _doctor_id(session, actor.id):
        raise HTTPException(status_code=404, detail="Consultation not found")
    if consultation.status != expected_status:
        raise HTTPException(
            status_code=409,
            detail=f"Consultation must be {expected_status.value} to transition",
        )
    if expected_status == ConsultationStatus.scheduled and _utc(
        consultation.starts_at
    ) > datetime.now(UTC):
        raise HTTPException(status_code=409, detail="Consultation has not started yet")

    version = consultation.version
    changed = session.execute(
        update(Consultation)
        .where(
            Consultation.id == consultation.id,
            Consultation.status == expected_status,
            Consultation.version == version,
        )
        .values(status=target_status, version=Consultation.version + 1)
    )
    if changed.rowcount != 1:
        raise HTTPException(status_code=409, detail="Consultation was updated concurrently")
    _audit(
        session,
        request,
        actor,
        action=action,
        entity_type="consultation",
        entity_id=consultation.id,
        details={"from_status": expected_status.value, "to_status": target_status.value},
    )
    _enqueue_notification(session, consultation.id, target_status)
    return {
        "consultation_id": consultation.id,
        "status": target_status.value,
        "version": version + 1,
    }


def _enqueue_notification(
    session: Session, consultation_id: UUID, target_status: ConsultationStatus
) -> None:
    session.add(
        OutboxEvent(
            event_type=f"consultation.{target_status.value}",
            aggregate_type="consultation",
            aggregate_id=str(consultation_id),
            payload={"consultation_id": str(consultation_id), "status": target_status.value},
        )
    )


@router.get("", response_model=list[ConsultationOut])
def list_consultations(
    session: DbSession,
    actor: PatientOrDoctor,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> list[Consultation]:
    statement = select(Consultation)
    if actor.role == Role.patient:
        statement = statement.where(Consultation.patient_id == actor.id)
    else:
        statement = statement.where(Consultation.doctor_id == _doctor_id(session, actor.id))
    return list(
        session.scalars(
            statement.order_by(Consultation.starts_at.desc()).limit(limit).offset(offset)
        )
    )


@router.get("/{consultation_id}", response_model=ConsultationOut)
def get_consultation(
    consultation_id: UUID,
    session: DbSession,
    actor: PatientOrDoctor,
) -> Consultation:
    return _consultation_for_actor(session, consultation_id, actor)


@router.post("/{consultation_id}/start", response_model=ConsultationStatusOut)
def start_consultation(
    consultation_id: UUID,
    request: Request,
    session: DbSession,
    doctor: DoctorUser,
    _: Annotated[None, Depends(require_mfa)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    _, response, _ = execute_auth_write(
        session,
        scope=f"consultation-start:{doctor.id}:{consultation_id}",
        key=idempotency_key,
        request_body={"consultation_id": str(consultation_id), "action": "start"},
        operation=lambda: (
            status.HTTP_200_OK,
            _transition(
                session,
                request,
                doctor,
                consultation_id,
                expected_status=ConsultationStatus.scheduled,
                target_status=ConsultationStatus.in_progress,
                action="consultation.started",
            ),
        ),
    )
    return response


@router.post("/{consultation_id}/complete", response_model=ConsultationStatusOut)
def complete_consultation(
    consultation_id: UUID,
    request: Request,
    session: DbSession,
    doctor: DoctorUser,
    _: Annotated[None, Depends(require_mfa)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    _, response, _ = execute_auth_write(
        session,
        scope=f"consultation-complete:{doctor.id}:{consultation_id}",
        key=idempotency_key,
        request_body={"consultation_id": str(consultation_id), "action": "complete"},
        operation=lambda: (
            status.HTTP_200_OK,
            _transition(
                session,
                request,
                doctor,
                consultation_id,
                expected_status=ConsultationStatus.in_progress,
                target_status=ConsultationStatus.completed,
                action="consultation.completed",
            ),
        ),
    )
    return response


@router.post("/{consultation_id}/cancel", response_model=ConsultationStatusOut)
def cancel_consultation(
    consultation_id: UUID,
    request: Request,
    session: DbSession,
    actor: Annotated[
        User,
        Depends(require_roles(Role.patient, Role.doctor, Role.admin)),
    ],
    _: Annotated[None, Depends(require_mfa)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation() -> tuple[int, dict]:
        if actor.role == Role.patient:
            consultation_query = select(Consultation).where(
                Consultation.id == consultation_id,
                Consultation.patient_id == actor.id,
            )
        elif actor.role == Role.doctor:
            consultation_query = select(Consultation).where(
                Consultation.id == consultation_id,
                Consultation.doctor_id == _doctor_id(session, actor.id),
            )
        else:
            consultation_query = select(Consultation).where(Consultation.id == consultation_id)

        # Lock payment before consultation to preserve webhook lock ordering.
        payment = session.scalar(
            select(Payment).where(Payment.consultation_id == consultation_id).with_for_update()
        )
        consultation = session.scalar(consultation_query.with_for_update())
        if consultation is None:
            raise HTTPException(status_code=404, detail="Consultation not found")
        if consultation.status not in (
            ConsultationStatus.pending_payment,
            ConsultationStatus.scheduled,
        ):
            raise HTTPException(status_code=409, detail="Consultation can no longer be cancelled")
        slot = session.scalar(
            select(AvailabilitySlot)
            .where(AvailabilitySlot.id == consultation.availability_slot_id)
            .with_for_update()
        )
        if slot is None:
            raise HTTPException(status_code=409, detail="Consultation availability is missing")

        previous_status = consultation.status
        consultation.status = ConsultationStatus.cancelled
        consultation.cancelled_at = datetime.now(UTC)
        consultation.version += 1
        if slot.status in (SlotStatus.held, SlotStatus.booked) and _utc(
            slot.starts_at
        ) > datetime.now(UTC):
            slot.status = SlotStatus.available
            slot.version += 1

        if payment is not None and payment.status == PaymentStatus.succeeded:
            session.add(
                OutboxEvent(
                    event_type="payment.refund.requested",
                    aggregate_type="payment",
                    aggregate_id=str(payment.id),
                    payload={
                        "payment_id": str(payment.id),
                        "provider_reference": payment.provider_reference,
                        "reason": "consultation_cancelled",
                    },
                )
            )
        _audit(
            session,
            request,
            actor,
            action="consultation.cancelled",
            entity_type="consultation",
            entity_id=consultation.id,
            details={"from_status": previous_status.value},
        )
        _enqueue_notification(session, consultation.id, ConsultationStatus.cancelled)
        return status.HTTP_200_OK, {
            "consultation_id": consultation.id,
            "status": ConsultationStatus.cancelled.value,
            "version": consultation.version,
        }

    _, response, _ = execute_auth_write(
        session,
        scope=f"consultation-cancel:{actor.id}:{consultation_id}",
        key=idempotency_key,
        request_body={"consultation_id": str(consultation_id), "action": "cancel"},
        operation=operation,
    )
    return response


@router.post(
    "/{consultation_id}/prescription",
    response_model=PrescriptionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_prescription(
    consultation_id: UUID,
    body: PrescriptionCreate,
    request: Request,
    session: DbSession,
    doctor: DoctorUser,
    _: Annotated[None, Depends(require_mfa)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation() -> tuple[int, dict]:
        consultation = session.scalar(
            select(Consultation).where(Consultation.id == consultation_id).with_for_update()
        )
        if consultation is None or consultation.doctor_id != _doctor_id(session, doctor.id):
            raise HTTPException(status_code=404, detail="Consultation not found")
        if consultation.status != ConsultationStatus.completed:
            raise HTTPException(
                status_code=409, detail="Prescription requires a completed consultation"
            )
        existing = session.scalar(
            select(Prescription.id).where(Prescription.consultation_id == consultation.id)
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Prescription already exists")

        prescription = Prescription(
            consultation_id=consultation.id,
            doctor_id=consultation.doctor_id,
            patient_id=consultation.patient_id,
            content_encrypted=body.content,
        )
        session.add(prescription)
        session.flush()
        _audit(
            session,
            request,
            doctor,
            action="prescription.created",
            entity_type="prescription",
            entity_id=prescription.id,
            details={"consultation_id": str(consultation.id)},
        )
        session.add(
            OutboxEvent(
                event_type="prescription.created",
                aggregate_type="prescription",
                aggregate_id=str(prescription.id),
                payload={
                    "prescription_id": str(prescription.id),
                    "consultation_id": str(consultation.id),
                    "patient_id": str(consultation.patient_id),
                },
            )
        )
        return status.HTTP_201_CREATED, {
            "id": prescription.id,
            "consultation_id": prescription.consultation_id,
            "doctor_id": prescription.doctor_id,
            "patient_id": prescription.patient_id,
            "content": prescription.content_encrypted,
            "created_at": prescription.created_at or datetime.now(UTC),
        }

    _, response, _ = execute_auth_write(
        session,
        scope=f"prescription-create:{doctor.id}:{consultation_id}",
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=operation,
    )
    return response


@router.get("/{consultation_id}/prescription", response_model=PrescriptionOut)
def get_prescription(
    consultation_id: UUID,
    request: Request,
    session: DbSession,
    actor: PatientOrDoctor,
    _: Annotated[None, Depends(require_mfa)],
) -> dict:
    consultation = _consultation_for_actor(session, consultation_id, actor)
    if consultation.status != ConsultationStatus.completed:
        raise HTTPException(status_code=404, detail="Prescription not found")
    prescription = session.scalar(
        select(Prescription).where(Prescription.consultation_id == consultation.id)
    )
    if prescription is None:
        raise HTTPException(status_code=404, detail="Prescription not found")
    _audit(
        session,
        request,
        actor,
        action="prescription.accessed",
        entity_type="prescription",
        entity_id=prescription.id,
        details={"consultation_id": str(consultation.id)},
    )
    session.commit()
    return {
        "id": prescription.id,
        "consultation_id": prescription.consultation_id,
        "doctor_id": prescription.doctor_id,
        "patient_id": prescription.patient_id,
        "content": prescription.content_encrypted,
        "created_at": prescription.created_at,
    }
