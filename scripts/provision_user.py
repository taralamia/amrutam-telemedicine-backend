import getpass

from pydantic import TypeAdapter, ValidationError
from pydantic.networks import EmailStr
from sqlalchemy import select

from app.core.security import hash_password
from app.db import SessionLocal
from app.models import Profile, Role, User

email_adapter = TypeAdapter(EmailStr)


def main() -> None:
    try:
        email = str(email_adapter.validate_python(input("Account email: ").strip())).lower()
    except ValidationError as exc:
        raise SystemExit("Provide a valid email address") from exc

    role_value = input("Role (doctor/admin): ").strip().lower()
    if role_value not in {Role.doctor.value, Role.admin.value}:
        raise SystemExit("Only doctor and admin accounts may be provisioned")
    full_name = input("Display name: ").strip()
    if not full_name or len(full_name) > 200:
        raise SystemExit("Display name must contain 1 to 200 characters")

    password = getpass.getpass("Initial password (12+ characters): ")
    if len(password) < 12 or not password.strip():
        raise SystemExit("Password must be at least 12 characters and not whitespace-only")

    with SessionLocal.begin() as session:
        if session.scalar(select(User.id).where(User.email == email)):
            raise SystemExit("Email already exists")
        user = User(email=email, password_hash=hash_password(password), role=Role(role_value))
        session.add(user)
        session.flush()
        session.add(Profile(user_id=user.id, full_name=full_name))

    print(
        f"Provisioned {role_value} account {email}; require MFA enrollment before privileged use."
    )


if __name__ == "__main__":
    main()
