from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine_options = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}
    if ":memory:" in settings.database_url:
        engine_options["poolclass"] = StaticPool
else:
    engine_options.update(pool_size=10, max_overflow=20)

engine = create_engine(settings.database_url, **engine_options)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def check_database() -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
