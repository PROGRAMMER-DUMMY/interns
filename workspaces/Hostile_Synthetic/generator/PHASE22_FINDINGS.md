# Hostile_Synthetic - Phase 2.2 findings (dictionary/lineage-first resolution)

Date: 2026-06-12. Branch: `worktree-agent-a122d47b32149fd70` (base: main @ 8bc4141, gate 487).
Gate after Phase 2.2: 518 tests, 0 failing (`green-gate`).

Commands run (sequential, per the documented flow; `prepare --force-onboard`
self-deadlocks on the non-reentrant workspace lock, see Environmental notes):

1. `uv run onboard-workspace --workspace workspaces/Hostile_Synthetic`
2. `uv run prepare-kpi-blocker-panel --workspace workspaces/Hostile_Synthetic --domain logistics`
3. `uv run validate-workspace-artifacts --workspace workspaces/Hostile_Synthetic`

Baseline being scored against: `BASELINE_FINDINGS.md` (Phase 2.1) and the
answer key `GROUND_TRUTH.md` (read for scoring only; platform code never reads it).

## Fixes shipped (per finding)

### F2 (root cause): prose KPI ingestion
`_read_markdown_kpis` now captures heading + full prose body (until the next
same-or-higher heading or a thematic break) into `description`. All 10 KPIs
carry their stakeholder prose (147-410 chars each), including Sandra's
parenthetical doubts. Table-style markdown registries unchanged.

### F1 (honesty): blocked KPIs always ask something
- Resolver invariant: a KPI can never be `blocked_questions_pending` with zero
  questions; a fallback definition blocker (prose excerpt + workspace anchor
  evidence) is appended when nothing else asked.
- No-silent-ready rules: a KPI without a metric never reaches `ready_for_sql`;
  a metric machine-derived from the question text (new provenance fields:
  `authored` / `lexicon_inferred` / `derived_from_question` / `user_confirmed`)
  blocks on a confirmation feature carrying its column candidates.
