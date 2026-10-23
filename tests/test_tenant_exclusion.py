import pytest
from fastapi import HTTPException

from analytics.dump import create_dump
from analytics.security import Principal, require_analytics_read

EXCLUDED_TENANT_ID = "5fbe26ab-6ed7-444a-8488-5bfb81208588"


def test_excluded_tenant_is_not_authorized_for_analytics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYTICS_DB_URL", "postgresql://unused:unused@localhost/unused")
    monkeypatch.setattr(
        "analytics.security.get_security_authorization_client",
        lambda: pytest.fail("excluded tenant should be rejected before authorization lookup"),
    )

    with pytest.raises(HTTPException) as exc_info:
        require_analytics_read(EXCLUDED_TENANT_ID, Principal(subject="user-1"))

    assert exc_info.value.status_code == 404


def test_excluded_tenant_cannot_be_dumped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANALYTICS_DB_URL", "postgresql://unused:unused@localhost/unused")
    monkeypatch.setattr(
        "analytics.dump.analytics_engine",
        lambda: pytest.fail("excluded tenant should be rejected before database writes"),
    )

    with pytest.raises(ValueError, match="excluded tenant"):
        create_dump(tenant_id=EXCLUDED_TENANT_ID, requested_by="test")
