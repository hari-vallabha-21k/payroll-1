"""Schema migrations, including the upgrade path for pre-Alembic databases."""

# ruff: noqa: E501  (the legacy-schema SQL below reads better on single lines)

import sqlite3
from datetime import date, time

import pytest
from sqlalchemy import create_engine, inspect, text

from backend.app import migrate


@pytest.fixture
def legacy_database(tmp_path):
    """A database shaped like one created by the old create_all startup.

    Only the columns the migration touches are modelled, which is enough to
    prove it stamps rather than rebuilds, and backfills correctly.
    """
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE tenants (id INTEGER PRIMARY KEY, code TEXT, name TEXT,
            timezone TEXT, currency TEXT, is_active BOOLEAN, created_at DATETIME);
        CREATE TABLE users (id INTEGER PRIMARY KEY, tenant_id INTEGER, email TEXT,
            full_name TEXT, password_hash TEXT, role TEXT, employee_id INTEGER,
            is_active BOOLEAN, created_at DATETIME);
        CREATE TABLE employees (id INTEGER PRIMARY KEY, tenant_id INTEGER,
            employee_code TEXT, first_name TEXT, last_name TEXT, date_of_joining DATE,
            employment_type TEXT, status TEXT, created_at DATETIME);
        CREATE TABLE webauthn_credentials (id INTEGER PRIMARY KEY, tenant_id INTEGER,
            employee_id INTEGER, credential_id TEXT, public_key BLOB, sign_count INTEGER,
            credential_type TEXT, transports TEXT, device_label TEXT, is_active BOOLEAN,
            created_at DATETIME, last_used_at DATETIME);
        CREATE TABLE payroll_runs (id INTEGER PRIMARY KEY, tenant_id INTEGER,
            period_year INTEGER, period_month INTEGER, status TEXT, gross_total NUMERIC,
            deduction_total NUMERIC, net_total NUMERIC, calculated_at DATETIME,
            approved_by_user_id INTEGER, approved_at DATETIME, created_at DATETIME);
        CREATE TABLE payslips (id INTEGER PRIMARY KEY, tenant_id INTEGER, run_id INTEGER,
            employee_id INTEGER, payslip_number TEXT, snapshot_json TEXT,
            generated_at DATETIME);

        INSERT INTO tenants VALUES (1, 'REST001', 'ABC Restaurant', 'Asia/Kolkata', 'INR', 1, '2026-01-01 00:00:00');
        INSERT INTO employees VALUES (1, 1, 'EMP001', 'Rahul', 'Sharma', '2026-08-01', 'FULL_TIME', 'ACTIVE', '2026-08-01 00:00:00');
        INSERT INTO employees VALUES (2, 1, 'EMP002', 'Priya', 'Nair', '2026-08-01', 'FULL_TIME', 'ACTIVE', '2026-08-01 00:00:00');
        INSERT INTO webauthn_credentials
            (id, tenant_id, employee_id, credential_id, public_key, sign_count, credential_type,
             device_label, is_active, created_at)
            VALUES (1, 1, 1, 'live-credential', X'00', 4, 'public-key', 'Rahul phone', 1, '2026-08-05 09:00:00');
        INSERT INTO webauthn_credentials
            (id, tenant_id, employee_id, credential_id, public_key, sign_count, credential_type,
             is_active, created_at)
            VALUES (2, 1, 2, 'old-credential', X'00', 1, 'public-key', 0, '2026-08-06 09:00:00');
        """)
    connection.commit()
    connection.close()
    return path


def test_legacy_database_is_stamped_not_rebuilt(legacy_database):
    engine = create_engine(f"sqlite:///{legacy_database}")
    assert migrate.has_legacy_tables(engine) is True

    revision = migrate.upgrade_database(engine)
    assert revision == "0002_biometric_payroll_config"

    with engine.connect() as connection:
        # Existing rows survived.
        assert connection.execute(text("SELECT COUNT(*) FROM employees")).scalar() == 2
        assert (
            connection.execute(text("SELECT first_name FROM employees WHERE id = 1")).scalar()
            == "Rahul"
        )
        # New tables exist.
        tables = set(inspect(engine).get_table_names())
        assert {"payroll_rules", "payroll_rule_sets", "payslip_templates"} <= tables


def test_backfill_keeps_existing_users_able_to_check_in(legacy_database):
    """Someone already checking in must not be locked out by the upgrade."""
    engine = create_engine(f"sqlite:///{legacy_database}")
    migrate.upgrade_database(engine)

    with engine.connect() as connection:
        rows = dict(
            connection.execute(text("SELECT employee_code, biometric_status FROM employees")).all()
        )
        assert rows["EMP001"] == "VERIFIED"  # had an active credential
        assert rows["EMP002"] == "NOT_REGISTERED"  # only a revoked one

        credentials = dict(
            connection.execute(text("SELECT credential_id, status FROM webauthn_credentials")).all()
        )
        assert credentials["live-credential"] == "ACTIVE"
        assert credentials["old-credential"] == "REVOKED"


def test_upgrade_is_idempotent(legacy_database):
    engine = create_engine(f"sqlite:///{legacy_database}")
    migrate.upgrade_database(engine)
    assert migrate.has_legacy_tables(engine) is False
    assert migrate.upgrade_database(engine) == "0002_biometric_payroll_config"


def test_fresh_database_is_built_from_migrations(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    migrate.upgrade_database(engine)

    tables = set(inspect(engine).get_table_names())
    for expected in (
        "employees",
        "attendance_events",
        "daily_attendance",
        "payroll_runs",
        "payroll_rules",
        "payslip_templates",
        "audit_logs",
    ):
        assert expected in tables


def test_migrated_schema_matches_the_models(tmp_path):
    """The migrations and the ORM must not drift apart."""
    from backend.app.db import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'compare.db'}")
    migrate.upgrade_database(engine)
    migrated = inspect(engine)

    for table_name, table in Base.metadata.tables.items():
        assert table_name in migrated.get_table_names(), f"{table_name} missing from migrations"
        migrated_columns = {c["name"] for c in migrated.get_columns(table_name)}
        model_columns = set(table.columns.keys())
        assert model_columns == migrated_columns, f"{table_name} columns differ"


def test_seed_runs_on_a_migrated_database(tmp_path):
    from sqlalchemy.orm import sessionmaker

    from backend.app.models import Employee
    from backend.app.seed import seed

    engine = create_engine(f"sqlite:///{tmp_path / 'seeded.db'}")
    migrate.upgrade_database(engine)
    with sessionmaker(bind=engine)() as session:
        tenant = seed(session)
        assert tenant.code == "REST001"
        employee = session.query(Employee).filter_by(employee_code="EMP001").one()
        # A newly seeded employee is active but not yet biometrically enrolled.
        assert employee.status.value == "ACTIVE"
        assert employee.biometric_status.value == "NOT_REGISTERED"
        assert isinstance(employee.date_of_joining, date)


def test_shift_times_survive_the_round_trip(tmp_path):
    from sqlalchemy.orm import sessionmaker

    from backend.app.models import Shift

    engine = create_engine(f"sqlite:///{tmp_path / 'shifts.db'}")
    migrate.upgrade_database(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(Shift(tenant_id=1, name="Night", start_time=time(22, 0), end_time=time(6, 0)))
        session.commit()
        shift = session.query(Shift).one()
        assert shift.start_time == time(22, 0)
