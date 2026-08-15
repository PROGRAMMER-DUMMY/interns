# Features Architecture Context: `core/onboarding/features`

This document provides an exhaustive, file-by-file reference for all components in [`core/onboarding/features`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features).

---

## Executive Overview & ASCII Architectural Model

The `features` module provides evidence-driven feature classification, KPI blocker prioritization, reusable expression extraction, derived-feature pattern search, evidence formatting, and stakeholder Markdown review generation. It bridges raw KPI text requirements and physical dataset schemas without relying on hardcoded domain vocabulary or LLMs.

```
┌─────────────────────────────┐      ┌───────────────────────────────┐
│     Expression Extractor    ├─────►│  Blocker Classifier & Search  │
│       (expression.py)       │      │  (blockers.py, search.py)     │
└──────────────┬──────────────┘      └──────────────┬────────────────┘
               │                                    │
               ▼                                    ▼
┌─────────────────────────────┐      ┌───────────────────────────────┐
│   Pattern Library & Engine  ├─────►│ Candidate Evidence Generator  │
│(derivation_patterns.json/py)│      │     (derived_evidence.py)     │
└─────────────────────────────┘      └──────────────┬────────────────┘
                                                    │
                                                    ▼
                                     ┌───────────────────────────────┐
                                     │  Markdown Review & Validator  │
                                     │     (derived_markdown.py)     │
                                     └───────────────────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/__init__.py#L1-L3)

- **Exact Purpose**: Package initialization docstring for feature derivation helpers.
- **Key Functions / Classes**: None (Package docstring only).
- **Inputs & Outputs**: None.
- **Failure Modes & Edge Cases**: None.

---

### 2. [`blockers.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/blockers.py#L1-L147)

