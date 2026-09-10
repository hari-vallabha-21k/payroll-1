"""The configurable payroll engine: formulas, priority, versioning, isolation."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from backend.app.models import (
    AttendanceEvent,
    Employee,
    EventSource,
    EventType,
    PayrollRule,
    PayrollRuleSet,
    RuleType,
)
from backend.app.services import attendance as attendance_service
from backend.app.services import formula
from backend.app.services import payroll as payroll_service
from backend.app.services import payroll_rules as rules_engine


# --- formula safety and correctness ----------------------------------------
@pytest.mark.parametrize(
    "expression,expected",
    [
        ("BASIC * 0.40", "8000.00"),
        ("MONTHLY_SALARY / 26", "1153.85"),
        ("OT_HOURS * OT_RATE", "400.00"),
        ("LOP_DAYS * DAILY_SALARY", "1000.00"),
        ("MIN(BASIC * 0.12, 1800)", "1800.00"),
        ("MAX(BASIC * 0.01, 500)", "500.00"),
        ("IF(GROSS <= 21000, GROSS * 0.0075, 0)", "0.00"),
        ("ROUND(BASIC / 3, 0)", "6667.00"),
    ],
)
def test_formula_evaluation(expression, expected):
    scope = {
        "BASIC": Decimal("20000"),
        "MONTHLY_SALARY": Decimal("30000"),
        "OT_HOURS": Decimal("2"),
        "OT_RATE": Decimal("200"),
        "LOP_DAYS": Decimal("1"),
        "DAILY_SALARY": Decimal("1000"),
        "GROSS": Decimal("30000"),
    }
    assert rules_engine.money(formula.evaluate(expression, scope)) == Decimal(expected)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('rm -rf /')",
        "open('/etc/passwd').read()",
        "BASIC.__class__.__bases__",
        "[x for x in range(10)]",
        "lambda: 1",
        "exec('print(1)')",
        "eval('1+1')",
        "globals()",
        "BASIC if True else 0",
        "{'a': 1}",
        "'string'",
    ],
)
def test_unsafe_formulas_are_rejected(expression):
    result = formula.validate(expression, {"BASIC"})
    assert result.ok is False, f"{expression} should not validate"


def test_unknown_variable_is_rejected():
    assert formula.validate("SALARY_TYPO * 2", {"BASIC"}).ok is False


def test_division_by_zero_yields_zero_not_a_crash():
    assert formula.evaluate("BASIC / DAYS", {"BASIC": Decimal(100), "DAYS": Decimal(0)}) == 0


def test_overlong_formula_is_rejected():
    assert formula.validate("1 + " * 200 + "1").ok is False


# --- rule execution ---------------------------------------------------------
@pytest.fixture
def rule_set(db):
    """An isolated rule set per test."""
    tenant_id = db.execute(select(Employee.tenant_id)).scalars().first()
    rule_set = PayrollRuleSet(
        tenant_id=tenant_id, name="Test rules", effective_from=date(2026, 1, 1)
    )
    db.add(rule_set)
    db.commit()
    db.refresh(rule_set)
    yield rule_set
    db.delete(rule_set)
    db.commit()


def add_rule(db, rule_set, code, name, rule_type, expression, priority, **kwargs):
    rule = PayrollRule(
        tenant_id=rule_set.tenant_id,
        rule_set_id=rule_set.id,
        code=code,
        name=name,
        rule_type=rule_type,
        formula=expression,
        priority=priority,
        effective_from=kwargs.pop("effective_from", date(2026, 1, 1)),
        **kwargs,
    )
    db.add(rule)
    db.commit()
    return rule


def test_rules_run_in_priority_order_and_build_on_each_other(db, rule_set):
    add_rule(db, rule_set, "BASIC", "Basic", RuleType.EARNING, "MONTHLY_SALARY * 0.5", 10)
    add_rule(db, rule_set, "HRA", "HRA", RuleType.EARNING, "BASIC * 0.40", 20)
    add_rule(db, rule_set, "PF", "PF", RuleType.DEDUCTION, "BASIC * 0.12", 60)

    rules = rules_engine.active_rules(db, rule_set, date(2026, 6, 1))
    assert [r.code for r in rules] == ["BASIC", "HRA", "PF"]

    result = rules_engine.run(rules, {"MONTHLY_SALARY": Decimal("30000")})
    amounts = {o.code: o.amount for o in result.outcomes}
    assert amounts["BASIC"] == Decimal("15000.00")
    assert amounts["HRA"] == Decimal("6000.00")  # depends on BASIC, so order matters
    assert amounts["PF"] == Decimal("1800.00")
    assert result.gross == Decimal("21000.00")
    assert result.net == Decimal("19200.00")


def test_priority_change_changes_the_result(db, rule_set):
    """A rule that runs before its input sees zero - proof order is honoured."""
    add_rule(db, rule_set, "BASIC", "Basic", RuleType.EARNING, "MONTHLY_SALARY * 0.5", 50)
    add_rule(db, rule_set, "HRA", "HRA", RuleType.EARNING, "BASIC * 0.40", 10)

    rules = rules_engine.active_rules(db, rule_set, date(2026, 6, 1))
    result = rules_engine.run(rules, {"MONTHLY_SALARY": Decimal("30000"), "BASIC": Decimal(0)})
    amounts = {o.code: o.amount for o in result.outcomes}
    assert amounts["HRA"] == Decimal("0.00")


def test_running_gross_is_available_to_later_rules(db, rule_set):
    add_rule(db, rule_set, "BASIC", "Basic", RuleType.EARNING, "20000", 10)
    add_rule(db, rule_set, "HRA", "HRA", RuleType.EARNING, "8000", 20)
    add_rule(db, rule_set, "ESI", "ESI", RuleType.DEDUCTION, "GROSS * 0.0075", 60)

    result = rules_engine.run(rules_engine.active_rules(db, rule_set, date(2026, 6, 1)), {})
    assert {o.code: o.amount for o in result.outcomes}["ESI"] == Decimal("210.00")


def test_each_outcome_explains_itself(db, rule_set):
    add_rule(db, rule_set, "BASIC", "Basic", RuleType.EARNING, "20000", 10)
    add_rule(db, rule_set, "LOP", "Loss of Pay", RuleType.DEDUCTION, "LOP_DAYS * DAILY", 20)

    result = rules_engine.run(
        rules_engine.active_rules(db, rule_set, date(2026, 6, 1)),
        {"LOP_DAYS": Decimal("2"), "DAILY": Decimal("500")},
    )
    lop = next(o for o in result.outcomes if o.code == "LOP")
    assert lop.amount == Decimal("1000.00")
    assert "LOP_DAYS = 2" in lop.explain()
    assert "DAILY = 500" in lop.explain()


def test_a_broken_rule_does_not_abort_the_run(db, rule_set):
    add_rule(db, rule_set, "BASIC", "Basic", RuleType.EARNING, "20000", 10)
    broken = add_rule(db, rule_set, "ODD", "Odd", RuleType.EARNING, "BASIC * 2", 20)
    broken.formula = "BASIC * MISSING"  # a variable removed after the rule was saved
    db.commit()

    result = rules_engine.run(rules_engine.active_rules(db, rule_set, date(2026, 6, 1)), {})
    odd = next(o for o in result.outcomes if o.code == "ODD")
    assert odd.error is not None
    assert odd.amount == Decimal("0.00")
    assert result.gross == Decimal("20000.00")  # the sound rules still count


# --- versioning -------------------------------------------------------------
def test_rule_versioning_pins_by_effective_date(db, rule_set):
    add_rule(
        db,
        rule_set,
        "LOP",
        "LOP v1",
        RuleType.DEDUCTION,
        "MONTHLY_SALARY / 26",
        20,
        effective_from=date(2026, 4, 1),
        effective_to=date(2026, 12, 31),
    )
    add_rule(
        db,
        rule_set,
        "LOP",
        "LOP v2",
        RuleType.DEDUCTION,
        "MONTHLY_SALARY / 30",
        20,
        effective_from=date(2027, 1, 1),
        version=2,
    )

    context = {"MONTHLY_SALARY": Decimal("30000")}
    in_2026 = rules_engine.run(rules_engine.active_rules(db, rule_set, date(2026, 6, 1)), context)
    in_2027 = rules_engine.run(rules_engine.active_rules(db, rule_set, date(2027, 6, 1)), context)

    assert in_2026.outcomes[0].amount == Decimal("1153.85")  # /26
    assert in_2027.outcomes[0].amount == Decimal("1000.00")  # /30


def test_new_version_via_api_closes_the_previous_one(client, auth):
    created = client.post(
        "/api/payroll-rules/sets",
        headers=auth,
        json={"name": "Versioning set", "effective_from": "2026-01-01"},
    ).json()
    rule = client.post(
        f"/api/payroll-rules/sets/{created['id']}/rules",
        headers=auth,
        json={
            "code": "HRA",
            "name": "HRA",
            "rule_type": "EARNING",
            "formula": "MONTHLY_SALARY * 0.40",
            "priority": 20,
            "effective_from": "2026-01-01",
        },
    ).json()

    successor = client.post(
        f"/api/payroll-rules/rules/{rule['id']}/versions",
        headers=auth,
        json={
            "code": "HRA",
            "name": "HRA",
            "rule_type": "EARNING",
            "formula": "MONTHLY_SALARY * 0.50",
            "priority": 20,
            "effective_from": "2027-01-01",
        },
    )
    assert successor.status_code == 201, successor.text
    assert successor.json()["version"] == 2

    rules = client.get(f"/api/payroll-rules/sets/{created['id']}/rules", headers=auth).json()
    original = next(r for r in rules if r["version"] == 1)
    assert original["effective_to"] == "2026-12-31"

    # Backdating a version is refused.
    assert (
        client.post(
            f"/api/payroll-rules/rules/{rule['id']}/versions",
            headers=auth,
            json={
                "code": "HRA",
                "name": "HRA",
                "rule_type": "EARNING",
                "formula": "1",
                "effective_from": "2025-01-01",
            },
        ).status_code
        == 400
    )


# --- API validation and isolation ------------------------------------------
def test_api_refuses_an_unsafe_formula(client, auth):
    rule_set = client.post(
        "/api/payroll-rules/sets",
        headers=auth,
        json={"name": "Guarded set", "effective_from": "2026-01-01"},
    ).json()
    response = client.post(
        f"/api/payroll-rules/sets/{rule_set['id']}/rules",
        headers=auth,
        json={
            "code": "EVIL",
            "name": "Evil",
            "rule_type": "EARNING",
            "formula": "__import__('os').system('ls')",
            "effective_from": "2026-01-01",
        },
    )
    assert response.status_code == 400
    assert "Invalid formula" in response.json()["detail"]


def test_validate_endpoint_reports_variables(client, auth):
    result = client.post(
        "/api/payroll-rules/validate", headers=auth, json={"formula": "BASIC * 0.4 + OT_HOURS"}
    ).json()
    assert result["ok"] is False  # BASIC is not a builtin without a rule set

    ok = client.post(
        "/api/payroll-rules/validate",
        headers=auth,
        json={"formula": "MONTHLY_SALARY * 0.4 + OT_HOURS * OT_RATE"},
    ).json()
    assert ok["ok"] is True
    assert set(ok["variables_used"]) == {"MONTHLY_SALARY", "OT_HOURS", "OT_RATE"}


def test_rule_sets_are_tenant_scoped(db, client, auth):
    """One company's rules must never be visible to another."""
    from backend.app.models import Tenant

    other = Tenant(code="OTHER1", name="Other Restaurant")
    db.add(other)
    db.commit()
    foreign = PayrollRuleSet(
        tenant_id=other.id, name="Foreign rules", effective_from=date(2026, 1, 1)
    )
    db.add(foreign)
    db.commit()

    listed = client.get("/api/payroll-rules/sets", headers=auth).json()
    assert all(item["tenant_id"] != other.id for item in listed)
    assert (
        client.get(f"/api/payroll-rules/sets/{foreign.id}/rules", headers=auth).status_code == 404
    )

    # And the engine will not pick it up for this tenant's employees.
    tenant_id = db.execute(select(Employee.tenant_id)).scalars().first()
    assert rules_engine.active_rule_set(db, tenant_id, date(2026, 6, 1)) is None or (
        rules_engine.active_rule_set(db, tenant_id, date(2026, 6, 1)).tenant_id == tenant_id
    )


