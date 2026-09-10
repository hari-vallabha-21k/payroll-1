"""Payslip templates: rendering, versioning, and historical immutability."""

import itertools
import json
from decimal import Decimal

import pytest

from backend.app.services import payslip_template as template_service

# --- rendering --------------------------------------------------------------
SNAPSHOT = {
    "tenant": {"name": "ABC Restaurant", "address": "Mumbai", "gst_number": "27ABCDE1234F1Z5"},
    "period": {"label": "August 2026"},
    "employee": {
        "code": "EMP001",
        "name": "Rahul Kumar",
        "designation": "Chef",
        "department": "Kitchen",
        "date_of_joining": "2026-01-08",
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
        {"label": "Food Allowance", "amount": "0.00"},
    ],
    "deductions": [{"label": "PF", "amount": "2400.00"}],
    "totals": {"gross": "28000.00", "deductions": "2400.00", "net": "25600.00"},
}


def test_placeholders_are_substituted():
    html = template_service.render_html(template_service.DEFAULT_TEMPLATE, SNAPSHOT)
    assert "ABC Restaurant" in html
    assert "Rahul Kumar" in html
    assert "EMP001" in html
    assert "August 2026" in html
    assert "{{" not in html  # nothing left unresolved


def test_components_render_dynamically_and_zero_rows_are_hidden():
    """An employee without a component gets no empty row for it."""
    html = template_service.render_html(template_service.DEFAULT_TEMPLATE, SNAPSHOT)
    assert "Basic Salary" in html
    assert "HRA" in html
    assert "Food Allowance" not in html  # zero-valued, so hidden

    shown = json.loads(json.dumps(template_service.DEFAULT_TEMPLATE))
    for section in shown["sections"]:
        if section["type"] == "earnings_deductions":
            section["hide_zero_rows"] = False
    assert "Food Allowance" in template_service.render_html(shown, SNAPSHOT)


def test_unknown_placeholder_renders_empty_rather_than_failing():
    template = json.loads(json.dumps(template_service.DEFAULT_TEMPLATE))
    template["sections"][1]["text"] = "Hello {{no_such_variable}}!"
    html = template_service.render_html(template, SNAPSHOT)
    assert "Hello !" in html


def test_disabled_sections_are_omitted():
    template = json.loads(json.dumps(template_service.DEFAULT_TEMPLATE))
    for section in template["sections"]:
        if section["type"] == "attendance":
            section["enabled"] = False
    html = template_service.render_html(template, SNAPSHOT)
    assert "Payable Days" not in html
    assert "Basic Salary" in html  # the rest still renders


def test_template_content_is_escaped():
    """Company details come from user input and must not inject markup."""
    snapshot = json.loads(json.dumps(SNAPSHOT))
    snapshot["employee"]["name"] = "<script>alert(1)</script>"
    html = template_service.render_html(template_service.DEFAULT_TEMPLATE, snapshot)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize(
    "amount,expected",
    [
        ("27800", "Rupees Twenty Seven Thousand Eight Hundred Only"),
        ("0", "Rupees Zero Only"),
        ("100.50", "Rupees One Hundred and Fifty Paise Only"),
        ("10000000", "Rupees One Crore Only"),
    ],
)
def test_amount_in_words(amount, expected):
    assert template_service.amount_in_words(amount) == expected


def test_template_validation_rejects_nonsense():
    assert template_service.validate_template({}) != []
    assert template_service.validate_template({"sections": []}) != []
    assert template_service.validate_template({"sections": [{"type": "not_a_section"}]}) != []
    # A payslip without earnings/deductions is not a payslip.
    assert template_service.validate_template({"sections": [{"type": "title"}]}) != []
    assert template_service.validate_template(template_service.DEFAULT_TEMPLATE) == []


