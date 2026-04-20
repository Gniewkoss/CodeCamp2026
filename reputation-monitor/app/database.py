from __future__ import annotations

import logging
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def _build_engine():
    settings = get_settings()
    connect_args = {}
    if settings.is_sqlite:
        connect_args = {"check_same_thread": False}
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        echo=False,
        connect_args=connect_args,
        future=True,
    )


engine = _build_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _sqlite_auto_migrate() -> None:
    """Lightweight auto-migration for SQLite demo DBs.

    Adds any columns that exist on the ORM model but are missing from the
    physical table. Good enough for the hackathon zero-config flow — for
    production use alembic on Postgres.
    """
    from app import models  # noqa: F401

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())

    with engine.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            if table_name not in existing_tables:
                continue
            existing_cols = {c["name"] for c in insp.get_columns(table_name)}
            for col in table.columns:
                if col.name in existing_cols:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                nullable = "" if col.nullable else " NOT NULL"
                default = ""
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    if isinstance(val, bool):
                        default = f" DEFAULT {1 if val else 0}"
                    elif isinstance(val, (int, float)):
                        default = f" DEFAULT {val}"
                    elif isinstance(val, str):
                        default = f" DEFAULT '{val}'"
                try:
                    sql = f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {col_type}{nullable}{default}'
                    conn.execute(text(sql))
                    logger.info("auto-migrate: added %s.%s", table_name, col.name)
                except Exception as e:
                    logger.warning("auto-migrate failed for %s.%s: %s", table_name, col.name, e)


def init_db() -> None:
    """Create tables for SQLite / lightweight demo setups.

    For Postgres use alembic migrations instead.
    """
    from app import models  # noqa: F401  — ensure models are imported

    Base.metadata.create_all(bind=engine)

    # For SQLite demo mode also add any missing columns (simple dev migrations).
    settings = get_settings()
    if settings.is_sqlite:
        try:
            _sqlite_auto_migrate()
        except Exception as e:
            logger.warning("sqlite auto-migrate skipped: %s", e)
