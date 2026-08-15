# Data Model Architecture Context: `core/onboarding/data_model`

This document provides an exhaustive reference for all components in [`core/onboarding/data_model`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model).

---

## Executive Overview & Architectural Model

The `core/onboarding/data_model` package provides a governed, panel-driven control plane for data model understanding, drafting, risk review, blocker resolution, image diagram parsing, and document finalization.

It bridges workspace data profiling evidence (`*.profile.json`), inferred relationship contracts (`relationship_contracts.json`), design patterns (`patterns.json`), and diagram sidecars (`*.model.json`) into certified data models (`data_model_contract.json`), entity relationship diagrams (`erd.md`), and markdown documentation (`data-model.md`).

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                   CLI Entry Points                                      │
│  prepare-data-model-generation | apply-data-model-answer | finalize-data-model-generation │
│  prepare-data-model-blocker-panel | apply-data-model-blocker-answer | understand-data     │
└────────────┬─────────────────────────────┬──────────────────────────────┬───────────────┘
             │                             │                              │
             ▼                             ▼                              ▼
┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌───────────────────────────┐
│ data_understanding_cli.py   │ │   generation_cli.py         │ │  image_parser.py          │
└────────────┬────────────────┘ └──────────┬──────────────────┘ └─────────────┬─────────────┘
             │                             │                                  │
             ▼                             ▼                                  ▼
┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌───────────────────────────┐
│ data_understanding.py       │ │ generation_workflow.py      │ │ Tesseract OCR & Profiles  │
│ - Quality Tier (Bronze/Silv/ │ │ - Route & Risk Panels       │ │ - Review-Gated Sidecars   │
│   Gold)                     │ │ - Blocker Resolution        │ │ - Fuzzy Endpoint Match    │
│ - Schema Type (Star/Snow/   │ │ - Readiness & Score Engine  │ └───────────────────────────┘
│   Galaxy/3NF/Flat)          │ │ - Final Markdown & Docs     │
└─────────────────────────────┘ └──────────┬──────────────────┘
                                           │
                                           ▼
                                ┌─────────────────────┐
                                │    patterns.json    │
                                │  9 Design Patterns  │
                                └─────────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/__init__.py)

