from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, text

from analytics.db import analytics_engine
from analytics.pricing_audit import pricing_integrity_report
from analytics.reports import _latest_dump, _query_rows
from analytics.security import Principal, require_analytics_read

router = APIRouter(
    prefix="/v1/analytics/tenants/{tenant_id}/business-intelligence",
    tags=["business-intelligence"],
)


_DEAL_FACTS_CTE = """
WITH
journeys AS (
    SELECT DISTINCT ON (row_data->>'journey_id')
           row_data->>'journey_id' AS journey_id,
           row_data->>'dealer_id' AS dealer_id,
           row_data->>'outlet_id' AS outlet_id,
           row_data->>'customer_id' AS customer_id
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journeys'
    ORDER BY row_data->>'journey_id', row_data->>'updated_at_utc' DESC NULLS LAST
),
dealers AS (
    SELECT DISTINCT ON (row_data->>'dealer_id')
           row_data->>'dealer_id' AS dealer_id,
           COALESCE(
               NULLIF(row_data->>'dealer_name',''),
               NULLIF(row_data->>'dealer_code',''),
               'Unspecified dealer'
           ) AS dealer_name
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='dealers'
    ORDER BY row_data->>'dealer_id'
),
outlets AS (
    SELECT DISTINCT ON (row_data->>'outlet_id')
           row_data->>'outlet_id' AS outlet_id,
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
    ORDER BY row_data->>'outlet_id'
),
bookings AS (
    SELECT DISTINCT ON (row_data->>'journey_id')
           row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'booking_date','')::date AS booking_date,
           NULLIF(row_data->>'booking_confirmation_date','')::date AS booking_confirmation_date,
           NULLIF(row_data->>'expected_delivery_date','')::date AS expected_delivery_date,
           NULLIF(row_data->>'sales_staff_id','') AS sales_staff_id,
           NULLIF(row_data->>'deal_type_code','') AS deal_type_code,
           NULLIF(row_data->>'deal_source_code','') AS deal_source_code,
           NULLIF(row_data->>'lead_source_code','') AS lead_source_code
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
    ORDER BY row_data->>'journey_id',
             NULLIF(row_data->>'booking_date','')::date DESC NULLS LAST,
             NULLIF(row_data->>'booking_confirmation_date','')::date DESC NULLS LAST
),
staff AS (
    SELECT DISTINCT ON (row_data->>'dealership_staff_id')
           row_data->>'dealership_staff_id' AS dealership_staff_id,
           NULLIF(row_data->>'display_name','') AS sales_staff_name,
           NULLIF(row_data->>'staff_role_code','') AS sales_staff_role
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='dealership_staff'
    ORDER BY row_data->>'dealership_staff_id', row_data->>'updated_at_utc' DESC NULLS LAST
),
products AS (
    SELECT DISTINCT ON (row_data->>'journey_id')
           row_data->>'journey_id' AS journey_id,
           COALESCE(NULLIF(row_data->>'canonical_model_name',''),
                    NULLIF(row_data->>'model_name_snapshot','')) AS model_name,
           COALESCE(NULLIF(row_data->>'canonical_variant_name',''),
                    NULLIF(row_data->>'variant_name_snapshot','')) AS variant_name,
           NULLIF(row_data->>'canonical_model_name','') IS NOT NULL AS model_catalog_resolved,
           NULLIF(row_data->>'colour_name_snapshot','') AS colour_name
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_products'
    ORDER BY row_data->>'journey_id',
             (NULLIF(row_data->>'canonical_model_name','') IS NOT NULL) DESC,
             (NULLIF(row_data->>'canonical_variant_name','') IS NOT NULL) DESC
),
deliveries AS (
    SELECT DISTINCT ON (row_data->>'journey_id')
           row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'planned_delivery_at','')::timestamptz AS planned_delivery_at,
           NULLIF(row_data->>'actual_delivered_at','')::timestamptz AS actual_delivered_at,
           NULLIF(row_data->>'actual_delivery_status_code','') AS delivery_status
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='deliveries'
    ORDER BY row_data->>'journey_id',
             NULLIF(row_data->>'actual_delivered_at','')::timestamptz DESC NULLS LAST,
             NULLIF(row_data->>'planned_delivery_at','')::timestamptz DESC NULLS LAST,
             NULLIF(row_data->>'created_at_utc','')::timestamptz DESC NULLS LAST
),
vehicles AS (
    SELECT DISTINCT ON (row_data->>'journey_id')
           row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'allocated_at_utc','')::timestamptz AS allocated_at_utc
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='vehicle_records'
    ORDER BY row_data->>'journey_id',
             NULLIF(row_data->>'allocated_at_utc','')::timestamptz DESC NULLS LAST
),
finance AS (
    SELECT row_data->>'journey_id' AS journey_id,
           count(*)::int AS finance_record_count
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='finance_records'
    GROUP BY 1
),
insurance AS (
    SELECT DISTINCT ON (row_data->>'journey_id')
           row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'insurance_by','') AS insurance_by,
           NULLIF(row_data->>'insurer_name','') AS insurer_name,
           NULLIF(row_data->>'actual_premium_amount','')::numeric AS actual_premium_amount,
           NULLIF(row_data->>'standard_premium_amount','')::numeric AS standard_premium_amount,
           NULLIF(row_data->>'self_insurance_flag','')::boolean AS self_insurance_flag,
           NULLIF(row_data->>'agent_intermediary_code','') AS insurance_intermediary_code,
           NULLIF(row_data->>'agent_intermediary_name','') AS insurance_intermediary_name,
           NULLIF(row_data->>'misp_code','') AS insurance_misp_code,
           row_data->'add_ons' AS insurance_add_ons,
           NULLIF(row_data->>'insurance_source','') AS insurance_source,
           CASE
               WHEN upper(COALESCE(NULLIF(row_data->>'insurance_source',''),''))='INHOUSE'
                    AND NULLIF(row_data->>'self_insurance_flag','')::boolean IS TRUE
                   THEN 'MIXED_OR_UNCLEAR'
               WHEN upper(COALESCE(NULLIF(row_data->>'insurance_source',''),''))='INHOUSE'
                   THEN 'DEALER_ARRANGED'
               WHEN upper(COALESCE(NULLIF(row_data->>'insurance_source',''),''))
                        IN ('SELF','EXTERNAL','OUTSIDE')
                    OR NULLIF(row_data->>'self_insurance_flag','')::boolean IS TRUE
                   THEN 'CUSTOMER_ARRANGED'
               WHEN regexp_replace(upper(COALESCE(row_data->>'insurance_by','')),
                                   '[^A-Z0-9]', '', 'g')
                        IN ('DEALER','DLR','DEALERARRANGED','DLRARRANGED')
                   THEN 'DEALER_ARRANGED'
               WHEN regexp_replace(upper(COALESCE(row_data->>'insurance_by','')),
                                   '[^A-Z0-9]', '', 'g')
                        IN ('SELF','OWN','CUSTOMER','DONEBYCUSTOMER',
                            'CUSTOMERARRANGED','OWNARRANGED')
                   THEN 'CUSTOMER_ARRANGED'
               WHEN regexp_replace(upper(COALESCE(row_data->>'insurance_by','')),
                                   '[^A-Z0-9]', '', 'g') LIKE '%OWN%DLR%'
                 OR regexp_replace(upper(COALESCE(row_data->>'insurance_by','')),
                                   '[^A-Z0-9]', '', 'g') LIKE '%DLR%OWN%'
                 OR regexp_replace(upper(COALESCE(row_data->>'insurance_by','')),
                                   '[^A-Z0-9]', '', 'g') LIKE '%OWN%DEALER%'
                 OR regexp_replace(upper(COALESCE(row_data->>'insurance_by','')),
                                   '[^A-Z0-9]', '', 'g') LIKE '%DEALER%OWN%'
                   THEN 'MIXED_OR_UNCLEAR'
               ELSE 'UNSPECIFIED'
           END AS insurance_route
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='insurance_records'
    ORDER BY row_data->>'journey_id',
             (NULLIF(row_data->>'actual_premium_amount','') IS NOT NULL) DESC,
             (NULLIF(row_data->>'agent_intermediary_name','') IS NOT NULL) DESC,
             NULLIF(row_data->>'created_at_utc','')::timestamptz DESC NULLS LAST
),
trade_in AS (
    SELECT row_data->>'journey_id' AS journey_id,
           sum(NULLIF(row_data->>'quoted_value','')::numeric) AS trade_in_quoted_value,
           sum(NULLIF(row_data->>'actual_value','')::numeric) AS trade_in_actual_value,
           max(NULLIF(row_data->>'handover_at_utc','')::timestamptz) AS trade_in_handover_at,
           max(NULLIF(row_data->>'payment_at_utc','')::timestamptz) AS trade_in_payment_at,
           max(NULLIF(row_data->>'resale_at_utc','')::timestamptz) AS trade_in_resale_at
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='trade_in_cases'
    GROUP BY 1
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
           COALESCE(sum(CASE
               WHEN NULLIF(row_data->>'actual_discount_amount','')::numeric IS NULL THEN 0
               WHEN row_data->>'discount_key'='OTHER_SCHEME' AND EXISTS (
                   SELECT 1 FROM analytics.snapshot_rows mr
                   WHERE mr.tenant_id=:tenant_id AND mr.dump_id=:dump_id
                     AND mr.source_table='p2_management_referrals'
                     AND mr.row_data->>'journey_id'=row_data->>'journey_id'
                     AND COALESCE(NULLIF(mr.row_data->>'opted','')::boolean,false)
               ) THEN 0
               WHEN NULLIF(row_data->>'standard_eligible_amount','')::numeric IS NOT NULL
                   THEN GREATEST(
                       NULLIF(row_data->>'actual_discount_amount','')::numeric
                         - NULLIF(row_data->>'standard_eligible_amount','')::numeric,
                       0
                   )
               WHEN upper(COALESCE(row_data->>'eligibility_result',''))='NOT_ELIGIBLE'
                    OR row_data->>'discount_key'='ADDITIONAL_DISCOUNT'
                   THEN GREATEST(NULLIF(row_data->>'actual_discount_amount','')::numeric,0)
               ELSE 0
           END),0) AS strict_excess_discount_amount,
           COALESCE(sum(CASE
               WHEN NULLIF(row_data->>'actual_discount_amount','')::numeric IS NULL THEN 0
               WHEN row_data->>'discount_key'='OTHER_SCHEME' AND EXISTS (
                   SELECT 1 FROM analytics.snapshot_rows mr
                   WHERE mr.tenant_id=:tenant_id AND mr.dump_id=:dump_id
                     AND mr.source_table='p2_management_referrals'
                     AND mr.row_data->>'journey_id'=row_data->>'journey_id'
                     AND COALESCE(NULLIF(mr.row_data->>'opted','')::boolean,false)
               ) THEN 0
               WHEN NULLIF(row_data->>'standard_eligible_amount','')::numeric IS NOT NULL
                   AND NULLIF(row_data->>'actual_discount_amount','')::numeric
                       - NULLIF(row_data->>'standard_eligible_amount','')::numeric > 100
                   THEN NULLIF(row_data->>'actual_discount_amount','')::numeric
                       - NULLIF(row_data->>'standard_eligible_amount','')::numeric
               WHEN (upper(COALESCE(row_data->>'eligibility_result',''))='NOT_ELIGIBLE'
                     OR row_data->>'discount_key'='ADDITIONAL_DISCOUNT')
                    AND NULLIF(row_data->>'actual_discount_amount','')::numeric > 100
                   THEN NULLIF(row_data->>'actual_discount_amount','')::numeric
               ELSE 0
           END),0) AS above_eligible_discount_amount,
           count(*) FILTER (
               WHERE NULLIF(row_data->>'actual_discount_amount','')::numeric IS NOT NULL
                 AND (row_data->>'discount_key'='OTHER_SCHEME' AND EXISTS (
                     SELECT 1 FROM analytics.snapshot_rows mr
                     WHERE mr.tenant_id=:tenant_id AND mr.dump_id=:dump_id
                       AND mr.source_table='p2_management_referrals'
                       AND mr.row_data->>'journey_id'=row_data->>'journey_id'
                       AND COALESCE(NULLIF(mr.row_data->>'opted','')::boolean,false)
                 ) OR (NULLIF(row_data->>'standard_eligible_amount','')::numeric IS NULL
                       AND upper(COALESCE(row_data->>'eligibility_result',''))<>'NOT_ELIGIBLE'
                       AND row_data->>'discount_key'<>'ADDITIONAL_DISCOUNT'))
           )::int AS unknown_entitlement_count,
           bool_or(NULLIF(row_data->>'actual_discount_amount','') IS NOT NULL) AS actual_present,
           bool_or(NULLIF(row_data->>'standard_eligible_amount','') IS NOT NULL) AS eligible_present,
           -- per-key actual amounts for discount waterfall charts
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (
               WHERE row_data->>'discount_key'='CASH_DISCOUNT'), 0) AS dk_cash_discount,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (
               WHERE row_data->>'discount_key'='EXCHANGE_BONUS'), 0) AS dk_exchange_bonus,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (
               WHERE row_data->>'discount_key' IN ('SCRAPPAGE_BONUS_DEALER','SCRAPPAGE_BONUS_COD')), 0) AS dk_scrappage,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (
               WHERE row_data->>'discount_key'='CORPORATE_PRIVILEGE'), 0) AS dk_corporate,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (
               WHERE row_data->>'discount_key'='WELCOME_BONUS'), 0) AS dk_loyalty,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (
               WHERE row_data->>'discount_key'='INSURANCE'), 0) AS dk_insurance,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (
               WHERE row_data->>'discount_key'='MANAGEMENT_REFERRAL'), 0) AS dk_mr,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (
               WHERE row_data->>'discount_key' IN ('ACCESSORIES_KIT','EXT_WARRANTY_4TH_YR','EXT_WARRANTY_4TH_5TH_YR')), 0) AS dk_scheme_benefit,
           COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) FILTER (
               WHERE row_data->>'discount_key' IN ('OTHER_SCHEME','ADDITIONAL_DISCOUNT')), 0) AS dk_other,
           -- eligible per key for standard-vs-actual comparison
           COALESCE(sum(NULLIF(row_data->>'standard_eligible_amount','')::numeric) FILTER (
               WHERE row_data->>'discount_key'='MANAGEMENT_REFERRAL'), 0) AS dk_mr_eligible,
           bool_or(row_data->>'discount_key'='MANAGEMENT_REFERRAL'
               AND NULLIF(row_data->>'actual_discount_amount','') IS NOT NULL) AS has_mr_discount,
           -- above-eligible per journey (actual > standard on any single key)
           COALESCE(bool_or(
               NULLIF(row_data->>'actual_discount_amount','')::numeric IS NOT NULL
               AND NOT (row_data->>'discount_key'='OTHER_SCHEME' AND EXISTS (
                   SELECT 1 FROM analytics.snapshot_rows mr
                   WHERE mr.tenant_id=:tenant_id AND mr.dump_id=:dump_id
                     AND mr.source_table='p2_management_referrals'
                     AND mr.row_data->>'journey_id'=row_data->>'journey_id'
                     AND COALESCE(NULLIF(mr.row_data->>'opted','')::boolean,false)
               ))
               AND NULLIF(row_data->>'actual_discount_amount','')::numeric >
                   CASE
                       WHEN NULLIF(row_data->>'standard_eligible_amount','')::numeric IS NOT NULL
                           THEN NULLIF(row_data->>'standard_eligible_amount','')::numeric
                       WHEN upper(COALESCE(row_data->>'eligibility_result',''))='NOT_ELIGIBLE'
                            OR row_data->>'discount_key'='ADDITIONAL_DISCOUNT' THEN 0
                       ELSE NULL
                   END + 100
           ),false) AS has_excess_on_any_key,
           COALESCE(bool_or(
               NULLIF(row_data->>'actual_discount_amount','')::numeric IS NOT NULL
               AND NOT (row_data->>'discount_key'='OTHER_SCHEME' AND EXISTS (
                   SELECT 1 FROM analytics.snapshot_rows mr
                   WHERE mr.tenant_id=:tenant_id AND mr.dump_id=:dump_id
                     AND mr.source_table='p2_management_referrals'
                     AND mr.row_data->>'journey_id'=row_data->>'journey_id'
                     AND COALESCE(NULLIF(mr.row_data->>'opted','')::boolean,false)
               ))
               AND NULLIF(row_data->>'actual_discount_amount','')::numeric >
                   CASE
                       WHEN NULLIF(row_data->>'standard_eligible_amount','')::numeric IS NOT NULL
                           THEN NULLIF(row_data->>'standard_eligible_amount','')::numeric
                       WHEN upper(COALESCE(row_data->>'eligibility_result',''))='NOT_ELIGIBLE'
                            OR row_data->>'discount_key'='ADDITIONAL_DISCOUNT' THEN 0
                       ELSE NULL
                   END
           ),false) AS has_strict_excess_on_any_key
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id
      AND source_table='discount_applications'
    GROUP BY 1
),
payments AS (
    SELECT row_data->>'journey_id' AS journey_id,
           count(*)::int AS payment_count,
           sum(NULLIF(row_data->>'amount','')::numeric) AS payment_amount
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
    SELECT DISTINCT ON (row_data->>'journey_id')
           row_data->>'journey_id' AS journey_id,
           NULLIF(row_data->>'customer_pincode','') AS customer_pincode,
           NULLIF(row_data->>'source_field_key','') AS geography_source_field,
           NULLIF(row_data->>'source_reviewed_at_utc','')::timestamptz AS geography_reviewed_at_utc,
           NULLIF(row_data->>'source_confidence_score','')::numeric AS geography_confidence_score
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id
      AND source_table='derived_customer_geography'
    ORDER BY row_data->>'journey_id',
             NULLIF(row_data->>'source_reviewed_at_utc','')::timestamptz DESC NULLS LAST,
             NULLIF(row_data->>'source_confidence_score','')::numeric DESC NULLS LAST
),
mr_discount AS (
    SELECT row_data->>'journey_id' AS journey_id,
           bool_or(NULLIF(row_data->>'opted','')::boolean) AS mr_opted,
           sum(NULLIF(row_data->>'amount','')::numeric) AS mr_amount,
           max(NULLIF(row_data->>'set_by_role','')) AS mr_set_by_role
    FROM analytics.snapshot_rows
    WHERE tenant_id=:tenant_id AND dump_id=:dump_id
      AND source_table='p2_management_referrals'
    GROUP BY 1
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
           (b.journey_id IS NOT NULL) AS has_booking,
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
           COALESCE(p.model_catalog_resolved,false) AS model_catalog_resolved,
           p.colour_name,
           (dl.journey_id IS NOT NULL) AS has_delivery_record,
           dl.delivery_status,
           dl.planned_delivery_at,
           dl.actual_delivered_at,
           v.allocated_at_utc,
           (f.journey_id IS NOT NULL) AS has_finance,
           (i.journey_id IS NOT NULL) AS has_insurance,
           i.insurance_by,
           i.insurer_name,
           i.actual_premium_amount,
           i.standard_premium_amount,
           i.self_insurance_flag,
           i.insurance_intermediary_code,
           i.insurance_intermediary_name,
           i.insurance_misp_code,
           i.insurance_add_ons,
           i.insurance_source,
           i.insurance_route,
           (i.journey_id IS NOT NULL AND i.insurance_route = 'DEALER_ARRANGED') AS has_inhouse_insurance,
           (i.journey_id IS NOT NULL AND i.insurance_route = 'CUSTOMER_ARRANGED') AS has_self_insurance,
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
           COALESCE(di.above_eligible_discount_amount,0) AS above_eligible_discount_amount,
           COALESCE(di.strict_excess_discount_amount,0) AS strict_excess_discount_amount,
           COALESCE(di.unknown_entitlement_count,0) AS unknown_entitlement_count,
           COALESCE(di.dk_cash_discount,0) AS dk_cash_discount,
           COALESCE(di.dk_exchange_bonus,0) AS dk_exchange_bonus,
           COALESCE(di.dk_scrappage,0) AS dk_scrappage,
           COALESCE(di.dk_corporate,0) AS dk_corporate,
           COALESCE(di.dk_loyalty,0) AS dk_loyalty,
           COALESCE(di.dk_insurance,0) AS dk_insurance,
           COALESCE(di.dk_mr,0) AS dk_mr,
           COALESCE(di.dk_scheme_benefit,0) AS dk_scheme_benefit,
           COALESCE(di.dk_other,0) AS dk_other,
           COALESCE(di.dk_mr_eligible,0) AS dk_mr_eligible,
           COALESCE(di.has_mr_discount,false) AS has_mr_discount,
           COALESCE(di.has_excess_on_any_key,false) AS has_excess_on_any_key,
           COALESCE(di.has_strict_excess_on_any_key,false) AS has_strict_excess_on_any_key,
           COALESCE(py.payment_count,0) AS payment_count,
           COALESCE(py.payment_amount,0) AS payment_amount,
           COALESCE(fi.finding_count,0) AS finding_count,
           COALESCE(fi.open_finding_count,0) AS open_finding_count,
           COALESCE(fi.high_finding_count,0) AS high_finding_count,
           COALESCE(fi.missing_document_flag_count,0) AS missing_document_flag_count,
           g.customer_pincode,
           g.geography_source_field,
           g.geography_reviewed_at_utc,
           g.geography_confidence_score,
           COALESCE(mr.mr_opted, false) AS mr_opted,
           mr.mr_amount,
           mr.mr_set_by_role
    FROM journeys j
    LEFT JOIN dealers d ON d.dealer_id=j.dealer_id
    LEFT JOIN outlets o ON o.outlet_id=j.outlet_id AND o.dealer_id=j.dealer_id
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
    LEFT JOIN mr_discount mr ON mr.journey_id=j.journey_id
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
                       count(*) FILTER (WHERE has_booking)::int AS booked_journeys,
                       count(*) FILTER (WHERE has_delivery_record)::int AS delivery_records,
                       count(*) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                           AS actual_deliveries,
                       count(*) FILTER (WHERE has_booking AND has_finance)::int AS finance_journeys,
                       count(*) FILTER (WHERE has_booking AND has_insurance)::int AS insurance_journeys,
                       count(*) FILTER (WHERE has_booking AND has_trade_in)::int AS trade_in_journeys,
                       count(*) FILTER (WHERE has_booking AND has_accessories)::int AS accessory_journeys,
                       count(*) FILTER (WHERE has_booking AND has_ew)::int AS ew_journeys,
                       count(*) FILTER (WHERE has_booking AND has_rsa)::int AS rsa_journeys,
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
        booked = int(summary.get("booked_journeys") or 0)
        summary.update(
            {
                "finance_penetration_pct": _rate(summary.get("finance_journeys") or 0, booked),
                "insurance_penetration_pct": _rate(
                    summary.get("insurance_journeys") or 0, booked
                ),
                "trade_in_penetration_pct": _rate(
                    summary.get("trade_in_journeys") or 0, booked
                ),
                "accessory_penetration_pct": _rate(
                    summary.get("accessory_journeys") or 0, booked
                ),
                "ew_penetration_pct": _rate(summary.get("ew_journeys") or 0, booked),
                "rsa_penetration_pct": _rate(summary.get("rsa_journeys") or 0, booked),
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
                   count(*) FILTER (WHERE has_booking)::int AS journey_count,
                   count(*) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                       AS actual_deliveries,
                   COALESCE(sum(actual_discount_amount),0) AS discount_amount
            FROM deal_facts
            WHERE has_booking
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
                   count(*) FILTER (WHERE has_booking)::int AS journey_count,
                   count(*) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                       AS actual_deliveries,
                   count(*) FILTER (WHERE finding_count > 0)::int
                       AS journeys_with_findings,
                   count(*) FILTER (WHERE has_finance)::int AS finance_journeys,
                   count(*) FILTER (WHERE has_insurance)::int AS insurance_journeys,
                   count(*) FILTER (WHERE has_accessories)::int AS accessory_journeys
            FROM deal_facts
            WHERE has_booking
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
                   count(DISTINCT journey_id) FILTER (WHERE has_booking)::int AS journey_count,
                   count(DISTINCT journey_id) FILTER (WHERE has_delivery_record)::int AS delivery_records,
                   count(DISTINCT journey_id) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                       AS actual_deliveries,
                   COALESCE(sum(payment_amount),0) AS payment_amount
            FROM deal_facts
            WHERE has_booking
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
                   count(DISTINCT journey_id) FILTER (WHERE has_booking)::int AS journey_count,
                   count(DISTINCT journey_id) FILTER (WHERE actual_delivered_at IS NOT NULL)::int
                       AS actual_deliveries,
                   COALESCE(sum(actual_discount_amount),0) AS discount_amount,
                   COALESCE(sum(payment_amount),0) AS payment_amount
            FROM deal_facts
            WHERE has_booking
            GROUP BY 1,2
            ORDER BY journey_count DESC, model_name, variant_name
            """,
        )
        by_dealer_model_variant = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   COALESCE(model_name,'Unresolved') AS model_name,
                   COALESCE(variant_name,'Unresolved') AS variant_name,
                   count(DISTINCT journey_id) FILTER (WHERE has_booking)::int AS booked_units,
                   count(DISTINCT journey_id) FILTER (
                       WHERE has_booking AND actual_delivered_at IS NOT NULL
                   )::int AS delivered_units
            FROM deal_facts
            WHERE has_booking
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name,
                     model_name, variant_name
            ORDER BY booked_units DESC, dealer_name, outlet_name,
                     model_name, variant_name
            """,
        )
        model_variant_coverage = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                _DEAL_FACTS_CTE
                + """
                SELECT count(DISTINCT journey_id) FILTER (WHERE has_booking)::int AS booked_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND model_name IS NOT NULL
                       )::int AS model_labelled_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND variant_name IS NOT NULL
                       )::int AS variant_labelled_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND model_catalog_resolved
                       )::int AS catalogue_mapped_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking
                             AND (model_name IS NULL OR variant_name IS NULL)
                       )::int AS unresolved_journeys
                FROM deal_facts
                """,
            )
        )
        by_colour = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(colour_name,'Unresolved') AS colour_name,
                   count(DISTINCT journey_id)::int AS journey_count
            FROM deal_facts
            WHERE has_booking
            GROUP BY 1
            ORDER BY journey_count DESC, colour_name
            """,
        )
        by_month = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            , sales_events AS (
                SELECT date_trunc('month', booking_date)::date AS period,
                       journey_id,
                       'BOOKED' AS event_type
                FROM deal_facts
                WHERE has_booking AND booking_date IS NOT NULL
                UNION ALL
                SELECT date_trunc('month', actual_delivered_at)::date AS period,
                       journey_id,
                       'DELIVERED' AS event_type
                FROM deal_facts
                WHERE actual_delivered_at IS NOT NULL
            ), monthly_sales AS (
                SELECT period,
                       count(DISTINCT journey_id) FILTER (
                           WHERE event_type='BOOKED'
                       )::int AS booked_units,
                       count(DISTINCT journey_id) FILTER (
                           WHERE event_type='DELIVERED'
                       )::int AS delivered_units
                FROM sales_events
                GROUP BY period
            ), recent_periods AS (
                SELECT * FROM monthly_sales
                ORDER BY period DESC
                LIMIT 12
            )
            SELECT to_char(period,'YYYY-MM') AS period,
                   booked_units,
                   delivered_units
            FROM recent_periods
            ORDER BY period
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "by_outlet": by_outlet,
        "by_model_variant": by_model,
        "by_dealer_model_variant": by_dealer_model_variant,
        "by_colour": by_colour,
        "by_month": by_month,
        "model_variant_coverage": model_variant_coverage,
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
        all_rows = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT
                -- summary row (scope='SUMMARY')
                'SUMMARY' AS _scope,
                NULL::text AS dealer_name,
                NULL::text AS outlet_name,
                NULL::text AS model_name,
                count(*) FILTER (
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
                    WHERE planned_delivery_at IS NOT NULL AND actual_delivered_at IS NOT NULL
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
                ),2) AS avg_booking_to_allocation_days,
                NULL::int AS completed_count,
                NULL::numeric AS avg_days,
                NULL::float8 AS median_days
            FROM deal_facts
            UNION ALL
            SELECT
                'OUTLET' AS _scope,
                dealer_name, outlet_name, NULL::text,
                NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
                count(*) FILTER (
                    WHERE booking_date IS NOT NULL
                      AND actual_delivered_at IS NOT NULL
                      AND actual_delivered_at::date >= booking_date
                )::int,
                round(avg((actual_delivered_at::date - booking_date)) FILTER (
                    WHERE booking_date IS NOT NULL
                      AND actual_delivered_at IS NOT NULL
                      AND actual_delivered_at::date >= booking_date
                ),2),
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY (actual_delivered_at::date - booking_date)
                ) FILTER (
                    WHERE booking_date IS NOT NULL
                      AND actual_delivered_at IS NOT NULL
                      AND actual_delivered_at::date >= booking_date
                )
            FROM deal_facts
            GROUP BY dealer_name, outlet_name
            UNION ALL
            SELECT
                'MODEL' AS _scope,
                NULL, NULL, COALESCE(model_name,'Unresolved'),
                NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
                count(*) FILTER (
                    WHERE booking_date IS NOT NULL
                      AND actual_delivered_at IS NOT NULL
                      AND actual_delivered_at::date >= booking_date
                )::int,
                round(avg((actual_delivered_at::date - booking_date)) FILTER (
                    WHERE booking_date IS NOT NULL
                      AND actual_delivered_at IS NOT NULL
                      AND actual_delivered_at::date >= booking_date
                ),2),
                percentile_cont(0.5) WITHIN GROUP (
                    ORDER BY (actual_delivered_at::date - booking_date)
                ) FILTER (
                    WHERE booking_date IS NOT NULL
                      AND actual_delivered_at IS NOT NULL
                      AND actual_delivered_at::date >= booking_date
                )
            FROM deal_facts
            GROUP BY model_name
            ORDER BY _scope, completed_count DESC NULLS LAST
            """,
        )
    finally:
        connection.close()

    summary_rows = [r for r in all_rows if r["_scope"] == "SUMMARY"]
    summary = dict(summary_rows[0]) if summary_rows else {}
    summary.pop("_scope", None)
    summary.pop("dealer_name", None)
    summary.pop("outlet_name", None)
    summary.pop("model_name", None)
    summary.pop("completed_count", None)
    summary.pop("avg_days", None)
    summary.pop("median_days", None)
    summary["on_time_delivery_pct"] = _rate(
        summary.get("on_time_deliveries") or 0,
        summary.get("planned_actual_pairs") or 0,
    )

    by_outlet = [
        {k: v for k, v in r.items() if k not in ("_scope", "model_name")}
        for r in all_rows if r["_scope"] == "OUTLET"
    ]
    by_model = [
        {k: v for k, v in r.items() if k not in ("_scope", "dealer_name", "outlet_name")}
        for r in all_rows if r["_scope"] == "MODEL"
    ]

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
                       COALESCE(sum(above_eligible_discount_amount),0)
                           AS above_eligible_discount_amount,
                       count(*) FILTER (
                           WHERE has_excess_on_any_key
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
        by_dealer_model_variant = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   COALESCE(model_name,'Unresolved') AS model_name,
                   COALESCE(variant_name,'Unresolved') AS variant_name,
                   count(*) FILTER (WHERE has_booking)::int AS booked_units,
                   count(*) FILTER (
                       WHERE has_booking AND actual_discount_present
                   )::int AS discounted_units,
                   COALESCE(sum(actual_discount_amount) FILTER (
                       WHERE has_booking AND actual_discount_present
                   ),0) AS actual_discount_amount,
                   round(avg(actual_discount_amount) FILTER (
                       WHERE has_booking AND actual_discount_present
                   ),2) AS avg_discount_per_discounted_unit,
                   COALESCE(sum(above_eligible_discount_amount) FILTER (
                       WHERE has_booking
                   ),0) AS known_excess_discount_amount,
                   COALESCE(sum(strict_excess_discount_amount) FILTER (
                       WHERE has_booking
                   ),0) AS strict_excess_discount_amount,
                   count(*) FILTER (
                       WHERE has_booking AND has_excess_on_any_key
                   )::int AS known_excess_units,
                   count(*) FILTER (
                       WHERE has_booking AND has_strict_excess_on_any_key
                   )::int AS strict_excess_units,
                   count(*) FILTER (
                       WHERE has_booking AND unknown_entitlement_count > 0
                   )::int AS units_with_unknown_entitlement
            FROM deal_facts
            WHERE has_booking
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name,
                     model_name, variant_name
            ORDER BY known_excess_discount_amount DESC,
                     actual_discount_amount DESC,
                     dealer_name, outlet_name, model_name, variant_name
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
        pricing_integrity = pricing_integrity_report(
            connection,
            tenant_id=tenant_id,
            dump_id=latest["dump_id"],
            total_journeys=int(summary.get("journeys") or 0),
        )
        # Preserve the legacy response shape while using the same per-key
        # comparison as the focused price-integrity report.
        summary["above_eligible_discount_amount"] = (
            pricing_integrity["summary"]["known_excess_discount_amount"]
        )
        summary["above_eligible_journeys"] = (
            pricing_integrity["summary"]["known_excess_discount_journeys"]
        )
        summary["strict_excess_discount_amount"] = (
            pricing_integrity["summary"]["strict_excess_discount_amount"]
        )
        summary["strict_excess_application_count"] = (
            pricing_integrity["summary"]["strict_excess_application_count"]
        )
        summary["strict_excess_discount_journeys"] = (
            pricing_integrity["summary"]["strict_excess_discount_journeys"]
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "by_model": by_model,
        "by_outlet": by_outlet,
        "by_dealer_model_variant": by_dealer_model_variant,
        "by_discount_key": by_key,
        "pricing_integrity": pricing_integrity,
        "above_eligible_definition": (
            "Per-application actual less known entitlement, without netting across discount keys. "
            "Unknown entitlements are excluded and shown in pricing_integrity coverage. "
            "strict_excess_discount_amount includes every positive known overage before Audit Core's "
            "₹100 finding tolerance; above_eligible_discount_amount remains the above-tolerance amount."
        ),
    }


@router.get("/insurance")
def insurance_intelligence(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    insurance_scope = _DEAL_FACTS_CTE + """
    , insurance_facts AS (
        SELECT DISTINCT ON (journey_id) *
        FROM deal_facts
        ORDER BY journey_id, has_booking DESC, booking_date DESC NULLS LAST
    )
    """
    insurer_scope = insurance_scope + """
    , insurer_rows AS (
        SELECT i.*,
               regexp_replace(upper(COALESCE(i.insurer_name,'')), '[^A-Z0-9]', '', 'g') AS insurer_key
        FROM insurance_facts i
        WHERE has_booking AND has_insurance
    ), normalized_insurers AS (
        SELECT *,
               CASE
                   WHEN insurer_key LIKE 'SBIGENERAL%' THEN 'SBI General Insurance'
                   WHEN insurer_key LIKE 'IFFCOTOKIO%' THEN 'IFFCO Tokio General Insurance'
                   WHEN insurer_key LIKE 'HDFCERGO%' THEN 'HDFC ERGO General Insurance'
                   WHEN insurer_key LIKE 'UNIVERSALSOMPO%' THEN 'Universal Sompo General Insurance'
                   WHEN insurer_key LIKE 'FUTUREGENERALI%' OR insurer_key LIKE 'GENERALICENTRAL%' THEN 'Generali Central Insurance'
                   WHEN insurer_key LIKE 'THEORIENTALINSURANCE%' OR insurer_key LIKE 'ORIENTALINSURANCE%' THEN 'The Oriental Insurance Company'
                   WHEN insurer_key LIKE 'NATIONALINSURANCE%' THEN 'National Insurance Company'
                   WHEN insurer_key LIKE 'TATAAIG%' THEN 'Tata AIG General Insurance'
                   ELSE COALESCE(NULLIF(trim(insurer_name),''),'Unspecified insurer')
               END AS normalized_insurer_name
        FROM insurer_rows
    )
    """
    try:
        summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                insurance_scope
                + """
                SELECT count(DISTINCT journey_id)::int AS journeys,
                       count(DISTINCT journey_id) FILTER (WHERE has_booking)::int AS booked_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                       )::int AS insurance_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND insurance_route='DEALER_ARRANGED'
                       )::int AS dealer_arranged_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND insurance_route='CUSTOMER_ARRANGED'
                       )::int AS customer_arranged_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND insurance_route='MIXED_OR_UNCLEAR'
                       )::int AS source_ambiguous_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND insurance_route='UNSPECIFIED'
                       )::int AS source_unknown_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND (insurance_source IS NOT NULL OR insurance_by IS NOT NULL)
                       )::int AS explicit_route_captured,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND (insurance_source IS NOT NULL OR insurance_by IS NOT NULL
                                  OR self_insurance_flag IS NOT NULL)
                       )::int AS route_information_populated,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND insurance_intermediary_name IS NOT NULL
                       )::int AS intermediary_populated,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND insurance_misp_code IS NOT NULL
                       )::int AS misp_code_populated,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND actual_premium_amount IS NOT NULL
                       )::int AS premium_populated,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND actual_premium_amount IS NOT NULL
                             AND standard_premium_amount IS NOT NULL
                       )::int AS premium_comparable,
                       COALESCE(sum(actual_premium_amount) FILTER (
                           WHERE has_booking AND has_insurance
                       ),0) AS premium_amount,
                       COALESCE(sum(standard_premium_amount) FILTER (
                           WHERE has_booking AND has_insurance
                       ),0) AS standard_premium_amount,
                       COALESCE(sum(actual_premium_amount-standard_premium_amount) FILTER (
                           WHERE has_booking AND has_insurance
                             AND actual_premium_amount IS NOT NULL
                             AND standard_premium_amount IS NOT NULL
                       ),0) AS premium_variance_amount,
                       round(avg(actual_premium_amount) FILTER (
                           WHERE has_booking AND has_insurance AND actual_premium_amount IS NOT NULL
                       ),2) AS avg_premium,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance AND self_insurance_flag IS NOT NULL
                       )::int AS self_insurance_flag_populated,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance AND self_insurance_flag IS TRUE
                       )::int AS self_insurance_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE has_booking AND has_insurance
                             AND insurance_add_ons IS NOT NULL
                             AND jsonb_typeof(insurance_add_ons)='array'
                             AND jsonb_array_length(insurance_add_ons)>0
                       )::int AS policies_with_add_ons
                FROM insurance_facts
                """,
            )
        )
        total_journeys = int(summary.get("booked_journeys") or 0)
        insurance_journeys = int(summary.get("insurance_journeys") or 0)
        summary["insurance_record_rate_pct"] = _rate(insurance_journeys, total_journeys)
        summary["route_information_coverage_pct"] = _rate(
            int(summary.get("route_information_populated") or 0), insurance_journeys
        )
        summary["explicit_route_coverage_pct"] = _rate(
            int(summary.get("explicit_route_captured") or 0), insurance_journeys
        )
        summary["intermediary_coverage_pct"] = _rate(
            int(summary.get("intermediary_populated") or 0), insurance_journeys
        )
        summary["misp_code_coverage_pct"] = _rate(
            int(summary.get("misp_code_populated") or 0), insurance_journeys
        )
        summary["premium_coverage_pct"] = _rate(
            int(summary.get("premium_populated") or 0), insurance_journeys
        )
        summary["add_on_policy_attach_pct"] = _rate(
            summary.get("policies_with_add_ons") or 0,
            insurance_journeys,
        )

        by_insurer = _rows(
            connection,
            latest,
            tenant_id,
            insurer_scope
            + """
            SELECT normalized_insurer_name AS insurer_name,
                   count(DISTINCT journey_id)::int AS policy_count,
                   count(DISTINCT journey_id) FILTER (
                       WHERE actual_premium_amount IS NOT NULL
                   )::int AS premium_populated,
                   COALESCE(sum(actual_premium_amount),0) AS premium_amount,
                   round(avg(actual_premium_amount) FILTER (
                       WHERE actual_premium_amount IS NOT NULL
                   ),2) AS avg_premium
            FROM normalized_insurers
            GROUP BY normalized_insurer_name
            ORDER BY policy_count DESC, normalized_insurer_name
            """,
        )
        by_insurer_outlet = _rows(
            connection,
            latest,
            tenant_id,
            insurer_scope
            + """
            SELECT dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   normalized_insurer_name AS insurer_name,
                   count(DISTINCT journey_id)::int AS policy_count,
                   count(DISTINCT journey_id) FILTER (
                       WHERE actual_premium_amount IS NOT NULL
                   )::int AS premium_populated,
                   COALESCE(sum(actual_premium_amount),0) AS premium_amount,
                   round(avg(actual_premium_amount) FILTER (
                       WHERE actual_premium_amount IS NOT NULL
                   ),2) AS avg_premium
            FROM normalized_insurers
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name, normalized_insurer_name
            ORDER BY dealer_name, outlet_name, policy_count DESC, normalized_insurer_name
            """,
        )
        by_source = _rows(
            connection,
            latest,
            tenant_id,
            insurance_scope
            + """
            SELECT COALESCE(
                       NULLIF(insurance_by,''),
                       NULLIF(insurance_source,''),
                       'Unspecified in Audit Core'
                   ) AS insurance_by,
                   count(DISTINCT journey_id)::int AS policy_count
            FROM insurance_facts
            WHERE has_booking AND has_insurance
            GROUP BY 1
            ORDER BY policy_count DESC, insurance_by
            """,
        )
        by_source_outlet = _rows(
            connection,
            latest,
            tenant_id,
            insurance_scope
            + """
            SELECT dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   insurance_route AS source_assessment,
                   COALESCE(NULLIF(trim(insurance_by),''),'(blank)') AS insurance_by,
                   COALESCE(NULLIF(trim(insurance_source),''),'(blank)') AS insurance_source,
                   COALESCE(self_insurance_flag::text,'(blank)') AS self_insurance_flag,
                   COALESCE(NULLIF(trim(insurance_misp_code),''),'(blank)') AS misp_code,
                   COALESCE(NULLIF(trim(insurance_intermediary_code),''),'(blank)')
                       AS intermediary_code,
                   COALESCE(NULLIF(trim(insurance_intermediary_name),''),'(blank)')
                       AS intermediary_name,
                   COALESCE(NULLIF(trim(insurer_name),''),'(blank)') AS insurer_name,
                   count(DISTINCT journey_id)::int AS policy_count,
                   count(DISTINCT journey_id) FILTER (
                       WHERE actual_premium_amount IS NOT NULL
                   )::int AS premium_populated,
                   COALESCE(sum(actual_premium_amount),0) AS premium_amount
            FROM insurance_facts
            WHERE has_booking AND has_insurance
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name, insurance_route,
                     insurance_by, insurance_source, self_insurance_flag,
                     insurance_misp_code, insurance_intermediary_code,
                     insurance_intermediary_name, insurer_name
            ORDER BY dealer_name, outlet_name, policy_count DESC, insurance_route,
                     insurance_by, insurance_source
            """,
        )
        by_route = _rows(
            connection,
            latest,
            tenant_id,
            insurance_scope
            + """
            SELECT insurance_route AS route,
                   count(DISTINCT journey_id)::int AS policy_count,
                   count(DISTINCT journey_id) FILTER (
                       WHERE actual_premium_amount IS NOT NULL
                   )::int AS premium_populated,
                   COALESCE(sum(actual_premium_amount),0) AS premium_amount,
                   round(avg(actual_premium_amount) FILTER (
                       WHERE actual_premium_amount IS NOT NULL
                   ),2) AS avg_premium
            FROM insurance_facts
            WHERE has_booking AND has_insurance
            GROUP BY insurance_route
            ORDER BY policy_count DESC, insurance_route
            """,
        )
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            insurance_scope
            + """
            SELECT dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   count(DISTINCT journey_id) FILTER (WHERE has_booking)::int AS booked_journeys,
                   count(DISTINCT journey_id) FILTER (
                       WHERE has_booking AND has_insurance
                   )::int AS insurance_journeys,
                   count(DISTINCT journey_id) FILTER (
                       WHERE has_booking AND has_insurance
                         AND insurance_route='DEALER_ARRANGED'
                   )::int AS dealer_arranged_journeys,
                   count(DISTINCT journey_id) FILTER (
                       WHERE has_booking AND has_insurance
                         AND insurance_route='CUSTOMER_ARRANGED'
                   )::int AS customer_arranged_journeys,
                   count(DISTINCT journey_id) FILTER (
                       WHERE has_booking AND has_insurance
                         AND insurance_route='MIXED_OR_UNCLEAR'
                   )::int AS source_ambiguous_journeys,
                   count(DISTINCT journey_id) FILTER (
                       WHERE has_booking AND has_insurance
                         AND insurance_route='UNSPECIFIED'
                   )::int AS source_unknown_journeys,
                   count(DISTINCT journey_id) FILTER (
                       WHERE has_booking AND has_insurance
                         AND actual_premium_amount IS NOT NULL
                   )::int AS premium_populated,
                   count(DISTINCT journey_id) FILTER (
                       WHERE has_booking AND has_insurance
                         AND actual_premium_amount IS NOT NULL
                         AND standard_premium_amount IS NOT NULL
                   )::int AS premium_comparable,
                   COALESCE(sum(actual_premium_amount) FILTER (
                       WHERE has_booking AND has_insurance
                   ),0) AS premium_amount,
                   COALESCE(sum(standard_premium_amount) FILTER (
                       WHERE has_booking AND has_insurance
                   ),0) AS standard_premium_amount,
                   COALESCE(sum(actual_premium_amount-standard_premium_amount) FILTER (
                       WHERE has_booking AND has_insurance
                         AND actual_premium_amount IS NOT NULL
                         AND standard_premium_amount IS NOT NULL
                   ),0) AS premium_variance_amount,
                   round(avg(actual_premium_amount) FILTER (
                       WHERE has_booking AND has_insurance
                         AND actual_premium_amount IS NOT NULL
                   ),2) AS avg_premium
            FROM insurance_facts
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name
            HAVING count(DISTINCT journey_id) FILTER (
                WHERE has_booking
            ) > 0
            ORDER BY insurance_journeys DESC, dealer_name, outlet_name
            """,
        )
        add_ons = _rows(
            connection,
            latest,
            tenant_id,
            insurance_scope
            + """
            , add_on_rows AS (
                SELECT i.dealer_id,
                       i.dealer_name,
                       i.outlet_id,
                       i.outlet_name,
                       i.journey_id,
                       trim(add_on.add_on_name) AS raw_name,
                       regexp_replace(upper(trim(add_on.add_on_name)),
                                      '[^A-Z0-9]', '', 'g') AS normalized_name
                FROM insurance_facts i
                CROSS JOIN LATERAL jsonb_array_elements_text(
                    CASE
                        WHEN jsonb_typeof(i.insurance_add_ons)='array'
                            THEN i.insurance_add_ons
                        ELSE '[]'::jsonb
                    END
                ) AS add_on(add_on_name)
                WHERE i.has_booking AND i.has_insurance
            ), categorized AS (
                SELECT dealer_id,
                       dealer_name,
                       outlet_id,
                       outlet_name,
                       journey_id,
                       raw_name,
                       CASE
                           WHEN normalized_name LIKE '%DEPRECIATION%'
                             OR normalized_name LIKE '%NILDEP%'
                             OR normalized_name LIKE '%ZERODEP%'
                               THEN 'Zero depreciation'
                           WHEN normalized_name='RTI'
                             OR normalized_name LIKE 'RETURNTOINVOICE%'
                               THEN 'Return to invoice'
                           WHEN normalized_name LIKE '%CONSUMABLE%'
                               THEN 'Consumables'
                           WHEN normalized_name LIKE '%ENGINE%'
                             OR normalized_name LIKE '%GEARBOX%'
                               THEN 'Engine / gearbox protection'
                           WHEN normalized_name IN ('RSA','ROADSIDEASSISTANCE')
                               THEN 'Roadside assistance'
                           WHEN normalized_name LIKE '%KEYREPLAC%'
                               THEN 'Key replacement'
                           WHEN normalized_name LIKE '%TYRE%'
                             OR normalized_name LIKE '%TIRE%'
                               THEN 'Tyre protection'
                           WHEN normalized_name LIKE '%PERSONALBELONG%'
                               THEN 'Personal belongings'
                           WHEN normalized_name LIKE '%IMT23%'
                               THEN 'IMT-23 extension'
                           WHEN normalized_name LIKE '%TOWING%'
                               THEN 'Towing cover'
                           WHEN normalized_name LIKE '%DRIVER%'
                               THEN 'Driver liability cover'
                           ELSE 'Other / unclear cover'
                       END AS add_on_family
                FROM add_on_rows
                WHERE raw_name<>''
            )
            SELECT dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   add_on_family AS add_on_name,
                   count(DISTINCT journey_id)::int AS policy_count,
                   array_agg(DISTINCT raw_name ORDER BY raw_name) AS raw_terms
            FROM categorized
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name, add_on_family
            ORDER BY policy_count DESC, dealer_name, outlet_name, add_on_family
            """,
        )
        by_outlet_rates = {
            (row.get("dealer_id"), row.get("outlet_id")): {
                "booked": int(row.get("booked_journeys") or 0),
                "insured": int(row.get("insurance_journeys") or 0),
            }
            for row in by_outlet
        }
        for row in add_ons:
            counts = by_outlet_rates.get((row.get("dealer_id"), row.get("outlet_id")), {})
            row["policy_attach_pct"] = _rate(
                int(row.get("policy_count") or 0),
                counts.get("insured", 0),
            )
            row["journey_penetration_pct"] = _rate(
                int(row.get("policy_count") or 0),
                counts.get("booked", 0),
            )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "by_insurer": by_insurer,
        "by_insurer_outlet": by_insurer_outlet,
        "by_source": by_source,
        "by_source_outlet": by_source_outlet,
        "by_route": by_route,
        "by_outlet": by_outlet,
        "add_ons": add_ons,
        "definition": (
            "Insurance record rate is captured policy records divided by booked journeys; it is not "
            "proof that unrecorded journeys were uninsured. The current Audit Core insurance record "
            "stores insurer, premium, self_insurance_flag, intermediary, MISP code and policy add-ons, "
            "but does not persist a typed dealer-versus-customer route. Dealer-arranged versus "
            "customer/self-arranged is counted only when a legacy explicit route label exists or the "
            "self-insurance flag is true; a false or blank flag is not treated as proof of dealer "
            "arrangement. Unknown route counts remain visible. Policy components and insurer premium "
            "are broken out by dealership and outlet; they are captured premium values, not dealership "
            "revenue."
        ),
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
                SELECT count(DISTINCT row_data->>'journey_id')::int AS journeys
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
                """,
            )
        )
        total_journeys = int(total_row.get("journeys") or 0)
        by_type = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT upper(COALESCE(a.row_data->>'addon_type_code','UNSPECIFIED')) AS addon_type,
                   count(*)::int AS record_count,
                   count(DISTINCT a.row_data->>'journey_id')::int AS journey_count,
                   COALESCE(sum(NULLIF(a.row_data->>'actual_amount','')::numeric),0)
                       AS actual_amount,
                   round(avg(NULLIF(a.row_data->>'actual_amount','')::numeric),2)
                       AS avg_record_amount
            FROM analytics.snapshot_rows a
            WHERE a.tenant_id=:tenant_id AND a.dump_id=:dump_id AND a.source_table='journey_addons'
              AND EXISTS (
                  SELECT 1 FROM analytics.snapshot_rows b
                  WHERE b.tenant_id=:tenant_id AND b.dump_id=:dump_id
                    AND b.source_table='bookings'
                    AND b.row_data->>'journey_id'=a.row_data->>'journey_id'
              )
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
                SELECT a.row_data->>'journey_id' AS journey_id,
                       upper(COALESCE(a.row_data->>'addon_type_code','UNSPECIFIED')) AS addon_type,
                       NULLIF(a.row_data->>'actual_amount','')::numeric AS actual_amount
                FROM analytics.snapshot_rows a
                WHERE a.tenant_id=:tenant_id AND a.dump_id=:dump_id
                  AND a.source_table='journey_addons'
                  AND EXISTS (
                      SELECT 1 FROM analytics.snapshot_rows b
                      WHERE b.tenant_id=:tenant_id AND b.dump_id=:dump_id
                        AND b.source_table='bookings'
                        AND b.row_data->>'journey_id'=a.row_data->>'journey_id'
                  )
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
        by_product_type = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT df.dealer_id,
                   df.dealer_name,
                   df.outlet_id,
                   df.outlet_name,
                   COALESCE(df.model_name,'Unresolved') AS model_name,
                   COALESCE(df.variant_name,'Unresolved') AS variant_name,
                   upper(COALESCE(a.row_data->>'addon_type_code','UNSPECIFIED')) AS addon_type,
                   count(*)::int AS record_count,
                   count(DISTINCT df.journey_id)::int AS journey_count,
                   count(*) FILTER (
                       WHERE NULLIF(a.row_data->>'actual_amount','') IS NOT NULL
                   )::int AS priced_records,
                   COALESCE(sum(NULLIF(a.row_data->>'actual_amount','')::numeric),0)
                       AS actual_amount,
                   round(avg(NULLIF(a.row_data->>'actual_amount','')::numeric),2)
                       AS avg_record_amount
            FROM deal_facts df
            JOIN analytics.snapshot_rows a
              ON a.tenant_id=:tenant_id
             AND a.dump_id=:dump_id
             AND a.source_table='journey_addons'
             AND a.row_data->>'journey_id'=df.journey_id
            WHERE df.has_booking
            GROUP BY df.dealer_id, df.dealer_name, df.outlet_id, df.outlet_name,
                     df.model_name, df.variant_name, addon_type
            ORDER BY actual_amount DESC, journey_count DESC,
                     df.dealer_name, df.outlet_name, model_name, variant_name, addon_type
            """,
        )
        warranty_rsa_cte = _DEAL_FACTS_CTE + """
        , warranty_rsa_source_rows AS (
            SELECT df.dealer_id,
                   df.dealer_name,
                   df.outlet_id,
                   df.outlet_name,
                   COALESCE(NULLIF(df.model_name,''),'Unresolved') AS model_name,
                   COALESCE(NULLIF(df.variant_name,''),'Unresolved') AS variant_name,
                   df.journey_id,
                   'journey_addons'::text AS source_kind,
                   a.source_pk AS source_record_id,
                   upper(COALESCE(a.row_data->>'addon_type_code','')) AS addon_type_code,
                   concat_ws(' ',
                       a.row_data->>'addon_type_code',
                       a.row_data->>'details',
                       a.row_data->>'provider_name',
                       a.row_data->>'reference_number'
                   ) AS evidence_text
            FROM deal_facts df
            JOIN analytics.snapshot_rows a
              ON a.tenant_id=:tenant_id
             AND a.dump_id=:dump_id
             AND a.source_table='journey_addons'
             AND a.row_data->>'journey_id'=df.journey_id
            WHERE df.has_booking
            UNION ALL
            SELECT df.dealer_id,
                   df.dealer_name,
                   df.outlet_id,
                   df.outlet_name,
                   COALESCE(NULLIF(df.model_name,''),'Unresolved') AS model_name,
                   COALESCE(NULLIF(df.variant_name,''),'Unresolved') AS variant_name,
                   df.journey_id,
                   'accessory_tax_invoice'::text AS source_kind,
                   a.source_pk AS source_record_id,
                   ''::text AS addon_type_code,
                   concat_ws(' ', a.row_data->>'description', a.row_data->>'item_code',
                       a.row_data->>'line_category') AS evidence_text
            FROM deal_facts df
            JOIN analytics.snapshot_rows a
              ON a.tenant_id=:tenant_id
             AND a.dump_id=:dump_id
             AND a.source_table='derived_accessory_line_items'
             AND a.row_data->>'journey_id'=df.journey_id
            WHERE df.has_booking
              AND upper(COALESCE(a.row_data->>'invoice_nature',''))='TAX_INVOICE'
            UNION ALL
            SELECT df.dealer_id,
                   df.dealer_name,
                   df.outlet_id,
                   df.outlet_name,
                   COALESCE(NULLIF(df.model_name,''),'Unresolved') AS model_name,
                   COALESCE(NULLIF(df.variant_name,''),'Unresolved') AS variant_name,
                   df.journey_id,
                   'delivery_tax_invoice'::text AS source_kind,
                   a.source_pk AS source_record_id,
                   ''::text AS addon_type_code,
                   concat_ws(' ', a.row_data->>'description', a.row_data->>'item_code',
                       a.row_data->>'line_category') AS evidence_text
            FROM deal_facts df
            JOIN analytics.snapshot_rows a
              ON a.tenant_id=:tenant_id
             AND a.dump_id=:dump_id
             AND a.source_table='derived_delivery_invoice_line_items'
             AND a.row_data->>'journey_id'=df.journey_id
            WHERE df.has_booking
              AND df.actual_delivered_at IS NOT NULL
              AND upper(COALESCE(a.row_data->>'invoice_nature',''))='TAX_INVOICE'
              AND upper(COALESCE(a.row_data->>'invoice_purpose',''))<>'ACCESSORY'
        ), classified_warranty_rsa AS (
            SELECT *,
                   CASE
                       WHEN addon_type_code='EW' OR evidence_text ~* '(extended[[:space:]_-]*warrant|warrant)'
                           THEN 'EW'
                       WHEN addon_type_code='RSA'
                         OR evidence_text ~* '(road[[:space:]_-]*side[[:space:]_-]*assist|roadside|(^|[^A-Z0-9])RSA([^A-Z0-9]|$))'
                           THEN 'RSA'
                   END AS product_type,
                   CASE
                       WHEN evidence_text ~* '(^|[^0-9])4[[:space:]_-]*(years?|yrs?|y)([^A-Z0-9]|$)|48[[:space:]_-]*months?|four[[:space:]_-]*years?'
                           THEN 'FOUR_YEAR'
                       WHEN evidence_text ~* '(^|[^0-9])5[[:space:]_-]*(years?|yrs?|y)([^A-Z0-9]|$)|60[[:space:]_-]*months?|five[[:space:]_-]*years?'
                           THEN 'FIVE_YEAR'
                   END AS warranty_term
            FROM warranty_rsa_source_rows
        )
        """
        warranty_rsa_summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                warranty_rsa_cte
                + """
                SELECT count(*) FILTER (WHERE product_type='EW')::int AS ew_record_count,
                       count(DISTINCT journey_id) FILTER (WHERE product_type='EW')::int
                           AS ew_journey_count,
                       count(DISTINCT journey_id) FILTER (
                           WHERE product_type='EW' AND warranty_term='FOUR_YEAR'
                       )::int AS ew_four_year_journey_count,
                       count(DISTINCT journey_id) FILTER (
                           WHERE product_type='EW' AND warranty_term='FIVE_YEAR'
                       )::int AS ew_five_year_journey_count,
                       count(DISTINCT journey_id) FILTER (
                           WHERE product_type='EW' AND warranty_term IS NULL
                       )::int AS ew_term_not_recorded_journey_count,
                       count(*) FILTER (WHERE product_type='RSA')::int AS rsa_record_count,
                       count(DISTINCT journey_id) FILTER (WHERE product_type='RSA')::int
                           AS rsa_journey_count
                FROM classified_warranty_rsa
                WHERE product_type IS NOT NULL
                """,
            )
        )
        warranty_rsa_by_product = _rows(
            connection,
            latest,
            tenant_id,
            warranty_rsa_cte
            + """
            SELECT dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   model_name,
                   variant_name,
                   product_type,
                   count(*)::int AS source_record_count,
                   count(DISTINCT journey_id)::int AS journey_count,
                   count(DISTINCT journey_id) FILTER (
                       WHERE warranty_term='FOUR_YEAR'
                   )::int AS four_year_journeys,
                   count(DISTINCT journey_id) FILTER (
                       WHERE warranty_term='FIVE_YEAR'
                   )::int AS five_year_journeys,
                   count(DISTINCT journey_id) FILTER (
                       WHERE product_type='EW' AND warranty_term IS NULL
                   )::int AS term_not_recorded_journeys,
                   count(DISTINCT journey_id) FILTER (
                       WHERE source_kind='journey_addons'
                   )::int AS addon_source_journeys,
                   count(DISTINCT journey_id) FILTER (
                       WHERE source_kind IN ('accessory_tax_invoice','delivery_tax_invoice')
                   )::int AS invoice_source_journeys
            FROM classified_warranty_rsa
            WHERE product_type IS NOT NULL
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name,
                     model_name, variant_name, product_type
            ORDER BY product_type, dealer_name, outlet_name, journey_count DESC,
                     model_name, variant_name
            """,
        )
        accessory_cost_by_product = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            , accessory_lines AS (
                SELECT df.dealer_id,
                       df.dealer_name,
                       df.outlet_id,
                       df.outlet_name,
                       COALESCE(df.model_name,'Unresolved') AS model_name,
                       COALESCE(df.variant_name,'Unresolved') AS variant_name,
                       NULLIF(a.row_data->>'standard_amount','')::numeric AS standard_amount,
                       NULLIF(a.row_data->>'actual_amount','')::numeric AS actual_amount
                FROM deal_facts df
                JOIN analytics.snapshot_rows a
                  ON a.tenant_id=:tenant_id
                 AND a.dump_id=:dump_id
                 AND a.source_table='commercial_lines'
                 AND a.row_data->>'journey_id'=df.journey_id
                 AND a.row_data->>'component_key'='accessories_cost'
                WHERE df.has_booking
            )
            SELECT dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   model_name,
                   variant_name,
                   count(*)::int AS evidence_rows,
                   count(*) FILTER (WHERE standard_amount IS NOT NULL)::int
                       AS standard_amount_rows,
                   count(*) FILTER (WHERE actual_amount IS NOT NULL)::int
                       AS actual_amount_rows,
                   count(*) FILTER (
                       WHERE standard_amount IS NOT NULL AND actual_amount IS NOT NULL
                   )::int AS comparable_rows,
                   sum(standard_amount) FILTER (
                       WHERE standard_amount IS NOT NULL AND actual_amount IS NOT NULL
                   ) AS comparable_standard_amount,
                   sum(actual_amount) FILTER (
                       WHERE standard_amount IS NOT NULL AND actual_amount IS NOT NULL
                   ) AS comparable_actual_amount,
                   sum(actual_amount-standard_amount) FILTER (
                       WHERE standard_amount IS NOT NULL AND actual_amount IS NOT NULL
                   ) AS comparable_actual_minus_standard
            FROM accessory_lines
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name,
                     model_name, variant_name
            ORDER BY comparable_actual_minus_standard ASC NULLS FIRST,
                     comparable_actual_amount DESC NULLS LAST,
                     dealer_name, outlet_name, model_name, variant_name
            """,
        )
        accessory_line_coverage = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                """
                WITH line_rows AS (
                    SELECT source_pk, row_data
                    FROM analytics.snapshot_rows
                    WHERE tenant_id=:tenant_id AND dump_id=:dump_id
                      AND source_table='derived_accessory_line_items'
                ), invoice_documents AS (
                    SELECT split_part(source_pk,':',1) AS document_key,
                           max(upper(COALESCE(row_data->>'invoice_nature','')))
                               AS invoice_nature,
                           max(NULLIF(row_data->>'source_document_count','')::int)
                               AS source_document_count
                    FROM line_rows
                    GROUP BY split_part(source_pk,':',1)
                )
                SELECT (SELECT count(*)::int FROM line_rows) AS total_invoice_lines,
                       (SELECT count(*)::int FROM line_rows
                        WHERE upper(COALESCE(row_data->>'invoice_nature',''))='TAX_INVOICE')
                           AS confirmed_tax_invoice_lines,
                       (SELECT count(*)::int FROM invoice_documents
                        WHERE invoice_nature='TAX_INVOICE') AS confirmed_tax_invoices,
                       (SELECT count(*)::int FROM line_rows
                        WHERE upper(COALESCE(row_data->>'invoice_nature',''))='PROFORMA_INVOICE')
                           AS proforma_lines,
                       (SELECT count(*)::int FROM line_rows
                        WHERE upper(COALESCE(row_data->>'invoice_nature','')) NOT IN
                            ('TAX_INVOICE','PROFORMA_INVOICE')) AS unclassified_lines,
                       (SELECT count(*)::int FROM line_rows
                        WHERE upper(COALESCE(row_data->>'invoice_nature',''))='TAX_INVOICE'
                          AND COALESCE(NULLIF(row_data->>'description',''),
                                       NULLIF(row_data->>'item_code','')) IS NOT NULL)
                           AS confirmed_lines_with_item_label,
                       (SELECT count(*)::int FROM line_rows
                        WHERE upper(COALESCE(row_data->>'invoice_nature',''))='TAX_INVOICE'
                          AND NULLIF(row_data->>'quantity','') IS NOT NULL)
                           AS confirmed_lines_with_quantity,
                       (SELECT count(*)::int FROM line_rows
                        WHERE upper(COALESCE(row_data->>'invoice_nature',''))='TAX_INVOICE'
                          AND NULLIF(row_data->>'net_amount','') IS NOT NULL)
                           AS confirmed_lines_with_net_amount,
                       (SELECT count(*)::int FROM invoice_documents
                        WHERE source_document_count > 1) AS duplicate_invoice_groups,
                       (SELECT COALESCE(sum(source_document_count-1),0)::int
                        FROM invoice_documents WHERE source_document_count > 1)
                           AS duplicate_source_documents
                """,
            )
        )
        accessory_items = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            , confirmed_accessory_lines AS (
                SELECT a.source_pk,
                       a.row_data,
                       df.dealer_id,
                       df.dealer_name,
                       df.outlet_id,
                       df.outlet_name,
                       COALESCE(NULLIF(df.model_name,''),'Unresolved') AS model_name,
                       COALESCE(NULLIF(df.variant_name,''),'Unresolved') AS variant_name
                FROM analytics.snapshot_rows a
                JOIN deal_facts df
                  ON df.journey_id=a.row_data->>'journey_id'
                WHERE a.tenant_id=:tenant_id AND a.dump_id=:dump_id
                  AND a.source_table='derived_accessory_line_items'
                  AND upper(COALESCE(a.row_data->>'invoice_nature',''))='TAX_INVOICE'
                  AND df.has_booking
            )
            SELECT dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   model_name,
                   variant_name,
                   COALESCE(NULLIF(row_data->>'line_category',''),'Uncategorized')
                       AS line_category,
                   COALESCE(NULLIF(row_data->>'description',''),
                            NULLIF(row_data->>'item_code',''),'Unlabelled accessory')
                       AS item_description,
                   NULLIF(row_data->>'item_code','') AS item_code,
                   string_agg(DISTINCT NULLIF(row_data->>'source_system',''), ', '
                       ORDER BY NULLIF(row_data->>'source_system','')) AS invoice_sources,
                   count(*)::int AS invoice_lines,
                   count(DISTINCT split_part(source_pk,':',1))::int AS invoice_count,
                   sum(NULLIF(row_data->>'quantity','')::numeric) AS invoiced_quantity,
                   count(*) FILTER (
                       WHERE NULLIF(row_data->>'quantity','') IS NOT NULL
                   )::int AS lines_with_quantity,
                   sum(NULLIF(row_data->>'net_amount','')::numeric) AS net_amount,
                   count(*) FILTER (
                       WHERE NULLIF(row_data->>'net_amount','') IS NOT NULL
                   )::int AS lines_with_net_amount,
                   sum(NULLIF(row_data->>'discount_amount','')::numeric) AS line_discount_amount
            FROM confirmed_accessory_lines
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name,
                     model_name, variant_name, line_category,
                     item_description, item_code
            ORDER BY net_amount DESC NULLS LAST, invoice_lines DESC,
                     dealer_name, outlet_name, model_name, variant_name,
                     item_description, item_code
            """,
        )
        by_model_outlet = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_name,
                   outlet_name,
                   COALESCE(NULLIF(model_name,''),'Unresolved') AS model_name,
                   COALESCE(NULLIF(variant_name,''),'Unresolved') AS variant_name,
                   count(*) FILTER (WHERE has_booking)::int AS booked_journeys,
                   count(*) FILTER (WHERE has_booking AND has_accessories)::int AS attached_journeys,
                   COALESCE(sum(accessories_value) FILTER (WHERE has_booking),0) AS actual_amount,
                   round(100.0 * count(*) FILTER (WHERE has_booking AND has_accessories)
                         / NULLIF(count(*) FILTER (WHERE has_booking),0),1) AS penetration_pct
            FROM deal_facts
            GROUP BY dealer_name, outlet_name, model_name, variant_name
            HAVING count(*) FILTER (WHERE has_booking AND has_accessories) > 0
            ORDER BY actual_amount DESC, attached_journeys DESC,
                     dealer_name, outlet_name, model_name, variant_name
            """,
        )
        vas_reconciliation_cte = _DEAL_FACTS_CTE + """
        , vas_categories(category) AS (
            VALUES ('EW'), ('INSURANCE'), ('RSA'), ('FASTAG'), ('ACCESSORIES')
        ), proposal_evidence AS (
            SELECT df.dealer_id,
                   df.dealer_name,
                   df.outlet_id,
                   df.outlet_name,
                   COALESCE(NULLIF(df.model_name,''),'Unresolved') AS model_name,
                   COALESCE(NULLIF(df.variant_name,''),'Unresolved') AS variant_name,
                   df.journey_id,
                   CASE
                       WHEN upper(COALESCE(a.row_data->>'addon_type_code',''))
                                IN ('EW','EXTENDED_WARRANTY','EXTENDED WARRANTY')
                         OR upper(COALESCE(a.row_data->>'addon_type_code','')) LIKE '%WARRANT%'
                           THEN 'EW'
                       WHEN upper(COALESCE(a.row_data->>'addon_type_code',''))
                                IN ('RSA','ROADSIDE_ASSISTANCE','ROADSIDE ASSISTANCE')
                         OR upper(COALESCE(a.row_data->>'addon_type_code','')) LIKE '%ROAD%'
                           THEN 'RSA'
                       WHEN upper(COALESCE(a.row_data->>'addon_type_code',''))
                                IN ('FASTAG','FAST_TAG','FAST TAG')
                         OR upper(COALESCE(a.row_data->>'addon_type_code','')) LIKE '%FAST%TAG%'
                           THEN 'FASTAG'
                       WHEN upper(COALESCE(a.row_data->>'addon_type_code',''))
                                IN ('INS','INSURANCE','MOTOR_INSURANCE','INSURANCE_PREMIUM')
                         OR upper(COALESCE(a.row_data->>'addon_type_code','')) LIKE '%INSUR%'
                           THEN 'INSURANCE'
                       WHEN upper(COALESCE(a.row_data->>'addon_type_code',''))
                                IN ('ACCESSORY','ACCESSORIES','ACCESSORIES_TOTAL')
                         OR upper(COALESCE(a.row_data->>'addon_type_code','')) LIKE '%ACCESSOR%'
                           THEN 'ACCESSORIES'
                   END AS category
            FROM deal_facts df
            JOIN analytics.snapshot_rows a
              ON a.tenant_id=:tenant_id
             AND a.dump_id=:dump_id
             AND a.source_table='journey_addons'
             AND a.row_data->>'journey_id'=df.journey_id
            WHERE df.has_booking
        ), proposal_by_journey AS (
            SELECT dealer_id, dealer_name, outlet_id, outlet_name,
                   model_name, variant_name, journey_id, category,
                   count(*)::int AS proposal_record_count
            FROM proposal_evidence
            WHERE category IS NOT NULL
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name,
                     model_name, variant_name, journey_id, category
        ), delivery_invoice_candidates AS (
            SELECT df.dealer_id,
                   df.dealer_name,
                   df.outlet_id,
                   df.outlet_name,
                   COALESCE(NULLIF(df.model_name,''),'Unresolved') AS model_name,
                   COALESCE(NULLIF(df.variant_name,''),'Unresolved') AS variant_name,
                   df.journey_id,
                   split_part(a.source_pk,':',1) AS invoice_document_id,
                   a.source_pk AS invoice_line_id,
                   upper(COALESCE(a.row_data->>'invoice_nature','')) AS invoice_nature,
                   upper(COALESCE(a.row_data->>'invoice_purpose','')) AS invoice_purpose,
                   CASE
                       WHEN concat_ws(' ',
                                a.row_data->>'description', a.row_data->>'item_code',
                                a.row_data->>'line_category', a.row_data->>'invoice_purpose'
                            ) ~* '(extended[[:space:]_-]*warrant|(^|[^A-Z0-9])warranty([^A-Z0-9]|$))'
                           THEN 'EW'
                       WHEN concat_ws(' ',
                                a.row_data->>'description', a.row_data->>'item_code',
                                a.row_data->>'line_category', a.row_data->>'invoice_purpose'
                            ) ~* '(road[[:space:]_-]*side[[:space:]_-]*assist|roadside|(^|[^A-Z0-9])RSA([^A-Z0-9]|$))'
                           THEN 'RSA'
                       WHEN concat_ws(' ',
                                a.row_data->>'description', a.row_data->>'item_code',
                                a.row_data->>'line_category', a.row_data->>'invoice_purpose'
                            ) ~* 'fast[[:space:]_-]*tag'
                           THEN 'FASTAG'
                       WHEN concat_ws(' ',
                                a.row_data->>'description', a.row_data->>'item_code',
                                a.row_data->>'line_category', a.row_data->>'invoice_purpose'
                            ) ~* '(insurance|premium)'
                           THEN 'INSURANCE'
                       WHEN upper(COALESCE(a.row_data->>'line_category','')) ~ 'ACCESSOR'
                         OR upper(COALESCE(a.row_data->>'invoice_purpose',''))='ACCESSORY'
                           THEN 'ACCESSORIES'
                   END AS category
            FROM deal_facts df
            JOIN analytics.snapshot_rows a
              ON a.tenant_id=:tenant_id
             AND a.dump_id=:dump_id
             AND a.source_table IN (
                 'derived_delivery_invoice_line_items',
                 'derived_unverified_delivery_invoice_line_items'
             )
             AND a.row_data->>'journey_id'=df.journey_id
            WHERE df.has_booking
              AND df.actual_delivered_at IS NOT NULL
        ), delivery_invoice_lines AS (
            SELECT *
            FROM delivery_invoice_candidates
            WHERE invoice_nature='TAX_INVOICE'
        ), unverified_delivery_invoice_lines AS (
            SELECT *
            FROM delivery_invoice_candidates
            WHERE invoice_nature='UNKNOWN'
        ), invoice_coverage_by_journey AS (
            SELECT journey_id,
                   count(DISTINCT invoice_document_id)::int AS invoice_document_count
            FROM delivery_invoice_lines
            GROUP BY journey_id
        ), invoice_purchase_by_journey AS (
            SELECT dealer_id, dealer_name, outlet_id, outlet_name,
                   model_name, variant_name, journey_id, category,
                   count(DISTINCT invoice_line_id)::int AS invoice_line_count,
                   count(DISTINCT invoice_document_id)::int AS invoice_document_count,
                   string_agg(DISTINCT NULLIF(invoice_purpose,''), ', '
                       ORDER BY NULLIF(invoice_purpose,'')) AS invoice_purposes
            FROM delivery_invoice_lines
            WHERE category IS NOT NULL
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name,
                     model_name, variant_name, journey_id, category
        ), unverified_invoice_purchase_by_journey AS (
            SELECT dealer_id, dealer_name, outlet_id, outlet_name,
                   model_name, variant_name, journey_id, category,
                   count(DISTINCT invoice_line_id)::int AS invoice_line_count,
                   count(DISTINCT invoice_document_id)::int AS invoice_document_count,
                   string_agg(DISTINCT NULLIF(invoice_purpose,''), ', '
                       ORDER BY NULLIF(invoice_purpose,'')) AS invoice_purposes
            FROM unverified_delivery_invoice_lines
            WHERE category IS NOT NULL
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name,
                     model_name, variant_name, journey_id, category
        ), booked_journey_categories AS (
            SELECT df.dealer_id,
                   df.dealer_name,
                   df.outlet_id,
                   df.outlet_name,
                   COALESCE(NULLIF(df.model_name,''),'Unresolved') AS model_name,
                   COALESCE(NULLIF(df.variant_name,''),'Unresolved') AS variant_name,
                   df.journey_id,
                   df.actual_delivered_at,
                   c.category,
                   COALESCE(p.proposal_record_count,0) AS proposal_record_count,
                   COALESCE(df.has_delivery_record,false) AS has_delivery_record,
                   COALESCE(i.invoice_line_count,0) AS invoice_line_count,
                   COALESCE(i.invoice_document_count,0) AS invoice_document_count,
                   i.invoice_purposes,
                   COALESCE(u.invoice_line_count,0) AS unverified_invoice_line_count,
                   COALESCE(u.invoice_document_count,0) AS unverified_invoice_document_count,
                   u.invoice_purposes AS unverified_invoice_purposes,
                   COALESCE(ic.invoice_document_count,0)>0 AS has_invoice_evidence,
                   COALESCE(i.invoice_line_count,0)>0 AS invoice_purchase_observed,
                   COALESCE(u.invoice_line_count,0)>0
                     AND COALESCE(i.invoice_line_count,0)=0 AS unverified_invoice_observed
            FROM deal_facts df
            CROSS JOIN vas_categories c
            LEFT JOIN proposal_by_journey p
              ON p.journey_id=df.journey_id AND p.category=c.category
            LEFT JOIN invoice_purchase_by_journey i
              ON i.journey_id=df.journey_id AND i.category=c.category
            LEFT JOIN unverified_invoice_purchase_by_journey u
              ON u.journey_id=df.journey_id AND u.category=c.category
            LEFT JOIN invoice_coverage_by_journey ic
              ON ic.journey_id=df.journey_id
            WHERE df.has_booking
        )
        """
        reconciliation_by_category = _rows(
            connection,
            latest,
            tenant_id,
            vas_reconciliation_cte
            + """
            SELECT c.category,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.proposal_record_count>0
                   )::int
                       AS booked_proposal_journeys,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.proposal_record_count>0 AND r.actual_delivered_at IS NOT NULL
                   )::int AS delivered_proposal_journeys,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.proposal_record_count>0 AND NOT r.has_delivery_record
                   )::int AS invoice_awaiting_journeys,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.invoice_purchase_observed
                   )::int
                       AS delivery_invoice_purchase_journeys,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.proposal_record_count>0 AND r.invoice_purchase_observed
                   )::int AS proposed_and_invoiced_journeys,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.unverified_invoice_observed
                   )::int AS unverified_invoice_evidence_journeys,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.proposal_record_count>0 AND r.unverified_invoice_observed
                   )::int AS proposal_with_unverified_invoice_evidence_journeys,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.proposal_record_count>0 AND r.has_invoice_evidence
                         AND NOT r.invoice_purchase_observed
                         AND NOT r.unverified_invoice_observed
                   )::int AS proposal_not_observed_on_invoice_journeys,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.proposal_record_count>0 AND r.actual_delivered_at IS NOT NULL
                         AND NOT r.has_invoice_evidence
                         AND NOT r.unverified_invoice_observed
                   )::int AS proposal_without_invoice_evidence_journeys,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.invoice_purchase_observed AND r.proposal_record_count=0
                   )::int AS invoice_only_journeys,
                   count(DISTINCT r.journey_id) FILTER (
                       WHERE r.has_invoice_evidence
                   )::int
                       AS invoice_covered_delivered_journeys,
                   round(
                       100.0 * count(DISTINCT r.journey_id) FILTER (
                           WHERE r.proposal_record_count>0 AND r.invoice_purchase_observed
                       ) / NULLIF(count(DISTINCT r.journey_id) FILTER (
                           WHERE r.proposal_record_count>0 AND r.has_invoice_evidence
                             AND NOT r.unverified_invoice_observed
                       ),0),
                       1
                   ) AS proposal_invoice_match_pct
            FROM vas_categories c
            LEFT JOIN booked_journey_categories r ON r.category=c.category
            GROUP BY c.category
            ORDER BY CASE c.category
                WHEN 'EW' THEN 1 WHEN 'INSURANCE' THEN 2 WHEN 'RSA' THEN 3
                WHEN 'FASTAG' THEN 4 ELSE 5 END
            """,
        )
        reconciliation_by_deal = _rows(
            connection,
            latest,
            tenant_id,
            vas_reconciliation_cte
            + """
            SELECT dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   model_name,
                   variant_name,
                   category,
                   count(*) FILTER (WHERE proposal_record_count>0)::int
                       AS booked_proposal_journeys,
                   count(*) FILTER (
                       WHERE proposal_record_count>0 AND actual_delivered_at IS NOT NULL
                   )::int AS delivered_proposal_journeys,
                   count(*) FILTER (
                       WHERE proposal_record_count>0 AND NOT has_delivery_record
                   )::int AS invoice_awaiting_journeys,
                   count(*) FILTER (WHERE invoice_purchase_observed)::int
                       AS delivery_invoice_purchase_journeys,
                   count(*) FILTER (
                       WHERE proposal_record_count>0 AND invoice_purchase_observed
                   )::int AS proposed_and_invoiced_journeys,
                   count(*) FILTER (
                       WHERE unverified_invoice_observed
                   )::int AS unverified_invoice_evidence_journeys,
                   count(*) FILTER (
                       WHERE proposal_record_count>0 AND unverified_invoice_observed
                   )::int AS proposal_with_unverified_invoice_evidence_journeys,
                   count(*) FILTER (
                       WHERE proposal_record_count>0 AND has_invoice_evidence
                         AND NOT invoice_purchase_observed
                         AND NOT unverified_invoice_observed
                   )::int AS proposal_not_observed_on_invoice_journeys,
                   count(*) FILTER (
                       WHERE proposal_record_count>0 AND actual_delivered_at IS NOT NULL
                         AND NOT has_invoice_evidence
                         AND NOT unverified_invoice_observed
                   )::int AS proposal_without_invoice_evidence_journeys,
                   count(*) FILTER (
                       WHERE invoice_purchase_observed AND proposal_record_count=0
                   )::int AS invoice_only_journeys,
                   string_agg(DISTINCT invoice_purposes, '; '
                       ORDER BY invoice_purposes) FILTER (
                       WHERE invoice_purchase_observed AND invoice_purposes IS NOT NULL
                   ) AS invoice_purposes,
                   string_agg(DISTINCT unverified_invoice_purposes, '; '
                       ORDER BY unverified_invoice_purposes) FILTER (
                       WHERE unverified_invoice_observed
                         AND unverified_invoice_purposes IS NOT NULL
                   ) AS unverified_invoice_purposes
            FROM booked_journey_categories
            GROUP BY dealer_id, dealer_name, outlet_id, outlet_name,
                     model_name, variant_name, category
            HAVING count(*) FILTER (WHERE proposal_record_count>0)>0
                OR count(*) FILTER (WHERE invoice_purchase_observed)>0
                OR count(*) FILTER (WHERE unverified_invoice_observed)>0
            ORDER BY dealer_name, outlet_name, model_name, variant_name, category
            """,
        )
        reconciliation_coverage = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                vas_reconciliation_cte
                + """
                SELECT count(DISTINCT journey_id) FILTER (
                           WHERE actual_delivered_at IS NOT NULL
                       )::int AS delivered_journeys,
                       count(DISTINCT journey_id) FILTER (
                           WHERE actual_delivered_at IS NOT NULL AND has_invoice_evidence
                       )::int AS delivered_journeys_with_tax_invoice_lines,
                       (SELECT count(DISTINCT invoice_document_id)::int
                        FROM delivery_invoice_lines) AS delivery_tax_invoice_documents,
                       (SELECT count(*)::int FROM delivery_invoice_lines)
                           AS delivery_tax_invoice_lines,
                       (SELECT count(*)::int FROM delivery_invoice_lines
                        WHERE category IS NULL) AS unclassified_tax_invoice_lines,
                       count(DISTINCT journey_id) FILTER (
                           WHERE actual_delivered_at IS NOT NULL
                             AND unverified_invoice_observed
                       )::int AS delivered_journeys_with_unverified_vas_evidence,
                       (SELECT count(DISTINCT invoice_document_id)::int
                        FROM unverified_delivery_invoice_lines)
                           AS unverified_invoice_documents,
                       (SELECT count(*)::int FROM unverified_delivery_invoice_lines)
                           AS unverified_invoice_lines,
                       (SELECT count(*)::int FROM unverified_delivery_invoice_lines
                        WHERE category IS NOT NULL) AS classified_unverified_vas_lines,
                       (SELECT count(*)::int FROM unverified_delivery_invoice_lines
                        WHERE category IS NULL) AS unclassified_unverified_invoice_lines,
                       round(100.0 * count(DISTINCT journey_id) FILTER (
                           WHERE actual_delivered_at IS NOT NULL AND has_invoice_evidence
                       ) / NULLIF(count(DISTINCT journey_id) FILTER (
                           WHERE actual_delivered_at IS NOT NULL
                       ),0),1) AS invoice_coverage_pct
                FROM booked_journey_categories
                """,
            )
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "journeys": total_journeys,
        "by_type": by_type,
        "by_outlet": by_outlet,
        "by_product_type": by_product_type,
        "warranty_rsa": {
            **warranty_rsa_summary,
            "by_product": warranty_rsa_by_product,
        },
        "accessory_cost_by_product": accessory_cost_by_product,
        "accessory_items": accessory_items,
        "accessory_line_coverage": accessory_line_coverage,
        "by_model_outlet": by_model_outlet,
        "booking_invoice_reconciliation": {
            "by_category": reconciliation_by_category,
            "by_dealer_model_variant": reconciliation_by_deal,
            "invoice_coverage": reconciliation_coverage,
            "definition": (
            "A booking proposal is a captured journey_addons category record linked to a booked "
                "journey; Audit Core currently does not provide a separate proposal/acceptance state. "
                "Invoice purchases are counted only when category evidence is present on a confirmed "
                "TAX_INVOICE linked to the journey. Invoice-awaiting counts include only booking proposals "
                "for journeys with no delivery record uploaded. A journey with a delivery record but no "
                "matching confirmed invoice line is excluded from these three states; missing or unclassified "
                "evidence is not interpreted as a purchase or a non-purchase."
            ),
        },
        "accessory_codes": ["ACCESSORY", "ACCESSORIES", "ACCESSORIES_TOTAL"],
        "definition": (
            "Add-on records show the captured VAS category by dealer, outlet, model, and variant. "
            "Accessory invoice items include only reviewed ACCESSORY-purpose TAX_INVOICE lines and "
            "are grouped by dealership, outlet, model, variant, category and item code. Proforma and "
            "unclassified lines are shown only in coverage and are not counted as sold. Missing amount "
            "or quantity fields remain unavailable. Repeated invoice identities on the same journey "
            "are counted once, using the most complete and recently reviewed source copy. Invoice-line "
            "net sales, VAS category totals and "
            "commercial-line pricing evidence are separate measures and must not be added together. "
            "The commercial-line comparison uses only rows with both standard and actual amounts. "
            "Catalogue-matched vehicle labels use the canonical Audit Core model and variant names."
        ),
    }


