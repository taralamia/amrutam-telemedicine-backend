from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class PatientRegistration(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    role: str = "patient"

    @field_validator("role")
    @classmethod
    def restrict_public_registration(cls, value: str) -> str:
        if value != "patient":
            raise ValueError("Public registration is limited to patient accounts")
        return value

    @field_validator("password")
    @classmethod
    def reject_whitespace_only_passwords(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Password cannot contain only whitespace")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    mfa_code: str | None = Field(default=None, pattern=r"^\d{6,8}$")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=40, max_length=256)


class MFASetupRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class MFAEnableRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6,8}$")


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    role: str
    full_name: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class MFASetupOut(BaseModel):
    secret: str
    provisioning_uri: str


class MFAEnabledOut(BaseModel):
    enabled: bool
    access_token: str
    token_type: str = "bearer"
