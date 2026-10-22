from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.deps import DbSession, require_mfa, require_roles
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
from app.schemas.admin_analytics import (
    AnalyticsOverview,
    ConsultationCounts,
    DoctorPerformance,
    DoctorPerformancePage,
    RevenueByCurrency,
    UtilizationSummary,
)

router = APIRouter(
    prefix="/admin/analytics",
    tags=["admin analytics"],
    dependencies=[Depends(require_roles(Role.admin)), Depends(require_mfa)],
)
MAX_WINDOW_DAYS = 366
REVENUE_STATUSES = (PaymentStatus.succeeded, PaymentStatus.refunded)
UTILIZED_CONSULTATION_STATUSES = (
    ConsultationStatus.scheduled,
    ConsultationStatus.in_progress,
    ConsultationStatus.completed,
)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start_utc = _utc(start)
    end_utc = _utc(end)
    if start.tzinfo is None or start.utcoffset() is None:
        raise HTTPException(status_code=422, detail="from_date must include a timezone")
    if end.tzinfo is None or end.utcoffset() is None:
        raise HTTPException(status_code=422, detail="to_date must include a timezone")
    if start_utc >= end_utc:
        raise HTTPException(status_code=422, detail="to_date must be after from_date")
    if end_utc - start_utc > timedelta(days=MAX_WINDOW_DAYS):
        raise HTTPException(status_code=422, detail="Analytics window cannot exceed 366 days")
    return start_utc, end_utc


def _consultation_counts(session: Session, start: datetime, end: datetime) -> ConsultationCounts:
    rows = session.execute(
        select(Consultation.status, func.count(Consultation.id))
        .where(Consultation.starts_at >= start, Consultation.starts_at < end)
        .group_by(Consultation.status)
    ).all()
    counts = {status.value: 0 for status in ConsultationStatus}
    for consultation_status, count in rows:
        counts[consultation_status.value] = count
    return ConsultationCounts(total=sum(counts.values()), by_status=counts)


def _revenue_rows(session: Session, start: datetime, end: datetime):
    captured = func.sum(
        case(
            (Payment.status.in_(REVENUE_STATUSES), Payment.amount),
            else_=Decimal("0.00"),
        )
    )
    refunded = func.sum(
        case(
            (Payment.status == PaymentStatus.refunded, Payment.amount),
            else_=Decimal("0.00"),
        )
    )
    return session.execute(
        select(Payment.currency, captured, refunded)
        .join(Consultation, Consultation.id == Payment.consultation_id)
        .where(
            Consultation.starts_at >= start,
            Consultation.starts_at < end,
            Payment.status.in_(REVENUE_STATUSES),
        )
        .group_by(Payment.currency)
        .order_by(Payment.currency)
    ).all()


def _duration_minutes(session: Session):
    if session.get_bind().dialect.name == "sqlite":
        return (
            func.julianday(AvailabilitySlot.ends_at) - func.julianday(AvailabilitySlot.starts_at)
        ) * 1440
    return func.extract("epoch", AvailabilitySlot.ends_at - AvailabilitySlot.starts_at) / 60


def _utilization(session: Session, start: datetime, end: datetime) -> UtilizationSummary:
    duration = _duration_minutes(session)
    offered = session.scalar(
        select(func.coalesce(func.sum(duration), 0)).where(
            AvailabilitySlot.starts_at >= start,
            AvailabilitySlot.starts_at < end,
            AvailabilitySlot.deleted_at.is_(None),
            AvailabilitySlot.status.in_((SlotStatus.available, SlotStatus.held, SlotStatus.booked)),
        )
    )
    utilized = session.scalar(
        select(func.coalesce(func.sum(duration), 0))
        .join(
            Consultation,
            Consultation.availability_slot_id == AvailabilitySlot.id,
        )
        .where(
            AvailabilitySlot.starts_at >= start,
            AvailabilitySlot.starts_at < end,
            AvailabilitySlot.deleted_at.is_(None),
            Consultation.status.in_(UTILIZED_CONSULTATION_STATUSES),
        )
    )
    offered_minutes = Decimal(str(offered or 0)).quantize(Decimal("0.01"))
    utilized_minutes = Decimal(str(utilized or 0)).quantize(Decimal("0.01"))
    percent = (
        (utilized_minutes * Decimal("100") / offered_minutes).quantize(Decimal("0.01"))
        if offered_minutes
        else Decimal("0.00")
    )
    return UtilizationSummary(
        offered_minutes=offered_minutes,
        utilized_minutes=utilized_minutes,
        utilization_percent=percent,
    )


