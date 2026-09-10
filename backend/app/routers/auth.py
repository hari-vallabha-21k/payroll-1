from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import client_ip, get_current_user, require_admin
from ..models import Tenant, User
from ..schemas import LoginRequest, TokenResponse, UserCreate, UserOut
from ..security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    stmt = select(User).where(User.email == payload.email.lower())
    if payload.tenant_code:
        stmt = stmt.join(Tenant, Tenant.id == User.tenant_id).where(
            Tenant.code == payload.tenant_code
        )
    user = db.execute(stmt).scalars().first()

    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")

    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="LOGIN",
        entity_type="user",
        entity_id=user.id,
        ip_address=client_ip(request),
    )
    db.commit()

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.post("/logout")
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Tokens are stateless; the client discards it. Logged for the audit trail.
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="LOGOUT",
        entity_type="user",
        entity_id=user.id,
    )
    db.commit()
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    exists = db.execute(
        select(User).where(User.tenant_id == admin.tenant_id, User.email == payload.email.lower())
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with that email already exists")

    user = User(
        tenant_id=admin.tenant_id,
        email=payload.email.lower(),
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
        employee_id=payload.employee_id,
    )
    db.add(user)
    audit.record(
        db,
        tenant_id=admin.tenant_id,
        user_id=admin.id,
        actor=admin.email,
        action="CREATE_USER",
        entity_type="user",
        detail={"email": payload.email, "role": payload.role.value},
    )
    db.commit()
    db.refresh(user)
    return user
