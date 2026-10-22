from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Amrutam Telemedicine API"
    app_env: str = "development"
    database_url: str = "sqlite+pysqlite:///./amrutam.db"
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    jwt_secret: SecretStr
    jwt_issuer: str = "amrutam-api"
    access_token_minutes: int = Field(default=10, ge=1, le=60)
    refresh_token_days: int = Field(default=30, ge=1, le=365)
    data_encryption_key: SecretStr
    payment_webhook_secret: SecretStr | None = None
    auth_rate_limit_per_minute: int = Field(default=10, ge=1, le=1000)

    @field_validator("jwt_secret", "data_encryption_key")
    @classmethod
    def validate_secret_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("Authentication and encryption secrets must be at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_production_database(self):
        if self.app_env == "production" and not self.database_url.startswith(
            "postgresql+psycopg://"
        ):
            raise ValueError("Production DATABASE_URL must use PostgreSQL with psycopg")
        if self.payment_webhook_secret and len(self.payment_webhook_secret.get_secret_value()) < 32:
            raise ValueError("PAYMENT_WEBHOOK_SECRET must be at least 32 characters")
        if self.app_env == "production" and self.payment_webhook_secret is None:
            raise ValueError("PAYMENT_WEBHOOK_SECRET is required in production")
        secrets = [
            self.jwt_secret.get_secret_value(),
            self.data_encryption_key.get_secret_value(),
        ]
        if self.payment_webhook_secret is not None:
            secrets.append(self.payment_webhook_secret.get_secret_value())
        if len(secrets) != len(set(secrets)):
            raise ValueError("JWT, encryption, and payment webhook secrets must be different")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
