from functools import lru_cache

from pydantic import Field, model_validator
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

    @model_validator(mode="after")
    def validate_production_database(self):
        if self.app_env == "production" and not self.database_url.startswith(
            "postgresql+psycopg://"
        ):
            raise ValueError("Production DATABASE_URL must use PostgreSQL with psycopg")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