- **Exact Purpose**: Package initialization for `core.onboarding.data_model`. Defines the public API surface for data model generation workflows.
- **Key Functions / Classes**:
  - [`__all__`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/__init__.py#L15-L25): Re-exports [`DataModelBlockerPanelResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L41-L53), [`DataModelFinalizeResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L56-L66), [`DataModelGenerationResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L27-L38), [`DataModelGenerationWorkflow`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L68-L475), [`apply_blocker_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L2274-L2277), [`apply_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L2256-L2259), [`finalize_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L2262-L2265), [`prepare_blocker_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L2268-L2271), and [`prepare_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L2250-L2253).
- **Inputs & Outputs**:
  - *Inputs*: Import statements from `generation_workflow.py`.
  - *Outputs*: Module-level symbols for clean package imports.
- **Failure Modes & Edge Cases**:
  - Circular import or missing symbol in `generation_workflow.py` will fail package initialization.

---

### 2. [`data_understanding.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py)

- **Exact Purpose**: Pure, importable, side-effect-free data-understanding classifier (BUG-010 data-understanding gate). Analyzes workspace profile artifacts and inferred relationship contracts to classify data-quality tier, schema type, and scoped processing options.
- **Key Functions / Classes**:
  - [`classify_quality_tier(profiles: list[dict]) -> dict`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L176-L336): Classifies data quality tier (`raw/bronze`, `silver`, `gold`) based on cell null rates, primary key nulls, key uniqueness, string dtypes, and ingestion metadata columns.
  - [`classify_schema_type(profiles: list[dict], relationships: Any) -> dict`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L366-L510): Builds directed foreign key graph (`child -> parent`) to identify fact and dimension topology, detecting star, snowflake, galaxy, flat, OBT, 3NF, or hierarchical schemas.
  - [`scoped_processing_options(tier: str, profiles: Any, schema_type: str | None = None) -> list[dict]`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L561-L720): Returns the processing options relevant to the detected tier (e.g. type casting and dedup for bronze; denormalize and aggregate for silver; layout optimization for gold).
  - Helper functions: [`_profile_table_name(profile)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L83-L97), [`_columns(profile)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L100-L108), [`_row_count(profile)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L111-L116), [`_is_key_like(name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L131-L148), [`_distinct_count(col)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L159-L171), [`_rel_child(rel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L518-L526), [`_rel_parent(rel)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L529-L537).
- **Inputs & Outputs**:
  - *Inputs*: Profile dictionaries from `profile_index.json` or `*.profile.json`, relationship contract dicts.
  - *Outputs*: Tier result dict (`tier`, `confidence`, `evidence`), schema result dict (`schema_type`, `confidence`, `evidence`), scoped processing options list.
- **Failure Modes & Edge Cases**:
  - Empty profile list defaults conservatively to `raw/bronze` (confidence 0.1) and `unknown` schema type (confidence 0.1).
  - Unmeasured null or distinct counts lower confidence rather than throwing exceptions.

---

### 3. [`data_understanding_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding_cli.py)

- **Exact Purpose**: Standalone CLI tool (`understand-data`) for the data-understanding gate. Loads workspace profiles and relationship contracts, invokes `data_understanding.py` classifiers, writes `interns/reports/data_understanding/current.json` and `current.md`, and prints output summaries.
- **Key Functions / Classes**:
  - [`_load_profiles(layout: WorkspaceLayout) -> list[dict[str, Any]]`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding_cli.py#L49-L69): Loads profiles from `profile_index.json` or falls back to `*.profile.json` files in `profiles_dir`.
  - [`run_understand_data(repo_root: str | Path, workspace: str | Path) -> dict[str, Any]`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding_cli.py#L72-L145): Orchestrates loading, classification, report construction, and artifact writes.
  - [`main(argv: list[str] | None = None) -> int`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding_cli.py#L150-L198): CLI entry point decorated with `@anchored("understand-data")`.
- **Inputs & Outputs**:
  - *Inputs*: `--workspace`, `--repo-root`, `--json`, `--quiet` CLI options.
  - *Outputs*: Artifacts `interns/reports/data_understanding/current.json` and `current.md`, exit code 0.
- **Failure Modes & Edge Cases**:
  - Missing workspace folder causes `FileNotFoundError`. Invalid JSON in profile files returns empty profiles.

---

### 4. [`generation_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_cli.py)

- **Exact Purpose**: Governed CLI entry points for data model generation workflow steps. Wraps workflow methods with `run_workspace_command` for locking, telemetry, cost ledger tracking, and idempotency.
- **Key Functions / Classes**:
  - [`_workflow(repo_root: str, workspace: str) -> DataModelGenerationWorkflow`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_cli.py#L12-L13): Instantiates the workflow object.
  - [`prepare_main(argv: list[str] | None = None) -> int`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_cli.py#L18-L29): `@anchored("prepare-data-model-generation")` handler.
  - [`apply_main(argv: list[str] | None = None) -> int`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_cli.py#L33-L67): `@anchored("apply-data-model-answer")` handler; parses `--operation` JSON strings.
  - [`finalize_main(argv: list[str] | None = None) -> int`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_cli.py#L71-L95): `@anchored("finalize-data-model-generation")` handler.
  - [`prepare_blocker_main(argv: list[str] | None = None) -> int`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_cli.py#L99-L110): `@anchored("prepare-data-model-blocker-panel")` handler.
  - [`apply_blocker_main(argv: list[str] | None = None) -> int`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_cli.py#L114-L139): `@anchored("apply-data-model-blocker-answer")` handler.
- **Inputs & Outputs**:
  - *Inputs*: Command-line arguments (`--workspace`, `--answer`, `--operation`, `--approve-final-preview`, `--allow-replay`).
  - *Outputs*: Exit code (0 for success) and CLI envelope JSON responses.
- **Failure Modes & Edge Cases**:
  - `json.JSONDecodeError` if `--operation` JSON strings are invalid. Exit code 2 on workspace lock timeout.

---

### 5. [`generation_workflow.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py)

- **Exact Purpose**: Core engine for governed data model generation, parsing, risk review, blocker management, readiness/understanding scoring, and doc finalization.
- **Key Functions / Classes**:
  - [`DataModelGenerationResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L27-L38), [`DataModelBlockerPanelResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L41-L53), [`DataModelFinalizeResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L56-L66): Dataclasses representing stage output states.
  - [`DataModelGenerationWorkflow`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L68-L475): Main workflow manager class:
    - [`prepare()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L74-L103): Scans workspace files and model docs, builds session, writes initial route panel.
    - [`apply_answer(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L105-L166): Applies panel decisions, transitions stages (`route_selection` -> `entity_inventory` -> `risk_review` -> `final_preview`).
    - [`prepare_blocker_panel()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L168-L178): Prepares highest-rank model blocker panel (primary key, grain, temporal anchor, SCD, or unconfirmed join).
    - [`apply_blocker_answer(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L180-L216): Applies blocker answer operation, updates model draft, and re-evaluates blockers.
    - [`finalize(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L218-L257): Requires `--approve-final-preview`, writes `data_model_contract.json` and user-facing docs in `docs/`.
  - Core logic helpers: [`_build_draft`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L259-L316), [`_table_from_profile`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L650-L702), [`_candidate_relationships`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L851-L894), [`_apply_model_operations`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L1298-L1318), [`_readiness`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L1529-L1575), [`_understanding_score`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L1599-L1665), [`_model_blockers`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L1743-L1766), [`_shared_key`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L2129-L2155).
- **Inputs & Outputs**:
  - *Inputs*: Profile index (`profile_index.json`), doc models, image models, `patterns.json`, user answers and structured JSON operations.
  - *Outputs*: `data_model_draft.json`, `data_model_contract.json`, reports in `interns/reports/data_model_generation/`, docs in `docs/data-model.md`, `docs/erd.md`, `docs/relationships.md`.
- **Failure Modes & Edge Cases**:
  - `PermissionError` if `finalize()` called without `approve_final_preview=True`.
  - `FileNotFoundError` if attempting to finalize or apply answers without an existing draft contract.

---

### 6. [`image_parser.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py)

- **Exact Purpose**: Local-safe ERD/diagram/medallion image parsing scaffolds. Generates review-gated sidecar files (`*.model.json` and `*.model.md`) from image files using local Tesseract OCR (with auto-installation support) and profile matching. Keep relationships non-executable until reviewed and approved.
- **Key Functions / Classes**:
  - [`DataModelImageParseResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L29-L42): Summary dataclass for image parsing.
  - [`DataModelImageParser`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L44-L239): Core manager class:
    - [`parse(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L50-L106): Discovers images in `docs/`, builds sidecars and report index.
    - [`_build_sidecar(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L108-L180): Extracts OCR text, parses tables/columns/relationships, matches profiles, builds governance state.
  - OCR & Installation: [`_run_local_ocr`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L242-L279), [`_run_tesseract`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L281-L313), [`_attempt_install_tesseract`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L325-L359), [`_find_tesseract`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L362-L376), [`_tesseract_install_command`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L379-L431).
  - Parsing & Profile Matching: [`_parse_ocr_schema_text`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L462-L529), [`_match_schema_to_profiles`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L767-L833), [`_generic_fact_table_profile_match`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L906-L964), [`_add_relationship_endpoint_profile_matches`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L801-L1108).
  - CLI entry point: [`main(argv: list[str] | None = None) -> int`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L1444-L1465) decorated with `@anchored("parse-data-model-images")`.
- **Inputs & Outputs**:
  - *Inputs*: Workspace image files (`.png`, `.jpg`, `.jpeg`, `.svg` under `docs/`), profile index.
  - *Outputs*: Sidecar files in `interns/generated/data_model_images/`, report files and review index in `interns/reports/data_model_images/`.
- **Failure Modes & Edge Cases**:
  - Unconfigured Tesseract OCR yields `provider_not_configured` status without crashing.
  - Remote vision calls are blocked unless both `--allow-remote-vision` and `--confirm-sensitive-upload` flags are explicitly set.

---

### 7. [`patterns.json`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/patterns.json)

- **Exact Purpose**: Standard design pattern library definitions for data modeling (9 patterns: `transaction_fact`, `dimension_scd2`, `date_dimension`, `bridge_table`, `audit_event_log`, `periodic_snapshot_fact`, `data_vault_hub_link_satellite`, `medallion_layered_model`, `semi_structured_json`).
- **Key Structure**:
  - JSON schema version 1 containing array of pattern objects ([L1-L125](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/patterns.json#L1-L125)). Each item specifies `pattern_id`, `label`, `use_when`, `required_decisions`, `evidence_requirements`, `default_checks`, `anti_pattern_warnings`, and `downstream_unlocks`.
- **Inputs & Outputs**: Loaded by `generation_workflow.py` (`_load_pattern_library`).
- **Failure Modes & Edge Cases**: Malformed JSON or missing required fields will trigger validation errors in `generation_workflow.py`.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**:
  - Redundant CLI main wrappers in `generation_workflow.py` ([L2249-L2277](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L2249-L2277)) re-import and call `generation_cli.py` functions, creating an unnecessary circular indirection loop.
- 🔌 **Unwired Components**:
  - `image_parser.py` contains `_remote_status` ([L442-L460](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L442-L460)) which returns `"provider_not_implemented"` when `--allow-remote-vision` and `--confirm-sensitive-upload` are passed; the remote multimodal vision provider integration is an unwired stub.
- 👯 **Logic & Code Duplication**:
  - String normalization helper `_norm(val)` is duplicated in `generation_workflow.py` ([L2222-L2223](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L2222-L2223)) and `image_parser.py` ([L1276-L1277](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L1276-L1277)).
  - `_now()` timestamp generator is duplicated across `generation_workflow.py` ([L2246-L2247](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/generation_workflow.py#L2246-L2247)) and `image_parser.py` ([L1440-L1441](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/image_parser.py#L1440-L1441)).
  - Snake-casing and string sanitization logic is repeated across `generation_workflow.py` (`_snake`, `_safe_name`) and `image_parser.py` (`_safe_stem`, `_normalize_table_name`).
- ⚠️ **Broken References & Mismatches**:
  - `data_understanding.py` uses distinct key heuristics `_DISTINCT_KEYS` ([L55](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L55)) and `_NULL_KEYS` ([L56](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/data_understanding.py#L56)) which expect profile dict keys to match specific strings; if profilers output non-standard keys (e.g. `null_percent`), the null rate calculation defaults to `None` and lowers confidence.
