from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from analytics.network_reports import business_scorecard
from analytics.reports import dashboard
from analytics.security import Principal, require_analytics_read

router = APIRouter(prefix="/v1/analytics/tenants/{tenant_id}", tags=["analytics"])


@router.get("/executive-dashboard")
def executive_dashboard(
    tenant_id: str,
    principal: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Return the project/dealer/outlet view and existing overview in one auth call."""
    return {
        "network": business_scorecard(tenant_id, principal),
        "overview": dashboard(tenant_id, principal),
    }
