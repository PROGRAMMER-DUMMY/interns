# ob-data_model — audit

## Purpose
`core/onboarding/data_model/` turns workspace data-model evidence into governed, review-gated
artifacts for the KPI/data-engineering control plane:

- **image_parser.py** — parses ER-diagram / schema images (e.g. `docs/DataModel.png`) into
  review-gated JSON sidecars. **It does NOT call a vision LLM.** The default path is local-safe:
  Tesseract OCR (optional, auto-detected) + a deterministic regex/heuristic schema parser that
  extracts tables, columns, and candidate fact→dim relationships, then matches each endpoint to
  workspace profiles. The remote multimodal-vision path is intentionally a stub
  (`provider_not_implemented`).
- **generation_workflow.py** — governed "create a data model when none exists" workflow that
  mirrors KPI generation: `prepare` (route panel) → `apply_answer` (entity inventory → risk review
  → final preview) → `finalize` (writes `data_model_contract.json` + docs), plus a separate
  blocker-panel sub-flow. Synthesizes tables/relationships/medallion contracts from profiles +
  text model docs.
- **data_understanding.py** — pure, side-effect-free classifier: medallion quality tier
  (raw/bronze, silver, gold), schema type (star/snowflake/galaxy/flat/OBT/3NF/hierarchical), and
  tier-scoped processing options.
