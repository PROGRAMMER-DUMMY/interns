# Gemini CLI Init

Read `AGENTS.md` first and follow it as the canonical operating guide for this repo.
Then inspect `TOOLS.md` and `.agents/tools.json` before inventing workflows or helper scripts.
For Gemini CLI command, configuration, policy, tool, and memory behavior, use
`docs/agents/gemini-cli-reference.md` as the repo-local reference.

For startup commands such as `set <workspace>`, `set current workspace to ...`, or a bare project
name, treat the message as workspace selection only. Do not create, edit, or write files from that
command.

Resolve fuzzy workspace names against existing folders under `workspaces/` and entries in
`config/tasks.json`. Then scan likely files, summarize the active workflow, ask for confirmation,
and only after confirmation continue from the highest-priority blocker. If the workspace is blocked
by KPI mappings, missing definitions, access, approvals, or contradictory evidence, start the
automatic blocker grilling flow from `AGENTS.md`.

After the user confirms a fresh KPI workspace, do not ask a generic "what would you like to do
next?" question. If the workspace has KPI registry/data model/datasets but no `interns/` artifacts
or task config entry, propose the deterministic next step:
`uv run onboard-workspace --workspace workspaces/<project>`. Say this will generate profiles,
contracts, normalized KPI registry, feature mapping, open questions, and evidence artifacts under
`workspaces/<project>/interns/`.

After a shell command completes successfully, respond quickly. Do not spend minutes narrating,
re-reading generated artifacts, or expanding hidden output. Summarize from the returned command
output in under 30 seconds, list key artifact paths, and give the next deterministic command. After
`onboard-workspace`, the next command is usually
`uv run resolve-kpi-features --workspace workspaces/<project> --domain <domain> --include-candidates`.
`resolve-kpi-features` prints `question_panel_path` and `question_panel_markdown_path`; if
`blocked_kpi_count` is nonzero, read `question_panel_markdown_path` next and do not invent a
separate interview.
After onboarding, feature resolution, derived-feature markdown, or question-panel generation, run
`uv run validate-workspace-artifacts --workspace <workspace>`. Treat validator errors as blockers
and do not manually rewrite generated contracts like `kpi_registry.json`.
For KPI blocker preparation, prefer the deterministic wrapper:
`uv run prepare-kpi-blocker-panel --workspace <workspace> --domain <domain>`. It owns missing
onboarding, feature resolution, derived-feature Markdown, panel generation, and validation.

Workspace scans must bypass gitignore rules. If `ReadFolder` says a workspace has zero items but
also reports ignored items, do not conclude the workspace is empty. Use
`uv run list-workspace-files --workspace workspaces/<project>` and summarize that shaped output.

If `rg` is unavailable, use this bounded PowerShell fallback instead:
`Get-ChildItem -LiteralPath workspaces/<project> -Force -File -Recurse | Select-Object -First 200 -ExpandProperty FullName`.

Workspace selection scans must be bounded. For `set <workspace>`, run
`uv run list-workspace-files --workspace workspaces/<project>` first. Do not read raw dataset
contents, profile datasets, parse Excel contents, or run onboarding before confirmation. After the
command returns, do not run more scans or perform extended reasoning; respond within 10 seconds using
only the tool output and `config/tasks.json`. Treat the `All files` section as the confirmation
boundary; ask whether the user wants to use that full file set, not only the classified KPI/model
matches. Labels such as `Possible KPI files` and `Possible data model files` are hints only, not
ground truth.

When the user answers `yes` to the workspace confirmation and the workspace is fresh, do not recheck
the same files or ask a generic follow-up. Within 10 seconds, either run
`uv run onboard-workspace --workspace workspaces/<project>` if the confirmation allowed continuing,
or ask one direct approval sentence that names that exact command.

Secret display is a hard stop. Do not print or paste `.env`, `.databrickscfg`, private keys,
tokens, shell environment dumps, connection strings, bearer headers, cookies, or config/AST/tree
dumps that include secret values. Report only existence/status or redacted key names such as
`OPENAI_API_KEY=<redacted>`. If sensitive output appears, suppress it in the response.

For KPI blockers, cluster unresolved features across all KPIs first. Ask for reusable workspace
definitions before KPI-specific exceptions, save accepted answers as workspace-level definitions,
and reuse them automatically for every KPI they apply to.