@router.get("/overview", response_model=AnalyticsOverview)
def analytics_overview(
    session: DbSession,
    from_date: datetime,
    to_date: datetime,
) -> AnalyticsOverview:
    start, end = _validate_window(from_date, to_date)
    revenue = [
        RevenueByCurrency(
            currency=currency,
            captured_amount=Decimal(captured or 0).quantize(Decimal("0.01")),
            refunded_amount=Decimal(refunded or 0).quantize(Decimal("0.01")),
            net_amount=Decimal(captured or 0).quantize(Decimal("0.01"))
            - Decimal(refunded or 0).quantize(Decimal("0.01")),
        )
        for currency, captured, refunded in _revenue_rows(session, start, end)
    ]
    return AnalyticsOverview(
        from_date=start,
        to_date=end,
        consultations=_consultation_counts(session, start, end),
        revenue=revenue,
        utilization=_utilization(session, start, end),
    )


@router.get("/doctor-performance", response_model=DoctorPerformancePage)
def doctor_performance(
    session: DbSession,
    from_date: datetime,
    to_date: datetime,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> DoctorPerformancePage:
    start, end = _validate_window(from_date, to_date)
    captured = func.sum(
        case(
            (Payment.status.in_(REVENUE_STATUSES), Payment.amount),
            else_=Decimal("0.00"),
        )
    )
    refunded = func.sum(
        case(
            (Payment.status == PaymentStatus.refunded, Payment.amount),
            else_=Decimal("0.00"),
        )
    )
    statement = (
        select(
            Doctor.id,
            Profile.full_name,
            Doctor.specialty,
            func.count(Consultation.id),
            func.sum(case((Consultation.status == ConsultationStatus.completed, 1), else_=0)),
            func.sum(case((Consultation.status == ConsultationStatus.cancelled, 1), else_=0)),
            Payment.currency,
            captured,
            refunded,
        )
        .join(Consultation, Consultation.doctor_id == Doctor.id)
        .join(User, User.id == Doctor.user_id)
        .join(Profile, Profile.user_id == User.id)
        .outerjoin(Payment, Payment.consultation_id == Consultation.id)
        .where(
            Consultation.starts_at >= start,
            Consultation.starts_at < end,
            Doctor.deleted_at.is_(None),
        )
        .group_by(Doctor.id, Profile.full_name, Doctor.specialty, Payment.currency)
        .order_by(func.count(Consultation.id).desc(), Doctor.id, Payment.currency)
        .limit(limit)
        .offset(offset)
    )
    items = []
    for (
        doctor_id,
        display_name,
        specialty,
        count,
        completed,
        cancelled,
        currency,
        captured_amount,
        refunded_amount,
    ) in session.execute(statement):
        captured_value = Decimal(captured_amount or 0).quantize(Decimal("0.01"))
        refunded_value = Decimal(refunded_amount or 0).quantize(Decimal("0.01"))
        items.append(
            DoctorPerformance(
                doctor_id=doctor_id,
                display_name=display_name,
                specialty=specialty,
                consultations=count,
                completed=completed or 0,
                cancelled=cancelled or 0,
                captured_amount=captured_value,
                refunded_amount=refunded_value,
                net_amount=captured_value - refunded_value,
                currency=currency or "N/A",
            )
        )
    return DoctorPerformancePage(from_date=start, to_date=end, items=items)
