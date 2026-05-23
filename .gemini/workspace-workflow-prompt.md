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
Do not use subagents unless the user explicitly asks for parallel agent work.
Do not use yolo mode or bypass permissions for this workflow.
