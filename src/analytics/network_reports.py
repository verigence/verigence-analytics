from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from analytics.db import analytics_engine
from analytics.reports import _latest_dump, _query_rows
from analytics.security import Principal, require_analytics_read

router = APIRouter(prefix="/v1/analytics/tenants/{tenant_id}", tags=["analytics"])


_SCORECARD_SQL = """
WITH
configured_dealers AS (
    SELECT row_data->>'dealer_id' AS dealer_id,
           COALESCE(NULLIF(row_data->>'dealer_name',''), NULLIF(row_data->>'dealer_code',''), 'Unspecified dealer') AS dealer_name
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='dealers'
),
configured_outlets AS (
    SELECT row_data->>'outlet_id' AS outlet_id,
           row_data->>'dealer_id' AS dealer_id,
           COALESCE(NULLIF(row_data->>'outlet_name',''), NULLIF(row_data->>'outlet_code',''), 'Unspecified outlet') AS outlet_name,
           COALESCE(row_data->>'city','') AS city,
           COALESCE(row_data->>'state_region','') AS state_region
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='dealer_outlets'
),
journeys AS (
    SELECT row_data->>'journey_id' AS journey_id,
           row_data->>'dealer_id' AS dealer_id,
           row_data->>'outlet_id' AS outlet_id
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journeys'
),
findings AS (
    SELECT row_data->>'journey_id' AS journey_id,
           count(*)::int AS finding_count,
           count(*) FILTER (WHERE upper(COALESCE(row_data->>'finding_status',''))='OPEN')::int AS open_finding_count,
           count(*) FILTER (WHERE upper(COALESCE(row_data->>'severity',''))='HIGH')::int AS high_finding_count,
           count(*) FILTER (
               WHERE lower(COALESCE(row_data->>'title','')) LIKE '%document%missing%'
                  OR lower(COALESCE(row_data->>'description','')) LIKE '%document%missing%'
                  OR lower(COALESCE(row_data->>'rule_key','')) LIKE '%document%missing%'
           )::int AS missing_document_flag_count
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='audit_findings'
    GROUP BY 1
),
finance AS (
    SELECT row_data->>'journey_id' AS journey_id
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='finance_records'
    GROUP BY 1
),
insurance AS (
    SELECT row_data->>'journey_id' AS journey_id
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='insurance_records'
    GROUP BY 1
),
trade_in AS (
    SELECT row_data->>'journey_id' AS journey_id
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='trade_in_cases'
    GROUP BY 1
),
addons AS (
    SELECT row_data->>'journey_id' AS journey_id,
           bool_or(upper(COALESCE(row_data->>'addon_type_code',''))='EW') AS has_ew,
           bool_or(upper(COALESCE(row_data->>'addon_type_code',''))='RSA') AS has_rsa,
           bool_or(upper(COALESCE(row_data->>'addon_type_code','')) IN ('ACCESSORY','ACCESSORIES')) AS has_accessories
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_addons'
    GROUP BY 1
),
bookings AS (
    SELECT row_data->>'journey_id' AS journey_id,
           bool_or(COALESCE((NULLIF(row_data->>'corporate_discount_taken',''))::boolean,false)) AS corporate_discount,
           bool_or(COALESCE((NULLIF(row_data->>'gst_benefit',''))::boolean,false)) AS gst_benefit,
           bool_or(COALESCE((NULLIF(row_data->>'exchange_discount_taken',''))::boolean,false)) AS exchange_discount
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
    GROUP BY 1
),
payments AS (
    SELECT row_data->>'journey_id' AS journey_id,
           COALESCE(sum(NULLIF(row_data->>'amount','')::numeric),0) AS payment_amount
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='payments'
    GROUP BY 1
),
discounts AS (
    SELECT row_data->>'journey_id' AS journey_id,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric),0) AS actual_discount_amount,
           COALESCE(sum(NULLIF(row_data->>'standard_eligible_amount','')::numeric),0) AS eligible_discount_amount
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='discount_applications'
    GROUP BY 1
),
journey_metrics AS (
    SELECT j.journey_id, j.dealer_id, j.outlet_id,
           COALESCE(f.finding_count,0) AS finding_count,
           COALESCE(f.open_finding_count,0) AS open_finding_count,
           COALESCE(f.high_finding_count,0) AS high_finding_count,
           COALESCE(f.missing_document_flag_count,0) AS missing_document_flag_count,
           (COALESCE(f.finding_count,0) > 0) AS has_findings,
           (COALESCE(f.missing_document_flag_count,0) > 0) AS has_missing_documents,
           (fin.journey_id IS NOT NULL) AS has_finance,
           (ins.journey_id IS NOT NULL) AS has_insurance,
           (tr.journey_id IS NOT NULL) AS has_trade_in,
           COALESCE(a.has_ew,false) AS has_ew,
           COALESCE(a.has_rsa,false) AS has_rsa,
           COALESCE(a.has_accessories,false) AS has_accessories,
           COALESCE(b.corporate_discount,false) AS has_corporate_discount,
           COALESCE(b.gst_benefit,false) AS has_gst_benefit,
           COALESCE(b.exchange_discount,false) AS has_exchange_discount,
           COALESCE(p.payment_amount,0) AS payment_amount,
           COALESCE(d.actual_discount_amount,0) AS actual_discount_amount,
           COALESCE(d.eligible_discount_amount,0) AS eligible_discount_amount
    FROM journeys j
    LEFT JOIN findings f USING (journey_id)
    LEFT JOIN finance fin USING (journey_id)
    LEFT JOIN insurance ins USING (journey_id)
    LEFT JOIN trade_in tr USING (journey_id)
    LEFT JOIN addons a USING (journey_id)
    LEFT JOIN bookings b USING (journey_id)
    LEFT JOIN payments p USING (journey_id)
    LEFT JOIN discounts d USING (journey_id)
),
dealer_rows AS (
    SELECT 'DEALER'::text AS scope_level,
           d.dealer_id,
           d.dealer_name,
           NULL::text AS outlet_id,
           NULL::text AS outlet_name,
           NULL::text AS city,
           NULL::text AS state_region,
           count(jm.journey_id)::int AS journey_count,
           count(jm.journey_id) FILTER (WHERE jm.has_findings)::int AS journeys_with_findings,
           COALESCE(sum(jm.finding_count),0)::int AS finding_count,
           COALESCE(sum(jm.open_finding_count),0)::int AS open_finding_count,
           COALESCE(sum(jm.high_finding_count),0)::int AS high_finding_count,
           COALESCE(sum(jm.missing_document_flag_count),0)::int AS missing_document_flag_count,
           count(jm.journey_id) FILTER (WHERE jm.has_missing_documents)::int AS journeys_with_missing_documents,
           count(jm.journey_id) FILTER (WHERE jm.has_finance)::int AS finance_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_insurance)::int AS insurance_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_trade_in)::int AS trade_in_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_ew)::int AS ew_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_rsa)::int AS rsa_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_accessories)::int AS accessory_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_corporate_discount)::int AS corporate_discount_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_gst_benefit)::int AS gst_benefit_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_exchange_discount)::int AS exchange_discount_journeys,
           COALESCE(sum(jm.payment_amount),0) AS payment_amount,
           COALESCE(sum(jm.actual_discount_amount),0) AS actual_discount_amount,
           COALESCE(sum(jm.eligible_discount_amount),0) AS eligible_discount_amount
    FROM configured_dealers d
    LEFT JOIN journey_metrics jm ON jm.dealer_id=d.dealer_id
    GROUP BY d.dealer_id,d.dealer_name
),
outlet_rows AS (
    SELECT 'OUTLET'::text AS scope_level,
           o.dealer_id,
           d.dealer_name,
           o.outlet_id,
           o.outlet_name,
           o.city,
           o.state_region,
           count(jm.journey_id)::int AS journey_count,
           count(jm.journey_id) FILTER (WHERE jm.has_findings)::int AS journeys_with_findings,
           COALESCE(sum(jm.finding_count),0)::int AS finding_count,
           COALESCE(sum(jm.open_finding_count),0)::int AS open_finding_count,
           COALESCE(sum(jm.high_finding_count),0)::int AS high_finding_count,
           COALESCE(sum(jm.missing_document_flag_count),0)::int AS missing_document_flag_count,
           count(jm.journey_id) FILTER (WHERE jm.has_missing_documents)::int AS journeys_with_missing_documents,
           count(jm.journey_id) FILTER (WHERE jm.has_finance)::int AS finance_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_insurance)::int AS insurance_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_trade_in)::int AS trade_in_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_ew)::int AS ew_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_rsa)::int AS rsa_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_accessories)::int AS accessory_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_corporate_discount)::int AS corporate_discount_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_gst_benefit)::int AS gst_benefit_journeys,
           count(jm.journey_id) FILTER (WHERE jm.has_exchange_discount)::int AS exchange_discount_journeys,
           COALESCE(sum(jm.payment_amount),0) AS payment_amount,
           COALESCE(sum(jm.actual_discount_amount),0) AS actual_discount_amount,
           COALESCE(sum(jm.eligible_discount_amount),0) AS eligible_discount_amount
    FROM configured_outlets o
    LEFT JOIN configured_dealers d ON d.dealer_id=o.dealer_id
    LEFT JOIN journey_metrics jm ON jm.outlet_id=o.outlet_id
    GROUP BY o.dealer_id,d.dealer_name,o.outlet_id,o.outlet_name,o.city,o.state_region
)
SELECT * FROM dealer_rows
UNION ALL
SELECT * FROM outlet_rows
ORDER BY scope_level, dealer_name, outlet_name NULLS FIRST
"""