- **`*_cli.py`** — argparse surfaces that delegate to the above; no business logic.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 26 | Re-exports generation workflow API | (exports only) |
| `data_understanding.py` | 753 | Deterministic tier/schema classifier (BUG-010 gate) | `classify_quality_tier`, `classify_schema_type`, `scoped_processing_options` |
| `data_understanding_cli.py` | 200 | CLI; loads profiles+rel contracts, writes `data_understanding/current.{json,md}` | `run_understand_data`, `_load_profiles`, `main` |
| `generation_workflow.py` | 2267 | Governed model creation/blocker/finalize workflow + draft synthesis + renderers | `DataModelGenerationWorkflow`, `_build_draft`, `_candidate_relationships`, `_promote_from_text_models`, `_readiness`, `_understanding_score`, `_model_blockers`, `_apply_one_operation` |
| `generation_cli.py` | 133 | argparse for prepare/apply/finalize/blocker | `prepare_main`, `apply_main`, `finalize_main`, `apply_blocker_main` |
| `image_parser.py` | 1426 | Local-safe image→sidecar parser (OCR + heuristics, NO vision LLM) | `DataModelImageParser`, `_run_local_ocr`, `_parse_ocr_schema_text`, `_infer_relationships`, `_match_schema_to_profiles`, `_generic_fact_table_profile_match` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [INTEGRATION] | image_parser.py:157-179, 467 vs `relationships/contracts.py:1288-1417`, :19 | **Governance-claim contradiction.** Every sidecar asserts `executable_usage_allowed=False`, `approval_state=needs_parser_or_manual_sidecar`, warning `image_derived_relationships_non_executable`, and "user approval must still pass". But `contracts.py:_relationships_from_diagram_sidecars` reads the same sidecars during onboarding and auto-emits `proven_data_model` edges (which are in `EXECUTABLE_RELATIONSHIP_STATES`) with **no human-approval gate** — solely on `profile_matched` + data-derived uniqueness/RI. Correctness is mitigated by the RI/uniqueness re-derivation, but the "non-executable until approved" promise printed to the user is false. | Either route diagram edges through `user_confirmed` (human gate) like the workflow's own `_approve_relationship`, or change the sidecar prose/`promotion_policy` to state that profile-matched diagram edges become executable automatically once RI passes. Align the two modules' governance language. |
| [NOT-PROD] | image_parser.py:471-516, 544-562, 630-676 | **OCR schema parser is brittle and silent.** Relationships are inferred from regex on raw OCR text (`\b(?:Dim|Fact)...`, PK/SK/FK token sniffing, suffix `id`/`code`). Garbled OCR (rotated/low-DPI ERDs, lines crossing boxes) yields wrong table/column tokens with no confidence floor or self-consistency check; failures degrade to "no headers detected" silently. Confidence values (0.68/0.82/0.86) are hardcoded magic constants, not measured. | Add a minimum-evidence gate (e.g. require ≥2 columns per detected table and a PK/FK marker before emitting a relationship), surface a low-confidence warning when OCR confidence is the fixed 0.5 placeholder, and document that OCR-only parsing is best-effort. |
| [NOT-PROD] | image_parser.py:324-358, 378-431 | `--auto-install-ocr` shells out to winget/choco/brew/apt with `subprocess.run(... timeout=900)`. A 15-minute package-manager install triggered from a parse CLI is unsuitable for unattended/CI pipelines and can prompt or hang on some hosts. | Keep gated behind the explicit flag (it is), but document it as interactive-only; never enable in orchestrated runs. Onboarding correctly passes `auto_install_ocr=False`. |
| [BUG] | image_parser.py:309 | OCR `confidence` is a fixed placeholder `0.5 if text else 0.0` — Tesseract's actual per-word confidence (`tsv`/`--psm` output) is never read, so the number is meaningless. Downstream `confidence_by_element` is also always `{}`. | Parse `tesseract ... tsv` for real confidence, or rename the field to `text_present` to avoid implying a measured score. |
| [BUG] | generation_workflow.py:1138-1142 (`_load_pattern_library`) | `prepare()`/`_build_draft()` call `_load_pattern_library()` with no error handling; a missing/corrupt bundled `patterns.json` raises `JSONDecodeError`/`ValueError` and crashes the whole prepare flow with an unhelpful trace. Low likelihood (bundled file) but no graceful degradation. | Wrap in try/except and emit a clear error (or fall back to an empty pattern set with a warning) so the panel still renders. |
| [NOT-PROD] | generation_workflow.py:1289 (`_apply_model_operations`) | Operation idempotency key is `json.dumps(operation, sort_keys=True)`, which includes the `accepted_at` timestamp injected in `_operations_from_option`. Replaying the same logical operation at a different time is NOT deduped, so repeated applies can stack (e.g. duplicate `add_relationship`). | Dedupe on a stable subset of operation fields (exclude `accepted_at`/`note`/`source`). |
| [NOT-PROD] | image_parser.py:1101-1109, 688-699 | Residual hardcoded alias dicts (`deptid↔departmentid`, `payorid↔payerid`) in `_column_similarity` contradict the module's stated workspace-agnostic design (and the docstring at :688 that says synonyms should come from the workspace lexicon). Two healthcare-leaning aliases leaked back in. | Source aliases from `workspace_vocabulary.json` / accepted aliases instead of the inline dict. |
| [DUP] | image_parser.py:1378-1399 & generation_workflow.py:2191-2237 | `_rel`, `_repo_path`, `_norm`, `_relationship_id`, `_now`, `_dataset_terms` are duplicated near-verbatim across both modules (and partly in `data_understanding.py`'s `_norm_table`). | Extract to a shared `core/onboarding/_pathutil.py` / `_textutil.py`. |
| [INTEGRATION] | data_understanding.py:366-509 vs contract key shape | `classify_schema_type` tolerates many relationship key spellings (`left_dataset`/`from_table`/`from`...). Robust, but the canonical emitter (`relationships/contracts.py`) uses `left_dataset`/`right_dataset`; the legacy `from_table`/`to_table` branch is dead against the current contract and risks silently mis-classifying if a caller passes the workflow's draft (which uses `from_table`). | Confirm which producer feeds the CLI; if only `relationship_contracts.json`, narrow the accepted keys or add a test that the workflow draft shape is intentionally also supported. |
| [DEAD] | image_parser.py:441-458 (`_remote_status`) | The entire remote multimodal-vision branch is a permanent stub (`provider_not_implemented`); `allow_remote_vision`/`confirm_sensitive_upload` flags thread through `parse()` but can never produce a remote call. Not wired anywhere except defaults-off in onboarding. | Fine as a forward-compat placeholder, but mark clearly as unimplemented in `--help` so operators don't expect vision extraction. |

## Cross-package coupling
- **Consumes:** `core.storage.workspace_layout.WorkspaceLayout` (all paths); `tools.list_workspace_files`
  (detected-files listing); `interns/generated/profiles/profile_index.json` + `*.profile.json`
  (table/column synthesis, schema matching, tier classification); workspace `docs/` text models
  (`_promote_from_text_models`); bundled `patterns.json`; `core.onboarding.panel_contract.normalize_decision_panel`
  and `core.onboarding.workspace.flow` helpers (CLI reuses the gate's markdown/artifact shapers, so
  the standalone artifact is byte-compatible).
- **Produces (downstream consumers):**
  - `interns/generated/data_model_images/*.model.json` (sidecars) → consumed by
    **`core/onboarding/relationships/contracts.py:_relationships_from_diagram_sidecars`** to emit
    executable `proven_data_model` edges (gated by data-derived uniqueness/RI). This IS the
    image→relationship→medallion path. Sidecars are invoked from
    **`core/onboarding/workspace/onboarding.py:_parse_data_model_images`** (always local-safe;
    remote/upload flags forced `False`).
  - `interns/contracts/data_model_contract.json` (finalize) → consumed by
    **`contracts.py:~1153` (`finalized_data_model_contract` evidence)** to seed relationship edges.
  - `data_understanding/current.{json,md}` → mirrors the `core.onboarding.workspace.flow` gate.
- **Secret handling:** none required — there is no API key, header, or URL credential anywhere in
  this package. The only network-ish actions are local Tesseract `subprocess` calls and (gated)
  package-manager installs. Remote vision is unimplemented. No secret-exposure risk.

## Verdict
**Conditionally production-ready as a local-safe, deterministic onboarding scaffold; NOT a
vision-LLM parser.** The package's biggest strength is exactly what the prompt feared: it does
*not* hallucinate via a vision model — there is no vision LLM, no API key, and image-diagram
relationships are double-gated (profile-match in the parser, then real uniqueness/referential-
integrity re-derivation in `contracts.py`). The synthesis workflow is well-governed (human blocker
panels, name-only joins capped at confidence ≤50 and forced through confirm/reject/relabel,
explicit `--approve-final-preview`).

The chief issue is an **integration/governance mismatch**: the sidecars loudly promise
"non-executable until user approval," yet onboarding auto-promotes profile-matched diagram edges to
executable `proven_data_model` with no human gate. Correctness survives (data gates), but the
promise is misleading and should be reconciled. Secondary concerns are the brittle, silently-
degrading OCR heuristic parser with placeholder confidences, the heavyweight auto-install path, a
timestamp-poisoned idempotency key, residual hardcoded healthcare aliases, and util duplication.
Recommend: align governance language, harden the OCR evidence floor, and de-dup the path/text
helpers before treating image-derived relationships as authoritative.
