"""Settings -> Payroll -> Payslip Templates.

Templates control presentation only. Nothing here can change a salary figure.
"""

import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import get_current_user, require_payroll
from ..models import PayslipTemplate, User, utcnow
from ..schemas import (
    PayslipTemplateCreate,
    PayslipTemplateOut,
    PayslipTemplatePreviewRequest,
    PayslipTemplateUpdate,
)
from ..services import payslip as payslip_service
from ..services import payslip_template as template_service

router = APIRouter(prefix="/api/payslip-templates", tags=["payslip templates"])

SAMPLE_SNAPSHOT = {
    "tenant": {
        "name": "ABC Restaurant",
        "currency": "INR",
        "address": "12 Marine Drive, Mumbai 400020",
        "phone": "+91 22 1234 5678",
        "email": "payroll@abcrestaurant.in",
        "gst_number": "27ABCDE1234F1Z5",
    },
    "period": {"year": 2026, "month": 8, "label": "August 2026"},
    "employee": {
        "code": "EMP001",
        "name": "Rahul Kumar",
        "designation": "Chef",
        "department": "Kitchen",
        "date_of_joining": "2026-01-08",
        "bank_account": "XXXXXXXX4321",
    },
    "attendance": {
        "working_days": 31,
        "payable_days": 30,
        "paid_leave_days": 1,
        "lop_days": 1,
        "overtime_minutes": 120,
    },
    "earnings": [
        {"label": "Basic Salary", "amount": "20000.00"},
        {"label": "HRA", "amount": "8000.00"},
        {"label": "Overtime", "amount": "2000.00"},
        {"label": "Bonus", "amount": "1000.00"},
    ],
    "deductions": [
        {"label": "PF", "amount": "2400.00"},
        {"label": "ESI", "amount": "300.00"},
        {"label": "Loss of Pay", "amount": "500.00"},
    ],
    "totals": {"gross": "31000.00", "deductions": "3200.00", "net": "27800.00"},
    "payslip_number": "REST001-202608-EMP001",
}


def get_template_or_404(db: Session, tenant_id: int, template_id: int) -> PayslipTemplate:
    template = db.execute(
        select(PayslipTemplate).where(
            PayslipTemplate.id == template_id, PayslipTemplate.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payslip template not found")
    return template


def active_template(db: Session, tenant_id: int, on_date: date) -> PayslipTemplate | None:
    """The template in force for a tenant on a date, default last."""
    stmt = (
        select(PayslipTemplate)
        .where(
            PayslipTemplate.tenant_id == tenant_id,
            PayslipTemplate.is_active.is_(True),
            PayslipTemplate.effective_from <= on_date,
            (PayslipTemplate.effective_to.is_(None)) | (PayslipTemplate.effective_to >= on_date),
        )
        .order_by(
            PayslipTemplate.is_default.desc(),
            PayslipTemplate.effective_from.desc(),
            PayslipTemplate.version.desc(),
        )
    )
    return db.execute(stmt).scalars().first()


def _validate_or_400(template_data: dict) -> None:
    problems = template_service.validate_template(template_data)
    if problems:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(problems))


@router.get("/default-definition")
def default_definition(user: User = Depends(get_current_user)):
    """The built-in professional template, as a starting point."""
    return {
        "template_data": template_service.DEFAULT_TEMPLATE,
        "section_types": list(template_service.SECTION_TYPES),
        "variables": sorted(template_service.build_context(SAMPLE_SNAPSHOT).variables.keys()),
    }


@router.get("", response_model=list[PayslipTemplateOut])
def list_templates(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(PayslipTemplate)
            .where(PayslipTemplate.tenant_id == user.tenant_id)
            .order_by(PayslipTemplate.name, PayslipTemplate.version.desc())
        ).scalars()
    )


@router.post("", response_model=PayslipTemplateOut, status_code=status.HTTP_201_CREATED)
def create_template(
    payload: PayslipTemplateCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    template_data = payload.template_data or template_service.DEFAULT_TEMPLATE
    _validate_or_400(template_data)

    if payload.is_default:
        _clear_default(db, user.tenant_id)

    template = PayslipTemplate(
        tenant_id=user.tenant_id,
        name=payload.name,
        template_data=template_service.dumps(template_data),
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        is_default=payload.is_default,
        created_by_user_id=user.id,
    )
    db.add(template)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="PAYSLIP_TEMPLATE_CREATED",
        entity_type="payslip_template",
        detail={"name": payload.name, "effective_from": str(payload.effective_from)},
    )
    db.commit()
    db.refresh(template)
    return template


