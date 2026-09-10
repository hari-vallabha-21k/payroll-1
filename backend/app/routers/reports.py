import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user, require_manager, require_payroll
from ..models import (
    AttendanceStatus,
    AuditLog,
    DailyAttendance,
    Employee,
    EmployeeStatus,
    LeaveRequest,
    LeaveStatus,
    PayrollRun,
    Role,
    User,
)
from ..schemas import DashboardOut, PayrollRunOut

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(
    work_date: date | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    day = work_date or date.today()
    tenant_id = user.tenant_id

    counts = dict(
        db.execute(
            select(DailyAttendance.status, func.count())
            .where(DailyAttendance.tenant_id == tenant_id, DailyAttendance.work_date == day)
            .group_by(DailyAttendance.status)
        ).all()
    )
    total_employees = db.execute(
        select(func.count())
        .select_from(Employee)
        .where(Employee.tenant_id == tenant_id, Employee.status == EmployeeStatus.ACTIVE)
    ).scalar_one()

    late = db.execute(
        select(func.count())
        .select_from(DailyAttendance)
        .where(
            DailyAttendance.tenant_id == tenant_id,
            DailyAttendance.work_date == day,
            DailyAttendance.late_minutes > 0,
        )
    ).scalar_one()
    missing = db.execute(
        select(func.count())
        .select_from(DailyAttendance)
        .where(
            DailyAttendance.tenant_id == tenant_id,
            DailyAttendance.work_date == day,
            DailyAttendance.missing_checkout.is_(True),
        )
    ).scalar_one()
    pending_leave = db.execute(
        select(func.count())
        .select_from(LeaveRequest)
        .where(LeaveRequest.tenant_id == tenant_id, LeaveRequest.status == LeaveStatus.PENDING)
    ).scalar_one()

    present = counts.get(AttendanceStatus.PRESENT, 0) + counts.get(AttendanceStatus.HALF_DAY, 0)
    on_leave = counts.get(AttendanceStatus.ON_LEAVE, 0)
    absent = counts.get(AttendanceStatus.ABSENT, 0)
    # Employees with no processed row yet are not counted as present.
    unprocessed = max(0, total_employees - sum(counts.values()))
    absent += unprocessed

    latest_run = (
        db.execute(
            select(PayrollRun)
            .where(PayrollRun.tenant_id == tenant_id)
            .order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc())
        )
        .scalars()
        .first()
    )

    alerts = []
    if late:
        alerts.append(f"{late} employee(s) late today")
    if missing:
        alerts.append(f"{missing} missing check-out(s)")
    if pending_leave:
        alerts.append(f"{pending_leave} leave request(s) awaiting approval")

    return DashboardOut(
        work_date=day,
        present=present,
        absent=absent,
        late=late,
        on_leave=on_leave,
        missing_checkout=missing,
        total_employees=total_employees,
        pending_leave_requests=pending_leave,
        latest_payroll=PayrollRunOut.model_validate(latest_run) if latest_run else None,
        alerts=alerts,
    )


@router.get("/reports/attendance-summary")
def attendance_summary(
    start: date | None = None,
    end: date | None = None,
    user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    end = end or date.today()
    start = start or end - timedelta(days=30)
    rows = db.execute(
        select(
            DailyAttendance.employee_id,
            Employee.employee_code,
            Employee.first_name,
            Employee.last_name,
            func.sum(DailyAttendance.payable_day_fraction),
            func.sum(DailyAttendance.worked_minutes),
            func.sum(DailyAttendance.overtime_minutes),
            func.sum(DailyAttendance.late_minutes),
        )
        .join(Employee, Employee.id == DailyAttendance.employee_id)
        .where(
            DailyAttendance.tenant_id == user.tenant_id,
            DailyAttendance.work_date >= start,
            DailyAttendance.work_date <= end,
        )
        .group_by(
            DailyAttendance.employee_id,
            Employee.employee_code,
            Employee.first_name,
            Employee.last_name,
        )
        .order_by(Employee.employee_code)
    ).all()

    if user.role == Role.MANAGER:
        team = set(
            db.execute(
                select(Employee.id).where(
                    Employee.tenant_id == user.tenant_id, Employee.manager_id == user.employee_id
                )
            ).scalars()
        )
        rows = [row for row in rows if row[0] in team]

    return {
        "start": start,
        "end": end,
        "rows": [
            {
                "employee_id": row[0],
                "employee_code": row[1],
                "employee_name": f"{row[2]} {row[3]}".strip(),
                "payable_days": round(float(row[4] or 0), 2),
                "worked_minutes": int(row[5] or 0),
                "overtime_minutes": int(row[6] or 0),
                "late_minutes": int(row[7] or 0),
            }
            for row in rows
        ],
    }


@router.get("/reports/attendance.csv")
def attendance_csv(
    start: date,
    end: date,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(DailyAttendance, Employee)
        .join(Employee, Employee.id == DailyAttendance.employee_id)
        .where(
            DailyAttendance.tenant_id == user.tenant_id,
            DailyAttendance.work_date >= start,
            DailyAttendance.work_date <= end,
        )
        .order_by(DailyAttendance.work_date, Employee.employee_code)
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "date",
            "employee_code",
            "employee_name",
            "first_in",
            "last_out",
            "worked_minutes",
            "late_minutes",
            "overtime_minutes",
            "status",
            "payable_fraction",
            "remarks",
        ]
    )
    for record, employee in rows:
        writer.writerow(
            [
                record.work_date,
                employee.employee_code,
                employee.full_name,
                record.first_in or "",
                record.last_out or "",
                record.worked_minutes,
                record.late_minutes,
                record.overtime_minutes,
                record.status.value,
                record.payable_day_fraction,
                record.remarks or "",
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="attendance-{start}-{end}.csv"'},
    )


@router.get("/audit-logs")
def audit_logs(
    limit: int = 100,
    action: str | None = None,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    stmt = select(AuditLog).where(AuditLog.tenant_id == user.tenant_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt.order_by(AuditLog.id.desc()).limit(min(limit, 500))).scalars())