@router.get("/compliance")
def compliance_intelligence(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    connection, latest = _latest_context(tenant_id)
    try:
        # Single CTE scan covering outlet breakdown, model breakdown, and top rules
        combined = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            , outlet_agg AS (
                SELECT 'OUTLET' AS _scope,
                       dealer_name, outlet_name,
                       NULL::text AS model_name,
                       NULL::text AS rule_key, NULL::text AS severity,
                       NULL::text AS finding_status,
                       count(*)::int AS journeys,
                       count(*) FILTER (WHERE finding_count > 0)::int AS journeys_with_findings,
                       COALESCE(sum(finding_count),0)::int AS finding_count,
                       COALESCE(sum(open_finding_count),0)::int AS open_finding_count,
                       COALESCE(sum(high_finding_count),0)::int AS high_finding_count,
                       count(*) FILTER (WHERE missing_document_flag_count > 0)::int
                           AS journeys_with_missing_documents,
                       COALESCE(sum(actual_discount_amount) FILTER (WHERE finding_count > 0),0)
                           AS discount_value_on_affected_journeys,
                       COALESCE(sum(payment_amount) FILTER (WHERE finding_count > 0),0)
                           AS payment_value_on_affected_journeys,
                       NULL::int AS journey_count
                FROM deal_facts
                GROUP BY dealer_name, outlet_name
            ),
            model_agg AS (
                SELECT 'MODEL' AS _scope,
                       NULL AS dealer_name, NULL AS outlet_name,
                       COALESCE(model_name,'Unresolved') AS model_name,
                       NULL, NULL, NULL,
                       count(*)::int, count(*) FILTER (WHERE finding_count > 0)::int,
                       COALESCE(sum(finding_count),0)::int,
                       NULL::int, COALESCE(sum(high_finding_count),0)::int,
                       NULL::int, NULL::numeric, NULL::numeric, NULL::int
                FROM deal_facts
                GROUP BY model_name
            )
            SELECT * FROM outlet_agg
            UNION ALL
            SELECT * FROM model_agg
            UNION ALL
            SELECT 'RULE' AS _scope,
                   NULL, NULL, NULL,
                   COALESCE(row_data->>'rule_key','UNSPECIFIED'),
                   COALESCE(row_data->>'severity','UNSPECIFIED'),
                   COALESCE(row_data->>'finding_status','UNSPECIFIED'),
                   NULL, NULL,
                   count(*)::int,
                   NULL, NULL, NULL, NULL, NULL,
                   count(DISTINCT row_data->>'journey_id')::int
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='audit_findings'
            GROUP BY rule_key, severity, finding_status
            ORDER BY _scope, finding_count DESC NULLS LAST
            """,
        )
    finally:
        connection.close()

    by_outlet = []
    for r in combined:
        if r["_scope"] != "OUTLET":
            continue
        total = int(r.get("journeys") or 0)
        by_outlet.append({
            "dealer_name": r["dealer_name"],
            "outlet_name": r["outlet_name"],
            "journeys": total,
            "journeys_with_findings": int(r.get("journeys_with_findings") or 0),
            "finding_count": int(r.get("finding_count") or 0),
            "open_finding_count": int(r.get("open_finding_count") or 0),
            "high_finding_count": int(r.get("high_finding_count") or 0),
            "journeys_with_missing_documents": int(r.get("journeys_with_missing_documents") or 0),
            "discount_value_on_affected_journeys": r.get("discount_value_on_affected_journeys"),
            "payment_value_on_affected_journeys": r.get("payment_value_on_affected_journeys"),
            "journeys_with_findings_pct": _rate(int(r.get("journeys_with_findings") or 0), total),
            "journeys_with_missing_documents_pct": _rate(int(r.get("journeys_with_missing_documents") or 0), total),
        })

    by_model = []
    for r in combined:
        if r["_scope"] != "MODEL":
            continue
        journeys = int(r.get("journeys") or 0)
        by_model.append({
            "model_name": r["model_name"],
            "journeys": journeys,
            "journeys_with_findings": int(r.get("journeys_with_findings") or 0),
            "finding_count": int(r.get("finding_count") or 0),
            "high_finding_count": int(r.get("high_finding_count") or 0),
            "journeys_with_findings_pct": _rate(int(r.get("journeys_with_findings") or 0), journeys),
        })

    top_rules = [
        {
            "rule_key": r["rule_key"],
            "severity": r["severity"],
            "finding_status": r["finding_status"],
            "finding_count": int(r.get("finding_count") or 0),
            "journey_count": int(r.get("journey_count") or 0),
        }
        for r in combined if r["_scope"] == "RULE"
    ][:50]

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
                SELECT count(*) FILTER (WHERE has_booking)::int AS booked_journeys,
                       count(*) FILTER (
                           WHERE has_booking AND customer_pincode IS NOT NULL
                       )::int AS journeys_with_pincode,
                       count(*) FILTER (
                           WHERE has_booking AND customer_pincode IS NOT NULL
                             AND geography_reviewed_at_utc IS NOT NULL
                       )::int AS reviewed_journeys
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
                   pin.district_name,
                   pin.locality_names,
                   dealer_id,
                   dealer_name,
                   outlet_id,
                   outlet_name,
                   COALESCE(model_name,'Unresolved') AS model_name,
                   booking_date::text AS booking_date,
                   actual_delivered_at::date::text AS delivery_date,
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
            LEFT JOIN analytics.geo_pin_master pin ON pin.pincode=deal_facts.customer_pincode
            WHERE customer_pincode IS NOT NULL AND has_booking
            GROUP BY customer_pincode, pin.district_name, pin.locality_names,
                     dealer_id, dealer_name, outlet_id, outlet_name, model_name,
                     booking_date, actual_delivered_at::date
            ORDER BY journey_count DESC, customer_pincode, dealer_name, outlet_name, model_name,
                     booking_date, delivery_date
            """,
        )
        outlet_locations = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT outlet_id, dealer_id, dealer_name, outlet_name, outlet_code,
                   pincode, latitude, longitude, location_precision
            FROM analytics.outlet_location_master
            WHERE tenant_id=:tenant_id
            ORDER BY dealer_name, outlet_name
            """,
        )
        map_boundaries = [dict(row) for row in connection.execute(text("""
            SELECT boundary_key, boundary_type, encode(content_gzip,'base64') AS content_gzip_base64,
                   source_name, source_url
            FROM analytics.geo_boundary_master
            WHERE state_name='Odisha'
            ORDER BY boundary_type
        """)).mappings().all()]
    finally:
        connection.close()

    journeys = int(total.get("booked_journeys") or 0)
    with_pin = int(total.get("journeys_with_pincode") or 0)
    reviewed = int(total.get("reviewed_journeys") or 0)
    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "coverage": _coverage_metric(with_pin, journeys),
        "quality": {
            "reviewed_journeys": reviewed,
            "provisional_journeys": max(with_pin - reviewed, 0),
            "reviewed_pct_of_pincode_records": _rate(reviewed, with_pin),
            "source_field": "address_pincode",
            "source_note": "Document-extracted values remain provisional until reviewed in Audit Core.",
        },
        "available": with_pin > 0,
        "rows": rows,
        "outlet_locations": outlet_locations,
        "map_boundaries": map_boundaries,
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


@router.get("/delivery-monthly")
def delivery_monthly(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Deliveries completed per calendar month with TAT distribution per month."""
    connection, latest = _latest_context(tenant_id)
    try:
        by_month = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT to_char(date_trunc('month', actual_delivered_at), 'YYYY-MM') AS delivery_month,
                   count(*)::int AS deliveries_completed,
                   round(avg(actual_delivered_at::date - booking_date),2) AS avg_tat_days,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY (actual_delivered_at::date - booking_date)
                   ) AS median_tat_days,
                   min(actual_delivered_at::date - booking_date)::int AS min_tat_days,
                   max(actual_delivered_at::date - booking_date)::int AS max_tat_days,
                   percentile_cont(0.9) WITHIN GROUP (
                       ORDER BY (actual_delivered_at::date - booking_date)
                   ) AS p90_tat_days
            FROM deal_facts
            WHERE actual_delivered_at IS NOT NULL
              AND booking_date IS NOT NULL
              AND actual_delivered_at::date >= booking_date
            GROUP BY 1
            ORDER BY 1
            """,
        )
        by_model = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(model_name, 'Unresolved') AS model_name,
                   count(*)::int AS deliveries_completed,
                   round(avg(actual_delivered_at::date - booking_date),2) AS avg_tat_days,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY (actual_delivered_at::date - booking_date)
                   ) AS median_tat_days,
                   min(actual_delivered_at::date - booking_date)::int AS min_tat_days,
                   max(actual_delivered_at::date - booking_date)::int AS max_tat_days,
                   percentile_cont(0.9) WITHIN GROUP (
                       ORDER BY (actual_delivered_at::date - booking_date)
                   ) AS p90_tat_days
            FROM deal_facts
            WHERE actual_delivered_at IS NOT NULL
              AND booking_date IS NOT NULL
              AND actual_delivered_at::date >= booking_date
            GROUP BY 1
            ORDER BY deliveries_completed DESC, model_name
            """,
        )
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_name, outlet_name,
                   count(*)::int AS deliveries_completed,
                   round(avg(actual_delivered_at::date - booking_date),2) AS avg_tat_days,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY (actual_delivered_at::date - booking_date)
                   ) AS median_tat_days,
                   min(actual_delivered_at::date - booking_date)::int AS min_tat_days,
                   max(actual_delivered_at::date - booking_date)::int AS max_tat_days,
                   percentile_cont(0.75) WITHIN GROUP (
                       ORDER BY (actual_delivered_at::date - booking_date)
                   ) AS p75_tat_days,
                   percentile_cont(0.9) WITHIN GROUP (
                       ORDER BY (actual_delivered_at::date - booking_date)
                   ) AS p90_tat_days,
                   count(*) FILTER (
                       WHERE planned_delivery_at IS NOT NULL
                         AND actual_delivered_at <= planned_delivery_at
                   )::int AS on_time_deliveries,
                   count(*) FILTER (
                       WHERE planned_delivery_at IS NOT NULL
                   )::int AS planned_count
            FROM deal_facts
            WHERE actual_delivered_at IS NOT NULL
              AND booking_date IS NOT NULL
              AND actual_delivered_at::date >= booking_date
            GROUP BY dealer_name, outlet_name
            ORDER BY deliveries_completed DESC, dealer_name, outlet_name
            """,
        )
        for row in by_outlet:
            row["on_time_pct"] = _rate(
                row.get("on_time_deliveries") or 0,
                row.get("planned_count") or 0,
            )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "by_month": by_month,
        "by_model": by_model,
        "by_outlet": by_outlet,
        "tat_definition": "Booking date to actual delivery date in calendar days.",
    }


@router.get("/discount-waterfall")
def discount_waterfall(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Discount breakdown by type across models and outlets.

    Each discount key is shown as standard-eligible vs actual-given,
    enabling a per-type standard-vs-actual waterfall.
    """
    connection, latest = _latest_context(tenant_id)
    try:
        # per-key totals across all journeys
        by_key = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT COALESCE(row_data->>'discount_key','UNSPECIFIED') AS discount_key,
                   count(DISTINCT row_data->>'journey_id')::int AS journey_count,
                   COALESCE(sum(NULLIF(row_data->>'standard_eligible_amount','')::numeric),0)
                       AS standard_eligible,
                   COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric),0)
                       AS actual_given,
                   COALESCE(sum(
                       GREATEST(
                           NULLIF(row_data->>'actual_discount_amount','')::numeric
                           - NULLIF(row_data->>'standard_eligible_amount','')::numeric,
                           0
                       )
                   ) FILTER (
                       WHERE NULLIF(row_data->>'actual_discount_amount','') IS NOT NULL
                         AND NULLIF(row_data->>'standard_eligible_amount','') IS NOT NULL
                   ),0) AS above_eligible,
                   count(*) FILTER (
                       WHERE NULLIF(row_data->>'actual_discount_amount','')::numeric IS NOT NULL
                         AND NULLIF(row_data->>'standard_eligible_amount','')::numeric IS NOT NULL
                         AND NULLIF(row_data->>'actual_discount_amount','')::numeric
                             > NULLIF(row_data->>'standard_eligible_amount','')::numeric
                   )::int AS excess_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id
              AND source_table='discount_applications'
            GROUP BY 1
            ORDER BY actual_given DESC, discount_key
            """,
        )
        # waterfall per model
        by_model = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(model_name,'Unresolved') AS model_name,
                   count(*)::int AS journeys,
                   COALESCE(sum(dk_cash_discount),0) AS cash_discount,
                   COALESCE(sum(dk_exchange_bonus),0) AS exchange_bonus,
                   COALESCE(sum(dk_scrappage),0) AS scrappage,
                   COALESCE(sum(dk_corporate),0) AS corporate,
                   COALESCE(sum(dk_loyalty),0) AS loyalty,
                   COALESCE(sum(dk_insurance),0) AS insurance_discount,
                   COALESCE(sum(dk_mr),0) AS management_referral,
                   COALESCE(sum(dk_scheme_benefit),0) AS scheme_benefit,
                   COALESCE(sum(dk_other),0) AS other,
                   COALESCE(sum(actual_discount_amount),0) AS total_actual_discount,
                   COALESCE(sum(eligible_discount_amount),0) AS total_eligible_discount,
                   count(*) FILTER (WHERE has_mr_discount)::int AS mr_journeys,
                   count(*) FILTER (WHERE has_excess_on_any_key)::int AS excess_journeys
            FROM deal_facts
            GROUP BY 1
            ORDER BY total_actual_discount DESC, model_name
            """,
        )
        # waterfall per outlet
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_name, outlet_name,
                   count(*)::int AS journeys,
                   COALESCE(sum(dk_cash_discount),0) AS cash_discount,
                   COALESCE(sum(dk_exchange_bonus),0) AS exchange_bonus,
                   COALESCE(sum(dk_scrappage),0) AS scrappage,
                   COALESCE(sum(dk_corporate),0) AS corporate,
                   COALESCE(sum(dk_loyalty),0) AS loyalty,
                   COALESCE(sum(dk_insurance),0) AS insurance_discount,
                   COALESCE(sum(dk_mr),0) AS management_referral,
                   COALESCE(sum(dk_scheme_benefit),0) AS scheme_benefit,
                   COALESCE(sum(dk_other),0) AS other,
                   COALESCE(sum(actual_discount_amount),0) AS total_actual_discount,
                   COALESCE(sum(eligible_discount_amount),0) AS total_eligible_discount,
                   count(*) FILTER (WHERE has_mr_discount)::int AS mr_journeys,
                   count(*) FILTER (WHERE has_excess_on_any_key)::int AS excess_journeys
            FROM deal_facts
            GROUP BY dealer_name, outlet_name
            ORDER BY total_actual_discount DESC, dealer_name, outlet_name
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "by_key": by_key,
        "by_model": by_model,
        "by_outlet": by_outlet,
        "note": (
            "management_referral is TL-authorised; it has no OEM-standard ceiling. "
            "above_eligible is computed only where both actual and eligible are populated."
        ),
    }


