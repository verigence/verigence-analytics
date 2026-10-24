"""Run read-only report queries against each included Analytics DEV tenant."""

from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal

from sqlalchemy import text

from analytics.config import load_settings
from analytics.db import source_engine
from analytics.business_intelligence import (
    _DEAL_FACTS_CTE,
    _latest_context,
    customer_geography,
    discount_intelligence,
    finance_intelligence,
    insurance_intelligence,
    payments_intelligence,
    sales_product,
    vas_intelligence,
)
from analytics.reports import productivity


def _print_data_diagnostics(
    tenant_id: str,
    vas: dict,
    insurance: dict,
    geography: dict,
) -> None:
    """Emit privacy-safe business dimensions for report/UI validation."""
    pin_totals: dict[str, dict] = {}
    for row in geography.get("rows", []):
        pin = str(row.get("customer_pincode") or "").strip()
        if not pin:
            continue
        item = pin_totals.setdefault(pin, {"pincode": pin, "journeys": 0, "dealers": {}})
        item["journeys"] += int(row.get("journey_count") or 0)
        dealer = str(row.get("dealer_name") or "Unspecified dealer")
        dealer_item = item["dealers"].setdefault(dealer, {"journeys": 0, "outlets": {}})
        dealer_item["journeys"] += int(row.get("journey_count") or 0)
        outlet = str(row.get("outlet_name") or "Unspecified outlet")
        dealer_item["outlets"][outlet] = dealer_item["outlets"].get(outlet, 0) + int(row.get("journey_count") or 0)
    print(
        "PIN_AREA_COUNTS|"
        f"tenant={tenant_id}|"
        + json.dumps(sorted(pin_totals.values(), key=lambda x: (-x["journeys"], x["pincode"])), separators=(",", ":"))
    )

    connection, latest = _latest_context(tenant_id)
    try:
        params = {"tenant_id": tenant_id, "dump_id": latest["dump_id"]}
        addon_fields = connection.execute(
            text(
                """
                SELECT field.key, count(*)::int AS row_count
                FROM analytics.snapshot_rows snapshot
                CROSS JOIN LATERAL jsonb_object_keys(snapshot.row_data) AS field(key)
                WHERE snapshot.tenant_id=:tenant_id AND snapshot.dump_id=:dump_id
                  AND snapshot.source_table='journey_addons'
                GROUP BY field.key ORDER BY field.key
                """
            ),
            params,
        ).mappings().all()
        term_values = connection.execute(
            text(
                """
                SELECT field.key, field.value, count(*)::int AS row_count
                FROM analytics.snapshot_rows snapshot
                CROSS JOIN LATERAL jsonb_each_text(snapshot.row_data) AS field(key, value)
                WHERE snapshot.tenant_id=:tenant_id AND snapshot.dump_id=:dump_id
                  AND snapshot.source_table='journey_addons'
                  AND field.key ~* '(warranty|term|duration|period|year)'
                  AND NULLIF(trim(field.value), '') IS NOT NULL
                GROUP BY field.key, field.value
                ORDER BY field.key, row_count DESC, field.value
                LIMIT 80
                """
            ),
            params,
        ).mappings().all()
        term_evidence = connection.execute(
            text(
                """
                WITH candidate_fields AS (
                    SELECT snapshot.source_table,
                           snapshot.source_pk,
                           field.key AS field_name,
                           field.value AS field_value,
                           upper(COALESCE(snapshot.row_data->>'addon_type_code','')) AS addon_type
                    FROM analytics.snapshot_rows snapshot
                    CROSS JOIN LATERAL jsonb_each_text(snapshot.row_data) AS field(key, value)
                    WHERE snapshot.tenant_id=:tenant_id AND snapshot.dump_id=:dump_id
                      AND snapshot.source_table IN (
                          'journey_addons', 'derived_accessory_line_items',
                          'commercial_lines', 'insurance_records'
                      )
                      AND (
                          field.value ~* '(warrant|RSA|road[[:space:]_-]*side[[:space:]_-]*assist)'
                          OR (snapshot.source_table='journey_addons'
                              AND upper(COALESCE(snapshot.row_data->>'addon_type_code',''))='EW'
                              AND field.key='details')
                      )
                ), categorized AS (
                    SELECT source_table, source_pk, field_name, addon_type,
                           CASE
                               WHEN (field_value ~* 'warrant'
                                     OR (source_table='journey_addons' AND addon_type='EW'))
                                    AND field_value ~* '(4[[:space:]_-]*(year|yr|y)|48[[:space:]_-]*month|four[[:space:]_-]*year)'
                                   THEN 'FOUR_YEAR'
                               WHEN (field_value ~* 'warrant'
                                     OR (source_table='journey_addons' AND addon_type='EW'))
                                    AND field_value ~* '(5[[:space:]_-]*(year|yr|y)|60[[:space:]_-]*month|five[[:space:]_-]*year)'
                                   THEN 'FIVE_YEAR'
                               WHEN field_value ~* 'warrant'
                                    OR (source_table='journey_addons' AND addon_type='EW')
                                   THEN 'WARRANTY_TERM_UNSPECIFIED'
                               WHEN field_value ~* '(road[[:space:]_-]*side[[:space:]_-]*assist|roadside|(^|[^A-Z0-9])RSA([^A-Z0-9]|$))'
                                    OR (source_table='journey_addons' AND addon_type='RSA')
                                   THEN 'RSA'
                           END AS evidence_type
                    FROM candidate_fields
                )
                SELECT source_table, field_name, evidence_type,
                       count(DISTINCT source_pk)::int AS source_records
                FROM categorized
                WHERE evidence_type IS NOT NULL
                GROUP BY source_table, field_name, evidence_type
                ORDER BY source_table, field_name, evidence_type
                """
            ),
            params,
        ).mappings().all()
        warranty_accessory_lines = connection.execute(
            text(
                """
                SELECT upper(COALESCE(NULLIF(row_data->>'invoice_nature',''),'UNCLASSIFIED')) AS invoice_nature,
                       COALESCE(NULLIF(row_data->>'line_category',''),'Uncategorized') AS line_category,
                       COALESCE(NULLIF(row_data->>'description',''),
                                NULLIF(row_data->>'item_code',''),'Unlabelled') AS item_description,
                       NULLIF(row_data->>'item_code','') AS item_code,
                       count(*)::int AS invoice_lines,
                       sum(NULLIF(row_data->>'quantity','')::numeric) AS invoiced_quantity,
                       sum(NULLIF(row_data->>'net_amount','')::numeric) AS net_amount
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id
                  AND source_table='derived_accessory_line_items'
                  AND COALESCE(row_data->>'description','') || ' ' ||
                      COALESCE(row_data->>'item_code','') ~* '(warrant|RSA|road[[:space:]_-]*side[[:space:]_-]*assist)'
                GROUP BY invoice_nature, line_category, item_description, item_code
                ORDER BY invoice_lines DESC, item_description
                """
            ),
            params,
        ).mappings().all()
        insurance_evidence = connection.execute(
            text(
                _DEAL_FACTS_CTE
                + """
                SELECT dealer_name, outlet_name, insurance_route,
                       COALESCE(NULLIF(insurance_source,''),'(blank)') AS insurance_source,
                       COALESCE(NULLIF(insurance_by,''),'(blank)') AS insurance_by,
                       COALESCE(self_insurance_flag::text,'(blank)') AS self_insurance_flag,
                       COALESCE(NULLIF(insurance_misp_code,''),'(blank)') AS misp_code,
                       COALESCE(NULLIF(insurance_intermediary_name,''),'(blank)') AS intermediary,
                       count(*)::int AS policies
                FROM deal_facts
                WHERE has_booking AND has_insurance
                GROUP BY dealer_name, outlet_name, insurance_route, insurance_source,
                         insurance_by, self_insurance_flag, insurance_misp_code,
                         insurance_intermediary_name
                ORDER BY dealer_name, outlet_name, policies DESC, insurance_source, insurance_by
                """
            ),
            params,
        ).mappings().all()
    finally:
        connection.close()

    print(
        "ADDON_SOURCE_FIELDS|"
        f"tenant={tenant_id}|"
        + json.dumps([dict(row) for row in addon_fields], separators=(",", ":"))
    )
    print(
        "ADDON_TERM_VALUES|"
        f"tenant={tenant_id}|"
        + json.dumps([dict(row) for row in term_values], separators=(",", ":"))
    )
    print(
        "ADDON_TERM_EVIDENCE|"
        f"tenant={tenant_id}|"
        + json.dumps([dict(row) for row in term_evidence], separators=(",", ":"))
    )
    print(
        "ACCESSORY_WARRANTY_RSA_LINES|"
        f"tenant={tenant_id}|"
        + json.dumps([dict(row) for row in warranty_accessory_lines], separators=(",", ":"), default=str)
    )
    print(
        "INSURANCE_SOURCE_CODES_BY_OUTLET|"
        f"tenant={tenant_id}|"
        + json.dumps([dict(row) for row in insurance_evidence], separators=(",", ":"))
    )
    product_addons = [
        row for row in vas.get("by_product_type", [])
        if str(row.get("addon_type") or "").upper() in {"EW", "RSA"}
    ]
    print(
        "EW_RSA_BY_DEALER_MODEL|"
        f"tenant={tenant_id}|"
        + json.dumps(product_addons, separators=(",", ":"), default=str)
    )
    print(
        "JOURNEY_ADDON_CODES|"
        f"tenant={tenant_id}|"
        + json.dumps(vas.get("by_type", []), separators=(",", ":"), default=str)
    )
    policy_terms = [
        row for row in insurance.get("add_ons", [])
        if any(re.search(r"warrant|rsa|roadside|assistance", term, re.IGNORECASE)
               for term in row.get("raw_terms", []))
    ]
    print(
        "INSURANCE_WARRANTY_RSA_TERMS_BY_OUTLET|"
        f"tenant={tenant_id}|"
        + json.dumps(policy_terms, separators=(",", ":"), default=str)
    )
    accessory_rows = sorted(
        vas.get("accessory_items", []),
        key=lambda row: (
            -(float(row.get("invoiced_quantity") or 0)),
            -(float(row.get("net_amount") or 0)),
            str(row.get("item_description") or ""),
        ),
    )
    print(
        "ACCESSORY_DETAIL_SAMPLE|"
        f"tenant={tenant_id}|"
        + json.dumps(accessory_rows[:60], separators=(",", ":"), default=str)
    )


