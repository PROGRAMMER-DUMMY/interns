# KPI Column-to-Concept Mapping Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two confirmed root causes behind wrong/noisy KPI feature-resolution blockers
(formula-vocabulary tokens mistaken for real features, and a miscalibrated fallback scorer that
can label a weak, generic match "confidence: high" and make it reachable via a one-word accept),
and add the two profiler signals (cardinality, value pattern) genuinely missing from the
resolution pipeline — per `docs/superpowers/specs/2026-08-03-kpi-column-concept-mapping-design.md`.

**Architecture:** No new files for production logic. Six tasks touching five existing files:
`core/onboarding/features/expression.py`, `core/onboarding/kpi/blocker_question_panel.py`,
`core/profiling/data_model_profiler.py`, `core/onboarding/kpi/feature_resolver.py`, and
`core/dev/resolver_accuracy.py`'s baseline fixture.

**Tech Stack:** Python 3.11, `unittest` (never pytest), Polars, DuckDB.

## Global Constraints

- **Test runner is `unittest`, never `pytest`.** A `PreToolUse` hook blocks `uv run pytest`. Use
  `.venv\Scripts\python.exe -m unittest <module>` or the portable gate `green-gate`.
- **The full gate's failure count must not increase after any task.** CORRECTED BASELINE
  (2026-08-03, independently re-verified by running the actual gate at the true plan-start commit
  6052f9d — the original "0 failing" claim below was never actually checked and was wrong):
  `.venv\Scripts\green-gate.exe` → `Green gate: 1648 tests, 2 failing` at plan start, both
  pre-existing and unrelated to this plan (tracked since commit `a56c1e6`, predating this plan
  entirely): `tests.regressions.test_json_nested_leaf_profiling` (ImportError: missing
  `iter_nested_leaf_entries` in `derived_evidence.py`) and
  `tests.regressions.test_profiler_tb_scale_csv_nullcount...test_null_count_is_correct_even_when_null_is_outside_the_sample_window`.
  Fixing either is explicitly OUT OF SCOPE for this plan (human decision) — do not attempt to
  implement nested-Struct/List leaf discovery or an always-full-file null_count change under this
  plan; that was tried once (Task 3, reverted) and is exactly what NOT to do here. A task in this
  plan is complete only when the gate shows exactly these same 2 failures and no others — verify by
  running the full `green-gate.exe` yourself and reading its own failure list, not by trusting a
  memorized "0 failing" expectation or a subagent's summary of its own run.
  ~~The full gate must stay green after every task: `.venv\Scripts\green-gate.exe` →
  `[ok] Green gate: 1642 tests, 0 failing` before this plan starts; must stay `[ok] all green`.~~
  (superseded by the corrected baseline above; struck through, not deleted, so the history of the
  mistake is visible)
- **No emojis in any output, report, or generated text.** Use ASCII markers `[ok]` / `[~]` / `[x]`.
- **Workspace-agnostic:** never hardcode against a workspace name, domain, or column vocabulary.
- **New regression tests go in `tests/regressions/`** — auto-discovered by the gate.
- **No new production files** — correct existing ones (explicit decision made during design).
- **Commit after every task.**

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `core/onboarding/features/expression.py` | Extend stopword/pattern filtering so formula vocabulary never becomes a fake "feature". | 1 |
| `core/onboarding/kpi/blocker_question_panel.py` | Recalibrate the fallback profile-scan's floor and "recommended" gating to its own real score scale. | 2 |
| `core/profiling/data_model_profiler.py` | Add `cardinality` and `value_pattern` to `ColumnProfile`, computed in the DuckDB CSV pushdown path. | 3 |
| `core/onboarding/kpi/feature_resolver.py` | Feed the two new signals into `_contextual_score`; raise the auto-proven bar for `financial_correctness`-risk features. | 4, 5 |
| `core/dev/resolver_accuracy.py` fixture | Re-baseline against real current labels. | 6 |

---

### Task 1: Fix the formula-vocabulary extraction gap

**Files:**
- Modify: `core/onboarding/features/expression.py:45-81` (`BUSINESS_TEXT_STOPWORDS`), `core/onboarding/features/expression.py:108-146` (`extract_expression`)
- Test: `tests/regressions/test_expression_formula_vocabulary_extraction.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `extract_expression(expression, *, workspace_filter_terms=None) -> ExtractedExpression` — same signature; `.identifiers` no longer contains formula-vocabulary noise.

- [ ] **Step 1: Write the failing test**

Create `tests/regressions/test_expression_formula_vocabulary_extraction.py`:

```python
"""Regression: formula/statistical vocabulary must never survive as a
KPI "feature" needing resolution.

Origin (2026-08-03 review): a live review of workspaces/rcm's 18 KPIs found
56 of 74 open blocker rows were English words extracted from ratio/
percentile/z-score/banded-tier formula text (e.g. "std", "High",
"benchmark"), several later mis-recommended at "confidence: high" against
an unrelated column. BUSINESS_TEXT_STOPWORDS did not cover this vocabulary,
and strip_literals() does not catch letter+digit tokens like "P95".

Deliberately NOT touched: "LOS" (Length of Stay). It looked like noise
alongside the others in the original review, but it is not -- it is a real,
domain-specific business concept with no column backing it in rcm, and it
must keep surfacing as an open question. Adding it to a stopword list would
also bake a healthcare-specific abbreviation into generic filtering logic,
which this platform's workspace-agnostic rule forbids. This is the line
between "formula glue with zero business meaning" (safe to filter,
generic across any domain) and "a real concept the resolver correctly
doesn't have data for" (must never be silently dropped).
"""
from __future__ import annotations

import unittest

from core.onboarding.features.expression import extract_expression

# Real metric/cuts text from workspaces/rcm/interns/generated/contracts/semantic_contract.json,
# kpi_004 through kpi_018.
REAL_KPI_TEXTS = [
    "(count of unplanned readmissions within 30 days) / (expected readmissions per diagnosis benchmark)",
    "count(ChargeAmount > P95 within ICD group) / count(all encounters in ICD group)",
    "(sum(PaidAmount) / sum(ChargeAmount)) - ContractedRate",
    "percentile_rank(count(distinct EncounterID) per provider, within Specialization peer group)",
    "sum(ChargeAmount) where (Claim_Date - Service_Date) is approaching or past the payor-specific filing limit",
    "count(claims resolved within 30 days, no resubmission) / count(distinct ClaimID)",
    "count(denied claims later paid within 120 days) / count(all initially denied claims)",
    "count(encounters with no matching transaction) / count(distinct EncounterID)",
    "weighted composite score (0-100), banded Low <33 / Medium 33-66 / High >66",
    "avg(ICD relative weight) per department per month",
    "flag = 1 if monthly (PaidAmount/ChargeAmount) falls outside [mean +/- 2 std dev], else 0",
    "avg(Actual LOS) / avg(Expected LOS benchmark) per ICD",
    "sum(PaidAmount) - sum(AdjustmentAmount) - allocated overhead per provider",
    "z-score = (current month volume - 3yr same-month avg) / 3yr same-month std dev",
    "count(distinct PatientID touching >2 departments within 90 days) / count(distinct PatientID with 2+ encounters in 90 days)",
]

