from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from analytics.config import load_settings
from analytics.db import analytics_engine, source_engine


@dataclass(frozen=True)
class SourceTable:
    name: str
    primary_key: str


SOURCE_TABLES: tuple[SourceTable, ...] = (
    SourceTable("dealers", "dealer_id"),
    SourceTable("dealer_outlets", "outlet_id"),
    SourceTable("dealership_staff", "dealership_staff_id"),
    SourceTable("customers", "customer_id"),
    SourceTable("journeys", "journey_id"),
    SourceTable("bookings", "booking_id"),
    SourceTable("journey_products", "journey_product_id"),
    SourceTable("commercial_lines", "commercial_line_id"),
    SourceTable("discount_applications", "discount_application_id"),
    SourceTable("payments", "payment_id"),
    SourceTable("payment_verification_events", "payment_verification_event_id"),
    SourceTable("finance_records", "finance_record_id"),
    SourceTable("insurance_records", "insurance_record_id"),
    SourceTable("journey_addons", "journey_addon_id"),
    SourceTable("trade_in_cases", "trade_in_case_id"),
    SourceTable("deliveries", "delivery_id"),
    SourceTable("audit_findings", "audit_finding_id"),
    SourceTable("review_decisions", "review_decision_id"),
    SourceTable("journey_document_requirements", "journey_document_requirement_id"),
    SourceTable("journey_document_assessments", "journey_document_assessment_id"),
    SourceTable("journey_workflow_events", "event_id"),
    SourceTable("workflow_tasks", "workflow_task_id"),
    SourceTable("workflow_task_events", "workflow_task_event_id"),
    SourceTable("workflow_task_attempts", "workflow_task_attempt_id"),
    SourceTable("daily_ops_runs", "daily_ops_run_id"),
    SourceTable("daily_ops_items", "daily_ops_item_id"),
    SourceTable("pc_daily_notes", "pc_daily_note_id"),
    SourceTable("customer_identity_index", "identity_index_id"),
    SourceTable("crm_interactions", "crm_interaction_id"),
    SourceTable("escalations", "escalation_id"),
)


def _identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"Unsafe SQL identifier: {value}")
    return value


def _insert_batch(target: Connection, *, dump_id: UUID, tenant_id: str, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    payload = [
        {
            "dump_id": str(dump_id),
            "tenant_id": tenant_id,
            "source_table": table,
            "source_pk": str(row["source_pk"]),
            "row_data": json.dumps(row["row_data"], default=str),
        }
        for row in rows
    ]
    target.execute(
        text(
            """
            INSERT INTO analytics.snapshot_rows
                (dump_id, tenant_id, source_table, source_pk, row_data)
            VALUES
                (CAST(:dump_id AS uuid), :tenant_id, :source_table, :source_pk, CAST(:row_data AS jsonb))
            """
        ),
        payload,
    )


def create_dump(*, tenant_id: str, requested_by: str = "manual") -> UUID:
    if not tenant_id.strip():
        raise ValueError("tenant_id is required")
    settings = load_settings(require_security=False)
    dump_id = uuid4()
    started = datetime.now(timezone.utc)

    with analytics_engine().begin() as target:
        target.execute(
            text(
                """
                INSERT INTO analytics.dump_runs
                    (dump_id, tenant_id, trigger_kind, requested_by, status, started_at_utc)
                VALUES
                    (:dump_id, :tenant_id, 'MANUAL', :requested_by, 'RUNNING', :started_at)
                """
            ),
            {"dump_id": dump_id, "tenant_id": tenant_id, "requested_by": requested_by, "started_at": started},
        )

    counts: dict[str, int] = {}
    total = 0
    try:
        with source_engine().connect() as source:
            source = source.execution_options(isolation_level="REPEATABLE READ")
            tx = source.begin()
            try:
                source.execute(text("SET TRANSACTION READ ONLY"))
                with analytics_engine().begin() as target:
                    lock = target.execute(
                        text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                        {"key": f"verigence-analytics:{tenant_id}"},
                    ).scalar_one()
                    if not lock:
                        raise RuntimeError(f"A dump is already running for tenant {tenant_id}")

                    for source_table in SOURCE_TABLES:
                        schema = _identifier(settings.source_schema)
                        table_name = _identifier(source_table.name)
                        primary_key = _identifier(source_table.primary_key)
                        query = text(
                            f"SELECT CAST(t.{primary_key} AS text) AS source_pk, to_jsonb(t) AS row_data "
                            f"FROM {schema}.{table_name} AS t WHERE t.tenant_id = :tenant_id"
                        )
                        result = source.execute(query, {"tenant_id": tenant_id}).mappings()
                        table_count = 0
                        while True:
                            batch = result.fetchmany(settings.dump_batch_size)
                            if not batch:
                                break
                            dict_batch = [dict(row) for row in batch]
                            _insert_batch(target, dump_id=dump_id, tenant_id=tenant_id, table=table_name, rows=dict_batch)
                            table_count += len(dict_batch)
                        counts[table_name] = table_count
                        total += table_count
                tx.commit()
            except Exception:
                tx.rollback()
                raise

        completed = datetime.now(timezone.utc)
        with analytics_engine().begin() as target:
            target.execute(
                text(
                    """
                    UPDATE analytics.dump_runs
                    SET status='COMPLETED', completed_at_utc=:completed,
                        data_as_of_utc=:completed, row_count=:row_count,
                        table_counts=CAST(:table_counts AS jsonb)
                    WHERE dump_id=:dump_id
                    """
                ),
                {"completed": completed, "row_count": total, "table_counts": json.dumps(counts), "dump_id": dump_id},
            )
            target.execute(
                text(
                    """
                    DELETE FROM analytics.snapshot_rows
                    WHERE dump_id IN (
                        SELECT dump_id FROM analytics.dump_runs
                        WHERE tenant_id=:tenant_id AND status='COMPLETED' AND dump_id <> :dump_id
                        ORDER BY completed_at_utc DESC NULLS LAST
                        OFFSET :retain
                    )
                    """
                ),
                {"tenant_id": tenant_id, "dump_id": dump_id, "retain": settings.dump_retention - 1},
            )
        return dump_id
    except Exception as exc:
        with analytics_engine().begin() as target:
            target.execute(
                text(
                    """
                    UPDATE analytics.dump_runs
                    SET status='FAILED', completed_at_utc=now(), error_message=:error
                    WHERE dump_id=:dump_id
                    """
                ),
                {"dump_id": dump_id, "error": str(exc)[:2000]},
            )
        raise
