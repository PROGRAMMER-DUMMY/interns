---
name: workspace-flow-operator
description: Use when a Codex plugin, Slack/Teams bridge, MCP wrapper, or CLI operator needs to run governed Autoresearch workspace workflows without duplicating core workflow logic.
---

# Workspace Flow Operator

## Contract

Use existing repo commands as the only workflow backend:

- `uv run workspace-flow start`
- `uv run workspace-flow status`
- `uv run workspace-flow answer`
- `uv run workspace-flow results`
- `uv run session-snapshot ...`
- `uv run record-workspace-trajectory ...`
- `uv run validate-workflow-guardrails --workspace <workspace>`

## Integration Rules

- Map each external Slack, Teams, MCP, or plugin thread to `{workspace, session_id}`.
- Show `current.md` to humans.
- Use `current.json` for buttons, option ids, and structured tool responses.
- Record accepted decisions and integration events through existing session or trajectory tools.
- Do not read raw datasets, edit generated contracts, or run remote execution from an integration adapter.
- Ask for explicit approval before any delete, remote execution, or production mutation.
