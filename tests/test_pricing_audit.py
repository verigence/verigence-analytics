from decimal import Decimal

from analytics.pricing_audit import (
    _decimal,
    _discount_audit,
    _discount_entitlement,
    _price_line_coverage,
)


def _discount_row(
    journey_id: str,
    key: str,
    actual: str,
    eligible: str | None,
    *,
    eligibility: str = "ELIGIBLE",
    mr_opted: bool = False,
    evidence: str = "PRESENT",
) -> dict:
    return {
        "journey_id": journey_id,
        "discount_key": key,
        "actual_discount_amount": actual,
        "standard_eligible_amount": eligible,
        "eligibility_result": eligibility,
        "evidence_status": evidence,
        "mr_opted": mr_opted,
        "dealer_name": "Dealer A",
        "outlet_name": "Outlet 1",
    }


def test_discount_excess_is_compared_per_application_without_cross_key_netting() -> None:
    rows = [
        _discount_row("journey-1", "CASH_DISCOUNT", "1200", "1000"),
        _discount_row("journey-1", "EXCHANGE_BONUS", "0", "500"),
    ]

    summary, outlets = _discount_audit(rows)

    assert summary["known_excess_discount_amount"] == Decimal(200)
    assert summary["known_excess_discount_journeys"] == 1
    assert summary["known_excess_application_count"] == 1
    assert outlets[("Dealer A", "Outlet 1")]["known_excess_discount_amount"] == Decimal(200)


def test_discount_tolerance_and_unknown_entitlements_are_kept_separate() -> None:
    within_tolerance = _discount_row("journey-1", "CASH_DISCOUNT", "1100", "1000")
    above_tolerance = _discount_row("journey-2", "ADDITIONAL_DISCOUNT", "101", None)
    unknown = _discount_row("journey-3", "CORPORATE_PRIVILEGE", "900", None)
    unattributed_mr = _discount_row(
        "journey-4", "OTHER_SCHEME", "700", None, mr_opted=True
    )
    not_eligible = _discount_row(
        "journey-5", "EXCHANGE_BONUS", "500", None, eligibility="NOT_ELIGIBLE"
    )

    summary, _ = _discount_audit(
        [within_tolerance, above_tolerance, unknown, unattributed_mr, not_eligible]
    )

    assert summary["known_excess_discount_amount"] == Decimal(601)
    assert summary["known_excess_application_count"] == 2
    assert summary["unknown_entitlement_application_count"] == 2
    assert summary["by_discount_key"][0]["discount_key"] == "EXCHANGE_BONUS"
    assert _discount_entitlement(unattributed_mr) is None


def test_price_line_coverage_reports_only_comparable_lines_and_known_sources() -> None:
    coverage = _price_line_coverage([
        {"standard_amount": "10000", "actual_amount": "10000", "source_reference": "tax_invoice_tally:1"},
        {"standard_amount": "20000", "actual_amount": "19900", "source_reference": "booking_form:2"},
        {"standard_amount": "30000", "actual_amount": None, "source_reference": None},
        {"standard_amount": None, "actual_amount": "40000", "source_reference": "tax_invoice:3"},
    ])

    assert coverage == {
        "standard_price_line_count": 3,
        "comparable_price_line_count": 2,
        "invoice_backed_price_line_count": 1,
        "booking_backed_price_line_count": 1,
        "comparable_price_line_coverage_pct": 66.7,
    }


def test_non_finite_amounts_are_not_treated_as_price_values() -> None:
    assert _decimal("NaN") is None
    assert _decimal("Infinity") is None
    assert _decimal("1200.50") == Decimal("1200.50")
