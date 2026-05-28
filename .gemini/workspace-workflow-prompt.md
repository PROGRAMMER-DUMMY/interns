# Workspace Workflow Launch Prompt

Read `GEMINI.md`, `AGENTS.md`, `TOOLS.md`, and `.agents/tools.json` before doing anything.

Use short user intents, but execute governed repo commands:

- Prefer `uv run workspace-flow ...` for end-to-end workspace workflows. Use lower-level commands
  only when debugging a specific failed stage.
- `generate KPIs by interview`: run
  `uv run workspace-flow start --workspace <workspace> --intent kpi_generation --domain <domain>`.
- `usual workflow`, `generate SQL`, or `run the KPI pipeline`: run
  `uv run workspace-flow start --workspace <workspace> --intent full_kpi_sql --domain <domain>`.
- `continue` after a workspace-flow question: run
  `uv run workspace-flow answer --session <session_id> --answer <option_or_text>`.
- `show results`: run `uv run workspace-flow results --session <session_id>`.
- `set <workspace>` or a fuzzy workspace name: resolve it against `workspaces/`, run only `uv run list-workspace-files --workspace <workspace>`, then ask for workspace confirmation.
- After confirmation, `start onboarding`, `prepare blockers`, `continue`, or `next`: run `uv run prepare-kpi-blocker-panel --workspace <workspace> --domain <domain>`.
- Before asking any KPI blocker question, rely on `interns/reports/blocker_question_panel/current.json` or `current.md`, and require successful `validate-workspace-artifacts`.
- Render `current.md` verbatim as the human-facing panel. Do not collapse blocker, approval,
  KPI-generation, data-model, duplicate-review, or pipeline-format panels into Gemini's generic
  `Ask User` / `Answer Questions` box.
- Use `current.json` only for exact option buttons and answer application.
- `accept option A` or `choose A`: run `uv run apply-kpi-panel-answer --workspace <workspace> --domain <domain> --answer option_a`.
- `show blocker json`: show `<workspace>/interns/reports/blocker_question_panel/current.json`.
- `show blocker markdown`: show `<workspace>/interns/reports/blocker_question_panel/current.md`.
- If the user asks to generate executable KPI SQL, generate every requested KPI, run
  `validate-workspace-artifacts`, and then explicitly ask whether to execute/preview results unless
  the user already asked to see results.
- If the user asks to show queries and results, show all requested KPIs, not examples. For each KPI,
  include the solution path, the generated SQL or a concise excerpt, and the result/preview returned
  by executing the SQL. If terminal output is truncated, rerun narrower per-KPI commands rather than
  summarizing only the visible tail.

Do not hand-chain lower-level resolver commands unless debugging a specific failing stage.
Do not invent unsupported flags such as `--accept-option`.
Do not ask from truncated command output.
Do not present "Recommended" as if it is the user's only answer. Treat it as an instruction or safe
default, then show the concrete option ids the user can choose.
Do not use yolo mode or bypass permissions for this workflow.

# Read & shell rules (Windows PowerShell host)

- Read workspace files in place via `ReadFile` / `SearchText`. Never `copy` a workspace
  file to the working directory just to read it; that is a banned habit.
- To find a string inside a file, use `SearchText` (or `Select-String` in PowerShell).
  Do NOT escalate `Get-Content -TotalCount N` repeatedly to "grep by reading more lines".
- On PowerShell, chain commands with `;` (or `; if ($?) { ... }` for "only on success").
  `&&` is a parser error in PowerShell 5.1 — never retry it.
- Before calling `workspace-flow start`, check `interns/state/workflow_sessions/` for an
  open session for this workspace. If one exists and is recent, RESUME it via
  `workspace-flow status --session <id>` and `workspace-flow status --diff --workspace <ws>`
  instead of minting a new session.

# Recovery without re-running

- When a panel reports a blocker, prefer `workspace-flow status --diff --workspace <ws>`
  to learn the exact missing pieces and recommended `apply-*` commands, instead of
  re-running `workspace-flow start` (which re-traverses the whole pipeline).
- A panel JSON's `summary.recovery_commands` and `summary.suggested_skills` arrays are
  the source of truth for the next step. Render them inline; do not paraphrase.

# Subagent delegation

- The Gemini subagents under `.gemini/agents/` ARE available. Delegate narrow
  read-or-extract tasks (e.g., "list relationship_ids in this 553-line JSON") to a
  subagent that returns only the extracted answer. Do not load large artifacts into the
  main chat context just to scan them.

# Required-specialist + suggested-skills enforcement (hard rule)

- Every panel JSON may carry `summary.required_specialists` (list of agent names)
  and `summary.suggested_skills` (list of `{name, why}`). These are not advisory.
  Before answering, rendering, or acting on a panel, you MUST:
  1. Activate every skill named in `summary.suggested_skills` (load its `SKILL.md`).
  2. For every agent in `summary.required_specialists`, either invoke the matching
     subagent for review, OR include in your reply why you are choosing not to
     invoke it (e.g., "trajectory shows it already fired in this session at <stage>").
- Every panel may also carry `summary.delegations` — the programmatic verdicts
  workspace-flow already captured on behalf of each specialist. Render those verdicts
  inline in your reply so the user sees who reviewed what.
- Never strip `required_specialists`, `suggested_skills`, or `delegations` from a
  panel before showing it to the user.
