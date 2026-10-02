# Verigence Analytics — Deep Review, Dump Script Changes & Dealer 360 Dashboard Plan

> **Scope:** `verigence-analytics` + `verigence-audit-core` — no code changes made
> **Approach:** Grounded in exact JSONB field paths, table names, and column names from both repositories
> **Audience:** Technical decision-makers and the dealer-facing dashboard product

---

## PART A — DO THE DUMP SCRIPTS NEED CHANGING?

### Short Answer: Yes — 3 additions to `SOURCE_TABLES`, 1 new derived query, and 3 new JSONB field paths

The current `dump.py` copies rows wholesale via `to_jsonb(t)`. This means any column added to an existing table (e.g. `insurance_source` on `insurance_records`) is **automatically captured** the next time a dump runs — no code change required for existing tables.

However, the following are **not yet in `SOURCE_TABLES`** and require explicit additions:

---

### A.1 — New Tables to Add to `SOURCE_TABLES`

| Table | Primary Key | Why Needed | Which Analytics Reports Unlock |
|-------|------------|------------|-------------------------------|
| `p2_management_referrals` | `journey_id` (composite with tenant_id) | Management Referral discount — new 0138 | MR discount reporting, buffer utilisation |
| `dealer_discount_grid_versions` | `grid_version_id` | Grid version metadata (effective dates) | Grid compliance reporting |
| `dealer_discount_grid_rows` | `grid_row_id` | Per-model buffer, OD cap, booking protection, OOT amounts | Buffer vs actual, model-level compliance |

**Exact lines to add in `dump.py` `SOURCE_TABLES` tuple:**
```python
SourceTable("p2_management_referrals", "journey_id"),       # ADD — migration 0138
SourceTable("dealer_discount_grid_versions", "grid_version_id"),  # ADD — migration 0137
SourceTable("dealer_discount_grid_rows", "grid_row_id"),    # ADD — migration 0137
```

> **Note on `p2_management_referrals`:** The primary key is a composite `(tenant_id, journey_id)`. The `source_pk` stored in `snapshot_rows` will be the `journey_id` cast as text — consistent with how other journey-keyed tables (e.g. `bookings`, `deliveries`) are handled.

---

### A.2 — Existing Tables: New Columns Already Auto-Captured by `to_jsonb(t)`

The following column additions land automatically because `to_jsonb(t)` serialises the whole row:

| Table | New Column(s) | Migration | Field in JSONB |
|-------|--------------|-----------|----------------|
| `insurance_records` | `insurance_source`, `insurance_source_set_by_actor_id`, `insurance_source_set_at_utc` | 0135 | `row_data->>'insurance_source'` |
| `price_list_items` | `price_since` | 0136 | `row_data->>'price_since'` |
| `p2_document_queue` | `display_name` | 0134 | `row_data->>'display_name'` (minor, not analytics-critical) |

**No change required to dump.py for these.** The next dump after migration deployment captures them automatically.

---

### A.3 — New Derived Tables to Consider

The existing `derived_customer_geography` pattern (extracting PINs from `evidence_facts`) can be extended to derive two new lookup tables that enrich analytics without copying PII:

#### Derived: `derived_mr_discount` (from `p2_management_referrals`)
Once `p2_management_referrals` is in `SOURCE_TABLES`, analytics queries can read `opted`, `amount`, `reason`, `set_by_role` directly from the JSONB snapshot. No separate derived table needed.

#### Derived: `derived_grid_for_model` (join of `dealer_discount_grid_rows` + `product_models`)
The grid rows reference `model_id` (nullable). To report grid compliance per journey, analytics needs to join `dealer_discount_grid_rows.model_id` → `journey_products.model_name_snapshot`. This join can be done in the analytics query itself since both tables will be in the snapshot — no new derived table needed.

**Conclusion:** No new derived tables required beyond the three `SOURCE_TABLES` additions.

---

### A.4 — Master Tables: `price_list_items` and `discount_scheme_benefits`

These tables are **not currently in `SOURCE_TABLES`** but are needed for:
- Per-component master-vs-actual analysis
- M&M vs dealer contribution split
- Entitlement-based discount funnel

| Table | Primary Key | Size Consideration | Priority |
|-------|------------|-------------------|---------|
| `price_list_items` | `price_list_item_id` | ~10 SKUs × 10 components × N versions — small | P3 |
| `discount_scheme_benefits` (or `discount_scheme_eligibility`) | varies | Per scheme version × model/variant count — moderate | P3 |

