from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from analytics.business_intelligence import (
    business_coverage,
    business_overview,
    commercial_components,
    customer_geography,
    sales_product,
)
from analytics.executive_dashboard import executive_dashboard
from analytics.security import Principal, require_analytics_read

router = APIRouter(
    prefix="/v1/analytics/tenants/{tenant_id}/business-intelligence",
    tags=["business-intelligence"],
)


@router.get("/project-dashboard")
def project_dashboard(
    tenant_id: str,
    principal: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Return the initial business-analytics screen in one authorized request.

    The endpoint deliberately composes existing snapshot-backed report functions so
    the browser does not fan out multiple Security authorization checks when the
    Analytics overview opens.
    """
    return {
        "executive": executive_dashboard(tenant_id, principal),
        "business": business_overview(tenant_id, principal),
        "coverage": business_coverage(tenant_id, principal),
        "sales_product": sales_product(tenant_id, principal),
        "geography": customer_geography(tenant_id, principal),
        "commercial_components": commercial_components(tenant_id, principal),
    }
