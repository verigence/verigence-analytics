from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import Connection

from analytics.db import analytics_engine
from analytics.reports import _latest_dump, _query_rows
from analytics.security import Principal, require_analytics_read

router = APIRouter(
    prefix="/v1/analytics/tenants/{tenant_id}/business-intelligence",
    tags=["business-intelligence"],
)


_DEAL_FACTS_CTE = """
WITH
journeys AS (
    SELECT row_data->>'journey_id' AS journey_id,
           row_data->>'dealer_id' AS dealer_id,
           row_data->>'outlet_id' AS outlet_id,
           row_data->>'customer_id' AS customer_id
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journeys'
),
dealers AS (
    SELECT row_data->>'dealer_id' AS dealer_id,
           COALESCE(
               NULLIF(row_data->>'dealer_name',''),
               NULLIF(row_data->>'dealer_code',''),
               'Unspecified dealer'
           ) AS dealer_name
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='dealers'
),
outlets AS (
    SELECT row_data->>'outlet_id' AS outlet_id,
           row_data->>'dealer_id' AS dealer_id,
           COALESCE(
               NULLIF(row_data->>'outlet_name',''),
               NULLIF(row_data->>'outlet_code',''),
               'Unspecified outlet'
           ) AS outlet_name,
           NULLIF(row_data->>'city','') AS city,
           NULLIF(row_data->>'state_region','') AS state_region
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='dealer_outlets'
),
bookings AS (
    SELECT row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'booking_date','')::date AS booking_date,
           NULLIF(row_data->>'booking_confirmation_date','')::date AS booking_confirmation_date,
           NULLIF(row_data->>'expected_delivery_date','')::date AS expected_delivery_date,
           NULLIF(row_data->>'sales_staff_id','') AS sales_staff_id,
           NULLIF(row_data->>'deal_type_code','') AS deal_type_code,
           NULLIF(row_data->>'deal_source_code','') AS deal_source_code,
           NULLIF(row_data->>'lead_source_code','') AS lead_source_code
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
),
staff AS (
    SELECT row_data->>'dealership_staff_id' AS dealership_staff_id,
           NULLIF(row_data->>'display_name','') AS sales_staff_name,
           NULLIF(row_data->>'staff_role_code','') AS sales_staff_role
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='dealership_staff'
),
products AS (
    SELECT row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'model_name_snapshot','') AS model_name,
           NULLIF(row_data->>'variant_name_snapshot','') AS variant_name,
           NULLIF(row_data->>'colour_name_snapshot','') AS colour_name
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_products'
),
deliveries AS (
    SELECT row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'planned_delivery_at','')::timestamptz AS planned_delivery_at,
           NULLIF(row_data->>'actual_delivered_at','')::timestamptz AS actual_delivered_at,
           NULLIF(row_data->>'actual_delivery_status_code','') AS delivery_status
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='deliveries'
),
vehicles AS (
    SELECT row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'allocated_at_utc','')::timestamptz AS allocated_at_utc
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='vehicle_records'
),
finance AS (
    SELECT row_data->>'journey_id' AS journey_id,
           string_agg(
               DISTINCT NULLIF(row_data->>'provider_name',''),
               ', ' ORDER BY NULLIF(row_data->>'provider_name','')
           ) AS finance_provider,
           string_agg(
               DISTINCT NULLIF(row_data->>'finance_type_code',''),
               ', ' ORDER BY NULLIF(row_data->>'finance_type_code','')
           ) AS finance_type,
           sum(NULLIF(row_data->>'financed_amount','')::numeric) AS financed_amount
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='finance_records'
    GROUP BY 1
),
insurance AS (
    SELECT row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'insurance_by','') AS insurance_by,
           NULLIF(row_data->>'insurer_name','') AS insurer_name,
           NULLIF(row_data->>'actual_premium_amount','')::numeric AS actual_premium_amount,
           NULLIF(row_data->>'standard_premium_amount','')::numeric AS standard_premium_amount,
           NULLIF(row_data->>'self_insurance_flag','')::boolean AS self_insurance_flag,
           row_data->'add_ons' AS insurance_add_ons
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='insurance_records'
),
trade_in AS (
    SELECT row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'quoted_value','')::numeric AS trade_in_quoted_value,
           NULLIF(row_data->>'actual_value','')::numeric AS trade_in_actual_value,
           NULLIF(row_data->>'handover_at_utc','')::timestamptz AS trade_in_handover_at,
           NULLIF(row_data->>'payment_at_utc','')::timestamptz AS trade_in_payment_at,
           NULLIF(row_data->>'resale_at_utc','')::timestamptz AS trade_in_resale_at
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='trade_in_cases'
),
addons AS (
    SELECT row_data->>'journey_id' AS journey_id,
           bool_or(upper(COALESCE(row_data->>'addon_type_code',''))='EW') AS has_ew,
           bool_or(upper(COALESCE(row_data->>'addon_type_code',''))='RSA') AS has_rsa,
           bool_or(
               upper(COALESCE(row_data->>'addon_type_code','')) IN (
                   'ACCESSORY', 'ACCESSORIES', 'ACCESSORIES_TOTAL'
               )
           ) AS has_accessories,
           sum(
               NULLIF(row_data->>'actual_amount','')::numeric
           ) FILTER (
               WHERE upper(COALESCE(row_data->>'addon_type_code',''))='EW'
           ) AS ew_value,
           sum(
               NULLIF(row_data->>'actual_amount','')::numeric
           ) FILTER (
               WHERE upper(COALESCE(row_data->>'addon_type_code',''))='RSA'
           ) AS rsa_value,
           sum(
               NULLIF(row_data->>'actual_amount','')::numeric
           ) FILTER (
               WHERE upper(COALESCE(row_data->>'addon_type_code','')) IN (
                   'ACCESSORY', 'ACCESSORIES', 'ACCESSORIES_TOTAL'
               )
           ) AS accessories_value
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_addons'
    GROUP BY 1
),
discounts AS (
    SELECT row_data->>'journey_id' AS journey_id,
           sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) AS actual_discount_amount,
           sum(NULLIF(row_data->>'standard_eligible_amount','')::numeric) AS eligible_discount_amount,
           bool_or(NULLIF(row_data->>'actual_discount_amount','') IS NOT NULL) AS actual_present,
           bool_or(NULLIF(row_data->>'standard_eligible_amount','') IS NOT NULL) AS eligible_present
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id
      AND source_table='discount_applications'
    GROUP BY 1
),
payments AS (
    SELECT row_data->>'journey_id' AS journey_id,
           count(*)::int AS payment_count,
           sum(NULLIF(row_data->>'amount','')::numeric) AS payment_amount,
           min(
               COALESCE(
                   NULLIF(row_data->>'receipt_date','')::timestamp,
                   NULLIF(row_data->>'payment_at_utc','')::timestamptz
               )
           ) AS first_payment_at
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='payments'
    GROUP BY 1
),
findings AS (
    SELECT row_data->>'journey_id' AS journey_id,
           count(*)::int AS finding_count,
           count(*) FILTER (
               WHERE upper(COALESCE(row_data->>'finding_status',''))='OPEN'
           )::int AS open_finding_count,
           count(*) FILTER (
               WHERE upper(COALESCE(row_data->>'severity',''))='HIGH'
           )::int AS high_finding_count,
           count(*) FILTER (
               WHERE lower(COALESCE(row_data->>'title','')) LIKE '%document%missing%'
                  OR lower(COALESCE(row_data->>'description','')) LIKE '%document%missing%'
                  OR lower(COALESCE(row_data->>'rule_key','')) LIKE '%document%missing%'
           )::int AS missing_document_flag_count
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='audit_findings'
    GROUP BY 1
),
geography AS (
    SELECT row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'customer_pincode','') AS customer_pincode,
           NULLIF(row_data->>'source_field_key','') AS geography_source_field
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id
      AND source_table='derived_customer_geography'
),
deal_facts AS (
    SELECT j.journey_id,
           j.dealer_id,
           COALESCE(d.dealer_name, 'Unspecified dealer') AS dealer_name,
           j.outlet_id,
           COALESCE(o.outlet_name, 'Unspecified outlet') AS outlet_name,
           o.city,
           o.state_region,
           b.booking_date,
           b.booking_confirmation_date,
           b.expected_delivery_date,
           b.deal_type_code,
           b.deal_source_code,
           b.lead_source_code,
           b.sales_staff_id,
           s.sales_staff_name,
           s.sales_staff_role,
           p.model_name,
           p.variant_name,
           p.colour_name,
           (dl.journey_id IS NOT NULL) AS has_delivery_record,
           dl.delivery_status,
           dl.planned_delivery_at,
           dl.actual_delivered_at,
           v.allocated_at_utc,
           (f.journey_id IS NOT NULL) AS has_finance,
           f.finance_provider,
           f.finance_type,
           f.financed_amount,
           (i.journey_id IS NOT NULL) AS has_insurance,
           i.insurance_by,
           i.insurer_name,
           i.actual_premium_amount,
           i.standard_premium_amount,
           i.self_insurance_flag,
           i.insurance_add_ons,
           (t.journey_id IS NOT NULL) AS has_trade_in,
           t.trade_in_quoted_value,
           t.trade_in_actual_value,
           t.trade_in_handover_at,
           t.trade_in_payment_at,
           t.trade_in_resale_at,
           COALESCE(a.has_ew,false) AS has_ew,
           COALESCE(a.has_rsa,false) AS has_rsa,
           COALESCE(a.has_accessories,false) AS has_accessories,
           a.ew_value,
           a.rsa_value,
           a.accessories_value,
           COALESCE(di.actual_discount_amount,0) AS actual_discount_amount,
           COALESCE(di.eligible_discount_amount,0) AS eligible_discount_amount,
           COALESCE(di.actual_present,false) AS actual_discount_present,
           COALESCE(di.eligible_present,false) AS eligible_discount_present,
           COALESCE(py.payment_count,0) AS payment_count,
           COALESCE(py.payment_amount,0) AS payment_amount,
           py.first_payment_at,
           COALESCE(fi.finding_count,0) AS finding_count,
           COALESCE(fi.open_finding_count,0) AS open_finding_count,
           COALESCE(fi.high_finding_count,0) AS high_finding_count,
           COALESCE(fi.missing_document_flag_count,0) AS missing_document_flag_count,
           g.customer_pincode,
           g.geography_source_field
    FROM journeys j
    LEFT JOIN dealers d ON d.dealer_id=j.dealer_id
    LEFT JOIN outlets o ON o.outlet_id=j.outlet_id
    LEFT JOIN bookings b ON b.journey_id=j.journey_id
    LEFT JOIN staff s ON s.dealership_staff_id=b.sales_staff_id
    LEFT JOIN products p ON p.journey_id=j.journey_id
    LEFT JOIN deliveries dl ON dl.journey_id=j.journey_id
    LEFT JOIN vehicles v ON v.journey_id=j.journey_id
    LEFT JOIN finance f ON f.journey_id=j.journey_id
    LEFT JOIN insurance i ON i.journey_id=j.journey_id
    LEFT JOIN trade_in t ON t.journey_id=j.journey_id
    LEFT JOIN addons a ON a.journey_id=j.journey_id
    LEFT JOIN discounts di ON di.journey_id=j.journey_id
    LEFT JOIN payments py ON py.journey_id=j.journey_id
    LEFT JOIN findings fi ON fi.journey_id=j.journey_id
    LEFT JOIN geography g ON g.journey_id=j.journey_id
)
"""


