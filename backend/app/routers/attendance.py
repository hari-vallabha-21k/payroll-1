from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import assert_can_view_employee, client_ip, get_current_user, require_payroll
from ..models import (
    AttendanceEvent,
    DailyAttendance,
    Employee,
    EventSource,
    Role,
    User,
)
from ..schemas import (
    AttendanceCorrection,
    AttendanceEventOut,
    DailyAttendanceOut,
    ManualPunch,
    PunchResult,
)
from ..services import attendance as attendance_service
from ..services import punch as punch_service
from .employees import get_employee_or_404
from .webauthn import resolve_employee

router = APIRouter(prefix="/api/attendance", tags=["attendance"])


def _visible_employee_ids(db: Session, user: User) -> list[int] | None:
    """None means 'all employees in the tenant'."""
    if user.role in (Role.ADMIN, Role.HR):
        return None
    if user.role == Role.MANAGER:
        team = db.execute(
            select(Employee.id).where(
                Employee.tenant_id == user.tenant_id,
                (Employee.manager_id == user.employee_id) | (Employee.id == user.employee_id),
            )
        ).scalars()
        return list(team)
    return [user.employee_id] if user.employee_id else []


@router.get("", response_model=list[DailyAttendanceOut])
def list_daily_attendance(
    start: date | None = None,
    end: date | None = None,
    employee_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    start = start or date.today()
    end = end or start
    stmt = select(DailyAttendance).where(
        DailyAttendance.tenant_id == user.tenant_id,
        DailyAttendance.work_date >= start,
        DailyAttendance.work_date <= end,
    )
    allowed = _visible_employee_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(DailyAttendance.employee_id.in_(allowed or [-1]))
    if employee_id:
        stmt = stmt.where(DailyAttendance.employee_id == employee_id)
    return list(
        db.execute(stmt.order_by(DailyAttendance.work_date, DailyAttendance.employee_id)).scalars()
    )


@router.get("/events", response_model=list[AttendanceEventOut])
def list_events(
    start: date | None = None,
    end: date | None = None,
    employee_id: int | None = None,
    limit: int = Query(200, le=1000),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(AttendanceEvent).where(AttendanceEvent.tenant_id == user.tenant_id)
    if start:
        stmt = stmt.where(AttendanceEvent.event_time >= start)
    if end:
        stmt = stmt.where(AttendanceEvent.event_time < end + timedelta(days=1))
    allowed = _visible_employee_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(AttendanceEvent.employee_id.in_(allowed or [-1]))
    if employee_id:
        stmt = stmt.where(AttendanceEvent.employee_id == employee_id)
    return list(db.execute(stmt.order_by(AttendanceEvent.event_time.desc()).limit(limit)).scalars())


@router.get("/{employee_id}", response_model=list[DailyAttendanceOut])
def employee_attendance(
    employee_id: int,
    start: date | None = None,
    end: date | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    assert_can_view_employee(db, user, employee)
    end = end or date.today()
    start = start or end.replace(day=1)
    return list(
        db.execute(
            select(DailyAttendance)
            .where(
                DailyAttendance.employee_id == employee.id,
                DailyAttendance.work_date >= start,
                DailyAttendance.work_date <= end,
            )
            .order_by(DailyAttendance.work_date)
        ).scalars()
    )


@router.post("/punch", response_model=PunchResult, status_code=status.HTTP_201_CREATED)
def manual_punch(
    payload: ManualPunch,
    request: Request,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """HR-entered punch, e.g. a forgotten check-out. Always audited."""
    employee = resolve_employee(db, payload.employee_code, None)
    if employee.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    try:
        event, daily = punch_service.record_punch(
            db,
            employee,
            event_type=payload.event_type,
            source=EventSource.MANUAL,
            event_time=payload.event_time,
            ip_address=client_ip(request),
            note=payload.note,
        )
    except punch_service.PunchError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="MANUAL_PUNCH",
        entity_type="attendance_event",
        entity_id=event.id,
        detail=payload.model_dump(mode="json"),
        ip_address=client_ip(request),
    )
    db.commit()
    return PunchResult(
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        event_type=event.event_type,
        event_time=event.event_time,
        message="Manual punch recorded",
        daily=DailyAttendanceOut.model_validate(daily) if daily else None,
    )


@router.post("/{employee_id}/correction", response_model=DailyAttendanceOut)
def correct_attendance(
    employee_id: int,
    payload: AttendanceCorrection,
    request: Request,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Override a processed day without touching the raw events."""
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    record = attendance_service.process_day(db, employee, payload.work_date)

    before = DailyAttendanceOut.model_validate(record).model_dump(mode="json")
    for field in ("first_in", "last_out", "status", "payable_day_fraction", "overtime_minutes"):
        value = getattr(payload, field)
        if value is not None:
            setattr(record, field, value)
    if record.first_in and record.last_out:
        record.worked_minutes = int((record.last_out - record.first_in).total_seconds() // 60)
        record.missing_checkout = False
    record.is_manual_override = True
    record.remarks = payload.reason

    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="CORRECT_ATTENDANCE",
        entity_type="daily_attendance",
        entity_id=record.id,
        detail={"before": before, "change": payload.model_dump(mode="json")},
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(record)
    return record


@router.delete("/{employee_id}/correction/{work_date}", response_model=DailyAttendanceOut)
def clear_correction(
    employee_id: int,
    work_date: date,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    record = db.execute(
        select(DailyAttendance).where(
            DailyAttendance.employee_id == employee.id, DailyAttendance.work_date == work_date
        )
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No processed attendance for that date")
    record.is_manual_override = False
    db.commit()
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="CLEAR_ATTENDANCE_CORRECTION",
        entity_type="daily_attendance",
        entity_id=record.id,
    )
    db.commit()
    return attendance_service.process_day(db, employee, work_date)


@router.post("/process")
def reprocess(
    work_date: date,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Rebuild every employee's processed attendance for a date."""
    count = attendance_service.process_tenant_day(db, user.tenant_id, work_date)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="REPROCESS_ATTENDANCE",
        entity_type="daily_attendance",
        detail={"work_date": str(work_date), "employees": count},
    )
    db.commit()
    return {"work_date": work_date, "employees_processed": count}
