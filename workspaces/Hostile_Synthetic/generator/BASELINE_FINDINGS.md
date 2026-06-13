# Hostile_Synthetic - baseline pipeline findings (Phase 2.1)

Date: 2026-06-12. Branch: `worktree-agent-a7fbdaa6b4533b799` (base: main @ c02b6b9, gate 478).
Commands run (current platform code, worktree venv):

1. `uv run list-workspace-files --workspace workspaces/Hostile_Synthetic`
2. `uv run onboard-workspace --workspace workspaces/Hostile_Synthetic`
3. `uv run prepare-kpi-blocker-panel --workspace workspaces/Hostile_Synthetic --domain logistics`
4. `uv run validate-workspace-artifacts --workspace workspaces/Hostile_Synthetic`

Workspace: 57 CSV tables / 30,564 rows, 10 prose KPIs, stale dictionary, written data model.
Answer key: `GROUND_TRUTH.md` (same folder). The platform must never read either file.

## What worked

- [ok] Discovery/classification: `kpi_wishlist_ops_review.md` -> kpi_input;
  `data_dictionary.csv` + `data_model_overview.md` -> data_model_input; `generator/`
  (ground truth, generator script) correctly NOT classified as any input. 8 dataset roots found.
- [ok] Onboarding scale: all 57 tables profiled (`profile_count: 57`), no warnings, no crash on
  colliding column names or mixed-type columns.
- [ok] KPI detection count: 10 of 10 markdown KPI headings became registry entries
  (`kpi_count: 10`) with stable ids kpi_001..kpi_010.

## What it got wrong vs ground truth

### F1 (headline): blocked KPIs with zero questions - a silent dead end, reported as success
Ground truth expects 9-10 of 10 KPIs to block with concrete questions (derived features,
workspace definitions, scope/grain choices, one false presupposition). Actual:

- `kpi_feature_mapping.json`: all 10 KPIs `status: blocked_questions_pending`, but
  `features: []`, `join_candidates: []`, `open_questions: []` for every KPI.
- `prepare-kpi-blocker-panel`: `question_count: 0`, `next_step: "No blocker question remains."`
- `open_questions.md`: "All KPI features resolved - no open questions." (false: nothing was
  resolved; there were no features at all)
- Blocker panel `current.md`: "No unresolved blocker questions were found."

So the workflow contradicts itself (KPIs blocked-pending-questions + no questions exist +
report says all resolved) and offers the user no path forward. The dead-end guard added in
d26230e did not fire for this shape. This is the exact "silently wrong / silently stuck"
failure Phase 2 must fix.

### F2 (root cause of F1): markdown KPI ingestion drops all prose
`_read_markdown_kpis` (core/onboarding/workspace/onboarding.py) keeps only heading text when a
heading contains "kpi". Every stakeholder sentence - the metric intent, the ambiguities, the
false presupposition, Sandra's parenthetical doubts - is discarded. Registry result: all 10
KPIs have empty `description`, `metric`, `cuts`. With no text, feature extraction yields zero
features, so the resolver has nothing to block ON and nothing to ask about.
Requirement for 2.2: prose-section ingestion (heading + body until next heading) so KPI
definitions written as natural language survive into the registry.

### F3: validator passes (`ok: true`) on a fully dead-ended workspace
`validate-workspace-artifacts` returned `error_count: 0` with only soft warnings ("kpi #N has
empty metric and cuts; resolver may block it"). A workspace where 10/10 KPIs are
blocked-with-no-question should not validate clean. Requirement: a hard validator error (or at
least a gate) when `status == blocked_questions_pending` coexists with an empty panel.

### F4: hostility targets never reached (untestable until F1/F2 fixed)
Because the flow dies before feature resolution produces anything, the baseline could not even
attempt the traps this workspace was built for. These remain open test cases for Phase 2.2/2.3:

- Base-fact selection among shipments (4,200) / movements (4,830) / invoices (4,809) - the
  "largest table" heuristic would pick movements, correct for only KPI 3.
- Non-name-matched joins (cust_ref->party.party_key, carrier_cd->carriers.scac,
  orig/dest->locs.loc_nbr, svc->svc_catalog.svc_id): the worktree-base flow generated no
  relationship contracts at all. A stray run of NEWER main-repo code (see F5) did run
  `build-relationship-contracts` against this workspace and produced
  `relationship_contracts.json` with `relationship_count: 0` - zero joins found, even though
  `docs/data_model_overview.md` documents every key join in a markdown table and the FK value
  overlap is near-100%. Evidence order lists `data_model_docs` first, yet the documented
  `cust_ref -> party.party_key` style mappings were not extracted. Non-name-matched join
  discovery is currently a total miss; this is the core Phase 2.2 lineage-first requirement.
- Dictionary reconciliation: the four false/stale entries (shipments.Amount "revenue",
  party.Status "ACTIVE/CLOSED", shipments.wgt "kilograms", phantom shipments.del_date) were
  never checked against profiles.
- KPI 5 false presupposition: never surfaced (it should block as missing-evidence, never be
  answered numerically).
- customers_legacy vs party source-of-truth question: never asked.

### F5 (environmental footnote, not a workspace finding)
`uv run run-kpi-pipeline` at this worktree base fell back via PATH to the MAIN repo's venv exe
(entry point does not exist at this base) and operated against the main repo root, failing with
"workspace not found" / "session not found". Silent cross-repo PATH fallback of `uv run` for
unknown entry points is worth a guard, but is not part of the hostile-workspace baseline.
Side effect: that stray run left main-code-generated contracts in this workspace
(`relationship_contracts.json`, `catalog_contract.json`, `data_engineering_route.json`,
`data_quality_contract.json`). They are kept as evidence (see F4) but were NOT produced by the
worktree-base flow; registry/mapping/profiles were regenerated with worktree code afterwards.

## Scorecard vs GROUND_TRUTH.md expectations

| Expectation | Result |
|---|---|
| KPIs 1,3,8,10 blocked on derived features | [x] no questions generated at all |
| KPIs 2,6,9 blocked on workspace definitions | [x] no questions generated at all |
| KPI 5 refused as false presupposition | [x] never detected |
| KPIs 4,7 one scope/grain question each | [x] no questions generated at all |
| Non-name-matched joins proposed from value overlap | [x] 0 relationships found, even the documented ones |
| Dictionary contradictions flagged | [x] never attempted |
| No silently wrong numeric answers emitted | [ok] nothing was emitted (stuck, not wrong) |
| Honest blocker surface to the user | [x] user-facing reports claim "all resolved" |

Baseline verdict: the pipeline did not produce silently WRONG answers, but it failed the honesty
bar a different way - it reported a fully-blocked workspace as having nothing to ask. F1-F3 are
the Phase 2.2 requirements input; F4 lists the still-armed traps for lineage-first resolution
and graph-based base-source selection in 2.2/2.3.