CONFIRMED_BAD_TOKENS = {
    "within", "per", "all", "benchmark", "std", "dev", "actual", "at", "on",
    "track", "expected", "expired", "high", "low", "medium", "flag", "if",
    "mean", "outside", "falls", "score", "weight", "weighted", "touching",
    "unplanned", "p95",
}

REAL_COLUMN_TOKENS = {
    "chargeamount", "adjustmentamount", "contractedrate", "servicedate",
    "claimdate", "paidamount", "encounterid", "patientid", "claimid",
    "procedurecode", "specialization", "icdcode",
}


class FormulaVocabularyExtractionTests(unittest.TestCase):
    def test_no_confirmed_bad_token_survives_extraction(self):
        for text in REAL_KPI_TEXTS:
            with self.subTest(text=text):
                extracted = extract_expression(text)
                identifiers_lower = {token.lower() for token in extracted.identifiers}
                leaked = identifiers_lower & CONFIRMED_BAD_TOKENS
                self.assertFalse(
                    leaked,
                    f"formula-vocabulary tokens leaked as features: {leaked} from: {text!r}",
                )

    def test_real_column_names_still_extracted(self):
        # The fix must not become so aggressive it also swallows real columns.
        combined = " ".join(REAL_KPI_TEXTS)
        extracted = extract_expression(combined)
        identifiers_lower = {token.lower() for token in extracted.identifiers}
        missing = REAL_COLUMN_TOKENS - identifiers_lower
        self.assertFalse(missing, f"real column tokens no longer extracted: {missing}")

    def test_percentile_literal_token_is_filtered(self):
        extracted = extract_expression("count(ChargeAmount > P95 within ICD group)")
        self.assertNotIn("P95", extracted.identifiers)
        self.assertNotIn("p95", {t.lower() for t in extracted.identifiers})

    def test_los_still_surfaces_as_a_real_unresolved_feature(self):
        # LOS is a real business concept with no backing column in rcm -- it
        # must keep surfacing as an open question, never get silently
        # dropped as if it were formula-glue noise like "std"/"benchmark".
        extracted = extract_expression("avg(Actual LOS) / avg(Expected LOS benchmark) per ICD")
        self.assertIn("los", {t.lower() for t in extracted.identifiers})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_expression_formula_vocabulary_extraction -v`
Expected: FAIL — `test_no_confirmed_bad_token_survives_extraction` and
`test_percentile_literal_token_is_filtered` fail; tokens like `within`, `std`, `high`, `p95`
appear in `.identifiers`.

- [ ] **Step 3: Extend the stopword set and add a percentile-literal filter**

In `core/onboarding/features/expression.py`, extend `BUSINESS_TEXT_STOPWORDS` (currently
lines 45-81):

```python
BUSINESS_TEXT_STOPWORDS = {
    "a",
    "above",
    "across",
    "actual",
    "all",
    "an",
    "at",
    "average",
    "banded",
    "benchmark",
    "but",
    "confirm",
    "dev",
    "dimension",
    "dimensions",
    "divided",
    "expected",
    "expired",
    "falls",
    "filing",
    "flag",
    "for",
    "grain",
    "high",
    "highest",
    "if",
    "it",
    "last",
    "low",
    "mean",
    "medium",
    "metric",
    "minus",
    "multiplied",
    "next",
    "no",
    "number",
    "of",
    "on",
    "outside",
    "past",
    "per",
    "percentage",
    "plus",
    "score",
    "share",
    "that",
    "the",
    "this",
    "times",
    "top",
    "total",
    "touching",
    "track",
    "unplanned",
    "using",
    "trend",
    "unique",
    "weight",
    "weighted",
    "with",
    "within",
}
```

Add a percentile-literal pattern near the other module-level regexes and use it in
`extract_expression`'s token loop (currently lines 108-146):

```python
# A bare "P95"-style percentile-literal reference in formula prose ("ChargeAmount
# > P95 within ICD group") is not a column -- it names a statistical rank. It
# survives strip_literals() (a letter+digit token, not a pure-digit one) and the
# identifier regex (a valid Python-identifier shape), so it needs its own filter.
_PERCENTILE_LITERAL_RE = re.compile(r"^p\d{1,3}$", re.IGNORECASE)


