import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pyotp
from pwdlib import PasswordHash
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import RefreshSession, User

password_hasher = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = password_hasher.hash(secrets.token_urlsafe(32))


class RefreshTokenReuseError(ValueError):
    """A rotated refresh token was presented again."""


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_access_token(user: User, *, mfa_verified: bool) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    claims = {
        "sub": str(user.id),
        "role": user.role.value,
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
        "jti": str(uuid4()),
        "typ": "access",
        "mfa": mfa_verified,
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    claims = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
        options={"require": ["sub", "role", "iss", "iat", "exp", "jti", "typ"]},
        leeway=timedelta(seconds=30),
    )
    if claims["typ"] != "access":
        raise jwt.InvalidTokenError("Expected an access token")
    return claims


def hash_refresh_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_refresh_session(session: Session, user: User, *, family_id: UUID | None = None) -> str:
    raw_token = secrets.token_urlsafe(48)
    session.add(
        RefreshSession(
            user_id=user.id,
            token_hash=hash_refresh_token(raw_token),
            token_family_id=family_id or uuid4(),
            expires_at=datetime.now(UTC) + timedelta(days=get_settings().refresh_token_days),
        )
    )
    return raw_token


def rotate_refresh_session(session: Session, raw_token: str) -> tuple[User, str]:
    current = session.scalar(
        select(RefreshSession)
        .where(RefreshSession.token_hash == hash_refresh_token(raw_token))
        .with_for_update()
    )
    now = datetime.now(UTC)
    if current is None:
        raise ValueError("Invalid refresh token")
    if current.revoked_at is not None:
        session.execute(
            update(RefreshSession)
            .where(
                RefreshSession.token_family_id == current.token_family_id,
                RefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        raise RefreshTokenReuseError("Refresh token reuse detected")
    expires_at = current.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        current.revoked_at = now
        raise ValueError("Expired refresh token")

    user = session.get(User, current.user_id)
    if user is None or not user.is_active or user.deleted_at is not None:
        current.revoked_at = now
        raise ValueError("Inactive account")

    current.revoked_at = now
    return user, create_refresh_session(session, user, family_id=current.token_family_id)


def revoke_refresh_session(session: Session, raw_token: str) -> None:
    record = session.scalar(
        select(RefreshSession)
        .where(RefreshSession.token_hash == hash_refresh_token(raw_token))
        .with_for_update()
    )
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)


def verify_password_and_mfa(user: User, password: str, code: str | None) -> bool:
    if not verify_password(password, user.password_hash):
        return False
    if not user.mfa_enabled:
        return code is None
    if code is None or user.mfa_secret_encrypted is None:
        return False
    return pyotp.TOTP(user.mfa_secret_encrypted).verify(code, valid_window=1)


def subject_id(claims: dict) -> UUID:
    return UUID(claims["sub"])
