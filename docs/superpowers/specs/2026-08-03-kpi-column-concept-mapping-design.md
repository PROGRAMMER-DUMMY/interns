# KPI Column-to-Concept Mapping — Design

## Context

The user handed down a "map to concept, not to column" protocol for resolving how physical
columns relate to the business concepts a KPI needs: profile every column independent of any
task, map to a name-agnostic concept (never column-to-column), score every mapping on
multiple independent signals (name similarity alone is weak evidence), never silently
auto-merge on a name match, surface conflicts explicitly instead of picking one silently, and
— a later addition — before declaring a needed concept BLOCKED/missing, check whether it is
genuinely derivable via formula from other resolved concepts, versus merely a correlated proxy
that must never silently substitute for the true source.

Before designing anything new, we read the full resolution pipeline
(`core/onboarding/kpi/feature_resolver.py`, `core/onboarding/relationships/schema_alias_matching.py`,
`core/onboarding/features/{expression,derivation_patterns,derived_evidence,blockers}.py`,
`core/onboarding/kpi/blocker_question_panel.py`) and researched how this problem is solved in
established practice (`docs/reference/kpi_column_concept_mapping_research.md`: schema-matching
literature, dbt MetricFlow/Cube/Looker, Featuretools/Feast/Tecton, metric-governance sources).
Two things followed from that:

1. **Most of the protocol's discipline already exists in this codebase.** `_contextual_score`
   already combines multiple signals (table alignment, column-name match, context-phrase match,
   an ID-column penalty, dictionary-description match, numeric-dtype bonus) with a real margin
   discipline (auto-proven needs score≥14 **and** beats the runner-up by ≥4). `derivation_patterns.py`'s
   four detectors already cap every derived candidate at `confidence: medium/low`,
   `needs_user_confirmation: True`, `evidence_state: candidate_derivation_not_ground_truth` —
   never silently promoted. `_resolve_direct_collision` already resolves a same-column-name
   collision through dictionary-context choice, then relationship-lineage union-find, and
   otherwise blocks with per-candidate evidence rather than picking one silently — which already
   **is** the union-vs-surface-conflict distinction the protocol asks for.

2. **Reading the real pipeline surfaced two concrete, confirmed bugs** that explain the actual
   pain (a live review of `workspaces/rcm`'s 18 KPIs found 76% of "blocker" rows were fake
   tokens extracted from formula text, several with a confidently wrong recommendation) — see
   "Root cause" below. Fixing those two bugs, plus adding the two signals that are genuinely
   absent (cardinality, value pattern), delivers what the protocol asks for without inventing a
   persisted Concept Registry or rearchitecting the resolver. That larger version (Approach A
   from the design conversation) is explicitly deferred — see "Out of scope."

## Root cause (diagnosed before any fix was proposed)

**Bug 1 — extraction noise.** `core/onboarding/features/expression.py`'s `BUSINESS_TEXT_STOPWORDS`
does not cover the ratio/percentile/z-score/banded-tier vocabulary rcm's harder KPIs (4-18) use:
`within, per, all, benchmark, std, dev, actual, at, on, track, expected, expired, high, low,
medium, flag, if, mean, outside, falls, score, weight, weighted, touching, unplanned`.
`strip_literals()` also does not catch letter+digit tokens like `P95` (it only strips
pure-digit tokens). These survive as "identifiers" needing resolution, indistinguishable from a
real column name. **Deliberately excluded from this list: `LOS`** (Length of Stay) — despite
looking like noise alongside the others in the original review, it's a real business concept
genuinely unbacked by any column in `rcm`, not formula glue; adding it to a generic stopword
list would both silently drop a legitimate open question and bake a healthcare-specific
abbreviation into workspace-agnostic filtering code.

**Bug 2 — a second, uncalibrated scorer with a real blast radius.** `blocker_question_panel.py`
does not reuse `feature_resolver.py`'s scoring. It has its own function, `_profile_candidate_score`
(line 1281), that re-scans every profiled column from scratch for every unresolved feature —
garbage or real — with different weights (exact match +100, partial match +60, dataset-name
alignment +30, "name appears anywhere in the KPI's full text" +20). `_physical_option_payload`
(line 1359) then labels `confidence = "high" if score >= 6 else ...` — a bar that one generic,
unrelated hit (e.g. "department" appearing in the KPI's text for a real cut, unrelated to the
garbage token being scored) clears more than 3x over. This is reachable, not just cosmetic:
`blocker_workflow.py`'s `apply_kpi_panel_answer` auto-applies whichever option is
`recommended_option_id` if a human answers "accept recommended" without naming an explicit
option — so a confidently-wrong label has a real path to silent acceptance.

## Design

No new files for production logic (explicit user decision — correct existing files, don't add
new abstractions). Six changes, in dependency order:

### 1. Fix the extraction gap
**File:** `core/onboarding/features/expression.py`.
Extend `BUSINESS_TEXT_STOPWORDS` with the confirmed missing words. Add a pattern check (e.g.
`^p\d+$` case-insensitive) for percentile-literal tokens that `strip_literals()` currently
misses because they're letter+digit, not pure-digit.
**Test:** feed rcm's actual kpi_004-kpi_018 metric/cuts text through `extract_expression` and
assert none of the confirmed-bad tokens survive as identifiers.

### 2. Unify the two scorers
**Files:** `core/onboarding/kpi/blocker_question_panel.py`, `core/onboarding/kpi/feature_resolver.py`.
Stop `blocker_question_panel.py` from re-deriving `_profile_candidate_score` for a feature that
already has resolver-computed `candidates` (from `contextual_column_candidates`/`_contextual_score`).
Panel consumes the resolver's existing score/reason instead of recomputing a second, cruder one.
This is the structural fix — one scorer, one source of truth, not two that can disagree.
**Test:** a feature with resolver-computed candidates must render panel options whose scores
trace back to `_contextual_score`'s output, not a freshly-computed `_profile_candidate_score`.

