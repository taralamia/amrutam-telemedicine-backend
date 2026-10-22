from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConsultationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_id: UUID
    doctor_id: UUID
    availability_slot_id: UUID
    status: str
    starts_at: datetime
    version: int


class ConsultationStatusOut(BaseModel):
    consultation_id: UUID
    status: str
    version: int


class PrescriptionCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Prescription content cannot be blank")
        return value


class PrescriptionOut(BaseModel):
    id: UUID
    consultation_id: UUID
    doctor_id: UUID
    patient_id: UUID
    content: str
    created_at: datetime
