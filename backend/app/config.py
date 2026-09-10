from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./payroll.db"

    # Where the static frontend lives; defaults to frontend/ beside backend/
    frontend_dir: str | None = None

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "Payroll Attendance"
    webauthn_origin: str = "http://localhost:8000"

    seed_demo_data: bool = True
    default_admin_email: str = "admin@abcrestaurant.in"
    default_admin_password: str = "admin12345"

    # Attendance anti-fraud: ignore a second punch of the same type within N seconds
    duplicate_punch_window_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
