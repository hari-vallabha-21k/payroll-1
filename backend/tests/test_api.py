"""End-to-end API flow: employee -> device punches -> payroll -> payslip."""

from datetime import date

import pytest

PERIOD = {"period_year": 2027, "period_month": 3}


@pytest.fixture(scope="module")
def kiosk_employee_code():
    return "EMP900"


def test_health_reports_frontend_state(client):
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    # The checked-out repo ships the frontend, so health must confirm it is there.
    assert health["frontend_ready"] is True, health
    assert health["missing_files"] == []


def test_login_rejects_bad_credentials(client):
    response = client.post(
        "/api/auth/login", json={"email": "admin@abcrestaurant.in", "password": "wrong"}
    )
    assert response.status_code == 401


def test_endpoints_require_authentication(client):
    assert client.get("/api/employees").status_code == 401
    assert client.get("/api/payroll").status_code == 401


def test_employee_cannot_reach_payroll_admin_actions(client, auth):
    created = client.post(
        "/api/auth/users",
        headers=auth,
        json={
            "email": "waiter@abcrestaurant.in",
            "full_name": "Waiter User",
            "password": "waiter12345",
            "role": "EMPLOYEE",
        },
    )
    assert created.status_code == 201

    token = client.post(
        "/api/auth/login", json={"email": "waiter@abcrestaurant.in", "password": "waiter12345"}
    ).json()["access_token"]
    employee_auth = {"Authorization": f"Bearer {token}"}

    assert (
        client.post("/api/payroll/calculate", headers=employee_auth, json=PERIOD).status_code == 403
    )
    assert (
        client.post(
            "/api/employees",
            headers=employee_auth,
            json={"employee_code": "EMP999", "first_name": "Nope", "date_of_joining": "2027-01-01"},
        ).status_code
        == 403
    )


def test_full_flow(client, auth, kiosk_employee_code):
    shifts = client.get("/api/shifts", headers=auth).json()
    morning = next(s for s in shifts if s["name"] == "Morning")

    employee = client.post(
        "/api/employees",
        headers=auth,
        json={
            "employee_code": kiosk_employee_code,
            "first_name": "Device",
            "last_name": "Tester",
            "date_of_joining": "2027-03-01",
            "shift_id": morning["id"],
        },
    )
    assert employee.status_code == 201, employee.text
    employee_id = employee.json()["id"]
    assert employee.json()["has_biometric"] is False

    structures = client.get("/api/salary-structures", headers=auth).json()
    assigned = client.post(
        f"/api/employees/{employee_id}/salary",
        headers=auth,
        json={"structure_id": structures[0]["id"], "effective_from": "2027-03-01"},
    )
    assert assigned.status_code == 201, assigned.text

    # A device connector posts punches with the same shape WebAuthn produces.
    device = client.post(
        "/api/devices",
        headers=auth,
        json={"device_code": "BIO900", "name": "Main Entrance", "brand": "ZKTeco", "port": 4370},
    )
    assert device.status_code == 201, device.text
    device_id = device.json()["device"]["id"]
    api_key = device.json()["api_key"]

    mapped = client.post(
        f"/api/devices/{device_id}/enrollments",
        headers=auth,
        json={"employee_id": employee_id, "device_user_id": "17"},
    )
    assert mapped.status_code == 201

    events = []
    for day in range(1, 32):
        events.append(
            {
                "device_user_id": "17",
                "event_type": "CHECK_IN",
                "event_time": f"2027-03-{day:02d}T09:00:00",
                "external_ref": f"BIO900-{day}-IN",
            }
        )
        events.append(
            {
                "device_user_id": "17",
                "event_type": "CHECK_OUT",
                "event_time": f"2027-03-{day:02d}T18:00:00",
                "external_ref": f"BIO900-{day}-OUT",
            }
        )

    unauthorized = client.post(f"/api/devices/{device_id}/sync", json={"events": events})
    assert unauthorized.status_code == 401

    sync = client.post(
        f"/api/devices/{device_id}/sync",
        headers={"X-Device-Key": api_key},
        json={"events": events},
    )
    assert sync.status_code == 200, sync.text
    assert sync.json()["accepted"] == 62
    assert sync.json()["failed"] == 0

    # Re-posting the same batch must not double-punch.
    replay = client.post(
        f"/api/devices/{device_id}/sync",
        headers={"X-Device-Key": api_key},
        json={"events": events},
    )
    assert replay.json()["accepted"] == 0
    assert replay.json()["duplicates"] == 62

    daily = client.get(
        f"/api/attendance/{employee_id}?start=2027-03-01&end=2027-03-31", headers=auth
    ).json()
    assert len(daily) == 31
    assert all(record["status"] == "PRESENT" for record in daily)
    assert daily[0]["worked_minutes"] == 540

    run = client.post("/api/payroll/calculate", headers=auth, json=PERIOD)
    assert run.status_code == 200, run.text
    run_id = run.json()["id"]
    assert run.json()["status"] == "CALCULATED"
    line = next(i for i in run.json()["items"] if i["employee_id"] == employee_id)
    assert line["payable_days"] == 31.0
    assert line["lop_days"] == 0.0
    assert line["gross"] == "30000.00"
    assert line["net"] == "27800.00"

    # Payslips only come out of approved payroll.
    assert client.post(f"/api/payroll/{run_id}/payslips", headers=auth).status_code == 409

    approved = client.post(f"/api/payroll/{run_id}/approve", headers=auth)
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"

    # An approved run is locked against silent recalculation.
    assert client.post("/api/payroll/calculate", headers=auth, json=PERIOD).status_code == 409

    slips = client.post(f"/api/payroll/{run_id}/payslips", headers=auth)
    assert slips.status_code == 200, slips.text
    assert slips.json()["created"] >= 1

    payslips = client.get(f"/api/payslips?run_id={run_id}&employee_id={employee_id}", headers=auth)
    payslip = payslips.json()[0]
    assert payslip["snapshot"]["totals"]["net"] == "27800.00"
    assert payslip["snapshot"]["employee"]["code"] == kiosk_employee_code

    pdf = client.get(f"/api/payslips/{payslip['id']}/pdf", headers=auth)
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")

    # Reopening is the only authorised way back out.
    reopened = client.post(f"/api/payroll/{run_id}/reopen?reason=correction", headers=auth)
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "UNDER_REVIEW"

    logs = client.get("/api/audit-logs?action=APPROVE_PAYROLL", headers=auth).json()
    assert any(log["entity_id"] == str(run_id) for log in logs)