Before asking any KPI blocker question, run
`uv run blocker-question-panel --workspace <workspace>` and ask from
`interns/reports/blocker_question_panel/current.json` or `current.md` only. Do not create freehand
Ask User prompts for direct mappings, source-of-truth choices, aliases, workspace definitions, or
derived features. If the panel files are missing, generate them first, then run
`uv run validate-workspace-artifacts --workspace <workspace>`.
When the user answers a panel option, apply it with
`uv run apply-kpi-panel-answer --workspace <workspace> --domain <domain> --answer <option_id_or_label>`.
Do not invent unsupported flags such as `--accept-option`, and do not edit generated contracts by
hand.

KPI blocker UI rules:

- For blocker, approval, KPI-generation, data-model, duplicate-review, and pipeline-format panels,
  render the generated `current.md` verbatim as the human-facing card before asking for an answer.
- Do not replace `current.md` with Gemini's generic `Ask User` / `Answer Questions` input box.
- Do not summarize away KPI source truth, AI understanding, evidence, SQL preview, result demo, or
  actions.
- Use `current.json` only for exact option/button rendering and answer application. Preserve the
  option labels, option ids, ordering, recommended option, and business summaries.
- Do not invent, rename, reorder, or simplify blocker options outside the panel artifact.
- Do not ask from hidden command output. If output is truncated, read the relevant `current.md` or
  `current.json` file explicitly before asking.
- If the panel validation fails, stop and report the validation errors instead of asking the user to
  choose from a malformed panel.

For derived-column blockers, prose-only options are invalid. Show JSON-backed options with
`derived_column_name`, `formula`, `input_columns`, `observed_values`, `value_profile`,
`semantic_meaning_sources`, per-column `reason`, `example`, `evidence_sources`,
`derivation_reasoning`, `evidence_state`, `confidence`, and `needs_user_confirmation`. If
`derived_feature_options` exist, run `uv run derived-feature-markdown --workspace <workspace>` and
then `uv run blocker-question-panel --workspace <workspace>`, followed by
`uv run validate-workspace-artifacts --workspace <workspace>`. Ask from
`interns/reports/blocker_question_panel/current.json` or `current.md`, not from freehand prose.

Do not offer semantically mismatched derived-feature candidates as selectable options. If the review
shows unrelated patterns for the requested feature, state that they are rejected and ask for a direct
mapping, source-origin rule, data dictionary evidence, or workspace business definition.

For dataset questions, use generated profile artifacts before raw data. Read
`interns/generated/profiles/profile_index.json` and relevant `*.profile.json` files first; use
bounded samples only when profiles are insufficient.

For SQL, Polars, PySpark, ETL, or medallion requests, do not generate code from KPI text alone. Use
the KPI requirements plus the data model to choose source datasets, joins, grain, filters, and
loading layers. If the data model does not prove the source table, join key, grain, temporal anchor,
or target layer, run the blocker-panel workflow instead of writing executable logic.

Repo skills are indexed in `.agents/gemini/SKILLS.md`, but the canonical skill bodies live in
`skills/*/SKILL.md`.

## Human-Friendly Command Rule

Users should not be expected to type full internal CLI commands. Map short user intents to governed
repo commands:

- `set rcm data`: run workspace selection only with `uv run list-workspace-files --workspace ...`
  and ask for confirmation.
- `start onboarding`, `prepare blockers`, `continue`, or `next` after workspace confirmation: run
  `uv run prepare-kpi-blocker-panel --workspace <workspace> --domain <domain>`.
- `accept option A`, `choose A`, `use recommendation`: run
  `uv run apply-kpi-panel-answer --workspace <workspace> --domain <domain> --answer option_a`.
- `show markdown`: show `interns/reports/blocker_question_panel/current.md`.
- `show json`, `show blocker json`: show `interns/reports/blocker_question_panel/current.json`.
- `show accepted definitions json`: show
  `interns/generated/contracts/workspace_feature_definitions.json`.
- `start fresh`, `clear workspace`: run cleanup dry-run first and require explicit delete
  confirmation.

Never ask users to type long internal commands unless they explicitly ask for CLI syntax.

## JSON Display Rule

If the user asks for JSON format, do not use the interactive picker and do not summarize in prose.
Show the relevant generated JSON artifact:

- Current blocker panel: `interns/reports/blocker_question_panel/current.json`
- Derived feature reviews: `interns/reports/derived_feature_reviews/json/*.json`
- Accepted workspace definitions: `interns/generated/contracts/workspace_feature_definitions.json`
- KPI registry: `interns/generated/contracts/kpi_registry.json`
- Feature mapping: `interns/generated/contracts/kpi_feature_mapping.json`

If multiple JSON files match, list the available files and ask which one to show.

## Agent Delegation Rule