_COUNT_FIELDS = (
    "journey_count",
    "journeys_with_findings",
    "finding_count",
    "open_finding_count",
    "high_finding_count",
    "missing_document_flag_count",
    "journeys_with_missing_documents",
    "finance_journeys",
    "insurance_journeys",
    "trade_in_journeys",
    "ew_journeys",
    "rsa_journeys",
    "accessory_journeys",
    "corporate_discount_journeys",
    "gst_benefit_journeys",
    "exchange_discount_journeys",
)

_AMOUNT_FIELDS = ("payment_amount", "actual_discount_amount", "eligible_discount_amount")


def _rate(part: int, whole: int) -> float:
    return round((part / whole) * 100.0, 1) if whole else 0.0


def _enrich(row: dict) -> dict:
    result = dict(row)
    for key in _COUNT_FIELDS:
        result[key] = int(result.get(key) or 0)
    for key in _AMOUNT_FIELDS:
        result[key] = float(result.get(key) or 0)

    journeys = result["journey_count"]
    result.update(
        {
            "journeys_with_findings_pct": _rate(result["journeys_with_findings"], journeys),
            "journeys_with_missing_documents_pct": _rate(result["journeys_with_missing_documents"], journeys),
            "finance_penetration_pct": _rate(result["finance_journeys"], journeys),
            "insurance_penetration_pct": _rate(result["insurance_journeys"], journeys),
            "trade_in_penetration_pct": _rate(result["trade_in_journeys"], journeys),
            "ew_penetration_pct": _rate(result["ew_journeys"], journeys),
            "rsa_penetration_pct": _rate(result["rsa_journeys"], journeys),
            "accessory_penetration_pct": _rate(result["accessory_journeys"], journeys),
            "corporate_discount_penetration_pct": _rate(result["corporate_discount_journeys"], journeys),
            "gst_benefit_penetration_pct": _rate(result["gst_benefit_journeys"], journeys),
            "exchange_discount_penetration_pct": _rate(result["exchange_discount_journeys"], journeys),
        }
    )
    return result


