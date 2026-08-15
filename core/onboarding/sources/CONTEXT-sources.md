# Sources Architecture Context: `core/onboarding/sources`

This document provides an exhaustive reference for all components in [`core/onboarding/sources`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources).

---

## Executive Overview & Architectural Model

The `core/onboarding/sources` package manages external data intake, source catalog ingestion, and resource-bounded discovery across API endpoints, local files, external directories, and Databricks Unity Catalog assets.

It provides a multi-stage workflow engine for external source discovery (`ExternalSourceDiscoverer`), source catalog planning/preflight/ingestion (`SourceCatalogManager`), and panel-driven workspace routing (`ExternalSourceIntakeWorkflow`). All actions adhere to strict SSRF security controls, resource-manager budgets, and external-root allowlists (`external_data_roots.local.json`).

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                   CLI Entry Points                                      │
│  prepare-external-source-intake | apply-external-source-intake | prepare-source-catalog │
│  ingest-source-catalog | source-catalog | discover-external-sources                    │
└────────────┬─────────────────────────────┬──────────────────────────────┬───────────────┘
             │                             │                              │
             ▼                             ▼                              ▼
┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌───────────────────────────┐
│ external_intake_cli.py      │ │  catalog.py (CLI commands)  │ │ external_discovery.py CLI │
└────────────┬────────────────┘ └──────────┬──────────────────┘ └─────────────┬─────────────┘
             │                             │                                  │
             ▼                             ▼                                  ▼
┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌───────────────────────────┐
│ external_intake_workflow.py │ │ catalog.py                  │ │ external_discovery.py     │
│ - Route Selection (New/     │ │ - SourceCatalogManager      │ │ - ExternalSourceDiscoverer│
│   Attach/Custom)            │ │ - HostRateLimiter           │ │ - Bounded Scans           │
│ - Outcome & Group Scope     │ │ - SSRF Guard                │ │ - Format & Group Classif. │
│ - Team & Workspace Memory   │ │ - API/Local/UC Ingestion    │ │ - Draft Selection Gen.    │
└────────────┬────────────────┘ └──────────┬──────────────────┘ └─────────────┬─────────────┘
             │                             │                                  │
             └─────────────────────────────┴──────────────────────────────────┘
                                           │
                                           ▼
                                ┌─────────────────────┐
                                │ Workspace Layout &  │
                                │   Generated Artifacts│
                                └─────────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/__init__.py)

- **Exact Purpose**: Package initialization file for `core.onboarding.sources`.
- **Key Functions / Classes**: None (contains package docstring `"Source onboarding helpers."`).
- **Inputs & Outputs**: None.
- **Failure Modes & Edge Cases**: None.

---

### 2. [`catalog.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py)

