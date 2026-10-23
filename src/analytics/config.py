from dataclasses import dataclass
import os


# This tenant is intentionally excluded from Analytics API responses and future snapshot imports.
_DEFAULT_EXCLUDED_TENANT_IDS = frozenset({"5fbe26ab-6ed7-444a-8488-5bfb81208588"})


def _csv_ids(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    auditcore_database_url: str
    security_jwks_url: str
    security_issuer: str
    security_audience: str
    security_base_url: str
    security_client_id: str
    security_client_secret: str
    analytics_schema: str
    source_schema: str
    dump_batch_size: int
    dump_retention: int
    excluded_tenant_ids: frozenset[str]


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _security_base_url(jwks_url: str) -> str:
    configured = os.environ.get("ANALYTICS_SECURITY_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    suffix = "/.well-known/jwks.json"
    if jwks_url.endswith(suffix):
        return jwks_url[: -len(suffix)].rstrip("/")
    raise RuntimeError(
        "ANALYTICS_SECURITY_BASE_URL is required when ANALYTICS_SECURITY_JWKS_URL "
        "does not end with /.well-known/jwks.json"
    )


def load_settings(*, require_security: bool = True) -> Settings:
    database_url = _required("ANALYTICS_DB_URL")
    security_jwks_url = os.environ.get("ANALYTICS_SECURITY_JWKS_URL", "").strip()
    security_issuer = os.environ.get("SECURITY_TOKEN_ISSUER", "").strip()
    security_audience = os.environ.get("SECURITY_TOKEN_AUDIENCE", "").strip()
    security_client_secret = os.environ.get("ANALYTICS_SECRET_KEY", "")
    security_client_id = os.environ.get("ANALYTICS_SECURITY_CLIENT_ID", "analytics").strip() or "analytics"

    security_base_url = ""
    if security_jwks_url:
        try:
            security_base_url = _security_base_url(security_jwks_url)
        except RuntimeError:
            if require_security:
                raise

    if require_security and not all(
        (
            security_jwks_url,
            security_issuer,
            security_audience,
            security_base_url,
            security_client_id,
            security_client_secret,
        )
    ):
        raise RuntimeError(
            "ANALYTICS_SECURITY_JWKS_URL, SECURITY_TOKEN_ISSUER, "
            "SECURITY_TOKEN_AUDIENCE and ANALYTICS_SECRET_KEY are required"
        )

    return Settings(
        app_env=os.environ.get("APP_ENV", "dev").strip() or "dev",
        database_url=database_url,
        auditcore_database_url=(os.environ.get("AUDITCORE_DATABASE_URL", "").strip() or database_url),
        security_jwks_url=security_jwks_url,
        security_issuer=security_issuer,
        security_audience=security_audience,
        security_base_url=security_base_url,
        security_client_id=security_client_id,
        security_client_secret=security_client_secret,
        analytics_schema=os.environ.get("ANALYTICS_SCHEMA", "analytics").strip() or "analytics",
        source_schema=os.environ.get("ANALYTICS_SOURCE_SCHEMA", "auditcore").strip() or "auditcore",
        dump_batch_size=max(50, int(os.environ.get("ANALYTICS_DUMP_BATCH_SIZE", "500"))),
        dump_retention=max(1, int(os.environ.get("ANALYTICS_DUMP_RETENTION", "2"))),
        excluded_tenant_ids=(
            _DEFAULT_EXCLUDED_TENANT_IDS
            | _csv_ids(os.environ.get("ANALYTICS_EXCLUDED_TENANT_IDS", ""))
        ),
    )
