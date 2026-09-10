"""Settings -> Payroll -> Payroll Rules.

Rules are configuration, not code: an administrator changes how salary is
calculated here without anyone touching the source.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..deps import get_current_user, require_payroll
from ..models import PayrollRule, PayrollRuleSet, RuleType, User, utcnow
from ..schemas import (
    FormulaValidationRequest,
    FormulaValidationResult,
    PayrollRuleCreate,
    PayrollRuleOut,
    PayrollRuleSetCreate,
    PayrollRuleSetOut,
    PayrollRuleUpdate,
    VariableOut,
)
from ..services import formula as formula_service
from ..services import payroll_rules as rules_engine

router = APIRouter(prefix="/api/payroll-rules", tags=["payroll rules"])

# A conventional Indian restaurant payroll, offered as a starting point so an
# administrator can adjust rather than compose from nothing.
STARTER_RULES = [
    ("BASIC", "Basic Salary", RuleType.EARNING, "MONTHLY_SALARY * 0.60", 10),
    ("DAILY_SALARY_CALC", "Daily Salary", RuleType.INTERMEDIATE, "MONTHLY_SALARY / DAY_BASIS", 15),
    ("HRA", "House Rent Allowance", RuleType.EARNING, "BASIC * 0.40", 20),
    (
        "SPECIAL_ALLOWANCE",
        "Special Allowance",
        RuleType.EARNING,
        "MONTHLY_SALARY - BASIC - HRA",
        30,
    ),
    ("OVERTIME", "Overtime", RuleType.EARNING, "OT_HOURS * OT_RATE", 40),
    ("PF", "Provident Fund", RuleType.DEDUCTION, "MIN(BASIC * 0.12, 1800)", 60),
    ("ESI", "ESI", RuleType.DEDUCTION, "IF(GROSS <= 21000, GROSS * 0.0075, 0)", 65),
    ("PROFESSIONAL_TAX", "Professional Tax", RuleType.DEDUCTION, "200", 70),
    ("LOP", "Loss of Pay", RuleType.DEDUCTION, "LOP_DAYS * DAILY_SALARY_CALC", 75),
]


def get_rule_set_or_404(db: Session, tenant_id: int, rule_set_id: int) -> PayrollRuleSet:
    rule_set = db.execute(
        select(PayrollRuleSet).where(
            PayrollRuleSet.id == rule_set_id, PayrollRuleSet.tenant_id == tenant_id
        )
    ).scalar_one_or_none()
    if rule_set is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payroll rule set not found")
    return rule_set


def _validate_or_400(db: Session, rule_set: PayrollRuleSet, code: str, formula: str) -> None:
    known = rules_engine.known_variable_codes(db, rule_set) | {code}
    result = formula_service.validate(formula, known)
    if not result.ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid formula: {result.error}")


@router.get("/variables", response_model=list[VariableOut])
def list_variables(
    rule_set_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Variables the formula builder offers, including this set's own rules."""
    variables = [
        VariableOut(code=v.code, label=v.label, description=v.description, kind="builtin")
        for v in formula_service.BUILTIN_VARIABLES
    ]
    variables += [
        VariableOut(
            code=code,
            label=code.replace("_", " ").title(),
            description="Running total produced by the engine",
            kind="derived",
        )
        for code in sorted(formula_service.DERIVED_CODES)
    ]
    if rule_set_id:
        rule_set = get_rule_set_or_404(db, user.tenant_id, rule_set_id)
        variables += [
            VariableOut(
                code=rule.code,
                label=rule.name,
                description=f"Result of the '{rule.name}' rule",
                kind="rule",
            )
            for rule in sorted(rule_set.rules, key=lambda r: r.priority)
        ]
    return variables


@router.get("/functions")
def list_functions(user: User = Depends(get_current_user)):
    return {
        "functions": sorted(formula_service.FUNCTIONS),
        "operators": ["+", "-", "*", "/", "%", "**", "<", "<=", ">", ">=", "==", "!="],
    }


@router.post("/validate", response_model=FormulaValidationResult)
def validate_formula(
    payload: FormulaValidationRequest,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Check a formula before saving it - the builder calls this as you type."""
    known = None
    if payload.rule_set_id:
        rule_set = get_rule_set_or_404(db, user.tenant_id, payload.rule_set_id)
        known = rules_engine.known_variable_codes(db, rule_set)
    result = formula_service.validate(payload.formula, known)
    return FormulaValidationResult(
        ok=result.ok,
        error=result.error,
        variables_used=result.variables_used,
        functions_used=result.functions_used,
    )


@router.get("/sets", response_model=list[PayrollRuleSetOut])
def list_rule_sets(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(PayrollRuleSet)
            .where(PayrollRuleSet.tenant_id == user.tenant_id)
            .order_by(PayrollRuleSet.effective_from.desc(), PayrollRuleSet.version.desc())
        ).scalars()
    )


@router.get("/sets/active", response_model=PayrollRuleSetOut | None)
def get_active_rule_set(
    on_date: date | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return rules_engine.active_rule_set(db, user.tenant_id, on_date or date.today())


@router.post("/sets", response_model=PayrollRuleSetOut, status_code=status.HTTP_201_CREATED)
def create_rule_set(
    payload: PayrollRuleSetCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    rule_set = PayrollRuleSet(
        tenant_id=user.tenant_id,
        created_by_user_id=user.id,
        **payload.model_dump(),
    )
    db.add(rule_set)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="PAYROLL_RULE_SET_CREATED",
        entity_type="payroll_rule_set",
        detail=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(rule_set)
    return rule_set


@router.post("/sets/starter", response_model=PayrollRuleSetOut, status_code=status.HTTP_201_CREATED)
def create_starter_rule_set(
    effective_from: date = Query(...),
    name: str = Query("Standard Payroll"),
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Create a conventional rule set to adapt, rather than starting empty."""
    rule_set = PayrollRuleSet(
        tenant_id=user.tenant_id,
        name=name,
        description="Starter rules - adjust the formulas to match your policy",
        effective_from=effective_from,
        created_by_user_id=user.id,
    )
    db.add(rule_set)
    db.flush()

    for code, rule_name, rule_type, expression, priority in STARTER_RULES:
        db.add(
            PayrollRule(
                tenant_id=user.tenant_id,
                rule_set_id=rule_set.id,
                code=code,
                name=rule_name,
                rule_type=rule_type,
                formula=expression,
                priority=priority,
                effective_from=effective_from,
                created_by_user_id=user.id,
                show_on_payslip=rule_type != RuleType.INTERMEDIATE,
            )
        )
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="PAYROLL_RULE_SET_CREATED",
        entity_type="payroll_rule_set",
        entity_id=rule_set.id,
        detail={"starter": True, "rules": len(STARTER_RULES)},
    )
    db.commit()
    db.refresh(rule_set)
    return rule_set


