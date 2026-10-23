"""Run read-only report queries against each included Analytics DEV tenant."""

from __future__ import annotations

import argparse
from decimal import Decimal

from analytics.business_intelligence import (
    customer_geography,
    discount_intelligence,
    finance_intelligence,
    insurance_intelligence,
    payments_intelligence,
    sales_product,
    vas_intelligence,
)
from analytics.reports import productivity


def _assert_tenant(report: dict, tenant_id: str, name: str) -> None:
    assert report.get("tenant_id") == tenant_id, f"{name} returned a different tenant"
    assert report.get("data_as_of"), f"{name} has no snapshot timestamp"


def verify_tenant(tenant_id: str) -> None:
    sales = sales_product(tenant_id, None)
    discounts = discount_intelligence(tenant_id, None)
    insurance = insurance_intelligence(tenant_id, None)
    accessories = vas_intelligence(tenant_id, None)
    finance = finance_intelligence(tenant_id, None)
    payments = payments_intelligence(tenant_id, None)
    geography = customer_geography(tenant_id, None)
    uploads = productivity(tenant_id, None)

    reports = {
        "sales": sales,
        "discounts": discounts,
        "insurance": insurance,
        "accessories": accessories,
        "finance": finance,
        "payments": payments,
        "geography": geography,
        "uploads": uploads,
    }
    for name, report in reports.items():
        _assert_tenant(report, tenant_id, name)

    sales_booked = int(sales["model_variant_coverage"].get("booked_journeys") or 0)
    dealer_model_booked = sum(
        int(row.get("booked_units") or 0) for row in sales["by_dealer_model_variant"]
    )
    assert sales_booked == dealer_model_booked, (
        "Dealer/model sales rows do not reconcile to booked units"
    )
    for row in sales["by_dealer_model_variant"]:
        assert int(row.get("delivered_units") or 0) <= int(row.get("booked_units") or 0)

    pricing = discounts["pricing_integrity"]["summary"]
    strict_excess = Decimal(str(pricing.get("strict_excess_discount_amount") or 0))
    known_excess = Decimal(str(pricing.get("known_excess_discount_amount") or 0))
    assert strict_excess >= known_excess, "Tolerance-adjusted overage exceeds strict overage"

    insurance_summary = insurance["summary"]
    route_total = sum(int(row.get("policy_count") or 0) for row in insurance["by_route"])
    assert route_total == int(insurance_summary.get("insurance_journeys") or 0)
    insurer_outlet_total = sum(
        int(row.get("policy_count") or 0) for row in insurance["by_insurer_outlet"]
    )
    assert insurer_outlet_total == int(insurance_summary.get("insurance_journeys") or 0)

    accessory_coverage = accessories["accessory_line_coverage"]
    assert int(accessory_coverage.get("confirmed_tax_invoice_lines") or 0) <= int(
        accessory_coverage.get("total_invoice_lines") or 0
    )
    assert all(
        "dealer_name" in row and "outlet_name" in row
        for row in accessories["accessory_items"]
    )

    assert "by_lender_outlet" in finance and "by_outlet" in finance
    assert "by_channel" in payments and "by_outlet" in payments
    assert "coverage" in geography and "rows" in geography
    assert "summary" in uploads and "upload_activity" in uploads

    uploaded_journeys = sum(
        int(row.get("journeys_created") or 0) for row in uploads["summary"]
    )
    delivered_units = sum(
        int(row.get("delivered_units") or 0)
        for row in sales["by_dealer_model_variant"]
    )
    print(
        "BUSINESS_REPORTS_VERIFIED"
        f"|tenant={tenant_id}"
        f"|journeys={uploaded_journeys}"
        f"|booked={sales_booked}"
        f"|delivered={delivered_units}"
        f"|dealer_model_rows={len(sales['by_dealer_model_variant'])}"
        f"|accessory_items={len(accessories['accessory_items'])}"
        f"|insurance_policies={insurance_summary.get('insurance_journeys', 0)}"
        f"|insurer_outlet_rows={len(insurance['by_insurer_outlet'])}"
        f"|finance_lender_outlet_rows={len(finance['by_lender_outlet'])}"
        f"|payment_channel_outlet_rows={len(payments['by_outlet'])}"
        f"|uploaders={len(uploads['summary'])}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-ids", required=True)
    args = parser.parse_args()
    tenant_ids = [tenant.strip() for tenant in args.tenant_ids.split(",") if tenant.strip()]
    if not tenant_ids:
        raise SystemExit("At least one tenant ID is required")
    for tenant_id in tenant_ids:
        verify_tenant(tenant_id)
    print("ANALYTICS_BUSINESS_REPORT_SMOKE=PASS")


if __name__ == "__main__":
    main()
