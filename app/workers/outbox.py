import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import OutboxEvent

logger = logging.getLogger("amrutam.outbox")
LEASE_SECONDS = 120
RETRY_BASE_SECONDS = 1
RETRY_MAX_SECONDS = 300


@dataclass(frozen=True)
class OutboxMessage:
    id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict
    attempt_count: int


OutboxHandler = Callable[[OutboxMessage], None]


def process_outbox_batch(
    session_factory: sessionmaker[Session],
    handlers: dict[str, OutboxHandler],
    *,
    batch_size: int = 50,
) -> int:
    if not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be between 1 and 500")
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=LEASE_SECONDS)
    with session_factory.begin() as session:
        events = list(
            session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= now)
                .order_by(OutboxEvent.available_at, OutboxEvent.created_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        messages = []
        for event in events:
            event.attempt_count += 1
            event.available_at = lease_until
            messages.append(
                OutboxMessage(
                    id=event.id,
                    event_type=event.event_type,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    payload=event.payload,
                    attempt_count=event.attempt_count,
                )
            )

    for message in messages:
        handler = handlers.get(message.event_type)
        try:
            if handler is None:
                raise LookupError(f"No outbox handler for {message.event_type!r}")
            handler(message)
        except Exception as exc:
            retry_at = datetime.now(UTC) + timedelta(
                seconds=_retry_delay_seconds(message.attempt_count)
            )
            with session_factory.begin() as session:
                session.execute(
                    update(OutboxEvent)
                    .where(
                        OutboxEvent.id == message.id,
                        OutboxEvent.published_at.is_(None),
                        OutboxEvent.available_at == lease_until,
                    )
                    .values(available_at=retry_at)
                )
            logger.error(
                "outbox_delivery_failed",
                extra={
                    "event_id": str(message.id),
                    "event_type": message.event_type,
                    "attempt_count": message.attempt_count,
                    "error_type": type(exc).__name__,
                    "retry_at": retry_at.isoformat(),
                },
            )
            continue

        with session_factory.begin() as session:
            session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == message.id, OutboxEvent.published_at.is_(None))
                .values(published_at=datetime.now(UTC))
            )

    return len(messages)


def _retry_delay_seconds(attempt_count: int) -> float:
    ceiling = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** min(attempt_count - 1, 16)))
    return ceiling / 2 + random.uniform(0, ceiling / 2)
