"""Employee biometric administration: the profile panel and its actions."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import assert_can_view_employee, client_ip, get_current_user, require_payroll
from ..models import BiometricStatus, User, WebAuthnCredential
from ..schemas import (
    BiometricDecision,
    BiometricPanelOut,
    CredentialOut,
)
from ..services import biometric as biometric_service
from ..services.biometric import BiometricError
from .employees import get_employee_or_404

router = APIRouter(prefix="/api/employees", tags=["biometric"])


def _panel(db: Session, employee) -> BiometricPanelOut:
    eligibility = biometric_service.check_attendance_eligibility(db, employee)
    credentials = list(
        db.execute(
            select(WebAuthnCredential)
            .where(WebAuthnCredential.employee_id == employee.id)
            .order_by(WebAuthnCredential.id.desc())
        ).scalars()
    )
    active = [c for c in credentials if c.status.value == "ACTIVE"]
    return BiometricPanelOut(
        employee_id=employee.id,
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        employment_status=employee.status,
        biometric_status=employee.biometric_status,
        authentication_method="WebAuthn / Device Biometric",
        registered_on=employee.biometric_registered_at,
        verified_on=employee.biometric_verified_at,
        last_used=max((c.last_used_at for c in active if c.last_used_at), default=None),
        note=employee.biometric_note,
        attendance_enabled=eligibility.allowed,
        blocked_reason=eligibility.reason,
        credentials=[CredentialOut.model_validate(c) for c in credentials],
    )


def _act(
    db: Session,
    request: Request,
    user: User,
    employee_id: int,
    action: str,
    operation,
) -> BiometricPanelOut:
    """Run a lifecycle transition, audit it, and return the refreshed panel."""
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    before = employee.biometric_status
    try:
        operation(employee)
    except BiometricError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action=action,
        entity_type="employee",
        entity_id=employee.id,
        detail={"from": before.value, "to": employee.biometric_status.value},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(employee)
    return _panel(db, employee)


@router.get("/{employee_id}/biometric", response_model=BiometricPanelOut)
def get_panel(
    employee_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    assert_can_view_employee(db, user, employee)
    return _panel(db, employee)


@router.post("/{employee_id}/biometric/verify", response_model=BiometricPanelOut)
def verify_biometric(
    employee_id: int,
    payload: BiometricDecision,
    request: Request,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Human approval - the only route from registered to attendance-enabled."""
    return _act(
        db,
        request,
        user,
        employee_id,
        "BIOMETRIC_VERIFIED",
        lambda employee: biometric_service.verify(
            db, employee, user_id=user.id, note=payload.reason
        ),
    )


@router.post("/{employee_id}/biometric/reject", response_model=BiometricPanelOut)
def reject_biometric(
    employee_id: int,
    payload: BiometricDecision,
    request: Request,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    if not payload.reason:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A reason is required to reject")
    return _act(
        db,
        request,
        user,
        employee_id,
        "BIOMETRIC_REJECTED",
        lambda employee: biometric_service.reject(
            db, employee, user_id=user.id, reason=payload.reason
        ),
    )


@router.post("/{employee_id}/biometric/disable", response_model=BiometricPanelOut)
def disable_biometric(
    employee_id: int,
    payload: BiometricDecision,
    request: Request,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    return _act(
        db,
        request,
        user,
        employee_id,
        "BIOMETRIC_DISABLED",
        lambda employee: biometric_service.disable(
            db, employee, user_id=user.id, reason=payload.reason
        ),
    )


@router.post("/{employee_id}/biometric/enable", response_model=BiometricPanelOut)
def enable_biometric(
    employee_id: int,
    payload: BiometricDecision,
    request: Request,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    return _act(
        db,
        request,
        user,
        employee_id,
        "BIOMETRIC_ENABLED",
        lambda employee: biometric_service.enable(db, employee, user_id=user.id),
    )


@router.get("/biometric/pending", response_model=list[BiometricPanelOut])
def list_pending(user: User = Depends(require_payroll), db: Session = Depends(get_db)):
    """Registrations waiting for a human decision."""
    from ..models import Employee

    employees = db.execute(
        select(Employee).where(
            Employee.tenant_id == user.tenant_id,
            Employee.biometric_status == BiometricStatus.PENDING_VERIFICATION,
        )
    ).scalars()
    return [_panel(db, employee) for employee in employees]
