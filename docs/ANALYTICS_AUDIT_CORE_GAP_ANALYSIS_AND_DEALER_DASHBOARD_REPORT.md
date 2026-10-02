# Verigence Analytics — Audit Core Gap Analysis & Dealer Dashboard Report

> **Scope:** Verigence repositories only — `verigence-analytics` and `verigence-audit-core`
> **Purpose:** What analytics exist today, what new audit-core data can be incorporated, what is not recommended, and how to build a dealer-differentiating dashboard
> **Date reviewed:** Post audit-core sprint (migrations 0133–0138)

---

## 1. Executive Summary

The `verigence-analytics` service is a mature, production-ready BI platform that currently reports against the original set of audit-core tables. The recent audit-core sprint (migrations 0133–0138) introduced **five structurally new data dimensions** — a fifth OEM master (Dealer Discount Grid), Management Referral discounts, insurance source classification, price-since dating, and extended commercial-line field coverage — none of which are yet reflected in analytics.

When these gaps are closed, the analytics platform will be able to deliver a **dealer-facing dashboard** that is meaningfully differentiated: it will show each dealer how their deals compare against OEM entitlements, flag where discounts exceed agreed buffers, surface compliance exposure by deal, and let dealers track their own salespeople's performance — information that no raw DMS or OEM portal provides in this combined form.

---

## 2. What Analytics Reports Today — Inventory

### 2.1 Core Deal Metrics ✅

| Metric | Endpoint | Notes |
|--------|----------|-------|
| Journey / booking counts | `/overview` | By status, outlet, model |
| Delivery counts & on-time % | `/business-intelligence/delivery` | Booking→delivery TAT, P75, P90 |
| Finance penetration % | `/finance` | Provider distribution, amounts |
| Insurance penetration % | `/insurance` | Insurer, premium totals, duplicate-agent detection |
| Trade-in participation | `/trade-in` | Quoted vs actual values |
| Accessories / EW / RSA | `/addons` | Attach rates by type and outlet |
| Payment methods & amounts | `/payments` | Method distribution, per-journey count |
| Customer geography | `/business-intelligence/geography` | 6-digit PIN heatmap, outlet catchment |
| Delivery TAT | `/business-intelligence/delivery` | avg / median / P75 / P90 |
| Data coverage metrics | `/business-intelligence/coverage` | Explicit numerator / denominator per dimension |

### 2.2 Discount Analytics ✅

| Metric | Endpoint | Notes |
|--------|----------|-------|
| Total actual discount by `discount_key` | `/discounts` | Keyed |
| Eligible vs actual discount comparison | `/business-intelligence/discounts` | Standard vs actual |
| Above-eligible discount identification | `/business-intelligence/discounts` | `above_eligible_amount` |
| Discount by model / outlet | `/business-intelligence/discounts` | Multi-dimension |
| Average discount per journey | `/business-intelligence/discounts` | Per journey, per discounted journey |
| Discount penetration % | `/business-intelligence/discounts` | Journeys with any discount |

### 2.3 Audit / Compliance Analytics ✅

| Metric | Endpoint | Notes |
|--------|----------|-------|
| Finding counts by rule / severity / status | `/business-intelligence/compliance` | Open vs closed |
| Finding rate % by outlet / model | `/business-intelligence/compliance` | Outlet scorecard |
| Missing-document flags | `/documents` | By document type |
| Top 50 rules by occurrence | `/business-intelligence/compliance` | Frequency ranking |
| Commercial value on affected journeys | `/business-intelligence/compliance` | Per rule |

### 2.4 Commercial Line Reporting ✅

| Metric | Endpoint | Notes |
|--------|----------|-------|
| Commercial line items by `component_key` | `/business-intelligence/commercial-components` | Standard vs actual amounts |
| Average component amount | `/business-intelligence/commercial-components` | Per journey with that component |

### 2.5 Network & Executive Views ✅

- Dealer / outlet scorecard with all penetration rates
- Executive dashboard (composite)
- Project dashboard (six combined views)

---

