from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
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
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    if settings.seed_demo_data:
        with SessionLocal() as db:
            seed(db)
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


@app.get("/api/health")
def health():
    return {"status": "ok"}


if FRONTEND_DIR.is_dir():

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/admin")

    @app.get("/admin", include_in_schema=False)
    def admin_page():
        return FileResponse(FRONTEND_DIR / "admin.html")

    @app.get("/attendance", include_in_schema=False)
    def attendance_page():
        return FileResponse(FRONTEND_DIR / "attendance.html")

    @app.get("/enroll", include_in_schema=False)
    def enroll_page():
        return FileResponse(FRONTEND_DIR / "enroll.html")

    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
