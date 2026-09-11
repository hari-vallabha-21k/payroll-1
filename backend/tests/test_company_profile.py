"""Company profile and the statutory identifiers a payslip prints."""

import base64
import io
import json

import pytest

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# --- employee statutory fields ---------------------------------------------
def test_employee_accepts_and_returns_statutory_fields(client, auth):
    created = client.post(
        "/api/employees",
        headers=auth,
        json={
            "employee_code": "EMPSTAT1",
            "first_name": "Statutory",
            "last_name": "Fields",
            "date_of_joining": "2026-01-01",
            "branch": "Marine Drive",
            "pan": "ABCDE1234F",
            "pf_number": "MH/12345/678",
            "esi_number": "3100123456",
            "bank_account": "XXXX4321",
            "bank_ifsc": "HDFC0001234",
            "uan": "100200300400",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["pan"] == "ABCDE1234F"
    assert body["pf_number"] == "MH/12345/678"
    assert body["esi_number"] == "3100123456"
    assert body["branch"] == "Marine Drive"

    fetched = client.get(f"/api/employees/{body['id']}", headers=auth).json()
    assert fetched["uan"] == "100200300400"
    assert fetched["bank_ifsc"] == "HDFC0001234"


def test_statutory_fields_are_optional(client, auth):
    """Restaurant staff may fall outside PF/ESI entirely."""
    created = client.post(
        "/api/employees",
        headers=auth,
        json={
            "employee_code": "EMPSTAT2",
            "first_name": "No",
            "last_name": "Statutory",
            "date_of_joining": "2026-01-01",
        },
    )
    assert created.status_code == 201
    assert created.json()["pan"] is None
    assert created.json()["esi_number"] is None


def test_statutory_fields_reach_the_payslip(client, auth):
    employee = client.post(
        "/api/employees",
        headers=auth,
        json={
            "employee_code": "EMPSTAT3",
            "first_name": "Printed",
            "last_name": "OnSlip",
            "date_of_joining": "2026-01-01",
            "pan": "ZZZZZ9999Z",
            "pf_number": "PF-PRINTED",
            "esi_number": "ESI-PRINTED",
            "branch": "Bandra",
        },
    ).json()

    period = {"period_year": 2031, "period_month": 3}
    run = client.post("/api/payroll/calculate", headers=auth, json=period).json()
    client.post(f"/api/payroll/{run['id']}/approve", headers=auth)
    client.post(f"/api/payroll/{run['id']}/payslips", headers=auth)

    payslips = client.get(
        f"/api/payslips?run_id={run['id']}&employee_id={employee['id']}", headers=auth
    ).json()
    snapshot = payslips[0]["snapshot"]["employee"]
    assert snapshot["pan"] == "ZZZZZ9999Z"
    assert snapshot["pf_number"] == "PF-PRINTED"
    assert snapshot["branch"] == "Bandra"

    # And a template that prints them resolves the placeholders.
    from backend.app.services import payslip_template as template_service

    template = json.loads(json.dumps(template_service.DEFAULT_TEMPLATE))
    for section in template["sections"]:
        if section["type"] == "employee_details":
            section["fields"] += [
                {"label": "PAN", "value": "{{pan}}"},
                {"label": "PF Number", "value": "{{pf_number}}"},
                {"label": "Branch", "value": "{{branch}}"},
            ]
    html = template_service.render_html(template, payslips[0]["snapshot"])
    assert "ZZZZZ9999Z" in html
    assert "PF-PRINTED" in html
    assert "Bandra" in html


# --- company profile --------------------------------------------------------
def test_company_profile_round_trips(client, auth):
    updated = client.put(
        "/api/settings/company",
        headers=auth,
        json={
            "name": "ABC Restaurant Pvt Ltd",
            "address": "12 Marine Drive, Mumbai 400020",
            "phone": "+91 22 1234 5678",
            "email": "payroll@abcrestaurant.in",
            "gst_number": "27ABCDE1234F1Z5",
            "registration_number": "U55101MH2026PTC000001",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["gst_number"] == "27ABCDE1234F1Z5"

    fetched = client.get("/api/settings/company", headers=auth).json()
    assert fetched["name"] == "ABC Restaurant Pvt Ltd"
    assert fetched["address"] == "12 Marine Drive, Mumbai 400020"


def test_company_details_appear_in_the_template_preview(client, auth):
    client.put(
        "/api/settings/company",
        headers=auth,
        json={"name": "Preview Kitchen", "gst_number": "29PREVIEW1234Z"},
    )
    preview = client.post("/api/payslip-templates/preview", headers=auth, json={})
    assert preview.status_code == 200
    assert "Preview Kitchen" in preview.text
    assert "29PREVIEW1234Z" in preview.text


def test_logo_upload_and_removal(client, auth):
    uploaded = client.post(
        "/api/settings/company/logo",
        headers=auth,
        files={"file": ("logo.png", io.BytesIO(PNG_1PX), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["logo"].startswith("data:image/png;base64,")

    # Inlined, so rendering an old payslip never depends on an external fetch.
    preview = client.post("/api/payslip-templates/preview", headers=auth, json={})
    assert "data:image/png;base64," in preview.text

    removed = client.delete("/api/settings/company/logo", headers=auth)
    assert removed.json()["logo"] == ""


@pytest.mark.parametrize(
    "filename,content_type",
    [("logo.gif", "image/gif"), ("payload.exe", "application/octet-stream")],
)
def test_unsupported_logo_types_are_refused(client, auth, filename, content_type):
    response = client.post(
        "/api/settings/company/logo",
        headers=auth,
        files={"file": (filename, io.BytesIO(b"x" * 10), content_type)},
    )
    assert response.status_code == 400


def test_oversized_logo_is_refused(client, auth):
    response = client.post(
        "/api/settings/company/logo",
        headers=auth,
        files={"file": ("big.png", io.BytesIO(b"x" * (600 * 1024)), "image/png")},
    )
    assert response.status_code == 413


def test_only_an_admin_may_change_the_company_profile(client, auth):
    client.post(
        "/api/auth/users",
        headers=auth,
        json={
            "email": "hronly@abcrestaurant.in",
            "full_name": "HR Only",
            "password": "hronly123456",
            "role": "HR",
        },
    )
    token = client.post(
        "/api/auth/login", json={"email": "hronly@abcrestaurant.in", "password": "hronly123456"}
    ).json()["access_token"]
    hr = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/settings/company", headers=hr).status_code == 200  # may read
    assert (
        client.put("/api/settings/company", headers=hr, json={"name": "Renamed"}).status_code == 403
    )


def test_company_changes_do_not_alter_issued_payslips(client, auth):
    client.put("/api/settings/company", headers=auth, json={"name": "Original Name Ltd"})

    period = {"period_year": 2032, "period_month": 4}
    run = client.post("/api/payroll/calculate", headers=auth, json=period).json()
    client.post(f"/api/payroll/{run['id']}/approve", headers=auth)
    client.post(f"/api/payroll/{run['id']}/payslips", headers=auth)
    payslip = client.get(f"/api/payslips?run_id={run['id']}", headers=auth).json()[0]

    before = client.get(f"/api/payslips/{payslip['id']}/html", headers=auth).text
    assert "Original Name Ltd" in before

    client.put("/api/settings/company", headers=auth, json={"name": "Completely Renamed Ltd"})
    after = client.get(f"/api/payslips/{payslip['id']}/html", headers=auth).text

    assert after == before
    assert "Completely Renamed Ltd" not in after


def test_generic_setting_route_still_works(client, auth):
    """The /company routes must not shadow the generic key/value settings.

    Regression guard: /settings/{key} was declared first and swallowed
    /settings/company, so the profile endpoint returned a validation error.
    """
    response = client.put("/api/settings/weekly_off_days?value=6", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json() == {"weekly_off_days": "6"}
    assert client.get("/api/settings", headers=auth).json()["weekly_off_days"] == "6"

    # And the company route is still reached rather than treated as a key.
    assert client.get("/api/settings/company", headers=auth).status_code == 200


def test_company_profile_changes_are_audited(client, auth):
    client.put("/api/settings/company", headers=auth, json={"name": "Audited Restaurant"})
    logs = client.get("/api/audit-logs?action=COMPANY_PROFILE_UPDATED", headers=auth).json()
    assert logs