def _project_summary(dealers: list[dict], outlets: list[dict]) -> dict:
    totals = {key: 0 for key in _COUNT_FIELDS}
    amounts = {key: 0.0 for key in _AMOUNT_FIELDS}
    for row in dealers:
        for key in _COUNT_FIELDS:
            totals[key] += int(row.get(key) or 0)
        for key in _AMOUNT_FIELDS:
            amounts[key] += float(row.get(key) or 0)

    journeys = totals["journey_count"]
    return {
        "dealer_count": len(dealers),
        "outlet_count": len(outlets),
        "active_dealer_count": sum(1 for row in dealers if row["journey_count"] > 0),
        "active_outlet_count": sum(1 for row in outlets if row["journey_count"] > 0),
        **totals,
        **amounts,
        "journeys_with_findings_pct": _rate(totals["journeys_with_findings"], journeys),
        "journeys_with_missing_documents_pct": _rate(totals["journeys_with_missing_documents"], journeys),
        "finance_penetration_pct": _rate(totals["finance_journeys"], journeys),
        "insurance_penetration_pct": _rate(totals["insurance_journeys"], journeys),
        "trade_in_penetration_pct": _rate(totals["trade_in_journeys"], journeys),
        "ew_penetration_pct": _rate(totals["ew_journeys"], journeys),
        "rsa_penetration_pct": _rate(totals["rsa_journeys"], journeys),
        "accessory_penetration_pct": _rate(totals["accessory_journeys"], journeys),
        "corporate_discount_penetration_pct": _rate(totals["corporate_discount_journeys"], journeys),
        "gst_benefit_penetration_pct": _rate(totals["gst_benefit_journeys"], journeys),
        "exchange_discount_penetration_pct": _rate(totals["exchange_discount_journeys"], journeys),
    }


@router.get("/business-scorecard")
def business_scorecard(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Cross-dealer and outlet scorecard for the latest controlled Analytics snapshot.

    The scorecard intentionally exposes issue rates and penetration rates rather than
    inventing a composite compliance score whose formula has not been baselined.
    """
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        raw_rows = _query_rows(
            connection,
            _SCORECARD_SQL,
            tenant_id=tenant_id,
            dump_id=latest["dump_id"],
        )

    rows = [_enrich(row) for row in raw_rows]
    dealers = [row for row in rows if row["scope_level"] == "DEALER"]
    outlets = [row for row in rows if row["scope_level"] == "OUTLET"]
    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "project_summary": _project_summary(dealers, outlets),
        "dealers": dealers,
        "outlets": outlets,
        "definitions": {
            "journeys_with_findings_pct": "Journeys with one or more audit findings / journeys",
            "journeys_with_missing_documents_pct": "Journeys with one or more missing-document findings / journeys",
            "penetration_pct": "Journeys with the product/benefit / journeys",
            "composite_compliance_score": None,
        },
    }
