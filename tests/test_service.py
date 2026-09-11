import os

import httpx

os.environ.setdefault("ANALYTICS_DB_URL", "postgresql://unused:unused@localhost/unused")
os.environ.setdefault("ANALYTICS_SECURITY_JWKS_URL", "https://example.invalid/.well-known/jwks.json")
os.environ.setdefault("SECURITY_TOKEN_ISSUER", "verigence-security")
os.environ.setdefault("SECURITY_TOKEN_AUDIENCE", "verigence-platform")
os.environ.setdefault("ANALYTICS_SECRET_KEY", "test-only-secret")

from analytics.dump import SOURCE_TABLES, _identifier
from analytics.main import create_app
from analytics.security_authorization import SecurityAuthorizationClient


def test_health_route_is_registered() -> None:
    app = create_app()
    paths = set(app.openapi()["paths"])
    assert "/health" in paths
    assert "/ready" in paths
    assert "/v1/analytics/tenants/{tenant_id}/findings" in paths
    assert "/v1/analytics/tenants/{tenant_id}/documents" in paths
    assert "/v1/analytics/tenants/{tenant_id}/payments" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-scorecard" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/overview" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/coverage" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/sales-product" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/delivery" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/discounts" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/insurance" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/vas" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/compliance" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/geography" in paths


def test_dump_sources_cover_business_domains() -> None:
    names = {table.name for table in SOURCE_TABLES}
    required = {
        "dealers",
        "dealer_outlets",
        "journeys",
        "bookings",
        "journey_products",
        "commercial_lines",
        "discount_applications",
        "payments",
        "finance_records",
        "insurance_records",
        "journey_addons",
        "trade_in_cases",
        "vehicle_records",
        "registration_records",
        "deliveries",
        "audit_evaluations",
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


def test_security_authorization_uses_service_identity_and_live_permission_check() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/security/v1/service/token":
            assert request.headers.get("authorization", "").startswith("Basic ")
            return httpx.Response(
                200,
                json={
                    "accessToken": "service-token",
                    "tokenType": "Bearer",
                    "audience": "security",
                    "expiresIn": 300,
                },
            )
        if request.url.path == "/security/v1/authorization/check":
            assert request.headers["authorization"] == "Bearer service-token"
            return httpx.Response(
                200,
                json={
                    "allowed": True,
                    "reasonCode": "ALLOW_ROLE_PERMISSION",
                    "userId": "11111111-1111-1111-1111-111111111111",
                    "tenantId": "22222222-2222-2222-2222-222222222222",
                    "permissionKey": "audit.analytics.read",
                    "roleKey": "TL",
                },
            )
        return httpx.Response(404)

    client = SecurityAuthorizationClient(
        base_url="https://security.invalid",
        client_id="analytics",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        decision = client.check_user_permission(
            user_id="11111111-1111-1111-1111-111111111111",
            tenant_id="22222222-2222-2222-2222-222222222222",
            permission_key="audit.analytics.read",
        )
    finally:
        client.close()

    assert decision.allowed is True
    assert decision.role_key == "TL"
    assert requests == [
        ("POST", "/security/v1/service/token"),
        ("POST", "/security/v1/authorization/check"),
    ]
