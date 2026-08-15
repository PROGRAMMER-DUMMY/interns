# Documents Package Architecture Context: `core/onboarding/documents`

This document provides an exhaustive, file-by-file architectural and technical reference for all components in [`core/onboarding/documents`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents).

---

## Executive Overview & Architectural Model

The `documents` package provides a governed PDF and document ingestion pipeline:
1. **Phase 1 (`document_loader.py`)**: Java preflight, `opendataloader-pdf` extraction, PHI redaction, and review-gated sidecar generation.
2. **Phase 2 (`classifier.py` & `dictionary_reconciliation.py`)**: Propose-only content routing to candidate types (KPI, lexicon, data-model, open-question, raw evidence) and evidence reconciliation between data dictionaries and dataset profiles (`dictionary_conflicts.json`).
3. **Phase 3 (`candidate_review.py` & `candidate_apply.py`)**: Human-review panel generation (`candidates.md` / `candidates.json`) and mandatory human-attributed decision recording (`accepted_candidates.json`).

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       Source Document                                       │
│                                           (*.pdf)                                           │
└──────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                      document_loader.py                                     │
│                                       scan_document()                                       │
│  - Preflight Java 11+ & opendataloader-pdf check                                            │
│  - Redacts PHI/PII text & structured JSON before persistence                                │
│  - Emits sidecar (*.doc.json) & current.md report panel                                     │
└──────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                        classifier.py                                        │
│                                     classify_document()                                     │
│  - Propose-only document & block classification                                             │
│  - Routes tables & text to candidate types: KPI, Lexicon, Data Model, Open Question, Raw    │
│  - Emits candidates.json under generated/documents/                                         │
└──────────────────────┬───────────────────────────────────────────────┬──────────────────────┘
                       │                                               │
                       ▼                                               ▼
┌─────────────────────────────────────────────┐ ┌─────────────────────────────────────────────┐
│         dictionary_reconciliation.py        │ │             candidate_review.py             │
│        reconcile_dictionary_claims()        │ │     prepare_document_candidate_review()     │
│  - Detects enum, unit, phantom & misplaced  │ │  - Generates display panel (candidates.md)  │
│    conflicts between dictionary & profile   │ │    and machine-readable candidates.json     │
│  - Emits dictionary_conflicts.json          │ │  - Assigns stable candidate_ids             │
└─────────────────────────────────────────────┘ └──────────────────────┬──────────────────────┘
                                                                       │
                                                                       ▼
                                                ┌─────────────────────────────────────────────┐
                                                │             candidate_apply.py              │
                                                │         apply_document_candidate()          │
                                                │  - Enforces mandatory --confirmed-by        │
                                                │  - Appends human decisions to durable       │
                                                │    accepted_candidates.json                 │
                                                │  - Enforces non-executable policy on        │
                                                │    data_model_candidates                    │
                                                └─────────────────────────────────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/__init__.py)

- **Exact Purpose**: Sub-package initialization file summarizing the multi-phase PDF and document ingestion pipeline architecture.
- **Key Functions / Classes**: None (package docstring only).
- **Inputs & Outputs**:
  - *Inputs*: None.
  - *Outputs*: None.
- **Failure Modes & Edge Cases**: None.

---

### 2. [`candidate_apply.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_apply.py)

