"""Demo tenant used to exercise the whole flow end to end."""

from __future__ import annotations

from datetime import date, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import (
    ComponentType,
    Department,
    Designation,
    Employee,
    EmployeeSalary,
    EmployeeShift,
    LeaveType,
    Role,
    SalaryComponent,
    SalaryStructure,
    Shift,
    Tenant,
    TenantSetting,
    User,
)
from .security import hash_password

settings = get_settings()

DEMO_TENANT_CODE = "REST001"


def seed(db: Session) -> Tenant:
    tenant = db.execute(select(Tenant).where(Tenant.code == DEMO_TENANT_CODE)).scalar_one_or_none()
    if tenant is not None:
        return tenant

    tenant = Tenant(code=DEMO_TENANT_CODE, name="ABC Restaurant", currency="INR")
    db.add(tenant)
    db.flush()

    db.add(TenantSetting(tenant_id=tenant.id, key="weekly_off_days", value=""))

    kitchen = Department(tenant_id=tenant.id, name="Kitchen")
    service = Department(tenant_id=tenant.id, name="Service")
    chef = Designation(tenant_id=tenant.id, name="Chef")
    waiter = Designation(tenant_id=tenant.id, name="Waiter")
    db.add_all([kitchen, service, chef, waiter])

    morning = Shift(
        tenant_id=tenant.id,
        name="Morning",
        start_time=time(9, 0),
        end_time=time(18, 0),
        break_minutes=60,
        grace_minutes=10,
    )
    evening = Shift(
        tenant_id=tenant.id,
        name="Evening",
        start_time=time(14, 0),
        end_time=time(23, 0),
        break_minutes=60,
        grace_minutes=10,
    )
    db.add_all([morning, evening])

    casual = LeaveType(tenant_id=tenant.id, name="Casual Leave", is_paid=True, annual_quota_days=12)
    unpaid = LeaveType(tenant_id=tenant.id, name="Unpaid Leave", is_paid=False, annual_quota_days=0)
    db.add_all([casual, unpaid])

    structure = SalaryStructure(
        tenant_id=tenant.id, name="Kitchen Staff - 30k", overtime_rate_per_hour=200
    )
    structure.components = [
        SalaryComponent(
            tenant_id=tenant.id,
            name="Basic",
            component_type=ComponentType.EARNING,
            amount=20000,
            is_basic=True,
            sort_order=0,
        ),
        SalaryComponent(
            tenant_id=tenant.id,
            name="HRA",
            component_type=ComponentType.EARNING,
            amount=5000,
            sort_order=1,
        ),
        SalaryComponent(
            tenant_id=tenant.id,
            name="Transport Allowance",
            component_type=ComponentType.EARNING,
            amount=2000,
            sort_order=2,
        ),
        SalaryComponent(
            tenant_id=tenant.id,
            name="Other Allowance",
            component_type=ComponentType.EARNING,
            amount=3000,
            sort_order=3,
        ),
        SalaryComponent(
            tenant_id=tenant.id,
            name="Provident Fund",
            component_type=ComponentType.DEDUCTION,
            percent_of_basic=10.0,
            sort_order=4,
        ),
        SalaryComponent(
            tenant_id=tenant.id,
            name="Professional Tax",
            component_type=ComponentType.DEDUCTION,
            amount=200,
            sort_order=5,
        ),
    ]
    db.add(structure)
    db.flush()

    employees = [
        ("EMP001", "Rahul", "Sharma", kitchen, chef, morning),
        ("EMP002", "Priya", "Nair", service, waiter, evening),
        ("EMP003", "Imran", "Khan", kitchen, chef, morning),
    ]
    created: list[Employee] = []
    for code, first, last, department, designation, shift in employees:
        employee = Employee(
            tenant_id=tenant.id,
            employee_code=code,
            first_name=first,
            last_name=last,
            date_of_joining=date(2026, 8, 1),
            department_id=department.id,
            designation_id=designation.id,
            shift_id=shift.id,
        )
        db.add(employee)
        db.flush()
        db.add(
            EmployeeShift(
                tenant_id=tenant.id,
                employee_id=employee.id,
                shift_id=shift.id,
                effective_from=date(2026, 8, 1),
            )
        )
        db.add(
            EmployeeSalary(
                tenant_id=tenant.id,
                employee_id=employee.id,
                structure_id=structure.id,
                monthly_ctc=30000,
                effective_from=date(2026, 8, 1),
            )
        )
        created.append(employee)

    db.add(
        User(
            tenant_id=tenant.id,
            email=settings.default_admin_email.lower(),
            full_name="Restaurant Admin",
            password_hash=hash_password(settings.default_admin_password),
            role=Role.ADMIN,
        )
    )
    db.add(
        User(
            tenant_id=tenant.id,
            email="rahul@abcrestaurant.in",
            full_name="Rahul Sharma",
            password_hash=hash_password(settings.default_admin_password),
            role=Role.EMPLOYEE,
            employee_id=created[0].id,
        )
    )
    db.commit()
    return tenant