### 3. Add the two genuinely missing profiler signals
**File:** `core/profiling/data_model_profiler.py` (plus the shared evidence helpers in
`core/onboarding/features/derived_evidence.py` — `value_profile()`, `column_profile_summary()`).
Add `cardinality_ratio` (unique_count / row_count, cheap to add to the existing scan; named
`cardinality_ratio` rather than `cardinality` because `relationships/contracts.py` and
`data_model/data_understanding.py` already use a literal `"cardinality"` dict key to mean an
absolute distinct count, discovered mid-implementation) and
`value_pattern` (a regex inferred from already-collected `sample_values`). Every profile is
stamped `profile_tier: "raw"` — confirmed: profiling only ever runs pre-medallion, against
bronze-shaped (pre-dedup, per `bronze_silver_standards.py`'s explicit forbidding of
`deduplication_application` in bronze) source data, never silver. A later re-profile against a
silver-promoted dataset can stamp `profile_tier: "silver"` and upgrade a mapping's cap.
**Test:** golden fixtures for cardinality/value_pattern against known column shapes (a PK-shaped
near-unique string, a low-cardinality categorical, a currency-pattern float).

### 4. Feed the new signals into the one remaining scorer; recalibrate the confidence bar
**File:** `core/onboarding/kpi/feature_resolver.py` (`_contextual_score`, `contextual_column_candidates`).
Add cardinality-match and value-pattern-match as two new weighted terms in the existing score
(role is already partially covered by the existing ID-suffix penalty and
`schema_alias_matching.py`'s `STRUCTURAL_ALIAS_SUFFIXES`/`blockers.py`'s `risk_class()` — reuse
those rather than inventing a parallel role taxonomy). Replace the panel's old flat 6/3
threshold (now moot after Task 2) with the resolver's own already-proven margin discipline
(HIGH: auto-proven bar, score≥14 and margin≥4 over runner-up; MEDIUM: scored but short of that
bar; LOW: below the score≥8 floor to be listed at all) as the one confidence tiering used
everywhere. Per the earlier profile-tier decision: HIGH computed only from `profile_tier: "raw"`
evidence is capped to "corroborated, not proven" — the same discipline
`relationship_contracts.json`'s `needs_runtime_validation` stamp already uses for profile-derived
relationships.
**Test:** known HIGH/MEDIUM/LOW combinations against the new weighted signals; a regression
fixture proving a raw-tier-only HIGH never silently outranks a `user_confirmed` decision.

### 5. Raise the bar further for the highest-risk category before "recommended" is reachable
**File:** `core/onboarding/kpi/feature_resolver.py` (the auto-proven check inside
`contextual_column_candidates`), reusing `blockers.py`'s existing `risk_class()`/`risk_score()` —
no new taxonomy. When `risk_class(feature) == "financial_correctness"` (already ranked the
highest-stakes category), require corroboration beyond the bare score≥14/margin≥4 bar (e.g. a
dictionary-description match or an existing higher-tier evidence entry) before the candidate can
be marked `is_recommended`/reachable via the panel's "accept recommended" shortcut. This is the
concrete, surgical form of the protocol's Step 5/4.5 rule (never silently substitute a
correlated proxy for a true source) — applied exactly where the existing risk taxonomy already
says the stakes are highest, rather than a new derivability/proxy subsystem.
**Test:** reproduce the exact kpi_012 (`"Risk Tier (Low/Medium/High)"` vs. `departments.Name`)
scenario as a fixture; assert it can never reach `is_recommended: True` after the fix, and add a
contracted-rate-style fixture (a financial-correctness feature with no true source and only a
correlated candidate) asserting it stays `blocked_missing_evidence`, never silently offered as
equivalent.

### 6. Regression coverage tying it together
New tests under `tests/regressions/`: the kpi_012 reproduction, the contracted-rate-style
fixture, an `extract_expression` fixture over rcm's real KPI text, and a `resolver_accuracy.py`
re-baseline (its checked-in baseline is already stale — zeroed on 2026-07-27 after a workspace
deletion, while `workspaces/rcm` currently has 10 real `user_confirmed` labels sitting unused).

## Out of scope (deferred, not forgotten)

- **A persisted Concept Registry** (Approach A from the design discussion): a first-class,
  MetricFlow-`entity`-style object that accumulates evidence over time. Research found no
  existing tool documents this for *automatic* (not human-declared) resolution — it would be
  genuinely new ground, not a retrofit, and the two confirmed bugs above don't need it to be
  fixed. Revisit if retrofitting the existing pipeline turns out insufficient in practice.
- **A general derivability-vs-proxy subsystem.** The existing default (block, ask a human) is
  already conservative enough for the general case; the one place it needed sharpening
  (financial-correctness features specifically) is handled surgically in Task 5. A broader
  formula-registry mechanism can wait for a case that actually needs it.
- **Silver-tier re-profiling.** Noted as the upgrade path for the `profile_tier` cap, not built
  now — no workspace in this repo currently has a medallion build feeding back into profiling.

## Verification

- `green-gate` stays green (1642+ tests, 0 failing) after every task.
- `resolver-accuracy --write-baseline` re-run and re-committed (Task 6).
- Live re-run of `resolve-kpi-features` (or `prepare-kpi-blocker-panel`) against `workspaces/rcm`:
  blocker count for KPIs 4-18 should drop sharply (most of the 56 confirmed-fake rows disappear),
  and no remaining option should show `is_recommended: True` with a purely generic-overlap reason.
