"""Employee biometric lifecycle.

Biometric status is deliberately independent of employment status: an ACTIVE
employee may still be NOT_REGISTERED, and attendance requires *both* an active
employment status and a VERIFIED biometric.

    NOT_REGISTERED -> PENDING_VERIFICATION -> VERIFIED
                            |                    |
                            +-> (rejected)       +-> DISABLED -> VERIFIED
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    BiometricStatus,
    CredentialStatus,
    Employee,
    EmployeeStatus,
    WebAuthnCredential,
    utcnow,
)

# Employment statuses that may record attendance at all.
ATTENDANCE_STATUSES = (EmployeeStatus.ACTIVE,)

NOT_VERIFIED_MESSAGE = (
    "Biometric authentication has not been verified for this employee. " "Please contact HR/Admin."
)


class BiometricError(Exception):
    """Raised when a lifecycle transition is not allowed."""


@dataclass
class AttendanceEligibility:
    allowed: bool
    reason: str | None = None


def active_credentials(db: Session, employee: Employee) -> list[WebAuthnCredential]:
    return list(
        db.execute(
            select(WebAuthnCredential).where(
                WebAuthnCredential.employee_id == employee.id,
                WebAuthnCredential.status == CredentialStatus.ACTIVE,
            )
        ).scalars()
    )


def check_attendance_eligibility(db: Session, employee: Employee) -> AttendanceEligibility:
    """Both gates must open before a WebAuthn ceremony may even start."""
    if employee.status not in ATTENDANCE_STATUSES:
        return AttendanceEligibility(
            False,
            f"This employee is {employee.status.value.lower()} and cannot record attendance.",
        )
    if employee.biometric_status != BiometricStatus.VERIFIED:
        return AttendanceEligibility(False, NOT_VERIFIED_MESSAGE)
    if not active_credentials(db, employee):
        return AttendanceEligibility(
            False, "No active biometric credential is registered for this employee."
        )
    return AttendanceEligibility(True)


def mark_registered(db: Session, employee: Employee) -> Employee:
    """Called after a successful WebAuthn registration ceremony.

    Registration alone never enables attendance - a human still has to verify.
    An already-verified employee adding a second device stays verified.
    """
    if employee.biometric_status != BiometricStatus.VERIFIED:
        employee.biometric_status = BiometricStatus.PENDING_VERIFICATION
    if employee.biometric_registered_at is None:
        employee.biometric_registered_at = utcnow()
    return employee


def verify(db: Session, employee: Employee, *, user_id: int, note: str | None = None) -> Employee:
    if employee.biometric_status == BiometricStatus.NOT_REGISTERED:
        raise BiometricError("Nothing to verify: the employee has not registered a biometric yet")
    if not active_credentials(db, employee):
        raise BiometricError("The employee has no active credential to verify")
    employee.biometric_status = BiometricStatus.VERIFIED
    employee.biometric_verified_at = utcnow()
    employee.biometric_verified_by_user_id = user_id
    employee.biometric_note = note
    return employee


def reject(db: Session, employee: Employee, *, user_id: int, reason: str) -> Employee:
    """Reject a pending registration and revoke what was registered."""
    if employee.biometric_status != BiometricStatus.PENDING_VERIFICATION:
        raise BiometricError("Only a pending registration can be rejected")
    for credential in active_credentials(db, employee):
        credential.status = CredentialStatus.REVOKED
        credential.is_active = False
        credential.revoked_at = utcnow()
    employee.biometric_status = BiometricStatus.NOT_REGISTERED
    employee.biometric_registered_at = None
    employee.biometric_verified_at = None
    employee.biometric_verified_by_user_id = user_id
    employee.biometric_note = reason
    return employee


def disable(
    db: Session, employee: Employee, *, user_id: int, reason: str | None = None
) -> Employee:
    """Suspend biometric attendance without discarding the credential."""
    if employee.biometric_status == BiometricStatus.NOT_REGISTERED:
        raise BiometricError("Biometric is not registered for this employee")
    employee.biometric_status = BiometricStatus.DISABLED
    employee.biometric_verified_by_user_id = user_id
    employee.biometric_note = reason
    return employee


def enable(db: Session, employee: Employee, *, user_id: int) -> Employee:
    """Re-enable a disabled biometric, provided a credential still stands."""
    if employee.biometric_status != BiometricStatus.DISABLED:
        raise BiometricError("Only a disabled biometric can be re-enabled")
    if not active_credentials(db, employee):
        raise BiometricError("No active credential remains; the employee must register again")
    employee.biometric_status = BiometricStatus.VERIFIED
    employee.biometric_verified_at = utcnow()
    employee.biometric_verified_by_user_id = user_id
    employee.biometric_note = None
    return employee


def revoke_credential(db: Session, credential: WebAuthnCredential) -> WebAuthnCredential:
    """Revoke one device; the employee falls back if it was their last."""
    credential.status = CredentialStatus.REVOKED
    credential.is_active = False
    credential.revoked_at = utcnow()
    db.flush()

    employee = db.get(Employee, credential.employee_id)
    if employee is not None and not active_credentials(db, employee):
        employee.biometric_status = BiometricStatus.NOT_REGISTERED
        employee.biometric_registered_at = None
        employee.biometric_verified_at = None
        employee.biometric_note = "Last credential revoked"
    return credential
