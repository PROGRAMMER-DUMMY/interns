# ob-documents — audit

## Purpose
Ingests workspace documents (PDFs, data dictionaries) and turns them into governed,
propose-only evidence. Two pipelines live here:

1. **PDF ingestion** (`document_loader.py` -> `classifier.py` -> `candidate_review.py`
   -> `candidate_apply.py`): scan a PDF via opendataloader-pdf (Java preflight, PHI
   redaction, deterministic free mode), classify structured content into propose-only
   candidates (KPI / lexicon / data-model / open-question / raw), present a human-review
   panel, and promote only with mandatory `--confirmed-by`. Nothing auto-mutates a
   contract; `data_model_candidate` stays non-executable even after acceptance.
2. **Dictionary-vs-profile reconciliation** (`dictionary_reconciliation.py`): treats the
   data dictionary as documentation evidence (not ground truth) and cross-checks every
   claim against profile statistics, emitting structured `dictionary_conflicts.json`.
   Error-severity conflicts demote proven KPI features to an answerable blocker.

Both pipelines are integration-confirmed (see Cross-package coupling).

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 8 | Package docstring only; no exports | — |
| `candidate_apply.py` | 464 | Accept/reject a document candidate; durable decision store; merge helper | `apply_document_candidate`, `merge_accepted_candidates`, `ApplyDocumentCandidateResult`, `_load_durable`, `main` |
| `candidate_review.py` | 397 | Build display-only human review panel from candidates.json | `prepare_document_candidate_review`, `_stable_candidate_id`, `_enrich`, `_render_markdown`, `main` |
| `classifier.py` | 698 | Propose-only block + whole-document classification of PDF JSON | `classify_document`, `detect_document_type`, `_classify_block`, `_classify_table_block`, `_iter_tree_nodes`, `_finalize_candidates` |
| `dictionary_reconciliation.py` | 935 | Dictionary claim vs profile reconciliation + conflict emission/application | `reconcile_dictionary_claims`, `write_dictionary_conflicts_contract`, `apply_conflicts_to_mapping`, `load_data_dictionary_rows`, `declared_enum_values`, `claimed_qualifiers`, `column_conflict_index`, `conflicts_for_source_column`, `phantom_conflict_index` |
| `document_loader.py` | 611 | PDF scan: Java preflight, lazy engine import, PHI redaction, review-gated sidecar | `scan_document`, `_check_java`, `_parse_java_major`, `_redact_json`, `_redact_text`, `DocumentScanResult`, `main` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | dictionary_reconciliation.py:655-666 (`_observed_code_values`) vs profiler `_sample_values` limit=8 | Reconciliation reads `sample_values`, which the profiler caps at 8 distinct values (`data_model_profiler.py:547`), but enum/qualifier checks treat them as the COMPLETE observed vocabulary (`_MAX_ENUM_OBSERVED_CARDINALITY=12`). A column with 9-12 real codes shows only 8 samples, so a declared enum value that exists in the data but falls outside the 8 samples is falsely reported as `undeclared_values` (warning) or contributes to a false `enum_mismatch`. Conflicts are derived from a truncated sample, not the true value set. | Have the profiler emit a `distinct_count` / `is_value_complete` flag (or raise the sample limit for low-cardinality code columns) and gate enum/unit conflicts on `distinct_count <= sample_len`; skip the check when the sample is known-truncated. |
| [BUG] | classifier.py:498-511 (`_extract_table_rows`) / 472-474 (`_extract_table_headers`) | Real-schema extraction is preferred only when it returns a TRUTHY value: a real opendataloader table whose data rows legitimately extract to `[]` (header-only table) or whose header text is all-empty falls through to legacy flat-key shapes and can mis-read `block["rows"]` (the raw row-node dicts) as data. Truthiness used as a "did the real parser apply" signal conflates "no real rows" with "not real schema". | Detect schema shape once (presence of `rows[0].type == "table row"`) and branch on that, not on truthiness of the extracted result. |
| [NOT-PROD] | document_loader.py:213-224 | After `opendataloader_pdf.convert`, only `json_files[0]` / `md_files[0]` are read and JSON parse errors are swallowed to `{}` with no warning. A multi-file or failed conversion silently yields an empty sidecar that still reports `status="ok"`; the only signal is the generic `no_content_extracted` warning. | Log/propagate the parse failure into `warnings` distinctly (e.g. `json_parse_failed`), and assert exactly one expected output file or record the count. |
| [NOT-PROD] | classifier.py:264-265, 284-285, 308 (overlap thresholds 0.15/0.20/0.15) | KPI/lexicon/ERD routing fires on header-token overlap >= 0.15-0.20 with no minimum table size. A 2-3 column table sharing one generic word (`definition`, `description`, `table`, `column`) trips the threshold; `definition` is in BOTH KPI and lexicon token sets, so a single column can emit two candidate types. False-positive candidates are bounded (propose-only, human-gated) but inflate the review panel. | Require a minimum header count or >=2 matched signals before routing; disambiguate the shared `definition`/`description`/`table`/`column` tokens or weight them lower. |
| [BUG] | classifier.py:305-307 (`full_text` join) | ERD `full_text` concatenation has no separator between the headers join and the rows join (`" ".join(headers) + " ".join(values)`), so the last header token and first row value fuse into one token (e.g. `"refsales"`), which can defeat the `\bref\b` / `\breferences\b` word-boundary regex in `_has_erd_signals`. | Add a space/separator between the two joins, or build a single token list and join once. |
| [NOT-PROD] | candidate_apply.py:169, candidate_review.py:141, document_loader.py:215-217 | `candidates.json` is read with bare `json.loads(...read_text())` with no try/except in `apply_document_candidate` and `prepare_document_candidate_review`; a corrupt/partial candidates.json raises an uncaught `JSONDecodeError` (CLI traceback) instead of a governed error result like the surrounding code returns elsewhere. | Wrap the read in try/except and return a `status="error"` result, matching the no-file branch already present. |
| [NOT-PROD] | candidate_apply.py:304-307, 356-360; dictionary_reconciliation.py:768; document_loader.py:200-205,217,223 | Several broad `except Exception: pass` / `-> {}` blocks (durable store load, merge result, opendataloader convert, raw json/md read). The convert/redaction path is defensible (degrade to blocker), but the durable-store and merge swallows can mask a corrupt `accepted_candidates.json`, silently discarding human-confirmed decisions on the next merge. | Narrow to `(json.JSONDecodeError, OSError)` and emit a warning when a non-empty file fails to parse, so destroyed human decisions are visible rather than silently empty. |
| [NOT-PROD] | dictionary_reconciliation.py:139-201 (`load_data_dictionary_rows`) | Dictionary CSV detection keys purely on filename containing `dictionary` (`*dictionary*.csv` glob + `"dictionary" in stem`). A dictionary CSV named otherwise (e.g. `data_glossary.csv`, `field_definitions.csv`) is never reconciled, so its false claims silently shape KPI logic — the exact failure this module exists to prevent. Encoding is fixed to `utf-8-sig` only; a non-UTF8/Latin-1 dictionary raises inside the `try` and is silently skipped (`except OSError` does not even catch `UnicodeDecodeError`, so it would actually propagate). | Broaden detection to shape (table+field columns present) not just filename; add an encoding fallback (utf-8-sig -> latin-1) and catch `UnicodeDecodeError` explicitly. |
| [BUG] | dictionary_reconciliation.py:199 (`except OSError`) | The loader's `try` wraps `csv.DictReader` iteration and `read`/decode; `UnicodeDecodeError` is a subclass of `ValueError`, NOT `OSError`, so a mis-encoded dictionary file is NOT caught here and will crash `load_data_dictionary_rows` (and the whole resolve-kpi-features run) despite the comment "Unreadable files contribute nothing." | Catch `(OSError, UnicodeDecodeError, csv.Error)`. |
| [NOT-PROD] | dictionary_reconciliation.py:298-308 (`_resolve_table`) | Suffix table resolution (`stem_norm.endswith(table_norm)` with `len>=3`) can match the wrong physical dataset when two datasets share a 3-char suffix (e.g. documented table `abc` matches both `xabc` and `yabc`), fanning a single dictionary row's conflicts across unrelated datasets. | Prefer the longest-suffix / unique match; if multiple datasets tie on suffix, emit a low-confidence ambiguity note rather than asserting conflicts against all. |
| [DUP] | candidate_apply.py:399-403, candidate_review.py:363-367, document_loader.py:476-480 | `_rel(path, root)` is reimplemented identically in three of the package files (and a fourth variant exists in dictionary_reconciliation.py:931). Minor drift risk. | Hoist a single `_rel` into a shared helper (e.g. workspace_layout or a package `_paths.py`). |
| [INTEGRATION] | dictionary_reconciliation.py — full chain | CONFIRMED wired: `feature_resolver.py:52-56,665-708` calls `load_data_dictionary_rows` + `write_dictionary_conflicts_contract` + `apply_conflicts_to_mapping`; demoted features get `resolution_type=dictionary_conflict`; `blocker_question_panel.py:476-480,689-758` builds a dedicated `_dictionary_conflict_question`; `validation.py:97,418-507` enforces the contract shape + no-tainted-ready-KPI gate. Not dead. | None — record as healthy. |
| [INTEGRATION] | classifier/loader/review/apply — full chain | CONFIRMED wired: `onboarding.py:266-360` runs `scan_document` + `detect_document_type` + `classify_document` and writes `candidates.json`; `merge_accepted_candidates` is consumed at onboarding.py:392,454,507 for KPI/lexicon/data-model merge; durable `accepted_candidates.json` is preserved across re-onboarding (onboarding.py:1676). Test coverage in 4 test files. Not dead. | None — record as healthy. |
| [MISSING] | document_loader.py:382-395 (`_redact_text`) / classifier inputs | PHI redaction runs in `scan_document` before write, but `classify_document` and `detect_document_type` are also callable directly on arbitrary `extracted_content`; there is no redaction guarantee at the classifier boundary. In the wired path the sidecar is already redacted, so this is latent, not active. | Document the precondition (input must be the redacted sidecar `extracted_content`) or redact defensively if classifier becomes a public entry point. |

