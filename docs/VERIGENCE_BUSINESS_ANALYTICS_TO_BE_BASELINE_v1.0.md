# Verigence Business Analytics — TO-BE Baseline v1.0

**Status:** BASELINED  
**Date:** 11-Sep-2026  
**Repository:** `verigence-analytics`  
**Scope:** Business Analytics for one selected Verigence Project/Tenant  
**Implementation boundary for Phase 1:** `verigence-analytics` only. No changes to DI, Audit Core, Security, Web, or any other repository.

---

## 1. Purpose

Verigence Analytics is not an audit-table viewer. Its target state is a dealership business-intelligence layer built from the same controlled evidence and operational facts used by Verigence Audit Core.

The system must answer business questions such as:

- Which dealers and outlets are performing strongly or weakly?
- How many cars are booked and delivered, and which models/variants/colours are driving volume?
- How long does a vehicle take from booking to allocation and booking to delivery?
- How much discount is being given per car/model/outlet, and where are the outliers?
- What are finance, insurance, trade-in, accessories, EW and RSA penetration rates?
- Which insurance add-ons are actually being sold?
- Which customer geographies/PIN codes are buying which products and from which outlets?
- Where are compliance failures concentrated and what commercial value is associated with them?
- Which employees/salespeople/teams are handling business effectively?

Incomplete source data must not reduce the target design. The target remains visible; unavailable metrics are returned with explicit coverage/gap information until the source data is populated.

---

## 2. Canonical analytical grain

The primary business grain is:

> **One vehicle journey / one car deal**

Each deal is enriched, where available, with:

`Project -> Dealer -> Outlet -> Salesperson -> Customer Geography -> Model -> Variant -> Colour -> Deal`

and with commercial/event dimensions including:

- booking and booking-confirmation dates
- vehicle allocation
- planned and actual delivery
- finance
- insurance and insurance add-ons
- accessories / EW / RSA / other VAS
- trade-in
- payments / receipts
- commercial price components
- discounts and eligibility
- document compliance
- audit findings / severity / status
- employee/workflow activity

All reports must reuse the same business definitions so a journey cannot be counted differently between reports.

---

## 3. Universal dimensions

Where the data exists, every applicable metric should be sliceable by:

- Project
- Dealer
- Outlet
- Salesperson / dealership staff
- Customer PIN code / city / district / state
- Vehicle model
- Vehicle variant
- Vehicle colour
- Deal type / source / lead source
- Booking month / delivery month / reporting period

No cross-tenant comparison is required by this baseline.

---

## 4. TO-BE report estate

### 4.1 Executive / Project Overview

Business pulse for the selected Project:

- cars booked / journeys
- cars delivered
- dealer and outlet contribution
- model / variant mix
- total receipt/payment value
- finance penetration
- dealer/self insurance penetration
- trade-in penetration
- accessories, EW, RSA and service-package penetration
- average discount per car
- journeys with compliance findings
- journeys with missing-document issues
- high-severity / open findings
- data-coverage indicators for every important metric

### 4.2 Dealer & Outlet Performance

Side-by-side dealer/outlet scorecards containing:

- volume and delivered volume
- model/variant mix
- finance / insurance / trade-in penetration
- accessories / EW / RSA penetration
- corporate / GST / exchange-benefit incidence
- average discount per deal and by model
- delivery TAT
- compliance issue rate
- missing-document rate
- high-severity findings
- payment value

Configured dealers/outlets with zero activity remain visible.

### 4.3 Sales & Product

- bookings/journeys by dealer/outlet
- deliveries by dealer/outlet
- model, variant and colour mix
- model x outlet comparison
- salesperson contribution when linkage is populated
- deal/lead source when populated
- period trends when historical snapshots are available

### 4.4 Customer Geography

Privacy-safe aggregation only; do not expose customer address in Analytics.

Target measures:

- sales/journeys by PIN code
- deliveries by PIN code
- model/variant mix by PIN code
- outlet catchment by PIN code
- territory overlap between outlets
- finance / insurance / VAS penetration by geography
- average discount by geography
- white-space / weak-penetration areas

A PIN may be derived in Analytics from a valid six-digit Indian PIN contained in an address fact. Only the derived PIN and provenance are retained in the Analytics snapshot; the full address is not copied merely for analytics.

### 4.5 Delivery Performance

This is a business-delivery measure, not an audit-process TAT.

- booking -> allocation days
- booking -> delivery days
- allocation -> delivery days
- promised/planned vs actual delivery variance
- on-time delivery percentage
- ageing buckets: <=7, 8-15, 16-30, >30 days (configurable later)
- average, median, P75 and P90 TAT
- dealer/outlet/model/variant comparison

If actual delivery timestamps are missing, the metric is **unavailable**, not zero.

### 4.6 Discounts & Pricing

- total discount value
- discount per deal / delivered vehicle
- discount by model / variant / dealer / outlet
- actual vs standard eligible discount
- above-eligible discount amount
- scheme/discount-key mix
- corporate / exchange / additional / scrappage / accessory benefit analysis
- discount as percentage of vehicle value when a reliable vehicle-value basis exists
- outlier deals

### 4.7 Finance

- finance penetration
- outright vs financed
- provider/lender share
- financed amount and average ticket
- financed amount relative to vehicle value when available
- dealer/outlet/model/variant comparison

### 4.8 Insurance

- insurance penetration
- dealer vs self insurance
- insurer share
- policy type
- premium value and average premium
- IDV and premium/IDV ratio when populated
- intermediary / MISP concentration
- dealer/outlet/model comparison

### 4.9 Insurance Add-ons

Separate from generic VAS. Target add-ons include, when extracted:

- Zero Depreciation
- Return to Invoice
- Engine / Gearbox Protect
- Consumables
- Key / Locks Cover
- Roadside Assistance
- other insurer-specific add-ons

Measures include add-on attach rate, journey penetration, insurer mix, model/outlet mix and add-on basket/combination analysis.

### 4.10 Accessories & VAS

- accessories penetration
- accessories value per deal
- accessory revenue/value by dealer/outlet/model
- EW penetration/value
- RSA penetration/value
- service-package penetration/value
- provider mix
- item/category/SKU analysis when line-level extraction becomes available

### 4.11 Trade-in

- trade-in penetration
- old vehicle make/model mix
- quoted vs actual value variance
- exchange-discount relationship
- handover -> payment days
- handover/payment -> resale days
- ageing of unresolved/unsold trade-ins

### 4.12 Payments

- receipt/payment value
- payment-mode mix
- payment count and average receipt value
- realised/pending status when populated
- timing of collection relative to booking/delivery
- short/excess/mismatch exceptions where a reliable commercial basis exists

### 4.13 Compliance

Compliance is a business dimension, not the entire Analytics product.

- journeys with findings %
- finding count per journey
- findings by rule/control/process area
- severity and status
- repeated failures
- dealer/outlet/model heatmaps
- missing-document rate
- conditional-document failures
- corporate/GST/exchange supporting-evidence failures
- repeat issue concentration

Do not invent a composite 0-100 compliance score until its formula and weighting are explicitly baselined.

### 4.14 Compliance Commercial Exposure

Where a rule can be reliably mapped to a financial amount:

- excess/unsupported discount value
- short/excess collection value
- unsupported benefit value
- payment exception value
- other rule-specific financial exposure

If causality cannot be established, label the amount as **commercial value associated with affected journeys**, not "loss" or "leakage".

### 4.15 Employee / Productivity

- salesperson booking/delivery contribution
- salesperson finance/insurance/VAS penetration
- PC/TL/PM activity/productivity
- findings per handled journey
- review turnaround
- employee/team/outlet comparisons

Employee measures must only be shown where actor/staff linkage is reliable.

### 4.16 Data Quality / Coverage

Data-quality information is diagnostic and must not dominate business pages.

