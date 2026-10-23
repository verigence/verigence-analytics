from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
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
    SourceTable("vehicle_records", "vehicle_record_id"),
    SourceTable("registration_records", "registration_record_id"),
    SourceTable("deliveries", "delivery_id"),
    SourceTable("audit_evaluations", "audit_evaluation_id"),
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
    # Phase-2 additions — migration 0137 (dealer discount grid)
    SourceTable("dealer_discount_grid_versions", "grid_version_id"),
    SourceTable("dealer_discount_grid_rows", "grid_row_id"),
    # Phase-2 additions — migration 0138 (management-referral discount)
    SourceTable("p2_management_referrals", "journey_id"),
)


_DERIVED_GEOGRAPHY_TABLE = "derived_customer_geography"
_DERIVED_ACCESSORY_LINE_ITEMS_TABLE = "derived_accessory_line_items"


def _identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"Unsafe SQL identifier: {value}")
    return value


def _insert_batch(
    target: Connection,
    *,
    dump_id: UUID,
    tenant_id: str,
    table: str,
    rows: list[dict],
) -> None:
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
                (CAST(:dump_id AS uuid), :tenant_id, :source_table, :source_pk,
                 CAST(:row_data AS jsonb))
            """
        ),
        payload,
    )


def _copy_table(
    source: Connection,
    target: Connection,
    *,
    source_schema: str,
    source_table: SourceTable,
    tenant_id: str,
    dump_id: UUID,
    batch_size: int,
) -> int:
    schema = _identifier(source_schema)
    table_name = _identifier(source_table.name)
    primary_key = _identifier(source_table.primary_key)
    if source_table.name == "journey_products":
        # Keep the captured evidence labels, but attach the catalogue names when
        # Audit Core has already resolved this product to a canonical SKU. This
        # makes Analytics grouping stable without changing the source records.
        query = text(
            f"SELECT CAST(t.{primary_key} AS text) AS source_pk, "
            "to_jsonb(t) || jsonb_strip_nulls(jsonb_build_object("
            "'canonical_sku_code', s.sku_code, "
            "'canonical_model_code', m.model_code, "
            "'canonical_model_name', m.model_name, "
            "'canonical_variant_code', v.variant_code, "
            "'canonical_variant_name', v.variant_name"
            ")) AS row_data "
            f"FROM {schema}.{table_name} AS t "
            f"LEFT JOIN {schema}.product_skus AS s ON s.product_sku_id=t.product_sku_id "
            f"LEFT JOIN {schema}.product_models AS m ON m.model_id=s.model_id "
            f"LEFT JOIN {schema}.product_variants AS v ON v.variant_id=s.variant_id "
            "WHERE t.tenant_id = :tenant_id"
        )
    else:
        query = text(
            f"SELECT CAST(t.{primary_key} AS text) AS source_pk, to_jsonb(t) AS row_data "
            f"FROM {schema}.{table_name} AS t WHERE t.tenant_id = :tenant_id"
        )
    result = source.execute(query, {"tenant_id": tenant_id}).mappings()
    table_count = 0
    while True:
        batch = result.fetchmany(batch_size)
        if not batch:
            break
        dict_batch = [dict(row) for row in batch]
        _insert_batch(
            target,
            dump_id=dump_id,
            tenant_id=tenant_id,
            table=table_name,
            rows=dict_batch,
        )
        table_count += len(dict_batch)
    return table_count


def _copy_derived_customer_geography(
    source: Connection,
    target: Connection,
    *,
    source_schema: str,
    tenant_id: str,
    dump_id: UUID,
    batch_size: int,
) -> int:
    """Copy only an explicit six-digit customer PIN and its review provenance.

    The extracted-field table contains address data, so this query intentionally
    reads only ``address_pincode`` and never persists a full address in Analytics.
    """
    schema = _identifier(source_schema)
    query = text(
        f"""
        WITH explicit_pins AS (
            SELECT extracted_field_id,
                   journey_id,
                   evidence_id,
                   field_key,
                   trim(effective_value #>> '{{}}') AS normalized_value,
                   is_modified,
                   reviewed_at_utc,
                   confidence_score,
                   created_at_utc
            FROM {schema}.journey_document_extracted_fields
            WHERE tenant_id = :tenant_id
              AND field_key = 'address_pincode'
              AND jsonb_typeof(effective_value) = 'string'
              AND trim(effective_value #>> '{{}}') ~ '^[1-9][0-9]{{5}}$'
        ),
        ranked AS (
            SELECT *,
                   row_number() OVER (
                       PARTITION BY journey_id
                       ORDER BY
                           (reviewed_at_utc IS NOT NULL) DESC,
                           confidence_score DESC NULLS LAST,
                           created_at_utc DESC NULLS LAST,
                           extracted_field_id
                   ) AS preference_rank
            FROM explicit_pins
        )
        SELECT CAST(extracted_field_id AS text) AS source_pk,
               jsonb_build_object(
                   'journey_id', CAST(journey_id AS text),
                   'customer_pincode', normalized_value,
                   'source_field_key', field_key,
                   'source_evidence_id', CAST(evidence_id AS text),
                   'derivation_method', 'EXPLICIT_ADDRESS_PINCODE',
                   'source_reviewed_at_utc', reviewed_at_utc,
                   'source_confidence_score', confidence_score,
                   'source_is_modified', COALESCE(is_modified, false)
               ) AS row_data
        FROM ranked
        WHERE preference_rank = 1
        """
    )
    result = source.execute(query, {"tenant_id": tenant_id}).mappings()
    row_count = 0
    while True:
        batch = result.fetchmany(batch_size)
        if not batch:
            break
        dict_batch = [dict(row) for row in batch]
        _insert_batch(
            target,
            dump_id=dump_id,
            tenant_id=tenant_id,
            table=_DERIVED_GEOGRAPHY_TABLE,
            rows=dict_batch,
        )
        row_count += len(dict_batch)
    return row_count


def _copy_derived_accessory_line_items(
    source: Connection,
    target: Connection,
    *,
    source_schema: str,
    tenant_id: str,
    dump_id: UUID,
    batch_size: int,
) -> int:
    """Project accessory invoice lines without copying customer or vehicle PII.

    ``invoice_review_values`` includes customer, tax, address and vehicle identifiers.
    This projection deliberately selects only the journey link, commercial line fields,
    document classification and redacted invoice source. It never serializes the source
    row or the complete ``line_items`` JSON.
    """
    schema = _identifier(source_schema)
    query = text(
        f"""
        WITH accessory_documents AS (
            SELECT invoice_review_value_id,
                   journey_id,
                   source_system,
                   document_type_key,
                   invoice_nature,
                   line_items
            FROM {schema}.invoice_review_values
            WHERE tenant_id = :tenant_id
              AND upper(trim(COALESCE(invoice_purpose, ''))) = 'ACCESSORY'
        ), line_rows AS (
            SELECT d.invoice_review_value_id,
                   d.journey_id,
                   d.source_system,
                   d.document_type_key,
                   d.invoice_nature,
                   item.ordinality AS line_number,
                   item.value AS line_item
            FROM accessory_documents d
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(d.line_items) = 'array'
                     THEN d.line_items ELSE '[]'::jsonb END
            ) WITH ORDINALITY AS item(value, ordinality)
        ), normalized AS (
            SELECT *,
                   left(
                       regexp_replace(
                           regexp_replace(
                               regexp_replace(
                                   regexp_replace(
                                       trim(COALESCE(line_item->>'description_raw', '')),
                                       '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{{2,}}',
                                       '[redacted email]', 'gi'
                                   ),
                                   '(?:\\+?91[ .-]?)?[6-9][0-9]{{4}}[ .-]?[0-9]{{5}}',
                                   '[redacted phone]', 'gi'
                               ),
                               '[A-HJ-NPR-Z0-9]{{17}}', '[redacted vehicle id]', 'gi'
                           ),
                           '[0-9]{{2}}[A-Z]{{5}}[0-9]{{4}}[A-Z][A-Z0-9]Z[A-Z0-9]',
                           '[redacted tax id]', 'gi'
                       ),
                       180
                   ) AS safe_description
            FROM line_rows
        )
        SELECT CAST(invoice_review_value_id AS text) || ':' || line_number AS source_pk,
               jsonb_strip_nulls(jsonb_build_object(
                   'journey_id', CAST(journey_id AS text),
                   'source_system', NULLIF(trim(COALESCE(source_system, '')), ''),
                   'document_type_key', NULLIF(trim(COALESCE(document_type_key, '')), ''),
                   'invoice_nature', NULLIF(trim(COALESCE(invoice_nature, '')), ''),
                   'line_number', line_number,
                   'line_category', NULLIF(trim(COALESCE(line_item->>'line_category', '')), ''),
                   'description', NULLIF(safe_description, ''),
                   'item_code', NULLIF(trim(COALESCE(line_item->>'item_code', '')), ''),
                   'quantity', CASE
                       WHEN trim(COALESCE(line_item->>'quantity', '')) ~ '^-?[0-9]+([.][0-9]+)?$'
                       THEN to_jsonb(trim(line_item->>'quantity')::numeric)
                   END,
                   'unit_rate', CASE
                       WHEN trim(COALESCE(line_item->>'unit_rate', '')) ~ '^-?[0-9]+([.][0-9]+)?$'
                       THEN to_jsonb(trim(line_item->>'unit_rate')::numeric)
                   END,
                   'gross_amount', CASE
                       WHEN trim(COALESCE(line_item->>'gross_amount', '')) ~ '^-?[0-9]+([.][0-9]+)?$'
                       THEN to_jsonb(trim(line_item->>'gross_amount')::numeric)
                   END,
                   'discount_amount', CASE
                       WHEN trim(COALESCE(line_item->>'discount_amount', '')) ~ '^-?[0-9]+([.][0-9]+)?$'
                       THEN to_jsonb(trim(line_item->>'discount_amount')::numeric)
                   END,
                   'net_amount', CASE
                       WHEN trim(COALESCE(line_item->>'net_amount', '')) ~ '^-?[0-9]+([.][0-9]+)?$'
                       THEN to_jsonb(trim(line_item->>'net_amount')::numeric)
                   END,
                   'taxable_amount', CASE
                       WHEN trim(COALESCE(line_item->>'taxable_amount', '')) ~ '^-?[0-9]+([.][0-9]+)?$'
                       THEN to_jsonb(trim(line_item->>'taxable_amount')::numeric)
                   END,
                   'tax_amount', CASE
                       WHEN trim(COALESCE(line_item->>'tax_amount', '')) ~ '^-?[0-9]+([.][0-9]+)?$'
                       THEN to_jsonb(trim(line_item->>'tax_amount')::numeric)
                   END
               )) AS row_data
        FROM normalized
        """
    )
    result = source.execute(query, {"tenant_id": tenant_id}).mappings()
    row_count = 0
    while True:
        batch = result.fetchmany(batch_size)
        if not batch:
            break
        dict_batch = [dict(row) for row in batch]
        _insert_batch(
            target,
            dump_id=dump_id,
            tenant_id=tenant_id,
            table=_DERIVED_ACCESSORY_LINE_ITEMS_TABLE,
            rows=dict_batch,
        )
        row_count += len(dict_batch)
    return row_count


def create_dump(*, tenant_id: str, requested_by: str = "manual") -> UUID:
    if not tenant_id.strip():
        raise ValueError("tenant_id is required")
    settings = load_settings(require_security=False)
    if tenant_id.strip() in settings.excluded_tenant_ids:
        raise ValueError("Analytics dump is disabled for an excluded tenant")
    dump_id = uuid4()
    started = datetime.now(UTC)

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
            {
                "dump_id": dump_id,
                "tenant_id": tenant_id,
                "requested_by": requested_by,
                "started_at": started,
            },
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
                        table_count = _copy_table(
                            source,
                            target,
                            source_schema=settings.source_schema,
                            source_table=source_table,
                            tenant_id=tenant_id,
                            dump_id=dump_id,
                            batch_size=settings.dump_batch_size,
                        )
                        counts[source_table.name] = table_count
                        total += table_count

                    geography_count = _copy_derived_customer_geography(
                        source,
                        target,
                        source_schema=settings.source_schema,
                        tenant_id=tenant_id,
                        dump_id=dump_id,
                        batch_size=settings.dump_batch_size,
                    )
                    counts[_DERIVED_GEOGRAPHY_TABLE] = geography_count
                    total += geography_count

                    accessory_line_count = _copy_derived_accessory_line_items(
                        source,
                        target,
                        source_schema=settings.source_schema,
                        tenant_id=tenant_id,
                        dump_id=dump_id,
                        batch_size=settings.dump_batch_size,
                    )
                    counts[_DERIVED_ACCESSORY_LINE_ITEMS_TABLE] = accessory_line_count
                    total += accessory_line_count
                tx.commit()
            except Exception:
                tx.rollback()
                raise

        completed = datetime.now(UTC)
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
                {
                    "completed": completed,
                    "row_count": total,
                    "table_counts": json.dumps(counts),
                    "dump_id": dump_id,
                },
            )
            target.execute(
                text(
                    """
                    DELETE FROM analytics.dump_runs
                    WHERE dump_id IN (
                        SELECT dump_id FROM analytics.dump_runs
                        WHERE tenant_id=:tenant_id AND status='COMPLETED' AND dump_id <> :dump_id
                        ORDER BY completed_at_utc DESC NULLS LAST
                        OFFSET :retain
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dump_id": dump_id,
                    "retain": settings.dump_retention - 1,
                },
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