def extract_expression(
    expression: str,
    *,
    workspace_filter_terms: list[str] | set[str] | None = None,
) -> ExtractedExpression:
    cleaned = strip_literals(expression)
    function_names = _function_names(cleaned)
    extra_stopwords: set[str] = set()
    if workspace_filter_terms:
        extra_stopwords = {str(term).lower() for term in workspace_filter_terms if term}
    identifiers = []
    seen = set()
    for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", cleaned):
        token_norm = token.lower()
        if (
            len(token) <= 1
            or token_norm in SQL_KEYWORDS
            or token_norm in COMMON_FUNCTIONS
            or token_norm in BUSINESS_TEXT_STOPWORDS
            or token_norm in extra_stopwords
            or token in function_names
            or _PERCENTILE_LITERAL_RE.match(token_norm)
        ):
            continue
        if token_norm not in seen:
            identifiers.append(token)
            seen.add(token_norm)
    return ExtractedExpression(
        identifiers=identifiers,
        functions=_function_contexts(cleaned),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_expression_formula_vocabulary_extraction -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Run the full gate**

Run: `.venv\Scripts\green-gate.exe`
Expected: `[ok] all green`. If any existing test fixture used one of the newly-added stopwords as
a deliberate test column name, adjust the fixture's column name, not the stopword list.

- [ ] **Step 6: Commit**

```bash
git add core/onboarding/features/expression.py tests/regressions/test_expression_formula_vocabulary_extraction.py
git commit -m "fix(kpi): formula/statistical vocabulary never survives as a feature

56 of 74 open blocker rows on a real workspace were English words tokenized
out of ratio/percentile/z-score/banded-tier formula text, not columns."
```

---

### Task 2: Recalibrate the panel's fallback scorer

**Files:**
- Modify: `core/onboarding/kpi/blocker_question_panel.py` (`_profile_candidate_options` around
  line 1192-1278, `_physical_option_payload` around line 1345-1387, and the call site around
  line 342-356/644-655 that decides `is_recommended`)
- Test: `tests/regressions/test_blocker_panel_fallback_confidence.py`

**Interfaces:**
- Consumes: `core.onboarding.kpi.blocker_question_panel._profile_candidate_score(feature, dataset, column, items) -> tuple[float, str]` (unchanged signature).
- Produces: no signature change; the fallback path (a) requires a real signal, not merely `score > 0`, to list a candidate; (b) never marks an option `is_recommended` unless the top score clears a real bar with a real margin over the runner-up; (c) `confidence` in `_physical_option_payload` reflects this function's own weight scale (100/60/30/20/−30), not a flat 6/3 bar tuned for a different scorer.

- [ ] **Step 1: Read the exact call site before editing**

`_profile_candidate_options` is only invoked as a fallback when the resolver found nothing
(`blocker_question_panel.py:355-356`: `if not physical_options: physical_options =
_profile_candidate_options(...)`). Read `blocker_question_panel.py:330-420` in full to find the
exact local variable name holding the sliced/rendered option list before `_physical_option_payload`
is called (seen at approximately line 647: `_physical_option_payload(option, idx, source_truth,
is_recommended=(idx == 1))`), and confirm the variable name to use in Step 3 below (referred to
as `top_options` in this plan; rename to match if the actual code differs).

- [ ] **Step 2: Write the failing test**

Create `tests/regressions/test_blocker_panel_fallback_confidence.py`:

```python
"""Regression: the panel's fallback profile scan must not label a weak,
generic match "confidence: high" or mark it recommended.

Origin (2026-08-03 review): _profile_candidate_options (the fallback that
fires when the resolver found nothing) scores candidates 100/60/30/20/-30,
but _physical_option_payload labeled confidence "high" at score>=6 -- a
single generic "text appears in the KPI's full text" hit (+20) cleared that
bar by 3x. Reproduced live: kpi_012's cut "Risk Tier (Low/Medium/High)" sits
beside "Department Name" in the same KPI; the garbage token "High" scored
+20 against departments.Name purely because "department" appears elsewhere
in the KPI's text, and was rendered RECOMMENDED at confidence: high.
"""
from __future__ import annotations

import unittest

from core.onboarding.kpi.blocker_question_panel import (
    _physical_option_payload,
    _profile_candidate_score,
)


class FallbackScorerCalibrationTests(unittest.TestCase):
    def test_generic_text_containment_alone_is_not_high_confidence(self):
        # "High" has no name/dataset relationship to departments.Name; the
        # +20 it can score comes only from "department" appearing elsewhere
        # in a shared KPI's text (kpi_text containment), never from anything
        # about "High" itself.
        items = [{"kpi": {
            "name": "Which self-pay balances are collectible",
            "description": "",
            "cuts": "Department Name, Risk Tier (Low/Medium/High)",
        }}]
        score, _reason = _profile_candidate_score("High", "departments.csv", "Name", items)
        self.assertLessEqual(score, 20)
        payload = _physical_option_payload({"score": score, "column": "Name", "dataset": "departments.csv"}, 1)
        self.assertNotEqual(
            payload["confidence"], "high",
            f"score={score} produced confidence=high; a single generic containment hit must cap at medium",
        )

    def test_partial_name_match_can_still_reach_high(self):
        # A genuine partial name match (+60) must still be able to reach
        # high confidence -- this is a calibration fix, not a ban on "high".
        items = [{"kpi": {"name": "", "description": "", "cuts": ""}}]
        score, _reason = _profile_candidate_score("chargeamnt", "claims.csv", "ChargeAmount", items)
        self.assertGreaterEqual(score, 60)
        payload = _physical_option_payload({"score": score, "column": "ChargeAmount", "dataset": "claims.csv"}, 1)
        self.assertEqual(payload["confidence"], "high")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_blocker_panel_fallback_confidence -v`
Expected: FAIL — `test_generic_text_containment_alone_is_not_high_confidence` fails because the
current `confidence = "high" if score >= 6 else ...` labels a score of 20 as `"high"`.

- [ ] **Step 4: Recalibrate `_physical_option_payload`'s confidence thresholds**

In `blocker_question_panel.py`, change (currently line 1359):

```python
    confidence = "high" if score >= 6 else ("medium" if score >= 3 else "low")
```

to:

```python
    # Recalibrated to THIS function's own weight scale (see
    # _profile_candidate_score: exact match +100, partial match +60, dataset
    # alignment +30, generic KPI-text containment +20, ID/generic-column
    # penalty -30) -- the previous 6/3 bar was tuned for a different scorer
    # entirely and let a single generic containment hit (+20) pass as "high".
    # A bare generic-containment-only score must cap at medium: name
    # similarity/context overlap alone is never sufficient evidence on its own.
    confidence = "high" if score >= 60 else ("medium" if score >= 20 else "low")
```

- [ ] **Step 5: Gate `is_recommended` on a real score-and-margin bar, not rank alone**

At the call site identified in Step 1 (the loop building payloads with
`is_recommended=(idx == 1)`), compute a real gate before the list comprehension:

```python
    top_score = float(top_options[0].get("score") or 0) if top_options else 0.0
    second_score = float(top_options[1].get("score") or 0) if len(top_options) > 1 else 0.0
    # A "recommended" label must survive the same discipline as an auto-proven
    # resolver match: a real bar (not merely being first in a list) and a real
    # margin over the runner-up. Mirrors feature_resolver.py's own auto-proven
    # check in spirit, scaled to this function's own score range.
    recommend_top = top_score >= 60 and (len(top_options) == 1 or top_score - second_score >= 20)
```

then change the comprehension's `is_recommended` argument from `(idx == 1)` to
`(idx == 1 and recommend_top)`.

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_blocker_panel_fallback_confidence -v`
Expected: PASS, 2 tests

- [ ] **Step 7: Run the full gate**

Run: `.venv\Scripts\green-gate.exe`
Expected: `[ok] all green`.

- [ ] **Step 8: Commit**

```bash
git add core/onboarding/kpi/blocker_question_panel.py tests/regressions/test_blocker_panel_fallback_confidence.py
git commit -m "fix(kpi): recalibrate the panel's fallback scorer to its own scale

confidence>=6 was tuned for a different scorer and let a single generic
text-containment hit (+20 of a 100/60/30/20 scale) pass as 'high' and
become the unconditional 'recommended' option regardless of margin."
```

---

### Task 3: Add cardinality and value_pattern to the profiler

**Files:**
- Modify: `core/profiling/data_model_profiler.py` (`ColumnProfile` dataclass at lines 86-99;
  `_profile_csv_duckdb` at lines 489+)
- Test: `tests/regressions/test_profiler_cardinality_and_value_pattern.py`

**Interfaces:**
- Consumes: nothing new (uses the DuckDB connection/table already opened in `_profile_csv_duckdb`).
- Produces: `ColumnProfile.cardinality_ratio: float | None` (unique_count / row_count),
  `ColumnProfile.value_pattern: str | None` (a named structural pattern, or `None`),
  `ColumnProfile.profile_tier: str = "raw"` (stamped on every profile; confirmed profiling
  only ever runs pre-medallion, against bronze-shaped source data).

**Naming note (2026-08-03, discovered mid-implementation, human-decided):** the field is named
`cardinality_ratio`, NOT `cardinality`. `core/onboarding/relationships/contracts.py`'s
`_ratio_from_stats` and `core/onboarding/data_model/data_understanding.py`'s `_DISTINCT_KEYS` both
already treat a `"cardinality"` dict key as a synonym for an ABSOLUTE distinct count (pre-existing,
documented, unrelated to this plan) — populating a `"cardinality"` key with a 0-1 ratio instead
collides with both: it silently breaks `contracts.py`'s relationship-executability ratio computation
(verified: `executable_relationship_count` drops from >=1 to 0 in
`tests.test_enterprise_optimization...test_kpi_sql_generator_joins_profiled_sources_instead_of_sparse_union`)
and would separately corrupt `data_understanding.py`'s `classify_quality_tier` via `int(ratio)`
truncation (untested, would fail silently in production). Do not touch either of those two files in
this task — the rename below avoids the collision entirely without touching pre-existing, working,
unrelated logic.

- [ ] **Step 1: Write the failing test**

Create `tests/regressions/test_profiler_cardinality_and_value_pattern.py`:

```python
"""Regression: the profiler must emit cardinality_ratio, value_pattern, and
profile_tier -- signals the KPI resolver needs and that did not exist
anywhere in the evidence chain before this fix (confirmed by reading
value_profile()/column_profile_summary() in derived_evidence.py, neither
of which carried them).
"""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from core.profiling.data_model_profiler import DataModelProfiler, _infer_value_pattern


class ProfilerNewSignalsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        path = Path(self.tmpdir.name) / "sample.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["ClaimID", "PayorType", "ChargeAmount"])
            for i in range(50):
                writer.writerow([f"CLAIM{i:06d}", "Commercial" if i % 2 else "Medicare", f"{100 + i}.50"])
        self.path = path

    def test_near_unique_column_reports_high_cardinality(self):
        profile = DataModelProfiler().profile_path(self.path)
        by_name = {col.name: col for col in profile.columns}
        self.assertGreater(by_name["ClaimID"].cardinality_ratio, 0.95)

    def test_low_cardinality_column_reports_low_cardinality(self):
        profile = DataModelProfiler().profile_path(self.path)
        by_name = {col.name: col for col in profile.columns}
        self.assertLess(by_name["PayorType"].cardinality_ratio, 0.2)

    def test_every_column_stamped_raw_tier(self):
        profile = DataModelProfiler().profile_path(self.path)
        for col in profile.columns:
            self.assertEqual(col.profile_tier, "raw")

    def test_currency_value_pattern_inferred(self):
        pattern = _infer_value_pattern(["100.50", "101.50", "102.50"])
        self.assertEqual(pattern, "currency_2dp")

    def test_no_pattern_below_threshold(self):
        pattern = _infer_value_pattern(["100.50", "abc", "2024-01-01"])
        self.assertIsNone(pattern)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_profiler_cardinality_and_value_pattern -v`
Expected: FAIL — `AttributeError` / `ImportError`, `cardinality_ratio`/`value_pattern`/`profile_tier`/`_infer_value_pattern` do not exist yet.

- [ ] **Step 3: Add the new fields to `ColumnProfile`**

In `core/profiling/data_model_profiler.py`, change (currently lines 86-99):

```python
@dataclass(frozen=True)
class ColumnProfile:
    name: str
    dtype: str
    nullable: bool | None = None
    sample_values: list[Any] = field(default_factory=list)
    sample_min: Any = None
    sample_max: Any = None
    exact_min: Any = None
    exact_max: Any = None
    metadata_min: Any = None
    metadata_max: Any = None
    null_count: int | None = None
    source: str = "schema"
    # Fraction of distinct values over row count (unique_count / row_count).
    # None where not computed (parquet-metadata and polars-fallback paths
    # do not populate it yet -- see Task 3 notes in the implementation plan).
    # Named cardinality_ratio, NOT cardinality: contracts.py/_ratio_from_stats
    # and data_understanding.py/_DISTINCT_KEYS both already treat a literal
    # "cardinality" key as an ABSOLUTE distinct count -- this field is a 0-1
    # ratio, so it must not collide with that pre-existing, unrelated key name.
    cardinality_ratio: float | None = None
    # Named structural pattern shared by >=80% of observed sample values
    # (see _infer_value_pattern), or None when no pattern clears that bar.
    value_pattern: str | None = None
    # Every profile is stamped "raw": profiling runs pre-medallion, against
    # bronze-shaped (pre-dedup -- bronze_silver_standards.py explicitly
    # forbids deduplication_application in bronze) source data, never
    # silver. A future silver re-profile can stamp "silver" and upgrade a
    # mapping's confidence cap; nothing does that yet.
    profile_tier: str = "raw"

    def authoritative_min(self) -> Any:
        return self.exact_min if self.exact_min is not None else self.metadata_min

    def authoritative_max(self) -> Any:
        return self.exact_max if self.exact_max is not None else self.metadata_max
```

- [ ] **Step 4: Add the value-pattern inference helper**

Add near the other module-level helpers in the same file:

```python
_VALUE_PATTERN_CHECKS: list[tuple[str, "re.Pattern[str]"]] = [
    ("currency_2dp", re.compile(r"^\d+\.\d{2}$")),
    ("iso_date", re.compile(r"^\d{4}-\d{2}-\d{2}$")),
    ("prefixed_numeric_code", re.compile(r"^[A-Za-z]+[-_]?\d+$")),
    ("fixed_length_alnum", re.compile(r"^[A-Za-z0-9]{6,12}$")),
]


def _infer_value_pattern(sample_values: list[Any]) -> str | None:
    """Named structural pattern shared by >=80% of non-null sample values.

    Evidence-driven, no domain vocabulary: matches shape (digits/letters/
    separators), never a specific business term. Returns None when no
    pattern clears the 80% bar -- a mixed-shape column reports no pattern
    rather than a misleading weak one.
    """
    values = [str(v) for v in sample_values if v is not None and str(v).strip()]
    if not values:
        return None
    for pattern_name, pattern in _VALUE_PATTERN_CHECKS:
        matches = sum(1 for v in values if pattern.match(v))
        if matches / len(values) >= 0.8:
            return pattern_name
    return None
```

Ensure `import re` is present at module level (it already is, used elsewhere in this file).

- [ ] **Step 5: Compute cardinality_ratio in the DuckDB CSV pushdown path**

In `_profile_csv_duckdb` (starting at line 489), add a full-file distinct-count pass over every
column as its own new, independent block — do NOT rename, restructure, or change the conditional
behavior of the pre-existing `exact_stats = (self._duckdb_column_stats(...) if exact else {})` line
or the existing `null_count=(...)` kwarg logic. (An earlier draft of this step assumed a variable
named `full_stats` already existed at this point in the file; it does not — only `exact_stats`,
computed conditionally on `exact`. That was this step's own mistaken assumption, not an instruction
to unify or always-run that computation. Leave `exact_stats`/`null_count` exactly as they are.)

```python
            distinct_selects = ", ".join(
                f"COUNT(DISTINCT {_quote_ident(name)}) AS {_quote_ident(name + '__distinct')}"
                for name in schema
            )
            distinct_row = conn.execute(f"SELECT {distinct_selects} FROM {source}").fetchone()
            distinct_counts = dict(zip([f"{name}__distinct" for name in schema], distinct_row))
```

Then in the per-column loop that builds `columns[name] = ColumnProfile(...)` (currently
starting at line 575), add the three new fields, leaving every existing kwarg (including
`null_count=...`) exactly as it already reads:

```python
                unique_count = distinct_counts.get(f"{name}__distinct")
                cardinality_ratio = (
                    (unique_count / row_count) if unique_count is not None and row_count else None
                )
                columns[name] = ColumnProfile(
                    name=name,
                    dtype=dtype_str,
                    # ... existing kwargs (null_count, sample_values, sample_min/max,
                    # exact_min/max, source) unchanged -- add only the three below
                    cardinality_ratio=cardinality_ratio,
                    value_pattern=_infer_value_pattern(sample_values),
                    profile_tier="raw",
                )
```

(Read the existing `ColumnProfile(...)` call at line 575 in full first and add these three
keyword arguments alongside what's already there — do not remove, reorder, or change any existing
kwarg.)

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_profiler_cardinality_and_value_pattern -v`
Expected: PASS, 5 tests

- [ ] **Step 7: Run the full gate**

Run: `.venv\Scripts\green-gate.exe`
Expected: `[ok] all green`. `asdict(col)` (used in `DatasetProfile.summary()`) picks up the three
new fields automatically since they're plain dataclass fields with defaults — no serialization
code changes needed.

- [ ] **Step 8: Commit**

```bash
git add core/profiling/data_model_profiler.py tests/regressions/test_profiler_cardinality_and_value_pattern.py
git commit -m "feat(profiling): add cardinality_ratio, value_pattern, profile_tier to ColumnProfile

Neither existed anywhere in the evidence chain (confirmed via
derived_evidence.value_profile()); the KPI resolver needs both as scoring
signals. Computed in the DuckDB CSV pushdown path; parquet/polars-fallback
paths report None for now (documented gap, not silently wrong). Named
cardinality_ratio (not cardinality) to avoid colliding with the pre-existing
absolute-distinct-count meaning of a literal 'cardinality' key in
relationships/contracts.py and data_model/data_understanding.py."
```

**Note on scope:** this task implements the three new fields for the DuckDB CSV pushdown path
only (`_profile_csv_duckdb`), which is what every workspace with CSV sources (including
`workspaces/rcm`) actually uses. The parquet-metadata and polars-fallback paths will report
`cardinality_ratio=None`/`value_pattern=None` until a follow-up extends them — Task 4's consumer must
treat `None` as "signal absent," never as zero/false.

---

### Task 4: Feed the new signals into `_contextual_score`

**Files:**
- Modify: `core/onboarding/kpi/feature_resolver.py:1298-1391` (`_contextual_score`)
- Test: `tests/regressions/test_contextual_score_new_signals.py`

**Interfaces:**
- Consumes: `entry.get("cardinality_ratio")`, `entry.get("value_pattern")` (from Task 3; may be `None`).
- Produces: no signature change to `_contextual_score(feature_norm, context_tokens, context_norm, entry) -> tuple[float, list[str], bool]`; score/reasons reflect the two new signals when present.

- [ ] **Step 1: Write the failing test**

Create `tests/regressions/test_contextual_score_new_signals.py`:

```python
"""Regression: cardinality_ratio and value_pattern (Task 3) must contribute to
_contextual_score, not sit unused in the profile evidence."""
from __future__ import annotations

import unittest

from core.onboarding.kpi.feature_resolver import _contextual_score


class ContextualScoreNewSignalsTests(unittest.TestCase):
    def test_near_unique_id_shaped_column_gets_identifier_bonus(self):
        entry = {
            "column": "ClaimID", "dataset": "claims", "dtype": "String",
            "cardinality_ratio": 0.99, "value_pattern": None,
        }
        with_cardinality_ratio, _, _ = _contextual_score("claimid", set(), "", entry)
        entry_no_signal = dict(entry, cardinality_ratio=None)
        without_cardinality_ratio, _, _ = _contextual_score("claimid", set(), "", entry_no_signal)
        self.assertGreater(with_cardinality_ratio, without_cardinality_ratio)

    def test_currency_pattern_boosts_a_financial_seed_feature(self):
        entry = {
            "column": "ChargeAmount", "dataset": "claims", "dtype": "Float64",
            "cardinality_ratio": None, "value_pattern": "currency_2dp",
        }
        with_pattern, reasons, _ = _contextual_score("charge", set(), "", entry)
        entry_no_pattern = dict(entry, value_pattern=None)
        without_pattern, _, _ = _contextual_score("charge", set(), "", entry_no_pattern)
        self.assertGreater(with_pattern, without_pattern)
        self.assertTrue(any("pattern" in reason for reason in reasons))

    def test_missing_signals_do_not_raise(self):
        entry = {"column": "Name", "dataset": "departments", "dtype": "String"}
        score, reasons, matched = _contextual_score("department", set(), "", entry)
        self.assertIsInstance(score, float)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_contextual_score_new_signals -v`
Expected: FAIL — `with_cardinality_ratio` equals `without_cardinality_ratio`, `with_pattern` equals
`without_pattern` (the new fields are on `entry` but nothing reads them yet).

- [ ] **Step 3: Add the two new scoring terms**

In `feature_resolver.py`'s `_contextual_score` (currently ending around line 1391, just before
`return score, reasons, name_matched`), insert before the `return`:

```python
    cardinality_ratio = entry.get("cardinality_ratio")
    if cardinality_ratio is not None:
        if cardinality_ratio >= 0.98 and (column_norm.endswith("id") or column_norm.endswith("code")):
            score += 4.0
            reasons.append(
                f"column is near-unique (cardinality={cardinality_ratio:.2f}), "
                "consistent with an identifier role"
            )
        elif cardinality_ratio < 0.05 and not column_norm.endswith("id"):
            score += 1.0
            reasons.append(
                f"column is low-cardinality (cardinality={cardinality_ratio:.2f}), "
                "consistent with a categorical dimension"
            )
    value_pattern = str(entry.get("value_pattern") or "")
    if value_pattern == "currency_2dp" and any(
        seed in feature_norm for seed in GENERIC_FINANCIAL_SEED
    ):
        score += 3.0
        reasons.append("column's observed value pattern matches a currency shape")
```

(`GENERIC_FINANCIAL_SEED` is already imported inside this function via
`from core.onboarding.lexicon.vocabulary import GENERIC_FINANCIAL_SEED` a few lines above —
reuse that import, do not add a second one.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_contextual_score_new_signals -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Run the full gate**

Run: `.venv\Scripts\green-gate.exe`
Expected: exactly the 2 pre-existing, unrelated failures from the corrected Global Constraints
baseline (`test_json_nested_leaf_profiling`, `test_profiler_tb_scale_csv_nullcount...`) and no
others — do not attempt to fix those two here.

- [ ] **Step 6: Commit**

```bash
git add core/onboarding/kpi/feature_resolver.py tests/regressions/test_contextual_score_new_signals.py
git commit -m "feat(kpi): feed profiler cardinality/value_pattern into contextual scoring

Both signals existed nowhere in the evidence chain before this change;
they now contribute two additive, evidence-gated terms to the existing
multi-signal score, never a standalone match on their own."
```

---

### Task 4b: Wire `cardinality_ratio`/`value_pattern` through `column_profile_summary` (plan gap, added 2026-08-03)

**Why this task exists:** Task 3's review reviewer independently confirmed Task 3 and Task 4 were
each individually spec-compliant, but the Task 4 reviewer found a real, plan-level gap while judging
whether Task 4 has any actual effect: `core/onboarding/features/derived_evidence.py`'s
`column_profile_summary()` — the function `schema_index_from_profiles`
(`core/onboarding/relationships/schema_alias_matching.py:46-61`) uses to build every `entry` dict
that reaches `_contextual_score` — returns a hand-enumerated dict of specific keys
(`sample_min/max`, `exact_min/max`, `metadata_min/max`, `null_count`, `sample_values`,
`profile_source`) and does NOT include `cardinality_ratio` or `value_pattern`, even though Task 3
already puts both onto the serialized `ColumnProfile` (`item` here) with `None` defaults. Neither
Task 3 nor Task 4 was scoped to touch this file, so this is a genuine omission in the plan's task
split, not an implementer defect — without this fix, Task 4's two new scoring terms can never fire
on real workspace data; `entry.get("cardinality_ratio")` and `entry.get("value_pattern")` are always
`None` on the actual `contextual_column_candidates` path.

**Files:**
- Modify: `core/onboarding/features/derived_evidence.py` (`column_profile_summary`, currently lines
  319-333)
- Test: `tests/regressions/test_column_profile_summary_carries_new_signals.py`

**Interfaces:**
- Consumes: nothing new (reads `item.get("cardinality_ratio")`/`item.get("value_pattern")` from the
  same `item` dict already being read for every other key).
- Produces: `column_profile_summary(...)`'s returned dict gains `"cardinality_ratio"` and
  `"value_pattern"` keys, alongside the existing ones — additive only, no existing key removed or
  renamed.

- [ ] **Step 1: Write the failing test**

Create `tests/regressions/test_column_profile_summary_carries_new_signals.py`:

```python
"""Regression: column_profile_summary must forward cardinality_ratio and
value_pattern (Task 3 fields) into the entry dicts _contextual_score reads
(Task 4) -- without this, both new scoring terms are permanently unreachable
on the real contextual_column_candidates path (plan gap found during Task 4
review, 2026-08-03)."""
from __future__ import annotations

import unittest

from core.onboarding.features.derived_evidence import column_profile_summary


class ColumnProfileSummaryNewSignalsTests(unittest.TestCase):
    def test_cardinality_ratio_and_value_pattern_are_forwarded(self):
        profile = {
            "columns": [
                {
                    "name": "ClaimID",
                    "cardinality_ratio": 0.99,
                    "value_pattern": "prefixed_numeric_code",
                }
            ]
        }
        summary = column_profile_summary(profile, "ClaimID")
        self.assertEqual(summary.get("cardinality_ratio"), 0.99)
        self.assertEqual(summary.get("value_pattern"), "prefixed_numeric_code")

    def test_missing_signals_default_to_none_not_a_missing_key(self):
        profile = {"columns": [{"name": "Name"}]}
        summary = column_profile_summary(profile, "Name")
        self.assertIsNone(summary.get("cardinality_ratio"))
        self.assertIsNone(summary.get("value_pattern"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_column_profile_summary_carries_new_signals -v`
Expected: FAIL — `summary.get("cardinality_ratio")` is `None` via a missing key today too, so use
`self.assertIn("cardinality_ratio", summary)` if the first assertion doesn't fail cleanly; the real
proof is the first test's `0.99`/`"prefixed_numeric_code"` values, which cannot appear today since
the keys aren't copied at all.

- [ ] **Step 3: Add the two keys**

In `core/onboarding/features/derived_evidence.py`'s `column_profile_summary` (currently lines
319-333), read the existing `return {...}` dict literal in full first, then add two more lines
matching the existing pattern exactly, changing nothing else:

