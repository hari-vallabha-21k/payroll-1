from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import get_current_user, require_payroll
from ..models import EmployeeShift, Holiday, Shift, User
from ..schemas import ShiftCreate, ShiftOut
from .employees import get_employee_or_404

router = APIRouter(prefix="/api", tags=["shifts"])


@router.get("/shifts", response_model=list[ShiftOut])
def list_shifts(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(Shift).where(Shift.tenant_id == user.tenant_id).order_by(Shift.name)
        ).scalars()
    )


@router.post("/shifts", response_model=ShiftOut, status_code=status.HTTP_201_CREATED)
def create_shift(
    payload: ShiftCreate, user: User = Depends(require_payroll), db: Session = Depends(get_db)
):
    shift = Shift(tenant_id=user.tenant_id, **payload.model_dump())
    db.add(shift)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="CREATE_SHIFT",
        entity_type="shift",
        detail=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(shift)
    return shift


@router.put("/shifts/{shift_id}", response_model=ShiftOut)
def update_shift(
    shift_id: int,
    payload: ShiftCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id, Shift.tenant_id == user.tenant_id)
    ).scalar_one_or_none()
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shift not found")
    for field, value in payload.model_dump().items():
        setattr(shift, field, value)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="UPDATE_SHIFT",
        entity_type="shift",
        entity_id=shift.id,
        detail=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(shift)
    return shift


@router.post("/employees/{employee_id}/shift")
def assign_shift(
    employee_id: int,
    shift_id: int,
    effective_from: date,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    shift = db.execute(
        select(Shift).where(Shift.id == shift_id, Shift.tenant_id == user.tenant_id)
    ).scalar_one_or_none()
    if shift is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Shift not found")

    # Close the assignment currently in force.
    for existing in db.execute(
        select(EmployeeShift).where(
            EmployeeShift.employee_id == employee.id, EmployeeShift.effective_to.is_(None)
        )
    ).scalars():
        existing.effective_to = effective_from

    db.add(
        EmployeeShift(
            tenant_id=user.tenant_id,
            employee_id=employee.id,
            shift_id=shift.id,
            effective_from=effective_from,
        )
    )
    employee.shift_id = shift.id
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="ASSIGN_SHIFT",
        entity_type="employee",
        entity_id=employee.id,
        detail={"shift_id": shift.id, "effective_from": str(effective_from)},
    )
    db.commit()
    return {"detail": f"{employee.employee_code} assigned to {shift.name}"}


@router.get("/holidays")
def list_holidays(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(Holiday)
            .where(Holiday.tenant_id == user.tenant_id)
            .order_by(Holiday.holiday_date)
        ).scalars()
    )


@router.post("/holidays", status_code=status.HTTP_201_CREATED)
def create_holiday(
    holiday_date: date,
    name: str,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    holiday = Holiday(tenant_id=user.tenant_id, holiday_date=holiday_date, name=name)
    db.add(holiday)
    db.commit()
    db.refresh(holiday)
    return holiday
