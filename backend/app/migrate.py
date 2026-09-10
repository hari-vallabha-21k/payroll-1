"""Schema management.

The application originally created its tables with ``Base.metadata.create_all``.
Databases from that era have the tables but no Alembic version stamp, so they
are stamped at the baseline revision and then upgraded - never re-created,
which would destroy live data.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

logger = logging.getLogger("payroll.migrate")

BASELINE_REVISION = "0001_baseline"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(engine: Engine) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "backend" / "migrations"))
    config.set_main_option("sqlalchemy.url", str(engine.url))
    return config


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def has_legacy_tables(engine: Engine) -> bool:
    """True when the database predates Alembic: real tables, no version stamp."""
    tables = set(inspect(engine).get_table_names())
    return "employees" in tables and "alembic_version" not in tables


def upgrade_database(engine: Engine | None = None) -> str | None:
    """Bring the database to head, stamping a pre-Alembic database first."""
    if engine is None:
        from .db import engine as default_engine

        engine = default_engine

    config = alembic_config(engine)

    if has_legacy_tables(engine):
        logger.info("Existing pre-Alembic database detected; stamping %s", BASELINE_REVISION)
        command.stamp(config, BASELINE_REVISION)

    command.upgrade(config, "head")
    revision = current_revision(engine)
    logger.info("Database schema at revision %s", revision)
    return revision