@router.get("/management-referral")
def management_referral(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Management Referral (MR) discount summary — TL-granted exception discounts."""
    connection, latest = _latest_context(tenant_id)
    try:
        summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                """
                SELECT count(*)::int AS total_journeys_with_mr_record,
                       count(*) FILTER (WHERE (NULLIF(row_data->>'opted','')::boolean) IS TRUE)::int
                           AS opted_count,
                       COALESCE(sum(NULLIF(row_data->>'amount','')::numeric) FILTER (
                           WHERE (NULLIF(row_data->>'opted','')::boolean) IS TRUE
                       ),0) AS total_mr_amount,
                       round(avg(NULLIF(row_data->>'amount','')::numeric) FILTER (
                           WHERE (NULLIF(row_data->>'opted','')::boolean) IS TRUE
                       ),2) AS avg_mr_amount
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id
                  AND source_table='p2_management_referrals'
                """,
            )
        )
        by_role = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT COALESCE(NULLIF(row_data->>'set_by_role',''),'UNSPECIFIED') AS set_by_role,
                   count(*) FILTER (WHERE (NULLIF(row_data->>'opted','')::boolean) IS TRUE)::int
                       AS opted_count,
                   COALESCE(sum(NULLIF(row_data->>'amount','')::numeric) FILTER (
                       WHERE (NULLIF(row_data->>'opted','')::boolean) IS TRUE
                   ),0) AS total_amount,
                   round(avg(NULLIF(row_data->>'amount','')::numeric) FILTER (
                       WHERE (NULLIF(row_data->>'opted','')::boolean) IS TRUE
                   ),2) AS avg_amount
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id
              AND source_table='p2_management_referrals'
            GROUP BY 1
            ORDER BY total_amount DESC, set_by_role
            """,
        )
        by_model = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(model_name,'Unresolved') AS model_name,
                   count(*) FILTER (WHERE has_mr_discount)::int AS mr_opted_journeys,
                   COALESCE(sum(dk_mr) FILTER (WHERE has_mr_discount),0) AS total_mr_amount,
                   round(avg(dk_mr) FILTER (WHERE has_mr_discount),2) AS avg_mr_amount,
                   count(*)::int AS total_journeys
            FROM deal_facts
            GROUP BY 1
            HAVING count(*) FILTER (WHERE has_mr_discount) > 0
            ORDER BY total_mr_amount DESC, model_name
            """,
        )
        for row in by_model:
            row["mr_penetration_pct"] = _rate(
                row.get("mr_opted_journeys") or 0,
                row.get("total_journeys") or 0,
            )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "by_role": by_role,
        "by_model": by_model,
        "note": (
            "Management Referral discount is TL-authorised with no OEM standard ceiling. "
            "opted=false records are journeys where the MR task was raised but not opted in."
        ),
    }


@router.get("/compliance-families")
def compliance_families(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Audit findings grouped into rule families for risk-category dashboards."""
    connection, latest = _latest_context(tenant_id)
    try:
        by_family = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT
                CASE
                    WHEN row_data->>'rule_key' IN ('EXCESS_DISCOUNT','DEAL_UNDERCHARGED')
                        THEN 'DISCOUNT_COMPLIANCE'
                    WHEN row_data->>'rule_key' IN ('TCS_SHORT','CASH_ABOVE_LIMIT')
                        THEN 'STATUTORY'
                    WHEN row_data->>'rule_key' IN (
                        'DELIVERED_ON_SHORT_PAYMENT',
                        'PAYMENT_AFTER_DELIVERY_WITHIN_GRACE',
                        'PAYMENT_AFTER_DELIVERY_BEYOND_GRACE',
                        'POST_DELIVERY_REFUND'
                    ) THEN 'SETTLEMENT'
                    WHEN row_data->>'rule_key' IN ('DO_PAYMENT_NOT_RECEIVED','DO_SHORT_PAYMENT')
                        THEN 'FINANCE'
                    WHEN row_data->>'rule_key' IN ('TRADE_IN_NOT_RESOLD','TRADE_IN_SOLD_AT_LOSS')
                        THEN 'TRADE_IN'
                    WHEN row_data->>'rule_key' IN (
                        'DELIVERY_NOT_COMPLETED_IN_TIME','NDC_NOT_SIGNED','NDC_SIGNATURE_UNCONFIRMED'
                    ) THEN 'PROCESS'
                    WHEN row_data->>'rule_key' IN (
                        'THIRD_PARTY_PAYMENT_UNDECLARED','THIRD_PARTY_PAYMENT_UNCONFIRMED',
                        'CASH_NOT_INTIMATED','CASH_INTIMATION_UNCONFIRMED'
                    ) THEN 'CASH_AML'
                    WHEN row_data->>'rule_key' IN (
                        'ACCESSORY_FITTED_UNBILLED','ACCESSORIES_FITTED_UNCONFIRMED'
                    ) THEN 'ACCESSORIES'
                    WHEN lower(COALESCE(row_data->>'rule_key','')) LIKE '%document%'
                        THEN 'DOCUMENT'
                    ELSE 'OTHER'
                END AS rule_family,
                COALESCE(row_data->>'finding_status','UNSPECIFIED') AS finding_status,
                count(*)::int AS finding_count,
                count(DISTINCT row_data->>'journey_id')::int AS journey_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='audit_findings'
            GROUP BY 1,2
            ORDER BY finding_count DESC, rule_family, finding_status
            """,
        )
        by_family_open = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT
                CASE
                    WHEN row_data->>'rule_key' IN ('EXCESS_DISCOUNT','DEAL_UNDERCHARGED')
                        THEN 'DISCOUNT_COMPLIANCE'
                    WHEN row_data->>'rule_key' IN ('TCS_SHORT','CASH_ABOVE_LIMIT')
                        THEN 'STATUTORY'
                    WHEN row_data->>'rule_key' IN (
                        'DELIVERED_ON_SHORT_PAYMENT',
                        'PAYMENT_AFTER_DELIVERY_WITHIN_GRACE',
                        'PAYMENT_AFTER_DELIVERY_BEYOND_GRACE',
                        'POST_DELIVERY_REFUND'
                    ) THEN 'SETTLEMENT'
                    WHEN row_data->>'rule_key' IN ('DO_PAYMENT_NOT_RECEIVED','DO_SHORT_PAYMENT')
                        THEN 'FINANCE'
                    WHEN row_data->>'rule_key' IN ('TRADE_IN_NOT_RESOLD','TRADE_IN_SOLD_AT_LOSS')
                        THEN 'TRADE_IN'
                    WHEN row_data->>'rule_key' IN (
                        'DELIVERY_NOT_COMPLETED_IN_TIME','NDC_NOT_SIGNED','NDC_SIGNATURE_UNCONFIRMED'
                    ) THEN 'PROCESS'
                    WHEN row_data->>'rule_key' IN (
                        'THIRD_PARTY_PAYMENT_UNDECLARED','THIRD_PARTY_PAYMENT_UNCONFIRMED',
                        'CASH_NOT_INTIMATED','CASH_INTIMATION_UNCONFIRMED'
                    ) THEN 'CASH_AML'
                    WHEN row_data->>'rule_key' IN (
                        'ACCESSORY_FITTED_UNBILLED','ACCESSORIES_FITTED_UNCONFIRMED'
                    ) THEN 'ACCESSORIES'
                    WHEN lower(COALESCE(row_data->>'rule_key','')) LIKE '%document%'
                        THEN 'DOCUMENT'
                    ELSE 'OTHER'
                END AS rule_family,
                count(*)::int AS open_finding_count,
                count(DISTINCT row_data->>'journey_id')::int AS journey_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='audit_findings'
              AND upper(COALESCE(row_data->>'finding_status','')) = 'OPEN'
            GROUP BY 1
            ORDER BY open_finding_count DESC, rule_family
            """,
        )
        statutory_detail = _rows(
            connection,
            latest,
            tenant_id,
            """
            SELECT COALESCE(row_data->>'rule_key','UNSPECIFIED') AS rule_key,
                   COALESCE(row_data->>'finding_status','UNSPECIFIED') AS finding_status,
                   COALESCE(row_data->>'severity','UNSPECIFIED') AS severity,
                   count(*)::int AS finding_count,
                   count(DISTINCT row_data->>'journey_id')::int AS journey_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='audit_findings'
              AND row_data->>'rule_key' IN ('TCS_SHORT','CASH_ABOVE_LIMIT')
            GROUP BY 1,2,3
            ORDER BY finding_count DESC, rule_key
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "by_family": by_family,
        "by_family_open": by_family_open,
        "statutory_detail": statutory_detail,
        "families": {
            "DISCOUNT_COMPLIANCE": "EXCESS_DISCOUNT, DEAL_UNDERCHARGED",
            "STATUTORY": "TCS_SHORT, CASH_ABOVE_LIMIT",
            "SETTLEMENT": "DELIVERED_ON_SHORT_PAYMENT, PAYMENT_AFTER_DELIVERY_*",
            "FINANCE": "DO_PAYMENT_NOT_RECEIVED, DO_SHORT_PAYMENT",
            "TRADE_IN": "TRADE_IN_NOT_RESOLD, TRADE_IN_SOLD_AT_LOSS",
            "PROCESS": "DELIVERY_NOT_COMPLETED_IN_TIME, NDC_NOT_SIGNED",
            "CASH_AML": "THIRD_PARTY_PAYMENT_*, CASH_INTIMATION_*",
            "ACCESSORIES": "ACCESSORY_FITTED_UNBILLED, ACCESSORIES_FITTED_UNCONFIRMED",
            "DOCUMENT": "All rule keys containing 'document'",
        },
    }


@router.get("/trade-in-health")
def trade_in_health(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Trade-in valuation accuracy and resale timeline health."""
    connection, latest = _latest_context(tenant_id)
    try:
        summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                _DEAL_FACTS_CTE
                + """
                SELECT count(*) FILTER (WHERE has_trade_in)::int AS trade_in_journeys,
                       count(*) FILTER (
                           WHERE has_trade_in AND trade_in_actual_value IS NOT NULL
                             AND trade_in_quoted_value IS NOT NULL
                       )::int AS valued_and_quoted,
                       count(*) FILTER (
                           WHERE has_trade_in
                             AND trade_in_actual_value IS NOT NULL
                             AND trade_in_quoted_value IS NOT NULL
                             AND trade_in_actual_value < trade_in_quoted_value
                       )::int AS sold_below_valuation,
                       COALESCE(sum(trade_in_quoted_value) FILTER (WHERE has_trade_in),0)
                           AS total_quoted_value,
                       COALESCE(sum(trade_in_actual_value) FILTER (WHERE has_trade_in),0)
                           AS total_actual_value,
                       round(avg(trade_in_quoted_value) FILTER (WHERE has_trade_in),2)
                           AS avg_quoted_value,
                       round(avg(trade_in_actual_value) FILTER (WHERE has_trade_in),2)
                           AS avg_actual_value,
                       count(*) FILTER (
                           WHERE has_trade_in AND trade_in_resale_at IS NOT NULL
                       )::int AS resold_count,
                       round(avg(
                           EXTRACT(EPOCH FROM (trade_in_resale_at - trade_in_handover_at)) / 86400.0
                       ) FILTER (
                           WHERE has_trade_in
                             AND trade_in_resale_at IS NOT NULL
                             AND trade_in_handover_at IS NOT NULL
                       ),2) AS avg_resale_days,
                       percentile_cont(0.9) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (trade_in_resale_at - trade_in_handover_at)) / 86400.0
                       ) FILTER (
                           WHERE has_trade_in
                             AND trade_in_resale_at IS NOT NULL
                             AND trade_in_handover_at IS NOT NULL
                       ) AS p90_resale_days
                FROM deal_facts
                """,
            )
        )
        valued_and_quoted = int(summary.get("valued_and_quoted") or 0)
        summary["sold_below_valuation_pct"] = _rate(
            summary.get("sold_below_valuation") or 0, valued_and_quoted
        )
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_name, outlet_name,
                   count(*) FILTER (WHERE has_trade_in)::int AS trade_in_journeys,
                   COALESCE(sum(trade_in_quoted_value) FILTER (WHERE has_trade_in),0)
                       AS total_quoted_value,
                   COALESCE(sum(trade_in_actual_value) FILTER (WHERE has_trade_in),0)
                       AS total_actual_value,
                   count(*) FILTER (
                       WHERE has_trade_in
                         AND trade_in_actual_value IS NOT NULL
                         AND trade_in_quoted_value IS NOT NULL
                         AND trade_in_actual_value < trade_in_quoted_value
                   )::int AS sold_below_valuation,
                   round(avg(
                       EXTRACT(EPOCH FROM (trade_in_resale_at - trade_in_handover_at)) / 86400.0
                   ) FILTER (
                       WHERE has_trade_in
                         AND trade_in_resale_at IS NOT NULL
                         AND trade_in_handover_at IS NOT NULL
                   ),2) AS avg_resale_days
            FROM deal_facts
            WHERE has_trade_in
            GROUP BY dealer_name, outlet_name
            ORDER BY trade_in_journeys DESC, dealer_name, outlet_name
            """,
        )
        pending_resale = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT journey_id, dealer_name, outlet_name, model_name,
                   trade_in_handover_at,
                   (CURRENT_DATE - trade_in_handover_at::date)::int AS days_since_handover
            FROM deal_facts
            WHERE has_trade_in
              AND trade_in_handover_at IS NOT NULL
              AND trade_in_resale_at IS NULL
            ORDER BY days_since_handover DESC NULLS LAST
            LIMIT 50
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "by_outlet": by_outlet,
        "pending_resale": pending_resale,
        "note": (
            "sold_below_valuation: trade-in vehicles where the actual resale value was less than "
            "the quoted valuation. pending_resale: top 50 vehicles handed over but not yet resold."
        ),
    }


@router.get("/insurance-source")
def insurance_source_split(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Insurance split by source (INHOUSE vs SELF) with correct penetration metrics."""
    connection, latest = _latest_context(tenant_id)
    try:
        summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                _DEAL_FACTS_CTE
                + """
                SELECT count(*) FILTER (WHERE has_booking)::int AS journeys,
                       count(*) FILTER (WHERE has_booking AND has_insurance)::int AS insurance_journeys,
                       count(*) FILTER (WHERE has_booking AND has_inhouse_insurance)::int AS inhouse_journeys,
                       count(*) FILTER (
                           WHERE has_booking AND has_insurance AND has_self_insurance
                       )::int AS self_journeys,
                       count(*) FILTER (
                           WHERE has_booking AND has_insurance
                             AND (insurance_source IS NULL
                                  OR insurance_source NOT IN ('INHOUSE','SELF','EXTERNAL','OUTSIDE'))
                             AND self_insurance_flag IS NOT TRUE
                       )::int AS source_unknown_journeys,
                       COALESCE(sum(actual_premium_amount) FILTER (
                           WHERE has_booking AND has_inhouse_insurance
                       ),0) AS inhouse_premium_total,
                       round(avg(actual_premium_amount) FILTER (
                           WHERE has_booking AND has_inhouse_insurance AND actual_premium_amount IS NOT NULL
                       ),2) AS avg_inhouse_premium
                FROM deal_facts
                """,
            )
        )
        total = int(summary.get("journeys") or 0)
        summary["inhouse_penetration_pct"] = _rate(
            summary.get("inhouse_journeys") or 0, total
        )
        summary["self_penetration_pct"] = _rate(
            summary.get("self_journeys") or 0, total
        )
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT dealer_name, outlet_name,
                   count(*) FILTER (WHERE has_booking)::int AS journeys,
                   count(*) FILTER (WHERE has_booking AND has_inhouse_insurance)::int AS inhouse_journeys,
                   count(*) FILTER (
                       WHERE has_booking AND has_insurance AND has_self_insurance
                   )::int AS self_journeys,
                   COALESCE(sum(actual_premium_amount) FILTER (
                       WHERE has_booking AND has_inhouse_insurance
                   ),0) AS inhouse_premium_total
            FROM deal_facts
            GROUP BY dealer_name, outlet_name
            ORDER BY inhouse_journeys DESC, dealer_name, outlet_name
            """,
        )
        for row in by_outlet:
            journeys = int(row.get("journeys") or 0)
            row["inhouse_penetration_pct"] = _rate(
                row.get("inhouse_journeys") or 0, journeys
            )
            row["self_penetration_pct"] = _rate(
                row.get("self_journeys") or 0, journeys
            )
        by_insurer_inhouse = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            SELECT COALESCE(insurer_name,'Unspecified') AS insurer_name,
                   count(*) FILTER (WHERE has_booking AND has_inhouse_insurance)::int AS policy_count,
                   COALESCE(sum(actual_premium_amount) FILTER (
                       WHERE has_booking AND has_inhouse_insurance
                   ),0) AS premium_total,
                   round(avg(actual_premium_amount) FILTER (
                       WHERE has_booking AND has_inhouse_insurance AND actual_premium_amount IS NOT NULL
                   ),2) AS avg_premium
            FROM deal_facts
            WHERE has_booking AND has_inhouse_insurance
            GROUP BY 1
            ORDER BY policy_count DESC, insurer_name
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "by_outlet": by_outlet,
        "by_insurer_inhouse": by_insurer_inhouse,
        "note": (
            "inhouse_penetration_pct counts only INHOUSE-sourced insurance as dealer revenue. "
            "SELF-sourced insurance is counted only when an explicit source or self-insurance flag is present. "
            "Uncoded source records remain unknown and are excluded from both in-house and self categories."
        ),
    }


@router.get("/dealer-discount-grid")
def dealer_discount_grid(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Dealer discount grid policy per model — buffer, OD cap, booking protection, out-of-territory.

    Combines the grid master with actual discount_applications to show how much
    of the agreed buffer has been consumed per model.
    """
    connection, latest = _latest_context(tenant_id)
    try:
        # Grid rows: one row per model from the latest PUBLISHED grid version
        grid_rows = _rows(
            connection,
            latest,
            tenant_id,
            """
            WITH ranked_versions AS (
                SELECT row_data->>'grid_version_id' AS grid_version_id,
                       row_data->>'effective_from' AS effective_from,
                       row_data->>'lifecycle_status' AS lifecycle_status,
                       row_number() OVER (
                           PARTITION BY row_data->>'tenant_id'
                           ORDER BY (row_data->>'effective_from') DESC NULLS LAST
                       ) AS rn
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id
                  AND source_table='dealer_discount_grid_versions'
                  AND row_data->>'lifecycle_status' = 'PUBLISHED'
            ),
            latest_version AS (
                SELECT grid_version_id, effective_from
                FROM ranked_versions WHERE rn = 1
            )
            SELECT gr.row_data->>'model_alias' AS model_alias,
                   NULLIF(gr.row_data->>'booking_protection_days','')::int
                       AS booking_protection_days,
                   NULLIF(gr.row_data->>'agreed_buffer_amount','')::numeric
                       AS agreed_buffer_amount,
                   NULLIF(gr.row_data->>'insurance_od_percent','')::numeric
                       AS insurance_od_percent,
                   NULLIF(gr.row_data->>'out_of_territory_amount','')::numeric
                       AS out_of_territory_amount,
                   lv.effective_from AS grid_effective_from
            FROM analytics.snapshot_rows gr
            JOIN latest_version lv
              ON gr.row_data->>'grid_version_id' = lv.grid_version_id
            WHERE gr.tenant_id=:tenant_id AND gr.dump_id=:dump_id
              AND gr.source_table='dealer_discount_grid_rows'
              AND (gr.row_data->>'in_scope')::boolean IS NOT FALSE
            ORDER BY gr.row_data->>'model_alias'
            """,
        )

        # Buffer utilisation: actual MR + OTHER discount consumed vs agreed_buffer per model
        # Uses journey_products to link model_name to grid model_alias (best-effort text match)
        buffer_utilisation = _rows(
            connection,
            latest,
            tenant_id,
            _DEAL_FACTS_CTE
            + """
            , grid AS (
                SELECT gr.row_data->>'model_alias' AS model_alias,
                       NULLIF(gr.row_data->>'agreed_buffer_amount','')::numeric
                           AS agreed_buffer_amount,
                       NULLIF(gr.row_data->>'insurance_od_percent','')::numeric
                           AS insurance_od_percent_max
                FROM analytics.snapshot_rows gr
                WHERE gr.tenant_id=:tenant_id AND gr.dump_id=:dump_id
                  AND gr.source_table='dealer_discount_grid_rows'
                  AND (gr.row_data->>'in_scope')::boolean IS NOT FALSE
            )
            SELECT df.model_name,
                   g.model_alias,
                   g.agreed_buffer_amount,
                   g.insurance_od_percent_max,
                   count(*) FILTER (WHERE df.has_mr_discount OR df.dk_other > 0)::int
                       AS journeys_with_buffer_discount,
                   COALESCE(sum(df.dk_mr + df.dk_other),0) AS total_buffer_consumed,
                   round(avg(df.dk_mr + df.dk_other) FILTER (
                       WHERE df.has_mr_discount OR df.dk_other > 0
                   ),2) AS avg_buffer_per_journey,
                   round(
                       CASE WHEN g.agreed_buffer_amount > 0
                            THEN (COALESCE(sum(df.dk_mr + df.dk_other),0) /
                                  (count(*) * g.agreed_buffer_amount)) * 100.0
                            ELSE NULL END
                   ,1) AS avg_buffer_utilisation_pct
            FROM deal_facts df
            LEFT JOIN grid g
              ON lower(trim(g.model_alias)) = lower(trim(COALESCE(df.model_name,'')))
            WHERE df.model_name IS NOT NULL
            GROUP BY df.model_name, g.model_alias, g.agreed_buffer_amount,
                     g.insurance_od_percent_max
            ORDER BY total_buffer_consumed DESC, df.model_name
            """,
        )

        # Models in grid but with no journey data yet (new models configured but not booked)
        grid_model_aliases = {r["model_alias"] for r in grid_rows if r.get("model_alias")}
        utilisation_models = {r["model_name"] for r in buffer_utilisation if r.get("model_name")}
        unmatched_grid_models = sorted(grid_model_aliases - utilisation_models)

    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "grid_rows": grid_rows,
        "buffer_utilisation": buffer_utilisation,
        "unmatched_grid_models": unmatched_grid_models,
        "note": (
            "buffer_utilisation combines Management Referral (dk_mr) and Other Scheme (dk_other) "
            "as the two discount types that consume the agreed dealer buffer per model. "
            "Model matching is case-insensitive text match between grid model_alias and "
            "journey model_name — use the unmatched_grid_models list to identify alias mismatches."
        ),
    }


