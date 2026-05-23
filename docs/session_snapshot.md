# Session Snapshot

`session-snapshot` records exact end-user conversation history across CLI tools. It is for
monitoring and audit: what the user asked, what the assistant answered, which commands ran, which
files changed, and which decisions were accepted.

Session files are local runtime artifacts under `.agents/sessions/` and are ignored by git.

## Start A Named Session

Use `--name` for normal work. The tool creates a timestamped folder and remembers the name as an
alias:

```powershell
uv run session-snapshot start --name gemini-hospital --workspace workspaces/Hospital_Patient_Records --tool gemini
```

This creates a folder like:

```text
.agents/sessions/2026-05-20-131855-gemini-hospital/
```

and records the alias in:

```text
.agents/sessions/_aliases/gemini-hospital.txt
.agents/sessions/current_session.txt
```

## Append Events

Record exact user and assistant turns:

```powershell
uv run session-snapshot append --name gemini-hospital --role user --content "set working directory as patients record"
uv run session-snapshot append --name gemini-hospital --role assistant --content "Workspace selected."
```

Record commands:

```powershell
uv run session-snapshot command --name gemini-hospital --command "uv run list-workspace-files --workspace workspaces/Hospital_Patient_Records" --status ok --summary "Workspace listed." --exit-code 0
```

Record file changes and decisions:

```powershell
uv run session-snapshot file-change --name gemini-hospital --path workspaces/Hospital_Patient_Records/interns/reports/current.md --action create --summary "Generated report."
uv run session-snapshot decision --name gemini-hospital --decision "User approved cleanup dry run." --status accepted
```

Finish the session:

```powershell
uv run session-snapshot finish --name gemini-hospital
```

Verify the session against inferred user intent:

```powershell
uv run session-snapshot verify --name gemini-hospital
```

After user or reviewer approval, verification can be promoted from `model_pass` to
`accepted_pass`:

```powershell
uv run session-snapshot verify --name gemini-hospital --accepted
```

## Session Files

Each session folder contains:

| File | Purpose |
|---|---|
| `compact.md` | Token-light context for agents. Read this first. |
| `intent_verification.md` | Human-readable pass/partial/fail review against user intent. |
| `intent_verification.json` | Machine-readable verification result and task records. |
| `transcript.md` | Exact human-readable conversation turns. |
| `events.jsonl` | Exact append-only event log for tools. |
| `commands.md` | Human-readable command audit. |
| `file_changes.md` | Human-readable file-change audit. |
| `decisions.md` | Human-readable accepted/rejected decisions. |
| `snapshot.json` | Machine-readable index and status. |

Use `compact.md` for monitoring long sessions. Open `transcript.md` or `events.jsonl` only when
an exact audit is required.

## Intent Verification

The lean verifier uses the recorded session evidence first:

```text
transcript turns -> commands -> file changes -> decisions -> compact context
```

It writes a session-level intent contract plus task-level verification records. Status values are:

| Status | Meaning |
|---|---|
| `model_pass` | Recorded evidence supports that the model completed the task. |
| `accepted_pass` | User or reviewer accepted a model pass. |
| `partial` | Some evidence exists, but intent satisfaction is incomplete or ambiguous. |
| `failed` | Deterministic evidence shows the task failed. |
| `blocked` | A required safety/business decision blocks verification. |
| `not_checked` | There is not enough evidence to check the task. |

The verifier is intentionally conservative. A model claim without evidence should become
`partial` or `not_checked`, not `model_pass`.

The verifier also runs deterministic guardrail checks:

| Check | Failure effect |
|---|---|
| No local absolute paths in non-user events | hard failure |
| No secret-like content requiring redaction | hard failure |
| No unresolved failed command events | failure |
| Evidence required before pass | partial if evidence is missing |
| Delete/cleanup requires an accepted decision and execution evidence | hard failure |

Hard guardrail failures override task success. For example, a task can have file-change evidence
but still fail verification if the assistant recorded a local absolute path or delete happened
without an accepted decision.

## Multiple Sessions

Use a different `--name` per workflow or tool:

```powershell
uv run session-snapshot start --name gemini-hospital --workspace workspaces/Hospital_Patient_Records --tool gemini
uv run session-snapshot start --name codex-guardrails --workspace workspaces/Hospital_Patient_Records --tool codex
uv run session-snapshot start --name teams-kpi-review --workspace workspaces/Hospital_Patient_Records --tool teams
```

Each name points to its latest timestamped session. Starting the same name again creates a new
timestamped folder and updates the alias.

## Default Current Session

If you do not pass `--name` or `--session-dir`, the tool writes to:

```text
.agents/sessions/current/
```

This is useful for quick local logging, but named sessions are better for enterprise monitoring.

## Security

The tool redacts common secret patterns before writing, including `api_key=...`, `token=...`,
`password=...`, `secret=...`, bearer tokens, and connection strings. This is a guardrail, not a
license to paste credentials. Do not record `.env` files, cloud tokens, private keys, connection
strings, cookies, or signed URLs.
