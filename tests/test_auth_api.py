import jwt
import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.security import create_access_token, hash_password
from app.db import Base
from app.main import app
from app.models import Profile, RefreshSession, Role, User


@pytest.fixture
def auth_api():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(engine, class_=Session, expire_on_commit=False)

    def override_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield {"client": client, "session_factory": session_factory}
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def create_user(session_factory, *, email="patient@example.com", role=Role.patient, mfa=False):
    with session_factory.begin() as session:
        user = User(
            email=email,
            password_hash=hash_password("patient password long enough"),
            role=role,
            mfa_enabled=mfa,
            mfa_secret_encrypted=pyotp.random_base32() if mfa else None,
        )
        session.add(user)
        session.flush()
        session.add(Profile(user_id=user.id, full_name="Sample Patient"))
        return user.id


def test_registration_is_patient_only_and_keyed_retry_replays(auth_api):
    client = auth_api["client"]
    body = {
        "email": "new.patient@example.com",
        "password": "a sufficiently long password",
        "full_name": "New Patient",
        "role": "patient",
    }
    headers = {"Idempotency-Key": "registration-1"}
    first = client.post("/v1/auth/register", json=body, headers=headers)
    replay = client.post("/v1/auth/register", json=body, headers=headers)
    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()

    changed_body = {**body, "full_name": "Changed Name"}
    changed = client.post("/v1/auth/register", json=changed_body, headers=headers)
    assert changed.status_code == 409

    doctor = client.post(
        "/v1/auth/register",
        json={**body, "email": "not-allowed@example.com", "role": "doctor"},
        headers={"Idempotency-Key": "registration-2"},
    )
    assert doctor.status_code == 422


def test_login_mfa_enrollment_and_claims(auth_api):
    session_factory = auth_api["session_factory"]
    user_id = create_user(session_factory)
    client = auth_api["client"]
    login = client.post(
        "/v1/auth/login",
        json={"email": "patient@example.com", "password": "patient password long enough"},
        headers={"Idempotency-Key": "login-1"},
    )
    assert login.status_code == 200
    claims = jwt.decode(
        login.json()["access_token"],
        get_settings().jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        issuer=get_settings().jwt_issuer,
    )
    assert claims["mfa"] is False
    auth_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    setup = client.post(
        "/v1/auth/mfa/setup",
        json={"password": "patient password long enough"},
        headers={**auth_headers, "Idempotency-Key": "mfa-setup-1"},
    )
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    code = pyotp.TOTP(secret).now()
    enabled = client.post(
        "/v1/auth/mfa/enable",
        json={"code": code},
        headers={**auth_headers, "Idempotency-Key": "mfa-enable-1"},
    )
    assert enabled.status_code == 200
    mfa_claims = jwt.decode(
        enabled.json()["access_token"],
        get_settings().jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        issuer=get_settings().jwt_issuer,
    )
    assert mfa_claims["mfa"] is True

    with session_factory() as session:
        user = session.get(User, user_id)
        assert user.mfa_enabled is True
        assert user.mfa_pending_secret_encrypted is None
        stored = session.execute(
            text("SELECT mfa_secret_encrypted FROM users WHERE id = :user_id"),
            {"user_id": user_id.hex},
        ).scalar_one()
        assert stored.startswith("v1:")
        assert stored != secret


def test_refresh_rotation_retry_is_idempotent_and_reuse_revokes_family(auth_api):
    client = auth_api["client"]
    login = client.post(
        "/v1/auth/register",
        json={
            "email": "refresh.patient@example.com",
            "password": "a sufficiently long password",
            "full_name": "Refresh Patient",
        },
        headers={"Idempotency-Key": "registration-refresh"},
    )
    assert login.status_code == 201
    tokens = client.post(
        "/v1/auth/login",
        json={
            "email": "refresh.patient@example.com",
            "password": "a sufficiently long password",
        },
        headers={"Idempotency-Key": "login-refresh"},
    ).json()
    body = {"refresh_token": tokens["refresh_token"]}
    headers = {"Idempotency-Key": "refresh-rotation-1"}
    first = client.post("/v1/auth/refresh", json=body, headers=headers)
    replay = client.post("/v1/auth/refresh", json=body, headers=headers)
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()

    reuse = client.post(
        "/v1/auth/refresh",
        json=body,
        headers={"Idempotency-Key": "refresh-rotation-different"},
    )
    assert reuse.status_code == 401
    with auth_api["session_factory"]() as session:
        active_sessions = session.scalars(
            select(RefreshSession).where(
                RefreshSession.revoked_at.is_(None),
                RefreshSession.token_family_id
                == session.scalar(
                    select(RefreshSession.token_family_id).where(
                        RefreshSession.user_id
                        == session.scalar(
                            select(User.id).where(User.email == "refresh.patient@example.com")
                        )
                    )
                ),
            )
        ).all()
        assert active_sessions == []


def test_token_role_claim_cannot_override_database_role(auth_api):
    session_factory = auth_api["session_factory"]
    admin_id = create_user(session_factory, email="admin@example.com", role=Role.admin)
    with session_factory() as session:
        admin = session.get(User, admin_id)
        token = create_access_token(admin, mfa_verified=False)
        claims = jwt.decode(
            token,
            get_settings().jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=get_settings().jwt_issuer,
        )
        claims["role"] = Role.patient.value
        tampered_claim_token = jwt.encode(
            claims,
            get_settings().jwt_secret.get_secret_value(),
            algorithm="HS256",
        )
    response = auth_api["client"].get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {tampered_claim_token}"},
    )
    assert response.status_code == 401


def test_access_token_authenticates_existing_user(auth_api):
    session_factory = auth_api["session_factory"]
    user_id = create_user(session_factory)
    with session_factory() as session:
        user = session.get(User, user_id)
        token = create_access_token(user, mfa_verified=False)
    response = auth_api["client"].get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["id"] == str(user_id)
