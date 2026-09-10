# Verigence Analytics Solution Design v1.0

**Status:** Baseline design for implementation planning  
**Date:** 10-Sep-2026  
**Product:** Verigence  
**Repository:** `verigence/verigence-analytics`

---

## 1. Purpose

This document captures the agreed direction for Verigence Analytics, including:

- architectural isolation from operational Verigence workflows;
- reuse/extension of Verigence Security, SSO, tenant, role and permission capabilities;
- business analytics and internal operations reporting scope;
- reusable analytics dimensions/measures;
- current Audit Core data-readiness status;
- known data/model/formula gaps that must be closed before specific reports are treated as authoritative;
- report traceability and drill-down expectations.

The primary business objective is to make Verigence Analytics a business and audit intelligence capability, not merely a collection of charts.

---

## 2. Agreed Product Boundary

### 2.1 User experience

Analytics is part of **Verigence** from the user's point of view.

A user enters Analytics from Verigence navigation, but the Analytics UI is independently deployable and independently operable. It may be deployed as a separate web application while retaining Verigence branding, navigation context and SSO continuity.

### 2.2 Operational isolation

Analytics must not be on the critical request path of any operational Verigence flow.

Failure, latency, deployment or scaling of Analytics must not block or degrade:

- Booking capture;
- Document upload/review;
- PC verification;
- Audit Flag processing;
- TL/PM review;
- Payment processing/verification;
- Delivery workflows;
- CRM activities;
- any other Audit Core operational transaction.

Audit Core must not depend on Analytics for a transaction-processing decision.

### 2.3 Logical architecture

```text
Verigence Product
│
├── Core Web / Audit Experience
│   └── Audit Core
│       └── Operational Audit Core schema/data
│
└── Analytics Entry Point
    └── Analytics Web Application (independently deployable)
        └── Analytics API / Service (independently deployable)
            └── Analytics schema / reporting store
                ↑
                └── asynchronous read-only ingestion from authoritative Audit Core facts
```

The core Verigence web application does not need to become a chart-processing or reporting engine. Its responsibility is to provide the product entry point and authenticated navigation into Analytics.

### 2.4 Database isolation requirement

Analytics must never write to `auditcore.*`.

Analytics ingestion is read-only against authoritative Audit Core facts and writes only to Analytics-owned storage.

To support the requirement that Analytics should not impact operational workloads, Analytics source reads should use a dedicated read-only database identity and, where supported by the deployment topology, a dedicated read replica / dedicated database compute rather than competing with the operational primary compute for heavy analytical reads.

At minimum, the following controls are mandatory:

- dedicated read-only source identity;
- dedicated Analytics runtime identity;
- bounded connection pool;
- query timeout;
- ETL batch/concurrency limits;
- no full-table analytical queries on the operational request path;
- incremental/watermark ingestion;
- independent scaling and deployment;
- observable data freshness and ETL health.

---

## 3. Security, Identity and Authorization

Analytics will extend existing Verigence Security rather than introduce a separate authentication model.

The Analytics application/service must consume the existing Verigence identity context:

```text
User / Principal
Tenant / Audit Project
Operating Role
Dealer Scope
Outlet Scope
Permissions
```

The Analytics application must not use a static browser-visible API key as its user authorization mechanism.

### 3.1 Proposed Analytics permissions

Permission names must ultimately follow the existing Verigence Security naming convention, but the required capability model is:

```text
analytics.dashboard.read
analytics.business.read
analytics.employee.read
analytics.financial.read
analytics.audit.read
analytics.export
analytics.admin
```

### 3.2 Scope enforcement

`tenant_id`, `dealer_id`, `outlet_id`, employee identity or any route/path parameter is never authorization by itself.

Analytics authorization must derive from the authenticated Verigence principal and its effective tenant/dealer/outlet/role scope.

Examples:

