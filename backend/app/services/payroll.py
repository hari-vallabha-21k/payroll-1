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
from . import payroll_rules as rules_engine

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
    # "rules" when a configured rule set produced these figures, "structure"
    # for the built-in salary-structure calculation.
    source: str = "structure"
    rule_set_id: int | None = None
    rule_set_version: int | None = None
    trace: list[dict] = field(default_factory=list)

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
            "source": self.source,
            "rule_set_id": self.rule_set_id,
            "rule_set_version": self.rule_set_version,
            "trace": self.trace,
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


@dataclass
class AttendanceSummary:
    """Attendance facts a payroll calculation needs, whatever engine runs."""

    total_days: int
    eligible_days: int
    payable_days: float
    present_days: float
    paid_leave_days: float
    unpaid_leave_days: float
    absent_days: float
    lop_days: float
    overtime_minutes: int


def summarise_attendance(
    db: Session, employee: Employee, year: int, month: int
) -> AttendanceSummary:
    """Aggregate processed attendance for one employee-month.

    Shared by both engines so the rule-driven and structure-driven paths can
    never disagree about how many days were worked.
    """
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
    unpaid_leave_days = 0.0
    absent_days = 0.0
    overtime_minutes = 0
    for offset in range(eligible_days):
        day = date.fromordinal(period_start.toordinal() + offset)
        record = by_date.get(day)
        if record is None:
            absent_days += 1.0  # unprocessed day counts as loss of pay
            continue
        payable += record.payable_day_fraction
        overtime_minutes += record.overtime_minutes
        if record.status in (AttendanceStatus.PRESENT, AttendanceStatus.HALF_DAY):
            present_days += record.payable_day_fraction
        elif record.status == AttendanceStatus.ON_LEAVE:
            if record.payable_day_fraction > 0:
                paid_leave_days += record.payable_day_fraction
            else:
                unpaid_leave_days += 1.0
        elif record.status == AttendanceStatus.ABSENT:
            absent_days += 1.0

    # Days before joining are not the employee's loss.
    payable += total_days - eligible_days
    lop_days = round(total_days - payable, 2)

    return AttendanceSummary(
        total_days=total_days,
        eligible_days=eligible_days,
        payable_days=round(payable, 2),
        present_days=round(present_days, 2),
        paid_leave_days=round(paid_leave_days, 2),
        unpaid_leave_days=round(unpaid_leave_days, 2),
        absent_days=round(absent_days, 2),
        lop_days=lop_days,
        overtime_minutes=overtime_minutes,
    )


def calculate_employee(
    db: Session, employee: Employee, year: int, month: int, *, run_id: int | None = None
) -> PayrollResult:
    """Calculate one employee's payroll.

    A tenant with a configured rule set is calculated by the rule engine; one
    without falls back to the built-in salary-structure calculation, so
    existing installations keep working untouched.
    """
    _, end = month_bounds(year, month)
    summary = summarise_attendance(db, employee, year, month)
    rule_set = rules_engine.active_rule_set(db, employee.tenant_id, end)
    if rule_set is not None:
        return _calculate_with_rules(db, employee, year, month, summary, rule_set, run_id)
    return _calculate_with_structure(db, employee, year, month, summary, run_id)


def _calculate_with_structure(
    db: Session,
    employee: Employee,
    year: int,
    month: int,
    summary: AttendanceSummary,
    run_id: int | None,
) -> PayrollResult:
    """The original engine: salary structure components, pro-rated by LOP."""
    _, end = month_bounds(year, month)
    total_days = summary.total_days
    eligible_days = summary.eligible_days
    payable = summary.payable_days
    paid_leave_days = summary.paid_leave_days
    overtime_minutes = summary.overtime_minutes
    lop_days = summary.lop_days

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