- **Exact Purpose**: Priority scoring, risk classification (`financial_correctness`, `temporal_correctness`, `structural`, `business_semantics`), and join candidate inference for blocked KPI features.
- **Key Functions / Classes**:
  - [`prioritize_blockers(mapping, *, structural_hints)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/blockers.py#L11-L47): Clusters blocked or unconfirmed features across KPIs, ranks them by priority score (`risk_score * 100 + count`), and attaches recommended questions.
  - [`infer_join_candidates(features)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/blockers.py#L50-L71): Inspects feature source columns for join key suffixes (`id`, `code`) across multiple datasets to propose join candidates requiring uniqueness, null, and grain checks.
  - [`risk_class(feature, *, structural_hints)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/blockers.py#L106-L126): Assigns risk category using generic financial/temporal terms, suffix checks, and workspace-profiled table hints.
  - [`risk_score(risk)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/blockers.py#L128-L134): Maps risk class to integer weights (4=financial, 3=temporal, 2=business semantics, 1=structural).
  - [`question_for_feature(feature, risk)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/blockers.py#L137-L143): Formulates standardized question text for a given risk class.
  - [`normalize(value)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/blockers.py#L145-L147): Strips non-alphanumeric characters and lowercases input string.
- **Inputs & Outputs**:
  - *Inputs*: Mapping dictionary containing KPI features, optional `structural_hints` set.
  - *Outputs*: Sorted list of clustered blocker dicts, list of inferred join candidate dicts.
- **Failure Modes & Edge Cases**:
  - Empty or missing `kpis` array in mapping returns empty cluster list.
  - Non-string feature names raise `AttributeError` or convert to `"None"` via `str()`.

---

### 3. [`derivation_patterns.json`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_patterns.json#L1-L166)

- **Exact Purpose**: Declarative JSON library defining reusable derived-feature patterns (e.g., `calendar_year_from_datetime`, `encounter_duration_bucket_24h`, `age_years`, `age_band`, `net_paid_amount`, `cpt_family`, `provider_specialty`, `days_in_ar`).
- **Key Functions / Classes**: None (Static JSON database).
- **Inputs & Outputs**:
  - *Inputs*: Loaded by `derivation_search.py`.
  - *Outputs*: Schema containing pattern IDs, feature terms, candidate columns, required inputs, multi-dialect SQL/Polars templates, and clarification prompts.
- **Failure Modes & Edge Cases**:
  - Malformed JSON will cause `json.JSONDecodeError` upon load in `DerivationPatternSearcher`.

---

### 4. [`derivation_patterns.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_patterns.py#L1-L669)

- **Exact Purpose**: Pure Python heuristics and evidence-driven pattern detectors for complex derived features (duration buckets, temporal self-joins, Unit of Measure [UOM] normalization, and JSON leaf column promotion).
- **Key Functions / Classes**:
  - [`detect_duration_bucket(question, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_patterns.py#L276-L302): Detects duration cuts (e.g. over 24 hours) from question prose and pairs start/stop temporal columns in the same dataset.
  - [`detect_recurrence_within_window(question, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_patterns.py#L305-L337): Identifies recurrence questions (e.g. within N days of prior event) and builds self-join SQL EXISTS clauses.
  - [`detect_uom_normalization(question, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_patterns.py#L389-L459): Detects quantity columns with mixed physical unit codes (kg/lb/g, m/ft, l/gal) and constructs multi-branch CASE statements using physical unit conversion constants.
  - [`detect_json_leaf_promotion_candidates(feature_token, schema_index)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_patterns.py#L547-L639): Finds fields nested in JSON payload columns profiled as `is_nested_leaf` and emits promotion candidates for DuckDB, Spark SQL, and Polars.
  - [`detect_derivation_patterns(question, columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_patterns.py#L442-L660): Aggregates option candidates across all detectors.
- **Inputs & Outputs**:
  - *Inputs*: Question text strings, profiled column metadata list, schema index dict.
  - *Outputs*: Derived option dictionaries conforming to the derived-feature contract.
- **Failure Modes & Edge Cases**:
  - Unsafe identifier names in JSON leaf promotion raise `UnsafeIdentifierError` and candidate is safely skipped.
  - Missing temporal pairs or unparseable unit strings return `None`.

---

### 5. [`derivation_search.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_search.py#L1-L129)

- **Exact Purpose**: Search engine for querying `derivation_patterns.json` against feature names, column availability, and domain context without treating patterns as ground-truth proof.
- **Key Functions / Classes**:
  - [`DerivationSearchInput`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_search.py#L15-L20): Dataclass holding search parameters (feature, available columns, expression context, domain).
  - [`DerivationCandidate`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_search.py#L24-L48): Dataclass representing a matched pattern candidate with confidence score and evidence gaps.
  - [`DerivationPatternSearcher(patterns_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_search.py#L51-L102): Loads pattern JSON and executes term matching and scoring (`search(request, limit=5)`).
- **Inputs & Outputs**:
  - *Inputs*: `DerivationSearchInput` instance.
  - *Outputs*: List of ranked `DerivationCandidate` objects.
- **Failure Modes & Edge Cases**:
  - If `derivation_patterns.json` is missing or unreadable, `Path.read_text()` raises `FileNotFoundError`.

---

### 6. [`derived_evidence.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_evidence.py#L1-L355)

- **Exact Purpose**: Binds candidate derivation patterns to profile evidence, constructs synthetic demonstration examples, and formats reasoning contracts for blocker panels.
- **Key Functions / Classes**:
  - [`derived_feature_options(feature, candidate_patterns, schema_index, kpi, expression_context)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_evidence.py#L11-L73): Constructs full JSON-backed derived option objects.
  - [`derived_input_column(input_name, column, schema_index)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_evidence.py#L76-L96): Extracts profile evidence (sample values, value profile, reason) for a formula input.
  - [`iter_nested_leaf_entries(profile)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_evidence.py#L292-L317): Unified helper for yielding evidence dicts from `nested_leaf_columns`.
  - [`derived_feature_example(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_evidence.py#L197-L218): Generates mechanics-only synthetic input/output examples.
  - [`substitute_formula_values(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_evidence.py#L256-L266): Replaces formula column names with synthetic example values.
- **Inputs & Outputs**:
  - *Inputs*: Feature string, candidate pattern dicts, `schema_index`, `kpi` dict.
  - *Outputs*: List of fully validated derived feature option dictionaries.
- **Failure Modes & Edge Cases**:
  - Missing column evidence in `schema_index` falls back to empty evidence structures without failing.

---

### 7. [`derived_markdown.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_markdown.py#L1-L464)

- **Exact Purpose**: Converts strict derived-feature JSON option objects into human-readable Markdown review cards (`interns/reports/derived_feature_reviews/md/*.md`).
- **Key Functions / Classes**:
  - [`DerivedFeatureMarkdownConverter`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_markdown.py#L50-L146): Reads `kpi_feature_mapping.json`, validates required schema fields, writes `.md` and `.json` reviews, and marks obsolete reviews stale.
  - [`_validate_option(option, kpi_id, option_idx)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_markdown.py#L149-L191): Strictly validates presence of all required option fields (`REQUIRED_OPTION_FIELDS`, `REQUIRED_INPUT_COLUMN_FIELDS`, etc.).
  - [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_markdown.py#L404-L460): CLI entry point; handles soft deprecation and redirects interactive calls to `prepare-kpi-blocker-panel`.
- **Inputs & Outputs**:
  - *Inputs*: `kpi_feature_mapping.json` contract file.
  - *Outputs*: Rendered Markdown files, JSON copies, index file, `DerivedFeatureMarkdownResult`.
- **Failure Modes & Edge Cases**:
  - If `--no-strict` is not passed and options lack required fields, raises `ValueError`.
  - Non-existent mapping file raises `FileNotFoundError`.

---

### 8. [`expression.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/expression.py#L1-L229)

- **Exact Purpose**: Extracts business identifiers and function calls from SQL or natural language metric/cut expressions while stripping literals and filtering SQL keywords/stopwords.
- **Key Functions / Classes**:
  - [`extract_expression(expression, *, workspace_filter_terms, known_columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/expression.py#L143-L204): Tokenizes expressions, filters out stopwords and percentile literals, while ensuring real physical columns (`known_columns`) survive extraction.
  - [`strip_literals(expression)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/expression.py#L207-L212): Strips single-quoted strings, double-quoted strings, and numeric literals from expression text.
  - [`ExtractedExpression`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/expression.py#L138-L140): Dataclass holding extracted identifier strings and function context dictionaries.
- **Inputs & Outputs**:
  - *Inputs*: Expression string, optional workspace filter vocabulary, optional known columns set.
  - *Outputs*: `ExtractedExpression` instance.
- **Failure Modes & Edge Cases**:
  - Unclosed quote strings are stripped up to end of line or match boundary.
  - Ambiguous words matching `known_columns` bypass stopword removal.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**:
  - `_PERCENTILE_LITERAL_RE` in [`expression.py:134`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/expression.py#L134) uses a limited regex `^p\d{1,3}$` that does not match decimal percentiles (e.g. `P99.5`), as noted in code comments.
  - `main()` in [`derived_markdown.py:404`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_markdown.py#L404) is soft-deprecated and acts as a wrapper redirecting to `prepare-kpi-blocker-panel`.
- 🔌 **Unwired Components**: None. All modules are actively imported and used by `onboard-workspace`, `prepare-kpi-blocker-panel`, and `validate-workspace-artifacts`.
- 👯 **Logic & Code Duplication**:
  - String normalization (`normalize`/`_norm`) is duplicated across [`blockers.py:145`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/blockers.py#L145), [`derivation_patterns.py:340`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_patterns.py#L340), [`derivation_search.py:127`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derivation_search.py#L127), and [`derived_evidence.py:8`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/derived_evidence.py#L8).
- ⚠️ **Broken References & Mismatches**: None found. All imported symbols (`PATTERNS_PATH`, `WorkspaceLayout`, `anchored`, `UnsafeIdentifierError`) resolve cleanly.
