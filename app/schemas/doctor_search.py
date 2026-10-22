from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class DoctorSearchResult(BaseModel):
    doctor_id: UUID
    display_name: str
    specialty: str
    qualifications: str
    languages: list[str]
    location: str | None
    rating: Decimal
    rating_count: int
    consultation_fee: Decimal
    currency: str
    next_available_at: datetime | None
