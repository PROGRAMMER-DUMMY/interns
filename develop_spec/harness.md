# develop_spec/harness.md — running the workspace harness

How we drive the deterministic KPI/onboarding harness while developing. These
are **governed `uv run` wrappers** and are allowed under the `uv run` guard hook
(only tests / pyspark / engine-generation are blocked from `uv run`; see
`testing.md`).

Use a synthetic / sample workspace under `workspaces/` for dev. Substitute
`<ws>` = `workspaces/<project>` and `<domain>` (e.g. `healthcare`, `general`).

## One-shot deterministic chain (preferred)

```powershell
uv run run-kpi-pipeline --workspace <ws> --domain <domain>
```

Runs onboard -> blocker resolution -> contracts -> generation -> results, and
stops only at genuine human gates. Lowest token cost; emits the result packet
once. Prefer this over issuing each step manually.

## Individual steps (when you need to inspect a stage)

```powershell
uv run onboard-workspace            --workspace <ws>
uv run resolve-kpi-features         --workspace <ws> --domain <domain> --include-candidates
uv run prepare-kpi-blocker-panel    --workspace <ws> --domain <domain>
uv run apply-kpi-panel-answer       --workspace <ws> --domain <domain> --answer <option_id>
uv run validate-workspace-artifacts --workspace <ws>
uv run plan-kpi-completion          --workspace <ws>      # parallel completion plan
```

- `onboard-workspace` regenerates everything under `<ws>/interns/` (profiles,
  contracts, normalized registry, feature mapping, evidence). Safe to re-run.
- `resolve-kpi-features` prints `question_panel_markdown_path`; if
  `blocked_kpi_count` is nonzero, read that `.md` next — do not invent a separate
  interview.
- `validate-workspace-artifacts` is the post-step check; treat errors (not
  warnings) as blockers. Do not hand-fix contracts to satisfy it.
- `plan-kpi-completion` writes `<ws>/interns/reports/parallel_completion/current.md`
  (dependency-aware worker plan; shared blockers resolved once, independent
  components fanned across 2/4/6 workers).

## Reading harness output fast

- Read the `.md` summary a wrapper points to, not the machine JSON it pairs with.
- Result packets are authoritative: forward
  `<ws>/interns/reports/kpi_results/current.md` verbatim; never re-type SQL from
  memory.

## Dev-mode boundaries for the harness

- Synthetic / sample workspaces only; no real-data or remote (Databricks)
  execution without an explicit access gate.
- Never print `.env`/secret values; report redacted key names only.
- File mutation only after the workspace is confirmed (see `AGENTS.md`
  selection rules).
