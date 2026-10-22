"""Pricing integrity reporting built from the current Audit Core snapshot.

Audit Core owns invoice selection and price-rule decisions. Analytics uses its
active findings for exception status and only calculates discount amounts
when the captured actual and entitlement can be compared without ambiguity.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


_PRICE_RULES = {"DEAL_UNDERCHARGED", "DEAL_TOTAL_VARIANCE_NEGATIVE"}
_DISCOUNT_RULE = "EXCESS_DISCOUNT"
_RULES = _PRICE_RULES | {_DISCOUNT_RULE}
_LINE_TOLERANCE = Decimal("100")
_INVOICE_SOURCE_TYPES = {
    "tax_invoice_tally",
    "customer_invoice_dms",
    "invoice_generic",
    "accessory_invoice_dms",
    "accessory_invoice_tally",
    "ew_invoice",
    "rsa_invoice",
    "tax_invoice",
    "tax_invoice_dms",
    "customer_invoice_dms_v2",
    "credit_note",
    "debit_note",
}
_BOOKING_SOURCE_TYPES = {"booking_form", "booking_docket", "order_taking_form"}


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _rows(connection: Connection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(text(sql), params).mappings().all()]


def _source_type(reference: Any) -> str:
    raw = str(reference or "").split(":", 1)[0]
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")


def _discount_entitlement(row: dict[str, Any]) -> Decimal | None:
    """Return only a known entitlement; null means the comparison is unknown."""
    key = str(row.get("discount_key") or "").upper()
    result = str(row.get("eligibility_result") or "").upper()
    actual = _decimal(row.get("actual_discount_amount"))
    if key == "OTHER_SCHEME" and row.get("mr_opted") and actual is not None:
        # Audit Core currently maps the booking MR amount to OTHER_SCHEME. If
        # MR was opted, the snapshot cannot attribute that actual to one offer.
        return None
    eligible = _decimal(row.get("standard_eligible_amount"))
    if eligible is not None:
        return eligible
    if result == "NOT_ELIGIBLE" or key == "ADDITIONAL_DISCOUNT":
        return Decimal(0)
    return None


def _discount_audit(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    by_key: dict[str, dict[str, Any]] = {}
    by_outlet: dict[tuple[str, str], dict[str, Any]] = {}
    total_known_excess = Decimal(0)
    excess_applications = 0
    excess_journeys: set[str] = set()
    unknown_entitlement_applications = 0
    missing_evidence_applications = 0

    for row in rows:
        actual = _decimal(row.get("actual_discount_amount"))
        if actual is None:
            continue

        key = str(row.get("discount_key") or "UNSPECIFIED")
        entitlement = _discount_entitlement(row)
        extra = max(actual - entitlement, Decimal(0)) if entitlement is not None else None
        if extra is not None and extra <= _LINE_TOLERANCE:
            extra = Decimal(0)
        is_exception = extra is not None and extra > 0
        if entitlement is None:
            unknown_entitlement_applications += 1
        if str(row.get("evidence_status") or "").upper() == "MISSING":
            missing_evidence_applications += 1

        key_row = by_key.setdefault(key, {
            "discount_key": key,
            "application_count": 0,
            "journey_ids": set(),
            "known_excess_discount_amount": Decimal(0),
            "excess_application_count": 0,
            "unknown_entitlement_count": 0,
            "missing_evidence_count": 0,
        })
        key_row["application_count"] += 1
        key_row["journey_ids"].add(row["journey_id"])
        if extra is not None:
            key_row["known_excess_discount_amount"] += extra
            total_known_excess += extra
        if is_exception:
            key_row["excess_application_count"] += 1
            excess_applications += 1
            excess_journeys.add(row["journey_id"])
        if entitlement is None:
            key_row["unknown_entitlement_count"] += 1
        if str(row.get("evidence_status") or "").upper() == "MISSING":
            key_row["missing_evidence_count"] += 1

        outlet_key = (str(row.get("dealer_name") or "Unspecified dealer"),
                      str(row.get("outlet_name") or "Unspecified outlet"))
        outlet_row = by_outlet.setdefault(outlet_key, {
            "dealer_name": outlet_key[0],
            "outlet_name": outlet_key[1],
            "known_excess_discount_amount": Decimal(0),
            "unknown_discount_applications": 0,
            "discount_journey_ids": set(),
        })
        outlet_row["discount_journey_ids"].add(row["journey_id"])
        if extra is not None:
            outlet_row["known_excess_discount_amount"] += extra
        if entitlement is None:
            outlet_row["unknown_discount_applications"] += 1

    key_rows = []
    for row in by_key.values():
        key_rows.append({
            **{key: value for key, value in row.items() if key != "journey_ids"},
            "journey_count": len(row["journey_ids"]),
        })
    key_rows.sort(key=lambda row: (-row["known_excess_discount_amount"], row["discount_key"]))

    outlet_rows = []
    for row in by_outlet.values():
        outlet_rows.append({
            **{key: value for key, value in row.items() if key != "discount_journey_ids"},
            "discount_journeys": len(row["discount_journey_ids"]),
        })
    outlet_rows_by_name = {(row["dealer_name"], row["outlet_name"]): row for row in outlet_rows}

    return {
        "known_excess_discount_amount": total_known_excess,
        "known_excess_application_count": excess_applications,
        "known_excess_discount_journeys": len(excess_journeys),
        "unknown_entitlement_application_count": unknown_entitlement_applications,
        "missing_discount_evidence_application_count": missing_evidence_applications,
        "by_discount_key": key_rows,
    }, outlet_rows_by_name


def _price_line_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    standards = 0
    comparable = 0
    invoice_backed = 0
    booking_backed = 0
    for row in rows:
        standard = _decimal(row.get("standard_amount"))
        actual = _decimal(row.get("actual_amount"))
        if standard is None:
            continue
        standards += 1
        if actual is None:
            continue
        comparable += 1
        source = _source_type(row.get("source_reference"))
        if source in _INVOICE_SOURCE_TYPES:
            invoice_backed += 1
        elif source in _BOOKING_SOURCE_TYPES:
            booking_backed += 1

    coverage_pct = round(comparable * 100 / standards, 1) if standards else None
    return {
        "standard_price_line_count": standards,
        "comparable_price_line_count": comparable,
        "invoice_backed_price_line_count": invoice_backed,
        "booking_backed_price_line_count": booking_backed,
        "comparable_price_line_coverage_pct": coverage_pct,
    }


def pricing_integrity_report(
    connection: Connection,
    *,
    tenant_id: str,
    dump_id: str,
    total_journeys: int | None = None,
) -> dict[str, Any]:
    """Build the focused price-integrity executive view and investigation data."""
    params = {"tenant_id": tenant_id, "dump_id": dump_id}
    dimensions_sql = """
    WITH journeys AS (
        SELECT row_data->>'journey_id' AS journey_id,
               row_data->>'journey_reference' AS journey_reference,
               row_data->>'dealer_id' AS dealer_id,
               row_data->>'outlet_id' AS outlet_id
        FROM analytics.snapshot_rows
        WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journeys'
    ), dealers AS (
        SELECT row_data->>'dealer_id' AS dealer_id,
               COALESCE(NULLIF(row_data->>'dealer_name',''), NULLIF(row_data->>'dealer_code',''),
                        'Unspecified dealer') AS dealer_name
        FROM analytics.snapshot_rows
        WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='dealers'
    ), outlets AS (
        SELECT row_data->>'outlet_id' AS outlet_id,
               COALESCE(NULLIF(row_data->>'outlet_name',''), NULLIF(row_data->>'outlet_code',''),
                        'Unspecified outlet') AS outlet_name
        FROM analytics.snapshot_rows
        WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='dealer_outlets'
    ), bookings AS (
        SELECT row_data->>'journey_id' AS journey_id,
               NULLIF(row_data->>'booking_date','') AS booking_date
        FROM analytics.snapshot_rows
        WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='bookings'
    ), products AS (
        SELECT row_data->>'journey_id' AS journey_id,
               COALESCE(NULLIF(row_data->>'model_name_snapshot',''), 'Unresolved') AS model_name,
               COALESCE(NULLIF(row_data->>'variant_name_snapshot',''), 'Unresolved') AS variant_name
        FROM analytics.snapshot_rows
        WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='journey_products'
    )
    SELECT j.journey_id, j.journey_reference, j.dealer_id, j.outlet_id,
           COALESCE(d.dealer_name, 'Unspecified dealer') AS dealer_name,
           COALESCE(o.outlet_name, 'Unspecified outlet') AS outlet_name,
           b.booking_date, COALESCE(p.model_name, 'Unresolved') AS model_name,
           COALESCE(p.variant_name, 'Unresolved') AS variant_name
    FROM journeys j
    LEFT JOIN dealers d ON d.dealer_id=j.dealer_id
    LEFT JOIN outlets o ON o.outlet_id=j.outlet_id
    LEFT JOIN bookings b ON b.journey_id=j.journey_id
    LEFT JOIN products p ON p.journey_id=j.journey_id
    """
    dimension_rows = _rows(connection, dimensions_sql, params)
    dimensions = {row["journey_id"]: row for row in dimension_rows}
    if total_journeys is None:
        total_journeys = len(dimension_rows)

    discount_rows = _rows(connection, """
        SELECT app.row_data->>'journey_id' AS journey_id,
               COALESCE(NULLIF(app.row_data->>'discount_key',''), 'UNSPECIFIED') AS discount_key,
               app.row_data->>'actual_discount_amount' AS actual_discount_amount,
               app.row_data->>'standard_eligible_amount' AS standard_eligible_amount,
               upper(COALESCE(app.row_data->>'eligibility_result','')) AS eligibility_result,
               upper(COALESCE(app.row_data->>'evidence_status','')) AS evidence_status,
               EXISTS (
                   SELECT 1 FROM analytics.snapshot_rows mr
                   WHERE mr.tenant_id=app.tenant_id AND mr.dump_id=app.dump_id
                     AND mr.source_table='p2_management_referrals'
                     AND mr.row_data->>'journey_id'=app.row_data->>'journey_id'
                     AND COALESCE(NULLIF(mr.row_data->>'opted','')::boolean,false)
               ) AS mr_opted
        FROM analytics.snapshot_rows app
        WHERE app.tenant_id=:tenant_id AND app.dump_id=:dump_id
          AND app.source_table='discount_applications'
        """, params)
    for row in discount_rows:
        dimension = dimensions.get(row["journey_id"], {})
        row["dealer_name"] = dimension.get("dealer_name") or "Unspecified dealer"
        row["outlet_name"] = dimension.get("outlet_name") or "Unspecified outlet"

    discount_summary, outlet_rows_by_name = _discount_audit(discount_rows)

    # Build finding rows separately: reuse the same compact journey dimensions,
    # and take only the privacy-safe finding text and audit routing fields.
    findings = _rows(connection, """
        WITH dims AS (
            """ + dimensions_sql + """
        )
        SELECT f.row_data->>'audit_finding_id' AS finding_id,
               d.journey_id, d.journey_reference, d.dealer_name, d.outlet_name, d.booking_date,
               d.model_name, d.variant_name,
               split_part(upper(COALESCE(f.row_data->>'rule_key','')), ':', 1) AS rule_key,
               COALESCE(NULLIF(f.row_data->>'title',''), 'Pricing exception') AS title,
               COALESCE(NULLIF(f.row_data->>'description',''), 'See the Audit Core finding for review details.') AS description,
               upper(COALESCE(f.row_data->>'severity','MEDIUM')) AS severity,
               upper(COALESCE(f.row_data->>'finding_status','OPEN')) AS finding_status,
               NULLIF(f.row_data->>'owner_role_code','') AS owner_role_code,
               NULLIF(f.row_data->>'sla_due_at_utc','') AS sla_due_at_utc
        FROM dims d
        JOIN analytics.snapshot_rows f ON f.tenant_id=:tenant_id AND f.dump_id=:dump_id
          AND f.source_table='audit_findings' AND f.row_data->>'journey_id'=d.journey_id
        WHERE upper(COALESCE(f.row_data->>'finding_status','')) IN ('OPEN','ACKNOWLEDGED')
          AND split_part(upper(COALESCE(f.row_data->>'rule_key','')), ':', 1)
              = ANY(ARRAY['DEAL_UNDERCHARGED','DEAL_TOTAL_VARIANCE_NEGATIVE','EXCESS_DISCOUNT'])
        """, params)

    # DEAL_TOTAL_VARIANCE_NEGATIVE overlaps the more detailed
    # DEAL_UNDERCHARGED finding. Keep it only when the detailed finding is absent.
    undercharged_journeys = {row["journey_id"] for row in findings if row["rule_key"] == "DEAL_UNDERCHARGED"}
    findings = [row for row in findings if not (
        row["rule_key"] == "DEAL_TOTAL_VARIANCE_NEGATIVE" and row["journey_id"] in undercharged_journeys
    )]

    line_rows = _rows(connection, """
        SELECT row_data->>'standard_amount' AS standard_amount,
               row_data->>'actual_amount' AS actual_amount,
               row_data->>'source_reference' AS source_reference
        FROM analytics.snapshot_rows
        WHERE tenant_id=:tenant_id AND dump_id=:dump_id AND source_table='commercial_lines'
        """, params)
    line_coverage = _price_line_coverage(line_rows)

    price_journeys = {row["journey_id"] for row in findings if row["rule_key"] in _PRICE_RULES}
    discount_journeys = {row["journey_id"] for row in findings if row["rule_key"] == _DISCOUNT_RULE}
    any_exception_journeys = price_journeys | discount_journeys

    exception_by_outlet: dict[tuple[str, str], dict[str, Any]] = {}
    for row in findings:
        key = (row["dealer_name"], row["outlet_name"])
        outlet = exception_by_outlet.setdefault(key, {
            "price_journey_ids": set(), "discount_journey_ids": set(),
        })
        if row["rule_key"] in _PRICE_RULES:
            outlet["price_journey_ids"].add(row["journey_id"])
        if row["rule_key"] == _DISCOUNT_RULE:
            outlet["discount_journey_ids"].add(row["journey_id"])

    outlet_rows = []
    for key in set(exception_by_outlet) | set(outlet_rows_by_name):
        audit_counts = exception_by_outlet.get(key, {})
        discount_data = outlet_rows_by_name.get(key, {})
        outlet_rows.append({
            "dealer_name": key[0],
            "outlet_name": key[1],
            "price_exception_journeys": len(audit_counts.get("price_journey_ids", set())),
            "discount_exception_journeys": len(audit_counts.get("discount_journey_ids", set())),
            "known_excess_discount_amount": discount_data.get("known_excess_discount_amount", Decimal(0)),
            "unknown_discount_applications": discount_data.get("unknown_discount_applications", 0),
        })
    outlet_rows.sort(key=lambda row: (
        -(row["price_exception_journeys"] + row["discount_exception_journeys"]),
        -row["known_excess_discount_amount"], row["dealer_name"], row["outlet_name"],
    ))

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    findings.sort(key=lambda row: (
        severity_order.get(row["severity"], 5),
        row["sla_due_at_utc"] or "9999-12-31",
        row["dealer_name"], row["outlet_name"], row["journey_id"], row["rule_key"],
    ))

    return {
        "summary": {
            "total_journeys": int(total_journeys or 0),
            "price_exception_journeys": len(price_journeys),
            "discount_exception_journeys": len(discount_journeys),
            "journeys_with_pricing_exceptions": len(any_exception_journeys),
            **discount_summary,
            **line_coverage,
        },
        "by_discount_key": discount_summary["by_discount_key"],
        "by_outlet": outlet_rows[:50],
        "exceptions": findings[:200],
        "definitions": {
            "exception_status": "Current OPEN and ACKNOWLEDGED Audit Core pricing findings in the latest snapshot.",
            "known_excess_discount": (
                "Per application, actual discount less its captured entitlement, floored at zero. "
                "Applications without a known entitlement, including unattributed management-referral "
                "amounts, are excluded and counted as unknown. A line exception uses Audit Core's ₹100 tolerance."
            ),
            "price_line_coverage": (
                "Commercial component rows with both a standard and current actual divided by rows with a "
                "standard. This is line coverage, not a claim that every deal has a complete invoice comparison."
            ),
        },
    }
