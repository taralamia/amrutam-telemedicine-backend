from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token, subject_id
from app.db import get_db
from app.models import Role, User

bearer_scheme = HTTPBearer(auto_error=False)
DbSession = Annotated[Session, Depends(get_db)]


def current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(bearer_scheme),
    ],
    session: DbSession,
    request: Request,
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        claims = decode_access_token(credentials.credentials)
        user_id = subject_id(claims)
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=401, detail="Invalid access token") from exc

    user = session.get(User, user_id)
    if (
        user is None
        or not user.is_active
        or user.deleted_at is not None
        or user.role.value != claims.get("role")
    ):
        raise HTTPException(status_code=401, detail="Inactive account")
    request.state.auth_claims = claims
    return user


def require_roles(*roles: Role) -> Callable:
    def role_dependency(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user

    return role_dependency


def require_mfa(
    request: Request,
    user: Annotated[User, Depends(current_user)],
) -> None:
    claims = getattr(request.state, "auth_claims", {})
    if not user.mfa_enabled or claims.get("mfa") is not True:
        raise HTTPException(status_code=403, detail="Verified MFA is required")
