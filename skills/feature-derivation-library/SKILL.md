---
name: feature-derivation-library
description: >
  Use when KPI/query work needs reusable derived-feature patterns, candidate formulas, temporal
  anchors, join-derived features, taxonomy-derived features, or SQL/Polars derivation templates.
  This skill helps propose derivations while preserving the rule that candidates are not proof.
---

# Feature Derivation Library

Use this skill when a KPI term is not a direct physical column and may need a reusable derivation,
such as age, age band, net paid amount, CPT family, provider specialty, AR aging, margin, or other
formula/join/taxonomy-derived features.

## Core Rule

Reusable patterns are candidates, not evidence. A derivation becomes executable only when it is:

- proven by KPI registry, data dictionary, methodology, contract, source code, schema/profile plus
  relationship evidence, or
- explicitly confirmed by the user and saved as a workspace decision.

Never turn a model-generated candidate into solution SQL without proof or user confirmation.
Never offer a derivation pattern as a selectable option when the feature name is semantically
unrelated to the pattern. Reject mismatches and ask for a physical mapping, source-origin rule,
dictionary evidence, or workspace business definition.

## Code Resources

Use the repo's machine-readable pattern/search implementation:

```text
core/onboarding/features/derivation_patterns.json
core/onboarding/features/derivation_search.py
core/onboarding/kpi/feature_resolver.py
```

Run `uv run prepare-kpi-blocker-panel --workspace <workspace> --domain <domain>` to attach candidate
patterns to unresolved KPI features after onboarding has generated profiles and the KPI registry
artifact. It owns resolution, derived-feature markdown, panel generation, and validation in
lock-step. (`resolve-kpi-features --include-candidates` is the deprecated entry point; it now just
redirects here and drops the flag.)

## Workflow

1. Read `kpi_feature_mapping.json` if it exists.
2. Identify blocked or ambiguous features.
3. Search reusable derivation patterns using feature term, schema columns, KPI expression context,
   domain, and prior workspace decisions.
4. Attach candidate patterns as `candidate_pattern` or `candidate_unconfirmed`.
5. Ask the user only when proof is missing or multiple business-valid derivations exist.
6. Save accepted answers in:

```text
workspaces/<project>/interns/generated/contracts/kpi_feature_mapping.json
workspaces/<project>/interns/generated/memory/decision_history.md
workspaces/<project>/interns/generated/requirements/
workspaces/<project>/interns/reports/open_questions.md
```

## States

Use these states consistently:

```text
proven_direct
proven_alias
proven_join
proven_formula
proven_taxonomy
user_confirmed
blocked_missing_evidence
blocked_ambiguous
candidate_unconfirmed
candidate_pattern
rejected
```

## Query Generation Gate

Generate authoritative solution SQL only when every required feature for the KPI is `proven_*` or
`user_confirmed`. Exploratory SQL may be written under `interns/generated/evidence/exploratory/`,
but it must never be treated as KPI truth.

## Built-in derivation patterns (deterministic)

`core/onboarding/features/derivation_patterns.py::detect_derivation_patterns(question, columns)`
emits JSON-backed candidate options for two shapes a single metric/cuts cell cannot express. Both
are evidence-driven (profiled column shape + generic English phrasing), `needs_user_confirmation`,
and never ground truth until confirmed:

- `duration_bucket` — a "over/under N hours/days" question + a start/stop temporal column pair ->
  `date_diff('<unit>', CAST(<start> AS TIMESTAMP), CAST(<stop> AS TIMESTAMP)) >= N`.
- `recurrence_within_window` — a "within N days of a previous ..." question + an entity id/fk and an
  event timestamp -> a self-join / `EXISTS` sketch over the same entity ordered by the event time.

Wiring point for the orchestrator/resolver: call `detect_derivation_patterns` for any KPI whose
metric/cuts stay empty after evidence derivation, and attach the returned options to that KPI's
blocker so the panel renders them as confirmable derived-feature options (same contract as
`derived_feature_options`).