These are **Priority 3** additions and are not required for the dealer dashboard MVP (P1 / P2 charts). Adding them now future-proofs the M&M vs dealer split report.

---

### A.5 — Summary: Dump Script Change Checklist

| Change | Type | Priority | How |
|--------|------|---------|-----|
| Add `p2_management_referrals` to SOURCE_TABLES | New table | **P1** | 1-line addition in dump.py |
| Add `dealer_discount_grid_versions` to SOURCE_TABLES | New table | **P1** | 1-line addition in dump.py |
| Add `dealer_discount_grid_rows` to SOURCE_TABLES | New table | **P1** | 1-line addition in dump.py |
| `insurance_source` field on `insurance_records` | Auto — no action | P1 | Already captured by `to_jsonb(t)` |
| `price_since` field on `price_list_items` | Auto — no action | P3 | Already captured by `to_jsonb(t)` |
| Add `price_list_items` to SOURCE_TABLES | New table | P3 | 1-line addition in dump.py |
| Add `discount_scheme_benefits` to SOURCE_TABLES | New table | P3 | 1-line addition in dump.py |

---

## PART B — EXISTING ANALYTICS JSONB FIELD GAPS

Below are fields already in snapshot but **not yet extracted** in any analytics query — free wins requiring only endpoint changes, not dump changes.

### B.1 — `insurance_records`: `insurance_source` field

**Current extraction in `_DEAL_FACTS_CTE`:**
```sql
NULLIF(row_data->>'self_insurance_flag','')::boolean AS self_insurance_flag
```

**Missing extraction:**
```sql
NULLIF(row_data->>'insurance_source','') AS insurance_source   -- 'INHOUSE' | 'SELF' | NULL
```

**Impact:** The `insurance_penetration_pct` in `business_overview` and all scorecard rows counts ALL insurance records. SELF-insured journeys inflate dealer's penetration metric. Without `insurance_source`, the metric is misleading.

---

### B.2 — `discount_applications`: `eligibility_result` and `details` fields

**Current extraction:**
```sql
sum(NULLIF(row_data->>'actual_discount_amount','')::numeric) AS actual_discount_amount,
sum(NULLIF(row_data->>'standard_eligible_amount','')::numeric) AS eligible_discount_amount
```

**Missing:**
```sql
row_data->>'eligibility_result'          -- ELIGIBLE | ELIGIBLE_UNCLAIMED | NOT_ELIGIBLE
row_data->>'discount_key'                -- Already used in reports.py discounts() but NOT in _DEAL_FACTS_CTE
row_data->'details'->>'schemeCategory'  -- CONSUMER | EXCHANGE | SCRAPPAGE | WELCOME | CORPORATE
row_data->'details'->>'benefitKey'      -- CASH_DISCOUNT | EXCHANGE_BONUS | MANAGEMENT_REFERRAL | etc.
```

**Impact:** The `ELIGIBLE_UNCLAIMED` result identifies journeys where a customer was entitled to a benefit but the dealer did not give it — a compliance signal and a sales coaching signal. Currently invisible.

---

### B.3 — `audit_findings`: Time-windowed rule keys not grouped

The `top_rules` query in `compliance_intelligence` already returns `rule_key`. But there is no grouping of the 22 rule keys into families. The analytics endpoint should compute:

| Rule Family | Rule Keys |
|-------------|-----------|
| Discount Compliance | `EXCESS_DISCOUNT`, `DEAL_UNDERCHARGED` |
| Statutory | `TCS_SHORT`, `CASH_ABOVE_LIMIT` |
| Settlement | `DELIVERED_ON_SHORT_PAYMENT`, `PAYMENT_AFTER_DELIVERY_WITHIN_GRACE`, `PAYMENT_AFTER_DELIVERY_BEYOND_GRACE` |
| Finance | `DO_PAYMENT_NOT_RECEIVED`, `DO_SHORT_PAYMENT` |
| Trade-In | `TRADE_IN_NOT_RESOLD`, `TRADE_IN_SOLD_AT_LOSS` |
| Process | `DELIVERY_NOT_COMPLETED_IN_TIME`, `POST_DELIVERY_REFUND`, `NDC_NOT_SIGNED` |
| Cash/AML | `CASH_ABOVE_LIMIT`, `THIRD_PARTY_PAYMENT_UNDECLARED`, `CASH_NOT_INTIMATED` |

---

### B.4 — `bookings`: `booking_date` not used in delivery month filter

