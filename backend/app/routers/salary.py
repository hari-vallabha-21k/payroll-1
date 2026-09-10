from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import assert_can_view_employee, get_current_user, require_payroll
from ..models import EmployeeSalary, SalaryComponent, SalaryStructure, User
from ..schemas import (
    EmployeeSalaryAssign,
    EmployeeSalaryOut,
    SalaryStructureCreate,
    SalaryStructureOut,
)
from .employees import get_employee_or_404

router = APIRouter(prefix="/api", tags=["salary"])


@router.get("/salary-structures", response_model=list[SalaryStructureOut])
def list_structures(user: User = Depends(require_payroll), db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(SalaryStructure)
            .where(SalaryStructure.tenant_id == user.tenant_id)
            .order_by(SalaryStructure.name)
        ).scalars()
    )


@router.post(
    "/salary-structures", response_model=SalaryStructureOut, status_code=status.HTTP_201_CREATED
)
def create_structure(
    payload: SalaryStructureCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    structure = SalaryStructure(
        tenant_id=user.tenant_id,
        name=payload.name,
        overtime_rate_per_hour=payload.overtime_rate_per_hour,
    )
    for index, component in enumerate(payload.components):
        structure.components.append(
            SalaryComponent(
                tenant_id=user.tenant_id,
                sort_order=component.sort_order or index,
                **component.model_dump(exclude={"sort_order"}),
            )
        )
    db.add(structure)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="CREATE_SALARY_STRUCTURE",
        entity_type="salary_structure",
        detail=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(structure)
    return structure


@router.put("/salary-structures/{structure_id}", response_model=SalaryStructureOut)
def replace_structure(
    structure_id: int,
    payload: SalaryStructureCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    structure = db.execute(
        select(SalaryStructure).where(
            SalaryStructure.id == structure_id, SalaryStructure.tenant_id == user.tenant_id
        )
    ).scalar_one_or_none()
    if structure is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Salary structure not found")

    before = {
        "name": structure.name,
        "components": [
            {"name": c.name, "type": c.component_type.value, "amount": str(c.amount)}
            for c in structure.components
        ],
    }
    structure.name = payload.name
    structure.overtime_rate_per_hour = payload.overtime_rate_per_hour
    structure.components.clear()
    db.flush()
    for index, component in enumerate(payload.components):
        structure.components.append(
            SalaryComponent(
                tenant_id=user.tenant_id,
                sort_order=component.sort_order or index,
                **component.model_dump(exclude={"sort_order"}),
            )
        )
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="UPDATE_SALARY_STRUCTURE",
        entity_type="salary_structure",
        entity_id=structure.id,
        detail={"before": before, "after": payload.model_dump(mode="json")},
    )
    db.commit()
    db.refresh(structure)
    return structure


@router.get("/employees/{employee_id}/salary", response_model=list[EmployeeSalaryOut])
def employee_salary_history(
    employee_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    assert_can_view_employee(db, user, employee)
    return list(
        db.execute(
            select(EmployeeSalary)
            .where(EmployeeSalary.employee_id == employee.id)
            .order_by(EmployeeSalary.effective_from.desc())
        ).scalars()
    )


@router.post(
    "/employees/{employee_id}/salary",
    response_model=EmployeeSalaryOut,
    status_code=status.HTTP_201_CREATED,
)
def assign_salary(
    employee_id: int,
    payload: EmployeeSalaryAssign,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    structure = db.execute(
        select(SalaryStructure).where(
            SalaryStructure.id == payload.structure_id,
            SalaryStructure.tenant_id == user.tenant_id,
        )
    ).scalar_one_or_none()
    if structure is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Salary structure not found")

    for existing in db.execute(
        select(EmployeeSalary).where(
            EmployeeSalary.employee_id == employee.id, EmployeeSalary.effective_to.is_(None)
        )
    ).scalars():
        existing.effective_to = payload.effective_from

    record = EmployeeSalary(
        tenant_id=user.tenant_id, employee_id=employee.id, **payload.model_dump()
    )
    db.add(record)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="ASSIGN_SALARY",
        entity_type="employee",
        entity_id=employee.id,
        detail=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(record)
    return record
