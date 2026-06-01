# Claude Code Init

Read `AGENTS.md` first and follow it as the canonical operating guide for this repo.
Then inspect `TOOLS.md` and `.agents/tools.json` before inventing workflows or helper scripts.

For startup commands such as `set <workspace>`, `set current workspace to ...`, or a bare project
name, treat the message as workspace selection only. File mutation during selection is a hard stop.

## Selection-only checklist

[ok] Allowed during workspace selection:
- `uv run list-workspace-files --workspace workspaces/<project>` (read-only scan)
- Bounded PowerShell fallback: `Get-ChildItem -LiteralPath workspaces/<project> -Force -File -Recurse | Select-Object -First 200 -ExpandProperty FullName`
- `git status --short` (read-only)
- Reading `config/tasks.json` (read-only)
- Presenting the file-set summary and asking the confirmation question

[x] Forbidden until the user has confirmed the workspace AND explicitly authorized continuing:
- Any call to Edit, Write, or a file-creation tool on ANY file
- Any call to Bash/PowerShell that creates, overwrites, deletes, or moves a file
- Editing or touching `.gitignore`, `.geminiignore`, `settings.json`, `settings.local.json`,
  generated artifacts, workspace files, or repo-root files of any kind
- Running onboarding, profiling, feature resolution, or any `apply-*`/`finalize-*` command
- Staging or committing files

If the agent finds itself about to call Edit/Write during a selection turn, it must stop, discard
the mutation, and ask the confirmation question instead.

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

Workspace scans must bypass gitignore rules. If a folder reader says a workspace has zero items but
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

Repo skills are indexed in `.agents/claude/SKILLS.md`, but the canonical skill bodies live in
`skills/*/SKILL.md`.
