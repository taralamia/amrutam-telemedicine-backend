import hashlib
import json
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import RefreshTokenReuseError
from app.models import AuthIdempotencyRecord


def execute_auth_write(
    session: Session,
    *,
    scope: str,
    key: str,
    request_body: Any,
    operation: Callable[[], tuple[int, dict[str, Any]]],
) -> tuple[int, dict[str, Any], bool]:
    if not key or len(key) > 255:
        raise HTTPException(status_code=400, detail="A valid Idempotency-Key is required")
    canonical = json.dumps(request_body, sort_keys=True, separators=(",", ":"), default=str)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    table = AuthIdempotencyRecord.__table__
    values = {"scope": scope, "key": key, "request_hash": request_hash}
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert(table)
    elif dialect == "sqlite":
        insert = sqlite_insert(table)
    else:
        raise RuntimeError(f"Auth idempotency unsupported for dialect {dialect!r}")

    try:
        session.execute(
            insert.values(**values).on_conflict_do_nothing(index_elements=["scope", "key"])
        )
        record = session.scalar(
            select(AuthIdempotencyRecord)
            .where(AuthIdempotencyRecord.scope == scope, AuthIdempotencyRecord.key == key)
            .with_for_update()
        )
        if record is None:
            raise RuntimeError("Idempotency record unavailable after insert")
        if record.request_hash != request_hash:
            raise HTTPException(
                status_code=409, detail="Idempotency-Key was used for another request"
            )
        if record.response_encrypted is not None:
            response = json.loads(record.response_encrypted)
            session.commit()
            return record.response_status or 200, response, True

        response_status, response_body = operation()
        record.response_encrypted = json.dumps(
            response_body, sort_keys=True, separators=(",", ":"), default=str
        )
        record.response_status = response_status
        session.commit()
        return response_status, response_body, False
    except RefreshTokenReuseError:
        session.commit()
        raise
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Request conflicts with an existing resource"
        ) from exc
    except Exception:
        session.rollback()
        raise
