import json

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import assert_can_view_employee, get_current_user, require_admin, require_payroll
from ..models import (
    Employee,
    PayrollAdjustment,
    PayrollItem,
    PayrollRun,
    PayrollStatus,
    Payslip,
    Role,
    Tenant,
    User,
    utcnow,
)
from ..schemas import (
    AdjustmentCreate,
    PayrollItemOut,
    PayrollRunCreate,
    PayrollRunDetail,
    PayrollRunOut,
)
from ..services import payroll as payroll_service
from ..services import payslip as payslip_service
from .employees import get_employee_or_404

router = APIRouter(prefix="/api", tags=["payroll"])

LOCKED = (PayrollStatus.APPROVED, PayrollStatus.PROCESSED)


def get_run_or_404(db: Session, tenant_id: int, run_id: int) -> PayrollRun:
    run = db.execute(
        select(PayrollRun).where(PayrollRun.id == run_id, PayrollRun.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payroll run not found")
    return run


@router.get("/payroll", response_model=list[PayrollRunOut])
def list_runs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(PayrollRun)
            .where(PayrollRun.tenant_id == user.tenant_id)
            .order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc())
        ).scalars()
    )


@router.post("/payroll", response_model=PayrollRunOut, status_code=status.HTTP_201_CREATED)
def create_run(
    payload: PayrollRunCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    existing = db.execute(
        select(PayrollRun).where(
            PayrollRun.tenant_id == user.tenant_id,
            PayrollRun.period_year == payload.period_year,
            PayrollRun.period_month == payload.period_month,
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "A payroll run already exists for that month")

    run = PayrollRun(tenant_id=user.tenant_id, **payload.model_dump())
    db.add(run)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="CREATE_PAYROLL_RUN",
        entity_type="payroll_run",
        detail=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(run)
    return run


@router.post("/payroll/calculate", response_model=PayrollRunDetail)
def calculate(
    payload: PayrollRunCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Create the run if needed, then (re)calculate every employee's payroll."""
    run = db.execute(
        select(PayrollRun).where(
            PayrollRun.tenant_id == user.tenant_id,
            PayrollRun.period_year == payload.period_year,
            PayrollRun.period_month == payload.period_month,
        )
    ).scalar_one_or_none()
    if run is None:
        run = PayrollRun(tenant_id=user.tenant_id, **payload.model_dump())
        db.add(run)
        db.commit()
        db.refresh(run)

    try:
        run = payroll_service.calculate_run(db, run)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="CALCULATE_PAYROLL",
        entity_type="payroll_run",
        entity_id=run.id,
        detail={"net_total": str(run.net_total), "employees": len(run.items)},
    )
    db.commit()
    db.refresh(run)
    return run


@router.get("/payroll/{run_id}", response_model=PayrollRunDetail)
def get_run(run_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    run = get_run_or_404(db, user.tenant_id, run_id)
    if user.role in (Role.ADMIN, Role.HR, Role.MANAGER):
        return run
    # An employee only sees their own line.
    detail = PayrollRunDetail.model_validate(run)
    detail.items = [
        PayrollItemOut.model_validate(item)
        for item in run.items
        if item.employee_id == user.employee_id
    ]
    return detail


@router.get("/payroll/{run_id}/items/{employee_id}")
def get_run_item(
    run_id: int,
    employee_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    run = get_run_or_404(db, user.tenant_id, run_id)
    employee = get_employee_or_404(db, user.tenant_id, employee_id)
    assert_can_view_employee(db, user, employee)
    item = db.execute(
        select(PayrollItem).where(
            PayrollItem.run_id == run.id, PayrollItem.employee_id == employee.id
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No payroll line for that employee")
    return json.loads(item.breakdown_json or "{}")


@router.post("/payroll/{run_id}/adjustments", status_code=status.HTTP_201_CREATED)
def add_adjustment(
    run_id: int,
    payload: AdjustmentCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    run = get_run_or_404(db, user.tenant_id, run_id)
    if run.status in LOCKED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Payroll is approved; reopen it before adjusting"
        )
    get_employee_or_404(db, user.tenant_id, payload.employee_id)

    adjustment = PayrollAdjustment(
        tenant_id=user.tenant_id,
        run_id=run.id,
        created_by_user_id=user.id,
        **payload.model_dump(),
    )
    db.add(adjustment)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="CREATE_PAYROLL_ADJUSTMENT",
        entity_type="payroll_run",
        entity_id=run.id,
        detail=payload.model_dump(mode="json"),
    )
    db.commit()
    payroll_service.calculate_run(db, run)
    return {"detail": "Adjustment applied and payroll recalculated"}


@router.post("/payroll/{run_id}/review", response_model=PayrollRunOut)
def mark_under_review(
    run_id: int, user: User = Depends(require_payroll), db: Session = Depends(get_db)
):
    run = get_run_or_404(db, user.tenant_id, run_id)
    if run.status != PayrollStatus.CALCULATED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a calculated payroll can go to review")
    run.status = PayrollStatus.UNDER_REVIEW
    db.commit()
    db.refresh(run)
    return run


@router.post("/payroll/{run_id}/approve", response_model=PayrollRunOut)
def approve(run_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    run = get_run_or_404(db, user.tenant_id, run_id)
    if run.status in LOCKED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Payroll is already approved")
    if run.status not in (PayrollStatus.CALCULATED, PayrollStatus.UNDER_REVIEW):
        raise HTTPException(status.HTTP_409_CONFLICT, "Calculate the payroll before approving")

    run.status = PayrollStatus.APPROVED
    run.approved_by_user_id = user.id
    run.approved_at = utcnow()
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="APPROVE_PAYROLL",
        entity_type="payroll_run",
        entity_id=run.id,
        detail={"net_total": str(run.net_total)},
    )
    db.commit()
    db.refresh(run)
    return run


@router.post("/payroll/{run_id}/reopen", response_model=PayrollRunOut)
def reopen(
    run_id: int,
    reason: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Authorised reversal - the only way back out of an approved payroll."""
    run = get_run_or_404(db, user.tenant_id, run_id)
    if run.status not in LOCKED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Payroll is not locked")
    run.status = PayrollStatus.UNDER_REVIEW
    run.approved_by_user_id = None
    run.approved_at = None
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="REOPEN_PAYROLL",
        entity_type="payroll_run",
        entity_id=run.id,
        detail={"reason": reason},
    )
    db.commit()
    db.refresh(run)
    return run


@router.post("/payroll/{run_id}/process", response_model=PayrollRunOut)
def mark_processed(run_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    run = get_run_or_404(db, user.tenant_id, run_id)
    if run.status != PayrollStatus.APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Approve the payroll before processing")
    run.status = PayrollStatus.PROCESSED
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="PROCESS_PAYROLL",
        entity_type="payroll_run",
        entity_id=run.id,
    )
    db.commit()
    db.refresh(run)
    return run


# --- payslips ---------------------------------------------------------------
@router.post("/payroll/{run_id}/payslips")
def generate_payslips(
    run_id: int, user: User = Depends(require_payroll), db: Session = Depends(get_db)
):
    run = get_run_or_404(db, user.tenant_id, run_id)
    if run.status not in LOCKED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Payslips can only be issued from approved payroll"
        )
    tenant = db.get(Tenant, run.tenant_id)

    created = 0
    for item in run.items:
        existing = db.execute(
            select(Payslip).where(Payslip.run_id == run.id, Payslip.employee_id == item.employee_id)
        ).scalar_one_or_none()
        employee = db.get(Employee, item.employee_id)
        snapshot = payslip_service.build_snapshot(tenant, run, item, employee)
        if existing is None:
            db.add(
                Payslip(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    employee_id=item.employee_id,
                    payslip_number=(
                        f"{tenant.code}-{run.period_year}{run.period_month:02d}-"
                        f"{employee.employee_code}"
                    ),
                    snapshot_json=json.dumps(snapshot),
                )
            )
            created += 1
        else:
            existing.snapshot_json = json.dumps(snapshot)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="GENERATE_PAYSLIPS",
        entity_type="payroll_run",
        entity_id=run.id,
        detail={"created": created},
    )
    db.commit()
    return {"detail": "Payslips generated", "created": created, "total": len(run.items)}


@router.get("/payslips")
def list_payslips(
    run_id: int | None = None,
    employee_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Payslip).where(Payslip.tenant_id == user.tenant_id)
    if run_id:
        stmt = stmt.where(Payslip.run_id == run_id)
    if employee_id:
        stmt = stmt.where(Payslip.employee_id == employee_id)
    if user.role == Role.EMPLOYEE:
        stmt = stmt.where(Payslip.employee_id == (user.employee_id or -1))
    payslips = db.execute(stmt.order_by(Payslip.id.desc())).scalars()
    return [
        {
            "id": p.id,
            "run_id": p.run_id,
            "employee_id": p.employee_id,
            "payslip_number": p.payslip_number,
            "generated_at": p.generated_at,
            "snapshot": json.loads(p.snapshot_json),
        }
        for p in payslips
    ]


@router.get("/payslips/{payslip_id}/pdf")
def download_payslip(
    payslip_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    payslip = db.execute(
        select(Payslip).where(Payslip.id == payslip_id, Payslip.tenant_id == user.tenant_id)
    ).scalar_one_or_none()
    if payslip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payslip not found")
    employee = get_employee_or_404(db, user.tenant_id, payslip.employee_id)
    assert_can_view_employee(db, user, employee)

    pdf = payslip_service.render_pdf(payslip)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{payslip.payslip_number}.pdf"',
        },
    )