def _clear_default(db: Session, tenant_id: int) -> None:
    for existing in db.execute(
        select(PayslipTemplate).where(
            PayslipTemplate.tenant_id == tenant_id, PayslipTemplate.is_default.is_(True)
        )
    ).scalars():
        existing.is_default = False


@router.get("/{template_id}", response_model=PayslipTemplateOut)
def get_template(
    template_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return get_template_or_404(db, user.tenant_id, template_id)


@router.put("/{template_id}", response_model=PayslipTemplateOut)
def update_template(
    template_id: int,
    payload: PayslipTemplateUpdate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Edit in place. Payslips already issued keep their own copy, so their
    appearance does not change."""
    template = get_template_or_404(db, user.tenant_id, template_id)
    changes = payload.model_dump(exclude_unset=True)

    if "template_data" in changes and changes["template_data"] is not None:
        _validate_or_400(changes["template_data"])
        template.template_data = template_service.dumps(changes.pop("template_data"))
    changes.pop("template_data", None)

    if changes.get("is_default"):
        _clear_default(db, user.tenant_id)

    for field, value in changes.items():
        setattr(template, field, value)
    template.updated_at = utcnow()

    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="PAYSLIP_TEMPLATE_UPDATED",
        entity_type="payslip_template",
        entity_id=template.id,
        detail={"fields": sorted(payload.model_dump(exclude_unset=True))},
    )
    db.commit()
    db.refresh(template)
    return template


@router.post(
    "/{template_id}/versions",
    response_model=PayslipTemplateOut,
    status_code=status.HTTP_201_CREATED,
)
def create_version(
    template_id: int,
    payload: PayslipTemplateCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Supersede a template from a date onward, closing the current version."""
    current = get_template_or_404(db, user.tenant_id, template_id)
    if payload.effective_from <= current.effective_from:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A new version must start after the version it replaces",
        )
    template_data = payload.template_data or json.loads(current.template_data)
    _validate_or_400(template_data)

    current.effective_to = payload.effective_from - timedelta(days=1)
    current.updated_at = utcnow()

    successor = PayslipTemplate(
        tenant_id=user.tenant_id,
        name=payload.name or current.name,
        template_data=template_service.dumps(template_data),
        version=current.version + 1,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        is_default=current.is_default,
        created_by_user_id=user.id,
    )
    db.add(successor)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="PAYSLIP_TEMPLATE_VERSION_CREATED",
        entity_type="payslip_template",
        entity_id=current.id,
        detail={
            "previous_version": current.version,
            "new_version": current.version + 1,
            "effective_from": str(payload.effective_from),
        },
    )
    db.commit()
    db.refresh(successor)
    return successor


@router.post("/preview", response_class=Response)
def preview_template(
    payload: PayslipTemplatePreviewRequest,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Render a template against sample or real data, before saving it."""
    if payload.template_data is not None:
        template_data = payload.template_data
    elif payload.template_id is not None:
        template = get_template_or_404(db, user.tenant_id, payload.template_id)
        template_data = json.loads(template.template_data)
    else:
        template_data = template_service.DEFAULT_TEMPLATE
    _validate_or_400(template_data)

    # Preview against this tenant's real company details, so an administrator
    # sees their own header rather than placeholder text.
    snapshot = json.loads(json.dumps(SAMPLE_SNAPSHOT))
    from .tenant_settings import get_company_profile

    company = get_company_profile(user=user, db=db)
    snapshot["tenant"] = {
        "name": company.name or snapshot["tenant"]["name"],
        "currency": company.currency,
        "address": company.address,
        "phone": company.phone,
        "email": company.email,
        "gst_number": company.gst_number,
        "registration_number": company.registration_number,
        "logo_url": company.logo,
    }

    if payload.payslip_id is not None:
        from ..models import Payslip

        payslip = db.execute(
            select(Payslip).where(
                Payslip.id == payload.payslip_id, Payslip.tenant_id == user.tenant_id
            )
        ).scalar_one_or_none()
        if payslip is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Payslip not found")
        snapshot = json.loads(payslip.snapshot_json)

    if payload.format == "pdf":
        from ..models import Payslip

        probe = Payslip(
            tenant_id=user.tenant_id,
            run_id=0,
            employee_id=0,
            payslip_number=snapshot.get("payslip_number", "PREVIEW"),
            snapshot_json=json.dumps(snapshot),
            template_snapshot_json=template_service.dumps(template_data),
        )
        return Response(
            content=payslip_service.render_pdf(probe),
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="payslip-preview.pdf"'},
        )

    return Response(
        content=template_service.render_html(template_data, snapshot), media_type="text/html"
    )