Do not use subagents, background agents, or multi-agent review for normal workspace onboarding,
blocker preparation, or KPI feature resolution. Use subagents only when the user explicitly asks for
parallel agent work, subagents, workers, or a review team.

## Data Isolation Rule

When the operator scopes a workspace to a subset of its datasets, persist that scope as a
`dataset_allowlist` in `workspaces/<project>/interns/state/workspace_settings.json` BEFORE running
onboarding, profiling, or generation. If the allowlist file already exists, honor it in every
session without being re-told. All downstream stages must read only from allowlisted paths. (Full
rule: `AGENTS.md` > Dataset Isolation Rule.)

## KPI Result Packet Forwarding Rule

When the KPI pipeline finishes, present the results automatically in the same turn — the user must
never have to type "show results" / "show me the results" to see the tables. The `complete` and
`results` stages render each KPI's definition + generated SQL + result table inline in their panel
markdown, and the same packet is written to `interns/reports/kpi_results/current.md`.

- Forward the emitted packet verbatim. Read and display
  `workspaces/<project>/interns/reports/kpi_results/current.md`; do NOT re-type, paraphrase, or
  reconstruct the generated SQL or result rows from memory (re-authoring from memory caused a
  fabricated data-source render, BUG-015).
- Do not stop at "Next Step: review result artifacts" and wait for the user to ask. If a run finished
  but you only printed paths or a one-line status, that is an under-presentation bug — surface the
  rendered tables.
- If the user does ask "show results" after the fact, still forward the file; never rebuild it.
- Compact vs full: by default forward the COMPACT packet (`interns/reports/kpi_results/current.md`,
  same content as `interns/runs/<date>/results.md`). If the user asks for "full results" /
  "entire results", forward the FULL packet (`current_full.md` / `runs/<date>/results_full.md`)
  verbatim — never answer a full-results request with the compact packet or a hand-built summary.

### Results read discipline (token/quota guardrail)

Reading the packet must be ONE cheap read. Re-reading the results in many forms in a single
"show me the results" turn burned ~7% of a quota in one go -- do not repeat that.

- Read `interns/reports/kpi_results/current.md` with the NATIVE `ReadFile` tool, NOT a shell
  command (`Get-Content`/`cat`/`powershell`). Shell output is summarized/capped by
  `model.summarizeToolOutput.run_shell_command.tokenBudget` (12000), which truncates a long read
  and is exactly what makes the re-read loop start. `ReadFile` is not shell-summarized and returns
  the whole compact file in one read. `current.md` is now the COMPACT packet (SQL is linked, not
  inlined; the full inlined-SQL packet is `current_full.md`), so it is small.
- Do NOT re-read the same file with `-TotalCount`, `-Head`, `-Tail`, `-Raw`, `-Encoding`,
  `Select-String`, or `workspace-flow results --preview-rows N` back-to-back to "see more" -- they
  all return the same packet. One `ReadFile` is the whole thing.
- For many KPIs, forward the per-KPI files `interns/runs/<date>/kpi_<id>.md` (one ReadFile each,
  each self-contained) instead of the combined file -- this never exceeds the read cap.
- NEVER `Get-Content` the evidence JSON `interns/generated/evidence/kpi_results/current.json`
  (a ~2000-line machine artifact). Read the paired `.md`. (CLAUDE.md/AGENTS.md token discipline.)
- NEVER use `-Wait` or any follow/stream flag on these files -- it hangs until cancelled.
- If the CLI display shows "... first N lines hidden ...", the read SUCCEEDED -- that is a UI
  truncation, not a failure. Forward what was read; do not retry with another command.

## Grain-Bucketing Blocker Rule

When the execution harness blocks a share/percentage KPI on a grain-bucketing decision (a raw
continuous cut like Age/DOB fragmenting the denominator), the blocker question panel shows NO
options -- this is a pipeline decision, not a feature blocker. Do NOT loop on
`apply-kpi-panel-answer` or `workspace-flow answer` (they error with "current panel has no options"
/ "not waiting for a supported answer"). Apply it deterministically, then re-run generation:

```
uv run apply-pipeline-decision --kpi-id <kpi_id> --grain-bucketing band_continuous_cuts
uv run workspace-flow start --workspace workspaces/<project> --intent full_kpi_sql --domain <domain>
```

Use `band_continuous_cuts:<width>` for a non-default band width (default 10), or `exact_value_grain`
only if exact-value rows are genuinely wanted. (The panel route for this facet is a known open bug --
see `develop_spec/follow_ups.md`.)