For each major metric expose numerator/denominator coverage, for example:

- resolved model: 8 / 14 journeys
- actual delivery date: 0 / 5 delivery records
- customer PIN: n / 14 journeys
- insurance premium populated: n / insurance records
- insurance add-ons populated: n / insurance records

Unknown/missing data is never converted into zero business performance.

---

## 5. Phase 1 — use current data first

Phase 1 changes are confined to `verigence-analytics`.

The service will use the latest controlled snapshot to build useful business views from fields already present in Audit Core. It may also derive privacy-safe values from existing source facts through the Analytics read-only connection.

Immediately usable domains include:

- Project / Dealer / Outlet hierarchy
- journeys and bookings
- model / variant / colour snapshots
- commercial lines
- discounts
- payments
- finance
- insurance
- insurance add-ons already populated in `insurance_records.add_ons`
- journey add-ons such as Accessories/RSA/EW where present
- trade-in
- vehicle allocation and registration records once included in the Analytics snapshot
- deliveries
- audit findings
- document requirements / assessments
- staff/workflow activity

### Phase 1 derived geography rule

Analytics may derive `customer_pincode` from current non-superseded Audit Core evidence facts containing an address. The derivation:

1. reads Audit Core through the existing Analytics read-only source connection;
2. recognises a bounded six-digit Indian PIN beginning 1-9;
3. stores only `journey_id`, derived PIN, source field/evidence reference and derivation method in Analytics snapshot rows;
4. does **not** copy the full source address into the Analytics snapshot for this purpose.

---

## 6. Progressive source enrichment — later phases

Analytics must first expose the business value and the data gap. DI/Audit Core are enhanced later only when required and explicitly approved.

Likely future source improvements include:

- customer address / PIN as a first-class canonical field
- expected delivery date consistency
- actual delivery timestamp coverage
- vehicle allocation timestamps
- salesperson/staff linkage
- richer booking-form commercial breakup
- insurance IDV/policy/add-on coverage
- accessory invoice item/SKU/category/quantity/value extraction
- reliable vehicle-value basis for discount percentage
- additional lead/deal-source fields where available

No Phase 1 implementation may silently change another repository to close these gaps.

---

## 7. Presentation / interpretation rules

1. **Business question first.** Do not expose raw table counts as the primary report.
2. **Use the appropriate denominator.** Example: insurance add-on attach rate may be measured against insurance policies; journey penetration is measured against journeys. Return both when useful.
3. **Coverage accompanies every incomplete metric.**
4. **No fabricated zeroes.** `null/unavailable` is valid and preferable to a misleading `0`.
5. **No invented composite scores.**
6. **No PII-heavy analytics.** Aggregate geography; do not expose customer addresses, IDs, phone numbers or policy numbers in business reports unless a separately approved drill-down requires them.
7. **Dealer/outlet/model drill-down is standard.** A project-level number alone is insufficient where a business hierarchy exists.
8. **Historical trends require historical snapshots.** Do not manufacture MoM trends from a single current snapshot.
9. **Audit findings remain traceable**, but Analytics is broader than audit/compliance.
10. **Definitions are stable.** A metric name must have one meaning across every report.

---

## 8. Implementation order

1. Canonical current-snapshot business intelligence endpoint/mart.
2. Data coverage and explicit unavailable-state handling.
3. Sales/product/dealer/outlet measures.
4. Discounts/pricing.
5. Insurance and insurance add-ons.
6. Accessories/VAS.
7. Delivery business TAT.
8. Compliance concentration and commercial association.
9. Privacy-safe customer PIN derivation from existing address facts.
10. Historical trend layer as scheduled snapshots accumulate.
11. Source-enrichment backlog for DI/Audit Core, only after separate approval.

---

## 9. Governance

This document is the TO-BE Business Analytics baseline. Current sample-data limitations do not remove a report or metric from this target. A target may be changed only by an explicit versioned update to this baseline.

Phase 1 implementation is intentionally restricted to the `verigence-analytics` repository.