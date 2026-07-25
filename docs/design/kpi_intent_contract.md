# Design: KPI Intent Contract

Status: Implemented (see `core/onboarding/kpi/intent_coverage.py`, 789 lines) — this doc is the
original design; verify current exact behavior against the code, not this file, for any specific
detail.
Author: platform
Related: `core/onboarding/kpi/intent_coverage.py`, `core/onboarding/kpi/result_view_builder.py`,
`core/onboarding/workspace/flow.py` (review + gate provenance), blocker-question-panel,
`docs/bugs/BUG_SESSION_REPORT.md` (BUG-005, BUG-024, BUG-025).

---

## 1. Problem

Almost every shipped-wrong-result in this platform traces to **one facet of KPI intent that was
never modeled, or was silently defaulted, and surfaced late (at the kpi-analyst review) instead of
being captured, scored, confirmed, and enforced up front.**

Evidence from real runs:

| Failure | Unmodeled / silently-defaulted intent facet |
| --- | --- |
| kpi_002 `COUNT(DISTINCT ...) OVER ()` (population total) instead of per-department | **denominator scope** — never captured; silently defaulted to grand-total; discovered only at review; the corrective decision (`within_department`) was then recorded in `pipeline_decisions.json` but **not enforced** in the generated SQL |
| BUG-024: percentage-share dropped gender/age/visit-type | **grain / cut completeness** |
| BUG-005: age computed as-of-today instead of as-of-event | **temporal anchor** |
| top-N ranking | **output shape** |

The platform treats "intent" as an implicit blob (`metric` + `cuts` text). It computes a per-KPI
"understanding score" (e.g. `kpi_002: 68/100 partially_understood`) but never says *which facet* is
weak, never clarifies the weak facet before generation, and does not enforce every facet at output.

## 2. Goal

Turn intent from an implicit blob into an **explicit, per-facet contract** that is:

1. **Modeled** — named facets, not free text.
2. **Scored** — confidence per facet (not per KPI).
3. **Clarified** — a low-confidence facet becomes a targeted blocker question *before* SQL is
   generated, not a silent default discovered at review.
4. **Reported** — a resolved-intent block is emitted before generation, with provenance per facet
   (`source: human | default | derived`).
5. **Enforced** — the `intent_coverage` harness fails generation/review if any facet is not realized
   in the produced SQL.

This subsumes the one-off denominator-scope fix: that fix becomes the first facet wired through the
contract end-to-end.

## 3. The Intent Facets

Canonical facets every KPI must resolve before SQL generation:

| Facet | What it captures | Today | Failure if missing |
| --- | --- | --- | --- |
| `metric` | aggregation fn + measure column | parsed from `metric` | wrong aggregation |
| `grain` | every declared cut/dimension | parsed from `cuts` | BUG-024 (dropped cuts) |
| `filters` | explicit + prose filters (`Medicare`, `> 50`) | partial | wrong/missing filter |
| `denominator_scope` | for ratios/shares: `grand_total` \| `within_<group>` | **not modeled** | kpi_002 |
| `temporal_anchor` | age/date arithmetic measured as-of `event_date` \| `current_date` | implicit | BUG-005 |
| `output_shape` | `trend` \| `ranking(top_n)` \| `share` \| `distribution` \| `flat` | partial (top-N only) | wrong shape |
| `null_zero_handling` | denominator-zero, missing-dimension policy | not modeled | divide-by-zero / dropped rows |

Each facet record carries:

```
{ "facet": "denominator_scope",
  "value": "within_department",
  "confidence": "high|medium|low|none",
  "source": "human|default|derived",
  "evidence": ["metric text '... for departement'", "blocker answer option_b"],
  "alternatives": ["grand_total"] }
```

## 4. Lifecycle (mirrors the intent-discovery discipline)

```
extract facets (independent of generator parser)
   -> score confidence per facet
       -> any facet Low/None  -> blocker panel: ONE targeted question, naming the interpretations
       -> all facets High/defaulted-with-provenance
           -> emit resolved-intent report (per-facet value + confidence + source)
               -> generate SQL
                   -> intent_coverage harness: every facet must be REALIZED in the SQL (hard gate)
```

- **Score per facet, not per KPI.** A `Low` facet (e.g. denominator scope, which `"... for
  departement"` genuinely makes ambiguous) is a hard stop, regardless of overall KPI score.
