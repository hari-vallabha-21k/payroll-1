"""Attendance processing, against the worked examples in the PRD."""

from datetime import date, datetime

import pytest
from sqlalchemy import select

from backend.app.models import (
    AttendanceEvent,
    AttendanceStatus,
    Employee,
    EventSource,
    EventType,
)
from backend.app.services import attendance as attendance_service
from backend.app.services import punch as punch_service


def get_employee(db, code: str) -> Employee:
    return db.execute(select(Employee).where(Employee.employee_code == code)).scalar_one()


def add_events(db, employee, day: date, punches: list[tuple[str, str]]) -> None:
    for clock, kind in punches:
        hour, minute = (int(part) for part in clock.split(":"))
        db.add(
            AttendanceEvent(
                tenant_id=employee.tenant_id,
                employee_id=employee.id,
                event_type=EventType[kind],
                event_time=datetime(day.year, day.month, day.day, hour, minute),
                source=EventSource.MOCK_DEVICE,
            )
        )
    db.commit()


def test_split_shift_sums_paired_intervals(db):
    """PRD 14: 09:02 in, 12:30 out, 13:15 in, 18:05 out -> 8h18m worked."""
    employee = get_employee(db, "EMP001")
    day = date(2026, 9, 10)
    add_events(
        db,
        employee,
        day,
        [
            ("09:02", "CHECK_IN"),
            ("12:30", "CHECK_OUT"),
            ("13:15", "CHECK_IN"),
            ("18:05", "CHECK_OUT"),
        ],
    )

    record = attendance_service.process_day(db, employee, day)

    assert record.worked_minutes == 498  # 8h 18m
    assert record.first_in.strftime("%H:%M") == "09:02"
    assert record.last_out.strftime("%H:%M") == "18:05"
    assert record.status == AttendanceStatus.PRESENT
    assert record.payable_day_fraction == 1.0
    assert record.overtime_minutes == 5
    assert record.missing_checkout is False


def test_late_and_overtime(db):
    """PRD 13: shift 09:00-18:00, in 09:07 out 18:32 -> late 7m, overtime 32m."""
    employee = get_employee(db, "EMP003")
    day = date(2026, 9, 11)
    add_events(db, employee, day, [("09:07", "CHECK_IN"), ("18:32", "CHECK_OUT")])

    record = attendance_service.process_day(db, employee, day)

    assert record.late_minutes == 7
    assert record.overtime_minutes == 32
    assert record.worked_minutes == 565  # 9h 25m
    assert record.status == AttendanceStatus.PRESENT


def test_missing_checkout_is_flagged(db):
    employee = get_employee(db, "EMP001")
    day = date(2026, 9, 12)
    add_events(db, employee, day, [("09:00", "CHECK_IN")])

    record = attendance_service.process_day(db, employee, day)

    assert record.missing_checkout is True
    assert record.worked_minutes == 0
    assert record.status == AttendanceStatus.ABSENT
    assert "issing check-out" in record.remarks


def test_short_day_is_half_day(db):
    employee = get_employee(db, "EMP001")
    day = date(2026, 9, 13)
    add_events(db, employee, day, [("09:00", "CHECK_IN"), ("14:00", "CHECK_OUT")])

    record = attendance_service.process_day(db, employee, day)

    assert record.status == AttendanceStatus.HALF_DAY
    assert record.payable_day_fraction == 0.5
    assert record.early_leave_minutes == 240


def test_raw_events_are_never_rewritten(db):
    """Reprocessing must not add, drop or edit raw punches."""
    employee = get_employee(db, "EMP001")
    day = date(2026, 9, 10)
    before = (
        db.execute(select(AttendanceEvent).where(AttendanceEvent.employee_id == employee.id))
        .scalars()
        .all()
    )
    snapshot = [(e.id, e.event_time, e.event_type) for e in before]

    attendance_service.process_day(db, employee, day)
    attendance_service.process_day(db, employee, day)

    after = (
        db.execute(select(AttendanceEvent).where(AttendanceEvent.employee_id == employee.id))
        .scalars()
        .all()
    )
    assert [(e.id, e.event_time, e.event_type) for e in after] == snapshot


def test_duplicate_punch_within_window_is_rejected(db):
    employee = get_employee(db, "EMP002")
    now = datetime(2026, 9, 14, 14, 5)
    punch_service.record_punch(
        db, employee, event_type=EventType.CHECK_IN, source=EventSource.WEBAUTHN, event_time=now
    )
    with pytest.raises(punch_service.PunchError):
        punch_service.record_punch(
            db,
            employee,
            event_type=EventType.CHECK_IN,
            source=EventSource.WEBAUTHN,
            event_time=datetime(2026, 9, 14, 14, 5, 30),
        )


def test_next_event_type_alternates(db):
    employee = get_employee(db, "EMP003")
    at = datetime(2026, 9, 15, 9, 0)
    assert punch_service.next_event_type(db, employee, at=at) == EventType.CHECK_IN

    punch_service.record_punch(
        db, employee, event_type=None, source=EventSource.WEBAUTHN, event_time=at
    )
    assert punch_service.next_event_type(db, employee, at=at) == EventType.CHECK_OUT


def test_future_dated_events_do_not_steer_todays_punch(db):
    """A back-dated correction or a future row must not flip the kiosk action."""
    employee = get_employee(db, "EMP002")
    now = datetime(2026, 9, 20, 14, 30)
    punch_service.record_punch(
        db, employee, event_type=EventType.CHECK_IN, source=EventSource.WEBAUTHN, event_time=now
    )
    # An event dated well into the future exists for this employee...
    punch_service.record_punch(
        db,
        employee,
        event_type=EventType.CHECK_IN,
        source=EventSource.MANUAL,
        event_time=datetime(2027, 6, 1, 9, 0),
    )
    # ...but the punch that follows the 14:30 check-in is still a check-out.
    assert (
        punch_service.next_event_type(db, employee, at=datetime(2026, 9, 20, 22, 0))
        == EventType.CHECK_OUT
    )