def _print_delivery_invoice_diagnostics(tenant_id: str) -> None:
    """Report safe invoice document and VAS line counts without emitting line text."""
    source_schema = load_settings(require_security=False).source_schema
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", source_schema):
        raise ValueError("Unsafe source schema identifier")

    with source_engine().connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            params = {"tenant_id": tenant_id}
            document_rows = connection.execute(
                text(
                    f"""
                    SELECT upper(COALESCE(NULLIF(trim(invoice_purpose),''),'(BLANK)'))
                               AS invoice_purpose,
                           upper(COALESCE(NULLIF(trim(document_type_key),''),'(BLANK)'))
                               AS document_type_key,
                           upper(COALESCE(NULLIF(trim(invoice_nature),''),'(BLANK)'))
                               AS invoice_nature,
                           count(*)::int AS invoice_documents,
                           count(*) FILTER (
                               WHERE jsonb_typeof(line_items)='array'
                                 AND jsonb_array_length(line_items)>0
                           )::int AS documents_with_lines
                    FROM {source_schema}.invoice_review_values
                    WHERE tenant_id=:tenant_id
                    GROUP BY invoice_purpose, document_type_key, invoice_nature
                    ORDER BY invoice_documents DESC, invoice_purpose,
                             document_type_key, invoice_nature
                    """
                ),
                params,
            ).mappings().all()
            line_rows = connection.execute(
                text(
                    f"""
                    WITH invoice_lines AS (
                        SELECT d.invoice_review_value_id,
                               d.journey_id,
                               upper(COALESCE(NULLIF(trim(d.invoice_purpose),''),'(BLANK)'))
                                   AS invoice_purpose,
                               upper(COALESCE(NULLIF(trim(d.document_type_key),''),'(BLANK)'))
                                   AS document_type_key,
                               upper(COALESCE(NULLIF(trim(d.invoice_nature),''),'(BLANK)'))
                                   AS invoice_nature,
                               upper(COALESCE(NULLIF(trim(item.value->>'line_category'),''),''))
                                   AS line_category,
                               concat_ws(' ', item.value->>'description_raw',
                                   item.value->>'item_code', item.value->>'line_category')
                                   AS line_evidence
                        FROM {source_schema}.invoice_review_values d
                        CROSS JOIN LATERAL jsonb_array_elements(
                            CASE WHEN jsonb_typeof(d.line_items)='array'
                                 THEN d.line_items ELSE '[]'::jsonb END
                        ) AS item(value)
                        WHERE d.tenant_id=:tenant_id
                    ), classified AS (
                        SELECT *,
                               CASE
                                   WHEN line_evidence ~* '(extended[[:space:]_-]*warrant|warrant)'
                                       THEN 'EW'
                                   WHEN line_evidence ~* '(road[[:space:]_-]*side[[:space:]_-]*assist|roadside|(^|[^A-Z0-9])RSA([^A-Z0-9]|$))'
                                       THEN 'RSA'
                                   WHEN line_evidence ~* 'fast[[:space:]_-]*tag'
                                       THEN 'FASTAG'
                                   WHEN line_evidence ~* 'insurance|premium'
                                       THEN 'INSURANCE'
                                   WHEN line_category ~* 'accessor'
                                     OR invoice_purpose='ACCESSORY'
                                       THEN 'ACCESSORY'
                               END AS vas_category
                        FROM invoice_lines
                    )
                    SELECT invoice_purpose, document_type_key, invoice_nature,
                           COALESCE(vas_category,'OTHER_OR_UNMATCHED') AS vas_category,
                           count(*)::int AS invoice_lines,
                           count(DISTINCT invoice_review_value_id)::int AS invoice_count,
                           count(DISTINCT journey_id)::int AS journey_count
                    FROM classified
                    GROUP BY invoice_purpose, document_type_key, invoice_nature, vas_category
                    ORDER BY invoice_purpose, document_type_key, invoice_nature,
                             vas_category, invoice_lines DESC
                    """
                ),
                params,
            ).mappings().all()
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise

    print(
        "DELIVERY_INVOICE_PURPOSE_COVERAGE|"
        f"tenant={tenant_id}|"
        + json.dumps([dict(row) for row in document_rows], separators=(",", ":"))
    )
    print(
        "DELIVERY_INVOICE_VAS_LINE_COVERAGE|"
        f"tenant={tenant_id}|"
        + json.dumps([dict(row) for row in line_rows], separators=(",", ":"))
    )


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
    _print_data_diagnostics(tenant_id, accessories, insurance, geography)
    _print_delivery_invoice_diagnostics(tenant_id)
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
