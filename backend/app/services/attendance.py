"""Attendance processing.

Raw ``AttendanceEvent`` rows are never modified. This module derives the
``DailyAttendance`` summary from them, so payroll can always be traced back to
the punches that produced it.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AttendanceEvent,
    AttendanceStatus,
    DailyAttendance,
    Employee,
    EventType,
    Holiday,
    LeaveRequest,
    LeaveStatus,
    Shift,
    TenantSetting,
    utcnow,
)

DEFAULT_FULL_DAY_MINUTES = 480


def _minutes(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60)


def _combine(day: date, t: time) -> datetime:
    return datetime.combine(day, t)


def shift_window(day: date, shift: Shift) -> tuple[datetime, datetime]:
    """Shift start/end as datetimes, handling shifts that cross midnight."""
    start = _combine(day, shift.start_time)
    end = _combine(day, shift.end_time)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def effective_shift(db: Session, employee: Employee, day: date) -> Shift | None:
    """Shift in force for an employee on a date (assignment history wins)."""
    from ..models import EmployeeShift

    stmt = (
        select(Shift)
        .join(EmployeeShift, EmployeeShift.shift_id == Shift.id)
        .where(
            EmployeeShift.employee_id == employee.id,
            EmployeeShift.effective_from <= day,
            (EmployeeShift.effective_to.is_(None)) | (EmployeeShift.effective_to >= day),
        )
        .order_by(EmployeeShift.effective_from.desc())
    )
    assigned = db.execute(stmt).scalars().first()
    if assigned:
        return assigned
    return employee.shift


def weekly_off_days(db: Session, tenant_id: int) -> set[int]:
    """Weekday numbers (Mon=0) configured as weekly offs for the tenant."""
    setting = db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == tenant_id, TenantSetting.key == "weekly_off_days"
        )
    ).scalar_one_or_none()
    if not setting or not setting.value.strip():
        return set()
    days = set()
    for part in setting.value.split(","):
        part = part.strip()
        if part.isdigit():
            days.add(int(part))
    return days


def pair_events(events: list[AttendanceEvent]) -> tuple[list[tuple[datetime, datetime]], bool]:
    """Pair CHECK_IN events with the next CHECK_OUT.

    Returns the paired intervals and whether an unmatched check-in remains
    (a missing check-out).
    """
    intervals: list[tuple[datetime, datetime]] = []
    open_in: datetime | None = None
    for event in sorted(events, key=lambda e: e.event_time):
        if event.event_type == EventType.CHECK_IN:
            if open_in is None:
                open_in = event.event_time
        elif open_in is not None:
            intervals.append((open_in, event.event_time))
            open_in = None
    return intervals, open_in is not None


def approved_leave_on(db: Session, employee_id: int, day: date) -> LeaveRequest | None:
    stmt = select(LeaveRequest).where(
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.status == LeaveStatus.APPROVED,
        LeaveRequest.start_date <= day,
        LeaveRequest.end_date >= day,
    )
    return db.execute(stmt).scalars().first()


def is_holiday(db: Session, tenant_id: int, day: date) -> Holiday | None:
    stmt = select(Holiday).where(Holiday.tenant_id == tenant_id, Holiday.holiday_date == day)
    return db.execute(stmt).scalars().first()


def events_for_day(db: Session, employee: Employee, day: date, shift: Shift | None) -> list:
    """Events belonging to a work date.

    For an overnight shift the window extends past midnight, so events are
    collected over the shift window rather than the calendar day.
    """
    if shift is not None:
        start, end = shift_window(day, shift)
        window_start = start - timedelta(hours=4)
        window_end = end + timedelta(hours=6)
    else:
        window_start = _combine(day, time.min)
        window_end = window_start + timedelta(days=1)

    stmt = (
        select(AttendanceEvent)
        .where(
            AttendanceEvent.employee_id == employee.id,
            AttendanceEvent.event_time >= window_start,
            AttendanceEvent.event_time < window_end,
        )
        .order_by(AttendanceEvent.event_time)
    )
    return list(db.execute(stmt).scalars())


def process_day(
    db: Session, employee: Employee, day: date, *, commit: bool = True
) -> DailyAttendance:
    """(Re)build the processed attendance row for one employee-day."""
    shift = effective_shift(db, employee, day)
    events = events_for_day(db, employee, day, shift)
    intervals, missing_checkout = pair_events(events)

    worked = sum(_minutes(end - start) for start, end in intervals)
    first_in = min(
        (e.event_time for e in events if e.event_type == EventType.CHECK_IN), default=None
    )
    last_out = max(
        (e.event_time for e in events if e.event_type == EventType.CHECK_OUT), default=None
    )

    late = early = overtime = 0
    if shift is not None:
        start_dt, end_dt = shift_window(day, shift)
        if first_in is not None:
            late = max(0, _minutes(first_in - start_dt))
        if last_out is not None:
            early = max(0, _minutes(end_dt - last_out))
            after_shift = max(0, _minutes(last_out - end_dt))
            overtime = max(0, after_shift - shift.overtime_after_minutes)

    full_day_minutes = int((shift.full_day_hours if shift else 8.0) * 60)
    half_day_minutes = int((shift.half_day_hours if shift else 4.0) * 60)

    holiday = is_holiday(db, employee.tenant_id, day)
    leave = approved_leave_on(db, employee.id, day)
    offs = weekly_off_days(db, employee.tenant_id)

    remarks = None
    if worked >= full_day_minutes:
        status, fraction = AttendanceStatus.PRESENT, 1.0
    elif worked >= half_day_minutes:
        status, fraction = AttendanceStatus.HALF_DAY, 0.5
    elif holiday is not None:
        status, fraction, remarks = AttendanceStatus.HOLIDAY, 1.0, holiday.name
    elif day.weekday() in offs:
        status, fraction = AttendanceStatus.WEEKLY_OFF, 1.0
    elif leave is not None:
        status = AttendanceStatus.ON_LEAVE
        fraction = 1.0 if leave.leave_type.is_paid else 0.0
        remarks = leave.leave_type.name
    elif worked > 0:
        status, fraction = AttendanceStatus.HALF_DAY, 0.5
        remarks = "Short hours"
    else:
        status, fraction = AttendanceStatus.ABSENT, 0.0

    if missing_checkout:
        remarks = "Missing check-out" if remarks is None else f"{remarks}; missing check-out"

    record = db.execute(
        select(DailyAttendance).where(
            DailyAttendance.tenant_id == employee.tenant_id,
            DailyAttendance.employee_id == employee.id,
            DailyAttendance.work_date == day,
        )
    ).scalar_one_or_none()

    if record is None:
        record = DailyAttendance(
            tenant_id=employee.tenant_id, employee_id=employee.id, work_date=day
        )
        db.add(record)
    elif record.is_manual_override:
        # An HR correction wins until it is explicitly cleared.
        return record

    record.shift_id = shift.id if shift else None
    record.first_in = first_in
    record.last_out = last_out
    record.worked_minutes = worked
    record.late_minutes = late
    record.early_leave_minutes = early
    record.overtime_minutes = overtime
    record.status = status
    record.payable_day_fraction = fraction
    record.missing_checkout = missing_checkout
    record.remarks = remarks
    record.processed_at = utcnow()

    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    return record


def process_range(db: Session, employee: Employee, start: date, end: date) -> list[DailyAttendance]:
    records = []
    day = start
    while day <= end:
        records.append(process_day(db, employee, day, commit=False))
        day += timedelta(days=1)
    db.commit()
    return records


def process_tenant_day(db: Session, tenant_id: int, day: date) -> int:
    from ..models import EmployeeStatus

    employees = db.execute(
        select(Employee).where(
            Employee.tenant_id == tenant_id, Employee.status == EmployeeStatus.ACTIVE
        )
    ).scalars()
    count = 0
    for employee in employees:
        process_day(db, employee, day, commit=False)
        count += 1
    db.commit()
    return count