```python
                "profile_source": item.get("source"),
                "cardinality_ratio": item.get("cardinality_ratio"),
                "value_pattern": item.get("value_pattern"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_column_profile_summary_carries_new_signals -v`
Expected: PASS, 2 tests

- [ ] **Step 5: Run the full gate**

Run: `.venv\Scripts\green-gate.exe`
Expected: exactly the 2 pre-existing, unrelated failures from the corrected Global Constraints
baseline and no others.

- [ ] **Step 6: Commit**

```bash
git add core/onboarding/features/derived_evidence.py tests/regressions/test_column_profile_summary_carries_new_signals.py
git commit -m "fix(kpi): forward cardinality_ratio/value_pattern through column_profile_summary

Task 3 added these fields to ColumnProfile; Task 4 added scoring terms
that read them from _contextual_score's entry dict. Neither task touched
column_profile_summary, the function that actually builds those entry
dicts from a serialized profile -- so on the real resolution path both
signals were always None. Additive-only fix, no existing key changed."
```

---

### Task 4c: Guard the cardinality bonus against bare 2-character ID columns (plan gap, added 2026-08-03)

**Why this task exists:** Task 4b's implementer, before committing its own (correct) plumbing fix,
ran the full gate with the new signal actually reachable for the first time and found a real
regression: `tests.test_contextual_dictionary_mapping.ContextualDictionaryMappingTests.test_resolver_creates_json_backed_time_derivation_options`
started failing. Root cause, reproduced directly (not guessed): the sibling ID-penalty in
`_contextual_score` (`feature_resolver.py:1359`, `if column_norm.endswith("id") and len(column_norm) > 2:`)
deliberately exempts a bare 2-character `id`/`code` column from the -30 penalty, but Task 4's new
cardinality bonus (`feature_resolver.py:1393`, `if cardinality_ratio >= 0.98 and (column_norm.endswith("id") or column_norm.endswith("code")):`)
has no matching guard — so once `cardinality_ratio` is actually wired through (Task 4b), a bare `Id`
primary-key column collects `+4.0` for ANY unrelated feature token that has weak generic
dataset-vocabulary overlap (`+6.0`), clearing the `>=8` candidate-inclusion gate
(`feature_resolver.py:1209`) and hijacking resolution away from the correct derivation-pattern
branch. This task was undetectable before Task 4b because `cardinality_ratio` was always `None`
(the bonus was dead code) — it is a real gap in Task 4's own logic, only exposed once wired.

