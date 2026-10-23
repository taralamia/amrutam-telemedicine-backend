from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import OutboxEvent
from app.workers.outbox import OutboxMessage, _retry_delay_seconds, process_outbox_batch


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


@pytest.mark.parametrize(
    ("attempt_count", "random_fraction", "expected"),
    [
        (1, 0.0, 0.5),
        (1, 1.0, 1.0),
        (2, 0.5, 1.5),
        (100, 0.0, 150.0),
        (100, 1.0, 300.0),
    ],
)
def test_outbox_retry_uses_capped_exponential_backoff_with_jitter(
    monkeypatch, attempt_count, random_fraction, expected
):
    monkeypatch.setattr(
        "app.workers.outbox.random.uniform", lambda lower, upper: upper * random_fraction
    )
    assert _retry_delay_seconds(attempt_count) == expected


def test_outbox_batch_size_is_bounded():
    engine, factory = _session_factory()
    try:
        with pytest.raises(ValueError, match="batch_size"):
            process_outbox_batch(factory, {}, batch_size=0)
        with pytest.raises(ValueError, match="batch_size"):
            process_outbox_batch(factory, {}, batch_size=501)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