- Definition blockers carry evidence: `kpi_prose` excerpt plus
  `prose_term_match` anchors scored against this workspace's own columns,
  dataset names, and dictionary descriptions (derive, don't curate).

### F3: validator hard-errors on the dead-end shape
`validate-workspace-artifacts` now returns errors (ok:false) for: a mapped KPI
blocked with zero questions and no machine-readable blocker; a panel that asks
nothing while KPIs are blocked; an `open_questions.md` claiming "All KPI
features resolved" while the mapping has blocked KPIs.

### F4a: documented joins as first-class relationship evidence
Dictionary glosses with a qualified target ("joins to party.party_key") and
data-model overview table rows (including `shipments.orig`/`dest` slash refs)
are extracted as a new `documented_data_model` state: documented-but-unproven,
never executable. A promotion pass checks each documented edge against
observed key values (dimension-key uniqueness + left->right overlap via the
existing bounded CSV readers) and upgrades to `proven_data_model` only when
the data proves it.

### F4b: lineage-first resolution; collisions block
A direct name match hitting multiple datasets resolves in evidence order:
(1) dictionary context (KPI text must share >=2 real semantic terms with one
candidate's description/table and beat every alternative by >=2; the column
name echoing itself never counts), (2) relationship/lineage unification
(colliding endpoints connected through join-worthy relationship contracts are
one logical field), (3) accepted workspace definitions, (4) name matching
LAST -- a surviving collision emits `blocked_ambiguous` with per-candidate
evidence (each candidate carries its documented meaning). The prepare wrapper
rebuilds relationship contracts before resolution so the lineage tier always
has current evidence.

### Adjacent fix: platform wiki notes excluded from input discovery
Workspace `wiki/` files are platform-written decision records; re-ingesting
them as KPI inputs created a phantom 4th KPI on the RCM workspace.

## Results on Hostile_Synthetic

- KPI registry: 10/10 KPIs with full prose descriptions; metric provenance
  recorded (`KPI 3` = `avg(hours)` `derived_from_question`, `KPI 4` cuts
  `carrier_cd` derived).
- Feature mapping: `{"kpi_count": 10, "ready_kpi_count": 0,
  "blocked_kpi_count": 10, "unresolved_feature_count": 10}` -- every KPI
  blocked WITH a concrete question (baseline: blocked with ZERO questions and
  "all resolved" reports; current-main-before-fixes: 2 KPIs silently READY on
  fabricated bindings).
- KPI 3: `hours -> timesheets.hours` is no longer silently ready; it blocks on
  a confirmation feature exposing the machine guess and its candidate column.
- KPI 4: cuts-only is no longer ready; carrier_cd stayed proven because BOTH
  `shipments.carrier_cd` and `settlements.carrier_cd` are documented FKs to
  `carriers.scac` (lineage unification), and the KPI blocks on its definition
  ask whose anchors include `cargo_claims.claim_type` (Marco's damage-vs-all
  scope ambiguity surfaces in the evidence).
- Relationship contracts: 400 edges, 19 executable -- every documented
  non-name-matched join extracted and proven with key-overlap 1.0:
  `cust_ref/acct -> party.party_key`, `carrier_cd -> carriers.scac`,
  `orig/dest/from_loc/to_loc -> locs.loc_nbr`, `svc -> svc_catalog.svc_id`,
  `veh -> vehicles.vin`, `drv -> drivers.drv_id`,
  `ship_ref/ship_no -> shipments.Id`, `vehicles.depot -> locs.loc_nbr`,
  `vehicles.class_cd -> vehicle_classes.class_cd`, plus contract/rate-card/
  quote account joins. `disputes.inv_no -> invoices.inv_no` and
  `payments.inv_no -> invoices.inv_no` correctly stay
  `documented_data_model`, NON-executable: `invoices.inv_no` is a line-level
  (non-unique) key -- the KPI 7 document-vs-line grain trap surfaced as
  evidence instead of fanning out a join.
- Traps avoided: no edge into `party.Id` (legacy CRM id); `customers_legacy`
  never endorsed as source of truth; the dictionary's false
  `shipments.Amount = revenue` claim can no longer silently bind anything
  (Amount collisions block).
- Blocker panel: current card = evidence-backed definition ask for `kpi_004`
  (prose verbatim, including Sandra's loss/delay note, with proven SQL sample
  and intent sketch); the definition-help card lists every blocked KPI with
  prose excerpt + anchors (`blocked_kpi_details`). 22 questions in the panel
  set. `open_questions.md` lists one concrete ask per blocked KPI.

## Scorecard vs GROUND_TRUTH.md expectations

| Expectation | Result |
|---|---|
| KPIs 1,3,8,10 blocked on derived features | [~] all blocked with evidence-backed asks; KPI 3 carries a JSON-backed candidate-column confirmation; leg-sequence dwell / wgt_kg UOM / composite formulas are not yet auto-proposed as derived options |
| KPIs 2,6,9 blocked on workspace definitions | [~] blocked with per-KPI definition asks whose anchors hit the right evidence (party.Status + invoices.Status for "active"; vehicles.Status for utilization; party for churn); not yet phrased as reusable workspace-definition questions |
| KPI 5 refused as false presupposition | [~] blocked, no numeric answer emitted; anchors show svc/premium_flag exist but no upgrade-event evidence; not yet explicitly labeled "presupposition unsupported by evidence" |
| KPIs 4,7 one scope/grain question each | [~] blocked with definition asks; KPI 4 anchors surface cargo_claims.claim_type (scope), KPI 7 anchors surface disputes.inv_no + invoices.inv_no and the non-unique inv_no edge documents the grain trap |
| Non-name-matched joins proposed from evidence | [ok] all documented joins extracted and proven by observed key overlap (19 executable, RI 1.0); party.Id trap avoided |
| Dictionary contradictions flagged | [x] not yet attempted (the four false/stale entries are not cross-checked against profiles; mitigated: collisions stop the false Amount gloss from resolving anything) |
| No silently wrong numeric answers emitted | [ok] ready_kpi_count 0; the two silent-ready fabrications present before Phase 2.2 (KPI 3 timesheets.hours, KPI 4 cuts-only) now block |
| Honest blocker surface to the user | [ok] 10/10 blocked with concrete evidence-backed questions; the dead-end shape is now a validator ERROR |

Verdict: the honesty bar is met -- the pipeline neither fabricates answers nor
goes silent; every KPI presents an answerable, evidence-backed blocker, and
lineage evidence is extracted and proven from the workspace's own docs. Still
open for Phase 2.3+: dictionary-vs-profile reconciliation (the four stale
entries), richer derived-feature option synthesis from prose (dwell window
logic, UOM normalization, composite "perfect shipment"), explicit
false-presupposition labeling, and reusable workspace-definition phrasing for
cross-KPI terms.

## Regression check (Healthcare-RCM-Data-Platform)

Regenerated from scratch in this worktree (onboard -> prepare -> plan ->
generate-kpi-sql -> execution harness):

- `kpi_feature_mapping.json`: 3/3 KPIs `ready_for_sql` with feature
  resolutions IDENTICAL to the main-repo baseline (PaidAmount, ServiceDate,
  LineOfBusiness, PayorID, Gender, DOB, PatientID, Name, VisitType -- all
  `proven_direct`).
- `plan-source-to-target`: `ready_kpi_count: 3, blocked_kpi_count: 0`.
- Execution harness: `ok: true`, kpi_001/kpi_002/kpi_003 all `passed`
  (kpi_002 required re-applying the human-confirmed `grain_bucketing`
  decision recorded in `wiki/features/intent_kpi_002_grain_bucketing.md`;
  the machine decision store is gitignored and absent in a fresh worktree).

## Environmental notes (not workspace findings)

- `prepare-kpi-blocker-panel --force-onboard` (and any prepare on a fresh
  workspace via `onboard_if_missing`) self-deadlocks: `prepare_main` holds the
  workspace lock and `WorkspaceOnboarder.run()` tries to acquire it again
  (non-reentrant `workspace_lock`); the timeout reports the process's own pid
  as the holder. Pre-existing at base 8bc4141; worked around by running
  `onboard-workspace` first. Worth a reentrancy fix or lock hoisting.

## Slice 3 refinements (2026-06-13)

Phase 2.2 left four hostile gaps open (dictionary reconciliation, richer
derived-feature synthesis, false-presupposition labeling, lock reentrancy).
Slice 3 addressed three of the four; this section APPENDS results without
rewriting the Phase 2.2 record above. Regenerated end-to-end on this date via
`onboard-workspace` -> `prepare-kpi-blocker-panel --domain logistics` ->
`validate-workspace-artifacts` (validator: 0 errors, 8 expected
empty-metric+cuts warnings). Platform code never reads GROUND_TRUTH.md; the
scoring below is a human cross-check.

### Updated scorecard vs GROUND_TRUTH.md

| Expectation | Phase 2.2 | Slice 3 |
|---|---|---|
| Dictionary contradictions flagged | [x] not attempted | [ok] all four false/stale entries flagged in `dictionary_conflicts.json` (4 error + 1 warning) |
| KPIs 1,3,8,10 carry JSON-backed derived options | [~] only KPI 3 candidate-column confirm | [~] KPI 8 now carries `wgt_kg` UOM-normalization option; 1/3/10 still bare asks (see open items) |
| KPI 5 false presupposition explicitly labeled | [~] blocked, unlabeled | [~] still unlabeled here (see open items) |
| `prepare --force-onboard` reentrancy | [x] self-deadlock | [ok] fixed: `workspace_lock` is re-entrant per pid+thread |
| No silently wrong numeric answers | [ok] ready=0 | [ok] ready=0; nothing fabricated |
| Honest evidence-backed blocker per KPI | [ok] 10/10 | [ok] 10/10 |

### 1a - workspace-lock reentrancy (commit 8a2fb48)
`workspace_lock` now tracks owner `(pid, thread)` + depth, so nested
acquisition in one process (CLI lock -> `WorkspaceOnboarder.run()`) no longer
self-deadlocks; cross-thread/cross-process exclusion is unchanged. The
"Environmental notes" deadlock above is resolved.

### 1b - dictionary-vs-profile reconciliation (commit 322592e)
New `core/onboarding/documents/dictionary_reconciliation.py` cross-checks every
documented claim against profile evidence. On Hostile_Synthetic it emits 5
conflicts covering all four designed lies:
- `shipments.wgt` "kilograms" -> `unit_mismatch` (error): profiled values are
  mixed KG/LB.
- `shipments.Amount` "final invoiced revenue" -> `misattributed_claim`
  (warning, names `invoices.csv`) + `unit_mismatch` (error, GBP claim vs mixed).
- `shipments.del_date` -> `phantom_column` (error): no profiled dataset has it.
- `party.Status` "ACTIVE/CLOSED" -> `enum_mismatch` (error): observed A/C/S.
Error-severity conflicts demote a proven feature on the tainted column to an
answerable `dictionary_conflict` blocker; the validator fails any ready KPI
consuming an error-conflicted column without a `user_confirmed` decision.

### 1c - no_supporting_evidence labeling (commit 2ea52f1)
The resolver labels a blocked KPI `no_supporting_evidence` when NO prose term
anchors to any profiled column, dataset name, dictionary description, or
accepted definition (panel then asks confirm-absence-or-point-at-source).
Note: on this RICH 57-table workspace, every KPI (including 5 and 9) anchors to
at least one column once the dictionary is enriched, so the strict zero-anchor
label does not fire here. The feature is exercised by unit tests and fires on
genuinely anchorless KPIs.

### 1d - derived-feature option synthesis (commit 532d950)
A new generic, evidence-driven detector recognizes a quantity stored in MIXED
units (numeric column + sibling unit-of-measure column with >=2 observed unit
codes) and proposes a JSON-backed normalization option. KPI 8 now carries:
`wgt_kg = CASE upper(trim("wgt_uom")) WHEN 'KG' THEN "wgt" WHEN 'LB' THEN
"wgt" * 0.45359237 ELSE NULL END` (the GROUND_TRUTH-expected formula and
factor). Detection uses universal physical constants only; no domain
vocabulary or per-workspace column list.

### Still open after Slice 3
- **KPI 1 (on_time) derived option**: needs a cross-table SLA-deadline
  comparison (`date(END) <= date(START) + svc_catalog.sla_days`) -- a join-fed
  derivation the current single-table detectors do not synthesize. Honest
  blocker stays.
- **KPI 3 (dwell) derived option**: needs a consecutive-leg self-join window
  (`START` of leg n+1 minus `END` of leg n, attributed to the intermediate
  facility) -- ordered self-join, not yet a detector. Honest blocker stays.
- **KPI 10 (perfect shipment) derived option**: composite of KPI 1/4/7
  outcomes; depends on those derivations existing first. Honest blocker stays.
- **KPI 5 explicit false-presupposition label**: `svc`/`premium_flag` exist so
  the KPI is not zero-anchor; flagging it specifically needs event-level
  evidence-absence detection (no post-dispatch service-change audit grain),
  which is stronger than the 1c zero-anchor rule. Honest blocker stays
  (no numeric answer emitted).