def _rows(
    connection: Connection,
    latest: dict,
    tenant_id: str,
    sql: str,
) -> list[dict]:
    return _query_rows(
        connection,
        sql,
        tenant_id=tenant_id,
        dump_id=latest["dump_id"],
    )


def _scalar_row(rows: list[dict]) -> dict:
    return rows[0] if rows else {}


def _rate(part: int | float, whole: int | float) -> float | None:
    if not whole:
        return None
    return round((float(part) / float(whole)) * 100.0, 1)


def _coverage_metric(available: int, total: int) -> dict:
    return {
        "available": int(available),
        "total": int(total),
        "coverage_pct": _rate(available, total),
    }


def _with_penetration(rows: list[dict], *, denominator: int, count_key: str) -> list[dict]:
    enriched: list[dict] = []
    for row in rows:
        item = dict(row)
        item["penetration_pct"] = _rate(int(item.get(count_key) or 0), denominator)
        enriched.append(item)
    return enriched


def _latest_context(tenant_id: str) -> tuple[Connection, dict]:
    connection = analytics_engine().connect()
    try:
        latest = _latest_dump(connection, tenant_id)
    except Exception:
        connection.close()
        raise
    return connection, latest


@router.get("/overview")
def business_overview(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                _DEAL_FACTS_CTE
                + """
                SELECT count(*)::int AS journeys,
                       count(*) FILTER (WHERE has_delivery_record)::int AS delivery_records,
                       count(*) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                           AS actual_deliveries,
                       count(*) FILTER (WHERE has_finance)::int AS finance_journeys,
                       count(*) FILTER (WHERE has_insurance)::int AS insurance_journeys,
                       count(*) FILTER (WHERE has_trade_in)::int AS trade_in_journeys,
                       count(*) FILTER (WHERE has_accessories)::int AS accessory_journeys,
                       count(*) FILTER (WHERE has_ew)::int AS ew_journeys,
                       count(*) FILTER (WHERE has_rsa)::int AS rsa_journeys,
                       count(*) FILTER (WHERE finding_count > 0)::int
                           AS journeys_with_findings,
                       count(*) FILTER (WHERE missing_document_flag_count > 0)::int
                           AS journeys_with_missing_documents,
                       COALESCE(sum(payment_amount),0) AS payment_amount,
                       COALESCE(sum(actual_discount_amount),0) AS discount_amount,
                       COALESCE(sum(actual_premium_amount),0) AS insurance_premium_amount,
                       COALESCE(sum(accessories_value),0) AS accessories_value,
                       COALESCE(sum(rsa_value),0) AS rsa_value,
                       COALESCE(sum(ew_value),0) AS ew_value
                FROM deal_facts
                """,
            )
        )
        total = int(summary.get("journeys") or 0)
        summary.update(
            {
                "finance_penetration_pct": _rate(summary.get("finance_journeys") or 0, total),
                "insurance_penetration_pct": _rate(
                    summary.get("insurance_journeys") or 0, total
                ),
                "trade_in_penetration_pct": _rate(
                    summary.get("trade_in_journeys") or 0, total
                ),
                "accessory_penetration_pct": _rate(
                    summary.get("accessory_journeys") or 0, total
                ),
                "ew_penetration_pct": _rate(summary.get("ew_journeys") or 0, total),
                "rsa_penetration_pct": _rate(summary.get("rsa_journeys") or 0, total),
                "journeys_with_findings_pct": _rate(
                    summary.get("journeys_with_findings") or 0, total
                ),
                "journeys_with_missing_documents_pct": _rate(
                    summary.get("journeys_with_missing_documents") or 0, total
                ),
            }
        )

        top_models = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(model_name,'Unresolved') AS model_name,
                   count(*)::int AS journey_count,
                   count(*) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                       AS actual_deliveries,
                   COALESCE(sum(actual_discount_amount),0) AS discount_amount
            FROM deal_facts
            GROUP BY 1
            ORDER BY journey_count DESC, model_name
            LIMIT 10
            """,
        )
        top_outlets = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_id, dealer_name, outlet_id, outlet_name,
                   count(*)::int AS journey_count,
                   count(*) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                       AS actual_deliveries,
                   count(*) FILTER (WHERE finding_count > 0)::int
                       AS journeys_with_findings,
                   count(*) FILTER (WHERE has_finance)::int AS finance_journeys,
                   count(*) FILTER (WHERE has_insurance)::int AS insurance_journeys,
                   count(*) FILTER (WHERE has_accessories)::int AS accessory_journeys
            FROM deal_facts
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name
            ORDER BY journey_count DESC, dealer_name, outlet_name
            """,
        )
        for row in top_outlets:
            journeys = int(row.get("journey_count") or 0)
            row["finding_rate_pct"] = _rate(row.get("journeys_with_findings") or 0, journeys)
            row["finance_penetration_pct"] = _rate(row.get("finance_journeys") or 0, journeys)
            row["insurance_penetration_pct"] = _rate(
                row.get("insurance_journeys") or 0, journeys
            )
            row["accessory_penetration_pct"] = _rate(
                row.get("accessory_journeys") or 0, journeys
            )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "top_models": top_models,
        "outlets": top_outlets,
    }


