from collections.abc import Callable, Iterable

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .db import get_db
from .models import Employee, Role, User
from .security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from exc

    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    if user.tenant_id != payload.get("tenant_id"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token tenant mismatch")
    return user


def require_roles(*roles: Role) -> Callable[[User], User]:
    allowed: Iterable[Role] = roles

    def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return user

    return _dep


require_admin = require_roles(Role.ADMIN)
require_payroll = require_roles(Role.ADMIN, Role.HR)
require_manager = require_roles(Role.ADMIN, Role.HR, Role.MANAGER)


def assert_can_view_employee(db: Session, user: User, employee: Employee) -> None:
    """Employees see only their own data; managers see their team."""
    if employee.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    if user.role in (Role.ADMIN, Role.HR):
        return
    if user.role == Role.MANAGER:
        if employee.manager_id == user.employee_id or employee.id == user.employee_id:
            return
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your team member")
    if user.employee_id != employee.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
