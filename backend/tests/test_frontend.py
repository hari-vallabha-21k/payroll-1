"""The static frontend: served when present, self-explaining when not."""

from pathlib import Path

import pytest


def test_pages_and_assets_are_served(client):
    for path in ("/admin", "/attendance", "/enroll"):
        page = client.get(path)
        assert page.status_code == 200, f"{path} -> {page.status_code}"
        assert "text/html" in page.headers["content-type"]

    for asset in ("/static/admin.js", "/static/styles.css", "/static/webauthn-client.js"):
        response = client.get(asset)
        assert response.status_code == 200, f"{asset} -> {response.status_code}"

    assert client.get("/", follow_redirects=False).status_code in (200, 307)


def test_static_cannot_escape_the_frontend_folder(client):
    assert client.get("/static/../backend/app/config.py").status_code == 404
    assert client.get("/static/..%2F..%2Fbackend%2Fapp%2Fconfig.py").status_code == 404


@pytest.fixture
def absent_frontend(monkeypatch, tmp_path):
    """Point the app at a folder that holds no frontend files."""
    from backend.app import main

    missing = tmp_path / "absent-frontend"
    monkeypatch.setattr(main, "FRONTEND_DIR", missing)
    return missing


def test_missing_frontend_explains_itself(client, absent_frontend):
    """A missing frontend must name the folder it searched, not 404 silently.

    Regression guard: the page routes used to be registered only when the
    folder existed, so a partial checkout answered '/' with a bare Not Found
    and no hint about what was wrong.
    """
    health = client.get("/api/health").json()
    assert health["frontend_ready"] is False
    assert "admin.html" in health["missing_files"]
    assert str(absent_frontend) == health["frontend_dir"]

    for path in ("/", "/admin", "/attendance", "/enroll"):
        response = client.get(path)
        assert response.status_code == 503, f"{path} -> {response.status_code}"
        detail = response.json()["detail"]
        assert "was not found" in detail
        assert absent_frontend.name in detail  # names the folder it searched

    asset = client.get("/static/admin.js")
    assert asset.status_code == 404
    assert absent_frontend.name in asset.json()["detail"]

    # A missing frontend never takes the API down with it.
    assert client.get("/docs").status_code == 200
    assert client.get("/api/employees").status_code == 401


def test_frontend_dir_default_sits_beside_backend():
    from backend.app import main

    expected = Path(main.__file__).resolve().parents[2] / "frontend"
    assert (expected / "admin.html").is_file(), f"frontend/ missing at {expected}"
