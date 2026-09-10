from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Connection, text

from analytics.db import analytics_engine
from analytics.security import Principal, require_analytics_read

router = APIRouter(prefix="/v1/analytics/tenants/{tenant_id}", tags=["analytics"])


def _latest_dump(connection: Connection, tenant_id: str) -> dict:
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
            SELECT COALESCE(row_data->>'payment_method_code','UNSPECIFIED') AS payment_method,
                   count(*)::int AS payment_count,
                   COALESCE(sum(NULLIF(row_data->>'amount','')::numeric),0) AS total_amount
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='payments'
            GROUP BY 1 ORDER BY total_amount DESC, payment_method
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "rows": rows}


@router.get("/finance")
def finance(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        rows = _query_rows(connection, """
            SELECT COALESCE(row_data->>'finance_type_code','UNSPECIFIED') AS finance_type,
                   COALESCE(row_data->>'provider_name','UNSPECIFIED') AS provider,
                   count(*)::int AS deal_count,
                   COALESCE(sum(NULLIF(row_data->>'financed_amount','')::numeric),0) AS financed_amount
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='finance_records'
            GROUP BY 1,2 ORDER BY deal_count DESC, finance_type, provider
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "rows": rows}


@router.get("/insurance")
def insurance(tenant_id: str, _: Annotated[Principal, Depends(require_analytics_read)]) -> dict:
    with analytics_engine().connect() as connection:
        latest = _latest_dump(connection, tenant_id)
        rows = _query_rows(connection, """
            SELECT COALESCE(row_data->>'insurance_by','UNSPECIFIED') AS insurance_by,
                   COALESCE(row_data->>'insurer_name','UNSPECIFIED') AS insurer,
                   count(*)::int AS policy_count,
                   COALESCE(sum(NULLIF(row_data->>'actual_premium_amount','')::numeric),0) AS premium_amount
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='insurance_records'
            GROUP BY 1,2 ORDER BY policy_count DESC, insurance_by, insurer
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
        duplicate_agent_codes = _query_rows(connection, """
            SELECT row_data->>'agent_intermediary_code' AS agent_code, count(*)::int AS booking_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='insurance_records'
              AND NULLIF(row_data->>'agent_intermediary_code','') IS NOT NULL
            GROUP BY 1 HAVING count(*) > 1 ORDER BY booking_count DESC, agent_code
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "rows": rows, "duplicate_agent_codes": duplicate_agent_codes}


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
        rows = _query_rows(connection, """
            SELECT COALESCE(row_data->>'actor_role_snapshot','UNSPECIFIED') AS actor_role,
                   COALESCE(row_data->>'actor_id','UNSPECIFIED') AS actor_id,
                   (NULLIF(row_data->>'occurred_at_utc','')::timestamptz)::date AS activity_date,
                   count(*)::int AS activity_count
            FROM analytics.snapshot_rows
            WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_workflow_events'
            GROUP BY 1,2,3 ORDER BY activity_date DESC NULLS LAST, activity_count DESC
            """, tenant_id=tenant_id, dump_id=latest["dump_id"])
    return {"tenant_id": tenant_id, "data_as_of": latest["data_as_of_utc"], "rows": rows}


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
