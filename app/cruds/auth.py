from sqlmodel import Session

from app.core.security import verify_password
from app.cruds.user import get_user_by_email
from app.models.user_model import User

# python -c "import argon2; print(argon2.PasswordHasher().hash('random_password'))"
DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$WVznKXJYiD+dHxVVSty6mA$ohglXBY9opZqH8QQtXF2TCAG8zPCH8xc3BpGBlSWx1M"


def authenticate(*, session: Session, email: str, password: str) -> User | None:
    db_user = get_user_by_email(session=session, email=email)
    if not db_user:
        # Prevent timing attacks by running password verification even when user doesn't exist
        # This ensures the response time is similar whether or not the email exists
        verify_password(password, DUMMY_HASH)
        return None
    verified, updated_password_hash = verify_password(password, db_user.hashed_password)
    if not verified:
        return None
    if updated_password_hash:
        db_user.hashed_password = updated_password_hash
        session.add(db_user)
        session.commit()
        session.refresh(db_user)
    return db_user