- PC: own permitted operational analytics only;
- TL: permitted PCs/cases within assigned scope;
- PM: assigned dealers/outlets/projects;
- management roles: business analytics within explicitly granted scope;
- Analytics Admin: operational/technical Analytics administration only as explicitly permitted.

---

## 4. Source-of-Truth Principle

Audit Core remains authoritative for business and audit facts.

Analytics may:

- copy facts read-only into its own reporting model;
- aggregate facts;
- group/filter/slice facts;
- calculate approved derived measures;
- provide drill-down and visualisation.

Analytics must not redefine authoritative booking, audit, payment, delivery, finding or evidence state.

Where a business formula remains unresolved, Analytics must expose the report as blocked/partial rather than invent the formula.

---

## 5. Analytics Data Model Strategy

The platform should avoid implementing every report as a separate hard-coded pipeline.

### 5.1 Reusable dimensions

The core reporting model should support, where source data exists:

- Tenant / Audit Project;
- Date / Day / Week / Month / Financial Period;
- Dealer;
- Outlet;
- Customer Type;
- Sales Consultant / dealership staff;
- PC;
- TL;
- PM;
- CRM user/team;
- Model;
- Variant;
- Product/SKU where relevant;
- Deal Type;
- Deal Source;
- Lead Source;
- Finance Type / Provider;
- Insurance Type / Provider / Agent Code;
- Payment Mode;
- Discount Scheme / Discount Type;
- Add-on / VAS Type;
- Trade-in;
- Document Type / Requirement;
- Rule / Control;
- Finding Type / Severity / Origin;
- Workflow Stage / Review Outcome.

### 5.2 Reusable measures

Common measures should include:

- Count;
- Amount;
- Average;
- Standard Amount;
- Actual Amount;
- Variance;
- Percentage;
- Penetration / Attach Rate;
- Compliance Rate;
- Breach Rate;
- Send-back Rate;
- Closure Rate;
- Turnaround Time;
- Ageing;
- Outstanding Amount;
- Financial Exposure.

A report should be a governed definition over reusable facts/dimensions/measures wherever possible.

---

## 6. Business Analytics Catalogue

Business analytics is the primary focus of the Analytics product.

### 6.1 Audit, Compliance and Risk

| Report | Requirement / Basis | Business Purpose | Current Readiness |
|---|---|---|---|
| Observation / Error Summary | `VAC-ANA-012` | Breach/error counts by rule/control, dealer, outlet and period | Ready structurally; current Audit Flags have rule traceability |
| Duplicate Booking Report | `VAC-ANA-001` | Duplicate booking counts and trend | Model support exists; source identity/match population currently incomplete |
| Short / Excess Report | `VAC-ANA-011` | Under/over-payment against approved commercial basis | Blocked pending approved formula/tolerance |
| Multi-tier Remarks Audit Trail | `VAC-ANA-013` | PC/TL/PM remark and decision history | Structurally supported; must preserve event history, not only latest state |
| Breach Classification Summary | `VAC-DAY-006/007` | BREACH / NO_BREACH / SEND_BACK by dealer/outlet/reviewer | Review-decision population currently incomplete |
| Previous-Day Exception Review | `VAC-DAY-004` | Non-intimated/exception transactions | Structurally supported |
| Dealer / Outlet Compliance Scorecard | Derived business report | Compare compliance and exception performance across dealer/outlet | Strong derivation once sufficient volume exists |
| Repeat Breach Analysis | Derived business report | Identify controls repeatedly failing over time | Strong derivation from finding rule/control history |
| Dealer Risk Heatmap | Derived business report | Dealer/outlet x rule/control concentration of exceptions | Strong derivation from findings |
| Finding Ageing & Closure | Derived business report | Open findings, ageing buckets and closure TAT | Supported by finding status/timestamps/SLA fields |
| Top Control Failures | Derived business report | Highest recurring rule/control failures | Supported by rule-keyed findings |
| Escalation Ageing / Resolution | Derived business report | Open escalations, owners and TAT | Model exists; live data currently unpopulated |
| Audit Coverage Report | Derived business report | Cases received vs audited/reviewed/completed | Derivable from Journey/workflow/audit states |

