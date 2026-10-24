import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import HTTPException

os.environ.setdefault("ANALYTICS_DB_URL", "postgresql://unused:unused@localhost/unused")
os.environ.setdefault("ANALYTICS_SECURITY_JWKS_URL", "https://example.invalid/.well-known/jwks.json")
os.environ.setdefault("SECURITY_TOKEN_ISSUER", "verigence-security")
os.environ.setdefault("SECURITY_TOKEN_AUDIENCE", "verigence-platform")
os.environ.setdefault("ANALYTICS_SECRET_KEY", "test-only-secret")

from analytics.main import create_app
from analytics.security_authorization import SecurityAuthorizationClient

from analytics import reports
from analytics.dump import (
    SOURCE_TABLES,
    _copy_derived_accessory_line_items,
    _copy_derived_delivery_invoice_line_items,
    _copy_derived_unverified_delivery_invoice_line_items,
    _identifier,
)


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
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/finance" in paths
    assert "/v1/analytics/tenants/{tenant_id}/business-intelligence/payments" in paths


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
    assert "invoice_review_values" not in names


def test_accessory_projection_uses_a_whitelisted_private_safe_query() -> None:
    class EmptyMappings:
        def fetchmany(self, _size: int) -> list[dict]:
            return []

    class EmptyResult:
        def mappings(self) -> EmptyMappings:
            return EmptyMappings()

    class RecordingSource:
        statement = ""

        def execute(self, statement, _params):
            self.statement = str(statement)
            return EmptyResult()

    source = RecordingSource()
    _copy_derived_accessory_line_items(
        source,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        source_schema="auditcore",
        tenant_id="tenant",
        dump_id=uuid4(),
        batch_size=10,
    )

    sql = source.statement.lower()
    assert "invoice_review_values" in sql
    assert "safe_description" in sql
    assert "source_document_count" in sql
    assert "row_number() over" in sql
    assert "updated_at_utc desc nulls last" in sql
    assert "[redacted phone]" in sql
    assert "[redacted email]" in sql
    assert "to_jsonb(t)" not in sql
    payload = sql.split("jsonb_build_object(", 1)[1].split(")) as row_data", 1)[0]
    assert "buyer_name" not in sql
    assert "buyer_address" not in sql
    assert "buyer_gstin" not in sql
    assert "'invoice_number'" not in payload
    assert "vin_number" not in sql
    assert "chassis_number" not in sql
    assert "engine_number" not in sql
    assert "vehicle_registration_number" not in sql


def test_delivery_invoice_projection_is_tax_invoice_only_and_private_safe() -> None:
    class EmptyMappings:
        def fetchmany(self, _size: int) -> list[dict]:
            return []

    class EmptyResult:
        def mappings(self) -> EmptyMappings:
            return EmptyMappings()

    class RecordingSource:
        def __init__(self) -> None:
            self.statement = ""
            self.params: dict[str, str] = {}

        def execute(self, statement, params):
            self.statement = str(statement)
            self.params = params
            return EmptyResult()

    source = RecordingSource()
    _copy_derived_delivery_invoice_line_items(
        source,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        source_schema="auditcore",
        tenant_id="tenant",
        dump_id=uuid4(),
        batch_size=10,
    )

    sql = source.statement.lower()
    assert "upper(trim(coalesce(invoice_nature, ''))) = :invoice_nature" in sql
    assert "journey_id is not null" in sql
    assert "'invoice_purpose'" in sql
    assert "safe_description" in sql
    assert "to_jsonb(t)" not in sql
    assert "buyer_name" not in sql
    assert "buyer_address" not in sql
    assert "vin_number" not in sql
    assert source.params == {"tenant_id": "tenant", "invoice_nature": "TAX_INVOICE"}


def test_unverified_delivery_invoice_projection_is_separate_and_private_safe() -> None:
    class EmptyMappings:
        def fetchmany(self, _size: int) -> list[dict]:
            return []

    class EmptyResult:
        def mappings(self) -> EmptyMappings:
            return EmptyMappings()

    class RecordingSource:
        def __init__(self) -> None:
            self.statement = ""
            self.params: dict[str, str] = {}

        def execute(self, statement, params):
            self.statement = str(statement)
            self.params = params
            return EmptyResult()

    source = RecordingSource()
    _copy_derived_unverified_delivery_invoice_line_items(
        source,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        source_schema="auditcore",
        tenant_id="tenant",
        dump_id=uuid4(),
        batch_size=10,
    )

    sql = source.statement.lower()
    assert "upper(trim(coalesce(invoice_nature, ''))) = :invoice_nature" in sql
    assert "journey_id is not null" in sql
    assert "safe_description" in sql
    assert "to_jsonb(t)" not in sql
    assert "buyer_name" not in sql
    assert "buyer_address" not in sql
    assert "vin_number" not in sql
    assert source.params == {"tenant_id": "tenant", "invoice_nature": "UNKNOWN"}


def test_identifier_guard() -> None:
    assert _identifier("auditcore") == "auditcore"
    try:
        _identifier("auditcore;drop table x")
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe SQL identifier accepted")


def test_excluded_project_history_is_not_served(monkeypatch) -> None:
    excluded_tenant_id = "5fbe26ab-6ed7-444a-8488-5bfb81208588"
    monkeypatch.setattr(
        reports,
        "load_settings",
        lambda **_: SimpleNamespace(excluded_tenant_ids=frozenset({excluded_tenant_id})),
    )

    class NoDatabaseAccess:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("excluded project history must not be queried")

    try:
        reports._latest_dump(NoDatabaseAccess(), excluded_tenant_id)
    except HTTPException as error:
        assert error.status_code == 404
        assert error.detail == "Analytics is disabled for this project"
    else:
        raise AssertionError("excluded project history was served")


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