## 3. What Audit-Core Now Captures That Analytics Doesn't Report

This section maps every new audit-core capability (migrations 0133–0138 + recent source changes) to its analytics gap.

### 3.1 Management Referral (MR) Discount — Gap: Critical

**What audit-core now has:**  
Migration `0138` introduced `p2_management_referrals` — one row per journey with:  
`opted (bool)`, `amount`, `reason`, `set_by_actor_id`, `set_by_role`, `set_at_utc`

The `uc03_deal_reconciliation.py` maps `booking_mr_discount_amount → MANAGEMENT_REFERRAL` in `discount_applications`. The Journey360 Deal tab now shows this as a named line item. A `TL_MANAGEMENT_REFERRAL` task type controls who can grant it.

**What analytics currently reports:** Nothing. `MANAGEMENT_REFERRAL` does not appear in any analytics query, aggregation, or endpoint.

**Impact:**  
MR discount is a TL-authorised exception that is invisible to any senior management view today. It is also the single discount most likely to be misused — given it is human-authorised with no OEM-entitlement ceiling.

---

### 3.2 Dealer Discount Grid — Gap: Critical

**What audit-core now has:**  
Migration `0137` introduced the fifth OEM master: `dealer_discount_grid_versions` + `dealer_discount_grid_rows`, with four policy fields per model:

| Field | Meaning |
|-------|---------|
| `booking_protection_days` | Closure buffer window |
| `agreed_buffer_amount` | Maximum allowed buffer discount (₹) |
| `insurance_od_percent` | Maximum insurance OD % the dealer may claim |
| `out_of_territory_amount` | Premium for out-of-territory sales |

**What analytics currently reports:** None of these fields are in analytics schema or any report.

**Impact:**  
Without these analytics cannot answer: "Which deals exceeded the agreed buffer?", "Which models show consistent buffer over-use?", "How many deals involved out-of-territory additions?".

---

### 3.3 Insurance Source Classification — Gap: High