All delivery TAT queries in `delivery_business` use `actual_delivered_at` (timestamptz from `deliveries`). There is no month-bucketing of deliveries. Adding `date_trunc('month', actual_delivered_at)` to the group-by in `delivery_business` would produce "deliveries completed per month" — a zero-cost addition to an existing query.

---

### B.5 — `payments`: `payment_stage` field not extracted

```sql
NULLIF(row_data->>'payment_stage','') AS payment_stage   -- 'BOOKING' | 'DELIVERY'
```

Currently the `payments` endpoint shows `payment_method_code` only. The `payment_stage` split (booking advance vs delivery balance) is valuable for understanding cash-flow patterns and for identifying `DELIVERED_ON_SHORT_PAYMENT` risk.

---

### B.6 — `finance_records`: `loan_disbursement_amount` and `do_reference` not extracted

```sql
NULLIF(row_data->>'loan_disbursement_amount','')::numeric AS loan_disbursement_amount
NULLIF(row_data->>'do_reference','')  AS do_reference
```

The difference between `financed_amount` (loan sanctioned) and `loan_disbursement_amount` (DO actually received) is the basis for `DO_PAYMENT_NOT_RECEIVED` and `DO_SHORT_PAYMENT` findings. Analytics should surface the aggregate gap.

---

## PART C — COMPREHENSIVE DASHBOARD & CHART SPECIFICATION

This is a 360-degree view from the audit data. Every metric below is **derivable from data already in audit-core** — we are not inventing new collection. We are surfacing what the audit process already sees.

---

### C.1 — DEALER OVERVIEW DASHBOARD

**Audience:** Dealer Principal / Dealer Admin
**Scope:** Filtered to their `dealer_id`, all outlets

---

#### Chart 1: Deal Stage Funnel (Bar or Funnel)
**What it shows:** How many deals are at each stage right now.

| Stage | Source | How Counted |
|-------|--------|-------------|
| Booking Taken | `journeys` count | journey created |
| Booking Complete | `bookings` with `booking_confirmation_date` not null | |
| Delivery Started | `deliveries` row exists | |
| Vehicle Allocated | `vehicle_records.allocated_at_utc` not null | |
| Delivery Completed | `deliveries.actual_delivered_at` not null | |

**Why it matters to the dealer:** Tells them exactly where deals are stuck. If 40 deals are in "Vehicle Allocated" but not "Delivery Completed", something is blocking the last mile.

**Chart type:** Horizontal funnel / stacked bar by outlet

---

#### Chart 2: Highest Selling Model per Outlet (Bar Chart — Grouped)
**What it shows:** Booking count by (outlet_name, model_name), top 5 models per outlet

**Data source:** `_DEAL_FACTS_CTE` fields: `outlet_name`, `model_name`, COUNT(journey_id)

**Sorting:** Descending by booking count per outlet, limited to top 5 models

**Variants:**
- Same chart filtered to `actual_delivered_at IS NOT NULL` → "Highest Delivered Models"
- Same chart with `booking_date` month filter → "Best Sellers This Month"

**Why it matters:** The dealer sees which models drive volume per outlet and which outlets are underperforming specific models. An auditor sees if a high-volume model has disproportionate findings.

---

#### Chart 3: Deliveries Completed Per Month (Column Chart)
**What it shows:** Count of `actual_delivered_at` records grouped by month

**Data source:** `deliveries.actual_delivered_at` → `date_trunc('month', actual_delivered_at::date)`

**Fields needed:**
```sql
date_trunc('month', NULLIF(row_data->>'actual_delivered_at','')::date) AS delivery_month,
count(*) AS deliveries_completed
```

**Drill-down:** By outlet, by model

**Why it matters (audit angle):** A spike in deliveries before month-end is a classic sign of booking-delivery mismatch (vehicles pre-dispatched without paperwork). This chart is an audit red flag detector, not just a sales chart.

---

#### Chart 4: Booking-to-Delivery Time Distribution (Box Plot / Range Bar)
**What it shows:** For each model or outlet: min, P25, median, P75, P90, max days from booking to delivery

**Data source:** `bookings.booking_date` and `deliveries.actual_delivered_at`

```sql
booking_date::date - actual_delivered_at::date AS tat_days
```

**Why this is differentiating for a dealer:**
- No DMS shows the P75 and P90 — they only show average
- An auditor uses P90 to identify outlier deals (is the 90th percentile deal really 120 days? Why?)
- A dealer Principal uses median to benchmark outlets