### 6.2 Document Compliance

| Report | Business Purpose | Current Readiness |
|---|---|---|
| Missing Document Audit Flags | Count missing-document flags by document/dealer/outlet/period | Ready from Audit Flags |
| Document Compliance Summary | Required vs available vs missing by document/dealer/outlet | Model exists; requirement/assessment lifecycle must be fully closed/populated |
| Conditional Document Compliance | Corporate/GST/Exchange/Finance/Insurance scenario vs required evidence | Strong model direction; depends on scenario fields and requirement evaluation being populated consistently |
| Document Failure Trend | Repeat missing document by dealer/outlet/document type | Ready once volumes grow |
| Evidence Quality / Review Status | Pending/rejected/review-required evidence | Structurally supported |

The report must distinguish **flag origin** from **case handler**. A system-generated missing-document flag on a PC-handled case must not be reported as a PC-raised finding unless the origin says so.

### 6.3 Payment and Finance

| Report | Requirement / Basis | Business Purpose | Current Readiness |
|---|---|---|---|
| Receipt / Payment Summary | `VAC-ANA-010` | Receipt value and realised value by payment mode | Basic receipt/payment facts available; realised logic for DO/PO/refund remains governed/open |
| Payment Mode Mix | Derived | Bank/Card/Cash/Cheque/UPI/DO/PO/Trade-in mix | Supported by payment method when populated |
| Payment Verification Tracker | `VAC-PAY-011` | Verified / unverified / pending | Event model exists; current live verification events unpopulated |
| Cash Controls Report | `VAC-PAY-009` | Cash transactions and required control evidence | Structurally supported; control-specific population required |
| Month-end Cash Trigger Log | `VAC-PAY-010` | Last-day cash payments and CRM triggers | Structurally supported with payment + CRM trigger data |
| PO Outstanding Tracker | `VAC-PAY-006` | PO pending/realised/outstanding | Finance structure exists; current finance rows unpopulated |
| Finance Type Breakdown | `VAC-ANA-003` | In-house / self finance / outright split | Finance model exists; population incomplete |
| Finance Provider Analysis | Derived | Volume and value by finance provider | Supported once finance facts are populated |
| Payment Realisation Ageing | Derived | Age outstanding receipts/DO/PO | Partial; depends on approved realisation semantics |
| Revenue Leakage / Commercial Variance | Derived business report | Standard vs actual commercial variance | Partial: standard/master amount population must improve |

### 6.4 Deal, Discount and Product

| Report | Requirement / Basis | Business Purpose | Current Readiness |
|---|---|---|---|
| Turnaround Analysis | `VAC-ANA-002` | First Receipt to Delivery TAT by slab/model/dealer | Structure exists; actual delivery timestamps currently under-populated |
| Per-Car Discount Analysis | `VAC-ANA-014` | Total, OEM Scheme and Above-Scheme discount per car | Discount facts exist; approved Total/Above-Scheme formula still required |
| Discount Scheme Utilisation | Derived | Eligible scheme vs applied scheme by model/dealer | Partial; scheme-version linkage must be consistently populated |
| Corporate Discount Analysis | Derived | Corporate discount use and compliance | Fields exist; current operational population incomplete |
| GST Benefit Analysis | Derived | GST-benefit deals and compliance | Fields exist; current operational population incomplete |
| Exchange Discount Analysis | Derived | Exchange discount use, value and exception rate | Fields exist; current operational population incomplete |
| Accessories Analysis | `VAC-ANA-004` | Accessory value/penetration and classification | Add-on/commercial facts exist; Genuine vs Non-Genuine requires explicit governed classification if not derivable from master data |
| Model / Variant Business Analysis | Derived | Commercial, VAS, finance, insurance and exception trends by model/variant | Strong derivation once model/variant population is consistent |
| Customer Type Analysis | Derived | Individual/Corporate/etc. business mix and exception pattern | Customer type exists; current live data includes many `PENDING` values |
| Deal Source Analysis | Derived | Deal-source mix, value and compliance | Booking field exists; live population currently incomplete |
| Deemed DSA Analysis | `VAC-ANA-008` | DSA-classified deals by approved combination of source/insurance/finance facts | Blocked until exact business formula is approved and source fields populated |