@router.get("/coverage")
def business_coverage(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        row = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                _DEAL_FACTS_CTE
                + """
                SELECT count(*)::int AS journeys,
                       count(*) FILTER (WHERE booking_date IS NOT NULL)::int AS booking_date,
                       count(*) FILTER (WHERE model_name IS NOT NULL)::int AS model,
                       count(*) FILTER (WHERE variant_name IS NOT NULL)::int AS variant,
                       count(*) FILTER (WHERE has_delivery_record)::int AS delivery_record,
                       count(*) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                           AS actual_delivery_date,
                       count(*) FILTER (WHERE allocated_at_utc IS NOT NULL)::int
                           AS allocation_date,
                       count(*) FILTER (WHERE customer_pincode IS NOT NULL)::int
                           AS customer_pincode,
                       count(*) FILTER (WHERE has_finance)::int AS finance,
                       count(*) FILTER (WHERE has_insurance)::int AS insurance,
                       count(*) FILTER (
                           WHERE insurance_add_ons IS NOT NULL
                             AND jsonb_typeof(insurance_add_ons)='array'
                             AND jsonb_array_length(insurance_add_ons) > 0
                       )::int AS insurance_add_ons,
                       count(*) FILTER (WHERE actual_discount_present)::int AS actual_discount,
                       count(*) FILTER (WHERE eligible_discount_present)::int
                           AS eligible_discount
                FROM deal_facts
                """,
            )
        )
    finally:
        connection.close()

    total = int(row.get("journeys") or 0)
    metrics = {
        key: _coverage_metric(int(row.get(key) or 0), total)
        for key in (
            "booking_date",
            "model",
            "variant",
            "delivery_record",
            "actual_delivery_date",
            "allocation_date",
            "customer_pincode",
            "finance",
            "insurance",
            "insurance_add_ons",
            "actual_discount",
            "eligible_discount",
        )
    }
    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "journeys": total,
        "metrics": metrics,
        "interpretation": "Missing data is unavailable, not zero performance.",
    }