**Visual:** Box plots per outlet/model with min-max whiskers

---

#### Chart 5: Pincode Demand Map (Bubble Map or Table)
**What it shows:** Booking count and delivery count by customer 6-digit PIN code

**Data source:** `derived_customer_geography.customer_pincode` + deal facts

**Fields per PIN:**
- journey_count (bookings originated)
- actual_deliveries
- top model (most booked model from that PIN)
- finance_penetration_pct
- insurance_penetration_pct (INHOUSE only — after insurance_source fix)
- avg_discount

**Why it matters to the dealer (and to the auditor):**
- Dealer: which PINs are their core catchment vs outlier? High-volume PINs that are not geographically theirs is an out-of-territory signal.
- Auditor: `out_of_territory_amount` from dealer_discount_grid flags if that PIN is outside the dealer's assigned territory.

---

### C.2 — DISCOUNT INTELLIGENCE DASHBOARD

**Audience:** Dealer Principal, Area Manager (Audit view)
**The key insight:** A dealer sees their own deal as compliant. The analytics platform shows whether the combination of all discounts on a deal exceeded what OEM mandated — a view no individual deal review gives.

---

#### Chart 6: Discount Waterfall by Type (Stacked Bar per Model)
**What it shows:** For each model, the total discount given broken into 13 discount_key buckets

| Discount Key | Source Field | Expected Typical Amount |
|-------------|-------------|------------------------|
| CASH_DISCOUNT | `sales_discount_amount` | OEM published |
| EXCHANGE_BONUS | `exchange_discount_amount` | Exchange scheme |
| SCRAPPAGE_BONUS_DEALER | `scrappage_discount_amount` | Scrappage scheme |
| CORPORATE_PRIVILEGE | `corporate_discount_amount` | By privilege category |
| WELCOME_BONUS | `loyalty_discount_amount` | Loyalty scheme |
| INSURANCE | `inhouse_insurance_discount_amount` | Insurance incentive |
| MANAGEMENT_REFERRAL | `mr_discount_amount` | TL-granted (NEW 0138) |
| ACCESSORIES_KIT | `free_accessory_discount_amount` | Kit-based |
| OTHER_SCHEME | `other_discount_amount` | Residual |

**Data source:** `discount_applications` grouped by `discount_key`, filtered by `model_name`

**Overlay:** A horizontal line at `standard_eligible_amount` per model (the OEM entitlement ceiling)

**Why this is the #1 differentiating chart for a dealer:**
The dealer can see, for the first time, where their discount mix is OEM-funded vs dealer-self-funded vs TL-authorised (MR). No DMS or OEM portal shows this breakdown.

---

#### Chart 7: Standard vs Actual Discount — Per Model (Diverging Bar)
**What it shows:** For each model, the gap between OEM-entitled discount and actual discount given

```
Standard Entitled → [XXXXXXXXXXXXXXXX]
Actual Given      → [XXXXXXXXXXXXXXXXXXXX] ← above the line = excess = EXCESS_DISCOUNT finding risk
                                            ← below the line = unclaimed = ELIGIBLE_UNCLAIMED
```

**Data source:** `discount_applications.standard_eligible_amount` vs `actual_discount_amount`

**Colour coding:**
- Green: actual ≤ standard (compliant)
- Amber: actual > standard by < 10% (borderline)
- Red: actual > standard by ≥ 10% (EXCESS_DISCOUNT finding likely)

**Audit angle:** Any bar in Red already has or will have an `EXCESS_DISCOUNT` audit_finding. Analytics shows the exposure before the finding is even raised.

---

#### Chart 8: Management Referral (MR) Discount Tracker (Table + Summary Card)
**NEW — requires `p2_management_referrals` in dump**

**Summary Cards:**
- Total journeys with MR opted: `count(*) WHERE opted = true`
- Total MR amount granted: `sum(amount) WHERE opted = true`
- Average MR per opted journey
- MR by role (TL breakdown)

**Detail Table:**
```
Journey ID | Model | Outlet | Amount | Reason (summarised) | Set By Role | Set At
```

**Why it matters (audit angle):**
MR is the only discount with no OEM standard — it is purely TL discretion. Without this chart, MR is invisible to the dealer Principal and to audit. With it, patterns emerge: is one TL granting MR 3x more than others? Are MR discounts clustering on specific models?

---

#### Chart 9: Dealer Discount Grid — Buffer Utilisation per Model (Gauge Chart)
**NEW — requires `dealer_discount_grid_rows` in dump**

