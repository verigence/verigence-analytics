from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Connection, text

from analytics.config import load_settings
from analytics.db import analytics_engine
from analytics.security import Principal, require_analytics_read

router = APIRouter(prefix="/v1/analytics/tenants/{tenant_id}", tags=["analytics"])


def _latest_dump(connection: Connection, tenant_id: str) -> dict:
    settings = load_settings(require_security=False)
    if tenant_id.strip() in settings.excluded_tenant_ids:
        raise HTTPException(status_code=404, detail="Analytics is disabled for this project")
    row = connection.execute(
        text(
            """
            SELECT dump_id, completed_at_utc, data_as_of_utc, row_count, table_counts
            FROM analytics.dump_runs
            WHERE tenant_id=:tenant_id AND status='COMPLETED'
            ORDER BY completed_at_utc DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="No completed analytics dump for tenant")
    return dict(row)


def _query_rows(connection: Connection, sql: str, *, tenant_id: str, dump_id: object) -> list[dict]:
    return [dict(row) for row in connection.execute(text(sql), {"tenant_id": tenant_id, "dump_id": dump_id}).mappings()]


@router.get("/sync-state")
def sync_state(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
    return {"tenant_id": tenant_id, "status": "CURRENT", **latest}


@router.get("/overview")
def overview(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        rows = _query_rows(connection, """
            SELECT source_table, count(*)::int AS row_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id
            GROUP BY source_table ORDER BY source_table
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "entities": rows}


@router.get("/findings")
def findings(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        rows = _query_rows(connection, """
            SELECT COALESCE(row_data->>'rule_key','UNSPECIFIED') AS rule_key,
                   COALESCE(row_data->>'finding_status','UNSPECIFIED') AS status,
                   COALESCE(row_data->>'severity','UNSPECIFIED') AS severity,
                   count(*)::int AS finding_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='audit_findings'
            GROUP BY 1,2,3 ORDER BY finding_count DESC, rule_key
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "rows": rows}


@router.get("/documents")
def documents(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        requirements = _query_rows(connection, """
            SELECT COALESCE(row_data->>'document_type_key','UNSPECIFIED') AS document_type,
                   COALESCE(row_data->>'requirement_status','UNSPECIFIED') AS status,
                   count(*)::int AS requirement_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_document_requirements'
            GROUP BY 1,2 ORDER BY requirement_count DESC, document_type
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
        missing_flags = _query_rows(connection, """
            SELECT COALESCE(row_data->>'rule_key','UNSPECIFIED') AS rule_key, count(*)::int AS flag_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='audit_findings'
              AND (lower(COALESCE(row_data->>'title','')) LIKE '%document%missing%'
                   OR lower(COALESCE(row_data->>'description','')) LIKE '%document%missing%'
                   OR lower(COALESCE(row_data->>'rule_key','')) LIKE '%document%missing%')
            GROUP BY 1 ORDER BY flag_count DESC, rule_key
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "requirements": requirements, "missing_document_flags": missing_flags}


@router.get("/payments")
def payments(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        rows = _query_rows(connection, """
            WITH payment_rows AS (
                SELECT upper(COALESCE(NULLIF(row_data->>'payment_mode_code',''),'UNSPECIFIED')) AS mode_code,
                       upper(COALESCE(NULLIF(row_data->>'payment_method_code',''),'')) AS method_code,
                       NULLIF(row_data->>'amount','')::numeric AS amount,
                       NULLIF(row_data->>'actual_status_code','') AS status_code
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='payments'
            ), normalized AS (
                SELECT CASE
                           WHEN method_code ~ '^(UPI|PHONEPE|PHONE[[:space:]]PAY|GPAY|GOOGLE[[:space:]]PAY)([[:space:]/_-]|$)' THEN 'UPI'
                           WHEN method_code ~ '^RTGS([[:space:]/_-]|$)' THEN 'RTGS'
                           WHEN method_code ~ '^NEFT([[:space:]/_-]|$)' THEN 'NEFT'
                           WHEN method_code ~ '^IMPS([[:space:]/_-]|$)' THEN 'IMPS'
                           WHEN method_code ~ '^CASH([[:space:]/_-]|$)' THEN 'CASH'
                           WHEN method_code ~ '^(CHEQUE|CHQ)([[:space:]/_-]|$)' THEN 'CHEQUE'
                           WHEN method_code ~ '^(CARD|DEBIT|CREDIT|VISA|MASTERCARD|RUPAY)([[:space:]/_-]|$)' THEN 'CARD'
                           WHEN mode_code IN ('RTGS','NEFT','IMPS','CASH','CHEQUE','BANK_TRANSFER') THEN mode_code
                           ELSE 'OTHER / UNMAPPED'
                       END AS payment_method,
                       amount, status_code
                FROM payment_rows
            )
            SELECT payment_method,
                   count(*)::int AS payment_count,
                   COALESCE(sum(amount),0) AS total_amount,
                   count(*) FILTER (WHERE status_code IS NOT NULL)::int AS status_coded_count
            FROM normalized
            GROUP BY payment_method
            ORDER BY total_amount DESC, payment_method
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
        summary = _query_rows(connection, """
            SELECT count(*)::int AS payment_entry_count,
                   count(DISTINCT row_data->>'journey_id')::int AS journeys_with_payment_entries,
                   COALESCE(sum(NULLIF(row_data->>'amount','')::numeric),0) AS recorded_receipt_amount,
                   count(*) FILTER (WHERE NULLIF(row_data->>'actual_status_code','') IS NOT NULL)::int AS status_coded_entries,
                   round(100.0 * count(*) FILTER (WHERE NULLIF(row_data->>'actual_status_code','') IS NOT NULL)
                         / NULLIF(count(*),0),1) AS status_coverage_pct
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='payments'
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary[0] if summary else {},
        "rows": rows,
        "definition": (
            "Amounts are recorded receipt values and are not automatically settled collections. Status coverage is "
            "reported separately. Payment stages are omitted because the source field is sparsely populated. Raw "
            "payment references and remarks are not returned."
        ),
    }


@router.get("/finance")
def finance(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        summary_rows = _query_rows(connection, """
            WITH booked AS (
                SELECT DISTINCT row_data->>'journey_id' AS journey_id
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
            ), finance_rows AS (
                SELECT f.row_data->>'journey_id' AS journey_id,
                       upper(COALESCE(NULLIF(f.row_data->>'loan_disbursement_confidence',''),'UNVERIFIED')) AS confidence,
                       NULLIF(f.row_data->>'loan_disbursement_amount','')::numeric AS loan_amount
                FROM analytics.snapshot_rows f
                JOIN booked b ON b.journey_id=f.row_data->>'journey_id'
                WHERE f.tenant_id=:tenant_id AND f.dump_id=:dump_id
                  AND f.source_table='finance_records'
            )
            SELECT (SELECT count(*)::int FROM booked) AS booked_journeys,
                   count(DISTINCT journey_id)::int AS financed_journeys,
                   count(*)::int AS finance_record_count,
                   count(*) FILTER (WHERE confidence='HIGH' AND loan_amount IS NOT NULL)::int AS high_confidence_loan_records,
                   COALESCE(sum(loan_amount) FILTER (WHERE confidence='HIGH'),0) AS high_confidence_loan_amount,
                   count(*) FILTER (WHERE confidence='MEDIUM' AND loan_amount IS NOT NULL)::int AS medium_confidence_loan_records,
                   COALESCE(sum(loan_amount) FILTER (WHERE confidence='MEDIUM'),0) AS medium_confidence_loan_amount,
                   count(*) FILTER (WHERE loan_amount IS NULL OR confidence NOT IN ('HIGH','MEDIUM'))::int AS unverified_or_missing_loan_records,
                   round(100.0 * count(DISTINCT journey_id)
                         / NULLIF((SELECT count(*) FROM booked),0),1) AS finance_attach_pct,
                   round(100.0 * count(*) FILTER (WHERE confidence IN ('HIGH','MEDIUM') AND loan_amount IS NOT NULL)
                         / NULLIF(count(*),0),1) AS loan_amount_capture_pct
            FROM finance_rows
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
        rows = _query_rows(connection, """
            SELECT COALESCE(NULLIF(f.row_data->>'finance_type_code',''),'UNSPECIFIED') AS finance_type,
                   COALESCE(NULLIF(f.row_data->>'provider_name',''),'High-confidence lender not named') AS provider,
                   'HIGH' AS confidence,
                   count(*)::int AS deal_count,
                   count(DISTINCT f.row_data->>'journey_id')::int AS journey_count,
                   COALESCE(sum(NULLIF(f.row_data->>'loan_disbursement_amount','')::numeric),0) AS loan_amount
            FROM analytics.snapshot_rows f
            WHERE f.tenant_id=:tenant_id AND f.dump_id=:dump_id AND f.source_table='finance_records'
              AND upper(COALESCE(NULLIF(f.row_data->>'loan_disbursement_confidence',''),'UNVERIFIED'))='HIGH'
              AND NULLIF(f.row_data->>'loan_disbursement_amount','') IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM analytics.snapshot_rows b
                  WHERE b.tenant_id=:tenant_id AND b.dump_id=:dump_id
                    AND b.source_table='bookings'
                    AND b.row_data->>'journey_id'=f.row_data->>'journey_id'
              )
            GROUP BY 1,2 ORDER BY loan_amount DESC, finance_type, provider
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary_rows[0] if summary_rows else {},
        "rows": rows,
        "definition": (
            "Finance attach is financed booked journeys divided by booked journeys; it is not an approval rate. "
            "Lender-attributed amounts use only HIGH-confidence loan disbursement records. MEDIUM-confidence amounts "
            "are reported separately. The legacy financed_amount field is not used."
        ),
    }


@router.get("/insurance")
def insurance(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        rows = _query_rows(connection, """
            WITH booked AS (
                SELECT DISTINCT row_data->>'journey_id' AS journey_id
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
            ), policy_rows AS (
                SELECT i.row_data->>'journey_id' AS journey_id,
                       COALESCE(NULLIF(i.row_data->>'insurance_by',''),'Unspecified') AS insurance_by,
                       COALESCE(NULLIF(trim(i.row_data->>'insurer_name'),''),'Unspecified insurer') AS raw_insurer,
                       regexp_replace(upper(COALESCE(i.row_data->>'insurer_name','')), '[^A-Z0-9]', '', 'g') AS insurer_key,
                       upper(NULLIF(i.row_data->>'insurance_source','')) AS source_code,
                       NULLIF(i.row_data->>'self_insurance_flag','')::boolean AS self_insurance_flag,
                       NULLIF(i.row_data->>'actual_premium_amount','')::numeric AS premium_amount
                FROM analytics.snapshot_rows i
                JOIN booked b ON b.journey_id=i.row_data->>'journey_id'
                WHERE i.tenant_id=:tenant_id AND i.dump_id=:dump_id
                  AND i.source_table='insurance_records'
            ), normalized_policy_rows AS (
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
                           ELSE raw_insurer
                       END AS insurer
                FROM policy_rows
            )
            SELECT insurance_by,
                   insurer,
                   CASE
                       WHEN source_code IN ('INHOUSE','SELF','EXTERNAL','OUTSIDE') THEN source_code
                       WHEN self_insurance_flag IS TRUE THEN 'SELF'
                       ELSE 'UNKNOWN'
                   END AS insurance_source,
                   count(*)::int AS policy_count,
                   count(*) FILTER (WHERE premium_amount IS NOT NULL)::int AS premium_populated,
                   COALESCE(sum(premium_amount),0) AS premium_amount
            FROM normalized_policy_rows
            GROUP BY 1,2,3 ORDER BY policy_count DESC, insurance_by, insurer
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
        summary = _query_rows(connection, """
            WITH booked AS (
                SELECT DISTINCT row_data->>'journey_id' AS journey_id
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
            ), policy_rows AS (
                SELECT i.row_data->>'journey_id' AS journey_id,
                       upper(NULLIF(i.row_data->>'insurance_source','')) AS source_code,
                       NULLIF(i.row_data->>'self_insurance_flag','')::boolean AS self_insurance_flag,
                       NULLIF(i.row_data->>'actual_premium_amount','')::numeric AS premium_amount
                FROM analytics.snapshot_rows i
                JOIN booked b ON b.journey_id=i.row_data->>'journey_id'
                WHERE i.tenant_id=:tenant_id AND i.dump_id=:dump_id
                  AND i.source_table='insurance_records'
            )
            SELECT (SELECT count(*)::int FROM booked) AS booked_journeys,
                   count(DISTINCT journey_id)::int AS insurance_journeys,
                   count(*)::int AS policy_records,
                   count(*) FILTER (WHERE premium_amount IS NOT NULL)::int AS premium_populated,
                   COALESCE(sum(premium_amount),0) AS captured_premium,
                   count(DISTINCT journey_id) FILTER (
                       WHERE (source_code IS NULL OR source_code NOT IN ('INHOUSE','SELF','EXTERNAL','OUTSIDE'))
                         AND self_insurance_flag IS NOT TRUE
                   )::int AS source_unclassified_journeys,
                   round(100.0 * count(DISTINCT journey_id)
                         / NULLIF((SELECT count(*) FROM booked),0),1) AS insurance_record_rate_pct
            FROM policy_rows
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
        source_split = _query_rows(connection, """
            WITH booked AS (
                SELECT DISTINCT row_data->>'journey_id' AS journey_id
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
            ), policy_rows AS (
                SELECT i.row_data->>'journey_id' AS journey_id,
                       upper(NULLIF(i.row_data->>'insurance_source','')) AS source_code,
                       NULLIF(i.row_data->>'self_insurance_flag','')::boolean AS self_insurance_flag,
                       NULLIF(i.row_data->>'actual_premium_amount','')::numeric AS premium_amount
                FROM analytics.snapshot_rows i
                JOIN booked b ON b.journey_id=i.row_data->>'journey_id'
                WHERE i.tenant_id=:tenant_id AND i.dump_id=:dump_id
                  AND i.source_table='insurance_records'
            )
            SELECT CASE
                       WHEN source_code IN ('INHOUSE','SELF','EXTERNAL','OUTSIDE') THEN source_code
                       WHEN self_insurance_flag IS TRUE THEN 'SELF'
                       ELSE 'UNKNOWN'
                   END AS insurance_source,
                   count(*)::int AS policy_count,
                   COALESCE(sum(premium_amount),0) AS premium_amount
            FROM policy_rows
            GROUP BY 1 ORDER BY policy_count DESC
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
        duplicate_agent_codes = _query_rows(connection, """
            SELECT row_data->>'agent_intermediary_code' AS agent_code, count(*)::int AS booking_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='insurance_records'
              AND NULLIF(row_data->>'agent_intermediary_code','') IS NOT NULL
            GROUP BY 1 HAVING count(*) > 1 ORDER BY booking_count DESC, agent_code
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary[0] if summary else {},
        "rows": rows,
        "source_split": source_split,
        "duplicate_agent_codes": duplicate_agent_codes,
        "definition": (
            "Insurance record rate uses distinct booked journeys with a policy record divided by booked journeys. "
            "An absent record is not proof a vehicle was uninsured. Unknown source values remain UNKNOWN; they are not "
            "defaulted to in-house. Premium is captured policy value, not automatically dealership revenue."
        ),
    }


@router.get("/addons")
def addons(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        rows = _query_rows(connection, """
            SELECT COALESCE(row_data->>'addon_type_code','UNSPECIFIED') AS addon_type,
                   count(*)::int AS attach_count,
                   COALESCE(sum(NULLIF(row_data->>'actual_amount','')::numeric),0) AS actual_amount
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_addons'
            GROUP BY 1 ORDER BY attach_count DESC, addon_type
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "rows": rows}


@router.get("/discounts")
def discounts(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        rows = _query_rows(connection, """
            SELECT COALESCE(row_data->>'discount_key','UNSPECIFIED') AS discount_key,
                   count(*)::int AS application_count,
                   COALESCE(sum(NULLIF(row_data->>'actual_discount_amount','')::numeric),0) AS actual_discount_amount,
                   COALESCE(sum(NULLIF(row_data->>'standard_eligible_amount','')::numeric),0) AS standard_eligible_amount
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='discount_applications'
            GROUP BY 1 ORDER BY actual_discount_amount DESC, discount_key
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "rows": rows}


@router.get("/trade-in")
def trade_in(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        rows = _query_rows(connection, """
            SELECT COALESCE(row_data->>'actual_status_code','UNSPECIFIED') AS status,
                   count(*)::int AS trade_in_count,
                   COALESCE(sum(NULLIF(row_data->>'actual_value','')::numeric),0) AS actual_value
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='trade_in_cases'
            GROUP BY 1 ORDER BY trade_in_count DESC, status
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "rows": rows}


@router.get("/turnaround")
def turnaround(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        rows = _query_rows(connection, """
            WITH receipts AS (
              SELECT row_data->>'journey_id' AS journey_id,
                     min(COALESCE(NULLIF(row_data->>'receipt_date','')::timestamp,
                                  NULLIF(row_data->>'payment_at_utc','')::timestamptz)) AS first_receipt
              FROM analytics.snapshot_rows
              WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='payments'
              GROUP BY 1
            ), deliveries AS (
              SELECT row_data->>'journey_id' AS journey_id,
                     NULLIF(row_data->>'actual_delivered_at','')::timestamptz AS delivered_at
              FROM analytics.snapshot_rows
              WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='deliveries'
            )
            SELECT count(*) FILTER (WHERE d.delivered_at IS NOT NULL AND r.first_receipt IS NOT NULL)::int AS completed_count,
                   round(avg(EXTRACT(EPOCH FROM (d.delivered_at-r.first_receipt))/86400.0)
                         FILTER (WHERE d.delivered_at IS NOT NULL AND r.first_receipt IS NOT NULL),2) AS avg_days
            FROM deliveries d LEFT JOIN receipts r USING (journey_id)
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "rows": rows}


@router.get("/productivity")
def productivity(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        # Per-employee summary: journeys created, bookings, deliveries, time taken
        summary = _query_rows(connection, """
            WITH jny AS (
                SELECT DISTINCT ON (row_data->>'journey_id')
                    COALESCE(NULLIF(row_data->>'created_by_actor_id',''), NULLIF(row_data->>'created_by_display_name',''), 'Unknown') AS employee_key,
                    COALESCE(NULLIF(row_data->>'created_by_display_name',''), NULLIF(row_data->>'created_by_actor_id',''), 'Unknown') AS employee_name,
                    NULLIF(row_data->>'created_by_actor_id','') AS actor_id,
                    row_data->>'journey_id' AS journey_id,
                    NULLIF(row_data->>'created_at_utc','')::timestamptz AS journey_created_at
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journeys'
                ORDER BY row_data->>'journey_id',
                         NULLIF(row_data->>'created_at_utc','')::timestamptz ASC NULLS LAST
            ),
            bkg AS (
                SELECT row_data->>'journey_id' AS journey_id,
                       bool_or(NULLIF(row_data->>'booking_date','') IS NOT NULL) AS has_booking_date
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
                GROUP BY 1
            ),
            dlv AS (
                SELECT row_data->>'journey_id' AS journey_id,
                       max(NULLIF(row_data->>'actual_delivered_at','')::timestamptz) AS actual_delivered_at,
                       bool_or(NULLIF(row_data->>'actual_delivery_status_code','') IS NOT NULL) AS delivery_started,
                       max(NULLIF(row_data->>'created_at_utc','')::timestamptz) AS delivery_created_at
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='deliveries'
                GROUP BY 1
            )
            SELECT
                COALESCE(NULLIF(max(j.employee_name),''), 'Unknown') AS employee_name,
                j.actor_id,
                count(DISTINCT j.journey_id)::int                               AS journeys_created,
                count(DISTINCT b.journey_id)::int                               AS bookings,
                count(DISTINCT b.journey_id) FILTER (WHERE b.has_booking_date)::int AS bookings_with_date,
                count(DISTINCT d.journey_id) FILTER (
                    WHERE b.journey_id IS NOT NULL
                      AND (d.delivery_created_at IS NOT NULL OR d.delivery_started)
                )::int AS deliveries_started,
                count(DISTINCT d.journey_id) FILTER (
                    WHERE b.journey_id IS NOT NULL AND d.actual_delivered_at IS NOT NULL
                )::int AS deliveries_completed,
                round(100.0 * count(DISTINCT b.journey_id)
                      / NULLIF(count(DISTINCT j.journey_id),0),1)::float AS booking_rate_pct,
                round(100.0 * count(DISTINCT d.journey_id) FILTER (
                          WHERE b.journey_id IS NOT NULL AND d.actual_delivered_at IS NOT NULL
                      ) / NULLIF(count(DISTINCT b.journey_id),0),1)::float AS delivery_completion_pct,
                round(avg(
                    EXTRACT(EPOCH FROM (
                        d.delivery_created_at - j.journey_created_at
                    )) / 86400.0
                ) FILTER (
                    WHERE j.journey_created_at IS NOT NULL
                      AND b.journey_id IS NOT NULL
                      AND d.delivery_created_at IS NOT NULL
                      AND d.delivery_created_at >= j.journey_created_at
                ), 1)::float AS avg_days_to_delivery_start,
                round(avg(
                    EXTRACT(EPOCH FROM (
                        d.actual_delivered_at - j.journey_created_at
                    )) / 86400.0
                ) FILTER (
                    WHERE j.journey_created_at IS NOT NULL
                      AND b.journey_id IS NOT NULL
                      AND d.actual_delivered_at IS NOT NULL
                      AND d.actual_delivered_at >= j.journey_created_at
                ), 1)::float AS avg_days_journey_to_delivery
            FROM jny j
            LEFT JOIN bkg b ON b.journey_id = j.journey_id
            LEFT JOIN dlv d ON d.journey_id = j.journey_id
            GROUP BY j.employee_key, j.actor_id
            ORDER BY journeys_created DESC
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])

        upload_activity = _query_rows(connection, """
            WITH journey_uploads AS (
                SELECT date_trunc(
                           'month', NULLIF(row_data->>'created_at_utc','')::timestamptz
                       )::date AS upload_month,
                       row_data->>'journey_id' AS journey_id,
                       COALESCE(
                           NULLIF(row_data->>'created_by_actor_id',''),
                           NULLIF(row_data->>'created_by_display_name','')
                       ) AS creator_key
                FROM analytics.snapshot_rows
                WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journeys'
                  AND NULLIF(row_data->>'created_at_utc','') IS NOT NULL
            )
            SELECT upload_month,
                   count(DISTINCT journey_id)::int AS journeys_created,
                   count(DISTINCT creator_key)::int AS attributed_creators,
                   count(DISTINCT journey_id) FILTER (WHERE creator_key IS NULL)::int
                       AS unattributed_journeys
            FROM journey_uploads
            GROUP BY upload_month
            ORDER BY upload_month
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])

    return {
        "tenant_id": tenant_id,
        "data_as_of": latest["data_as_of_utc"],
        "summary": summary,
        "upload_activity": upload_activity,
    }


@router.get("/dashboard")
def dashboard(tenant_id: str, principal: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    """Return the initial Analytics screen in one authenticated request.

    Calling the report functions directly reuses the already-authorized principal,
    so the browser no longer fans out five concurrent Security authorization checks.
    """
    return {
        "overview": overview(tenant_id, principal),
        "findings": findings(tenant_id, principal),
        "documents": documents(tenant_id, principal),
        "payments": payments(tenant_id, principal),
        "turnaround": turnaround(tenant_id, principal),
    }
