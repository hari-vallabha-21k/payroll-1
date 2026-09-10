import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import models  # noqa: F401  (import registers the tables)
from .config import get_settings
from .db import Base, SessionLocal, engine
from .ratelimit import RateLimitMiddleware
from .routers import (
    attendance,
    auth,
    devices,
    employees,
    leave,
    payroll,
    reports,
    salary,
    shifts,
    tenant_settings,
    webauthn,
)
from .seed import seed

settings = get_settings()
logger = logging.getLogger("payroll")

# frontend/ sits next to backend/ in the repo. FRONTEND_DIR in the environment
# overrides it, for deployments that put the static files somewhere else.
FRONTEND_DIR = (
    Path(settings.frontend_dir).expanduser().resolve()
    if settings.frontend_dir
    else Path(__file__).resolve().parents[2] / "frontend"
)
PAGES = {
    "/": "admin.html",
    "/admin": "admin.html",
    "/attendance": "attendance.html",
    "/enroll": "enroll.html",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    if settings.seed_demo_data:
        with SessionLocal() as db:
            seed(db)
    if missing_pages():
        logger.warning(
            "Frontend files not found under %s (missing: %s). The API works, but "
            "/admin and /attendance will explain the problem instead of loading. "
            "Check out the full repository, or point FRONTEND_DIR at the folder.",
            FRONTEND_DIR,
            ", ".join(missing_pages()),
        )
    yield


app = FastAPI(
    title="Payroll & Attendance Management System",
    version="1.0.0",
    description=(
        "Restaurant payroll and attendance. Attendance is captured as standardized "
        "events, whether they come from WebAuthn on a phone or a biometric machine."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.webauthn_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware, limit=30)

for router in (
    auth.router,
    employees.router,
    shifts.router,
    webauthn.router,
    attendance.router,
    leave.router,
    salary.router,
    payroll.router,
    devices.router,
    reports.router,
    tenant_settings.router,
):
    app.include_router(router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def missing_pages() -> list[str]:
    """Page files the frontend needs but that are not on disk."""
    return sorted({name for name in PAGES.values() if not (FRONTEND_DIR / name).is_file()})


@app.get("/api/health")
def health():
    absent = missing_pages()
    return {
        "status": "ok",
        "frontend_dir": str(FRONTEND_DIR),
        "frontend_ready": not absent,
        "missing_files": absent,
    }


def serve_page(filename: str):
    """Serve a frontend page, or say exactly what is missing and where."""
    path = FRONTEND_DIR / filename
    if path.is_file():
        return FileResponse(path)
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"Frontend file '{filename}' was not found. Looked in: {FRONTEND_DIR}. "
        "The frontend/ folder ships with the repository - check it out alongside "
        "backend/, or set FRONTEND_DIR to where the files live. The API itself is "
        "running; see /docs.",
    )


# Registered unconditionally: a missing frontend should explain itself rather
# than fall through to a bare 404.
@app.get("/", include_in_schema=False)
def root():
    if (FRONTEND_DIR / PAGES["/admin"]).is_file():
        return RedirectResponse("/admin")
    return serve_page(PAGES["/admin"])


@app.get("/admin", include_in_schema=False)
def admin_page():
    return serve_page(PAGES["/admin"])


@app.get("/attendance", include_in_schema=False)
def attendance_page():
    return serve_page(PAGES["/attendance"])


@app.get("/enroll", include_in_schema=False)
def enroll_page():
    return serve_page(PAGES["/enroll"])


@app.get("/static/{asset:path}", include_in_schema=False)
def static_asset(asset: str):
    """Static files, served without mounting so a missing folder still answers.

    The path is resolved and confined to FRONTEND_DIR so it cannot escape.
    """
    candidate = (FRONTEND_DIR / asset).resolve()
    if not str(candidate).startswith(str(FRONTEND_DIR.resolve())):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not candidate.is_file():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Static asset '{asset}' not found under {FRONTEND_DIR}.",
        )
    return FileResponse(candidate)
