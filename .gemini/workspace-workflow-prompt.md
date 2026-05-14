# Workspace Workflow Launch Prompt

Read `GEMINI.md`, `AGENTS.md`, `TOOLS.md`, and `.agents/tools.json` before doing anything.

Use short user intents, but execute governed repo commands:

- `set <workspace>` or a fuzzy workspace name: resolve it against `workspaces/`, run only `uv run list-workspace-files --workspace <workspace>`, then ask for workspace confirmation.
- After confirmation, `start onboarding`, `prepare blockers`, `continue`, or `next`: run `uv run prepare-kpi-blocker-panel --workspace <workspace> --domain <domain>`.
- Before asking any KPI blocker question, rely on `interns/reports/blocker_question_panel/current.json` or `current.md`, and require successful `validate-workspace-artifacts`.
- `accept option A` or `choose A`: run `uv run apply-kpi-panel-answer --workspace <workspace> --domain <domain> --answer option_a`.
- `show blocker json`: show `<workspace>/interns/reports/blocker_question_panel/current.json`.
- `show blocker markdown`: show `<workspace>/interns/reports/blocker_question_panel/current.md`.

Do not hand-chain lower-level resolver commands unless debugging a specific failing stage.
Do not invent unsupported flags such as `--accept-option`.
Do not ask from truncated command output.
Do not use subagents unless the user explicitly asks for parallel agent work.
Do not use yolo mode or bypass permissions for this workflow.
