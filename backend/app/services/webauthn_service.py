"""WebAuthn ceremonies for employee attendance.

Only credential ids, public keys and signature counters are persisted. The
fingerprint never leaves the employee's phone - the platform authenticator
verifies it locally and signs a challenge.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..config import get_settings
from ..models import Employee, WebAuthnChallenge, WebAuthnCredential, utcnow

settings = get_settings()

CHALLENGE_TTL_SECONDS = 300
PURPOSE_REGISTER = "REGISTER"
PURPOSE_AUTHENTICATE = "AUTHENTICATE"


class WebAuthnError(Exception):
    pass


def _store_challenge(db: Session, employee: Employee, challenge: bytes, purpose: str) -> None:
    # Only one live challenge per employee/purpose.
    for stale in db.execute(
        select(WebAuthnChallenge).where(
            WebAuthnChallenge.employee_id == employee.id,
            WebAuthnChallenge.purpose == purpose,
            WebAuthnChallenge.consumed.is_(False),
        )
    ).scalars():
        stale.consumed = True

    db.add(
        WebAuthnChallenge(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            challenge=challenge,
            purpose=purpose,
            expires_at=datetime.now(UTC).replace(tzinfo=None)
            + timedelta(seconds=CHALLENGE_TTL_SECONDS),
        )
    )
    db.commit()


def _consume_challenge(db: Session, employee: Employee, purpose: str) -> bytes:
    record = (
        db.execute(
            select(WebAuthnChallenge)
            .where(
                WebAuthnChallenge.employee_id == employee.id,
                WebAuthnChallenge.purpose == purpose,
                WebAuthnChallenge.consumed.is_(False),
            )
            .order_by(WebAuthnChallenge.id.desc())
        )
        .scalars()
        .first()
    )
    if record is None:
        raise WebAuthnError("No pending challenge; start the ceremony again")
    if record.expires_at < datetime.now(UTC).replace(tzinfo=None):
        record.consumed = True
        db.commit()
        raise WebAuthnError("Challenge expired; please try again")
    record.consumed = True
    db.commit()
    return record.challenge


def credentials_for(db: Session, employee: Employee) -> list[WebAuthnCredential]:
    return list(
        db.execute(
            select(WebAuthnCredential).where(
                WebAuthnCredential.employee_id == employee.id,
                WebAuthnCredential.is_active.is_(True),
            )
        ).scalars()
    )


def registration_options(db: Session, employee: Employee) -> dict:
    existing = credentials_for(db, employee)
    options = generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=str(employee.id).encode(),
        user_name=employee.employee_code,
        user_display_name=employee.full_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in existing
        ],
    )
    _store_challenge(db, employee, options.challenge, PURPOSE_REGISTER)
    return json.loads(options_to_json(options))


def verify_registration(
    db: Session, employee: Employee, credential: dict, device_label: str | None = None
) -> WebAuthnCredential:
    expected = _consume_challenge(db, employee, PURPOSE_REGISTER)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=expected,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            require_user_verification=True,
        )
    except Exception as exc:  # library raises several verification error types
        raise WebAuthnError(f"Registration verification failed: {exc}") from exc

    from webauthn.helpers import bytes_to_base64url

    credential_id = bytes_to_base64url(verified.credential_id)
    if db.execute(
        select(WebAuthnCredential).where(WebAuthnCredential.credential_id == credential_id)
    ).scalar_one_or_none():
        raise WebAuthnError("This device is already registered")

    record = WebAuthnCredential(
        tenant_id=employee.tenant_id,
        employee_id=employee.id,
        credential_id=credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=",".join(credential.get("transports") or []) or None,
        device_label=device_label,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def authentication_options(db: Session, employee: Employee) -> dict:
    existing = credentials_for(db, employee)
    if not existing:
        raise WebAuthnError("No biometric credential registered for this employee")
    options = generate_authentication_options(
        rp_id=settings.webauthn_rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in existing
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    _store_challenge(db, employee, options.challenge, PURPOSE_AUTHENTICATE)
    return json.loads(options_to_json(options))


def verify_authentication(db: Session, employee: Employee, credential: dict) -> WebAuthnCredential:
    expected = _consume_challenge(db, employee, PURPOSE_AUTHENTICATE)
    raw_id = credential.get("id") or credential.get("rawId")
    record = db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.credential_id == raw_id,
            WebAuthnCredential.employee_id == employee.id,
            WebAuthnCredential.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if record is None:
        raise WebAuthnError("Unknown credential for this employee")

    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=expected,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=record.public_key,
            credential_current_sign_count=record.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        raise WebAuthnError(f"Biometric verification failed: {exc}") from exc

    # A counter that fails to advance can indicate a cloned authenticator.
    if verified.new_sign_count and verified.new_sign_count <= record.sign_count:
        raise WebAuthnError("Signature counter did not advance; credential rejected")

    record.sign_count = verified.new_sign_count
    record.last_used_at = utcnow()
    db.commit()
    db.refresh(record)
    return record
