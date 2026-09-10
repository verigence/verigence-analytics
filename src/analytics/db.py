from functools import lru_cache

from sqlalchemy import Engine, create_engine

from analytics.config import load_settings


def _normalise(url: str) -> str:
    for prefix, replacement in (
        ("postgresql+asyncpg://", "postgresql+psycopg://"),
        ("postgresql://", "postgresql+psycopg://"),
        ("postgres://", "postgresql+psycopg://"),
    ):
        if url.startswith(prefix):
            return replacement + url[len(prefix) :]
    return url


def _make_engine(url: str, *, application_name: str, statement_timeout_ms: int) -> Engine:
    return create_engine(
        _normalise(url),
        pool_size=3,
        max_overflow=2,
        pool_timeout=10,
        pool_recycle=300,
        pool_pre_ping=True,
        connect_args={
            "prepare_threshold": None,
            "options": (
                f"-c statement_timeout={statement_timeout_ms} "
                "-c idle_in_transaction_session_timeout=120000 "
                "-c jit=off "
                f"-c application_name={application_name}"
            ),
        },
    )


@lru_cache
def analytics_engine() -> Engine:
    settings = load_settings(require_security=False)
    return _make_engine(
        settings.database_url,
        application_name="verigence-analytics-api",
        statement_timeout_ms=15000,
    )


@lru_cache
def source_engine() -> Engine:
    settings = load_settings(require_security=False)
    return _make_engine(
        settings.auditcore_database_url,
        application_name="verigence-analytics-dump",
        statement_timeout_ms=120000,
    )
