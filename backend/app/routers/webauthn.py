"""Public attendance kiosk + biometric enrolment endpoints.

These endpoints are unauthenticated by design: the employee proves who they
are with their phone's platform authenticator, not with a password.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..deps import client_ip, require_payroll
from ..models import (
    Employee,
    EnrollmentToken,
    EventSource,
    Tenant,
    User,
    WebAuthnCredential,
    utcnow,
)
from ..schemas import (
    CredentialOut,
    DailyAttendanceOut,
    EnrollmentTokenOut,
    KioskEmployeeOut,
    PunchResult,
    WebAuthnAuthVerify,
    WebAuthnOptionsRequest,
    WebAuthnRegisterOptionsRequest,
    WebAuthnRegisterVerify,
)
from ..services import biometric as biometric_service
from ..services import punch as punch_service
from ..services import webauthn_service
from ..services.webauthn_service import WebAuthnError

router = APIRouter(prefix="/api/webauthn", tags=["webauthn"])
settings = get_settings()

ENROLLMENT_TTL_MINUTES = 30


def resolve_employee(db: Session, employee_code: str, tenant_code: str | None) -> Employee:
    stmt = select(Employee).where(Employee.employee_code == employee_code.strip().upper())
    if tenant_code:
        stmt = stmt.join(Tenant, Tenant.id == Employee.tenant_id).where(Tenant.code == tenant_code)
    employees = list(db.execute(stmt).scalars())
    if not employees:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    if len(employees) > 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Employee code is ambiguous; a restaurant code is required"
        )
    return employees[0]


def require_attendance_eligible(db: Session, employee: Employee) -> None:
    """Refuse before any ceremony starts, so an unverified employee never
    reaches the authenticator."""
    eligibility = biometric_service.check_attendance_eligibility(db, employee)
    if not eligibility.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, eligibility.reason)


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_token(db: Session, token: str) -> tuple[EnrollmentToken, Employee]:
    record = db.execute(
        select(EnrollmentToken).where(EnrollmentToken.token == token)
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invalid enrolment link")
    if record.used_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "This enrolment link has already been used")
    if record.expires_at < _naive_utcnow():
        raise HTTPException(status.HTTP_410_GONE, "This enrolment link has expired")
    employee = db.get(Employee, record.employee_id)
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    return record, employee


# --- kiosk lookup -----------------------------------------------------------
@router.post("/lookup", response_model=KioskEmployeeOut)
def lookup(payload: WebAuthnOptionsRequest, db: Session = Depends(get_db)):
    employee = resolve_employee(db, payload.employee_code, payload.tenant_code)
    eligibility = biometric_service.check_attendance_eligibility(db, employee)
    return KioskEmployeeOut(
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        has_biometric=bool(webauthn_service.credentials_for(db, employee)),
        biometric_status=employee.biometric_status,
        can_authenticate=eligibility.allowed,
        blocked_reason=eligibility.reason,
        next_action=punch_service.next_event_type(db, employee),
    )


# --- enrolment (admin issues a link, employee completes it on their phone) ---
@router.post("/enrollment-token", response_model=EnrollmentTokenOut)
def create_enrollment_token(
    employee_id: int,
    request: Request,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    employee = db.execute(
        select(Employee).where(Employee.id == employee_id, Employee.tenant_id == user.tenant_id)
    ).scalar_one_or_none()
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")

    token = EnrollmentToken(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        token=secrets.token_urlsafe(32),
        expires_at=_naive_utcnow() + timedelta(minutes=ENROLLMENT_TTL_MINUTES),
        created_by_user_id=user.id,
    )
    db.add(token)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="ISSUE_ENROLLMENT_TOKEN",
        entity_type="employee",
        entity_id=employee.id,
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(token)

    base = str(request.base_url).rstrip("/")
    return EnrollmentTokenOut(
        token=token.token,
        employee_id=employee.id,
        employee_code=employee.employee_code,
        expires_at=token.expires_at,
        enroll_url=f"{base}/enroll?token={token.token}",
    )


@router.post("/register/options")
def register_options(payload: WebAuthnRegisterOptionsRequest, db: Session = Depends(get_db)):
    _, employee = resolve_token(db, payload.token)
    try:
        return webauthn_service.registration_options(db, employee)
    except WebAuthnError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/register/verify", response_model=CredentialOut)
def register_verify(
    payload: WebAuthnRegisterVerify, request: Request, db: Session = Depends(get_db)
):
    token, employee = resolve_token(db, payload.token)
    try:
        credential = webauthn_service.verify_registration(
            db, employee, payload.credential, payload.device_label
        )
    except WebAuthnError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    token.used_at = utcnow()
    biometric_service.mark_registered(db, employee)
    audit.record(
        db,
        tenant_id=employee.tenant_id,
        action="BIOMETRIC_REGISTERED",
        entity_type="employee",
        entity_id=employee.id,
        actor=employee.employee_code,
        detail={
            "credential_id": credential.credential_id,
            "biometric_status": employee.biometric_status.value,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(credential)
    return credential


# --- attendance authentication ---------------------------------------------
@router.post("/authenticate/options")
def authenticate_options(payload: WebAuthnOptionsRequest, db: Session = Depends(get_db)):
    employee = resolve_employee(db, payload.employee_code, payload.tenant_code)
    require_attendance_eligible(db, employee)
    try:
        return webauthn_service.authentication_options(db, employee)
    except WebAuthnError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/authenticate/verify", response_model=PunchResult)
def authenticate_verify(
    payload: WebAuthnAuthVerify, request: Request, db: Session = Depends(get_db)
):
    employee = resolve_employee(db, payload.employee_code, payload.tenant_code)
    require_attendance_eligible(db, employee)
    try:
        credential = webauthn_service.verify_authentication(db, employee, payload.credential)
    except WebAuthnError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    try:
        event, daily = punch_service.record_punch(
            db,
            employee,
            event_type=payload.event_type,
            source=EventSource.WEBAUTHN,
            credential_id=credential.credential_id,
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except punch_service.PunchError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    audit.record(
        db,
        tenant_id=employee.tenant_id,
        action=event.event_type.value,
        entity_type="attendance_event",
        entity_id=event.id,
        actor=employee.employee_code,
        detail={"source": event.source.value},
        ip_address=client_ip(request),
    )
    db.commit()

    action = "Checked in" if event.event_type.value == "CHECK_IN" else "Checked out"
    return PunchResult(
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        event_type=event.event_type,
        event_time=event.event_time,
        message=f"{action} at {event.event_time.strftime('%H:%M:%S')}",
        daily=DailyAttendanceOut.model_validate(daily) if daily else None,
    )


# --- credential management --------------------------------------------------
@router.get("/credentials/{employee_id}", response_model=list[CredentialOut])
def list_credentials(
    employee_id: int, user: User = Depends(require_payroll), db: Session = Depends(get_db)
):
    return list(
        db.execute(
            select(WebAuthnCredential).where(
                WebAuthnCredential.employee_id == employee_id,
                WebAuthnCredential.tenant_id == user.tenant_id,
            )
        ).scalars()
    )


@router.delete("/credentials/{credential_id}")
def revoke_credential(
    credential_id: int, user: User = Depends(require_payroll), db: Session = Depends(get_db)
):
    credential = db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.id == credential_id, WebAuthnCredential.tenant_id == user.tenant_id
        )
    ).scalar_one_or_none()
    if credential is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Credential not found")
    biometric_service.revoke_credential(db, credential)
    employee = db.get(Employee, credential.employee_id)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="BIOMETRIC_CREDENTIAL_REVOKED",
        entity_type="webauthn_credential",
        entity_id=credential.id,
        detail={
            "employee_id": credential.employee_id,
            "biometric_status": employee.biometric_status.value if employee else None,
        },
    )
    db.commit()
    return {"detail": "Credential revoked"}
