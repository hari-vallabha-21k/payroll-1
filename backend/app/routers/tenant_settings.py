from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import get_current_user, require_admin
from ..models import Tenant, TenantSetting, User
from ..schemas import TenantOut

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
