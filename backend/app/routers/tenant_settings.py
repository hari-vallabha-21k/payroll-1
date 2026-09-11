import base64

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import Tenant, TenantSetting, User
from ..schemas import CompanyProfile, TenantOut

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/tenant", response_model=TenantOut)
def get_tenant(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.get(Tenant, user.tenant_id)


@router.get("")
def list_settings(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.execute(
        select(TenantSetting).where(TenantSetting.tenant_id == user.tenant_id)
    ).scalars()
    return {row.key: row.value for row in rows}


# The company details a payslip header can print. Held as tenant settings so a
# new field needs no migration.
COMPANY_KEYS = (
    "company_address",
    "company_phone",
    "company_email",
    "company_gst",
    "company_registration",
    "company_logo",
)

MAX_LOGO_BYTES = 512 * 1024
ALLOWED_LOGO_TYPES = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}


def _read_settings(db: Session, tenant_id: int, keys) -> dict[str, str]:
    rows = db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == tenant_id, TenantSetting.key.in_(list(keys))
        )
    ).scalars()
    return {row.key: row.value for row in rows}


def _write_setting(db: Session, tenant_id: int, key: str, value: str) -> None:
    row = db.execute(
        select(TenantSetting).where(TenantSetting.tenant_id == tenant_id, TenantSetting.key == key)
    ).scalar_one_or_none()
    if row is None:
        db.add(TenantSetting(tenant_id=tenant_id, key=key, value=value))
    else:
        row.value = value


@router.get("/company", response_model=CompanyProfile)
def get_company_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, user.tenant_id)
    values = _read_settings(db, user.tenant_id, COMPANY_KEYS)
    return CompanyProfile(
        name=tenant.name,
        code=tenant.code,
        currency=tenant.currency,
        address=values.get("company_address", ""),
        phone=values.get("company_phone", ""),
        email=values.get("company_email", ""),
        gst_number=values.get("company_gst", ""),
        registration_number=values.get("company_registration", ""),
        logo=values.get("company_logo", ""),
    )


@router.put("/company", response_model=CompanyProfile)
def update_company_profile(
    payload: CompanyProfile,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Company details for the payslip header.

    Changing these does not alter payslips already issued - each one carries
    its own copy of the company details as they were at the time.
    """
    tenant = db.get(Tenant, user.tenant_id)
    if payload.name:
        tenant.name = payload.name
    if payload.currency:
        tenant.currency = payload.currency

    for key, value in (
        ("company_address", payload.address),
        ("company_phone", payload.phone),
        ("company_email", payload.email),
        ("company_gst", payload.gst_number),
        ("company_registration", payload.registration_number),
    ):
        _write_setting(db, user.tenant_id, key, value or "")

    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="COMPANY_PROFILE_UPDATED",
        entity_type="tenant",
        entity_id=user.tenant_id,
        detail={"fields": [k for k, v in payload.model_dump().items() if v]},
    )
    db.commit()
    return get_company_profile(user=user, db=db)


@router.post("/company/logo", response_model=CompanyProfile)
async def upload_logo(
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Store the logo inline as a data URI.

    Small images only, which keeps payslip rendering self-contained - no
    external fetch when a PDF is produced months later.
    """
    if file.content_type not in ALLOWED_LOGO_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unsupported image type '{file.content_type}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_LOGO_TYPES))}",
        )

    content = await file.read(MAX_LOGO_BYTES + 1)
    if len(content) > MAX_LOGO_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Logo must be {MAX_LOGO_BYTES // 1024} KB or smaller",
        )
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty")

    encoded = base64.b64encode(content).decode("ascii")
    _write_setting(db, user.tenant_id, "company_logo", f"data:{file.content_type};base64,{encoded}")
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="COMPANY_LOGO_UPDATED",
        entity_type="tenant",
        entity_id=user.tenant_id,
        detail={"content_type": file.content_type, "bytes": len(content)},
    )
    db.commit()
    return get_company_profile(user=user, db=db)


@router.delete("/company/logo", response_model=CompanyProfile)
def remove_logo(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    _write_setting(db, user.tenant_id, "company_logo", "")
    db.commit()
    return get_company_profile(user=user, db=db)


# Declared last: a catch-all path parameter would otherwise shadow the
# specific /company routes above.
@router.put("/{key}")
def set_setting(
    key: str, value: str, user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    row = db.execute(
        select(TenantSetting).where(
            TenantSetting.tenant_id == user.tenant_id, TenantSetting.key == key
        )
    ).scalar_one_or_none()
    if row is None:
        row = TenantSetting(tenant_id=user.tenant_id, key=key, value=value)
        db.add(row)
    else:
        row.value = value
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="UPDATE_SETTING",
        entity_type="tenant_setting",
        entity_id=key,
        detail={"value": value},
    )
    db.commit()
    return {key: value}
