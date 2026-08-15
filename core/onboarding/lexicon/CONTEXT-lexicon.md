# Lexicon Package Architecture Context: `core/onboarding/lexicon`

This document provides an exhaustive reference for all components in [`core/onboarding/lexicon`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon).

---

## Executive Overview & Architectural Model

The `lexicon` package manages per-workspace vocabulary resolution and column alias derivation without relying on pre-baked or hardcoded domain keyword dictionaries. It extracts metric phrases, cut phrases, and column aliases directly from workspace evidence (authored KPI registries, profiles, accepted feature definitions, feature mappings, and human-confirmed PDF glossary candidates).

It serves two primary architectural roles:
1. **Lexicon Builder & Matcher (`builder.py`)**: Builds `interns/generated/contracts/workspace_lexicon.json` from workspace contracts and provides in-memory text matching (`WorkspaceLexicon`) for KPI metric/cut inference and schema alias resolution.
2. **Vocabulary Loader & Generic Fallbacks (`vocabulary.py`)**: Loads category-specific terms from `workspace_vocabulary.json` (produced during workspace research) and provides domain-neutral generic seed fallbacks when research has not yet run.

```
                  ┌─────────────────────────────────────────────────────────┐
                  │                 Workspace Artifacts                     │
                  │ (kpi_registry.json, profile_index.json,                 │
                  │  workspace_feature_definitions.json, PDF glossaries)    │
                  └────────────────────────────┬────────────────────────────┘
                                               │
                                               ▼
                               ┌───────────────────────────────┐
                               │  build_workspace_lexicon()    │  (builder.py)
                               └───────────────┬───────────────┘
                                               │
                                               ▼
┌─────────────────────────────┐  Writes  ┌───────────────────────────────┐
│     load_workspace_lexicon()│─────────►│    workspace_lexicon.json     │
└──────────────┬──────────────┘          └───────────────────────────────┘
               │
               ▼
┌─────────────────────────────┐
│      WorkspaceLexicon       │  (builder.py)
│  - infer_metric_and_cuts()  │
│  - aliases_for_column()     │
└─────────────────────────────┘

                               ┌───────────────────────────────┐
                               │   workspace_vocabulary.json   │ (Research artifact)
                               └───────────────┬───────────────┘
                                               │
                                               ▼
                               ┌───────────────────────────────┐
                               │       terms_for()             │  (vocabulary.py)
                               │  (Fallback to Generic Seeds)  │
                               └───────────────────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/__init__.py)

- **Exact Purpose**: Package initialization file defining the public API surface for the workspace lexicon builder and matcher components. Re-exports primary symbols from [`builder.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py).
- **Key Functions / Classes**:
  - [`WorkspaceLexicon`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/__init__.py#L27): Re-exported matcher class from [`builder.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L142-L208).
  - [`build_workspace_lexicon`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/__init__.py#L28): Re-exported builder function from [`builder.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L211-L291).
  - [`load_workspace_lexicon`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/__init__.py#L29): Re-exported contract loader function from [`builder.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L294-L304).
- **Inputs & Outputs**:
  - *Inputs*: Relative imports from `.builder` ([`L20-L24`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/__init__.py#L20-L24)).
  - *Outputs*: Explicit `__all__` list ([`L26-L30`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/__init__.py#L26-L30)).
- **Failure Modes & Edge Cases**:
  - Does not re-export helper functions from [`vocabulary.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py). Consumers requiring vocabulary terms must import directly from `core.onboarding.lexicon.vocabulary`.

---

### 2. [`vocabulary.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py)

- **Exact Purpose**: Workspace vocabulary loader with universal generic seed fallbacks (`GENERIC_FINANCIAL_SEED`, `GENERIC_IDENTIFIER_SUFFIXES`, `GENERIC_TEMPORAL_SEED`). Provides safe access to workspace-derived vocabulary categories without hardcoding domain dictionaries into Python code.
- **Key Functions / Classes**:
  - Re-exported Seeds ([`L71-L73`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py#L71-L73)): `GENERIC_FINANCIAL_SEED`, `GENERIC_IDENTIFIER_SUFFIXES`, `GENERIC_TEMPORAL_SEED` imported from [`core.onboarding.workspace.research`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/research.py).
  - [`_read_vocabulary(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py#L25-L33): Internal helper to locate and load `contracts/workspace_vocabulary.json`. Returns a dictionary or `{}` if absent/invalid.
  - [`terms_for(layout, category)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py#L36-L53): Returns derived terms for `"financial_terms"`, `"temporal_terms"`, `"entity_terms"`, `"filter_terms"`, or `"identifier_terms"`. Falls back to generic seeds when the category is empty or research has not been run.
  - [`vocabulary_confidence(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py#L56-L61): Extracts `(overall_confidence: float, needs_user_confirmation: bool)` from `workspace_vocabulary.json`. Defaults to `(0.0, False)`.
  - [`vocabulary_present(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py#L64-L67): Returns `True` if `workspace_vocabulary.json` exists on disk, `False` otherwise.
- **Inputs & Outputs**:
  - *Inputs*: `WorkspaceLayout` instance, category name strings, disk file `workspace_vocabulary.json`.
  - *Outputs*: String lists of vocabulary terms, tuple `(float, bool)` for confidence, boolean presence flag.
- **Failure Modes & Edge Cases**:
  - Malformed/Corrupt JSON in `workspace_vocabulary.json`: Catches `json.JSONDecodeError` and `OSError` ([`L31`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py#L31)) and returns `{}`.
  - Non-dictionary JSON root: Rejects non-dict values ([`L33`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py#L33)) and returns `{}`.
  - Unknown/unseeded category (e.g. `"entity_terms"`, `"filter_terms"` before research): Returns empty list `[]`.

---

### 3. [`builder.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py)

- **Exact Purpose**: Derives per-workspace lexicon contract (`workspace_lexicon.json`) from existing workspace artifacts (`kpi_registry.json`, `profile_index.json`, `workspace_feature_definitions.json`, `kpi_feature_mapping.json`, and human-accepted PDF glossary candidates from `merge_accepted_candidates`). Provides in-memory matching class `WorkspaceLexicon` for KPI metric/cut inference and column alias resolution.
- **Key Functions / Classes**:
  - Contract Registration ([`L67-L70`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L67-L70)): Defines `ARTIFACT_TYPE = "workspace_lexicon.json"`, `ARTIFACT_VERSION = 1`, and registers it via `register_contract`.
  - [`MetricPhrase`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L82-L97): Immutable dataclass holding phrase-to-metric mappings. `to_dict()` converts to contract JSON format.
  - [`CutPhrase`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L100-L115): Immutable dataclass holding phrase-to-cut/grain mappings. `to_dict()` converts to contract JSON format.
  - [`ColumnAliasEntry`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L118-L140): Mutable dataclass tracking column aliases across sources (`normalized`, `from_user_definitions`, `from_dictionary`, `sources`). `all_aliases()` merges all alias sets.
  - [`WorkspaceLexicon`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L142-L208): In-memory matcher class.
    - `__init__`: Sorts `metric_phrases` and `cut_phrases` by descending phrase length then alphabetically for greedy longest-first matching.
    - [`is_empty()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L168-L169): Returns `True` if no metric phrases, cut phrases, or column aliases are indexed.
    - [`infer_metric_and_cuts(name, description)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L171-L184): Normalizes text and matches against stored metric and cut phrases. Returns `(metric: str, cuts: str)`.
    - [`aliases_for_column(column)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L186-L191): Resolves all normalized aliases for a target column name.
    - [`to_dict()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L193-L208): Serializes `WorkspaceLexicon` into contract dict shape.
  - [`build_workspace_lexicon(layout, repo_root)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L211-L291): Primary builder entrypoint. Reads workspace contracts, invokes harvesting functions, computes stats, dedupes phrases and aliases, writes `workspace_lexicon.json`, and returns `WorkspaceLexicon`.
  - [`load_workspace_lexicon(layout)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L294-L304): Loads `workspace_lexicon.json` from `layout.contracts_dir`. Returns `None` if absent or unreadable.
  - [`_lexicon_from_payload(payload)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L307-L349): Deserializes contract dictionary into a `WorkspaceLexicon` instance.
  - [`_harvest_from_kpi_registry(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L352-L402): Extracts metric and cut phrases from authored KPI registry cells.
  - [`_harvest_from_profile_index(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L404-L429): Indexes column names and profile sources from `profile_index.json`.
  - [`_harvest_from_feature_definitions(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L431-L474): Carries user-accepted feature terms and metric targets from `workspace_feature_definitions.json`. Handles both string and dictionary column specifications (`{"column": "...", "dataset": "..."}`).
  - [`_harvest_from_feature_mapping(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L476-L513): Carries term-to-column aliases from proven/ready feature mappings in `kpi_feature_mapping.json`.
  - [`_glossary_header_role(header)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L523-L529): Classifies document table headers into `"term"` or `"definition"`.
  - [`_harvest_from_document_candidates(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L532-L603): Enriches existing column aliases with human-accepted PDF glossary terms from `merge_accepted_candidates(layout)`. Attaches terms strictly to existing profile columns (never invents new columns).
  - Private Helpers ([`L605-L705`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L605-L705)):
    - `_phrases_from_name(name)` ([`L605-L616`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L605-L616)): Extracts 1-to-4 token n-grams from name strings, stripping stopwords.
    - `_split_cuts(value)` ([`L619-L621`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L619-L621)): Splits cut strings by commas, semicolons, or newlines.
    - `_normalize_token(value)` ([`L624-L625`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L624-L625)): Alphanumeric lowercase token normalization.
    - `_normalize_text(value)` ([`L628-L629`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L628-L629)): Squashes spaces and lowercases text.
    - `_read_json(path)` ([`L632-L640`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L632-L640)): Reads JSON file safely, catching exceptions.
    - `_rel(path, base)` ([`L643-L647`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L643-L647)): Computes relative path string or falls back to POSIX path.
    - `_dedupe_metric_phrases(phrases)` ([`L650-L668`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L650-L668)): Merges duplicate metric phrases, combining KPI IDs and promoting confidence.
    - `_dedupe_cut_phrases(phrases)` ([`L671-L689`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L671-L689)): Merges duplicate cut phrases, combining KPI IDs and promoting confidence.
    - `_higher_confidence(a, b)` ([`L692-L694`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L692-L694)): Priority ordering: `user_confirmed` (3) > `authored` (2) > `derived` (1) > `""` (0).
    - `_dedupe_lower_preserve_case(values)` ([`L697-L705`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L697-L705)): Case-insensitive list deduplication preserving original casing.
- **Inputs & Outputs**:
  - *Inputs*: `WorkspaceLayout`, `repo_root` path, JSON contracts (`kpi_registry.json`, `profile_index.json`, `workspace_feature_definitions.json`, `kpi_feature_mapping.json`), accepted PDF document candidate store.
  - *Outputs*: Disk artifact `interns/generated/contracts/workspace_lexicon.json`, in-memory `WorkspaceLexicon` matcher object.
- **Failure Modes & Edge Cases**:
  - Empty Workspace / Missing Contracts: Missing input files are treated as empty inputs rather than errors. An empty workspace produces an empty `WorkspaceLexicon` to prevent unproven inference.
  - Unreadable Source Files: `_read_json` catches `Exception`, logs a warning (`lexicon_source_unreadable`), and continues with remaining sources.
  - PDF Document Candidate Import Failure: `_harvest_from_document_candidates` catches import errors and execution exceptions ([`L550-L556`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L550-L556)), logging warnings and skipping candidates without stopping lexicon build.
  - PDF Glossary Term Fallback Matching: Matches glossary terms to column names within definition text only when column length is >= 4 characters (`len(col_norm) >= 4`, [`L589`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L589)) to guard against short-token false positives.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**:
  - Unused Import: `lru_cache` imported in [`vocabulary.py:L13`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py#L13) is never used as a decorator on any function in that module.
- 🔌 **Unwired Components**:
  - Package Export Mismatch: [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/__init__.py) only exports `builder.py` symbols (`WorkspaceLexicon`, `build_workspace_lexicon`, `load_workspace_lexicon`). It does not re-export `terms_for`, `vocabulary_confidence`, or `vocabulary_present` from [`vocabulary.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/vocabulary.py). All external callers must import directly from `core.onboarding.lexicon.vocabulary`.
- 👯 **Logic & Code Duplication**:
  - Phrase Deduplication: [`_dedupe_metric_phrases`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L650-L668) and [`_dedupe_cut_phrases`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L671-L689) in `builder.py` share identical deduplication, KPI ID merging, and confidence promotion logic, differing only in target field (`metric` vs `cut`).
  - Token Normalization: Lowercase alphanumeric token normalization (`re.sub(r"[^a-z0-9]", "", value.lower())`) is redefined in [`builder.py:L624-L625`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/builder.py#L624-L625) (`_normalize_token`), duplicating similar token-cleaning helpers found across `core.onboarding`.
- ⚠️ **Broken References & Mismatches**:
  - None found. All contract fields, dataclass conversions, exception handlers, and internal function calls match signatures and disk reality.
