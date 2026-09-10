"""Payroll engine, using the PRD's example salary structure."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.app.models import (
    AttendanceEvent,
    Employee,
    EventSource,
    EventType,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    PayrollRun,
    PayrollStatus,
)
from backend.app.services import attendance as attendance_service
from backend.app.services import payroll as payroll_service


def get_employee(db, code: str) -> Employee:
    return db.execute(select(Employee).where(Employee.employee_code == code)).scalar_one()


def full_month_attendance(
    db, employee, year: int, month: int, *, skip_days: set[int] = frozenset()
):
    """Give an employee a clean 09:00-18:00 record for the whole month."""
    start, end = payroll_service.month_bounds(year, month)
    for day_number in range(1, end.day + 1):
        if day_number in skip_days:
            continue
        for hour, kind in ((9, EventType.CHECK_IN), (18, EventType.CHECK_OUT)):
            db.add(
                AttendanceEvent(
                    tenant_id=employee.tenant_id,
                    employee_id=employee.id,
                    event_type=kind,
                    event_time=datetime(year, month, day_number, hour, 0),
                    source=EventSource.MOCK_DEVICE,
                )
            )
    db.commit()
    attendance_service.process_range(db, employee, start, end)


def test_full_attendance_pays_full_gross(db):
    employee = get_employee(db, "EMP001")
    full_month_attendance(db, employee, 2026, 11)

    result = payroll_service.calculate_employee(db, employee, 2026, 11)

    # Basic 20000 + HRA 5000 + Transport 2000 + Other 3000
    assert result.gross == Decimal("30000.00")
    assert result.lop_days == 0.0
    # PF is 10% of basic, professional tax is flat 200
    assert result.deduction_total == Decimal("2200.00")
    assert result.net == Decimal("27800.00")
    assert not any(line.label == "Loss of Pay" for line in result.deductions)


def test_absence_creates_loss_of_pay(db):
    employee = get_employee(db, "EMP003")
    full_month_attendance(db, employee, 2026, 11, skip_days={5, 6})

    result = payroll_service.calculate_employee(db, employee, 2026, 11)

    assert result.lop_days == 2.0
    lop = next(line for line in result.deductions if line.label == "Loss of Pay")
    # 30000 gross over 30 days -> 1000/day
    assert lop.amount == Decimal("2000.00")
    assert result.net == Decimal("25800.00")


def test_overtime_is_paid_at_the_configured_rate(db):
    """PRD 17: 2 hours of overtime at Rs 200/hour = Rs 400."""
    employee = get_employee(db, "EMP001")
    year, month = 2026, 10
    for day_number in (1, 2):
        for hour, kind in ((9, EventType.CHECK_IN), (19, EventType.CHECK_OUT)):
            db.add(
                AttendanceEvent(
                    tenant_id=employee.tenant_id,
                    employee_id=employee.id,
                    event_type=kind,
                    event_time=datetime(year, month, day_number, hour, 0),
                    source=EventSource.MOCK_DEVICE,
                )
            )
    db.commit()
    attendance_service.process_range(db, employee, date(year, month, 1), date(year, month, 2))

    result = payroll_service.calculate_employee(db, employee, year, month)

    assert result.overtime_minutes == 120
    assert result.overtime_amount == Decimal("400.00")
    assert any(
        line.label == "Overtime" and line.amount == Decimal("400.00") for line in result.earnings
    )


def test_paid_leave_does_not_cost_the_employee(db):
    employee = get_employee(db, "EMP001")
    casual = db.execute(
        select(LeaveType).where(
            LeaveType.tenant_id == employee.tenant_id, LeaveType.name == "Casual Leave"
        )
    ).scalar_one()
    db.add(
        LeaveRequest(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            leave_type_id=casual.id,
            start_date=date(2026, 12, 10),
            end_date=date(2026, 12, 11),
            status=LeaveStatus.APPROVED,
        )
    )
    db.commit()
    full_month_attendance(db, employee, 2026, 12, skip_days={10, 11})

    result = payroll_service.calculate_employee(db, employee, 2026, 12)

    assert result.paid_leave_days == 2.0
    assert result.lop_days == 0.0
    assert result.net == Decimal("27800.00")


def test_unpaid_leave_is_a_loss_of_pay(db):
    employee = get_employee(db, "EMP003")
    unpaid = db.execute(
        select(LeaveType).where(
            LeaveType.tenant_id == employee.tenant_id, LeaveType.name == "Unpaid Leave"
        )
    ).scalar_one()
    db.add(
        LeaveRequest(
            tenant_id=employee.tenant_id,
            employee_id=employee.id,
            leave_type_id=unpaid.id,
            start_date=date(2027, 1, 5),
            end_date=date(2027, 1, 5),
            status=LeaveStatus.APPROVED,
        )
    )
    db.commit()
    full_month_attendance(db, employee, 2027, 1, skip_days={5})

    result = payroll_service.calculate_employee(db, employee, 2027, 1)

    assert result.paid_leave_days == 0.0
    assert result.lop_days == 1.0


def test_approved_payroll_cannot_be_recalculated(db):
    run = PayrollRun(
        tenant_id=get_employee(db, "EMP001").tenant_id,
        period_year=2027,
        period_month=2,
        status=PayrollStatus.APPROVED,
    )
    db.add(run)
    db.commit()

    with pytest.raises(ValueError):
        payroll_service.calculate_run(db, run)
