"""Biometric registration, verification and access control."""

from datetime import datetime

import pytest
from sqlalchemy import select

from backend.app.models import (
    BiometricStatus,
    CredentialStatus,
    Employee,
    EmployeeStatus,
    WebAuthnCredential,
)
from backend.app.services import biometric as biometric_service


@pytest.fixture
def employee(db, client, auth):
    """A fresh employee per test, so lifecycle states cannot leak between them."""
    import uuid

    code = f"BIO{uuid.uuid4().hex[:6].upper()}"
    created = client.post(
        "/api/employees",
        headers=auth,
        json={
            "employee_code": code,
            "first_name": "Bio",
            "last_name": "Subject",
            "date_of_joining": "2026-01-01",
        },
    )
    assert created.status_code == 201, created.text
    return db.get(Employee, created.json()["id"])


def register_credential(db, employee, *, credential_id: str | None = None) -> WebAuthnCredential:
    """Stand in for a completed WebAuthn registration ceremony."""
    credential = WebAuthnCredential(
        tenant_id=employee.tenant_id,
        employee_id=employee.id,
        credential_id=credential_id or f"cred-{employee.id}-{datetime.now().timestamp()}",
        public_key=b"\x00",
        sign_count=0,
        device_name="Test phone",
    )
    db.add(credential)
    biometric_service.mark_registered(db, employee)
    db.commit()
    db.refresh(employee)
    return credential


def test_new_employee_starts_unregistered(client, auth, employee):
    assert employee.status == EmployeeStatus.ACTIVE
    assert employee.biometric_status == BiometricStatus.NOT_REGISTERED

    panel = client.get(f"/api/employees/{employee.id}/biometric", headers=auth).json()
    assert panel["biometric_status"] == "NOT_REGISTERED"
    assert panel["attendance_enabled"] is False
    assert panel["credentials"] == []


def test_registration_moves_to_pending_not_verified(db, client, auth, employee):
    register_credential(db, employee)

    panel = client.get(f"/api/employees/{employee.id}/biometric", headers=auth).json()
    assert panel["biometric_status"] == "PENDING_VERIFICATION"
    # Registering must not by itself enable attendance.
    assert panel["attendance_enabled"] is False
    assert panel["registered_on"] is not None
    assert panel["verified_on"] is None


