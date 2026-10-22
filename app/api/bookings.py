import hashlib
import hmac
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import DbSession, require_roles
from app.core.audit import record_audit
from app.core.auth_idempotency import execute_auth_write
from app.core.config import get_settings
from app.models import (
    AvailabilitySlot,
    Consultation,
    ConsultationStatus,
    Doctor,
    OutboxEvent,
    Payment,
    PaymentStatus,
    PaymentWebhookEvent,
    Role,
    SlotStatus,
    User,
)
from app.schemas.booking import BookingCreate, BookingOut, PaymentWebhookIn, PaymentWebhookOut

router = APIRouter(tags=["bookings"])
PatientUser = Annotated[User, Depends(require_roles(Role.patient))]
settings = get_settings()
WEBHOOK_CLOCK_TOLERANCE_SECONDS = 300
ACTIVE_CONSULTATION_STATUSES = (
    ConsultationStatus.pending_payment,
    ConsultationStatus.scheduled,
    ConsultationStatus.in_progress,
    ConsultationStatus.completed,
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _audit(
    session: Session,
    request: Request,
    *,
    actor_id: UUID | None,
    action: str,
    entity_type: str,
    entity_id: UUID | str,
    details: dict,
) -> None:
    record_audit(
        session,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        request_id=getattr(request.state, "request_id", None),
    )


def _outbox(
    session: Session,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict,
) -> None:
    session.add(
        OutboxEvent(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload,
        )
    )


@router.post(
    "/consultations",
    response_model=BookingOut,
    status_code=status.HTTP_201_CREATED,
    tags=["consultations"],
)
def create_booking(
    body: BookingCreate,
    request: Request,
    session: DbSession,
    patient: PatientUser,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation() -> tuple[int, dict]:
        slot = session.scalar(
            select(AvailabilitySlot)
            .where(
                AvailabilitySlot.id == body.availability_slot_id,
                AvailabilitySlot.status == SlotStatus.available,
                AvailabilitySlot.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if slot is None:
            raise HTTPException(status_code=409, detail="Availability slot is not bookable")

        doctor = session.scalar(
            select(Doctor)
            .join(User, User.id == Doctor.user_id)
            .where(
                Doctor.id == slot.doctor_id,
                Doctor.is_listed.is_(True),
                Doctor.deleted_at.is_(None),
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        if doctor is None:
            raise HTTPException(status_code=409, detail="Doctor is not available for booking")
        now = datetime.now(UTC)
        if _utc(slot.starts_at) <= now:
            raise HTTPException(status_code=409, detail="Availability slot has already started")

        amount = Decimal(doctor.consultation_fee)
        is_free = amount == 0
        next_slot_status = SlotStatus.booked if is_free else SlotStatus.held
        changed = session.execute(
            update(AvailabilitySlot)
            .where(
                AvailabilitySlot.id == slot.id,
                AvailabilitySlot.version == slot.version,
                AvailabilitySlot.status == SlotStatus.available,
                AvailabilitySlot.deleted_at.is_(None),
            )
            .values(status=next_slot_status, version=AvailabilitySlot.version + 1)
        )
        if changed.rowcount != 1:
            raise HTTPException(status_code=409, detail="Availability slot was just booked")

        consultation = Consultation(
            patient_id=patient.id,
            doctor_id=doctor.id,
            availability_slot_id=slot.id,
            status=ConsultationStatus.scheduled if is_free else ConsultationStatus.pending_payment,
            starts_at=slot.starts_at,
        )
        session.add(consultation)
        session.flush()
        payment = Payment(
            consultation_id=consultation.id,
            amount=amount,
            currency=doctor.currency,
            status=PaymentStatus.succeeded if is_free else PaymentStatus.pending,
        )
        session.add(payment)
        session.flush()

        _audit(
            session,
            request,
            actor_id=patient.id,
            action="consultation.booked",
            entity_type="consultation",
            entity_id=consultation.id,
            details={
                "doctor_id": str(doctor.id),
                "availability_slot_id": str(slot.id),
                "payment_id": str(payment.id),
                "amount": str(amount),
                "currency": doctor.currency,
                "status": consultation.status.value,
            },
        )
        if is_free:
            _outbox(
                session,
                event_type="consultation.scheduled",
                aggregate_type="consultation",
                aggregate_id=str(consultation.id),
                payload={"consultation_id": str(consultation.id)},
            )
        else:
            _outbox(
                session,
                event_type="payment.intent.requested",
                aggregate_type="payment",
                aggregate_id=str(payment.id),
                payload={
                    "payment_id": str(payment.id),
                    "consultation_id": str(consultation.id),
                    "amount": str(amount),
                    "currency": doctor.currency,
                },
            )
        return status.HTTP_201_CREATED, {
            "consultation_id": consultation.id,
            "payment_id": payment.id,
            "status": consultation.status.value,
            "payment_status": payment.status.value,
            "amount": amount,
            "currency": doctor.currency,
            "starts_at": _utc(slot.starts_at),
        }

    _, response, _ = execute_auth_write(
        session,
        scope=f"consultation-booking:{patient.id}",
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=operation,
    )
    return response


@router.post("/webhooks/payments", response_model=PaymentWebhookOut, include_in_schema=True)
async def payment_webhook(
    body: PaymentWebhookIn,
    request: Request,
    session: DbSession,
    x_webhook_timestamp: Annotated[str, Header()],
    x_webhook_signature: Annotated[str, Header()],
) -> dict:
    secret = settings.payment_webhook_secret
    if secret is None:
        raise HTTPException(status_code=503, detail="Payment webhook is not configured")
    try:
        timestamp = int(x_webhook_timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid webhook signature") from exc
    if abs(int(time.time()) - timestamp) > WEBHOOK_CLOCK_TOLERANCE_SECONDS:
        raise HTTPException(
            status_code=401, detail="Webhook timestamp is outside the allowed window"
        )

    raw_body = await request.body()
    signed_content = str(timestamp).encode() + b"." + raw_body
    expected_signature = hmac.new(
        secret.get_secret_value().encode(), signed_content, hashlib.sha256
    ).hexdigest()
    supplied_signature = x_webhook_signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload_hash = hashlib.sha256(raw_body).hexdigest()

    def operation() -> tuple[int, dict]:
        payment = session.scalar(
            select(Payment).where(Payment.id == body.payment_id).with_for_update()
        )
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        consultation = session.scalar(
            select(Consultation).where(Consultation.id == payment.consultation_id).with_for_update()
        )
        if consultation is None:
            raise HTTPException(status_code=409, detail="Payment has no consultation")
        if payment.amount != body.amount or payment.currency != body.currency:
            raise HTTPException(status_code=409, detail="Payment amount or currency mismatch")
        if payment.provider_reference not in (None, body.provider_reference):
            raise HTTPException(status_code=409, detail="Payment provider reference mismatch")
        if payment.provider_reference is None:
            payment.provider_reference = body.provider_reference

        session.add(
            PaymentWebhookEvent(
                payment_id=payment.id,
                provider_event_id=body.provider_event_id,
                payload_hash=payload_hash,
                event_type=body.status,
            )
        )
        slot = session.scalar(
            select(AvailabilitySlot)
            .where(AvailabilitySlot.id == consultation.availability_slot_id)
            .with_for_update()
        )
        if slot is None:
            raise HTTPException(status_code=409, detail="Consultation availability is missing")

        now = datetime.now(UTC)
        doctor_is_available = (
            session.scalar(
                select(Doctor.id)
                .join(User, User.id == Doctor.user_id)
                .where(
                    Doctor.id == consultation.doctor_id,
                    Doctor.is_listed.is_(True),
                    Doctor.deleted_at.is_(None),
                    User.is_active.is_(True),
                    User.deleted_at.is_(None),
                )
            )
            is not None
        )

        if body.status == "succeeded" and payment.status not in (
            PaymentStatus.succeeded,
            PaymentStatus.refunded,
        ):
            payment.status = PaymentStatus.succeeded
            payment.provider_reference = body.provider_reference
            if (
                consultation.status == ConsultationStatus.pending_payment
                and slot.status == SlotStatus.held
                and doctor_is_available
                and _utc(slot.starts_at) > now
            ):
                consultation.status = ConsultationStatus.scheduled
                slot.status = SlotStatus.booked
                slot.version += 1
                _outbox(
                    session,
                    event_type="consultation.scheduled",
                    aggregate_type="consultation",
                    aggregate_id=str(consultation.id),
                    payload={"consultation_id": str(consultation.id)},
                )
                audit_action = "payment.succeeded"
            else:
                if consultation.status == ConsultationStatus.pending_payment:
                    consultation.status = ConsultationStatus.cancelled
                    consultation.cancelled_at = now
                _outbox(
                    session,
                    event_type="payment.refund.requested",
                    aggregate_type="payment",
                    aggregate_id=str(payment.id),
                    payload={
                        "payment_id": str(payment.id),
                        "provider_reference": body.provider_reference,
                        "reason": "booking_no_longer_confirmable",
                    },
                )
                audit_action = "payment.succeeded_refund_required"
                _release_slot_if_unreserved(session, consultation, slot)
            _audit(
                session,
                request,
                actor_id=None,
                action=audit_action,
                entity_type="payment",
                entity_id=payment.id,
                details={
                    "consultation_id": str(consultation.id),
                    "provider_event_id": body.provider_event_id,
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                },
            )
        elif body.status == "failed" and payment.status == PaymentStatus.pending:
            payment.status = PaymentStatus.failed
            if consultation.status == ConsultationStatus.pending_payment:
                consultation.status = ConsultationStatus.payment_failed
                _release_slot_if_unreserved(session, consultation, slot)
            _audit(
                session,
                request,
                actor_id=None,
                action="payment.failed",
                entity_type="payment",
                entity_id=payment.id,
                details={
                    "consultation_id": str(consultation.id),
                    "provider_event_id": body.provider_event_id,
                },
            )
        elif body.status == "refunded" and payment.status != PaymentStatus.refunded:
            payment.status = PaymentStatus.refunded
            if consultation.status in (
                ConsultationStatus.pending_payment,
                ConsultationStatus.scheduled,
            ):
                consultation.status = ConsultationStatus.cancelled
                consultation.cancelled_at = now
                _release_slot_if_unreserved(session, consultation, slot, include_booked=True)
            _audit(
                session,
                request,
                actor_id=None,
                action="payment.refunded",
                entity_type="payment",
                entity_id=payment.id,
                details={
                    "consultation_id": str(consultation.id),
                    "provider_event_id": body.provider_event_id,
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                },
            )

        return status.HTTP_200_OK, {"accepted": True}

    _, response, _ = execute_auth_write(
        session,
        scope="payment-webhook",
        key=body.provider_event_id,
        request_body=body.model_dump(mode="json"),
        operation=operation,
    )
    return response


def _release_slot_if_unreserved(
    session: Session,
    consultation: Consultation,
    slot: AvailabilitySlot,
    *,
    include_booked: bool = False,
) -> None:
    active_consultation_id = session.scalar(
        select(Consultation.id)
        .where(
            Consultation.availability_slot_id == slot.id,
            Consultation.id != consultation.id,
            Consultation.status.in_(ACTIVE_CONSULTATION_STATUSES),
        )
        .limit(1)
    )
    releasable_statuses = [SlotStatus.held]
    if include_booked and _utc(slot.starts_at) > datetime.now(UTC):
        releasable_statuses.append(SlotStatus.booked)
    if active_consultation_id is None and slot.status in releasable_statuses:
        slot.status = SlotStatus.available
        slot.version += 1