@router.get("/sales-product")
def sales_product(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_id, dealer_name, outlet_id, outlet_name,
                   count(*)::int AS journey_count,
                   count(*) FILTER (WHERE has_delivery_record)::int AS delivery_records,
                   count(*) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                       AS actual_deliveries,
                   COALESCE(sum(payment_amount),0) AS payment_amount
            FROM deal_facts
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name
            ORDER BY journey_count DESC, dealer_name, outlet_name
            """,
        )
        by_model = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(model_name,'Unresolved') AS model_name,
                   COALESCE(variant_name,'Unresolved') AS variant_name,
                   count(*)::int AS journey_count,
                   count(*) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                       AS actual_deliveries,
                   COALESCE(sum(actual_discount_amount),0) AS discount_amount,
                   COALESCE(sum(payment_amount),0) AS payment_amount
            FROM deal_facts
            GROUP BY 1,2
            ORDER BY journey_count DESC, model_name, variant_name
            """,
        )
        by_colour = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(colour_name,'Unresolved') AS colour_name,
                   count(*)::int AS journey_count
            FROM deal_facts
            GROUP BY 1
            ORDER BY journey_count DESC, colour_name
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "by_outlet": by_outlet,
        "by_model_variant": by_model,
        "by_colour": by_colour,
        "delivery_definition": {
            "delivery_records": "Journeys with a delivery record",
            "actual_deliveries": "Journeys with actual_delivered_at populated",
        },
    }


