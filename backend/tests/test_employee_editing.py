"""Editing an existing employee, and payslip rows that hide when empty."""

import itertools
import json

import pytest

from backend.app.services import payslip_template as template_service

# One employee per test: employee codes are unique within a tenant.
_codes = itertools.count(1)


@pytest.fixture
def employee(client, auth):
    created = client.post(
        "/api/employees",
        headers=auth,
        json={
            "employee_code": f"EMPEDIT{next(_codes)}",
            "first_name": "Before",
            "last_name": "Edit",
            "date_of_joining": "2026-01-01",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_statutory_fields_can_be_set_after_creation(client, auth, employee):
    """They belong on an edit, not only on the create form."""
    updated = client.put(
        f"/api/employees/{employee['id']}",
        headers=auth,
        json={
            "first_name": "After",
            "pan": "PQRST5678K",
            "pf_number": "MH/99999/111",
            "esi_number": "3100999888",
            "uan": "999888777666",
            "branch": "Colaba",
            "bank_account": "XXXX9999",
            "bank_ifsc": "ICIC0005678",
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["first_name"] == "After"
    assert body["pan"] == "PQRST5678K"
    assert body["uan"] == "999888777666"
    assert body["branch"] == "Colaba"

    fetched = client.get(f"/api/employees/{employee['id']}", headers=auth).json()
    assert fetched["esi_number"] == "3100999888"
    assert fetched["bank_ifsc"] == "ICIC0005678"


def test_department_and_designation_can_be_assigned(client, auth, employee):
    department = client.post("/api/departments", headers=auth, json={"name": "Bar"}).json()
    designation = client.post("/api/designations", headers=auth, json={"name": "Bartender"}).json()

    updated = client.put(
        f"/api/employees/{employee['id']}",
        headers=auth,
        json={"department_id": department["id"], "designation_id": designation["id"]},
    )
    assert updated.status_code == 200
    assert updated.json()["department"]["name"] == "Bar"
    assert updated.json()["designation"]["name"] == "Bartender"


def test_editing_is_audited_with_the_change(client, auth, employee):
    client.put(f"/api/employees/{employee['id']}", headers=auth, json={"pan": "AUDIT1234A"})
    logs = client.get("/api/audit-logs?action=UPDATE_EMPLOYEE", headers=auth).json()
    entry = next(log for log in logs if log["entity_id"] == str(employee["id"]))
    assert "AUDIT1234A" in entry["detail"]


def test_employment_status_can_be_changed_without_touching_biometrics(client, auth, employee):
    updated = client.put(
        f"/api/employees/{employee['id']}", headers=auth, json={"status": "ONBOARDING"}
    )
    assert updated.json()["status"] == "ONBOARDING"
    # Biometric status is a separate lifecycle and must be unaffected.
    assert updated.json()["biometric_status"] == "NOT_REGISTERED"


def test_employee_role_cannot_edit_others(client, auth, employee):
    client.post(
        "/api/auth/users",
        headers=auth,
        json={
            "email": "editor@abcrestaurant.in",
            "full_name": "Editor",
            "password": "editor123456",
            "role": "EMPLOYEE",
        },
    )
    token = client.post(
        "/api/auth/login", json={"email": "editor@abcrestaurant.in", "password": "editor123456"}
    ).json()["access_token"]
    response = client.put(
        f"/api/employees/{employee['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"pan": "SNEAK1234S"},
    )
    assert response.status_code == 403


# --- payslip rows that hide when empty --------------------------------------
def snapshot_for(**employee_fields) -> dict:
    return {
        "tenant": {"name": "ABC Restaurant"},
        "period": {"label": "August 2026"},
        "employee": {"code": "EMP001", "name": "Rahul Kumar", **employee_fields},
        "attendance": {"working_days": 31, "payable_days": 31, "overtime_minutes": 0},
        "earnings": [{"label": "Basic Salary", "amount": "20000.00"}],
        "deductions": [],
        "totals": {"gross": "20000.00", "deductions": "0.00", "net": "20000.00"},
    }


def test_default_template_prints_statutory_fields_when_present():
    html = template_service.render_html(
        template_service.DEFAULT_TEMPLATE,
        snapshot_for(pan="ABCDE1234F", pf_number="MH/12345/678", branch="Marine Drive"),
    )
    assert "ABCDE1234F" in html
    assert "MH/12345/678" in html
    assert "Marine Drive" in html


def test_empty_statutory_rows_are_omitted_entirely():
    """No hollow 'ESI Number: -' row for staff outside ESI."""
    html = template_service.render_html(template_service.DEFAULT_TEMPLATE, snapshot_for())
    assert "ESI Number" not in html
    assert "PF Number" not in html
    assert "UAN" not in html
    # The rows that always apply are still there.
    assert "Employee ID" in html
    assert "Basic Salary" in html


def test_hide_when_empty_is_opt_in():
    """A template that insists on a row keeps it, dash and all."""
    template = json.loads(json.dumps(template_service.DEFAULT_TEMPLATE))
    for section in template["sections"]:
        if section["type"] == "employee_details":
            for field in section["fields"]:
                field.pop("hide_when_empty", None)
    html = template_service.render_html(template, snapshot_for())
    assert "ESI Number" in html


def test_pdf_honours_the_same_rule(client, auth):
    """The PDF must not print rows the HTML preview hides."""
    from backend.app.models import Payslip
    from backend.app.services import payslip as payslip_service

    with_fields = Payslip(
        tenant_id=1,
        run_id=0,
        employee_id=0,
        payslip_number="WITH",
        snapshot_json=json.dumps(snapshot_for(pan="ABCDE1234F")),
        template_snapshot_json=template_service.dumps(template_service.DEFAULT_TEMPLATE),
    )
    without_fields = Payslip(
        tenant_id=1,
        run_id=0,
        employee_id=0,
        payslip_number="WITHOUT",
        snapshot_json=json.dumps(snapshot_for()),
        template_snapshot_json=template_service.dumps(template_service.DEFAULT_TEMPLATE),
    )

    with_pdf = payslip_service.render_pdf(with_fields)
    without_pdf = payslip_service.render_pdf(without_fields)
    assert with_pdf.startswith(b"%PDF") and without_pdf.startswith(b"%PDF")
    # The PAN row makes the populated payslip the larger document.
    assert len(with_pdf) > len(without_pdf)