**For each model:**
```
Agreed Buffer: ₹45,000
Average Buffer Discount Given: ₹38,000
Utilisation: 84%
```

**Data source:**
- `dealer_discount_grid_rows.agreed_buffer_amount` — The ceiling
- `discount_applications WHERE discount_key = 'MANAGEMENT_REFERRAL' OR discount_key = 'OTHER_SCHEME'` — The actuals that consume buffer

**Visual:** Gauge per model, with a warning threshold at 80% and a breach indicator at 100%

**Audit angle:** Consistent ≥95% buffer utilisation on one model is a signal that the buffer ceiling should be reviewed, or that a pattern of near-ceiling discounts is being awarded.

---

### C.3 — COMPLIANCE EXPOSURE DASHBOARD

**Audience:** Dealer Principal, Area Audit Manager
**The framing:** Not "how many violations" — but "what is the financial exposure attached to non-compliant deals?"

---

#### Chart 10: Open Findings by Rule Family (Donut Chart)
**What it shows:** Count of open audit findings grouped by the 7 rule families defined in B.3

| Family | Representative Rules | What It Means |
|--------|---------------------|---------------|
| Discount Compliance | EXCESS_DISCOUNT, DEAL_UNDERCHARGED | Pricing risk |
| Statutory | TCS_SHORT, CASH_ABOVE_LIMIT | Regulatory risk |
| Settlement | DELIVERED_ON_SHORT_PAYMENT, PAYMENT_AFTER_DELIVERY | Cash flow risk |
| Finance | DO_PAYMENT_NOT_RECEIVED, DO_SHORT_PAYMENT | Financer relationship risk |
| Trade-In | TRADE_IN_NOT_RESOLD, TRADE_IN_SOLD_AT_LOSS | Asset risk |
| Process | DELIVERY_NOT_COMPLETED_IN_TIME, NDC_NOT_SIGNED | Operational risk |
| Cash/AML | THIRD_PARTY_PAYMENT_UNDECLARED, CASH_NOT_INTIMATED | AML risk |

**Drill-through:** Click any segment → list of journeys with that family of findings

---

#### Chart 11: Commercial Exposure on Findings Journeys (Stacked Bar)
**What it shows:** Total payment amount and discount amount on journeys that have open findings, split by finding category

**Data source:**
- `audit_findings` (open only) → `journey_id` list
- For those journey_ids: `payment_amount`, `actual_discount_amount` from `_DEAL_FACTS_CTE`

**Why this is powerful (audit angle):**
A dealer with 10 EXCESS_DISCOUNT findings on journeys totalling ₹42 lakhs in payments understands the financial stake. Presenting findings as counts alone never achieves what amount-on-affected-journeys achieves.

---

#### Chart 12: Time-Window Compliance Tracker (4-Metric Scorecard)
**NEW — requires rule_key grouping by family**

| Metric | Rule Key | How Computed | Target |
|--------|---------|-------------|--------|
| Settlement Grace Compliance % | `PAYMENT_AFTER_DELIVERY_BEYOND_GRACE` | (delivered journeys - journeys with this finding) / delivered journeys | > 95% |
| Finance DO Receipt % | `DO_PAYMENT_NOT_RECEIVED` | (financed journeys - open DO findings) / financed journeys | > 90% |
| Trade-In Resale % | `TRADE_IN_NOT_RESOLD` | (trade-in journeys - open resale findings) / trade-in journeys | > 85% |
| Delivery Completion % | `DELIVERY_NOT_COMPLETED_IN_TIME` | (delivery journeys - open completion findings) / delivery journeys | > 95% |

**Why it matters:**
These four metrics tell the dealer what % of their operational commitments are being honoured on time. Unlike sales KPIs, these are legally and commercially binding. An auditor uses these to rate dealer operational discipline.

---

#### Chart 13: TCS Compliance Card (Summary + Journey List)
**NEW metric — `TCS_SHORT` rule key**

**Summary Card:**
- Journeys with TCS_SHORT finding: N
- Statutory TCS threshold: ₹10,00,000 (configurable)
- TCS rate: 1%
- Estimated TCS shortfall: (sum of vehicle amounts on TCS_SHORT journeys × 1%)

**Why it matters:**
TCS (Tax Collected at Source) is a statutory obligation under the Income Tax Act. A dealer unaware of their TCS shortfall is exposed to notices. The analytics platform surfaces this as a proactive advisory — a genuine value-add.

