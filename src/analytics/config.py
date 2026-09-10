import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    auditcore_database_url: str
    security_jwks_url: str
    security_issuer: str
    security_audience: str
    analytics_schema: str
    source_schema: str
    dump_batch_size: int
    dump_retention: int


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def load_settings(*, require_security: bool = True) -> Settings:
    database_url = _required("DATABASE_URL")
    security_jwks_url = os.environ.get("SECURITY_JWKS_URL", "").strip()
    security_issuer = os.environ.get("SECURITY_ISSUER", "").strip()
    security_audience = os.environ.get("SECURITY_AUDIENCE", "").strip()
    if require_security and not all((security_jwks_url, security_issuer, security_audience)):
        raise RuntimeError("SECURITY_JWKS_URL, SECURITY_ISSUER and SECURITY_AUDIENCE are required")
    return Settings(
        app_env=os.environ.get("APP_ENV", "dev").strip() or "dev",
        database_url=database_url,
        auditcore_database_url=(os.environ.get("AUDITCORE_DATABASE_URL", "").strip() or database_url),
        security_jwks_url=security_jwks_url,
        security_issuer=security_issuer,
        security_audience=security_audience,
        analytics_schema=os.environ.get("ANALYTICS_SCHEMA", "analytics").strip() or "analytics",
        source_schema=os.environ.get("ANALYTICS_SOURCE_SCHEMA", "auditcore").strip() or "auditcore",
        dump_batch_size=max(50, int(os.environ.get("ANALYTICS_DUMP_BATCH_SIZE", "500"))),
        dump_retention=max(1, int(os.environ.get("ANALYTICS_DUMP_RETENTION", "8"))),
    )
