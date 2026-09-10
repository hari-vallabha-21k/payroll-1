"""The configurable payroll rule engine.

Rules are ordered by priority and evaluated in sequence; each result is put
back into scope under the rule's code, so later rules can build on earlier
ones (HRA on BASIC, LOP on DAILY_SALARY, NET on GROSS). Every step records how
it was computed, which is what the preview screen and the audit trail show.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PayrollRule, PayrollRuleSet, RuleType
from .formula import FormulaError, evaluate

TWOPLACES = Decimal("0.01")


def money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


@dataclass
class RuleOutcome:
    """One evaluated rule, with enough detail to explain the number."""

    code: str
    name: str
    rule_type: RuleType
    priority: int
    formula: str
    amount: Decimal
    inputs: dict[str, str] = field(default_factory=dict)
    show_on_payslip: bool = True
    error: str | None = None

    def explain(self) -> str:
        if self.error:
            return f"{self.formula} -> {self.error}"
        if not self.inputs:
            return self.formula
        substituted = ", ".join(f"{name} = {value}" for name, value in sorted(self.inputs.items()))
        return f"{self.formula}  (with {substituted})"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "label": self.name,
            "type": self.rule_type.value,
            "priority": self.priority,
            "formula": self.formula,
            "amount": str(self.amount),
            "inputs": self.inputs,
            "explanation": self.explain(),
            "show_on_payslip": self.show_on_payslip,
            "error": self.error,
        }


@dataclass
class RuleRunResult:
    outcomes: list[RuleOutcome] = field(default_factory=list)
    scope: dict[str, Decimal] = field(default_factory=dict)

    @property
    def earnings(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.rule_type == RuleType.EARNING]

    @property
    def deductions(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.rule_type == RuleType.DEDUCTION]

    @property
    def gross(self) -> Decimal:
        return money(sum((o.amount for o in self.earnings), Decimal(0)))

    @property
    def deduction_total(self) -> Decimal:
        return money(sum((o.amount for o in self.deductions), Decimal(0)))

    @property
    def net(self) -> Decimal:
        return money(self.gross - self.deduction_total)


def active_rule_set(db: Session, tenant_id: int, on_date: date) -> PayrollRuleSet | None:
    """The rule set in force for a tenant on a date.

    Tenant scoping is part of the query, so one company's rules can never be
    applied to another company's employees.
    """
    stmt = (
        select(PayrollRuleSet)
        .where(
            PayrollRuleSet.tenant_id == tenant_id,
            PayrollRuleSet.is_active.is_(True),
            PayrollRuleSet.effective_from <= on_date,
            (PayrollRuleSet.effective_to.is_(None)) | (PayrollRuleSet.effective_to >= on_date),
        )
        .order_by(PayrollRuleSet.effective_from.desc(), PayrollRuleSet.version.desc())
    )
    return db.execute(stmt).scalars().first()


def active_rules(db: Session, rule_set: PayrollRuleSet, on_date: date) -> list[PayrollRule]:
    """Rules of a set that are in force on a date, in execution order."""
    stmt = (
        select(PayrollRule)
        .where(
            PayrollRule.rule_set_id == rule_set.id,
            PayrollRule.is_active.is_(True),
            PayrollRule.effective_from <= on_date,
            (PayrollRule.effective_to.is_(None)) | (PayrollRule.effective_to >= on_date),
        )
        .order_by(PayrollRule.priority, PayrollRule.id)
    )
    rules = list(db.execute(stmt).scalars())

    # A code may have several versions; keep the one in force with the highest
    # version, preserving priority order.
    by_code: dict[str, PayrollRule] = {}
    for rule in rules:
        current = by_code.get(rule.code)
        if current is None or rule.version > current.version:
            by_code[rule.code] = rule
    return sorted(by_code.values(), key=lambda r: (r.priority, r.id))


def run(rules: list[PayrollRule], context: dict[str, Decimal]) -> RuleRunResult:
    """Evaluate rules in priority order against a starting context."""
    scope: dict[str, Decimal] = dict(context)
    scope.setdefault("GROSS", Decimal(0))
    scope.setdefault("TOTAL_EARNINGS", Decimal(0))
    scope.setdefault("TOTAL_DEDUCTIONS", Decimal(0))
    scope.setdefault("NET", Decimal(0))

    result = RuleRunResult(scope=scope)

    for rule in rules:
        from .formula import names_used

        try:
            referenced, _ = names_used(rule.formula)
        except FormulaError:
            referenced = set()
        inputs = {name: str(scope[name]) for name in sorted(referenced) if name in scope}

        try:
            amount = money(evaluate(rule.formula, scope))
            error = None
        except FormulaError as exc:
            # One broken rule must not abort the whole payroll run; it is
            # surfaced on the payslip preview and in the audit detail instead.
            amount, error = Decimal("0.00"), str(exc)

        outcome = RuleOutcome(
            code=rule.code,
            name=rule.name,
            rule_type=rule.rule_type,
            priority=rule.priority,
            formula=rule.formula,
            amount=amount,
            inputs=inputs,
            show_on_payslip=rule.show_on_payslip and (rule.show_if_zero or amount != 0),
            error=error,
        )
        result.outcomes.append(outcome)

        # Feed the result forward for later rules.
        scope[rule.code] = amount
        if rule.rule_type == RuleType.EARNING:
            scope["TOTAL_EARNINGS"] = scope["TOTAL_EARNINGS"] + amount
            scope["GROSS"] = scope["TOTAL_EARNINGS"]
        elif rule.rule_type == RuleType.DEDUCTION:
            scope["TOTAL_DEDUCTIONS"] = scope["TOTAL_DEDUCTIONS"] + amount
        scope["NET"] = scope["GROSS"] - scope["TOTAL_DEDUCTIONS"]

    return result


def known_variable_codes(db: Session, rule_set: PayrollRuleSet | None) -> set[str]:
    """Variables a formula in this rule set may reference."""
    from .formula import BUILTIN_CODES, DERIVED_CODES

    codes = set(BUILTIN_CODES) | set(DERIVED_CODES)
    if rule_set is not None:
        codes |= {rule.code for rule in rule_set.rules}
    return codes
