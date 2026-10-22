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
    redis_url: str | None = None
    trusted_hosts: str = "*"
    otel_service_name: str = "amrutam-telemedicine-api"
    otel_exporter_otlp_endpoint: str | None = None
    otel_trace_sample_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    jwt_secret: SecretStr
    jwt_issuer: str = "amrutam-api"
    access_token_minutes: int = Field(default=10, ge=1, le=60)
    refresh_token_days: int = Field(default=30, ge=1, le=365)
    data_encryption_key: SecretStr
    payment_webhook_secret: SecretStr | None = None
    auth_rate_limit_per_minute: int = Field(default=10, ge=1, le=1000)
    booking_rate_limit_per_minute: int = Field(default=20, ge=1, le=5000)
    webhook_rate_limit_per_minute: int = Field(default=1200, ge=1, le=10000)

    @field_validator("redis_url", mode="before")
    @classmethod
    def normalize_empty_redis_url(cls, value):
        return None if value == "" else value

    @field_validator("otel_exporter_otlp_endpoint", mode="before")
    @classmethod
    def normalize_empty_otel_endpoint(cls, value):
        return None if value == "" else value

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
        if self.redis_url is not None and not self.redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must use redis:// or rediss://")
        if self.otel_exporter_otlp_endpoint is not None:
            valid_endpoint = self.otel_exporter_otlp_endpoint.startswith(("http://", "https://"))
            if not valid_endpoint:
                raise ValueError("OTEL_EXPORTER_OTLP_ENDPOINT must use http:// or https://")
        if self.app_env == "production" and not self.redis_url:
            raise ValueError("REDIS_URL is required in production for distributed rate limiting")
        if self.app_env == "production" and (
            not self.trusted_host_list or "*" in self.trusted_host_list
        ):
            raise ValueError("TRUSTED_HOSTS must explicitly list production host names")
        secrets = [
            self.jwt_secret.get_secret_value(),
            self.data_encryption_key.get_secret_value(),
        ]
        if self.payment_webhook_secret is not None:
            secrets.append(self.payment_webhook_secret.get_secret_value())
        if len(secrets) != len(set(secrets)):
            raise ValueError("JWT, encryption, and payment webhook secrets must be different")
        return self

    @property
    def trusted_host_list(self) -> list[str]:
        return [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