---

#### Chart 14: Cash Receipt Compliance (CASH_ABOVE_LIMIT)
**What it shows:** Number of receipts exceeding ₹2,00,000 in a single transaction (Section 269ST, Income Tax Act)

**Data source:**
- `audit_findings WHERE rule_key = 'CASH_ABOVE_LIMIT'`
- OR: `payments WHERE amount > 200000 AND payment_method_code IN ('CASH',...)`

**Display:** Count of affected receipts, total cash amounts, list of journeys

**Why it matters:**
This is a pure regulatory compliance metric. A dealer with 5 CASH_ABOVE_LIMIT findings is at real legal risk. No DMS surfaces this. Verigence analytics can be the early warning system.

---

### C.4 — ACCESSORIES & VAS PERFORMANCE DASHBOARD

**Audience:** Sales Manager, Dealer Principal
**Audit angle:** Accessories billed but not fitted is an audit violation (`ACCESSORY_FITTED_UNBILLED` and `ACCESSORIES_FITTED_UNCONFIRMED`). This dashboard shows both the commercial and compliance dimension.

---

#### Chart 15: Accessories Value per Model (Grouped Bar)
**What it shows:** Total accessories value by model across all outlets

**Data source:**
```sql
FROM journey_addons WHERE addon_type_code IN ('ACCESSORY','ACCESSORIES','ACCESSORIES_TOTAL')
GROUP BY model_name (via join to journey_products)
```

**Fields:**
- `model_name`
- `journey_count` (journeys with accessories)
- `accessories_value` (sum of `actual_amount`)
- `avg_accessories_per_journey`
- `attach_rate_pct` (journeys with accessories / total journeys for that model)

**Variant:** Same chart by outlet, by month

---

#### Chart 16: VAS Attach Rate Heatmap (Model × VAS Type)
**What it shows:** A matrix of: rows = models, columns = VAS types (EW / RSA / Accessories / Service Package), cells = attach rate %

| Model | EW | RSA | Accessories | Service Pkg |
|-------|----|-----|------------|------------|
| Thar | 45% | 62% | 78% | 12% |
| Scorpio-N | 38% | 71% | 82% | 8% |
| XUV700 | 55% | 68% | 90% | 22% |

**Data source:**
- `journey_addons.addon_type_code` for EW / RSA
- Add `service_package_amount` from `booking_form_review_values` for service package
- `green_tax_amount` can be a separate column

**Audit angle:** A model with 90% accessories attach rate is normal for some models. But if the compliance endpoint shows ACCESSORY_FITTED_UNBILLED findings clustering on the same high-attach model, that is an audit red flag — the accessories are being billed but not fitted.

---

#### Chart 17: Accessories Compliance Overlay (Combined Chart)
**What it shows:** For each model — accessories attach rate (bar) overlaid with ACCESSORIES_FITTED_UNCONFIRMED / ACCESSORY_FITTED_UNBILLED finding rate (line)

**Data source:**
- Left axis: `accessories_value / journey_count` from `journey_addons`
- Right axis: `count(audit_findings WHERE rule_key IN ('ACCESSORY_FITTED_UNBILLED', 'ACCESSORIES_FITTED_UNCONFIRMED'))` / journey_count

**Why it is an audit-first chart:** High attach rate + high finding rate = systematic accessories billing without delivery. This chart surfaces that pattern in one view.

---

### C.5 — FINANCE & INSURANCE PERFORMANCE DASHBOARD

---

#### Chart 18: Finance Penetration by Outlet × Provider (Heatmap)
**What it shows:** Finance penetration % per outlet and per finance provider

**Data source:**
- `finance_records.provider_name` + `journey_products.model_name`
- Finance penetration = journeys with `finance_record` / total journeys

**Audit angle:** `DO_PAYMENT_NOT_RECEIVED` findings cluster on specific providers. This chart shows which provider–outlet combinations are causing the most DO issues.

---

#### Chart 19: Insurance Source Split (INHOUSE vs SELF) — Donut + Bar
**NEW — requires `insurance_source` field extraction**

**Donut:** INHOUSE % vs SELF % of all insurance records

**Bar by Outlet:**
```
Outlet A: [INHOUSE: 78%] [SELF: 22%]
Outlet B: [INHOUSE: 45%] [SELF: 55%]  ← unusually high SELF
```

