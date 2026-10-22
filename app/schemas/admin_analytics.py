from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class RevenueByCurrency(BaseModel):
    currency: str
    captured_amount: Decimal
    refunded_amount: Decimal
    net_amount: Decimal


class ConsultationCounts(BaseModel):
    total: int
    by_status: dict[str, int]


class UtilizationSummary(BaseModel):
    offered_minutes: Decimal
    utilized_minutes: Decimal
    utilization_percent: Decimal


class AnalyticsOverview(BaseModel):
    from_date: datetime
    to_date: datetime
    consultations: ConsultationCounts
    revenue: list[RevenueByCurrency]
    utilization: UtilizationSummary


class DoctorPerformance(BaseModel):
    doctor_id: UUID
    display_name: str
    specialty: str
    consultations: int
    completed: int
    cancelled: int
    captured_amount: Decimal
    refunded_amount: Decimal
    net_amount: Decimal
    currency: str


class DoctorPerformancePage(BaseModel):
    from_date: datetime
    to_date: datetime
    items: list[DoctorPerformance]