@router.post("/sets/{rule_set_id}/deactivate", response_model=PayrollRuleSetOut)
def deactivate_rule_set(
    rule_set_id: int,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    rule_set = get_rule_set_or_404(db, user.tenant_id, rule_set_id)
    rule_set.is_active = False
    rule_set.updated_at = utcnow()
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="PAYROLL_RULE_DEACTIVATED",
        entity_type="payroll_rule_set",
        entity_id=rule_set.id,
    )
    db.commit()
    db.refresh(rule_set)
    return rule_set


@router.get("/sets/{rule_set_id}/rules", response_model=list[PayrollRuleOut])
def list_rules(
    rule_set_id: int,
    on_date: date | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rule_set = get_rule_set_or_404(db, user.tenant_id, rule_set_id)
    if on_date:
        return rules_engine.active_rules(db, rule_set, on_date)
    return sorted(rule_set.rules, key=lambda r: (r.priority, r.id))


@router.post(
    "/sets/{rule_set_id}/rules", response_model=PayrollRuleOut, status_code=status.HTTP_201_CREATED
)
def create_rule(
    rule_set_id: int,
    payload: PayrollRuleCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    rule_set = get_rule_set_or_404(db, user.tenant_id, rule_set_id)
    _validate_or_400(db, rule_set, payload.code, payload.formula)

    rule = PayrollRule(
        tenant_id=user.tenant_id,
        rule_set_id=rule_set.id,
        created_by_user_id=user.id,
        **payload.model_dump(),
    )
    db.add(rule)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="PAYROLL_RULE_CREATED",
        entity_type="payroll_rule",
        detail=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(rule)
    return rule


@router.put("/rules/{rule_id}", response_model=PayrollRuleOut)
def update_rule(
    rule_id: int,
    payload: PayrollRuleUpdate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Edit a rule in place. To change a formula from a date onward without
    touching past payroll, create a new version instead."""
    rule = db.execute(
        select(PayrollRule).where(
            PayrollRule.id == rule_id, PayrollRule.tenant_id == user.tenant_id
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payroll rule not found")

    changes = payload.model_dump(exclude_unset=True)
    if "formula" in changes:
        _validate_or_400(db, rule.rule_set, rule.code, changes["formula"])

    before = {"formula": rule.formula, "priority": rule.priority, "is_active": rule.is_active}
    for field, value in changes.items():
        setattr(rule, field, value)
    rule.updated_at = utcnow()

    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="PAYROLL_RULE_UPDATED",
        entity_type="payroll_rule",
        entity_id=rule.id,
        detail={"before": before, "after": payload.model_dump(mode="json", exclude_unset=True)},
    )
    db.commit()
    db.refresh(rule)
    return rule


@router.post(
    "/rules/{rule_id}/versions", response_model=PayrollRuleOut, status_code=status.HTTP_201_CREATED
)
def create_rule_version(
    rule_id: int,
    payload: PayrollRuleCreate,
    user: User = Depends(require_payroll),
    db: Session = Depends(get_db),
):
    """Supersede a rule from a date onward.

    The previous version is closed the day before the new one starts, so a
    payroll already run against the old formula keeps its figures.
    """
    current = db.execute(
        select(PayrollRule).where(
            PayrollRule.id == rule_id, PayrollRule.tenant_id == user.tenant_id
        )
    ).scalar_one_or_none()
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payroll rule not found")
    if payload.effective_from <= current.effective_from:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A new version must start after the version it replaces",
        )

    _validate_or_400(db, current.rule_set, current.code, payload.formula)

    from datetime import timedelta

    current.effective_to = payload.effective_from - timedelta(days=1)
    current.updated_at = utcnow()

    fields = payload.model_dump()
    fields["code"] = current.code
    successor = PayrollRule(
        tenant_id=user.tenant_id,
        rule_set_id=current.rule_set_id,
        created_by_user_id=user.id,
        version=current.version + 1,
        **fields,
    )
    db.add(successor)
    audit.record(
        db,
        tenant_id=user.tenant_id,
        user_id=user.id,
        actor=user.email,
        action="PAYROLL_RULE_VERSION_CREATED",
        entity_type="payroll_rule",
        entity_id=current.id,
        detail={
            "code": current.code,
            "previous_version": current.version,
            "new_version": current.version + 1,
            "effective_from": str(payload.effective_from),
            "formula": payload.formula,
        },
    )
    db.commit()
    db.refresh(successor)
    return successor