- **Clarify the weak facet up front** via the existing blocker panel — name the interpretations
  (*"denominator: within each department, or share of total population?"*) — instead of silently
  defaulting and discovering at review.
- **Record every default with provenance** (`source: default`) and surface it in the gate-provenance
  section, exactly as relationship approvals and the review verdict already are. A silent default is
  the anti-pattern that produced kpi_002.

## 5. Enforcement (close the loop)

Extend `core/onboarding/kpi/intent_coverage.py` so each facet has a realized-in-SQL check, run inside
the already-gated `KPIExecutionHarness._semantic_errors`:

| Facet | Realization check | Error code |
| --- | --- | --- |
| grain | every declared cut appears in result view (exists) | `grain_not_realized` |
| metric | aggregation fn + column present (exists) | `metric_not_realized` |
| filters | declared filter literal/threshold present (exists) | `filter_not_realized` |
| `denominator_scope` | `within_<group>` ⇒ denominator window is `OVER (PARTITION BY <group>)`, NOT `OVER ()` | `denominator_scope_not_realized` |
| `temporal_anchor` | `event_date` ⇒ age uses `CAST(<event> AS DATE)`, not `CURRENT_DATE` | `temporal_anchor_not_realized` |
| `output_shape` | `ranking(top_n)` ⇒ `ORDER BY ... DESC LIMIT n` present | `output_shape_not_realized` |

The `denominator_scope_not_realized` check is the one that would have failed kpi_002 instead of
letting the review rubber-stamp unchanged SQL. **A recorded decision that the generator ignores must
become a hard error**, never a silent no-op.

## 6. Integration points

- **Extract**: a new `kpi_intent_contract.json` under `interns/generated/contracts/`, produced during
  feature resolution / source-to-target planning. Facet extraction is INDEPENDENT of
  `result_view_builder.parse_kpi` (same independence rationale as `intent_coverage` — a generator that
  drops a facet must not pass a check that reused its own parser).
- **Score + clarify**: low-confidence facets feed the existing `blocker-question-panel` (one question
  per weak facet; reuse the panel contract — no freehand prompts).
- **Report**: extend `source_to_target_plan.md` (or a dedicated `kpi_intent_report.md`) with the
  per-facet table before generation.
- **Generate**: `result_view_builder` reads the resolved facets (e.g. `denominator_scope`,
  `temporal_anchor`) and emits accordingly — wiring `pipeline_decisions.json.percentage_denominator_scopes`
  through to the denominator window is the first slice.
- **Enforce**: `intent_coverage` + `execution_harness` as in §5.
- **Provenance**: defaulted/derived facets appear in the gate-provenance section at completion, so
  `--require-human-gates` can block on unconfirmed high-impact facets.

## 7. Phasing

1. **Denominator-scope facet, end-to-end** (first concrete slice; also fixes the live BUG-025 bug):
   record → clarify-if-ambiguous → wire `within_<group>` into `result_view_builder` denominator
   window → `denominator_scope_not_realized` enforcement + test.
2. **Temporal-anchor + output-shape facets** wired + enforced (closes BUG-005 latent risk).
3. **Full contract artifact** (`kpi_intent_contract.json`) + per-facet confidence scoring + blocker
   routing for any `Low` facet + resolved-intent report.
4. **Provenance surfacing** of defaulted facets in the completion gate-provenance section.

Each phase is independently shippable and must leave the green gate at 0 regressions.

## 8. Tests

- A `within_department` decision flips the denominator window to `OVER (PARTITION BY <group>)`;
  `OVER ()` under that decision is a `denominator_scope_not_realized` error.
- An age KPI with an event date uses the event date, not `CURRENT_DATE` (temporal anchor).
- A `Low`-confidence facet routes to a blocker question instead of generating.
- A defaulted facet is recorded `source: default` and appears in gate provenance.

## 9. What this borrows from the intent-discovery skill

The skill's reusable discipline — **enumerate intent into named facets, score confidence per facet,
clarify the single weakest one before acting, emit an intent report before output, never silently
default** — applied to KPIs. The skill's document-reading/classification half is out of scope here;
it belongs to `docs/design/pdf_ingestion.md`.

## 10. Non-goals

- Not changing default KPI semantics silently (grand-total stays the default denominator until a
  facet decision says otherwise).
- Not an LLM re-interpretation layer; facet extraction is deterministic + evidence-backed, with the
  blocker panel as the only human-clarification surface.