@router.get("/finance")
def finance_intelligence(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Finance attach and confidence-qualified lender disbursement reporting."""
    connection, latest = _latest_context(tenant_id)
    try:
        summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                _DEAL_FACTS_CTE
                + """
                , finance_rows AS (
                    SELECT row_data->>'journey_id' AS journey_id,
                           upper(COALESCE(NULLIF(row_data->>'loan_disbursement_confidence',''), 'UNVERIFIED')) AS confidence,
                           NULLIF(row_data->>'loan_disbursement_amount','')::numeric AS loan_amount
                    FROM analytics.snapshot_rows
                    WHERE tenant_id=:tenant_id AND dump_id=:dump_id
                      AND source_table='finance_records'
                )
                SELECT count(*) FILTER (WHERE has_booking)::int AS booked_journeys,
                       count(*) FILTER (WHERE has_booking AND has_finance)::int AS financed_journeys,
                       round(100.0 * count(*) FILTER (WHERE has_booking AND has_finance)
                             / NULLIF(count(*) FILTER (WHERE has_booking),0),1) AS finance_record_attach_pct,
                       (SELECT count(*)::int FROM finance_rows fr
                        JOIN deal_facts d USING (journey_id) WHERE d.has_booking) AS finance_record_count,
                       (SELECT count(*)::int FROM finance_rows fr
                        JOIN deal_facts d USING (journey_id)
                        WHERE d.has_booking AND fr.confidence='HIGH' AND fr.loan_amount IS NOT NULL)
                           AS high_confidence_loan_records,
                       (SELECT COALESCE(sum(fr.loan_amount),0) FROM finance_rows fr
                        JOIN deal_facts d USING (journey_id)
                        WHERE d.has_booking AND fr.confidence='HIGH')
                           AS high_confidence_loan_amount,
                       (SELECT count(*)::int FROM finance_rows fr
                        JOIN deal_facts d USING (journey_id)
                        WHERE d.has_booking AND fr.confidence='MEDIUM' AND fr.loan_amount IS NOT NULL)
                           AS medium_confidence_loan_records,
                       (SELECT COALESCE(sum(fr.loan_amount),0) FROM finance_rows fr
                        JOIN deal_facts d USING (journey_id)
                        WHERE d.has_booking AND fr.confidence='MEDIUM')
                           AS medium_confidence_loan_amount,
                       (SELECT count(*)::int FROM finance_rows fr
                        JOIN deal_facts d USING (journey_id)
                        WHERE d.has_booking AND (fr.loan_amount IS NULL OR fr.confidence NOT IN ('HIGH','MEDIUM')))
                           AS unverified_or_missing_loan_records
                FROM deal_facts
                """,
            )
        )
        summary["loan_amount_capture_pct"] = _rate(
            int(summary.get("high_confidence_loan_records") or 0)
            + int(summary.get("medium_confidence_loan_records") or 0),
            int(summary.get("finance_record_count") or 0),
        )

        finance_scope = _DEAL_FACTS_CTE + """
        , finance_rows AS (
            SELECT row_data->>'journey_id' AS journey_id,
                   upper(COALESCE(NULLIF(row_data->>'loan_disbursement_confidence',''), 'UNVERIFIED')) AS confidence,
                   NULLIF(row_data->>'loan_disbursement_amount','')::numeric AS loan_amount,
                   NULLIF(row_data->>'provider_name','') AS provider_name,
                   COALESCE(NULLIF(row_data->>'finance_type_code',''),'UNSPECIFIED') AS finance_type
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id
              AND source_table='finance_records'
        ), scoped_finance AS (
            SELECT d.dealer_id,
                   d.dealer_name,
                   d.outlet_id,
                   d.outlet_name,
                   f.journey_id,
                   f.confidence,
                   f.loan_amount,
                   f.finance_type,
                   CASE
                     WHEN f.confidence <> 'HIGH' THEN 'Lender not verified'
                     WHEN regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'HDFC%' THEN 'HDFC Bank'
                     WHEN regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'AXISBANK%' THEN 'Axis Bank'
                     WHEN regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'STATEBANK%' OR regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'SBI%' THEN 'State Bank of India'
                     WHEN regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'UCOBANK%' THEN 'UCO Bank'
                     WHEN regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'BANKOFBARODA%' OR regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'BOB%' THEN 'Bank of Baroda'
                     WHEN regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'PUNJABANDSIND%' THEN 'Punjab & Sind Bank'
                     WHEN regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'ICICIBANK%' THEN 'ICICI Bank'
                     WHEN regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'KOTAK%' THEN 'Kotak Mahindra Bank'
                     WHEN regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'MMFSL%' OR regexp_replace(upper(COALESCE(f.provider_name,'')), '[^A-Z0-9]', '', 'g') LIKE 'MAHINDRAANDMAHINDRAFINANCIAL%' THEN 'Mahindra Finance'
                     WHEN NULLIF(trim(f.provider_name),'') IS NOT NULL THEN trim(f.provider_name)
                     ELSE 'High-confidence lender not named'
                   END AS lender_name
            FROM finance_rows f
            JOIN deal_facts d ON d.journey_id=f.journey_id AND d.has_booking
        )
        """
        by_lender = _rows(
            connection,
            latest,
            tenant_id,
            finance_scope
            + """
            SELECT lender_name,
                   finance_type,
                   count(*)::int AS finance_record_count,
                   count(DISTINCT journey_id)::int AS financed_journeys,
                   COALESCE(sum(loan_amount),0) AS loan_amount
            FROM scoped_finance
            WHERE confidence='HIGH' AND loan_amount IS NOT NULL
            GROUP BY lender_name, finance_type
            ORDER BY loan_amount DESC, financed_journeys DESC, lender_name
            """,
        )
        by_lender_outlet = _rows(
            connection,
            latest,
            tenant_id,
            finance_scope
            + """
            SELECT dealer_name,
                   outlet_name,
                   lender_name,
                   confidence,
                   count(*)::int AS finance_record_count,
                   count(DISTINCT journey_id)::int AS financed_journeys,
                   COALESCE(sum(loan_amount),0) AS loan_amount
            FROM scoped_finance
            GROUP BY dealer_name, outlet_name, lender_name, confidence
            ORDER BY dealer_name, outlet_name, loan_amount DESC, lender_name
            """,
        )
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            finance_scope
            + """
            , booked AS (
                SELECT dealer_id, dealer_name, outlet_id, outlet_name,
                       count(*)::int AS booked_journeys
                FROM deal_facts
                WHERE has_booking
                GROUP BY dealer_id, dealer_name, outlet_id, outlet_name
            ), financed AS (
                SELECT dealer_id, dealer_name, outlet_id, outlet_name,
                       count(DISTINCT journey_id)::int AS financed_journeys,
                       count(*) FILTER (WHERE confidence='HIGH' AND loan_amount IS NOT NULL)::int AS high_confidence_records,
                       COALESCE(sum(loan_amount) FILTER (WHERE confidence='HIGH'),0) AS high_confidence_amount,
                       count(*) FILTER (WHERE confidence='MEDIUM' AND loan_amount IS NOT NULL)::int AS medium_confidence_records,
                       COALESCE(sum(loan_amount) FILTER (WHERE confidence='MEDIUM'),0) AS medium_confidence_amount
                FROM scoped_finance
                GROUP BY dealer_id, dealer_name, outlet_id, outlet_name
            )
            SELECT b.dealer_name,
                   b.outlet_name,
                   b.booked_journeys,
                   COALESCE(f.financed_journeys,0)::int AS financed_journeys,
                   round(100.0 * COALESCE(f.financed_journeys,0) / NULLIF(b.booked_journeys,0),1) AS finance_record_attach_pct,
                   COALESCE(f.high_confidence_records,0)::int AS high_confidence_loan_records,
                   COALESCE(f.high_confidence_amount,0) AS high_confidence_loan_amount,
                   COALESCE(f.medium_confidence_records,0)::int AS medium_confidence_loan_records,
                   COALESCE(f.medium_confidence_amount,0) AS medium_confidence_loan_amount
            FROM booked b
            LEFT JOIN financed f USING (dealer_id, dealer_name, outlet_id, outlet_name)
            ORDER BY high_confidence_loan_amount DESC, financed_journeys DESC,
                     dealer_name, outlet_name
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "by_lender": by_lender,
        "by_lender_outlet": by_lender_outlet,
        "by_outlet": by_outlet,
        "definition": (
            "Finance record attach is distinct booked journeys with a finance record divided by booked journeys. "
            "Lender attribution and confirmed loan totals use only HIGH-confidence loan disbursement matches. "
            "MEDIUM-confidence amounts are shown separately and not attributed to a lender. A loan-to-vehicle-price "
            "percentage is unavailable without complete, comparable net vehicle prices. The legacy financed_amount "
            "field is not used because its current values are not credible loan sizes."
        ),
    }


