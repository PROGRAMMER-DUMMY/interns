# Enterprise Agent Integrations

## Decision

Subagents are canonical repo assets. Define them in `skills/<skill>/agents/*.yaml`, then run
`uv run generate-skill-adapters` to refresh `.agents/subagents_index.json` and the generated
tool adapters.

## Core Roles

- `workspace-flow-orchestrator`: owns workspace-flow sessions and deterministic next commands.
- `source-to-target-reviewer`: blocks executable generation when source proof is incomplete.
- `validation-gatekeeper`: runs local-safe validation and harness checks.
- `integration-notification-operator`: maps Slack, Teams, MCP, or plugin threads to existing
  workflow/session tools.

## Slack And Teams Bridge

Slack and Teams adapters must stay thin:

- Map `{channel, thread, user}` to `{workspace, session_id}`.
- Post `current.md` for human-readable prompts and summaries.
- Use `current.json` to build buttons and preserve option ids.
- Apply answers through existing repo commands, not custom blocker/KPI logic.
- Record events with `session-snapshot` or `record-workspace-trajectory`.

## Plugin And MCP Boundary

The repo-local Codex plugin lives at `plugins/autoresearch-workflow/`. It packages operator guidance
only; it does not introduce a second workflow engine. MCP wrappers should expose the same
`workspace-flow start/status/answer/results` surface.

## Safety

- Read-only analysis roles should not write files.
- Implementer or validation roles may write only generated artifacts required by local-safe tools.
- Remote execution, deletes, production mutations, and external notifications that affect humans
  require explicit approval.