- **Exact Purpose**: Governed source catalog planning, selection, preflight checking, API/local/UC ingestion, catalog indexing, term matching, and artifact validation. Enforces SSRF security rules, resource budgets, host rate-limiting, and external path allowlists.
- **Key Functions / Classes**:
  - [`ApiFetchPolicy`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L46-L74): Dataclass defining API fetch parameters (max pages, max rows, page size, max bytes, QPS, attempts, timeouts, backoff).
  - [`CatalogResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L78-L88): Dataclass encapsulating source catalog operation results.
  - [`HostRateLimiter`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L90-L104): Async per-host lock and rate-limiting coordinator.
  - [`SourceCatalogManager`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L106-L519): Main source catalog manager class:
    - [`plan()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L120-L133): Builds dry-run action plan from source selection and template configs.
    - [`ingest()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L135-L149): Executes approved source actions (API fetch, local copy/register, Databricks UC metadata export).
    - [`apply_stage(source_type, source_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L151-L175): Applies ingestion actions for a specific source type (`api`, `local`, `databricks_uc`).
    - [`preflight(source_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L177-L225): Runs pre-ingestion checks against disk budgets, URLs, and local paths.
    - [`discover_docs(source_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L227-L257): Discovers document links from fetched catalog payloads.
    - [`index_catalog(source_id, max_entries)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L259-L319): Indexes large catalog JSON/JSONL files into tokenized JSONL records.
    - [`match_catalog(source_id, keywords, min_score)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L321-L359): Matches indexed catalog entries against workspace terms and keywords.
    - [`draft_selection(source_id, min_score)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L361-L389): Generates `source_selection.generated.json` from catalog matches.
    - [`finalize_selection(source_id, approve_final_preview)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L391-L440): Promotes draft selection to `docs/source_selection.json`.
    - [`process(source_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L442-L476): Classifies and profiles materialized dataset files using Polars.
    - [`validate(strict)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L478-L516): Validates materialized paths, provenance files, and partial fetch errors.
  - Security & Helper Functions:
    - [`assert_url_egress_allowed(url)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L1183-L1211): SSRF guard enforcing public HTTP/HTTPS scheme and blocking loopback/private/link-local/metadata IPs.
    - [`_register_external_allowlist(path, source_id)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L723-L740): Thread-safe atomic update of workspace `dataset_allowlist`.
    - CLI entry points: [`plan_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L2065-L2071), [`ingest_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L2074-L2080), [`main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L2083-L2157).
- **Inputs & Outputs**:
  - *Inputs*: Selection JSON (`docs/source_selection.json`), catalog templates (`config/source_catalogs/*.json`), environment credentials.
  - *Outputs*: Plan/preflight/validation/index artifacts in `interns/generated/` and `interns/reports/`, materialized datasets in `datasets/` or `docs/`.
- **Failure Modes & Edge Cases**:
  - SSRF guard raises `RuntimeError` if egress URL resolves to private/loopback IP (unless `AUTORESEARCH_ALLOW_PRIVATE_EGRESS=1`).
  - Attempting to copy/register a local source outside allowed roots returns `blocked` status.
  - `finalize_selection` without `--approve-final-preview` raises `ValueError`.

---

### 3. [`external_discovery.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py)

- **Exact Purpose**: Bounded scanner and classifier for external data roots (local folders or S3/ADLS/GCS storage URIs via Universal Pathlib `upath`). Groups files into dataset, document, delta table, database, and log/system classes, and generates draft `source_selection.generated.json`.
- **Key Functions / Classes**:
  - [`ExternalFileClass`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L38-L47), [`ExternalGroup`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L50-L65), [`ExternalDiscoveryResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L68-L78): Dataclasses for discovery items and summary.
  - [`ExternalSourceDiscoverer`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L81-L334): Discovery engine class:
    - [`run()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L108-L164): Executes bounded file scanning (`bounded_external_files`), classifies files, forms groups, writes discovery JSON/Markdown and draft selection.
    - [`_classify(files)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L166-L195): Categorizes files by extension, directory path, and delta log presence.
    - [`_groups(classes)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L217-L246): Aggregates classified files into functional folder groups.
    - [`_draft_source_selection(groups)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L248-L308): Builds `source_selection.generated.json` for review.
    - [`_validate()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L310-L334): Enforces external-root allowlist policy and verifies folder existence.
  - Helper functions: [`_delta_roots`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L373-L379), [`_strategy_for`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L351-L368), [`main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L487-L504) decorated with `@anchored("discover-external-sources")`.
- **Inputs & Outputs**:
  - *Inputs*: External data root path or URI, `--max-files`, `--max-seconds`.
  - *Outputs*: Artifacts `external_source_discovery.json`, `external_source_discovery.md`, and `docs/source_selection.generated.json`.
- **Failure Modes & Edge Cases**:
  - Un-allowlisted external root path raises `PermissionError` ([L316](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L316)).
  - Reaching `max_files` or `max_seconds` flags `truncated=True` in output summary without crashing.

---

### 4. [`external_intake_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_cli.py)

- **Exact Purpose**: Governed CLI entry points for external source intake commands (`prepare-external-source-intake`, `apply-external-source-intake`). Resolves target workspace context and dispatches to `ExternalSourceIntakeWorkflow` via `run_workspace_command`.
- **Key Functions / Classes**:
  - [`_workflow_workspace_hint(args: argparse.Namespace) -> str`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_cli.py#L11-L20): Extracts workspace or proposed workspace string for lock keying.
  - [`prepare_main(argv: list[str] | None = None) -> int`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_cli.py#L25-L54): `@anchored("prepare-external-source-intake")` CLI handler.
  - [`apply_main(argv: list[str] | None = None) -> int`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_cli.py#L58-L122): `@anchored("apply-external-source-intake")` CLI handler; passes answer and workspace options.
- **Inputs & Outputs**:
  - *Inputs*: Command-line flags (`--external-root`, `--workspace`, `--proposed-workspace`, `--answer`, `--existing-workspace`, `--workspace-name`).
  - *Outputs*: CLI return code 0 and JSON summary output.
- **Failure Modes & Edge Cases**:
  - Missing `--external-root` or `--answer` causes `argparse` validation error. Executes un-locked if no workspace context is specified.

---

### 5. [`external_intake_workflow.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py)

- **Exact Purpose**: Multi-stage panel-driven workflow engine for external source intake decisions (`route_selection`, `route_change_reason`, `metadata_discovery`, `outcome_selection`, `source_group_selection`, `approval_gate`, `terminal`). Persists team preferences and workspace memory.
- **Key Functions / Classes**:
  - [`ExternalSourceIntakeResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L20-L30): Result summary dataclass.
  - [`ExternalSourceIntakeWorkflow`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L33-L339): Main intake workflow manager:
    - [`prepare()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L71-L95): Validates external root, loads team preferences, creates session, writes initial route panel.
    - [`apply_answer(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L97-L189): Applies panel decisions and advances workflow through route, outcome, group, and approval stages.
    - [`_apply_route(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L192-L232): Executes `ExternalSourceDiscoverer`, updates team and workspace memory.
  - Panel rendering helpers: [`_route_panel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L341-L386), [`_change_reason_panel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L389-L421), [`_outcome_panel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L424-L456), [`_source_group_panel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L459-L495), [`_approval_panel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L498-L526), [`_terminal_panel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L529-L542).
- **Inputs & Outputs**:
  - *Inputs*: `--external-root`, workspace options, panel answers, team memory (`state/team_memory/external_source_intake_preferences.json`).
  - *Outputs*: Session file `external_source_intake_session.json`, reports in `interns/reports/external_source_intake/`, workspace memory `external_source_intake_memory.json`.
- **Failure Modes & Edge Cases**:
  - Non-existent `external_root` raises `FileNotFoundError`.
  - Selecting `attach_existing` route without specifying `--existing-workspace` raises `ValueError`.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**:
  - `__init__.py` is completely empty except for docstring; no functions, classes, or `__all__` exported.
- 🔌 **Unwired Components**:
  - `catalog.py` contains `discover_docs` ([L227-L257](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L227-L257)) which parses URLs from JSON payloads via `_discover_doc_links`; however, `_discover_doc_links` only inspects file extension strings in URLs and does not extract or download document contents.
- 👯 **Logic & Code Duplication**:
  - String slugification and safe name conversion is duplicated across `catalog.py` (`_safe_name` on [L2044-L2046](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L2044-L2046)), `external_discovery.py` (`_safe_id` on [L422-L424](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L422-L424)), and `external_intake_workflow.py` (`_safe_slug` on [L680-L682](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L680-L682)).
  - Relative path formatting helper `_rel` is re-implemented in `catalog.py` ([L31](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L31)), `external_discovery.py` ([L427-L431](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_discovery.py#L427-L431)), and `external_intake_workflow.py` ([L689-L693](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/external_intake_workflow.py#L689-L693)).
- ⚠️ **Broken References & Mismatches**:
  - `catalog.py`'s `index_catalog` relies on `_catalog_array_offset` ([L1663-L1677](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/catalog.py#L1663-L1677)) searching for hardcoded keys (`dataset`, `datasets`, `resources`, `items`, `results`, `data`, `tables`). If a vendor catalog uses a different key name (e.g. `records` or `entities`), the streaming array offset fails and falls back to whole-file `json.loads` which errors out if > `max_single_json_bytes`.
