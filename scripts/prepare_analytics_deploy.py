"""Safety-gate the Analytics DEV schema migration before each Railway deploy."""

from __future__ import annotations

import os
import subprocess
import sys

from sqlalchemy import create_engine, text


def _database_url() -> str:
    raw = os.environ.get("ANALYTICS_DB_URL", "").strip()
    if not raw:
        raise RuntimeError("ANALYTICS_DB_URL is required")

    for prefix, replacement in (
        ("postgresql+asyncpg://", "postgresql+psycopg://"),
        ("postgresql://", "postgresql+psycopg://"),
        ("postgres://", "postgresql+psycopg://"),
    ):
        if raw.startswith(prefix):
            return replacement + raw[len(prefix) :]
    return raw


def _verify_source_database(engine) -> None:
    with engine.connect() as connection:
        database_name, has_auditcore = connection.execute(
            text("SELECT current_database(), to_regnamespace('auditcore') IS NOT NULL")
        ).one()
        if not has_auditcore:
            raise SystemExit(
                "ANALYTICS_DB_URL does not point to a database containing auditcore schema"
            )

        journey_count = connection.execute(
            text("SELECT count(*) FROM auditcore.journeys")
        ).scalar_one()

    print(
        "ANALYTICS_DB_SOURCE=PASS"
        f"|database={database_name}"
        f"|auditcore_journeys={journey_count}"
    )


def _verify_analytics_schema(engine) -> None:
    with engine.connect() as connection:
        dump_runs, snapshot_rows = connection.execute(
            text(
                "SELECT to_regclass('analytics.dump_runs'), "
                "to_regclass('analytics.snapshot_rows')"
            )
        ).one()

    if dump_runs is None or snapshot_rows is None:
        raise SystemExit("Analytics migration did not create required tables")

    print("ANALYTICS_SCHEMA_MIGRATION=PASS")


def main() -> None:
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        _verify_source_database(engine)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=True,
        )
        _verify_analytics_schema(engine)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
