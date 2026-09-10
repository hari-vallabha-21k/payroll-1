"""Recording a standardized attendance event, whatever produced it."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    AttendanceEvent,
    DailyAttendance,
    Employee,
    EventSource,
    EventType,
    utcnow,
)
from . import attendance as attendance_service

settings = get_settings()


class PunchError(Exception):
    pass


def last_event(
    db: Session, employee: Employee, *, at: datetime | None = None
) -> AttendanceEvent | None:
    """The employee's most recent punch as of ``at`` (default: now).

    Back-dated corrections mean the newest row is not always the latest punch,
    so the lookup is always anchored to a point in time.
    """
    reference = at or utcnow().replace(tzinfo=None)
    return (
        db.execute(
            select(AttendanceEvent)
            .where(
                AttendanceEvent.employee_id == employee.id, AttendanceEvent.event_time <= reference
            )
            .order_by(AttendanceEvent.event_time.desc(), AttendanceEvent.id.desc())
        )
        .scalars()
        .first()
    )


def next_event_type(db: Session, employee: Employee, *, at: datetime | None = None) -> EventType:
    """A kiosk shows the action the employee is due for."""
    previous = last_event(db, employee, at=at)
    if previous is None or previous.event_type == EventType.CHECK_OUT:
        return EventType.CHECK_IN
    return EventType.CHECK_OUT


def work_date_for(db: Session, employee: Employee, event_time: datetime) -> date:
    """Map a punch to a work date, accounting for overnight shifts."""
    shift = attendance_service.effective_shift(db, employee, event_time.date())
    if shift is None or shift.end_time > shift.start_time:
        return event_time.date()
    # Overnight shift: a punch before the start time belongs to the previous day.
    if event_time.time() < shift.start_time:
        return event_time.date() - timedelta(days=1)
    return event_time.date()


def record_punch(
    db: Session,
    employee: Employee,
    *,
    event_type: EventType | None,
    source: EventSource,
    event_time: datetime | None = None,
    device_id: int | None = None,
    credential_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    external_ref: str | None = None,
    note: str | None = None,
    reprocess: bool = True,
) -> tuple[AttendanceEvent, DailyAttendance | None]:
    """Append a raw event and rebuild the affected day.

    ``event_time`` defaults to the server clock - a client-supplied time is
    never trusted for WebAuthn punches.
    """
    when = event_time or utcnow().replace(tzinfo=None)
    resolved = event_type or next_event_type(db, employee, at=when)

    # A replayed device batch is a plain duplicate, not a suspicious punch, so
    # the idempotency key is checked before the same-type window.
    if external_ref:
        duplicate = db.execute(
            select(AttendanceEvent).where(
                AttendanceEvent.tenant_id == employee.tenant_id,
                AttendanceEvent.external_ref == external_ref,
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise PunchError("duplicate")

    previous = last_event(db, employee, at=when)
    if previous is not None and previous.event_type == resolved:
        gap = abs((when - previous.event_time).total_seconds())
        if gap < settings.duplicate_punch_window_seconds:
            raise PunchError(
                f"Already recorded a {resolved.value.replace('_', ' ').lower()} "
                f"at {previous.event_time.strftime('%H:%M:%S')}"
            )

    event = AttendanceEvent(
        tenant_id=employee.tenant_id,
        employee_id=employee.id,
        event_type=resolved,
        event_time=when,
        source=source,
        device_id=device_id,
        credential_id=credential_id,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255] or None,
        external_ref=external_ref,
        note=note,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    daily = None
    if reprocess:
        daily = attendance_service.process_day(db, employee, work_date_for(db, employee, when))
    return event, daily
