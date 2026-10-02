from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text

from analytics.db import analytics_engine
from analytics.reports import _latest_dump
from analytics.security import Principal, require_analytics_read

router = APIRouter(
    prefix="/v1/analytics/tenants/{tenant_id}/business-intelligence",
    tags=["business-intelligence"],
)

_PROJECT_AUDITOR_CTE = """
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
           NULLIF(row_data->>'deal_type_code','') AS deal_type_code
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
),
products AS (
    SELECT row_data->>'journey_id' AS journey_id,
           COALESCE(NULLIF(row_data->>'model_name_snapshot',''), 'Unresolved') AS model_name,
           COALESCE(NULLIF(row_data->>'variant_name_snapshot',''), 'Unresolved') AS variant_name
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
finance AS (
    SELECT row_data->>'journey_id' AS journey_id,
           string_agg(DISTINCT NULLIF(row_data->>'provider_name',''), ', ') AS finance_provider,
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
           COALESCE(NULLIF(row_data->>'insurance_source',''), 'INHOUSE') AS insurance_source
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='insurance_records'
),
trade_in AS (
    SELECT row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'actual_value','')::numeric AS trade_in_actual_value
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='trade_in_cases'
),
addons AS (
    SELECT row_data->>'journey_id' AS journey_id,
           bool_or(upper(COALESCE(row_data->>'addon_type_code',''))='EW') AS has_ew,
           bool_or(upper(COALESCE(row_data->>'addon_type_code',''))='RSA') AS has_rsa,
           bool_or(upper(COALESCE(row_data->>'addon_type_code','')) IN ('ACCESSORY', 'ACCESSORIES', 'ACCESSORIES_TOTAL')) AS has_accessories,
           sum(NULLIF(row_data->>'actual_amount','')::numeric) FILTER (WHERE upper(COALESCE(row_data->>'addon_type_code',''))='EW') AS ew_value,
           sum(NULLIF(row_data->>'actual_amount','')::numeric) FILTER (WHERE upper(COALESCE(row_data->>'addon_type_code',''))='RSA') AS rsa_value,
           sum(NULLIF(row_data->>'actual_amount','')::numeric) FILTER (WHERE upper(COALESCE(row_data->>'addon_type_code','')) IN ('ACCESSORY', 'ACCESSORIES', 'ACCESSORIES_TOTAL')) AS accessories_value
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_addons'
    GROUP BY 1
),
discounts AS (
    SELECT row_data->>'journey_id' AS journey_id,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric),0) AS actual_discount_amount,
           COALESCE(sum(NULLIF(row_data->>'standard_eligible_amount','')::numeric),0) AS eligible_discount_amount,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (WHERE row_data->>'discount_key'='CASH_DISCOUNT'), 0) AS dk_cash_discount,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (WHERE row_data->>'discount_key'='EXCHANGE_BONUS'), 0) AS dk_exchange_bonus,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (WHERE row_data->>'discount_key' IN ('SCRAPPAGE_BONUS_DEALER','SCRAPPAGE_BONUS_COD')), 0) AS dk_scrappage,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (WHERE row_data->>'discount_key'='CORPORATE_PRIVILEGE'), 0) AS dk_corporate,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (WHERE row_data->>'discount_key'='MANAGEMENT_REFERRAL'), 0) AS dk_mr,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (WHERE row_data->>'discount_key' IN ('OTHER_SCHEME','ADDITIONAL_DISCOUNT')), 0) AS dk_other,
           bool_or(
               NULLIF(row_data->>'actual_discount_amount','')::numeric IS NOT NULL
               AND NULLIF(row_data->>'standard_eligible_amount','')::numeric IS NOT NULL
               AND NULLIF(row_data->>'actual_discount_amount','')::numeric > NULLIF(row_data->>'standard_eligible_amount','')::numeric
           ) AS has_excess_discount
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='discount_applications'
    GROUP BY 1
),
payments AS (
    SELECT row_data->>'journey_id' AS journey_id,
           count(*)::int AS payment_count,
           COALESCE(sum(NULLIF(row_data->>'amount','')::numeric),0) AS payment_amount,
           COALESCE(sum(NULLIF(row_data->>'amount','')::numeric) FILTER (WHERE NULLIF(row_data->>'payment_stage','')='BOOKING'), 0) AS booking_payment_amount,
           COALESCE(sum(NULLIF(row_data->>'amount','')::numeric) FILTER (WHERE NULLIF(row_data->>'payment_stage','')='DELIVERY'), 0) AS delivery_payment_amount
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='payments'
    GROUP BY 1
),
findings AS (
    SELECT row_data->>'journey_id' AS journey_id,
           count(*)::int AS finding_count,
           count(*) FILTER (WHERE upper(COALESCE(row_data->>'finding_status',''))='OPEN')::int AS open_finding_count,
           count(*) FILTER (WHERE upper(COALESCE(row_data->>'severity',''))='HIGH')::int AS high_finding_count,
           count(*) FILTER (WHERE lower(COALESCE(row_data->>'title','')) LIKE '%document%missing%'
                              OR lower(COALESCE(row_data->>'description','')) LIKE '%document%missing%'
                              OR lower(COALESCE(row_data->>'rule_key','')) LIKE '%document%missing%')::int AS missing_document_flag_count
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='audit_findings'
    GROUP BY 1
),
geography AS (
    SELECT row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'customer_pincode','') AS customer_pincode
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='derived_customer_geography'
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
           p.model_name,
           p.variant_name,
           (dl.journey_id IS NOT NULL) AS has_delivery_record,
           dl.actual_delivered_at,
           (dl.actual_delivered_at IS NOT NULL) AS is_delivered,
           CASE WHEN b.booking_date IS NOT NULL AND dl.actual_delivered_at IS NOT NULL AND dl.actual_delivered_at::date >= b.booking_date
                THEN (dl.actual_delivered_at::date - b.booking_date)
                ELSE NULL END AS tat_days,
           (f.journey_id IS NOT NULL) AS has_finance,
           f.financed_amount,
           (i.journey_id IS NOT NULL) AS has_insurance,
           i.insurance_by,
           i.insurance_source,
           (i.journey_id IS NOT NULL AND i.insurance_source = 'INHOUSE') AS is_inhouse_insurance,
           (i.journey_id IS NOT NULL AND i.insurance_source != 'INHOUSE') AS is_outside_insurance,
           COALESCE(i.actual_premium_amount, 0) AS insurance_premium_amount,
           (t.journey_id IS NOT NULL) AS has_trade_in,
           COALESCE(t.trade_in_actual_value, 0) AS trade_in_value,
           COALESCE(a.has_ew, false) AS has_ew,
           COALESCE(a.has_rsa, false) AS has_rsa,
           COALESCE(a.has_accessories, false) AS has_accessories,
           COALESCE(a.ew_value, 0) AS ew_value,
           COALESCE(a.rsa_value, 0) AS rsa_value,
           COALESCE(a.accessories_value, 0) AS accessories_value,
           COALESCE(di.actual_discount_amount, 0) AS actual_discount_amount,
           COALESCE(di.eligible_discount_amount, 0) AS eligible_discount_amount,
           GREATEST(COALESCE(di.actual_discount_amount,0) - COALESCE(di.eligible_discount_amount,0), 0) AS excess_discount_amount,
           (COALESCE(di.actual_discount_amount,0) > COALESCE(di.eligible_discount_amount,0)) AS is_excess_discount,
           COALESCE(di.dk_cash_discount, 0) AS dk_cash_discount,
           COALESCE(di.dk_exchange_bonus, 0) AS dk_exchange_bonus,
           COALESCE(di.dk_scrappage, 0) AS dk_scrappage,
           COALESCE(di.dk_corporate, 0) AS dk_corporate,
           COALESCE(di.dk_mr, 0) AS dk_mr,
           COALESCE(di.dk_other, 0) AS dk_other,
           COALESCE(py.payment_count, 0) AS payment_count,
           COALESCE(py.payment_amount, 0) AS payment_amount,
           COALESCE(py.booking_payment_amount, 0) AS booking_payment_amount,
           COALESCE(py.delivery_payment_amount, 0) AS delivery_payment_amount,
           COALESCE(fi.finding_count, 0) AS finding_count,
           COALESCE(fi.open_finding_count, 0) AS open_finding_count,
           COALESCE(fi.high_finding_count, 0) AS high_finding_count,
           COALESCE(fi.missing_document_flag_count, 0) AS missing_document_flag_count,
           g.customer_pincode
    FROM journeys j
    LEFT JOIN dealers d ON d.dealer_id=j.dealer_id
    LEFT JOIN outlets o ON o.outlet_id=j.outlet_id
    LEFT JOIN bookings b ON b.journey_id=j.journey_id
    LEFT JOIN products p ON p.journey_id=j.journey_id
    LEFT JOIN deliveries dl ON dl.journey_id=j.journey_id
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


@router.get("/project-dashboard")
def project_dashboard(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Return unified auditor analytics across dealerships, outlets, models, and pincodes in one high-performance query."""
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        params = {"tenant_id": tenant_id, "dump_id": latest["dump_id"]}

        # 1. Project Overall Summary
        summary_sql = _PROJECT_AUDITOR_CTE + """
        SELECT
            count(*)::int AS total_journeys,
            count(*) FILTER (WHERE is_delivered)::int AS delivered_cars,
            count(*) FILTER (WHERE has_delivery_record)::int AS delivery_records,
            COALESCE(sum(eligible_discount_amount), 0) AS total_standard_discount,
            COALESCE(sum(actual_discount_amount), 0) AS total_actual_discount,
            COALESCE(sum(excess_discount_amount), 0) AS total_excess_discount,
            count(*) FILTER (WHERE is_excess_discount)::int AS journeys_with_excess_discount,
            COALESCE(sum(dk_cash_discount), 0) AS total_cash_discount,
            COALESCE(sum(dk_exchange_bonus), 0) AS total_exchange_bonus,
            count(*) FILTER (WHERE dk_exchange_bonus > 0)::int AS exchange_journeys,
            COALESCE(sum(dk_scrappage), 0) AS total_scrappage_bonus,
            count(*) FILTER (WHERE dk_scrappage > 0)::int AS scrappage_journeys,
            COALESCE(sum(dk_corporate), 0) AS total_corporate_discount,
            COALESCE(sum(dk_mr), 0) AS total_mr_discount,
            COALESCE(sum(dk_other), 0) AS total_other_discount,
            count(*) FILTER (WHERE has_insurance)::int AS insured_journeys,
            count(*) FILTER (WHERE is_inhouse_insurance)::int AS inhouse_insurance_journeys,
            count(*) FILTER (WHERE is_outside_insurance)::int AS outside_insurance_journeys,
            COALESCE(sum(insurance_premium_amount), 0) AS total_insurance_premium,
            count(*) FILTER (WHERE has_accessories)::int AS accessory_journeys,
            COALESCE(sum(accessories_value), 0) AS total_accessories_value,
            count(*) FILTER (WHERE has_ew)::int AS ew_journeys,
            COALESCE(sum(ew_value), 0) AS total_ew_value,
            count(*) FILTER (WHERE has_rsa)::int AS rsa_journeys,
            COALESCE(sum(rsa_value), 0) AS total_rsa_value,
            count(*) FILTER (WHERE has_trade_in)::int AS trade_in_journeys,
            COALESCE(sum(trade_in_value), 0) AS total_trade_in_value,
            count(*) FILTER (WHERE has_finance)::int AS finance_journeys,
            COALESCE(sum(payment_amount), 0) AS total_payment_collected,
            COALESCE(sum(booking_payment_amount), 0) AS booking_payment_collected,
            COALESCE(sum(delivery_payment_amount), 0) AS delivery_payment_collected,
            round(avg(tat_days) FILTER (WHERE tat_days IS NOT NULL), 1) AS avg_booking_to_delivery_days,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY tat_days) FILTER (WHERE tat_days IS NOT NULL) AS median_booking_to_delivery_days,
            count(*) FILTER (WHERE finding_count > 0)::int AS journeys_with_findings,
            count(*) FILTER (WHERE missing_document_flag_count > 0)::int AS journeys_with_missing_docs
        FROM deal_facts
        """
        summary_rows = [dict(r) for r in connection.execute(text(summary_sql), params).mappings()]
        summary = summary_rows[0] if summary_rows else {}

        # 2. Dealership Audit Matrix
        dealer_sql = _PROJECT_AUDITOR_CTE + """
        SELECT
            dealer_id,
            dealer_name,
            count(*)::int AS booked_cars,
            count(*) FILTER (WHERE is_delivered)::int AS delivered_cars,
            COALESCE(sum(eligible_discount_amount), 0) AS standard_discount,
            COALESCE(sum(actual_discount_amount), 0) AS actual_discount,
            COALESCE(sum(excess_discount_amount), 0) AS excess_discount,
            count(*) FILTER (WHERE is_excess_discount)::int AS excess_discount_journeys,
            COALESCE(sum(dk_exchange_bonus), 0) AS exchange_bonus,
            count(*) FILTER (WHERE dk_exchange_bonus > 0)::int AS exchange_journeys,
            COALESCE(sum(dk_scrappage), 0) AS scrappage_bonus,
            count(*) FILTER (WHERE dk_scrappage > 0)::int AS scrappage_journeys,
            count(*) FILTER (WHERE has_insurance)::int AS total_insured,
            count(*) FILTER (WHERE is_inhouse_insurance)::int AS inhouse_insurance,
            count(*) FILTER (WHERE is_outside_insurance)::int AS outside_insurance,
            COALESCE(sum(accessories_value), 0) AS accessories_value,
            COALESCE(sum(ew_value), 0) AS ew_value,
            COALESCE(sum(rsa_value), 0) AS rsa_value,
            COALESCE(sum(payment_amount), 0) AS payment_collected,
            round(avg(tat_days) FILTER (WHERE tat_days IS NOT NULL), 1) AS avg_delivery_days,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY tat_days) FILTER (WHERE tat_days IS NOT NULL) AS median_delivery_days,
            count(*) FILTER (WHERE finding_count > 0)::int AS journeys_with_findings
        FROM deal_facts
        GROUP BY dealer_id, dealer_name
        ORDER BY excess_discount DESC, booked_cars DESC
        """
        dealers = [dict(r) for r in connection.execute(text(dealer_sql), params).mappings()]

        # 3. Outlet Audit Matrix
        outlet_sql = _PROJECT_AUDITOR_CTE + """
        SELECT
            dealer_id,
            dealer_name,
            outlet_id,
            outlet_name,
            city,
            state_region,
            count(*)::int AS booked_cars,
            count(*) FILTER (WHERE is_delivered)::int AS delivered_cars,
            COALESCE(sum(eligible_discount_amount), 0) AS standard_discount,
            COALESCE(sum(actual_discount_amount), 0) AS actual_discount,
            COALESCE(sum(excess_discount_amount), 0) AS excess_discount,
            count(*) FILTER (WHERE is_excess_discount)::int AS excess_discount_journeys,
            COALESCE(sum(dk_exchange_bonus), 0) AS exchange_bonus,
            count(*) FILTER (WHERE dk_exchange_bonus > 0)::int AS exchange_journeys,
            COALESCE(sum(dk_scrappage), 0) AS scrappage_bonus,
            count(*) FILTER (WHERE dk_scrappage > 0)::int AS scrappage_journeys,
            count(*) FILTER (WHERE has_insurance)::int AS total_insured,
            count(*) FILTER (WHERE is_inhouse_insurance)::int AS inhouse_insurance,
            count(*) FILTER (WHERE is_outside_insurance)::int AS outside_insurance,
            COALESCE(sum(accessories_value), 0) AS accessories_value,
            COALESCE(sum(ew_value), 0) AS ew_value,
            COALESCE(sum(rsa_value), 0) AS rsa_value,
            COALESCE(sum(payment_amount), 0) AS payment_collected,
            round(avg(tat_days) FILTER (WHERE tat_days IS NOT NULL), 1) AS avg_delivery_days,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY tat_days) FILTER (WHERE tat_days IS NOT NULL) AS median_delivery_days,
            count(*) FILTER (WHERE finding_count > 0)::int AS journeys_with_findings
        FROM deal_facts
        GROUP BY dealer_id, dealer_name, outlet_id, outlet_name, city, state_region
        ORDER BY excess_discount DESC, booked_cars DESC
        """
        outlets = [dict(r) for r in connection.execute(text(outlet_sql), params).mappings()]

        # 4. Model Velocity & Discount Analysis
        model_sql = _PROJECT_AUDITOR_CTE + """
        SELECT
            model_name,
            count(*)::int AS booked_units,
            count(*) FILTER (WHERE is_delivered)::int AS delivered_units,
            COALESCE(sum(eligible_discount_amount), 0) AS standard_discount,
            COALESCE(sum(actual_discount_amount), 0) AS actual_discount,
            COALESCE(sum(excess_discount_amount), 0) AS excess_discount,
            round(avg(actual_discount_amount) FILTER (WHERE actual_discount_amount > 0), 2) AS avg_discount_per_car,
            round(avg(tat_days) FILTER (WHERE tat_days IS NOT NULL), 1) AS avg_delivery_days,
            percentile_cont(0.5) WITHIN GROUP (ORDER BY tat_days) FILTER (WHERE tat_days IS NOT NULL) AS median_delivery_days,
            min(tat_days) FILTER (WHERE tat_days IS NOT NULL)::int AS min_delivery_days,
            max(tat_days) FILTER (WHERE tat_days IS NOT NULL)::int AS max_delivery_days
        FROM deal_facts
        GROUP BY model_name
        ORDER BY booked_units DESC, excess_discount DESC
        """
        models = [dict(r) for r in connection.execute(text(model_sql), params).mappings()]

        # 5. Customer Pincode Placement & Territory Audit
        pincode_sql = _PROJECT_AUDITOR_CTE + """
        SELECT
            customer_pincode,
            dealer_name,
            outlet_name,
            count(*)::int AS car_count,
            count(*) FILTER (WHERE is_delivered)::int AS delivered_count,
            COALESCE(sum(actual_discount_amount), 0) AS total_discount,
            COALESCE(sum(excess_discount_amount), 0) AS excess_discount,
            COALESCE(sum(payment_amount), 0) AS payment_collected
        FROM deal_facts
        WHERE customer_pincode IS NOT NULL
        GROUP BY customer_pincode, dealer_name, outlet_name
        ORDER BY car_count DESC, customer_pincode
        LIMIT 100
        """
        pincodes = [dict(r) for r in connection.execute(text(pincode_sql), params).mappings()]

        # 6. Discount Scheme Breakdown (Cash, Exchange, Scrappage, Corporate, etc.)
        schemes_sql = """
        SELECT
            COALESCE(row_data->>'discount_key', 'Unspecified') AS discount_key,
            count(*)::int AS application_count,
            count(DISTINCT row_data->>'journey_id')::int AS journey_count,
            COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric), 0) AS actual_discount_amount,
            COALESCE(sum(NULLIF(row_data->>'standard_eligible_amount','')::numeric), 0) AS standard_eligible_amount,
            COALESCE(sum(
                GREATEST(
                    NULLIF(row_data->>'actual_discount_amount','')::numeric
                    - NULLIF(row_data->>'standard_eligible_amount','')::numeric,
                    0
                )
            ) FILTER (
                WHERE NULLIF(row_data->>'actual_discount_amount','') IS NOT NULL
                  AND NULLIF(row_data->>'standard_eligible_amount','') IS NOT NULL
            ), 0) AS excess_discount_amount
        FROM analytics.snapshot_rows
        WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='discount_applications'
        GROUP BY 1
        ORDER BY actual_discount_amount DESC, discount_key
        """
        schemes = [dict(r) for r in connection.execute(text(schemes_sql), params).mappings()]

        # 7. Accessories Breakdown by Type
        accessories_sql = """
        SELECT
            upper(COALESCE(row_data->>'addon_type_code','UNSPECIFIED')) AS addon_type,
            count(*)::int AS record_count,
            count(DISTINCT row_data->>'journey_id')::int AS journey_count,
            COALESCE(sum(NULLIF(row_data->>'actual_amount','')::numeric), 0) AS actual_amount,
            round(avg(NULLIF(row_data->>'actual_amount','')::numeric), 2) AS avg_amount
        FROM analytics.snapshot_rows
        WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_addons'
        GROUP BY 1
        ORDER BY actual_amount DESC, addon_type
        """
        accessories = [dict(r) for r in connection.execute(text(accessories_sql), params).mappings()]

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "dealers": dealers,
        "outlets": outlets,
        "models": models,
        "pincodes": pincodes,
        "discount_schemes": schemes,
        "accessories": accessories,
    }
