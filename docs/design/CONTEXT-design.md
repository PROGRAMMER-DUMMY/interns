# Design Architecture Context: `docs/design`

This document provides an exhaustive reference for all components in [`docs/design`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design).

---

## Executive Overview & Architectural Model

The `docs/design` directory contains technical architecture and design specifications for platform extensions. It documents the contracts, lifecycle, safety rules, and enforcement mechanisms for turning unstructured input (PDF documents) and implicit business requirements into explicit, machine-verifiable contracts (`KPI Intent Contract` and `opendataloader-pdf` document ingestion).

```
┌──────────────────────────────────────────────────────────────────────────┐
│                               docs/design                                │
├─────────────────────────────────────┬────────────────────────────────────┤
│ kpi_intent_contract.md              │ pdf_ingestion.md                   │
│ (KPI Intent Contract Design)        │ (Governed PDF/Document Ingestion)  │
└─────────────────────────────────────┴────────────────────────────────────┘
```

---

## File Details

### 1. [`kpi_intent_contract.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/kpi_intent_contract.md)

- **Exact Purpose**: Design specification for transforming implicit KPI requirements into explicit, per-facet intent contracts (`kpi_intent_contract.json`), preventing silent SQL generation defaults and enforcing facet realization at execution time.
- **Key Sections & Content**:
  - [`Section 1: Problem`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/kpi_intent_contract.md#L13-L31): Identifies root causes of shipped-wrong results, such as unmodeled denominator scopes (e.g. global total vs. per-department in `kpi_002`), dropped cuts ([`BUG-024`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L757-L804)), and unanchored age calculations ([`BUG-005`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/BUG_SESSION_REPORT.md#L219-L255)).
  - [`Section 2: Goal`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/kpi_intent_contract.md#L32-L47): Establishes the 5 pillars of explicit intent (Modeled, Scored, Clarified, Reported, Enforced).
  - [`Section 3: The Intent Facets`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/kpi_intent_contract.md#L48-L72): Defines canonical facets: `metric`, `grain`, `filters`, `denominator_scope`, `temporal_anchor`, `output_shape`, `null_zero_handling`.
  - [`Section 4: Lifecycle`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/kpi_intent_contract.md#L73-L93): Outlines extraction, facet-level scoring, targeted blocker panel routing for low-confidence facets, resolved-intent reporting, and SQL generation.
  - [`Section 5: Enforcement`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/kpi_intent_contract.md#L94-L111): Details `intent_coverage` realization checks inside `KPIExecutionHarness._semantic_errors` (e.g., `denominator_scope_not_realized`, `grain_not_realized`).
  - [`Section 6 & 7: Integration Points & Phasing`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/kpi_intent_contract.md#L112-L140): Describes implementation phasing starting from denominator-scope contracts through temporal anchors and output shapes.
  - [`Section 8-10: Tests, Borrowed Discipline & Non-goals`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/kpi_intent_contract.md#L141-L162): Unit test criteria, alignment with intent-discovery discipline, and non-goals (no silent semantic defaults).
- **Inputs & Outputs**:
  - *Inputs*: KPI registry text, feature mappings, business definitions.
  - *Outputs*: `interns/generated/contracts/kpi_intent_contract.json`, resolved-intent markdown reports, `intent_coverage` validation rules.
- **Failure Modes & Edge Cases**:
  - Fails code generation or review if a recorded decision (e.g. `within_department` denominator scope) is not realized in the generated SQL window function (`OVER (PARTITION BY ...)`).

---

### 2. [`pdf_ingestion.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/pdf_ingestion.md)

- **Exact Purpose**: Architecture specification for governed PDF document ingestion using `opendataloader-pdf` (pinned dependency v2.4.7), providing local, offline extraction of text, tables, headings, and lists into review-gated sidecar artifacts.
- **Key Sections & Content**:
  - [`Section 1 & 2: Problem & Core Principle`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/pdf_ingestion.md#L15-L35): Offline PDF extraction requirement (no PHI upload) and the core principle: "Extracted content is evidence, not authority" (sidecars created with `authoritative usage allowed: False`).
  - [`Section 3 & 4: Library Facts & Governed Tool`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/pdf_ingestion.md#L36-L56): `opendataloader-pdf` integration details, Java 11+ JVM requirement, local-safe `Free mode` (XY-Cut++) vs non-deterministic `Hybrid mode`, and `scan-document` CLI wrapper with graceful Java degradation.
  - [`Section 5: Artifacts`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/pdf_ingestion.md#L57-L66): Defines sidecar JSON structure (`<doc>.doc.json`), review panel markdown (`interns/reports/documents/current.md`), and pre-persistence PHI redaction.
  - [`Section 6: Content Classifier & Routing`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/pdf_ingestion.md#L67-L86): Routing table mapping detected PDF shapes (KPI tables, term definitions, ERD boxes, prose rules) to candidate platform artifacts (`kpi_registry.json`, `workspace_lexicon.json`, data-model sidecars, `open_questions.md`).
  - [`Section 7 & 8: Provenance, Gate Fit & Determinism`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/pdf_ingestion.md#L87-L98): Tagging extracted candidates with `{ source: document_pdf, page, bbox }` and enforcing exact sidecar reproduction via `source_sha256`.
  - [`Section 9-12: Phasing, Risks, Tests & Non-goals`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/pdf_ingestion.md#L99-L134): Phased implementation steps, Java 24 runtime checks, PHI redaction unit tests, and boundaries (does not replace XLSX/PNG pipelines).
- **Inputs & Outputs**:
  - *Inputs*: Input PDF documents (data dictionaries, KPI spec sheets, ERD exports).
  - *Outputs*: `interns/generated/documents/<doc>.doc.json`, `interns/reports/documents/current.md`, candidate proposed artifacts.
- **Failure Modes & Edge Cases**:
  - Gracefully degrades when Java 11+ is unavailable by emitting a clear blocker and skipping PDF scanning without failing workspace onboarding.
  - Requires profile referential integrity proof before any PDF-derived ERD join can be promoted to executable.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None. Both design documents describe active, implemented modules in `core/onboarding/kpi/intent_coverage.py` and `core/onboarding/documents/`.
- 🔌 **Unwired Components**: None. Both specs correspond to live runtime CLI utilities and test suites.
- 👯 **Duplication & Overlap**: Complementary alignment exists — `pdf_ingestion.md` handles document content extraction and candidate routing, supplying extracted intent evidence to `kpi_intent_contract.md`.
- ⚠️ **Mismatches & Risks**: `pdf_ingestion.md` relies on Java 11+ runtime. If Java is missing, PDF ingestion degrades gracefully while logging a clear notice. `kpi_intent_contract.md` ensures unconfirmed defaults or low-confidence facets emit targeted blocker questions instead of silent SQL defaults.