def test_verification_enables_attendance(db, client, auth, employee):
    register_credential(db, employee)

    verified = client.post(
        f"/api/employees/{employee.id}/biometric/verify",
        headers=auth,
        json={"reason": "Identity confirmed in person"},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["biometric_status"] == "VERIFIED"
    assert verified.json()["attendance_enabled"] is True
    assert verified.json()["verified_on"] is not None


def test_unverified_employee_cannot_start_a_ceremony(db, client, auth, employee):
    """The ceremony must be refused before any challenge is issued."""
    register_credential(db, employee)

    lookup = client.post(
        "/api/webauthn/lookup",
        json={"employee_code": employee.employee_code, "tenant_code": "REST001"},
    ).json()
    assert lookup["can_authenticate"] is False
    assert "has not been verified" in lookup["blocked_reason"]

    options = client.post(
        "/api/webauthn/authenticate/options",
        json={"employee_code": employee.employee_code, "tenant_code": "REST001"},
    )
    assert options.status_code == 403
    assert "has not been verified" in options.json()["detail"]

    # No challenge may have been created for a blocked employee.
    from backend.app.models import WebAuthnChallenge

    challenges = db.execute(
        select(WebAuthnChallenge).where(WebAuthnChallenge.employee_id == employee.id)
    ).scalars()
    assert list(challenges) == []


def test_verified_employee_reaches_the_ceremony(db, client, auth, employee):
    register_credential(db, employee)
    client.post(
        f"/api/employees/{employee.id}/biometric/verify", headers=auth, json={"reason": "ok"}
    )

    options = client.post(
        "/api/webauthn/authenticate/options",
        json={"employee_code": employee.employee_code, "tenant_code": "REST001"},
    )
    assert options.status_code == 200
    assert "challenge" in options.json()


def test_inactive_employee_is_blocked_even_when_verified(db, client, auth, employee):
    """Biometric status is independent of employment status; both gates apply."""
    register_credential(db, employee)
    client.post(
        f"/api/employees/{employee.id}/biometric/verify", headers=auth, json={"reason": "ok"}
    )

    client.delete(f"/api/employees/{employee.id}", headers=auth)  # soft delete -> INACTIVE
    db.expire_all()

    options = client.post(
        "/api/webauthn/authenticate/options",
        json={"employee_code": employee.employee_code, "tenant_code": "REST001"},
    )
    assert options.status_code == 403
    assert "inactive" in options.json()["detail"].lower()

    refreshed = db.get(Employee, employee.id)
    assert refreshed.biometric_status == BiometricStatus.VERIFIED  # unchanged by employment


def test_rejection_revokes_the_credential(db, client, auth, employee):
    register_credential(db, employee)

    rejected = client.post(
        f"/api/employees/{employee.id}/biometric/reject",
        headers=auth,
        json={"reason": "Could not confirm identity"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["biometric_status"] == "NOT_REGISTERED"
    assert all(c["status"] == "REVOKED" for c in rejected.json()["credentials"])


def test_reject_requires_a_reason(db, client, auth, employee):
    register_credential(db, employee)
    response = client.post(f"/api/employees/{employee.id}/biometric/reject", headers=auth, json={})
    assert response.status_code == 400


def test_disable_then_re_enable(db, client, auth, employee):
    register_credential(db, employee)
    client.post(
        f"/api/employees/{employee.id}/biometric/verify", headers=auth, json={"reason": "ok"}
    )

    disabled = client.post(
        f"/api/employees/{employee.id}/biometric/disable",
        headers=auth,
        json={"reason": "On suspension"},
    )
    assert disabled.json()["biometric_status"] == "DISABLED"
    assert disabled.json()["attendance_enabled"] is False

    blocked = client.post(
        "/api/webauthn/authenticate/options",
        json={"employee_code": employee.employee_code, "tenant_code": "REST001"},
    )
    assert blocked.status_code == 403

    enabled = client.post(f"/api/employees/{employee.id}/biometric/enable", headers=auth, json={})
    assert enabled.json()["biometric_status"] == "VERIFIED"
    assert enabled.json()["attendance_enabled"] is True


def test_revoking_the_last_credential_resets_status(db, client, auth, employee):
    credential = register_credential(db, employee)
    client.post(
        f"/api/employees/{employee.id}/biometric/verify", headers=auth, json={"reason": "ok"}
    )

    revoked = client.delete(f"/api/webauthn/credentials/{credential.id}", headers=auth)
    assert revoked.status_code == 200

    panel = client.get(f"/api/employees/{employee.id}/biometric", headers=auth).json()
    assert panel["biometric_status"] == "NOT_REGISTERED"
    assert panel["attendance_enabled"] is False


def test_second_device_does_not_unverify_the_employee(db, client, auth, employee):
    register_credential(db, employee, credential_id="device-one")
    client.post(
        f"/api/employees/{employee.id}/biometric/verify", headers=auth, json={"reason": "ok"}
    )
    register_credential(db, employee, credential_id="device-two")

    db.refresh(employee)
    assert employee.biometric_status == BiometricStatus.VERIFIED


def test_cannot_verify_without_a_credential(client, auth, employee):
    response = client.post(
        f"/api/employees/{employee.id}/biometric/verify", headers=auth, json={"reason": "ok"}
    )
    assert response.status_code == 409


def test_lifecycle_actions_are_audited(db, client, auth, employee):
    register_credential(db, employee)
    client.post(
        f"/api/employees/{employee.id}/biometric/verify", headers=auth, json={"reason": "ok"}
    )
    logs = client.get("/api/audit-logs?action=BIOMETRIC_VERIFIED", headers=auth).json()
    assert any(log["entity_id"] == str(employee.id) for log in logs)


def test_employee_role_cannot_verify_biometrics(client, auth, employee, db):
    register_credential(db, employee)
    client.post(
        "/api/auth/users",
        headers=auth,
        json={
            "email": f"staff{employee.id}@abcrestaurant.in",
            "full_name": "Staff User",
            "password": "staff123456",
            "role": "EMPLOYEE",
        },
    )
    token = client.post(
        "/api/auth/login",
        json={"email": f"staff{employee.id}@abcrestaurant.in", "password": "staff123456"},
    ).json()["access_token"]

    response = client.post(
        f"/api/employees/{employee.id}/biometric/verify",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "self approval"},
    )
    assert response.status_code == 403


def test_credentials_never_store_biometric_data(db, employee):
    """Only key material is persisted - never a fingerprint image or template."""
    credential = register_credential(db, employee)
    columns = set(WebAuthnCredential.__table__.columns.keys())
    forbidden = {"fingerprint", "template", "biometric_data", "image", "face"}
    assert not (columns & forbidden)
    assert credential.status == CredentialStatus.ACTIVE
    assert credential.public_key  # a public key, which is not secret