@router.get("/payments")
def payments_intelligence(
    tenant_id: str,
    _: Annotated[Principal, Depends(require_analytics_read)],
) -> dict:
    """Recorded receipt value by safe normalized channel and dealership."""
    connection, latest = _latest_context(tenant_id)
    payment_scope = _DEAL_FACTS_CTE + """
    , payment_rows AS (
        SELECT row_data->>'journey_id' AS journey_id,
               upper(COALESCE(NULLIF(row_data->>'payment_mode_code',''),'UNSPECIFIED')) AS mode_code,
               upper(COALESCE(NULLIF(row_data->>'payment_method_code',''),'')) AS method_code,
               NULLIF(row_data->>'amount','')::numeric AS amount,
               NULLIF(row_data->>'actual_status_code','') AS status_code
        FROM analytics.snapshot_rows
        WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='payments'
    ), scoped_payments AS (
        SELECT d.dealer_id,
               d.dealer_name,
               d.outlet_id,
               d.outlet_name,
               p.journey_id,
               p.amount,
               p.status_code,
               CASE
                 WHEN p.method_code ~ '^(UPI|PHONEPE|PHONE[[:space:]]PAY|GPAY|GOOGLE[[:space:]]PAY)([[:space:]/_-]|$)' THEN 'UPI'
                 WHEN p.method_code ~ '^RTGS([[:space:]/_-]|$)' THEN 'RTGS'
                 WHEN p.method_code ~ '^NEFT([[:space:]/_-]|$)' THEN 'NEFT'
                 WHEN p.method_code ~ '^IMPS([[:space:]/_-]|$)' THEN 'IMPS'
                 WHEN p.method_code ~ '^CASH([[:space:]/_-]|$)' THEN 'CASH'
                 WHEN p.method_code ~ '^(CHEQUE|CHQ)([[:space:]/_-]|$)' THEN 'CHEQUE'
                 WHEN p.method_code ~ '^(CARD|DEBIT|CREDIT|VISA|MASTERCARD|RUPAY)([[:space:]/_-]|$)' THEN 'CARD'
                 WHEN p.mode_code IN ('RTGS','NEFT','IMPS','CASH','CHEQUE','BANK_TRANSFER') THEN p.mode_code
                 ELSE 'OTHER / UNMAPPED'
               END AS channel
        FROM payment_rows p
        JOIN deal_facts d ON d.journey_id=p.journey_id AND d.has_booking
    )
    """
    try:
        summary = _scalar_row(
            _rows(
                connection,
                latest,
                tenant_id,
                payment_scope
                + """
                SELECT count(*)::int AS payment_entry_count,
                       count(DISTINCT journey_id)::int AS journeys_with_payment_entries,
                       COALESCE(sum(amount),0) AS recorded_receipt_amount,
                       count(*) FILTER (WHERE status_code IS NOT NULL)::int AS status_coded_entries,
                       round(100.0 * count(*) FILTER (WHERE status_code IS NOT NULL)
                             / NULLIF(count(*),0),1) AS status_coverage_pct,
                       count(DISTINCT channel)::int AS channel_count,
                       count(*) FILTER (WHERE channel='OTHER / UNMAPPED')::int AS unmapped_entries
                FROM scoped_payments
                """,
            )
        )
        by_channel = _rows(
            connection,
            latest,
            tenant_id,
            payment_scope
            + """
            SELECT channel,
                   count(*)::int AS payment_entry_count,
                   count(DISTINCT journey_id)::int AS journey_count,
                   COALESCE(sum(amount),0) AS recorded_receipt_amount,
                   count(*) FILTER (WHERE status_code IS NOT NULL)::int AS status_coded_entries
            FROM scoped_payments
            GROUP BY channel
            ORDER BY recorded_receipt_amount DESC, payment_entry_count DESC, channel
            """,
        )
        by_outlet = _rows(
            connection,
            latest,
            tenant_id,
            payment_scope
            + """
            SELECT dealer_name,
                   outlet_name,
                   channel,
                   count(*)::int AS payment_entry_count,
                   count(DISTINCT journey_id)::int AS journey_count,
                   COALESCE(sum(amount),0) AS recorded_receipt_amount,
                   count(*) FILTER (WHERE status_code IS NOT NULL)::int AS status_coded_entries
            FROM scoped_payments
            GROUP BY dealer_name, outlet_name, channel
            ORDER BY dealer_name, outlet_name, recorded_receipt_amount DESC, channel
            """,
        )
    finally:
        connection.close()

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "by_channel": by_channel,
        "by_outlet": by_outlet,
        "definition": (
            "Recorded receipt amounts are the amounts present in payment records. Missing payment status is not treated "
            "as settled; status_coverage_pct shows how many entries have a status code. Channel aliases are normalized "
            "only from anchored, recognized prefixes; raw payment references, remarks, and customer details are never "
            "returned. Booking/delivery stage is omitted because the source field is sparsely populated and not reliable "
            "enough for a stage split."
        ),
    }