**Why it matters:**
INHOUSE insurance = dealer revenue and on-road price component.  
SELF insurance = customer's choice, not dealer revenue.  
An outlet with 55% SELF insurance may indicate the dealer is not actively promoting in-house insurance, or is steering customers to self-arrange (which can indicate an incentive misalignment).

From the audit side: `insurance_od_percent` from the dealer discount grid caps how much OD percentage the dealer can quote. The analytics platform can check if any INHOUSE insurance records have OD% above the grid cap.

---

#### Chart 20: Insurance OD vs Grid Cap Compliance (NEW)
**What it shows:** For journeys with INHOUSE insurance, the actual OD % vs the model's grid cap

**Data source:**
- `insurance_records.actual_premium_amount` (as a % of EX_SHOWROOM for that SKU)
- `dealer_discount_grid_rows.insurance_od_percent` (the allowed maximum)

**Display:** Scatter plot: x = model, y = OD %, horizontal line = grid cap. Points above the line = compliance breach.

**Audit angle:** This chart is a direct compliance check on insurance pricing — something no audit tool currently surfaces in visual form.

---

### C.6 — TRADE-IN PERFORMANCE DASHBOARD

---

#### Chart 21: Trade-In Valuation vs Actual Realisation (Scatter Plot)
**What it shows:** Per trade-in case: quoted_value (x-axis) vs actual_value (y-axis)

**Data source:** `trade_in_cases.quoted_value` vs `trade_in_cases.actual_value`

**Points below the diagonal line** = sold below valuation → `TRADE_IN_SOLD_AT_LOSS` finding risk

**Why it matters:**
A cluster of points significantly below the diagonal suggests systematic under-valuation at quote time (quote high to incentivise exchange, sell low), which is a financial compliance issue.

---

#### Chart 22: Trade-In Resale TAT (Days to Resell)
**What it shows:** Distribution of days from `trade_in_handover_at` to `trade_in_resale_at`

**Data source:** `trade_in_cases.handover_at_utc` and `trade_in_resale_at_utc`

**Metric:** `resale_at_utc - handover_at_utc` in days

**Overlay:** Vertical line at 90 days (P2_TRADE_IN_RESALE_DAYS default) — the audit threshold for `TRADE_IN_NOT_RESOLD` finding

**Why it matters:**
Vehicles not resold within 90 days tie up dealer capital and trigger audit findings. This chart shows the distribution so the dealer Principal can see how many vehicles are approaching the threshold.

---

### C.7 — PAYMENT HEALTH DASHBOARD

---

#### Chart 23: Payment Stage Split — Booking vs Delivery Payments (Stacked Bar)
**NEW — requires `payment_stage` field extraction**

**What it shows:** For each month: total booking-stage payments vs total delivery-stage payments

**Data source:** `payments.payment_stage` + `payments.amount` + `payments.receipt_date`

**Audit angle:** A large jump in delivery-stage payments in a single month with few corresponding delivery-stage findings suggests an aggressive month-end delivery push — a common audit risk.

---

#### Chart 24: Bank Statement Match Rate (Gauge / Table)
**What it shows:** % of payment receipts that have a matched bank statement credit line

**Data source:** `payment_bank_matches.match_status`

```
Matched: 78%
Unmatched: 15%
No Statement: 7%
```

**Why it matters (audit angle):**
Unmatched payments are the primary indicator of cash handling irregularities. A dealer with 15% unmatched payments should prompt immediate inquiry. This is an audit-first metric that a dealer Principal can use to self-govern.

---

#### Chart 25: Delivered on Short Payment Exposure (Summary Card + Journey List)
**What it shows:** Journeys where the vehicle was delivered but full payment was not received

**Data source:** `audit_findings WHERE rule_key = 'DELIVERED_ON_SHORT_PAYMENT'`

**Summary Card:**
- Open DELIVERED_ON_SHORT_PAYMENT findings: N
- Total outstanding amount: ₹X
- Average days since delivery: D days

**Why it matters:**
This is money owed to the dealer that has not been collected. Presenting it as a financial exposure amount (not a finding count) immediately makes the dealer Principal act.

---

### C.8 — DELIVERY PERFORMANCE DASHBOARD

---

#### Chart 26: Delivery Performance Scorecard by Outlet (Table with RAG status)
**What it shows:** Per outlet — key delivery metrics with Red/Amber/Green status

| Outlet | Deliveries (MTD) | On-Time % | Avg TAT Days | Median TAT | P90 TAT | RAG |
|--------|-----------------|-----------|-------------|-----------|---------|-----|
| Outlet A | 12 | 91% | 14 | 12 | 28 | 🟢 |
| Outlet B | 8 | 62% | 22 | 19 | 45 | 🔴 |
| Outlet C | 5 | 80% | 18 | 16 | 35 | 🟡 |

