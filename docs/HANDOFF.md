# Handoff — remaining work as of 2026-06-12 EOD

State: `main`, clean tree, green gate **632/632** (`green-gate`). Everything
below is verified-not-started or explicitly user-owned. History/context:
roadmap memory (`project_enterprise_roadmap`) + `docs/prd/databricks_deployment.md`.

Done today, for orientation: Phases 0-3 complete; chart-selection knowledge
base (data-to-viz) + 14 chart types; dashboard screener with vision-review
provenance; per-KPI data viewer (redacted, CSV, sortable); user data policies
(`data_policy.json`); Databricks plan-apply gates G1-G5 +
`medallion apply-deploy` stopping at the approval boundary.

---

## 1. Slice 3 — hostile refinements + HPR (NOT STARTED, investigated)

Scope is the `[x]`/`[~]` rows in
`workspaces/Hostile_Synthetic/generator/PHASE22_FINDINGS.md` (lines ~105-150).
Constraint reminders: workspace-agnostic (no healthcare/logistics words in
platform code), derive-don't-curate, tests registered in
`core/dev/green_gate.py` CURATED_MODULES + `.github/workflows/ci.yml`,
commit per item.

### 1a. Workspace-lock re-entrancy (smallest; start here)
- Symptom: `prepare-kpi-blocker-panel --force-onboard` self-deadlocks —
  `prepare_main` holds the lock, then `onboard_if_missing` re-acquires.
- Found so far: lock lives at `core/storage/workspace_lock.py` (context
  manager `workspace_lock(...)` at line ~166, `WorkspaceLockTimeout` at ~27).
  Nesting call sites: `core/onboarding/kpi/blocker_cli.py:42` and `:161`
  (`with workspace_lock(workspace_path):` wrapping calls that may re-lock).
- Fix: make acquisition re-entrant within a process (track owner pid/thread +
  depth in the lock), or restructure `blocker_cli` to not nest. Re-entrant
  lock is the better fix (other wrappers may nest too). Add a regression test
  (nested `with workspace_lock(...)` must not deadlock; cross-process still
  blocks).

### 1b. Dictionary-vs-profile reconciliation (the `[x]` item)
- The hostile dictionary (`workspaces/Hostile_Synthetic/docs/data_dictionary.csv`)
  lies 4 times: shipments.Amount "is revenue"; party.Status "ACTIVE/CLOSED"
  (actual values A/C/S); wgt "kilograms" (30% of values LB); phantom column
  `del_date`.
- Build an evidence-reconciliation pass (suggested home: feature resolution
  or `validate-workspace-artifacts`) cross-checking dictionary claims against
  profile evidence: declared enums vs observed values, claimed units vs value
  patterns, entries for nonexistent columns. Emit structured
  `dictionary_conflicts` into contracts + a blocker-panel question when a
  conflicted column feeds a KPI. Evaluate against
  `workspaces/Hostile_Synthetic/generator/GROUND_TRUTH.md` (platform code
  must never read GROUND_TRUTH; you may, to score).

### 1c. `no_supporting_evidence` labeling (KPI 5)
- Hostile KPI 5 ("premium upgrade after dispatch") presupposes data that
  exists nowhere; today it blocks generically. Detect the stronger condition —
  no term in the KPI prose maps to ANY column/dictionary/definition evidence —
  and label the blocker `no_supporting_evidence` with a panel question saying
  the KPI may presuppose data the workspace lacks (confirm or point at the
  source). Resolver: `core/onboarding/kpi/feature_resolver.py` (prose-term
  anchor machinery from Phase 2.2 is the input: `prose_term_match` evidence).

### 1d. Derived-feature option synthesis (the `[~]` rows; largest, optional last)
- On Hostile_Synthetic, KPIs 1,3,8,10 should carry JSON-backed
  `derived_feature_options` (per GROUND_TRUTH) but get bare definition-asks.
  Suspected cause: prose-described features never reach
  `DerivationPatternSearcher` input shape (see
  `core/onboarding/features/derivation_search.py`). Investigate the chain;
  fix so evidence-backed candidates appear; honest blockers stay when
  evidence genuinely absent.

### 1e. HPR re-onboard + scorecard
- `workspaces/Hospital_Patient_Records` is reset/fresh. Run:
  `uv run onboard-workspace --workspace workspaces/Hospital_Patient_Records`,
  then `uv run prepare-kpi-blocker-panel --workspace workspaces/Hospital_Patient_Records --domain healthcare`,
  then `uv run validate-workspace-artifacts ...`. Do NOT invent/auto-confirm
  human answers — blocked-on-definitions is the correct end state; commit
  artifacts + report what a human must answer.
- Re-run the hostile pipeline end-to-end; APPEND a "refinements" section to
  PHASE22_FINDINGS.md (don't rewrite history).
- Regression: RCM execution harness stays 3/3
  (`.venv\Scripts\python.exe -m core.onboarding.kpi.execution_harness --workspace workspaces/Healthcare-RCM-Data-Platform`).

## 2. Databricks deployer slice (next major feature)
- `medallion apply-deploy` now stops at `interns/state/medallion/deploy_approval.json`.
  Next: `core/onboarding/databricks/workspace_deployer.py` consumes that
  artifact (must REFUSE without a fresh one — re-verify G4 hash + G5 env at
  execution time) and performs the Unity Catalog deployment described in
  `deploy_plan.json`. Seam documented in PRD section 9. Live calls remain
  behind `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` (human-set only).

## 3. Fingerprint-helper unification (deliberate duplication)
- `core/onboarding/workspace/incremental.py` vs `core/medallion/incremental.py`
  grew same-day sibling fingerprint helpers. Fold into one shared helper when
  either next changes. Recorded in PRD section 10.

## 4. User-owned decisions (blocking items)
- **Ratify the 5 RCM medallion design decisions** (`fact:department`,
  `fact:encounter`, `fact:patient`, `fact:provider`, `fact:transaction`) via
  the design panel (`interns/reports/medallion_design_panel/current.md`).
  This is the literal G2 blocker for apply-deploy and for local Silver builds.
- **Gemini CLI dies June 18**: keep-or-drop the parked `agy` smoke test.

## Operational notes for the next session
- Subagent quota burned out twice today (~300-token deaths); resets 12:10am
  IST. If relaunching agents: verify the worktree base is current main FIRST
  (one agent landed 114 commits stale and correctly refused), and mandate
  incremental commits.
- Hooks: `uv run` is blocked for tests/pyspark — use
  `.venv\Scripts\python.exe -m unittest <modules>` or portable `green-gate`.
- Dashboard verification loop: `uv run workspace-dashboard --workspace <ws>
  --screen`, then Read the staged shots, then
  `--record-vision-review --reviewed-by <name>` (workflow guard flags it
  if skipped).