- **Exact Purpose**: Governed CLI runner and module (`apply-document-candidate`) for accepting or rejecting document-derived candidates. Enforces mandatory human confirmation (`--confirmed-by`) and persists decisions durably to `interns/generated/documents/accepted_candidates.json`.
- **Key Functions / Classes**:
  - [`ApplyDocumentCandidateResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_apply.py#L55-L96): Result class holding decision details, path, status, and summary method.
  - [`apply_document_candidate(repo_root, workspace, candidate_id, confirmed_by, reject, note)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_apply.py#L103-L272): Primary entry point. Refuses unconfirmed acceptances, matches `candidate_id` against `candidates.json`, builds decision record, marks `data_model_candidate` entries `executable: False` (requiring profile RI proof), and appends to `accepted_candidates.json`.
  - [`merge_accepted_candidates(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_apply.py#L279-L347): Integration seam for onboarding callers to read accepted decisions grouped by target contract type without directly mutating contract files.
  - Helper functions: [`_load_durable`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_apply.py#L353-L384), [`_empty_merge_result`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_apply.py#L387-L397), [`_rel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_apply.py#L400-L404), [`main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_apply.py#L412-L463).
- **Inputs & Outputs**:
  - *Inputs*: `candidates.json`, candidate ID, human reviewer identity (`--confirmed-by`), reject flag, note.
  - *Outputs*: `interns/generated/documents/accepted_candidates.json` and `ApplyDocumentCandidateResult`.
- **Failure Modes & Edge Cases**:
  - Accepting without `--confirmed-by` returns `status="refused"`.
  - Missing `candidates.json` or unresolvable `candidate_id` returns `status="error"`.
  - `data_model_candidate` entries are strictly set `executable: False` even when accepted, requiring downstream referential integrity proof.

---

### 3. [`candidate_review.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_review.py)

- **Exact Purpose**: Generates human-review panel artifacts (`candidates.md` and `candidates.json`) under `interns/reports/documents/` from `candidates.json`.
- **Key Functions / Classes**:
  - [`DocumentCandidateReviewResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_review.py#L55-L96): Result class containing paths, candidate counts, and status.
  - [`prepare_document_candidate_review(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_review.py#L103-L199): Reads `generated/documents/candidates.json`, enriches each entry with a stable candidate ID and routing label, and writes `reports/documents/candidates.json` and `candidates.md`.
  - [`_stable_candidate_id(candidate)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_review.py#L206-L220): Computes deterministic 12-char SHA-256 hash `doc_cand_<hash>` from candidate type, source document, page, and content repr.
  - [`_enrich(candidate)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_review.py#L223-L233): Adds stable ID and human-readable routing target string.
  - [`_render_markdown(workspace_rel, candidates, source_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_review.py#L240-L357): Generates display panel markdown grouped by candidate type.
  - Helper functions: [`_rel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_review.py#L364-L368), [`main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_review.py#L376-L395).
- **Inputs & Outputs**:
  - *Inputs*: `generated/documents/candidates.json`.
  - *Outputs*: `reports/documents/candidates.json` and `reports/documents/candidates.md`.
- **Failure Modes & Edge Cases**:
  - Missing `candidates.json` returns `status="no_candidates"` with instructional next steps.
  - Panel generation is strictly display-only and performs zero contract mutation.

---

### 4. [`classifier.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py)

- **Exact Purpose**: Propose-only document content classifier mapping structured PDF JSON sidecars into candidate records (`kpi_registry_candidate`, `lexicon_candidate`, `data_model_candidate`, `open_question_candidate`, `raw_evidence`).
- **Key Functions / Classes**:
  - [`classify_document(doc_json)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L101-L163): Primary entry point. Traverses opendataloader-pdf tree nodes or flat pages/blocks, routes tables and text snippets, tags whole-document type, applies confidence boosts, and deduplicates candidates.
  - [`_iter_tree_nodes(doc_json)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L169-L198): Flattens nested `kids` tree from opendataloader-pdf into typed nodes.
  - [`_classify_block(block, page)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L201-L245): Routes individual block elements (tables, ERD text, business rules).
  - [`_classify_table_block(block, page, bbox)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L248-L337): Evaluates table header token overlap against KPI, Lexicon, and ERD signal sets.
  - [`_extract_table_headers_real(block)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L413-L431): Extracts headers from real opendataloader-pdf 2.4.7 table node schemas (`table row` / `table cell`).
  - [`_extract_table_rows_real(block)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L434-L461): Extracts cell text values for data rows.
  - [`_extract_table_headers(block)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L464-L488) & [`_extract_table_rows(block)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L491-L511): Multi-schema extractors handling both real and synthetic fixture shapes.
  - [`detect_document_type(doc_json)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L590-L640): Whole-document classifier assigning types (`data_dictionary`, `kpi_spec`, `report`, `reference`, `mixed`).
  - [`_finalize_candidates(candidates, doc_json)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L643-L669): Applies document-type confidence boosts (`_DOC_TYPE_CONFIDENCE_BOOST = 0.15`) and deduplicates.
  - Helper functions: [`_make_candidate`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L346-L362), [`_extract_text`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L366-L374), [`_extract_cell_text`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L377-L401), [`_has_erd_signals`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L514-L525), [`_has_prose_rule_signals`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L537-L542), [`_deduplicate`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L672-L681).
- **Inputs & Outputs**:
  - *Inputs*: Extracted document JSON sidecar payload.
  - *Outputs*: List of propose-only candidate dicts with `authoritative: False` and `review_required: True`.
- **Failure Modes & Edge Cases**:
  - Unrecognized table structures degrade to `raw_evidence` with `confidence=0.30`.
  - Non-dict inputs return an empty list without raising exceptions.

---

### 5. [`dictionary_reconciliation.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py)

- **Exact Purpose**: Cross-checks workspace data dictionary claims against profile evidence, identifies evidence conflicts, emits `dictionary_conflicts.json`, and demotes tainted KPI feature mappings.
- **Key Functions / Classes**:
  - [`load_data_dictionary_rows(workspace, repo_root, layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L139-L202): Parses all data-dictionary-shaped CSVs into table/field/description rows.
  - [`reconcile_dictionary_claims(rows, profiles)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L215-L260): Primary reconciliation logic checking for `enum_mismatch`, `unit_mismatch`, `phantom_column`, `misplaced_column`, and `misattributed_claim`.
  - [`_enum_conflicts(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L357-L415): Detects when documented code enumerations (e.g., `X / Y / Z`) do not match observed code values in the dataset profile.
  - [`_qualifier_conflicts(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L418-L483): Detects when a documented single unit claim ("in kilograms") is contradicted by mixed unit values observed in a sibling column.
  - [`_attribution_conflicts(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L503-L575): Detects when a field description references another profiled dataset that carries its own same-named column.
  - [`declared_enum_values(description)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L594-L612): Extracts declared uppercase enum values from description text.
  - [`claimed_qualifiers(description, exclude)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L615-L652): Extracts single unit/qualifier claims in measurement contexts ("in <unit>", "per <unit>", "(<unit>)").
  - [`write_dictionary_conflicts_contract(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L720-L759): Generates and writes `dictionary_conflicts.json`.
  - [`apply_conflicts_to_mapping(mapping, conflicts)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L816-L865): Attaches conflict evidence to KPI feature mappings and demotes non-human-confirmed ready features to `blocked_ambiguous`.
  - Helper functions: [`load_dictionary_conflicts`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L762-L771), [`column_conflict_index`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L774-L786), [`conflicts_for_source_column`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L789-L795), [`phantom_conflict_index`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L798-L809).
- **Inputs & Outputs**:
  - *Inputs*: Workspace dictionary CSVs, `profile_index.json`, `kpi_feature_mapping.json`.
  - *Outputs*: `interns/generated/contracts/dictionary_conflicts.json` and updated feature mappings.
- **Failure Modes & Edge Cases**:
  - Missing profiled dataset for a documented table is ignored (absence of evidence is not a conflict).
  - Error-severity conflicts demote non-human-confirmed ready features to `blocked_ambiguous` (`resolution_type: dictionary_conflict`). `user_confirmed` features are never demoted.

---

### 6. [`document_loader.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py)

- **Exact Purpose**: Preflights environment (Java 11+ and `opendataloader-pdf`), converts PDFs to JSON/Markdown, applies PHI/PII redaction, and writes review-gated document sidecars (`*.doc.json`) and report panels (`current.md`).
- **Key Functions / Classes**:
  - [`DocumentScanResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L43-L59): Dataclass holding scan status, paths, SHA256 hashes, and next steps.
  - [`scan_document(repo_root, workspace, input_path, mode)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L86-L293): Primary entry point (`scan-document`). Validates mode, input path, source SHA256 hash, Java version, and `opendataloader_pdf` installation, runs PDF conversion in a temporary directory, applies PHI redaction, and writes the sidecar and markdown report.
  - [`_check_java()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L302-L359): Preflight check verifying `java` is on PATH and version is Java 11+.
  - [`_parse_java_major(version_output)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L362-L373): Regex parser for Java major version strings.
  - [`_redact_text(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L383-L396): Redacts SSNs, phone numbers, and email addresses using regex patterns, and neutralizes prompt-injection patterns via `injection_guard`.
  - [`_redact_json(obj)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L399-L407) & [`_redact_dict(obj)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L410-L430): Recursively redacts PHI column values in JSON payloads.
  - Helper functions: [`_local_is_pii_column`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L433-L438), [`_get_engine_version`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L445-L457), [`_sha256`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L464-L469), [`_render_report`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L522-L575), [`main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L583-L609).
- **Inputs & Outputs**:
  - *Inputs*: Input PDF file path, workspace path, mode (`free` | `hybrid`).
  - *Outputs*: Document sidecar `interns/generated/documents/<stem>.doc.json` and report `interns/reports/documents/current.md`.
- **Failure Modes & Edge Cases**:
  - Missing Java 11+ or missing `opendataloader-pdf` returns `status="blocker"` with installation instructions instead of raising unhandled exceptions.
  - Non-PDF inputs return `status="blocker"`.
  - `hybrid` mode without `AUTORESEARCH_ALLOW_HYBRID_PDF=1` environment variable returns `status="blocker"`.

### 7. [`docling_loader.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/docling_loader.py)

- **Exact Purpose**: Host-side entry point for Docling document extraction. Runs Docling in a **separate interpreter** and never imports it in-process, so `torch` and its model stack cannot resolve against the primary `.venv`'s pinned `pyspark<4` / `deltalake` / `numpy<2.0`. Complements (does not yet replace) the JVM-bound `opendataloader-pdf` path in `document_loader.py`.
- **Key Functions / Classes**:
  - [`can_parse_with_docling(root, runner=...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/docling_loader.py): Preflight. Returns `DoclingPreflight` with `available`, the resolved interpreter, the installed version, and — when unavailable — the exact platform-correct install command.
  - [`parse_document(input_path, root, timeout_s, runner=...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/docling_loader.py): Converts via the isolated runner. **Never raises for an unavailable or failing engine** — returns `ok=False` with `fallback_recommended=True` so callers keep using the existing text/table parser instead of failing the run.
  - [`resolve_docling_python(root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/docling_loader.py): Interpreter resolution, first match wins — `$AUTORESEARCH_DOCLING_PYTHON`, then `<repo>/.venv_docling`, else `None`.
  - `DoclingPreflight` / `DoclingResult`: Result dataclasses, each with a `summary()` for report rendering.
- **Inputs & Outputs**:
  - *Inputs*: Document path (PDF/DOCX/XLSX/image — Docling is not PDF-only). Optional `runner` injection so tests never spawn a real process.
  - *Outputs*: `DoclingResult` carrying Markdown plus **structurally extracted** tables (columns + rows).
- **Failure Modes & Edge Cases**:
  - No isolated env → `available=False` with `uv venv .venv_docling && uv pip install --python <interp> docling`.
  - Interpreter present but `import docling` fails, runner cannot start, timeout, or a payload that never lands → all return `fallback_recommended=True`, never an exception.
  - A missing **input file** is deliberately NOT a fallback (`fallback_recommended=False`) — that is a caller error, not engine absence.
- **Preflight**: `.venv/Scripts/python.exe core/onboarding/documents/docling_loader.py` prints the availability JSON; exit 1 when unavailable.

### 8. [`docling_runner.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/docling_runner.py)

- **Exact Purpose**: The half that executes **inside** the isolated Docling environment. Deliberately standalone — imports only `docling` and the stdlib, and is invoked **by path, never `-m`**, because the isolated env does not have this repo installed. Adding a `core.*` import here silently breaks the isolation boundary at runtime.
- **Key Functions / Classes**:
  - [`extract(input_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/docling_runner.py): `DocumentConverter().convert(...)` → Markdown + structured tables + engine version.
  - [`_table_payload(table, document, index)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/docling_runner.py): Calls `export_to_dataframe(doc=...)`, falling back to the legacy no-kwarg signature on `TypeError`.
- **Inputs & Outputs**:
  - *Inputs*: `<input> --out <json path>`.
  - *Outputs*: A JSON payload written to `--out` (never stdout — Docling emits model-loading chatter that would corrupt a piped payload). Exit 0 ok, 2 conversion failed, 1 could not write.
- **Failure Modes & Edge Cases**:
  - Tables are exported **structurally, not scraped from Markdown**: Docling's serialization docs state Markdown tables flatten row/col spans, so a spanned cell silently becomes empty. Markdown is kept for prose; tables are the structured truth.
  - One unparseable table records a per-table `error` rather than losing the whole document.
  - `export_to_dataframe` returns a pandas frame, but it never crosses the process boundary — it is converted to plain lists and serialized as JSON, so the repo-wide DataFrame Rule (Polars, no pandas) is unaffected.
- **Tests**: [`tests/onboarding/test_docling_loader.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/onboarding/test_docling_loader.py) — 14 cases covering preflight, isolation, fallback, timeout, and API-drift, all with an injected runner so CI needs no Docling install or model download.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**:
  - `_local_is_pii_column` in [`document_loader.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L433-L438) acts as a local fallback if `core.onboarding.kpi.pii_redaction` is missing.
- 🔌 **Unwired Components**:
  - None. All modules are wired to CLI entry points (`scan-document`, `prepare-document-candidate-review`, `apply-document-candidate`) or imported during onboarding feature resolution.
- 👯 **Logic & Code Duplication**:
  - `_rel` is reimplemented in [`candidate_apply.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_apply.py#L400-L404), [`candidate_review.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_review.py#L364-L368), [`dictionary_reconciliation.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L931-L935), and [`document_loader.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/document_loader.py#L477-L481).
  - Identifier string normalization (`_norm`) is redefined across [`classifier.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/classifier.py#L562-L563) and [`dictionary_reconciliation.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/dictionary_reconciliation.py#L674-L675).
- ⚠️ **Broken References & Mismatches**:
  - None. In [`candidate_apply.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/candidate_apply.py#L38), `read_json_or_quarantine` is correctly imported from `core.storage.atomic_io` to prevent corrupt JSON stores from silently zeroing out accepted human decisions.
