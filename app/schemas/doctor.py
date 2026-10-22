from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class DoctorProfileCreate(BaseModel):
    specialty: str = Field(min_length=2, max_length=120)
    qualifications: str = Field(default="", max_length=4000)
    languages: list[str] = Field(default_factory=list, max_length=20)
    location: str | None = Field(default=None, max_length=160)
    consultation_fee: Decimal = Field(
        default=Decimal("500.00"), ge=0, max_digits=12, decimal_places=2
    )
    currency: Currency = "INR"

    @field_validator("specialty", "qualifications", "location")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        stripped = value.strip() if value is not None else None
        if value is not None and not stripped:
            raise ValueError("Text fields cannot be blank")
        return stripped

    @field_validator("languages")
    @classmethod
    def normalize_languages(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = []
        for value in values:
            language = value.strip()
            if not language or len(language) > 64:
                raise ValueError("Language labels must contain 1 to 64 characters")
            if language.casefold() not in {item.casefold() for item in normalized}:
                normalized.append(language)
        return normalized


class DoctorProfileUpdate(DoctorProfileCreate):
    specialty: str | None = Field(default=None, min_length=2, max_length=120)
    qualifications: str | None = Field(default=None, max_length=4000)
    languages: list[str] | None = Field(default=None, max_length=20)
    location: str | None = Field(default=None, max_length=160)
    consultation_fee: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    currency: Currency | None = None
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set.intersection(
            {"specialty", "qualifications", "languages", "location", "consultation_fee", "currency"}
        ):
            raise ValueError("At least one profile field must be provided")
        return self

    @model_validator(mode="after")
    def reject_null_required_fields(self):
        for field in ("specialty", "qualifications", "languages", "consultation_fee", "currency"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class DoctorProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    specialty: str
    qualifications: str
    languages: list[str]
    location: str | None
    consultation_fee: Decimal
    currency: str
    rating: Decimal
    rating_count: int
    is_listed: bool
    version: int


class AvailabilitySlotCreate(BaseModel):
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def require_timezone_and_positive_duration(self):
        if self.starts_at.tzinfo is None or self.starts_at.utcoffset() is None:
            raise ValueError("starts_at must include a timezone offset")
        if self.ends_at.tzinfo is None or self.ends_at.utcoffset() is None:
            raise ValueError("ends_at must include a timezone offset")
        if self.starts_at >= self.ends_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class AvailabilitySlotUpdate(BaseModel):
    version: int = Field(ge=1)
    available: bool


class AvailabilitySlotRemove(BaseModel):
    version: int = Field(ge=1)


class AvailabilitySlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    doctor_id: UUID
    starts_at: datetime
    ends_at: datetime
    status: str
    version: int


class DoctorOwnProfileOut(DoctorProfileOut):
    user_id: UUID