### 6.5 Insurance and VAS

| Report | Requirement / Basis | Business Purpose | Current Readiness |
|---|---|---|---|
| Insurance Penetration | `VAC-ANA-005` | Dealer vs self-insurance penetration | Insurance facts exist; classification population incomplete |
| Insurance Premium Analysis | Derived | Standard/actual premium by insurer/dealer/model | Partial; premium population incomplete |
| Self-Insurance Agent Code Analysis | `VAC-ANA-006` | Detect reused agent codes across bookings | **Structured agent-code source is currently missing and must be added** |
| Extended Warranty Penetration | `VAC-ANA-007` | EW take-rate | Supported through add-on model where populated |
| RSA Penetration | `VAC-VAS-001` capability basis / derived analytics | RSA attach rate | Supported through add-on model |
| Service Package Penetration | `VAC-VAS-001` capability basis / derived analytics | Service-package attach rate | Supported through add-on model where captured |
| VAS Basket / Attach Matrix | Derived | Insurance + EW + RSA + service + accessories combinations | Strong derivation from Journey-level add-on/insurance facts |

`VAC-VAS-001` supports the underlying configurable VAS capability; penetration/attach-rate reports should be treated as derived analytics unless separately baselined as explicit analytics requirements.

### 6.6 Trade-in

| Report | Requirement / Basis | Business Purpose | Current Readiness |
|---|---|---|---|
| Trade-in Penetration | Derived | Share of deals with trade-in | Supported when trade-in linkage is populated |
| Trade-in P&L Summary | `VAC-ANA-009` | Purchase, sale and profit/loss | **Structured resale/sale amount is currently missing and must be added if not available from an authoritative structured source** |
| Trade-in Ageing | `VAC-ANA-009` | Age unsold trade-ins by bucket | Model supports lifecycle dates; final ageing threshold (60 vs 90) remains governed/open |
| Trade-in Lifecycle | Derived | Quoted, actual, handover, payment, resale progression | Structurally supported |

### 6.7 Dealer, Outlet and Sales Consultant Business Analytics

| Report | Business Purpose | Current Readiness |
|---|---|---|
| Executive Business Dashboard | Cross-dealer commercial/audit/penetration/turnaround KPIs | Derivable from all mature facts |
| Dealer Business Scorecard | Overall dealer commercial, compliance and product performance | Strong target report |
| Outlet Business Scorecard | Outlet comparison and trend | Strong target report |
| Sales Consultant Business Profile | Discount, VAS, finance, insurance, trade-in and exceptions by SC | **Model exists but current `dealership_staff` and booking `sales_staff_id` population is missing** |
| Sales Consultant Compliance Profile | Missing docs, duplicate flags, discount/payment exceptions by SC | Same population dependency as above |
| SC VAS / Cross-sell Analysis | Insurance/EW/RSA/service/accessories attach rate by SC | Same population dependency |
| SC Discount Analysis | Average discount, scheme use and exception rate by SC | Same population dependency plus approved discount formulas |
| SC Trade-in / Finance Mix | Trade-in/finance business profile by SC | Same population dependency |

Sales Consultant identity is a high-priority business-data dimension and should be captured consistently on Booking/Journey records.

---

## 7. Internal Operations Analytics Catalogue

Internal reporting is supported from the same Analytics platform, but it is secondary to business analytics.