**Files:**
- Modify: `core/onboarding/kpi/feature_resolver.py:1391-1398` (`_contextual_score`, the
  cardinality-bonus branch only)
- Test: extend `tests/regressions/test_contextual_score_new_signals.py` (Task 4's test file) with one
  new case

**Interfaces:**
- No signature change. The cardinality bonus (`+4.0`, near-unique/id-shaped) gains the same
  `len(column_norm) > 2` guard the sibling penalty already has, two lines above.

- [ ] **Step 1: Write the failing test**

Add to `tests/regressions/test_contextual_score_new_signals.py`:

```python
    def test_bare_two_character_id_column_does_not_get_the_identifier_bonus(self):
        # Mirrors the sibling ID-penalty's own len(column_norm) > 2 exemption
        # (feature_resolver.py:1359) -- a bare "Id" column must not collect
        # the cardinality bonus either, or it wins unrelated features purely
        # on generic dataset-vocabulary overlap once cardinality_ratio is
        # reachable (2026-08-03 regression, found wiring Task 4b).
        entry = {
            "column": "Id", "dataset": "encounters", "dtype": "String",
            "cardinality_ratio": 1.0, "value_pattern": None,
        }
        score, reasons, _ = _contextual_score("encounterdurationbucket", {"encounters"}, "encounters", entry)
        self.assertLess(score, 8.0)
        self.assertFalse(any("identifier role" in reason for reason in reasons))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_contextual_score_new_signals -v`
Expected: FAIL — the bare `"Id"` column currently collects the `+4.0` bonus (`score` includes it and
a reason mentioning "identifier role" is present).

- [ ] **Step 3: Add the length guard**

In `feature_resolver.py`'s `_contextual_score`, change (currently line 1393):

```python
        if cardinality_ratio >= 0.98 and (column_norm.endswith("id") or column_norm.endswith("code")):
```

to:

```python
        if (
            cardinality_ratio >= 0.98
            and len(column_norm) > 2
            and (column_norm.endswith("id") or column_norm.endswith("code"))
        ):
```

(Mirrors the sibling penalty's own `len(column_norm) > 2` guard at line 1359 exactly — same
rationale: a bare 2-character `id`/`code` column is exempt from both the penalty and the bonus.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_contextual_score_new_signals -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Run the full gate**

Run: `.venv\Scripts\green-gate.exe`
Expected: exactly the 2 pre-existing, unrelated failures from the corrected Global Constraints
baseline and no others — specifically confirm
`tests.test_contextual_dictionary_mapping.ContextualDictionaryMappingTests.test_resolver_creates_json_backed_time_derivation_options`
passes again.

- [ ] **Step 6: Commit**

```bash
git add core/onboarding/kpi/feature_resolver.py tests/regressions/test_contextual_score_new_signals.py
git commit -m "fix(kpi): exempt bare 2-char id/code columns from the cardinality bonus

Mirrors the sibling ID-penalty's own len(column_norm) > 2 guard
(feature_resolver.py:1359). Without it, a bare Id primary key collects
the +4.0 near-unique-identifier bonus for ANY unrelated feature with
weak generic dataset-vocabulary overlap, once cardinality_ratio is
reachable (Task 4b) -- hijacking resolution away from the correct
derivation-pattern branch. Found and reproduced during Task 4b."
```

---

### Task 5: Raise the auto-proven bar for financial-correctness-risk features

**Files:**
- Modify: `core/onboarding/kpi/feature_resolver.py:1174-1245` (`contextual_column_candidates`)
- Test: `tests/regressions/test_financial_correctness_requires_corroboration.py`

**Interfaces:**
- Consumes: `core.onboarding.features.blockers.risk_class(feature: str) -> str` (already exists, unchanged).
- Produces: no signature change to `contextual_column_candidates`; a `financial_correctness`-risk feature can no longer auto-prove on score/margin alone — it additionally requires the top candidate to carry a `dictionary_description`.

- [ ] **Step 1: Write the failing test**

Create `tests/regressions/test_financial_correctness_requires_corroboration.py`:

```python
"""Regression: a financial_correctness-risk feature must never auto-prove
on score/margin alone -- it needs corroboration beyond a bare threshold
pass, because a silently wrong money mapping is the highest-stakes failure
mode (blockers.risk_score ranks financial_correctness highest).

This is the surgical, evidence-driven form of "never silently substitute a
correlated proxy for a true source" (contracted rate vs. avg paid/charge is
the motivating case) -- applied exactly where the existing risk taxonomy
already says the stakes are highest, not a new derivability subsystem.

Fixture note: "margin" is used because it is both (a) a literal entry in
blockers.GENERIC_FINANCIAL_TERMS (so risk_class resolves to
financial_correctness), and (b) a feature whose table-name-alignment bonus
(+30 in _contextual_score, dataset "margins.csv" vs feature "margin",
after pluralization stripping) reliably clears the score>=14/margin>=4
auto-proven bar on its own, with only one candidate in schema_index -- so
the ONLY variable between the two tests is presence/absence of a
dictionary_description, isolating exactly what this fix changes.
"""
from __future__ import annotations

import unittest

from core.onboarding.kpi.feature_resolver import contextual_column_candidates


class FinancialCorrectnessCorroborationTests(unittest.TestCase):
    def test_financial_feature_without_dictionary_corroboration_does_not_auto_prove(self):
        schema_index = {
            "value": [
                {"dataset": "margins.csv", "column": "Value", "dtype": "Float64"},
                # No dictionary_description -- score/margin alone must not be enough.
            ],
        }
        candidates = contextual_column_candidates("margin", "", schema_index)
        self.assertTrue(candidates, "expected the table-alignment bonus to surface a candidate")
        self.assertFalse(
            candidates[0].get("auto_proven"),
            "a financial_correctness feature auto-proved without dictionary corroboration",
        )

    def test_financial_feature_with_dictionary_corroboration_can_still_auto_prove(self):
        schema_index = {
            "value": [
                {
                    "dataset": "margins.csv", "column": "Value", "dtype": "Float64",
                    "dictionary_description": "The realized margin value for this transaction.",
                },
            ],
        }
        candidates = contextual_column_candidates("margin", "", schema_index)
        self.assertTrue(
            candidates and candidates[0].get("auto_proven"),
            "a table-aligned, dictionary-corroborated financial match should still auto-prove",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_financial_correctness_requires_corroboration -v`
Expected: FAIL on `test_financial_feature_without_dictionary_corroboration_does_not_auto_prove`
(the current bare score≥14/margin≥4 check auto-proves regardless of risk class).

- [ ] **Step 3: Add the risk-class gate**

In `feature_resolver.py`, add the import near the top (alongside the other
`core.onboarding.features` imports, e.g. near line 19-23):

```python
from core.onboarding.features.blockers import risk_class as _feature_risk_class
```

Then in `contextual_column_candidates` (currently lines 1236-1240), change:

```python
    auto_proven = (
        not _expression_shaped_feature(feature)
        and top_score >= 14
        and (len(scored) == 1 or top_score - second >= 4)
    )
```

to:

```python
    auto_proven = (
        not _expression_shaped_feature(feature)
        and top_score >= 14
        and (len(scored) == 1 or top_score - second >= 4)
    )
    if auto_proven and _feature_risk_class(feature) == "financial_correctness":
        # Highest-stakes risk category (blockers.risk_score ranks it first).
        # A bare score/margin pass is corroborated evidence for most
        # features, but a silently wrong MONEY mapping is the one failure
        # mode that must never slip through on score alone -- require the
        # top candidate to also carry independent dictionary corroboration.
        auto_proven = bool(top.get("dictionary_description"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_financial_correctness_requires_corroboration -v`
Expected: PASS, 2 tests

- [ ] **Step 5: Run the full gate**

Run: `.venv\Scripts\green-gate.exe`
Expected: exactly the 2 pre-existing, unrelated failures from the corrected Global Constraints
baseline and no others. If an existing fixture relied on a financial-correctness feature
auto-proving without dictionary evidence, add a `dictionary_description` to that fixture rather
than weakening the new gate.

- [ ] **Step 6: Commit**

```bash
git add core/onboarding/kpi/feature_resolver.py tests/regressions/test_financial_correctness_requires_corroboration.py
git commit -m "fix(kpi): financial_correctness features require corroboration to auto-prove

A bare score>=14/margin>=4 pass silently proved money-affecting mappings on
the same footing as any other feature. The highest-stakes risk category
(per blockers.risk_score) now needs a dictionary-description match too."
```

---

### Task 6: Re-baseline resolver accuracy and lock in the end-to-end fix

**Files:**
- Modify: `tests/fixtures/resolver_accuracy_baseline.json`
- Test: `tests/regressions/test_kpi_012_scenario_end_to_end.py`

**Interfaces:**
- Consumes: `core.dev.resolver_accuracy` CLI (`resolver-accuracy --write-baseline`, existing).
- Produces: nothing new; locks in the combined effect of Tasks 1, 2, 4, 5 against the real
  `workspaces/rcm` data.

- [ ] **Step 1: Write the end-to-end regression test**

Create `tests/regressions/test_kpi_012_scenario_end_to_end.py`:

```python
"""Regression: the exact kpi_012 scenario from the 2026-08-03 review --
"Risk Tier (Low/Medium/High)" must never again produce a blocker for
"High" recommending departments.Name, after Tasks 1 and 2 land.
"""
from __future__ import annotations

import unittest

from core.onboarding.features.expression import extract_expression
from core.onboarding.kpi.blocker_question_panel import _physical_option_payload


class Kpi012ScenarioEndToEndTests(unittest.TestCase):
    def test_high_never_extracted_from_risk_tier_cuts(self):
        extracted = extract_expression(
            "weighted composite score (0-100), banded Low <33 / Medium 33-66 / High >66"
        )
        leaked = {t.lower() for t in extracted.identifiers} & {"high", "low", "medium", "weighted", "score", "banded"}
        self.assertFalse(leaked, f"formula-tier vocabulary leaked: {leaked}")

    def test_a_bare_generic_containment_score_never_renders_high_confidence(self):
        # Even if some future change lets a similarly weak token through,
        # the panel's OWN calibration (Task 2) must still refuse to call a
        # +20-only score "high".
        payload = _physical_option_payload(
            {"score": 20.0, "column": "Name", "dataset": "departments.csv"}, 1
        )
        self.assertNotEqual(payload["confidence"], "high")
```

- [ ] **Step 2: Run test to verify it passes** (Tasks 1 and 2 already landed)

Run: `.venv\Scripts\python.exe -m unittest tests.regressions.test_kpi_012_scenario_end_to_end -v`
Expected: PASS, 2 tests

- [ ] **Step 3: Re-baseline the resolver-accuracy grader**

The checked-in baseline was zeroed on 2026-07-27 after a workspace deletion
(`tests/fixtures/resolver_accuracy_baseline.json` reports `total: 0`), but
`workspaces/rcm/interns/generated/contracts/kpi_feature_mapping.json` currently has 10 real
`user_confirmed` labels the grader has never been re-run against.

Run: `.venv\Scripts\python.exe -m core.dev.resolver_accuracy --write-baseline`
Expected: `[ok] wrote baseline: N correct, M wrong, K abstained` with `N + M + K > 0` (not the
stale all-zero baseline).

- [ ] **Step 4: Run the full gate**

Run: `.venv\Scripts\green-gate.exe`
Expected: exactly the 2 pre-existing, unrelated failures from the corrected Global Constraints
baseline and no others, test count >= 1648 + the new regression tests from Tasks 1-6.

- [ ] **Step 5: Commit**

```bash
git add tests/regressions/test_kpi_012_scenario_end_to_end.py tests/fixtures/resolver_accuracy_baseline.json
git commit -m "test(kpi): lock in the kpi_012 fix end-to-end; re-baseline resolver-accuracy

The checked-in baseline was stale (zeroed after a 2026-07-27 workspace
deletion) while workspaces/rcm now has 10 real user_confirmed labels the
gate was never re-run against."
```

---

## Final verification

- [ ] **Full gate:** `.venv\Scripts\green-gate.exe` → `[ok] all green`, test count >= 1642 + 6 new regression files' tests.
- [ ] **Re-run the real blocker panel against `workspaces/rcm`:**
  `uv run prepare-kpi-blocker-panel --workspace workspaces/rcm --domain general` and read
  `workspaces/rcm/interns/reports/blocker_question_panel/current.md` — the count of
  formula-vocabulary blocker rows for KPIs 4-18 should drop sharply from the 56 confirmed-fake
  rows found in the 2026-08-03 review, and no remaining option should render `RECOMMENDED` at
  `confidence: high` on a purely generic-containment reason.
- [ ] **`resolver-accuracy` no longer reports a stale zero baseline:**
  `.venv\Scripts\python.exe -m core.dev.resolver_accuracy` (without `--write-baseline`) should
  report real grading numbers against the re-baselined fixture from Task 6.