def build_context(
    db: Session, employee: Employee, summary: AttendanceSummary, end: date
) -> dict[str, Decimal]:
    """Variables every formula starts with, drawn from attendance and salary."""
    salary = active_salary(db, employee, end)

    monthly = Decimal("0")
    ot_rate = Decimal("0")
    if salary is not None:
        if salary.monthly_ctc is not None:
            monthly = Decimal(str(salary.monthly_ctc))
        else:
            monthly = sum(
                (
                    _component_amount(component, Decimal("0"))
                    for component in salary.structure.components
                    if component.component_type == ComponentType.EARNING
                ),
                Decimal("0"),
            )
        ot_rate = Decimal(
            str(
                salary.overtime_rate_per_hour
                if salary.overtime_rate_per_hour is not None
                else salary.structure.overtime_rate_per_hour or 0
            )
        )

    day_basis = Decimal(str(tenant_number(db, employee.tenant_id, "payroll_day_basis", 0)))
    if day_basis <= 0:
        day_basis = Decimal(summary.total_days)
    standard_hours = Decimal(str(tenant_number(db, employee.tenant_id, "standard_hours", 8)))

    daily = money(monthly / day_basis) if day_basis else Decimal("0.00")
    hourly = money(daily / standard_hours) if standard_hours else Decimal("0.00")

    return {
        "MONTHLY_SALARY": money(monthly),
        "DAILY_SALARY": daily,
        "HOURLY_RATE": hourly,
        "DAY_BASIS": day_basis,
        "STANDARD_HOURS": standard_hours,
        "TOTAL_DAYS": Decimal(summary.total_days),
        "WORKING_DAYS": Decimal(str(summary.eligible_days)),
        "PAYABLE_DAYS": Decimal(str(summary.payable_days)),
        "PRESENT_DAYS": Decimal(str(summary.present_days)),
        "ABSENT_DAYS": Decimal(str(summary.absent_days)),
        "PAID_LEAVE": Decimal(str(summary.paid_leave_days)),
        "UNPAID_LEAVE": Decimal(str(summary.unpaid_leave_days)),
        "LOP_DAYS": Decimal(str(summary.lop_days)),
        "OT_HOURS": (Decimal(summary.overtime_minutes) / Decimal(60)).quantize(Decimal("0.01")),
        "OT_RATE": money(ot_rate),
    }


def tenant_number(db: Session, tenant_id: int, key: str, default: float) -> float:
    """Read a numeric tenant setting, falling back when unset or unparsable."""
    from ..models import TenantSetting

    setting = db.execute(
        select(TenantSetting).where(TenantSetting.tenant_id == tenant_id, TenantSetting.key == key)
    ).scalar_one_or_none()
    if setting is None or not setting.value.strip():
        return default
    try:
        return float(setting.value)
    except ValueError:
        return default


def _calculate_with_rules(
    db: Session,
    employee: Employee,
    year: int,
    month: int,
    summary: AttendanceSummary,
    rule_set,
    run_id: int | None,
) -> PayrollResult:
    """Run the tenant's configured rules in priority order."""
    _, end = month_bounds(year, month)
    context = build_context(db, employee, summary, end)
    rules = rules_engine.active_rules(db, rule_set, end)
    run_result = rules_engine.run(rules, context)

    earnings = [
        Line(outcome.name, outcome.amount)
        for outcome in run_result.earnings
        if outcome.show_on_payslip
    ]
    deductions = [
        Line(outcome.name, outcome.amount)
        for outcome in run_result.deductions
        if outcome.show_on_payslip
    ]
    gross = run_result.gross
    deduction_total = run_result.deduction_total
    trace = [outcome.to_dict() for outcome in run_result.outcomes]

    overtime_amount = Decimal("0.00")
    for outcome in run_result.outcomes:
        if outcome.code in ("OVERTIME", "OT"):
            overtime_amount = outcome.amount
            break

    if run_id is not None:
        adjustments = db.execute(
            select(PayrollAdjustment).where(
                PayrollAdjustment.run_id == run_id, PayrollAdjustment.employee_id == employee.id
            )
        ).scalars()
        for adjustment in adjustments:
            amount = money(adjustment.amount)
            line = Line(adjustment.label, amount)
            if adjustment.component_type == ComponentType.EARNING:
                earnings.append(line)
                gross += amount
            else:
                deductions.append(line)
                deduction_total += amount
            trace.append(
                {
                    "code": f"ADJUSTMENT_{adjustment.id}",
                    "label": adjustment.label,
                    "type": adjustment.component_type.value,
                    "priority": 999,
                    "formula": "manual adjustment",
                    "amount": str(amount),
                    "inputs": {},
                    "explanation": f"Manual adjustment: {adjustment.reason or 'no reason given'}",
                    "show_on_payslip": True,
                    "error": None,
                }
            )

    gross = money(gross)
    deduction_total = money(deduction_total)

    return PayrollResult(
        employee_id=employee.id,
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        total_days=summary.total_days,
        working_days=round(float(summary.eligible_days), 2),
        payable_days=summary.payable_days,
        lop_days=summary.lop_days,
        paid_leave_days=summary.paid_leave_days,
        overtime_minutes=summary.overtime_minutes,
        overtime_amount=overtime_amount,
        earnings=earnings,
        deductions=deductions,
        gross=gross,
        deduction_total=deduction_total,
        net=money(gross - deduction_total),
        source="rules",
        rule_set_id=rule_set.id,
        rule_set_version=rule_set.version,
        trace=trace,
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

    period_end = month_bounds(run.period_year, run.period_month)[1]
    pinned = rules_engine.active_rule_set(db, run.tenant_id, period_end)
    if pinned is not None:
        run.rule_set_id = pinned.id
        run.rule_set_version = pinned.version

    run.gross_total = money(gross_total)
    run.deduction_total = money(deduction_total)
    run.net_total = money(net_total)
    run.status = PayrollStatus.CALCULATED
    run.calculated_at = utcnow()
    db.commit()
    db.refresh(run)
    return run
