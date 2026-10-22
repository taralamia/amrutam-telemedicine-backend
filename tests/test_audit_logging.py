import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.audit import record_audit
from app.db import Base
from app.models import AuditLog, Role, User


@pytest.fixture
def audit_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER audit_logs_immutable_update
            BEFORE UPDATE ON audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs is append-only');
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER audit_logs_immutable_delete
            BEFORE DELETE ON audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs is append-only');
            END
            """
        )
    factory = sessionmaker(engine, class_=Session, expire_on_commit=False)
    try:
        with factory.begin() as session:
            actor = User(
                email="audit-actor@example.com",
                password_hash="unused",
                role=Role.admin,
            )
            session.add(actor)
            session.flush()
            record_audit(
                session,
                actor_id=actor.id,
                action="audit.test",
                entity_type="test",
                entity_id="example-1",
                details={"safe_metadata": "value"},
                request_id="request-1",
            )
            session.flush()
            audit_id = session.scalar(select(AuditLog.id))
        yield factory, audit_id
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_audit_rows_reject_update_and_delete(audit_session):
    factory, audit_id = audit_session
    with factory() as session:
        with pytest.raises(DatabaseError, match="append-only"):
            session.execute(
                text("UPDATE audit_logs SET action = 'tampered' WHERE id = :id"),
                {"id": audit_id.hex},
            )
        session.rollback()
        with pytest.raises(DatabaseError, match="append-only"):
            session.execute(
                text("DELETE FROM audit_logs WHERE id = :id"),
                {"id": audit_id.hex},
            )
        session.rollback()

    with factory() as session:
        audit = session.get(AuditLog, audit_id)
        assert audit.action == "audit.test"
        assert audit.details == {"safe_metadata": "value"}