## Cross-package coupling
- **`core.onboarding.kpi.feature_resolver`** — primary consumer of reconciliation: loads
  dictionary rows, writes the conflicts contract, applies conflicts to the KPI mapping
  (demotion). The reconciliation module is the producer; the resolver owns orchestration.
- **`core.onboarding.kpi.blocker_question_panel`** — renders demoted `dictionary_conflict`
  features into a dedicated answerable question (the blocker-panel feed the task asked to
  confirm — present and correct).
- **`core.onboarding.workspace.validation`** — validates `dictionary_conflicts.json` shape,
  recomputes summary counts to catch hand-edits, and enforces that no proven/ready KPI
  feature sits on an error-severity conflicted column.
- **`core.onboarding.workspace.onboarding`** — drives the PDF pipeline (`scan_document`,
  `detect_document_type`, `classify_document`) and merges accepted candidates into KPI /
  lexicon / data-model contracts.
- **`core.profiling.data_model_profiler`** — upstream evidence source; supplies `schema`,
  `row_count`, `columns[].sample_values`. The 8-value sample cap there is the root of the
  truncated-vocabulary [BUG] above. Note `profile_path` is read by `_dataset_views` but the
  profiler `to_dict()` does not emit it (defaults to "") — cosmetic, used only for display.
- **`core.contracts.versioning`** (`register_contract`), **`core.storage.workspace_layout`**
  (path resolution), **`core.governance.injection_guard`** (lazy prompt-injection
  neutralization), **`core.onboarding.kpi.pii_redaction`** (PII column detection with local
  fallback). All lazy/optional imports degrade gracefully.

## Verdict
**Conditionally production-ready.** The architecture is sound and unusually disciplined: the
reconciliation conflict model is conservative (only fires when evidence actively contradicts a
claim), the document pipeline is correctly propose-only with a mandatory human gate, durable
decisions survive re-onboarding, and both pipelines are genuinely wired end-to-end (no dead
code, blocker-panel feed confirmed). Test coverage is strong (30+ reconciliation tests, 4
document test files).

Two correctness bugs should block sign-off: (1) reconciliation derives enum/unit conflicts
from an 8-value truncated sample treated as the complete vocabulary, which produces false
`enum_mismatch`/`undeclared_values` conflicts on 9-12 cardinality columns — directly
undermining the module's purpose; (2) the dictionary loader catches only `OSError` while a
mis-encoded CSV raises `UnicodeDecodeError` (a `ValueError`), so it crashes the whole
resolve-kpi-features run rather than skipping the file as documented. The classifier
false-positive surface (low overlap thresholds, shared `definition` token, missing
header/row separator) is real but bounded by the human gate, so it degrades panel quality
rather than data integrity. Fix the two [BUG]s and narrow the silent excepts around the
durable candidate store before relying on this in production.
