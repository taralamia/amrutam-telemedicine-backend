from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BookingCreate(BaseModel):
    availability_slot_id: UUID


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    consultation_id: UUID
    payment_id: UUID
    status: str
    payment_status: str
    amount: Decimal
    currency: str
    starts_at: datetime


class PaymentWebhookIn(BaseModel):
    provider_event_id: str = Field(min_length=1, max_length=200)
    payment_id: UUID
    provider_reference: str = Field(min_length=1, max_length=200)
    status: Literal["succeeded", "failed", "refunded"]
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class PaymentWebhookOut(BaseModel):
    accepted: bool = True