@router.get("/delivery")
def delivery_business(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                _DEAL_FACTS_CTE
                + """
                SELECT count(*) FILTER (
                           WHERE booking_date IS NOT NULL
                             AND actual_delivered_at IS NOT NULL
                             AND actual_delivered_at::date >= booking_date
                       )::int AS booking_delivery_pairs,
                       round(avg((actual_delivered_at::date - booking_date)) FILTER (
                           WHERE booking_date IS NOT NULL
                             AND actual_delivered_at IS NOT NULL
                             AND actual_delivered_at::date >= booking_date
                       ),2) AS avg_booking_to_delivery_days,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY (actual_delivered_at::date - booking_date)
                       ) FILTER (
                           WHERE booking_date IS NOT NULL
                             AND actual_delivered_at IS NOT NULL
                             AND actual_delivered_at::date >= booking_date
                       ) AS median_booking_to_delivery_days,
                       percentile_cont(0.75) WITHIN GROUP (
                           ORDER BY (actual_delivered_at::date - booking_date)
                       ) FILTER (
                           WHERE booking_date IS NOT NULL
                             AND actual_delivered_at IS NOT NULL
                             AND actual_delivered_at::date >= booking_date
                       ) AS p75_booking_to_delivery_days,
                       percentile_cont(0.9) WITHIN GROUP (
                           ORDER BY (actual_delivered_at::date - booking_date)
                       ) FILTER (
                           WHERE booking_date IS NOT NULL
                             AND actual_delivered_at IS NOT NULL
                             AND actual_delivered_at::date >= booking_date
                       ) AS p90_booking_to_delivery_days,
                       count(*) FILTER (
                           WHERE planned_delivery_at IS NOT NULL
                             AND actual_delivered_at IS NOT NULL
                       )::int AS planned_actual_pairs,
                       count(*) FILTER (
                           WHERE planned_delivery_at IS NOT NULL
                             AND actual_delivered_at IS NOT NULL
                             AND actual_delivered_at <= planned_delivery_at
                       )::int AS on_time_deliveries,
                       count(*) FILTER (
                           WHERE booking_date IS NOT NULL
                             AND allocated_at_utc IS NOT NULL
                             AND allocated_at_utc::date >= booking_date
                       )::int AS booking_allocation_pairs,
                       round(avg((allocated_at_utc::date - booking_date)) FILTER (
                           WHERE booking_date IS NOT NULL
                             AND allocated_at_utc IS NOT NULL
                             AND allocated_at_utc::date >= booking_date
                       ),2) AS avg_booking_to_allocation_days
                FROM deal_facts
                """,
            )
        )
        summary["on_time_delivery_pct"] = _rate(
            summary.get("on_time_deliveries") or 0,
            summary.get("planned_actual_pairs") or 0,
        )
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_name, outlet_name,
                   count(*) FILTER (
                       WHERE booking_date IS NOT NULL
                         AND actual_delivered_at IS NOT NULL
                         AND actual_delivered_at::date >= booking_date
                   )::int AS completed_count,
                   round(avg((actual_delivered_at::date - booking_date)) FILTER (
                       WHERE booking_date IS NOT NULL
                         AND actual_delivered_at IS NOT NULL
                         AND actual_delivered_at::date >= booking_date
                   ),2) AS avg_days,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY (actual_delivered_at::date - booking_date)
                   ) FILTER (
                       WHERE booking_date IS NOT NULL
                         AND actual_delivered_at IS NOT NULL
                         AND actual_delivered_at::date >= booking_date
                   ) AS median_days
            FROM deal_facts
            GROUP BY dealer_name, outlet_name
            ORDER BY completed_count DESC, dealer_name, outlet_name
            """,
        )
        by_model = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(model_name,'Unresolved') AS model_name,
                   count(*) FILTER (
                       WHERE booking_date IS NOT NULL
                         AND actual_delivered_at IS NOT NULL
                         AND actual_delivered_at::date >= booking_date
                   )::int AS completed_count,
                   round(avg((actual_delivered_at::date - booking_date)) FILTER (
                       WHERE booking_date IS NOT NULL
                         AND actual_delivered_at IS NOT NULL
                         AND actual_delivered_at::date >= booking_date
                   ),2) AS avg_days,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY (actual_delivered_at::date - booking_date)
                   ) FILTER (
                       WHERE booking_date IS NOT NULL
                         AND actual_delivered_at IS NOT NULL
                         AND actual_delivered_at::date >= booking_date
                   ) AS median_days
            FROM deal_facts
            GROUP BY 1
            ORDER BY completed_count DESC, model_name
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "by_outlet": by_outlet,
        "by_model": by_model,
        "definition": "Booking date to actual vehicle delivery date; not audit-process TAT.",
    }


@router.get("/discounts")
def discount_intelligence(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                _DEAL_FACTS_CTE
                + """
                SELECT count(*)::int AS journeys,
                       count(*) FILTER (WHERE actual_discount_present)::int
                           AS journeys_with_actual_discount,
                       COALESCE(sum(actual_discount_amount),0) AS total_actual_discount,
                       round(avg(actual_discount_amount) FILTER (
                           WHERE actual_discount_present
                       ),2) AS avg_discount_per_discounted_journey,
                       round(avg(actual_discount_amount),2) AS avg_discount_per_journey,
                       round(avg(actual_discount_amount) FILTER (
                           WHERE actual_delivered_at IS NOT NULL
                       ),2) AS avg_discount_per_actual_delivery,
                       COALESCE(sum(
                           GREATEST(actual_discount_amount-eligible_discount_amount,0)
                       ) FILTER (
                           WHERE actual_discount_present AND eligible_discount_present
                       ),0) AS above_eligible_discount_amount,
                       count(*) FILTER (
                           WHERE actual_discount_present
                             AND eligible_discount_present
                             AND actual_discount_amount > eligible_discount_amount
                       )::int AS above_eligible_journeys
                FROM deal_facts
                """,
            )
        )
        by_model = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(model_name,'Unresolved') AS model_name,
                   count(*)::int AS journeys,
                   count(*) FILTER (WHERE actual_discount_present)::int
                       AS discounted_journeys,
                   COALESCE(sum(actual_discount_amount),0) AS total_actual_discount,
                   round(avg(actual_discount_amount) FILTER (
                       WHERE actual_discount_present
                   ),2) AS avg_discount_per_discounted_journey
            FROM deal_facts
            GROUP BY 1
            ORDER BY total_actual_discount DESC, model_name
            """,
        )
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_name, outlet_name,
                   count(*)::int AS journeys,
                   count(*) FILTER (WHERE actual_discount_present)::int
                       AS discounted_journeys,
                   COALESCE(sum(actual_discount_amount),0) AS total_actual_discount,
                   round(avg(actual_discount_amount) FILTER (
                       WHERE actual_discount_present
                   ),2) AS avg_discount_per_discounted_journey
            FROM deal_facts
            GROUP BY dealer_name, outlet_name
            ORDER BY total_actual_discount DESC, dealer_name, outlet_name
            """,
        )
        by_key = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT COALESCE(row_data->>'discount_key','Unspecified') AS discount_key,
                   count(*)::int AS application_count,
                   count(DISTINCT row_data->>'journey_id')::int AS journey_count,
                   COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric),0)
                       AS actual_discount_amount,
                   COALESCE(sum(NULLIF(row_data->>'standard_eligible_amount','')::numeric),0)
                       AS eligible_discount_amount
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id
              AND source_table='discount_applications'
            GROUP BY 1
            ORDER BY actual_discount_amount DESC, discount_key
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "by_model": by_model,
        "by_outlet": by_outlet,
        "by_discount_key": by_key,
        "above_eligible_definition": (
            "Computed only where both actual and eligible discount values are populated."
        ),
    }


@router.get("/insurance")
def insurance_intelligence(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                _DEAL_FACTS_CTE
                + """
                SELECT count(*)::int AS journeys,
                       count(*) FILTER (WHERE has_insurance)::int AS insurance_journeys,
                       count(*) FILTER (
                           WHERE has_insurance AND actual_premium_amount IS NOT NULL
                       )::int AS premium_populated,
                       COALESCE(sum(actual_premium_amount),0) AS premium_amount,
                       round(avg(actual_premium_amount) FILTER (
                           WHERE actual_premium_amount IS NOT NULL
                       ),2) AS avg_premium,
                       count(*) FILTER (
                           WHERE has_insurance AND self_insurance_flag IS TRUE
                       )::int AS self_insurance_journeys,
                       count(*) FILTER (
                           WHERE insurance_add_ons IS NOT NULL
                             AND jsonb_typeof(insurance_add_ons)='array'
                             AND jsonb_array_length(insurance_add_ons)>0
                       )::int AS policies_with_add_ons
                FROM deal_facts
                """,
            )
        )
        total_journeys = int(summary.get("journeys") or 0)
        insurance_journeys = int(summary.get("insurance_journeys") or 0)
        summary["insurance_penetration_pct"] = _rate(insurance_journeys, total_journeys)
        summary["add_on_policy_attach_pct"] = _rate(
            summary.get("policies_with_add_ons") or 0,
            insurance_journeys,
        )

        by_insurer = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(insurer_name,'Unspecified') AS insurer_name,
                   count(*) FILTER (WHERE has_insurance)::int AS policy_count,
                   count(*) FILTER (
                       WHERE has_insurance AND actual_premium_amount IS NOT NULL
                   )::int AS premium_populated,
                   COALESCE(sum(actual_premium_amount),0) AS premium_amount,
                   round(avg(actual_premium_amount) FILTER (
                       WHERE actual_premium_amount IS NOT NULL
                   ),2) AS avg_premium
            FROM deal_facts
            WHERE has_insurance
            GROUP BY 1
            ORDER BY policy_count DESC, insurer_name
            """,
        )
        by_source = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(insurance_by,'Unspecified') AS insurance_by,
                   count(*)::int AS policy_count
            FROM deal_facts
            WHERE has_insurance
            GROUP BY 1
            ORDER BY policy_count DESC, insurance_by
            """,
        )
        add_ons = _rows(
            connection,
            latest,
            tenant_id,
            """
            WITH policies AS (
                SELECT row_data->>'journey_id' AS journey_id,
                       row_data->'add_ons' AS add_ons
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id
                  AND source_table='insurance_records'
            )
            SELECT add_on.add_on_name,
                   count(DISTINCT p.journey_id)::int AS policy_count
            FROM policies p
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(p.add_ons)='array' THEN p.add_ons
                    ELSE '[]'::jsonb
                END
            ) AS add_on(add_on_name)
            GROUP BY add_on.add_on_name
            ORDER BY policy_count DESC, add_on.add_on_name
            """,
        )
        for row in add_ons:
            count = int(row.get("policy_count") or 0)
            row["policy_attach_pct"] = _rate(count, insurance_journeys)
            row["journey_penetration_pct"] = _rate(count, total_journeys)
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "by_insurer": by_insurer,
        "by_source": by_source,
        "add_ons": add_ons,
    }


@router.get("/vas")
def vas_intelligence(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        total_row = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                """
                SELECT count(*)::int AS journeys
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journeys'
                """,
            )
        )
        total_journeys = int(total_row.get("journeys") or 0)
        by_type = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT upper(COALESCE(row_data->>'addon_type_code','UNSPECIFIED')) AS addon_type,
                   count(*)::int AS record_count,
                   count(DISTINCT row_data->>'journey_id')::int AS journey_count,
                   COALESCE(sum(NULLIF(row_data->>'actual_amount','')::numeric),0)
                       AS actual_amount,
                   round(avg(NULLIF(row_data->>'actual_amount','')::numeric),2)
                       AS avg_record_amount
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_addons'
            GROUP BY 1
            ORDER BY journey_count DESC, addon_type
            """,
        )
        by_type = _with_penetration(
            by_type,
            denominator=total_journeys,
            count_key="journey_count",
        )
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            """
            WITH journeys AS (
                SELECT row_data->>'journey_id' AS journey_id,
                       row_data->>'dealer_id' AS dealer_id,
                       row_data->>'outlet_id' AS outlet_id
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journeys'
            ),
            dealers AS (
                SELECT row_data->>'dealer_id' AS dealer_id,
                       COALESCE(row_data->>'dealer_name','Unspecified dealer') AS dealer_name
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='dealers'
            ),
            outlets AS (
                SELECT row_data->>'outlet_id' AS outlet_id,
                       COALESCE(row_data->>'outlet_name','Unspecified outlet') AS outlet_name
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id
                  AND source_table='dealer_outlets'
            ),
            addon_rows AS (
                SELECT row_data->>'journey_id' AS journey_id,
                       upper(COALESCE(row_data->>'addon_type_code','UNSPECIFIED')) AS addon_type,
                       NULLIF(row_data->>'actual_amount','')::numeric AS actual_amount
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id
                  AND source_table='journey_addons'
            )
            SELECT d.dealer_name, o.outlet_name, a.addon_type,
                   count(DISTINCT a.journey_id)::int AS journey_count,
                   COALESCE(sum(a.actual_amount),0) AS actual_amount
            FROM addon_rows a
            JOIN journeys j USING (journey_id)
            LEFT JOIN dealers d ON d.dealer_id=j.dealer_id
            LEFT JOIN outlets o ON o.outlet_id=j.outlet_id
            GROUP BY d.dealer_name, o.outlet_name, a.addon_type
            ORDER BY journey_count DESC, d.dealer_name, o.outlet_name, a.addon_type
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "journeys": total_journeys,
        "by_type": by_type,
        "by_outlet": by_outlet,
        "accessory_codes": ["ACCESSORY", "ACCESSORIES", "ACCESSORIES_TOTAL"],
    }


@router.get("/compliance")
def compliance_intelligence(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_name, outlet_name,
                   count(*)::int AS journeys,
                   count(*) FILTER (WHERE finding_count > 0)::int
                       AS journeys_with_findings,
                   COALESCE(sum(finding_count),0)::int AS finding_count,
                   COALESCE(sum(open_finding_count),0)::int AS open_finding_count,
                   COALESCE(sum(high_finding_count),0)::int AS high_finding_count,
                   count(*) FILTER (WHERE missing_document_flag_count > 0)::int
                       AS journeys_with_missing_documents,
                   COALESCE(sum(actual_discount_amount) FILTER (
                       WHERE finding_count > 0
                   ),0) AS discount_value_on_affected_journeys,
                   COALESCE(sum(payment_amount) FILTER (
                       WHERE finding_count > 0
                   ),0) AS payment_value_on_affected_journeys
            FROM deal_facts
            GROUP BY dealer_name, outlet_name
            ORDER BY journeys_with_findings DESC, finding_count DESC,
                     dealer_name, outlet_name
            """,
        )
        for row in by_outlet:
            total = int(row.get("journeys") or 0)
            row["journeys_with_findings_pct"] = _rate(
                row.get("journeys_with_findings") or 0, total
            )
            row["journeys_with_missing_documents_pct"] = _rate(
                row.get("journeys_with_missing_documents") or 0, total
            )

        by_model = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(model_name,'Unresolved') AS model_name,
                   count(*)::int AS journeys,
                   count(*) FILTER (WHERE finding_count > 0)::int
                       AS journeys_with_findings,
                   COALESCE(sum(finding_count),0)::int AS finding_count,
                   COALESCE(sum(high_finding_count),0)::int AS high_finding_count
            FROM deal_facts
            GROUP BY 1
            ORDER BY journeys_with_findings DESC, finding_count DESC, model_name
            """,
        )
        for row in by_model:
            row["journeys_with_findings_pct"] = _rate(
                row.get("journeys_with_findings") or 0,
                row.get("journeys") or 0,
            )

        top_rules = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT COALESCE(row_data->>'rule_key','UNSPECIFIED') AS rule_key,
                   COALESCE(row_data->>'severity','UNSPECIFIED') AS severity,
                   COALESCE(row_data->>'finding_status','UNSPECIFIED') AS finding_status,
                   count(*)::int AS finding_count,
                   count(DISTINCT row_data->>'journey_id')::int AS journey_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='audit_findings'
            GROUP BY 1,2,3
            ORDER BY finding_count DESC, rule_key
            LIMIT 50
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "by_outlet": by_outlet,
        "by_model": by_model,
        "top_rules": top_rules,
        "commercial_value_note": (
            "Associated values are amounts on affected journeys; they are not automatically loss "
            "or leakage unless a rule-specific financial calculation establishes causality."
        ),
    }


@router.get("/geography")
def customer_geography(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        total = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                _DEAL_FACTS_CTE
                + """
                SELECT count(*)::int AS journeys,
                       count(*) FILTER (WHERE customer_pincode IS NOT NULL)::int
                           AS journeys_with_pincode
                FROM deal_facts
                """,
            )
        )
        rows = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT customer_pincode,
                   dealer_name,
                   outlet_name,
                   COALESCE(model_name,'Unresolved') AS model_name,
                   count(*)::int AS journey_count,
                   count(*) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                       AS actual_deliveries,
                   count(*) FILTER (WHERE has_finance)::int AS finance_journeys,
                   count(*) FILTER (WHERE has_insurance)::int AS insurance_journeys,
                   count(*) FILTER (WHERE has_accessories)::int AS accessory_journeys,
                   round(avg(actual_discount_amount) FILTER (
                       WHERE actual_discount_present
                   ),2) AS avg_discount
            FROM deal_facts
            WHERE customer_pincode IS NOT NULL
            GROUP BY customer_pincode, dealer_name, outlet_name, model_name
            ORDER BY journey_count DESC, customer_pincode, dealer_name, outlet_name, model_name
            """,
        )
    finally:
        connection.close()

    journeys = int(total.get("journeys") or 0)
    with_pin = int(total.get("journeys_with_pincode") or 0)
    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "coverage": _coverage_metric(with_pin, journeys),
        "available": with_pin > 0,
        "rows": rows,
        "privacy": (
            "Analytics stores only the derived PIN and provenance for this use case, not the full "
            "customer address."
        ),
    }


@router.get("/commercial-components")
def commercial_components(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        rows = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT COALESCE(row_data->>'component_key','UNSPECIFIED') AS component_key,
                   count(*)::int AS record_count,
                   count(DISTINCT row_data->>'journey_id')::int AS journey_count,
                   COALESCE(sum(NULLIF(row_data->>'standard_amount','')::numeric),0)
                       AS standard_amount,
                   COALESCE(sum(NULLIF(row_data->>'actual_amount','')::numeric),0)
                       AS actual_amount,
                   round(avg(NULLIF(row_data->>'actual_amount','')::numeric),2)
                       AS avg_actual_amount
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id
              AND source_table='commercial_lines'
            GROUP BY 1
            ORDER BY actual_amount DESC, component_key
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "rows": rows,
        "note": (
            "Components are reported separately. They are not blindly summed into revenue because "
            "the source component semantics may contain overlapping totals/subtotals."
        ),
    }