def test_employee_role_cannot_create_rules(client, auth):
    client.post(
        "/api/auth/users",
        headers=auth,
        json={
            "email": "rulereader@abcrestaurant.in",
            "full_name": "Rule Reader",
            "password": "reader123456",
            "role": "EMPLOYEE",
        },
    )
    token = client.post(
        "/api/auth/login",
        json={"email": "rulereader@abcrestaurant.in", "password": "reader123456"},
    ).json()["access_token"]
    response = client.post(
        "/api/payroll-rules/sets",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Sneaky", "effective_from": "2026-01-01"},
    )
    assert response.status_code == 403


# --- end to end through the payroll engine ---------------------------------
def test_rule_engine_drives_payroll_and_preview(db, client, auth):
    """A configured rule set takes over from the salary-structure engine."""
    employee = db.execute(select(Employee).where(Employee.employee_code == "EMP003")).scalar_one()

    for day in range(1, 32):
        for hour, kind in ((9, EventType.CHECK_IN), (18, EventType.CHECK_OUT)):
            db.add(
                AttendanceEvent(
                    tenant_id=employee.tenant_id,
                    employee_id=employee.id,
                    event_type=kind,
                    event_time=datetime(2028, 3, day, hour, 0),
                    source=EventSource.MOCK_DEVICE,
                )
            )
    db.commit()
    attendance_service.process_range(db, employee, date(2028, 3, 1), date(2028, 3, 31))

    # With no rule set in force, the original salary-structure engine is used.
    suspended = list(
        db.execute(
            select(PayrollRuleSet).where(
                PayrollRuleSet.tenant_id == employee.tenant_id,
                PayrollRuleSet.is_active.is_(True),
            )
        ).scalars()
    )
    for other in suspended:
        other.is_active = False
    db.commit()

    before = payroll_service.calculate_employee(db, employee, 2028, 3)
    assert before.source == "structure"

    rule_set = PayrollRuleSet(
        tenant_id=employee.tenant_id, name="Engine test", effective_from=date(2028, 1, 1)
    )
    db.add(rule_set)
    db.commit()
    add_rule(
        db,
        rule_set,
        "BASIC",
        "Basic",
        RuleType.EARNING,
        "MONTHLY_SALARY * 0.6",
        10,
        effective_from=date(2028, 1, 1),
    )
    add_rule(
        db,
        rule_set,
        "HRA",
        "HRA",
        RuleType.EARNING,
        "BASIC * 0.4",
        20,
        effective_from=date(2028, 1, 1),
    )
    add_rule(
        db,
        rule_set,
        "PF",
        "PF",
        RuleType.DEDUCTION,
        "MIN(BASIC * 0.12, 1800)",
        60,
        effective_from=date(2028, 1, 1),
    )

    after = payroll_service.calculate_employee(db, employee, 2028, 3)
    assert after.source == "rules"
    assert after.rule_set_id == rule_set.id
    assert after.gross == Decimal("25200.00")  # 18000 basic + 7200 HRA
    assert after.deduction_total == Decimal("1800.00")
    assert after.net == Decimal("23400.00")
    assert [line["code"] for line in after.trace] == ["BASIC", "HRA", "PF"]

    preview = client.get(
        f"/api/payroll/preview?period_year=2028&period_month=3&employee_id={employee.id}",
        headers=auth,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()[0]
    assert body["source"] == "rules"
    assert body["net"] == "23400.00"
    assert any("MONTHLY_SALARY" in line["explanation"] for line in body["trace"])

    db.delete(rule_set)
    for other in suspended:
        other.is_active = True
    db.commit()


def test_manual_adjustment_is_included_and_audited(db, client, auth):
    run = client.post(
        "/api/payroll/calculate", headers=auth, json={"period_year": 2028, "period_month": 4}
    ).json()
    employee_id = run["items"][0]["employee_id"]
    before = next(i for i in run["items"] if i["employee_id"] == employee_id)

    added = client.post(
        f"/api/payroll/{run['id']}/adjustments",
        headers=auth,
        json={
            "employee_id": employee_id,
            "label": "Performance Bonus",
            "component_type": "EARNING",
            "amount": "5000.00",
            "reason": "Performance bonus for April",
        },
    )
    assert added.status_code == 201, added.text

    after_run = client.get(f"/api/payroll/{run['id']}", headers=auth).json()
    after = next(i for i in after_run["items"] if i["employee_id"] == employee_id)
    assert Decimal(after["gross"]) == Decimal(before["gross"]) + Decimal("5000.00")

    logs = client.get("/api/audit-logs?action=CREATE_PAYROLL_ADJUSTMENT", headers=auth).json()
    assert logs, "adjustments must be audited, never silent"
