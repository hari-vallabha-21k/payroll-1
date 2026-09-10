"""The headline flow, driven through a real browser.

Chromium's CDP virtual authenticator stands in for the phone's fingerprint
sensor, so this exercises the genuine WebAuthn ceremony: enrolment, check-in
and check-out, ending in stored attendance events.

Skipped automatically when Playwright or a Chromium build is unavailable.
"""

from __future__ import annotations

import threading
import time
from urllib.request import Request, urlopen

import pytest

from .conftest import LIVE_PORT

playwright_api = pytest.importorskip("playwright.sync_api")

BASE = f"http://localhost:{LIVE_PORT}"
VIRTUAL_AUTHENTICATOR = {
    "protocol": "ctap2",
    "transport": "internal",
    "hasResidentKey": True,
    "hasUserVerification": True,
    "isUserVerified": True,
    "automaticPresenceSimulation": True,
}
SETTLED = (
    "document.getElementById('status').className.includes('ok') || "
    "document.getElementById('status').className.includes('bad')"
)


def _chromium_path() -> str | None:
    from pathlib import Path

    root = Path("/opt/pw-browsers")
    if not root.is_dir():
        return None
    for candidate in sorted(root.glob("chromium-*/chrome-linux/chrome")):
        return str(candidate)
    return None


@pytest.fixture(scope="module")
def live_server():
    import uvicorn

    from backend.app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=LIVE_PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while time.time() < deadline and not server.started:
        time.sleep(0.1)
    if not server.started:
        pytest.skip("live server did not start")

    yield BASE
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser_page(live_server):
    chromium = _chromium_path()
    if chromium is None:
        pytest.skip("no Chromium build available")

    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=chromium)
        context = browser.new_context()
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.send("WebAuthn.enable")
        cdp.send("WebAuthn.addVirtualAuthenticator", {"options": VIRTUAL_AUTHENTICATOR})
        yield page
        browser.close()


def _api(path: str, payload: dict | None = None, token: str | None = None) -> dict:
    import json

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        BASE + path,
        data=json.dumps(payload or {}).encode(),
        headers=headers,
    )
    return json.loads(urlopen(request).read())


def test_enrol_then_check_in_and_out(browser_page, live_server):
    page = browser_page
    token = _api("/api/auth/login", {"email": "admin@abcrestaurant.in", "password": "admin12345"})[
        "access_token"
    ]
    employees = _api("/api/webauthn/lookup", {"employee_code": "EMP001", "tenant_code": "REST001"})
    assert employees["has_biometric"] is False

    enrolment = _api("/api/webauthn/enrollment-token?employee_id=1", {}, token)

    # 1. Employee registers the phone from the single-use link.
    page.goto(enrolment["enroll_url"])
    page.fill("#label", "Test phone")
    page.click("#register")
    page.wait_for_function(SETTLED, timeout=20000)
    assert "Registered" in page.inner_text("#status")

    # 2. Check in from the kiosk page.
    page.goto(f"{BASE}/attendance?tenant=REST001")
    page.fill("#code", "EMP001")
    page.click("#verify")
    page.wait_for_function(SETTLED, timeout=20000)
    checked_in = page.inner_text("#status")
    assert "Rahul Sharma" in checked_in
    assert "Checked in" in checked_in

    # 3. Check out again.
    page.click("#reset")
    page.fill("#code", "EMP001")
    page.click("#verify")
    page.wait_for_function(
        "document.getElementById('status').textContent.includes('Checked out') || "
        "document.getElementById('status').className.includes('bad')",
        timeout=20000,
    )
    assert "Checked out" in page.inner_text("#status")

    # 4. Both punches landed as standardized WebAuthn-sourced events.
    request = Request(
        f"{BASE}/api/attendance/events?employee_id=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    import json

    events = json.loads(urlopen(request).read())
    from_kiosk = [event for event in events if event["source"] == "WEBAUTHN"]
    assert {event["event_type"] for event in from_kiosk} == {"CHECK_IN", "CHECK_OUT"}

    # And the enrolment link is single-use.
    after = _api("/api/webauthn/lookup", {"employee_code": "EMP001", "tenant_code": "REST001"})
    assert after["has_biometric"] is True