**What audit-core now has:**  
Migration `0135` added `insurance_source` (INHOUSE | SELF) to `insurance_records`. An `INHOUSE` insurance record means the dealer arranged it (counts toward on-road price); a `SELF` record means the customer brought their own (should not inflate dealer's insurance penetration or premium totals).

**What analytics currently reports:** Insurance penetration and premium totals treat all insurance records identically. INHOUSE vs SELF split is lost.

**Impact:**  
Insurance penetration % currently includes SELF-insured customers, inflating the metric. Premium totals include non-dealer revenue. Compliance checks that verify insurance OD % against the grid cap can't be reported at aggregate.

---

### 3.4 Per-Component Price-Since Dating — Gap: Medium

**What audit-core now has:**  
Migration `0136` added `price_since` to `price_list_items`. Each price component now carries the exact date its figure last changed, independent of the version effective date. The dump already copies `price_list_items` as a source table.

**What analytics currently reports:** Analytics has no price-list version or component-level pricing dimension. There is no comparison of what a component was billed vs. what the master says.

**Impact:**  
Analytics cannot answer: "How many deals were priced on a stale price list?", "What was the EX-showroom price for this model in March vs February?", "Which deals had a pricing-date override applied?"

---

### 3.5 Expanded Discount Field Set — Gap: High

**What audit-core now has (14 discount fields + 8 add-on fields in `booking_form_review_values`):**

New / previously absent fields now consistently captured:
- `mr_discount_amount` (Management Referral — new 0138)
- `buffer_discount_amount` (from dealer discount grid — new 0137)
- `oem_referral_discount_amount` (OEM referral — distinct from corporate)
- `scrappage_discount_amount` (column backfill — 0105 fix)
- `essential_kit_amount`, `genuine_accessories_amount`, `non_genuine_accessories_amount`
- `green_tax_amount`, `service_package_amount`

**What analytics currently reports:**  
Analytics `discount_applications` snapshot captures discount_key + amounts, but the field registry powering that is the old 6-field set. `MANAGEMENT_REFERRAL` and `BUFFER_DISCOUNT` as distinct keys are not in any query.

**Impact:**  
Net discount totals undercount. Discount mix by type (especially MR and buffer) cannot be shown.

---

### 3.6 Five-Master SKU Standard Entitlement Data — Gap: Medium

**What audit-core now has:**  
`uc03_sku_standard.py` produces a complete "entitlement block" for any vehicle on any date:
- **priceList** — 10 components with price_since dates, on-road for INDIVIDUAL and CORPORATE
- **consumerScheme** — Cash / accessories / warranty / insurance benefits with M&M vs dealer split
- **exchangeScheme** — Exchange / scrappage / welcome benefits by scenario
- **corporate** — Privilege by category (Z / Y / F / A / B)
- **grid** — Buffer, protection days, OD cap, out-of-territory per model

The underlying tables (`price_list_items`, `discount_scheme_benefits`, `dealer_discount_grid_rows`) are already in the dump source table list.

**What analytics currently reports:**  
Analytics does not query any of these master tables directly. The `_DEAL_FACTS_CTE` uses `discount_applications` (which is already reconciled master vs actual) but the raw entitlement master itself is not used as a dimension.

**Impact:**  
Analytics can show "above eligible" for existing discount_keys, but cannot break this down by M&M vs dealer contribution, by scheme category (CONSUMER / EXCHANGE / CORPORATE), or by benefit tier.

---

### 3.7 Audit Rules Per-Component and Time-Window Metrics — Gap: High

**What audit-core now has (22 named deal-audit rules in `uc03_p2_audit_rules.py`):**

New rule families not previously in analytics:
- `TCS_SHORT` — Statutory TCS underpayment flag
- `DEAL_UNDERCHARGED` per component (not just net) 
- `EXCESS_DISCOUNT` per discount key (not just total)
- Time-windowed findings: `PAYMENT_AFTER_DELIVERY_BEYOND_GRACE`, `DO_PAYMENT_NOT_RECEIVED`, `TRADE_IN_NOT_RESOLD`, `DELIVERY_NOT_COMPLETED_IN_TIME`
- Cash compliance: `CASH_ABOVE_LIMIT` (Income Tax Act s.269ST, ₹2L limit)
- Post-delivery: `POST_DELIVERY_REFUND`

**What analytics currently reports:**  
Compliance endpoint shows finding counts by rule_key — these rules will appear if they fire. But analytics has no breakout of:
- TCS compliance specifically (statutory exposure)
- Per-component undercharge analysis
- Time-window breach rates (% of deals settling within grace, % with DO received in time)
- Cash compliance rate (% of deals with any CASH_ABOVE_LIMIT finding)

---

### 3.8 Pricing Date Override Tracking — Gap: Low–Medium

**What audit-core now has:**  
`uc03_p2_deal_actions.py` records `PRICING_EFFECTIVE_DATE_APPLIED` events. Each journey can have a pricing date that differs from its booking date (invoice date or manual date + reason).

**What analytics currently reports:** Nothing. There is no pricing date dimension in any analytics query.

**Impact:**  
Analytics cannot flag "deal priced on invoice date, not booking date" — a common audit manipulation where a later price list (after a price cut) is applied to inflate the standard, making an otherwise-excess discount appear compliant.

---

## 4. What Should NOT Be Added to Analytics

The following data either carries privacy risk, lacks statistical significance for aggregated BI, or would violate the analytics service's operational isolation guarantee.

| Data | Reason Not to Add |
|------|------------------|
| Full customer addresses / names | Privacy — analytics already derives 6-digit PINs only; full PII must stay in audit-core |
| DI-extracted raw field values (low confidence) | These are unreviewed guesses; aggregating them would pollute metrics |
| Per-journey Actor IDs in raw form | Staff-level drill-down into named individuals is HR territory, not BI |
| Real-time rule execution logs | analytics is snapshot-based; transactional rule execution is not a snapshot dimension |
| Document page content / OCR text | Not a business metric; no analytics value |
| `workflow_task_attempts` retry detail | Operational telemetry, not business intelligence |
| `customer_identity_index` fields | PII index — no business aggregation value; high privacy risk |
| `p2_upload_batches` internal state | DI plumbing; not a business dimension |
| Lease tokens / work queue internals | Operational infrastructure |
| Pricing date override REASON text | Free-text; PII risk in actor attribution; no aggregation value |

---

## 5. Dealer Dashboard — Differentiator Design

### 5.1 The Core Proposition

Dealers today see their own DMS (bookings, invoices) and get periodic OEM scorecards. Neither shows them:
- How their deal terms compare to OEM entitlements at the discount-component level
- Where their compliance exposure sits (and what the financial value is)
- How their salespeople perform on attachment (insurance, accessories, EW)
- Where they are at risk on time-windows (settlements, finance disbursements, trade-in resale)

A dealer dashboard built on analytics gives them all four — and nothing in the OEM portal or DMS can replicate the combination.

---

### 5.2 Proposed Dealer Dashboard Sections

#### Section A — Deal Health Overview

*Answers: "What is the state of my active deals right now?"*

| Card | Metric | Source |
|------|--------|--------|
| Open Journeys | Count by stage (Booking / Delivery / Complete) | `journeys` |
| Deals Delivered This Month | Count with actual delivery date in period | `deliveries` |
| Pending Settlement | Count: delivered but balance > 0 | `payments` vs `commercial_lines` |
| Finance DO Pending | Count: DO_PAYMENT_NOT_RECEIVED open finding | `audit_findings` |
| Trade-In Pending Resale | Count: TRADE_IN_NOT_RESOLD open finding | `audit_findings` |

---

#### Section B — Discount Intelligence (The Differentiator)

*Answers: "How are my deals positioned against what OEM allows?"*

| Visual | Metric | New Data Required |
|--------|--------|-----------------|
| Discount Waterfall by Type | CASH / EXCHANGE / CORPORATE / LOYALTY / MR / BUFFER / OTHER amounts | MR discount (0138), buffer discount (0137) |
| Standard vs Actual Heatmap | Per model: standard entitlement vs. actual given | `discount_applications.standard_eligible_amount` vs `actual_discount_amount` |
| Above-Eligible by Model | Models where actual > standard (EXCESS_DISCOUNT rule) | Already in analytics, needs model dimension |
| MR Discount Usage | Count of MR-opted journeys, total MR amount, by TL | `p2_management_referrals` (0138) — NEW |
| Buffer Utilisation % | Actual buffer claimed / agreed buffer per model (grid) | `dealer_discount_grid_rows` (0137) — NEW |
| M&M vs Dealer Split | What portion of benefit came from OEM vs dealer self-funding | `discount_scheme_benefits` M&M/dealer contribution — NEW |

This section is the clearest differentiator. No DMS, OEM portal, or generic BI tool can produce the "standard vs actual vs buffer vs MR" multi-layer discount view — it requires the OEM master, the dealer grid, and the journey commercial data together.

---

#### Section C — Compliance Exposure

*Answers: "Where do I stand on audit risk?"*

| Card / Visual | Metric | Notes |
|--------------|--------|-------|
| Open High-Severity Findings | Count with finding severity = HIGH, status = OPEN | Existing |
| Open Findings by Category | EXCESS_DISCOUNT / DEAL_UNDERCHARGED / PAYMENT / TRADE_IN / CASH | New rule families |
| TCS Exposure | Count of TCS_SHORT findings + total short amount | Statutory risk — NEW |
| Cash Compliance | Count of CASH_ABOVE_LIMIT findings | Regulatory — NEW |
| Time-Window Breach Rates | % of deals settling within grace / DO received in time / trade-in resold in time | NEW aggregation |
| Documents Missing Rate | % of active deals with DOCUMENT_MISSING finding | Existing |
| Compliance Trend (30 days) | Finding count over last 30 days by category | Requires period-sliced dump |

---

#### Section D — Attachment Performance

*Answers: "How well is my team selling the full deal?"*

| Visual | Metric | Notes |
|--------|--------|-------|
| Finance Penetration | % of deals financed, by salesperson | Existing |
| Insurance Penetration | INHOUSE only % (exclude SELF) | Needs 0135 split |
| Insurance OD vs Grid Cap | Actual OD % vs `insurance_od_percent` from grid | NEW — 0135 + 0137 |
| EW / RSA / Accessories Attach | % by type, by salesperson | Existing |
| VAS Value per Deal | Average VAS revenue per delivered journey | Existing |
| Corporate Deal Mix | % of deals with CORPORATE_PRIVILEGE discount | Existing |

---

#### Section E — Delivery Performance

*Answers: "Am I keeping my delivery promises?"*

| Card | Metric | Notes |
|------|--------|-------|
| On-Time Delivery % | Actual ≤ planned delivery date | Existing |
| Avg Booking-to-Delivery Days | With P75, P90 | Existing |
| Delivery Completion Compliance | % of deals with no DELIVERY_NOT_COMPLETED_IN_TIME finding | NEW rule-based |
| Deals Awaiting Delivery (by age) | Pending > 30 days, > 60 days | Existing |

---

#### Section F — Sales & Product Mix

*Answers: "What am I selling and to whom?"*

| Visual | Metric | Notes |
|--------|--------|-------|
| Model Mix (Pie/Bar) | Bookings by model / variant | Existing |
| Outlet Contribution | Booking count per outlet | Existing |
| Customer Geography (PIN Map) | Bookings by 6-digit PIN code | Existing |
| Out-of-Territory Deals | Count/amount where out_of_territory_amount applied | NEW — 0137 |
| Lead Source Mix | WALK_IN / REFERRAL / DIGITAL / OEM etc | Existing |

---

### 5.3 Dealer vs. Network Access Model

| View Level | Who Sees It | Data Scope |
|------------|-------------|-----------|
| Dealer Dashboard | Each dealer (new permission: `audit.analytics.dealer`) | Their own `dealer_id` journeys only |
| Outlet Scorecard | Dealer admin | All their outlets |
| Network Scorecard | OEM / Platform Admin | Cross-dealer (existing `business-scorecard` endpoint) |

Key implementation note: the analytics service already enforces `tenant_id` scoping. A dealer-level scope would add `dealer_id` as a second filter. The permission key `audit.analytics.dealer` (vs `audit.analytics.read` for OEM-level access) would be checked via the existing `security_authorization.py` pattern.

---

## 6. Prioritised Gaps — What to Add and In What Order

### Priority 1 — Do First (Closes Critical Gaps, Enables Dealer Dashboard)

| Gap | Action Required | Benefit |
|-----|----------------|---------|
| Management Referral discount | Add `p2_management_referrals` to dump source list; add MR as a named dimension in `/discounts` and `/business-intelligence/discounts` | Enables MR monitoring on dealer + network dashboards |
| Insurance source split (INHOUSE vs SELF) | Add `insurance_source` to snapshot field extraction; add split to `/insurance` endpoint | Fixes inflated penetration metrics |
| Buffer utilisation | Add `dealer_discount_grid_rows` to dump; add buffer vs agreed-buffer comparison to `/business-intelligence/discounts` | Enables grid compliance view |

### Priority 2 — Add Next (Enriches Compliance Reporting)

| Gap | Action Required | Benefit |
|-----|----------------|---------|
| TCS compliance metric | Add TCS_SHORT finding aggregation as a named card | Statutory exposure visible |
| Cash compliance metric | Add CASH_ABOVE_LIMIT finding count | Regulatory risk visible |
| Time-window breach rates | Add settlement grace / DO receipt / trade-in resale / delivery completion rates | Operational risk view |
| Per-discount EXCESS flagging | Ensure EXCESS_DISCOUNT rule breaks out by discount_key in compliance report | Replace single "excess total" with per-type view |

### Priority 3 — Add Later (Master-Level Intelligence)

| Gap | Action Required | Benefit |
|-----|----------------|---------|
| M&M vs dealer contribution split | Add `discount_scheme_benefits` M&M/dealer columns to snapshot; expose in discount endpoint | Shows true dealer cost of discount |
| Pricing date dimension | Add `PRICING_EFFECTIVE_DATE_APPLIED` activity events to analytics; flag deals priced on non-booking date | Audit manipulation detection |
| Component-level price master view | Add `price_list_items` with `price_since` to snapshot; add master-vs-actual per component | Full commercial variance report |
| Out-of-territory deal flagging | Extract `out_of_territory_amount` from grid; flag journeys where it applies | Territorial compliance |

### Priority 4 — Not Recommended

| Item | Reason |
|------|--------|
| Composite compliance score | No agreed formula; would produce a misleading single number |
| Historical trending | Requires architectural change (multi-dump retention, period filters); separate workstream |
| Real-time sync | Snapshot architecture is intentional; real-time is operationally invasive |
| Predictive analytics | No historical baseline yet; premature |
| Employee-level individual named reports | Privacy and HR boundary concerns; keep at role-level aggregation |

---

## 7. Dealer Dashboard — Differentiator Narrative

When presenting this to a dealer, the pitch is simple:

> "Your DMS tells you what you sold. OEM portal tells you what you're supposed to sell. Verigence tells you whether the deal terms you gave were compliant, where you left money on the table, where you over-discounted, and how your team is actually performing — all before the audit catches it."

Specific differentiators no competitor can match:
1. **Standard-vs-actual discount at the component level** — not just a net number
2. **Buffer utilisation per model** — you know when your TL is at the edge of the agreed ceiling
3. **MR discount visibility** — the one discount that is hardest to govern without a tool like this
4. **Insurance source split** — INHOUSE penetration (your revenue) vs SELF (just a compliance record)
5. **Time-window compliance** — know your settlement, DO receipt, and trade-in status before the audit flags them

---

## 8. Summary Table — Full Gap Inventory

| New Audit-Core Capability | Migration / File | Analytics Gap | Priority |
|--------------------------|-----------------|--------------|---------|
| Management Referral discount | 0138, `p2_management_referrals` | Not in any report | P1 |
| Insurance source INHOUSE/SELF | 0135, `insurance_records.insurance_source` | Penetration / premium inflated | P1 |
| Dealer Discount Grid (buffer, OD cap, OOT) | 0137, `dealer_discount_grid_rows` | Not in any report | P1 |
| Expanded discount field set (14 fields) | `uc03_booking_commercial_components.py` | MR, buffer, OEM-referral missing | P1 |
| TCS_SHORT audit rule | `uc03_p2_audit_rules.py` | No TCS compliance metric | P2 |
| CASH_ABOVE_LIMIT audit rule | `uc03_p2_audit_rules.py` | No cash compliance metric | P2 |
| Time-windowed audit rules | `uc03_p2_audit_rules.py` | No breach-rate aggregations | P2 |
| EXCESS_DISCOUNT per key | `uc03_p2_audit_rules.py` | Only net excess shown | P2 |
| M&M vs dealer contribution split | `discount_scheme_benefits` | Not in analytics | P3 |
| Pricing date override tracking | `uc03_p2_deal_actions.py` | Not in analytics | P3 |
| Per-component price_since dating | `price_list_items.price_since` | Not in analytics | P3 |
| Out-of-territory additions | `dealer_discount_grid_rows.out_of_territory_amount` | Not in analytics | P3 |
| Scrappage discount (backfill 0105) | `booking_form_review_values` | Possibly missing from discount totals | P2 |
| Corporate privilege by category | `discount_scheme_benefits` | Not broken by Z/Y/F/A/B | P3 |

---

*This report was produced by deep review of `verigence-analytics` (all source files) and `verigence-audit-core` (migrations 0133–0138, uc03_sku_standard.py, oem_price_masters.py, oem_master_parsers.py, uc03_p2_audit_rules.py, uc03_deal_reconciliation.py, uc03_p2_journey360.py, uc03_p2_deal_actions.py, uc03_p2_worker.py, uc03_p2_runtime.py, journey_housekeeping.py, p2_templates/). No code changes were made.*