**RAG thresholds (configurable per deployment):**
- Green: on-time % > 85%, P90 < 30 days
- Amber: on-time % 70–85% OR P90 30–45 days
- Red: on-time % < 70% OR P90 > 45 days

---

#### Chart 27: Monthly Delivery Trend + Pending Delivery Queue (Combo Chart)
**What it shows:**
- Left axis (bar): deliveries completed per month
- Right axis (line): open deliveries pending at end of each month

**Data source:**
- `deliveries.actual_delivered_at` grouped by month → completed count
- `journeys` without `actual_delivered_at` at snapshot date → pending count

**Why it matters (audit angle):**
A growing "pending" line while "completed" stays flat indicates inventory or operational blockage. A sudden spike in completions followed by a spike in `DELIVERY_NOT_COMPLETED_IN_TIME` findings is the audit red flag.

---

## PART D — DEALER DASHBOARD ACCESS MODEL

### D.1 — Permission Scoping
The existing `security_authorization.py` uses `audit.analytics.read` as the permission key checked against the Security service. No architectural change is needed — just two new permission keys:

| Permission Key | Who Holds It | Data Scope |
|---------------|-------------|-----------|
| `audit.analytics.read` | OEM / Platform Admins | All dealers, all outlets (existing) |
| `audit.analytics.dealer.read` | Dealer Principals | Their `dealer_id` only |
| `audit.analytics.outlet.read` | Outlet Managers | Their `outlet_id` only |

### D.2 — Endpoint Filtering
All analytics queries already use `tenant_id` scoping. Adding a `dealer_id` filter parameter to BI endpoints is a straightforward addition — the `_DEAL_FACTS_CTE` already joins `dealers` and `outlets` with `dealer_id` and `outlet_id` columns.

### D.3 — What the Dealer Sees vs What Audit Sees
| Feature | Dealer Dashboard | Audit / OEM Dashboard |
|---------|-----------------|----------------------|
| Own deals, own discounts | ✅ Full detail | ✅ Full detail |
| Cross-dealer comparison | ❌ Not shown | ✅ Full network scorecard |
| MR discount detail (reason, actor) | ✅ Their journeys | ✅ Cross-dealer |
| Audit finding detail (rule text) | ✅ Own findings | ✅ Cross-dealer |
| OEM master data (scheme entitlements) | View own entitlement | Full master admin |
| Customer PII | ❌ PIN codes only | ❌ PIN codes only |

---

## PART E — PRIORITY EXECUTION ORDER

### Phase 1 — Dump Changes + Quick-Win Field Extractions (No new endpoints)
1. Add 3 lines to `SOURCE_TABLES` in `dump.py`
2. Extract `insurance_source` in insurance CTE
3. Extract `payment_stage` in payments CTE
4. Extract `discount_key` into the per-journey discount breakdown
5. Add `delivery_month` bucket to delivery TAT query
6. Run fresh dump after migration 0137 + 0138 deployment

### Phase 2 — New Analytics Endpoints (Priority Dashboard Charts)
7. MR discount endpoint (`/business-intelligence/management-referral`)
8. Grid compliance endpoint (`/business-intelligence/grid-compliance`)
9. Insurance source split in existing insurance endpoint
10. Time-window breach rate aggregations in compliance endpoint
11. Statutory compliance cards (TCS, Cash, 269ST)

### Phase 3 — Dealer-Scoped Access
12. `dealer_id` filter parameter on BI endpoints
13. New permission key `audit.analytics.dealer.read`
14. Security service permission registration

### Phase 4 — Master-Level Intelligence
15. Add `price_list_items` to SOURCE_TABLES
16. Add `discount_scheme_benefits` to SOURCE_TABLES
17. M&M vs dealer contribution split endpoint
18. Entitlement funnel (ELIGIBLE_UNCLAIMED analysis)

---

*This document was produced by reading every source file in `verigence-analytics` (dump.py, business_intelligence.py, reports.py, network_reports.py, executive_dashboard.py, project_dashboard.py) and `verigence-audit-core` (migrations 0133–0138, uc03_p2_journey360.py, uc03_sku_standard.py, uc03_deal_reconciliation.py, uc03_p2_audit_rules.py, uc03_booking_commercial_components.py). No code changes were made.*
