"""Payroll engine.

Attendance + leave + overtime + salary structure -> gross, deductions, net.

The engine consumes standardized processed attendance only; it has no idea
whether a punch came from WebAuthn, a biometric machine or a mock device.
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    AttendanceStatus,
    ComponentType,
    DailyAttendance,
    Employee,
    EmployeeSalary,
    EmployeeStatus,
    PayrollAdjustment,
    PayrollItem,
    PayrollRun,
    PayrollStatus,
    SalaryComponent,
    utcnow,
)

TWOPLACES = Decimal("0.01")


def money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


@dataclass
class Line:
    label: str
    amount: Decimal


@dataclass
class PayrollResult:
    employee_id: int
    employee_code: str
    employee_name: str
    total_days: int
    working_days: float
    payable_days: float
    lop_days: float
    paid_leave_days: float
    overtime_minutes: int
    overtime_amount: Decimal
    earnings: list[Line] = field(default_factory=list)
    deductions: list[Line] = field(default_factory=list)
    gross: Decimal = Decimal("0.00")
    deduction_total: Decimal = Decimal("0.00")
    net: Decimal = Decimal("0.00")

    def to_dict(self) -> dict:
        return {
            "employee_id": self.employee_id,
            "employee_code": self.employee_code,
            "employee_name": self.employee_name,
            "total_days": self.total_days,
            "working_days": self.working_days,
            "payable_days": self.payable_days,
            "lop_days": self.lop_days,
            "paid_leave_days": self.paid_leave_days,
            "overtime_minutes": self.overtime_minutes,
            "overtime_amount": str(self.overtime_amount),
            "earnings": [{"label": e.label, "amount": str(e.amount)} for e in self.earnings],
            "deductions": [{"label": d.label, "amount": str(d.amount)} for d in self.deductions],
            "gross": str(self.gross),
            "deduction_total": str(self.deduction_total),
            "net": str(self.net),
        }


def month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def active_salary(db: Session, employee: Employee, on_date: date) -> EmployeeSalary | None:
    stmt = (
        select(EmployeeSalary)
        .where(
            EmployeeSalary.employee_id == employee.id,
            EmployeeSalary.effective_from <= on_date,
            (EmployeeSalary.effective_to.is_(None)) | (EmployeeSalary.effective_to >= on_date),
        )
        .order_by(EmployeeSalary.effective_from.desc())
    )
    return db.execute(stmt).scalars().first()


def _component_amount(component: SalaryComponent, basic: Decimal) -> Decimal:
    if component.percent_of_basic:
        return money(basic * Decimal(str(component.percent_of_basic)) / Decimal("100"))
    return money(component.amount or 0)


def calculate_employee(
    db: Session, employee: Employee, year: int, month: int, *, run_id: int | None = None
) -> PayrollResult:
    start, end = month_bounds(year, month)
    total_days = (end - start).days + 1

    # An employee who joined mid-month is only liable for days from joining.
    period_start = max(start, employee.date_of_joining)
    eligible_days = (end - period_start).days + 1

    records = list(
        db.execute(
            select(DailyAttendance).where(
                DailyAttendance.employee_id == employee.id,
                DailyAttendance.work_date >= period_start,
                DailyAttendance.work_date <= end,
            )
        ).scalars()
    )
    by_date = {r.work_date: r for r in records}

    payable = 0.0
    present_days = 0.0
    paid_leave_days = 0.0
    overtime_minutes = 0
    for offset in range(eligible_days):
        day = date.fromordinal(period_start.toordinal() + offset)
        record = by_date.get(day)
        if record is None:
            continue  # unprocessed day counts as loss of pay
        payable += record.payable_day_fraction
        overtime_minutes += record.overtime_minutes
        if record.status in (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY):
            present_days += record.payable_day_fraction
        elif record.status == AttendanceStatus.ON_LEAVE and record.payable_day_fraction > 0:
            paid_leave_days += record.payable_day_fraction

    # Days before joining are not the employee's loss.
    payable += total_days - eligible_days
    lop_days = round(total_days - payable, 2)

    salary = active_salary(db, employee, end)
    earnings: list[Line] = []
    deductions: list[Line] = []
    lop_base = Decimal("0.00")
    gross = Decimal("0.00")
    ot_rate = Decimal("0.00")

    if salary is not None:
        components = sorted(salary.structure.components, key=lambda c: (c.sort_order, c.id))
        basic = next(
            (_component_amount(c, Decimal("0")) for c in components if c.is_basic), Decimal("0.00")
        )
        for component in components:
            amount = _component_amount(component, basic)
            if component.component_type == ComponentType.EARNING:
                earnings.append(Line(component.name, amount))
                gross += amount
                if component.prorated:
                    lop_base += amount
            else:
                deductions.append(Line(component.name, amount))
        ot_rate = Decimal(
            str(
                salary.overtime_rate_per_hour
                if salary.overtime_rate_per_hour is not None
                else salary.structure.overtime_rate_per_hour or 0
            )
        )

    overtime_amount = money(Decimal(overtime_minutes) / Decimal("60") * ot_rate)
    if overtime_amount > 0:
        earnings.append(Line("Overtime", overtime_amount))
        gross += overtime_amount

    if lop_days > 0 and lop_base > 0 and total_days:
        lop_amount = money(lop_base * Decimal(str(lop_days)) / Decimal(total_days))
        if lop_amount > 0:
            deductions.append(Line("Loss of Pay", lop_amount))

    if run_id is not None:
        adjustments = db.execute(
            select(PayrollAdjustment).where(
                PayrollAdjustment.run_id == run_id, PayrollAdjustment.employee_id == employee.id
            )
        ).scalars()
        for adjustment in adjustments:
            amount = money(adjustment.amount)
            if adjustment.component_type == ComponentType.EARNING:
                earnings.append(Line(adjustment.label, amount))
                gross += amount
            else:
                deductions.append(Line(adjustment.label, amount))

    deduction_total = money(sum((line.amount for line in deductions), Decimal("0")))
    gross = money(gross)

    return PayrollResult(
        employee_id=employee.id,
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        total_days=total_days,
        working_days=round(float(eligible_days), 2),
        payable_days=round(payable, 2),
        lop_days=lop_days,
        paid_leave_days=round(paid_leave_days, 2),
        overtime_minutes=overtime_minutes,
        overtime_amount=overtime_amount,
        earnings=earnings,
        deductions=deductions,
        gross=gross,
        deduction_total=deduction_total,
        net=money(gross - deduction_total),
    )


def calculate_run(db: Session, run: PayrollRun) -> PayrollRun:
    """Recalculate every item in a payroll run. Refuses to touch a locked run."""
    if run.status in (PayrollStatus.APPROVED, PayrollStatus.PROCESSED):
        raise ValueError("Approved payroll cannot be recalculated; raise an adjustment instead")

    employees = list(
        db.execute(
            select(Employee).where(
                Employee.tenant_id == run.tenant_id,
                Employee.status == EmployeeStatus.ACTIVE,
            )
        ).scalars()
    )

    existing = {item.employee_id: item for item in run.items}
    gross_total = deduction_total = net_total = Decimal("0.00")
    _, end = month_bounds(run.period_year, run.period_month)

    for employee in employees:
        if employee.date_of_joining > end:
            continue
        result = calculate_employee(db, employee, run.period_year, run.period_month, run_id=run.id)
        item = existing.pop(employee.id, None)
        if item is None:
            item = PayrollItem(tenant_id=run.tenant_id, run_id=run.id, employee_id=employee.id)
            db.add(item)
        item.working_days = result.working_days
        item.payable_days = result.payable_days
        item.lop_days = result.lop_days
        item.paid_leave_days = result.paid_leave_days
        item.overtime_minutes = result.overtime_minutes
        item.overtime_amount = result.overtime_amount
        item.gross = result.gross
        item.deductions = result.deduction_total
        item.net = result.net
        item.breakdown_json = json.dumps(result.to_dict())

        gross_total += result.gross
        deduction_total += result.deduction_total
        net_total += result.net

    for stale in existing.values():  # employees no longer in scope for this run
        db.delete(stale)

    run.gross_total = money(gross_total)
    run.deduction_total = money(deduction_total)
    run.net_total = money(net_total)
    run.status = PayrollStatus.CALCULATED
    run.calculated_at = utcnow()
    db.commit()
    db.refresh(run)
    return run
