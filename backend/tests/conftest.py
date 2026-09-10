import os
import tempfile
from pathlib import Path

import pytest

TMP_DIR = Path(tempfile.mkdtemp(prefix="payroll-test-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TMP_DIR / 'test.db'}")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SEED_DEMO_DATA", "true")
LIVE_PORT = 8765
os.environ.setdefault("WEBAUTHN_RP_ID", "localhost")
os.environ.setdefault("WEBAUTHN_ORIGIN", f"http://localhost:{LIVE_PORT}")

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.db import Base, SessionLocal, engine  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.migrate import upgrade_database  # noqa: E402
from backend.app.seed import seed  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(bind=engine)
    # Build the schema through the migrations, so every run exercises them.
    upgrade_database(engine)
    with SessionLocal() as db:
        seed(db)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_token(client):
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@abcrestaurant.in", "password": "admin12345"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def auth(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}
