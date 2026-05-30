---
name: data-model-creation
description: >
  Create a data model WITH the user through conversation, not by guessing from column names.
  Interview for grain, entities, keys, facts/dimensions, relationships, cardinality, temporal anchors,
  and SCD policy; score how well the model is understood; then produce a governed model + ERD/SVG.
  Use when a workspace needs a data model created, refined, or proven before SQL/pipeline generation,
  or when relationship detection is uncertain. Pairs with [[grill-requirements]], [[clarify-ambiguity]],
  [[domain-model]], [[stakeholder-memory]], and [[dashboard-design]] (for the diagram export).
---

# Data Model Creation

A data model is a set of design decisions, not a column-name guess. Build it through conversation:
inspect the evidence, ask one decision at a time, confirm anything inferred from names, and only
finalize when the **understanding score** is high and the user approves the preview.

> Hard rule: never assert a relationship, grain, or fact/dimension role purely from column-name
> matching. Name overlap (e.g. two tables both have `id`) is a *candidate to confirm*, never proof.
> Confirm with the user or with profile/cardinality evidence. This is workspace-agnostic — no domain
> entity names are assumed.

## Conversation flow

Drive the governed CLI while interviewing; the CLI writes drafts, the conversation resolves intent.

```
prepare-data-model-generation        # route panel (text docs / image ERD / infer-from-profiles)
  -> apply-data-model-answer          # writes a DRAFT model pack (never user-facing yet)
  -> prepare-data-model-blocker-panel # next unresolved design decision, JSON-backed
  -> apply-data-model-blocker-answer  # records the decision; re-asks until none remain
  -> finalize-data-model-generation --approve-final-preview   # docs/data-model.md, erd.md, contract
  -> export-data-model-diagram        # ERD as SVG + Mermaid for stakeholders
```

Use [[grill-requirements]] for the interview and [[clarify-ambiguity]] only for the few high-impact
ambiguities a reasonable default cannot resolve. Save accepted decisions with [[stakeholder-memory]]
and align terms with [[domain-model]].

## Design order (interview one decision at a time)

1. **Purpose** — which KPIs/questions must this model answer? (drives what must be modeled)
2. **Entities** — the real-world things (confirm each; don't infer from filenames).
3. **Grain** — one row of each table represents *what*? (the single most important decision)
4. **Keys** — primary key per entity; which columns are identifiers vs attributes. Confirm inferred
   keys; a shared `*_id` is a candidate join, not a proven one.
5. **Facts vs dimensions** — facts = measurable events at a grain; dimensions = descriptive context.
   Decide by what is measured + cardinality, never by the table's name.
6. **Relationships** — for each join: left/right key, cardinality (1:1 / 1:N / N:N), and *evidence*
   (profile uniqueness/RI, data dictionary, or user confirmation). N:N needs a bridge table.
7. **Temporal anchor** — the date/timestamp each fact is measured by (for trend/period cuts).
8. **SCD policy** — do dimension attributes change over time, and is history needed (SCD1 vs SCD2)?
9. **Gaps** — missing dictionaries, ambiguous columns, or relationships that need user proof.

## Understanding score (show it every turn)

Tell the user how confident the model is, so they know what is still a guess:

```text
Data model understanding: 72/100
  Entities & grain confirmed:        ✅
  Keys confirmed (not name-guessed):  ⚠ 2 of 5 still inferred
  Relationships proven/confirmed:     ⚠ 3 candidates unconfirmed
  Temporal anchor + SCD decided:      ❌
Lowest-confidence item → ask next: confirm the join between <A> and <B>.
```

Score = mean of (entities, grain, keys, relationships, temporal/SCD) confidence, each 0–100. A
relationship inferred only from a shared column name counts as *candidate* (≤50), not *confirmed*,
until the user or profile/cardinality evidence backs it. Do not finalize below a high score without
the user explicitly accepting the remaining gaps.

## Output

- Draft pack under `interns/generated/requirements/` (never user-facing until approved).
- On approval: `docs/data-model.md`, `docs/erd.md`, `docs/relationships.md`, and
  `interns/generated/contracts/data_model_contract.json`.
- **ERD/SVG** via `export-data-model-diagram` (native SVG + Mermaid under `interns/reports/presentation/`).
- Approved relationships are promoted by `build-relationship-contracts` for executable SQL only after
  proof or user confirmation — image/name-derived links stay non-executable until validated.

Record accepted decisions, rejected options, and remaining open questions under the workspace's
`interns/` before any executable logic depends on the model.