# --- API --------------------------------------------------------------------
@pytest.fixture
def template(client, auth):
    created = client.post(
        "/api/payslip-templates",
        headers=auth,
        json={
            "name": "Restaurant Staff Payslip",
            "effective_from": "2026-01-01",
            "is_default": True,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_preview_renders_html_and_pdf(client, auth, template):
    html = client.post(
        "/api/payslip-templates/preview", headers=auth, json={"template_id": template["id"]}
    )
    assert html.status_code == 200
    assert "text/html" in html.headers["content-type"]
    assert "Salary Slip" in html.text

    pdf = client.post(
        "/api/payslip-templates/preview",
        headers=auth,
        json={"template_id": template["id"], "format": "pdf"},
    )
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")


def test_preview_of_an_unsaved_definition(client, auth):
    definition = client.get("/api/payslip-templates/default-definition", headers=auth).json()
    data = definition["template_data"]
    data["sections"][1]["text"] = "DRAFT LAYOUT"
    response = client.post(
        "/api/payslip-templates/preview", headers=auth, json={"template_data": data}
    )
    assert response.status_code == 200
    assert "DRAFT LAYOUT" in response.text


def test_invalid_template_is_refused(client, auth):
    response = client.post(
        "/api/payslip-templates",
        headers=auth,
        json={
            "name": "Broken",
            "effective_from": "2026-01-01",
            "template_data": {"sections": [{"type": "made_up"}]},
        },
    )
    assert response.status_code == 400


def test_versioning_closes_the_previous_version(client, auth, template):
    successor = client.post(
        f"/api/payslip-templates/{template['id']}/versions",
        headers=auth,
        json={"name": "Restaurant Staff Payslip", "effective_from": "2027-01-01"},
    )
    assert successor.status_code == 201, successor.text
    assert successor.json()["version"] == 2

    original = client.get(f"/api/payslip-templates/{template['id']}", headers=auth).json()
    assert original["effective_to"] == "2026-12-31"

    assert (
        client.post(
            f"/api/payslip-templates/{template['id']}/versions",
            headers=auth,
            json={"name": "Backdated", "effective_from": "2025-01-01"},
        ).status_code
        == 400
    )


def test_templates_are_tenant_scoped(db, client, auth):
    from datetime import date

    from backend.app.models import PayslipTemplate, Tenant

    other = Tenant(code="OTHER2", name="Another Restaurant")
    db.add(other)
    db.commit()
    foreign = PayslipTemplate(
        tenant_id=other.id,
        name="Foreign template",
        template_data="{}",
        effective_from=date(2026, 1, 1),
    )
    db.add(foreign)
    db.commit()

    listed = client.get("/api/payslip-templates", headers=auth).json()
    assert all(item["tenant_id"] != other.id for item in listed)
    assert client.get(f"/api/payslip-templates/{foreign.id}", headers=auth).status_code == 404


def test_employee_role_cannot_edit_templates(client, auth, template):
    client.post(
        "/api/auth/users",
        headers=auth,
        json={
            "email": "designer@abcrestaurant.in",
            "full_name": "Designer",
            "password": "design123456",
            "role": "EMPLOYEE",
        },
    )
    token = client.post(
        "/api/auth/login", json={"email": "designer@abcrestaurant.in", "password": "design123456"}
    ).json()["access_token"]
    response = client.put(
        f"/api/payslip-templates/{template['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Renamed"},
    )
    assert response.status_code == 403


# --- historical payslips ----------------------------------------------------
# Each test gets its own pay period: an approved run is locked, so two tests
# sharing one period would collide.
_period_counter = itertools.count(2029)


@pytest.fixture
def issued_payslip(client, auth, template):
    """An approved payroll run with payslips issued from the template."""
    period = {"period_year": next(_period_counter), "period_month": 5}
    calculated = client.post("/api/payroll/calculate", headers=auth, json=period)
    assert calculated.status_code == 200, calculated.text
    run = calculated.json()
    client.post(f"/api/payroll/{run['id']}/approve", headers=auth)
    generated = client.post(f"/api/payroll/{run['id']}/payslips", headers=auth)
    assert generated.status_code == 200, generated.text
    payslips = client.get(f"/api/payslips?run_id={run['id']}", headers=auth).json()
    return payslips[0]


def test_payslip_records_the_template_it_used(issued_payslip, template):
    assert issued_payslip["template_id"] == template["id"]
    assert issued_payslip["template_version"] == template["version"]


def test_editing_a_template_does_not_change_issued_payslips(client, auth, template, issued_payslip):
    """The central guarantee: history does not move when a design changes."""
    before = client.get(f"/api/payslips/{issued_payslip['id']}/html", headers=auth).text
    before_pdf = client.get(f"/api/payslips/{issued_payslip['id']}/pdf", headers=auth).content

    definition = client.get("/api/payslip-templates/default-definition", headers=auth).json()
    data = definition["template_data"]
    data["page"]["accent"] = "#ff0000"
    data["sections"][1]["text"] = "COMPLETELY REDESIGNED"
    updated = client.put(
        f"/api/payslip-templates/{template['id']}", headers=auth, json={"template_data": data}
    )
    assert updated.status_code == 200

    after = client.get(f"/api/payslips/{issued_payslip['id']}/html", headers=auth).text
    assert after == before
    assert "COMPLETELY REDESIGNED" not in after
    assert len(
        client.get(f"/api/payslips/{issued_payslip['id']}/pdf", headers=auth).content
    ) == len(before_pdf)

    # The new design does apply to fresh previews.
    preview = client.post(
        "/api/payslip-templates/preview", headers=auth, json={"template_id": template["id"]}
    )
    assert "COMPLETELY REDESIGNED" in preview.text


def test_company_and_employee_changes_do_not_alter_issued_payslips(
    db, client, auth, issued_payslip
):
    before = client.get(f"/api/payslips/{issued_payslip['id']}/html", headers=auth).text

    from backend.app.models import Employee, Tenant

    employee = db.get(Employee, issued_payslip["employee_id"])
    employee.first_name = "Renamed"
    tenant = db.get(Tenant, employee.tenant_id)
    tenant.name = "Renamed Restaurant Ltd"
    db.commit()

    after = client.get(f"/api/payslips/{issued_payslip['id']}/html", headers=auth).text
    assert after == before
    assert "Renamed Restaurant Ltd" not in after


def test_regenerate_keeps_figures_and_is_audited(client, auth, issued_payslip):
    response = client.post(f"/api/payslips/{issued_payslip['id']}/regenerate", headers=auth)
    assert response.status_code == 200
    assert response.json()["figures_unchanged"] is True

    logs = client.get("/api/audit-logs?action=PAYSLIP_REGENERATED", headers=auth).json()
    assert any(log["entity_id"] == str(issued_payslip["id"]) for log in logs)


def test_moving_a_payslip_to_the_current_template_is_deliberate(
    client, auth, template, issued_payslip
):
    """Re-templating happens only when explicitly asked for."""
    successor = client.post(
        f"/api/payslip-templates/{template['id']}/versions",
        headers=auth,
        json={"name": "Restaurant Staff Payslip", "effective_from": "2029-01-01"},
    ).json()

    unchanged = client.post(f"/api/payslips/{issued_payslip['id']}/regenerate", headers=auth)
    assert unchanged.json()["template_version"] == issued_payslip["template_version"]

    moved = client.post(
        f"/api/payslips/{issued_payslip['id']}/regenerate?use_current_template=true", headers=auth
    )
    assert moved.json()["template_version"] == successor["version"]


def test_payslip_figures_come_from_payroll_not_the_template(client, auth, issued_payslip):
    """Presentation and calculation stay separate."""
    snapshot = issued_payslip["snapshot"]
    net = Decimal(snapshot["totals"]["net"])
    gross = Decimal(snapshot["totals"]["gross"])
    deductions = Decimal(snapshot["totals"]["deductions"])
    assert net == gross - deductions
