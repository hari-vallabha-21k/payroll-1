from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import assert_can_view_employee, get_current_user, require_payroll
from ..models import (
    Department,
    Designation,
    Employee,
    EmployeeStatus,
    Role,
    User,
    WebAuthnCredential,
)
from ..schemas import EmployeeCreate, EmployeeOut, EmployeeUpdate, NamedCreate, NamedOut

router = APIRouter(prefix="/api", tags=["employees"])


def _with_biometric(db: Session, employee: Employee) -> EmployeeOut:
    out = EmployeeOut.model_validate(employee)
    out.has_biometric = (
        db.execute(
            select(WebAuthnCredential.id).where(
                WebAuthnCredential.employee_id == employee.id,
                WebAuthnCredential.is_active.is_(True),
            )
        ).first()
        is not None
    )
    return out


def get_employee_or_404(db: Session, tenant_id: int, employee_id: int) -> Employee:
    employee = db.execute(
        select(Employee).where(Employee.id == employee_id, Employee.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if employee is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
    return employee


@router.get("/employees", response_model=list[EmployeeOut])
def list_employees(
    q: str | None = None,
    status_filter: EmployeeStatus | None = Query(None, alias="status"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Employee).where(Employee.tenant_id == user.tenant_id)
    if status_filter:
        stmt = stmt.where(Employee.status == status_filter)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            (Employee.employee_code.ilike(like))
            | (Employee.first_name.ilike(like))
            | (Employee.last_name.ilike(like))
        )
    if user.role == Role.MANAGER:
        stmt = stmt.where(
            (Employee.manager_id == user.employee_id) | (Employee.id == user.employee_id)
        )
    elif user.role == Role.EMPLOYEE:
        stmt = stmt.where(Employee.id == user.employee_id)

    employees = db.execute(stmt.order_by(Employee.employee_code)).scalars()
    return [_with_biometric(db, e) for e in employees]


@router.post("/employees", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED)
def create_employee(
    payload: EmployeeCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    exists = db.execute(
        select(Employee).where(
            Employee.tenant_id == user.tenant_id, Employee.employee_code == payload.employee_code
        )
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Employee code already in use")

    employee = Employee(tenant_id=user.tenant_id, **payload.model_dump())
    db.add(employee)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="CREATE_EMPLOYEE",
        entity_type="employee",
        detail=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(employee)
    return _with_biometric(db, employee)


@router.get("/employees/{employee_id}", response_model=EmployeeOut)
def get_employee(
    employee_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    assert_can_view_employee(db, user, employee)
    return _with_biometric(db, employee)


@router.put("/employees/{employee_id}", response_model=EmployeeOut)
def update_employee(
    employee_id: int,
    payload: EmployeeUpdate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(employee, field, value)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="UPDATE_EMPLOYEE",
        entity_type="employee",
        entity_id=employee.id,
        detail=payload.model_dump(mode="json", exclude_unset=True),
    )
    db.commit()
    db.refresh(employee)
    return _with_biometric(db, employee)


@router.delete("/employees/{employee_id}", status_code=status.HTTP_200_OK)
def deactivate_employee(
    employee_id: int, user: User = Depends(require_payroll), db: Session = Depends(get_db)
):
    """Soft delete - payroll history must remain intact."""
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    employee.status = EmployeeStatus.INACTIVE
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="DEACTIVATE_EMPLOYEE",
        entity_type="employee",
        entity_id=employee.id,
    )
    db.commit()
    return {"detail": "Employee deactivated"}


@router.get("/departments", response_model=list[NamedOut])
def list_departments(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(Department)
            .where(Department.tenant_id == user.tenant_id)
            .order_by(Department.name)
        ).scalars()
    )


@router.post("/departments", response_model=NamedOut, status_code=status.HTTP_201_CREATED)
def create_department(
    payload: NamedCreate, user: User = Depends(require_payroll), db: Session = Depends(get_db)
):
    department = Department(tenant_id=user.tenant_id, name=payload.name)
    db.add(department)
    db.commit()
    db.refresh(department)
    return department


@router.get("/designations", response_model=list[NamedOut])
def list_designations(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(Designation)
            .where(Designation.tenant_id == user.tenant_id)
            .order_by(Designation.name)
        ).scalars()
    )


@router.post("/designations", response_model=NamedOut, status_code=status.HTTP_201_CREATED)
def create_designation(
    payload: NamedCreate, user: User = Depends(require_payroll), db: Session = Depends(get_db)
):
    designation = Designation(tenant_id=user.tenant_id, name=payload.name)
    db.add(designation)
    db.commit()
    db.refresh(designation)
    return designation
