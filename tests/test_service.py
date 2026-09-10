import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost/unused")
os.environ.setdefault("ANALYTICS_SECURITY_JWKS_URL", "https://example.invalid/jwks")
os.environ.setdefault("SECURITY_TOKEN_ISSUER", "verigence-security")
os.environ.setdefault("SECURITY_TOKEN_AUDIENCE", "verigence-platform")

from analytics.dump import SOURCE_TABLES, _identifier
from analytics.main import create_app


def test_health_route_is_registered() -> None:
    app = create_app()
    paths = set(app.openapi()["paths"])
    assert "/health" in paths
    assert "/ready" in paths
    assert "/v1/analytics/tenants/{tenant_id}/findings" in paths
    assert "/v1/analytics/tenants/{tenant_id}/documents" in paths
    assert "/v1/analytics/tenants/{tenant_id}/payments" in paths


def test_dump_sources_cover_business_domains() -> None:
    names = {table.name for table in SOURCE_TABLES}
    required = {
        "journeys",
        "bookings",
        "commercial_lines",
        "discount_applications",
        "payments",
        "finance_records",
        "insurance_records",
        "journey_addons",
        "trade_in_cases",
        "deliveries",
        "audit_findings",
        "journey_document_requirements",
        "journey_document_assessments",
        "journey_workflow_events",
        "dealership_staff",
    }
    assert required <= names


def test_identifier_guard() -> None:
    assert _identifier("auditcore") == "auditcore"
    try:
        _identifier("auditcore;drop table x")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe SQL identifier accepted")
