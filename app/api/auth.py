import hashlib
from typing import Annotated

import pyotp
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import DbSession, current_user
from app.core.auth_idempotency import execute_auth_write
from app.core.config import get_settings
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    RefreshTokenReuseError,
    create_access_token,
    create_refresh_session,
    hash_password,
    revoke_refresh_session,
    rotate_refresh_session,
    verify_password,
    verify_password_and_mfa,
)
from app.models import Profile, Role, User
from app.schemas.auth import (
    LoginRequest,
    MFAEnabledOut,
    MFAEnableRequest,
    MFASetupOut,
    MFASetupRequest,
    PatientRegistration,
    RefreshRequest,
    TokenPair,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


def _token_pair(user: User, refresh_token: str, *, mfa_verified: bool) -> dict:
    settings = get_settings()
    return {
        "access_token": create_access_token(user, mfa_verified=mfa_verified),
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": settings.access_token_minutes * 60,
    }


def _user_response(user: User, full_name: str) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role.value,
        "full_name": full_name,
    }


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    body: PatientRegistration,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation():
        email = str(body.email).strip().lower()
        user = User(
            email=email,
            password_hash=hash_password(body.password),
            role=Role.patient,
        )
        session.add(user)
        session.flush()
        session.add(Profile(user_id=user.id, full_name=body.full_name.strip()))
        return status.HTTP_201_CREATED, _user_response(user, body.full_name.strip())

    _, response, _ = execute_auth_write(
        session,
        scope="auth-register",
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=operation,
    )
    return response


@router.post("/login", response_model=TokenPair)
def login(
    body: LoginRequest,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation():
        email = str(body.email).strip().lower()
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            verify_password(body.password, DUMMY_PASSWORD_HASH)
            raise HTTPException(status_code=401, detail="Invalid credentials")
        credentials_valid = verify_password_and_mfa(user, body.password, body.mfa_code)
        if not credentials_valid or not user.is_active or user.deleted_at is not None:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        refresh_token = create_refresh_session(session, user)
        return status.HTTP_200_OK, _token_pair(user, refresh_token, mfa_verified=user.mfa_enabled)

    _, response, _ = execute_auth_write(
        session,
        scope="auth-login",
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=operation,
    )
    return response


@router.post("/refresh", response_model=TokenPair)
def refresh(
    body: RefreshRequest,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation():
        try:
            user, refresh_token = rotate_refresh_session(session, body.refresh_token)
        except RefreshTokenReuseError:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return status.HTTP_200_OK, _token_pair(user, refresh_token, mfa_verified=user.mfa_enabled)

    try:
        _, response, _ = execute_auth_write(
            session,
            scope="auth-refresh",
            key=idempotency_key,
            request_body={
                "refresh_token_hash": hashlib.sha256(body.refresh_token.encode()).hexdigest()
            },
            operation=operation,
        )
    except RefreshTokenReuseError as exc:
        raise HTTPException(
            status_code=401, detail="Refresh token reuse detected; sign in again"
        ) from exc
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    body: RefreshRequest,
    session: DbSession,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> Response:
    execute_auth_write(
        session,
        scope="auth-logout",
        key=idempotency_key,
        request_body={
            "refresh_token_hash": hashlib.sha256(body.refresh_token.encode()).hexdigest()
        },
        operation=lambda: (
            status.HTTP_204_NO_CONTENT,
            _logout_operation(session, body.refresh_token),
        ),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _logout_operation(session: Session, refresh_token: str) -> dict:
    revoke_refresh_session(session, refresh_token)
    return {"logged_out": True}


@router.post("/mfa/setup", response_model=MFASetupOut)
def mfa_setup(
    body: MFASetupRequest,
    session: DbSession,
    user: Annotated[User, Depends(current_user)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation():
        if not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Reauthentication required")
        if user.mfa_enabled:
            raise HTTPException(status_code=409, detail="MFA is already enabled")
        if user.mfa_pending_secret_encrypted is None:
            user.mfa_pending_secret_encrypted = pyotp.random_base32()
        secret = user.mfa_pending_secret_encrypted
        return status.HTTP_200_OK, {
            "secret": secret,
            "provisioning_uri": pyotp.TOTP(secret).provisioning_uri(
                name=user.email,
                issuer_name=get_settings().app_name,
            ),
        }

    _, response, _ = execute_auth_write(
        session,
        scope=f"auth-mfa-setup:{user.id}",
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=operation,
    )
    return response


@router.post("/mfa/enable", response_model=MFAEnabledOut)
def mfa_enable(
    body: MFAEnableRequest,
    session: DbSession,
    user: Annotated[User, Depends(current_user)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict:
    def operation():
        secret = user.mfa_pending_secret_encrypted
        if secret is None and user.mfa_enabled:
            secret = user.mfa_secret_encrypted
        if secret is None or not pyotp.TOTP(secret).verify(body.code, valid_window=1):
            raise HTTPException(status_code=400, detail="Invalid MFA code")
        user.mfa_secret_encrypted = secret
        user.mfa_pending_secret_encrypted = None
        user.mfa_enabled = True
        return status.HTTP_200_OK, {
            "enabled": True,
            "access_token": create_access_token(user, mfa_verified=True),
            "token_type": "bearer",
        }

    _, response, _ = execute_auth_write(
        session,
        scope=f"auth-mfa-enable:{user.id}",
        key=idempotency_key,
        request_body=body.model_dump(mode="json"),
        operation=operation,
    )
    return response


@router.get("/me", response_model=UserOut)
def get_me(
    session: DbSession,
    user: Annotated[User, Depends(current_user)],
) -> dict:
    profile = session.scalar(select(Profile).where(Profile.user_id == user.id))
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _user_response(user, profile.full_name)
