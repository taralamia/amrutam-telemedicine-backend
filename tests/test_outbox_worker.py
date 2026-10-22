from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import OutboxEvent
from app.workers.outbox import OutboxMessage, process_outbox_batch


def _session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, class_=Session, expire_on_commit=False)
    return engine, factory


def test_outbox_worker_marks_handled_event_published_and_passes_stable_event_id():
    engine, factory = _session_factory()
    try:
        with factory.begin() as session:
            event = OutboxEvent(
                event_type="consultation.scheduled",
                aggregate_type="consultation",
                aggregate_id="consultation-1",
                payload={"consultation_id": "consultation-1"},
            )
            session.add(event)
            session.flush()
            event_id = event.id

        received: list[OutboxMessage] = []
        assert process_outbox_batch(factory, {"consultation.scheduled": received.append}) == 1

        with factory() as session:
            stored = session.get(OutboxEvent, event_id)
            assert stored.published_at is not None
            assert stored.attempt_count == 1
            assert received[0].id == event_id
            assert received[0].payload == {"consultation_id": "consultation-1"}
            assert process_outbox_batch(factory, {"consultation.scheduled": received.append}) == 0
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_outbox_worker_schedules_retry_after_handler_failure():
    engine, factory = _session_factory()
    try:
        with factory.begin() as session:
            session.add(
                OutboxEvent(
                    event_type="payment.intent.requested",
                    aggregate_type="payment",
                    aggregate_id="payment-1",
                    payload={"payment_id": "payment-1"},
                )
            )

        def failing_handler(_: OutboxMessage) -> None:
            raise ConnectionError("gateway temporarily unavailable")

        assert process_outbox_batch(factory, {"payment.intent.requested": failing_handler}) == 1

        with factory() as session:
            stored = session.scalar(select(OutboxEvent))
            assert stored.published_at is None
            assert stored.attempt_count == 1
            assert stored.available_at.replace(tzinfo=UTC) > datetime.now(UTC)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
