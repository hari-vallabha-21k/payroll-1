"""Biometric device registry and the connector ingestion endpoint.

A device connector (running on the restaurant's LAN) pulls punches off the
machine and POSTs them here. The payroll engine cannot tell the difference
between these events and WebAuthn ones - both become standardized
``AttendanceEvent`` rows.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import get_current_user, require_payroll
from ..models import (
    BiometricDevice,
    BiometricUser,
    DeviceStatus,
    Employee,
    EventSource,
    SyncLog,
    User,
    utcnow,
)
from ..schemas import (
    DeviceCreate,
    DeviceEnrollment,
    DeviceOut,
    DeviceSyncRequest,
    DeviceSyncResult,
)
from ..security import hash_password, verify_password
from ..services import punch as punch_service
from .employees import get_employee_or_404

router = APIRouter(prefix="/api/devices", tags=["devices"])


def get_device_or_404(db: Session, tenant_id: int, device_id: int) -> BiometricDevice:
    device = db.execute(
        select(BiometricDevice).where(
            BiometricDevice.id == device_id, BiometricDevice.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")
    return device


@router.get("", response_model=list[DeviceOut])
def list_devices(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(BiometricDevice)
            .where(BiometricDevice.tenant_id == user.tenant_id)
            .order_by(BiometricDevice.device_code)
        ).scalars()
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_device(
    payload: DeviceCreate, user: User = Depends(require_payroll), db: Session = Depends(get_db)
):
    """Registers a device and returns its API key once - it is stored hashed."""
    exists = db.execute(
        select(BiometricDevice).where(
            BiometricDevice.tenant_id == user.tenant_id,
            BiometricDevice.device_code == payload.device_code,
        )
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Device code already in use")

    api_key = secrets.token_urlsafe(32)
    device = BiometricDevice(
        tenant_id=user.tenant_id, api_key_hash=hash_password(api_key), **payload.model_dump()
    )
    db.add(device)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="REGISTER_DEVICE",
        entity_type="biometric_device",
        detail=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(device)
    return {
        "device": DeviceOut.model_validate(device),
        "api_key": api_key,
        "note": "Store this key in the connector now - it is not shown again.",
    }


@router.put("/{device_id}", response_model=DeviceOut)
def update_device(
    device_id: int,
    payload: DeviceCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    device = get_device_or_404(db, user.tenant_id, device_id)
    for field, value in payload.model_dump().items():
        setattr(device, field, value)
    db.commit()
    db.refresh(device)
    return device


@router.get("/{device_id}/status")
def device_status(
    device_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    device = get_device_or_404(db, user.tenant_id, device_id)
    recent = list(
        db.execute(
            select(SyncLog)
            .where(SyncLog.device_id == device.id)
            .order_by(SyncLog.id.desc())
            .limit(10)
        ).scalars()
    )
    return {
        "device": DeviceOut.model_validate(device),
        "recent_syncs": [
            {
                "started_at": log.started_at,
                "finished_at": log.finished_at,
                "received": log.received,
                "accepted": log.accepted,
                "duplicates": log.duplicates,
                "failed": log.failed,
                "status": log.status,
                "message": log.message,
            }
            for log in recent
        ],
    }


@router.post("/{device_id}/enrollments", status_code=status.HTTP_201_CREATED)
def map_device_user(
    device_id: int,
    payload: DeviceEnrollment,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Map the device's local user id to an employee."""
    device = get_device_or_404(db, user.tenant_id, device_id)
    employee = get_employee_or_404(db, user.tenant_id, payload.employee_id)
    existing = db.execute(
        select(BiometricUser).where(
            BiometricUser.device_id == device.id,
            BiometricUser.device_user_id == payload.device_user_id,
        )
    ).scalar_one_or_none()
    if existing:
        existing.employee_id = employee.id
    else:
        db.add(
            BiometricUser(
                tenant_id=user.tenant_id,
                device_id=device.id,
                employee_id=employee.id,
                device_user_id=payload.device_user_id,
            )
        )
    db.commit()
    return {"detail": f"Device user {payload.device_user_id} -> {employee.employee_code}"}


@router.post("/{device_id}/sync", response_model=DeviceSyncResult)
def sync_events(
    device_id: int,
    payload: DeviceSyncRequest,
    x_device_key: str | None = Header(None, alias="X-Device-Key"),
    db: Session = Depends(get_db),
):
    """Connector endpoint: ingest a batch of punches.

    Authenticated with the device's own API key, not a user session, so a
    connector never needs staff credentials. Events are idempotent on
    ``external_ref`` so a retry after a network failure cannot double-punch.
    """
    device = db.get(BiometricDevice, device_id)
    if device is None or not device.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")
    if (
        not x_device_key
        or not device.api_key_hash
        or not verify_password(x_device_key, device.api_key_hash)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid device key")

    log = SyncLog(tenant_id=device.tenant_id, device_id=device.id, received=len(payload.events))
    db.add(log)
    db.commit()

    accepted = duplicates = failed = 0
    errors: list[str] = []

    for item in payload.events:
        employee: Employee | None = None
        if item.device_user_id:
            mapping = db.execute(
                select(BiometricUser).where(
                    BiometricUser.device_id == device.id,
                    BiometricUser.device_user_id == item.device_user_id,
                )
            ).scalar_one_or_none()
            if mapping:
                employee = db.get(Employee, mapping.employee_id)
        if employee is None and item.employee_code:
            employee = db.execute(
                select(Employee).where(
                    Employee.tenant_id == device.tenant_id,
                    Employee.employee_code == item.employee_code,
                )
            ).scalar_one_or_none()

        if employee is None:
            failed += 1
            errors.append(f"Unmapped user {item.device_user_id or item.employee_code}")
            continue

        try:
            punch_service.record_punch(
                db,
                employee,
                event_type=item.event_type,
                source=EventSource.BIOMETRIC_DEVICE,
                event_time=item.event_time,
                device_id=device.id,
                external_ref=item.external_ref,
            )
            accepted += 1
        except punch_service.PunchError as exc:
            if str(exc) == "duplicate":
                duplicates += 1
            else:
                duplicates += 1
                errors.append(f"{employee.employee_code}: {exc}")
        except Exception as exc:  # keep the batch going; the connector will retry
            db.rollback()
            failed += 1
            errors.append(f"{employee.employee_code}: {exc}")

    log.accepted = accepted
    log.duplicates = duplicates
    log.failed = failed
    log.finished_at = utcnow()
    log.status = "SUCCESS" if failed == 0 else "PARTIAL"
    log.message = "; ".join(errors[:10]) or None

    device.status = DeviceStatus.CONNECTED if failed == 0 else DeviceStatus.ERROR
    device.last_sync_at = utcnow()
    db.commit()

    return DeviceSyncResult(
        received=len(payload.events),
        accepted=accepted,
        duplicates=duplicates,
        failed=failed,
        errors=errors[:10],
    )
