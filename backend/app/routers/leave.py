from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import assert_can_view_employee, get_current_user, require_manager, require_payroll
from ..models import (
    Employee,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    Role,
    User,
    utcnow,
)
from ..schemas import (
    LeaveDecision,
    LeaveRequestCreate,
    LeaveRequestOut,
    LeaveTypeCreate,
    LeaveTypeOut,
)
from ..services import attendance as attendance_service
from .employees import get_employee_or_404

router = APIRouter(prefix="/api", tags=["leave"])


@router.get("/leave-types", response_model=list[LeaveTypeOut])
def list_leave_types(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(LeaveType).where(LeaveType.tenant_id == user.tenant_id).order_by(LeaveType.name)
        ).scalars()
    )


@router.post("/leave-types", response_model=LeaveTypeOut, status_code=status.HTTP_201_CREATED)
def create_leave_type(
    payload: LeaveTypeCreate, user: User = Depends(require_payroll), db: Session = Depends(get_db)
):
    leave_type = LeaveType(tenant_id=user.tenant_id, **payload.model_dump())
    db.add(leave_type)
    db.commit()
    db.refresh(leave_type)
    return leave_type


@router.get("/leave-requests", response_model=list[LeaveRequestOut])
def list_leave_requests(
    status_filter: LeaveStatus | None = None,
    employee_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(LeaveRequest).where(LeaveRequest.tenant_id == user.tenant_id)
    if status_filter:
        stmt = stmt.where(LeaveRequest.status == status_filter)
    if employee_id:
        stmt = stmt.where(LeaveRequest.employee_id == employee_id)
    if user.role == Role.EMPLOYEE:
        stmt = stmt.where(LeaveRequest.employee_id == (user.employee_id or -1))
    elif user.role == Role.MANAGER:
        team = db.execute(
            select(Employee.id).where(
                Employee.tenant_id == user.tenant_id,
                (Employee.manager_id == user.employee_id) | (Employee.id == user.employee_id),
            )
        ).scalars()
        stmt = stmt.where(LeaveRequest.employee_id.in_(list(team) or [-1]))
    return list(db.execute(stmt.order_by(LeaveRequest.created_at.desc())).scalars())


@router.post("/leave-requests", response_model=LeaveRequestOut, status_code=status.HTTP_201_CREATED)
def create_leave_request(
    payload: LeaveRequestCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    employee_id = payload.employee_id or user.employee_id
    if employee_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No employee to raise this request for")
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    assert_can_view_employee(db, user, employee)

    if payload.end_date < payload.start_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "End date is before start date")

    leave_type = db.execute(
        select(LeaveType).where(
            LeaveType.id == payload.leave_type_id, LeaveType.tenant_id == user.tenant_id
        )
    ).scalar_one_or_none()
    if leave_type is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Leave type not found")

    request_row = LeaveRequest(
        tenant_id=user.tenant_id,
        employee_id=employee.id,
        leave_type_id=leave_type.id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        reason=payload.reason,
    )
    db.add(request_row)
    db.commit()
    db.refresh(request_row)
    return request_row


@router.post("/leave-requests/{request_id}/decision", response_model=LeaveRequestOut)
def decide_leave(
    request_id: int,
    payload: LeaveDecision,
    user: User = Depends(require_manager),
    db: Session = Depends(get_db),
):
    request_row = db.execute(
        select(LeaveRequest).where(
            LeaveRequest.id == request_id, LeaveRequest.tenant_id == user.tenant_id
        )
    ).scalar_one_or_none()
    if request_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Leave request not found")
    if payload.status not in (LeaveStatus.APPROVED, LeaveStatus.REJECTED, LeaveStatus.CANCELLED):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported decision")

    employee = get_employee_or_404(db, user.tenant_id, request_row.employee_id)
    assert_can_view_employee(db, user, employee)

    request_row.status = payload.status
    request_row.decision_note = payload.note
    request_row.decided_by_user_id = user.id
    request_row.decided_at = utcnow()
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="DECIDE_LEAVE",
        entity_type="leave_request",
        entity_id=request_row.id,
        detail=payload.model_dump(mode="json"),
    )
    db.commit()

    # Approved leave changes the attendance picture for those days.
    attendance_service.process_range(db, employee, request_row.start_date, request_row.end_date)
    db.refresh(request_row)
    return request_row