| Report | Audience | Measures |
|---|---|---|
| PC Daily Productivity | PC / TL | Cases handled, booking/delivery/payment activities |
| PC Document Review Activity | PC / TL | Documents reviewed, missing-document flags on handled cases |
| PC Low-Confidence Review Activity | PC / TL | Low-confidence values reviewed/confirmed/corrected |
| PC Rework / Send-Back | PC / TL | Returned cases, repeat rework, send-back rate |
| PC Workload & Ageing | PC / TL | Open cases and ageing |
| PC Completion TAT | PC / TL | Capture/review turnaround |
| TL Review Productivity | TL / PM | Reviews completed |
| TL Review Outcome Mix | TL / PM | BREACH / NO_BREACH / SEND_BACK |
| TL Review TAT | TL / PM | Review turnaround |
| TL Backlog & Ageing | TL / PM | Pending review workload |
| PM Daily Oversight | PM | Breaches, exceptions and dealer/outlet trends |
| PM Escalation Oversight | PM | Escalated cases, ageing and ownership |
| CRM Call Productivity | CRM / Manager | Triggered/completed/pending calls |
| CRM Call Ageing | CRM / Manager | Overdue calls and ageing |
| Workflow Backlog | Operations | Cases pending by stage/role |
| SLA Breach Report | Operations | Tasks/cases beyond SLA |
| Audit Task Throughput | Operations | Created/completed/pending tasks |
| Evidence Processing Status | Operations | Processing/ready/failed evidence |
| Document Assessment Status | Operations | Pending/answered/resolved assessments |
| Rule Engine Activity | Audit Admin | Rules evaluated/triggered/passed |
| Audit Flag Origin Analysis | Audit Admin | System vs PC/TL/PM origin |
| Data Completeness Report | Admin | Missing critical business dimensions/facts |
| ETL Freshness Report | Analytics Admin | Last successful sync, lag, failures |
| Analytics Usage Report | Analytics Admin | Report usage/exports where telemetry is captured |
| Analytics API Performance | Technical Admin | Analytics service latency/error/load only |

### 7.1 PC measurement rule

`Findings Raised` must not be treated as a mandatory PC productivity KPI.

A PC may raise an Audit Flag where permitted, but the operational analytics should primarily measure:

- cases handled/submitted;
- documents reviewed;
- missing-document flags on handled cases;
- system-generated vs human-originated flags;
- low-confidence values reviewed;
- corrections made;
- rework/send-back;
- completion TAT;
- backlog/ageing.

---

## 8. Report Interaction and Traceability

A Verigence Analytics chart must not become a dead-end presentation.

Every aggregate that represents an audit/business exception should support drill-down, subject to permission scope.

Example:

```text
Missing Document Flags = 37
    ↓
37 affected Journeys
    ↓
Dealer / Outlet / Customer / Vehicle / Document Requirement / Rule / Origin
    ↓
Authoritative Audit Core Journey / Finding / Evidence context
```

Analytics must retain enough provenance to explain:

- which authoritative Journey/fact contributed;
- which rule/control generated a finding;
- which definition version/formula produced the metric;
- data freshness (`data_as_of`);
- filters/scope applied.

---

## 9. Common Analytics API Contract

The Analytics service should expose governed report definitions rather than unrelated custom response shapes for every screen.

Conceptual request:

```text
AnalyticsRequest
- report_code
- date_from
- date_to
- tenant/project scope
- dealer/outlet filters
- model/variant filters
- employee/role filters where permitted
- dimensions
- report-specific filters
```

Conceptual response metadata:

```text
AnalyticsResult
- report_code
- definition_version
- generated_at
- data_as_of
- last_successful_sync
- sync_status
- filters_applied
- dimensions
- measures
- rows
- totals
- drilldown references/capability
```

No stale dataset should be presented as current without freshness metadata.

---

## 10. ETL / Ingestion Design Requirements

Analytics ingestion must be asynchronous and independent of operational request handling.

Required properties:

- incremental/watermark-driven;
- idempotent;
- bounded batches;
- safe retry;
- failure-isolated;
- freshness-aware;
- single logical runner per sync job or protected by advisory lock/leader election;
- correct handling of equal timestamps using a stable tie-breaker such as `(updated_at, primary_key)` or safe inclusive reprocessing;
- watermark only advances after a successful committed batch;
- initial backfill procedure;
- late-arriving/update handling;
- observability of last successful sync and lag.

ETL failure must not break Analytics API requests unnecessarily, but stale data must be explicitly identified.

---

## 11. Current Audit Core Data Readiness Snapshot

The following is a point-in-time observation from the connected Neon `production` branch on **10-Sep-2026**. These numbers are operationally transient and must not be treated as design constants.

### 11.1 Current population

| Fact/Table | Current Rows / Population Observation |
|---|---:|
| Journeys | 12 |
| Bookings | 11 |
| Audit Findings | 29 |
| Commercial Lines | 42 |
| Commercial Lines with Actual Amount | 34 |
| Commercial Lines with Standard Amount | 8 |
| Discount Applications | 9 |
| Discount Applications linked to scheme version | 4 |
| Evidence | 64 |
| Journey Document Requirements | 181 |
| Document Requirements currently `PENDING` | 181 |
| Journey Document Assessments | 46 |
| Answered Document Assessments | 0 |
| Payments | 3 |
| Payment Verification Events | 0 |
| Finance Records | 0 |
| Insurance Records | 3 |
| Journey Add-ons | 6 |
| Trade-in Cases | 3 |
| Deliveries | 3 |
| Deliveries with actual delivery timestamp | 0 |
| Dealership Staff | 0 |
| CRM Interactions | 0 |
| Escalations | 0 |
| Daily Ops Items/Runs | 0 |
| Customer Identity Index | 0 |
| Journey Workflow Events | 34 |
| Workflow Tasks | 7 |

### 11.2 Booking dimension population gaps

For the current 11 bookings, the following dimensions are presently unpopulated:

- `sales_staff_id`;
- `deal_type_code`;
- `deal_source_code`;
- `lead_source_code`;
- `corporate_customer`;
- `corporate_discount_taken`;
- `corporate_id_available`;
- `gst_benefit`;
- `exchange_discount_taken`;
- `accessories_taken`;
- `outright_purchase`.

Customer type is structurally present, but most current customer rows are still `PENDING`, so customer-segment analytics is not yet meaningful.

### 11.3 Current useful Audit Flag population

The current finding population already has strong rule traceability (`rule_key` and origin are populated on the observed findings), including explicit required-document-missing rules. This is sufficient to begin Audit Flag / missing-document analytics even though the broader document-assessment lifecycle still needs maturity.

---

## 12. Data Capture / Model Gaps to Close

These are the highest-priority gaps for business analytics.

### Priority A — capture/population gaps

1. Sales Consultant / dealership staff master and booking linkage.
2. Deal Type, Deal Source and Lead Source.
3. Customer Type finalisation rather than `PENDING`.
4. Corporate / Corporate Discount / Corporate ID / GST Benefit / Exchange / Accessories / Outright flags.
5. Standard commercial amounts and master/price-list linkage.
6. Discount scheme-version linkage.
7. Actual delivery timestamp.
8. Payment verification events.
9. Finance records and finance status.
10. Insurance classification and premium values.
11. Document requirement/assessment completion states.
12. Duplicate identity/match/index population.
13. CRM interaction and escalation population where those workflows are used.

### Priority B — structured model gaps

1. **Insurance Agent Code** — required for reliable `VAC-ANA-006`; must have an authoritative structured source.
2. **Trade-in Resale/Sale Amount** — required for reliable Trade-in P&L.
3. **Accessories Genuine/Non-Genuine classification** — needs explicit structured classification or a governed master-data derivation.

### Priority C — unresolved business definitions

The following must not be guessed in Analytics:

1. Deemed DSA exact formula.
2. Short / Excess formula and tolerance.
3. Per-car Total Discount / Above-Scheme formula.
4. DO / PO / Refund realised-payment semantics.
5. Trade-in ageing threshold where 60 vs 90 days remains unresolved.

---

## 13. Recommended Business Reporting Priority

The reporting roadmap should prioritise management/business value rather than report count.

### Wave 1 — high-value with strongest current source readiness

- Observation / Error Summary;
- Missing Document Audit Flags;
- Rule / Control Failure Analysis;
- Repeat Breach Analysis;
- Dealer / Outlet Compliance and Exception views;
- Basic Receipt / Payment Summary;
- Basic Commercial Actual-value analysis;
- Basic Discount analysis;
- Trade-in volume/status;
- Accessories / RSA currently captured add-on analysis;
- Data Completeness dashboard.

### Wave 2 — after operational population gaps close

- Sales Consultant Business Profile;
- Dealer / Outlet Business Scorecard;
- Document Compliance %;
- Conditional Document Compliance;
- Finance Type / Provider;
- Payment Verification;
- Insurance Penetration / Premium;
- Turnaround;
- Customer Type / Deal Source / Model-Variant analysis;
- VAS basket / attach rate;
- Audit Coverage and SLA/ageing.

### Wave 3 — after structured model/formula gaps close

- Self-Insurance Agent Code reuse;
- Trade-in P&L;
- Short / Excess;
- Deemed DSA;
- authoritative Per-Car Above-Scheme Discount;
- Payment realisation ageing where DO/PO/refund semantics apply.

---

## 14. Frontend Technology Direction

Analytics UI remains independent from the operational web application.

For analytics rendering, the preferred technology set is:

```text
Charts       Apache ECharts + echarts-for-react
Tables       TanStack Table
Server state TanStack Query
PDF          jsPDF + jspdf-autotable
```

Recharts may be introduced only if there is a deliberate product/engineering rule for its use. If both chart libraries are used, business screens should not import them directly; Verigence-owned chart wrappers should provide consistent design, drill-down, tooltips, empty states and export behaviour.

ECharts alone is sufficient for the currently identified chart range and avoids duplicate charting stacks.

---

## 15. Non-Functional Requirements

Analytics must have its own:

- deployment lifecycle;
- health checks;
- logs/metrics/tracing;
- DB connection pool;
- ETL controls;
- caching strategy;
- failure handling;
- performance budget;
- CI/CD checks;
- schema migrations;
- API contract tests.

Analytics outage or deployment must not require Audit Core downtime.

Report query execution must be bounded and must not query operational Audit Core tables synchronously from the end-user request path for heavy analytical workloads.

---

## 16. Governance Rules

1. A report may be **Baselined**, **Derived**, **Partial**, or **Blocked**.
2. Derived reports must use authoritative source facts and approved definitions.
3. Blocked reports must state the missing formula/data dependency rather than substitute assumptions.
4. Every metric definition should be versioned.
5. Business filters and permission scope must be visible/auditable.
6. Aggregate audit metrics must support Journey/fact drill-down wherever permissions allow.
7. Analytics must expose data freshness.
8. Employee analytics must avoid misleading attribution; case handler and flag origin are separate dimensions.
9. Report definitions should be reusable across dashboard, table, export and PDF output.

---

## 17. Design Summary

Verigence Analytics is an independently deployable analytics application/service that is entered from Verigence and uses the same Verigence security and business scope model.

Its primary value is business analytics across audited vehicle-sale transactions: dealer/outlet performance, compliance, discounts, payment/finance, insurance, VAS, trade-in, documents, audit exceptions, turnaround and Sales Consultant business performance.

Internal PC/TL/PM/CRM/workflow analytics are supported from the same reporting foundation but are not the primary product focus.

The current Audit Core model already contains most required business structures. The immediate priority is not to invent additional reports or duplicate facts in Analytics; it is to ensure the operational workflows consistently populate the existing business dimensions and close the small number of structured model/formula gaps identified in this document.