def test_kiosk_lookup_and_missing_credential(client, auth, kiosk_employee_code):
    lookup = client.post(
        "/api/webauthn/lookup",
        json={"employee_code": kiosk_employee_code, "tenant_code": "REST001"},
    )
    assert lookup.status_code == 200
    assert lookup.json()["has_biometric"] is False
    assert lookup.json()["next_action"] == "CHECK_IN"

    # No credential registered yet -> the ceremony cannot start.
    options = client.post(
        "/api/webauthn/authenticate/options",
        json={"employee_code": kiosk_employee_code, "tenant_code": "REST001"},
    )
    assert options.status_code == 400
    assert "No biometric credential" in options.json()["detail"]

    assert client.post("/api/webauthn/lookup", json={"employee_code": "NOPE"}).status_code == 404


def test_enrollment_token_is_single_use_and_scoped(client, auth):
    employees = client.get("/api/employees", headers=auth).json()
    employee_id = employees[0]["id"]

    issued = client.post(f"/api/webauthn/enrollment-token?employee_id={employee_id}", headers=auth)
    assert issued.status_code == 200, issued.text
    token = issued.json()["token"]
    assert issued.json()["enroll_url"].endswith(token)

    options = client.post("/api/webauthn/register/options", json={"token": token})
    assert options.status_code == 200
    assert options.json()["rp"]["id"] == "localhost"
    assert options.json()["authenticatorSelection"]["userVerification"] == "required"

    assert (
        client.post("/api/webauthn/register/options", json={"token": "made-up"}).status_code == 404
    )


def test_manual_punch_and_correction_are_audited(client, auth, kiosk_employee_code):
    employees = client.get(f"/api/employees?q={kiosk_employee_code}", headers=auth).json()
    employee_id = employees[0]["id"]

    punch = client.post(
        "/api/attendance/punch",
        headers=auth,
        json={
            "employee_code": kiosk_employee_code,
            "event_type": "CHECK_IN",
            "event_time": "2027-04-01T09:15:00",
            "note": "Forgot to punch",
        },
    )
    assert punch.status_code == 201, punch.text

    corrected = client.post(
        f"/api/attendance/{employee_id}/correction",
        headers=auth,
        json={
            "work_date": "2027-04-01",
            "last_out": "2027-04-01T18:00:00",
            "status": "PRESENT",
            "payable_day_fraction": 1.0,
            "reason": "Missed check-out confirmed by manager",
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["is_manual_override"] is True
    assert corrected.json()["status"] == "PRESENT"

    # A correction survives reprocessing until it is cleared.
    client.post("/api/attendance/process?work_date=2027-04-01", headers=auth)
    after = client.get(
        f"/api/attendance/{employee_id}?start=2027-04-01&end=2027-04-01", headers=auth
    ).json()[0]
    assert after["is_manual_override"] is True
    assert after["payable_day_fraction"] == 1.0

    logs = client.get("/api/audit-logs?action=CORRECT_ATTENDANCE", headers=auth).json()
    assert logs


def test_leave_approval_updates_attendance(client, auth, kiosk_employee_code):
    employees = client.get(f"/api/employees?q={kiosk_employee_code}", headers=auth).json()
    employee_id = employees[0]["id"]
    leave_types = client.get("/api/leave-types", headers=auth).json()
    casual = next(t for t in leave_types if t["name"] == "Casual Leave")

    created = client.post(
        "/api/leave-requests",
        headers=auth,
        json={
            "employee_id": employee_id,
            "leave_type_id": casual["id"],
            "start_date": "2027-04-10",
            "end_date": "2027-04-11",
            "reason": "Personal",
        },
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]

    decided = client.post(
        f"/api/leave-requests/{request_id}/decision",
        headers=auth,
        json={"status": "APPROVED", "note": "Approved by manager"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "APPROVED"

    records = client.get(
        f"/api/attendance/{employee_id}?start=2027-04-10&end=2027-04-11", headers=auth
    ).json()
    assert [r["status"] for r in records] == ["ON_LEAVE", "ON_LEAVE"]
    assert all(r["payable_day_fraction"] == 1.0 for r in records)


def test_dashboard_reports_today(client, auth):
    dashboard = client.get(f"/api/dashboard?work_date={date(2027, 3, 2)}", headers=auth).json()
    assert dashboard["present"] >= 1
    assert dashboard["total_employees"] >= 3

    summary = client.get(
        "/api/reports/attendance-summary?start=2027-03-01&end=2027-03-31", headers=auth
    ).json()
    assert any(row["payable_days"] == 31.0 for row in summary["rows"])

    csv_export = client.get(
        "/api/reports/attendance.csv?start=2027-03-01&end=2027-03-02", headers=auth
    )
    assert csv_export.status_code == 200
    assert "employee_code" in csv_export.text
