---
name: handoff
description: >
  Compact the current conversation into a handoff document for another
  agent to pick up. Save to the temporary directory of the user's OS —
  not the current workspace. Reference existing artifacts (PRDs, plans,
  ADRs, issues, commits, diffs) by path; do not re-paste them. Redact
  secrets and PII. Include a "suggested skills" section.
argument-hint: "What will the next session be used for?"
---

# Handoff

Write a handoff document so a fresh agent can continue the work without
loading the entire prior transcript. The document is the bridge between
two sessions; it captures only what cannot be re-derived from the repo.

## When to invoke

- User says `/handoff` (with or without an argument).
- User says "wrap this up for the next session" / "compact this for
  another agent" / "save context for tomorrow".
- Near a session-context limit, before compaction would erase load-bearing
  decisions.

Do NOT invoke for ordinary checkpoints (use `session-snapshot` for that).
A handoff is for cross-session continuity, not in-session memory.

## Where to save

Always to the OS temporary directory, NEVER the working directory:

- Windows: `$env:TEMP\autoresearch-handoff-<UTC-timestamp>.md`
- macOS/Linux: `${TMPDIR:-/tmp}/autoresearch-handoff-<UTC-timestamp>.md`

The temporary directory is correct because:

- The repo's working tree shouldn't accumulate scratch files.
- Handoffs may contain context the user does not want to commit.
- The next agent loads it once and discards it.

After writing, print the absolute path so the user can hand it to the
next session: `cat <path>` then paste, or pass via `--context-file`.

## What to include

Structure the handoff document with these sections in order:

### 1. Header

```markdown
# Handoff: <one-line summary of what's in flight>

- Generated: <UTC ISO timestamp>
- Originating session: <claude/gemini/codex> @ <hostname>
- Next session focus: <user's argument verbatim, or "general continuation">
```

### 2. State snapshot — what is true RIGHT NOW

Bullet list of facts the next agent must know that *cannot be derived
from the repo state* in under 30 seconds:

- Active workspace (`workspaces/<project>`), dataset_allowlist scope.
- Stage of the workflow (e.g., "blockers cleared for kpi_1, kpi_2;
  kpi_3 awaiting source-of-truth confirmation").
- In-flight CLI commands or background jobs (PID + command + expected
  completion). Specifically check for stuck workspace locks under
  `workspaces/<ws>/interns/state/workspace.lock`.
- Any uncommitted git changes (don't list them — reference
  `git status` and `git diff` instead).

### 3. Anchors — paths the next agent should read first

A short bulleted list of the smallest set of files that gives the next
agent context. PREFER:

- The active panel file: `workspaces/<ws>/interns/reports/blocker_question_panel/current.md`
- The latest plan or PRD if one exists.
- The PR description / issue body if work is tied to a GitHub item.
- The most recent commit: reference `git log -1` (do not paste).
- Memory entries that are load-bearing for the current task (reference
  by slug, e.g., `[[derive-not-curate]]`).

DO NOT re-paste content from these files. Reference them.

### 4. Open decisions / unresolved questions

Numbered list. Each item: the question, current best-guess answer, the
piece of evidence still missing. Format:

```markdown
1. **<question>** — current lean: <best guess>. Blocked on: <missing evidence>.
```

### 5. What was just attempted (the last 30 minutes)

Brief — 3 to 6 bullets, each one a single sentence. Include any
failure modes worth knowing (e.g., "DuckDB preview timed out on `age`
derivation for hospital-b CSV; switched to hospital-a allowlist").

### 6. Suggested skills for the next session

A bulleted list of skills the next agent should plan to invoke. For
each, one line of WHY.

```markdown
- `/grill-requirements` — the open decision about `denied_claim_status`
  needs the user's preferred denial taxonomy before we propose mapping.
- `/grill-requirements` — before recommending the final SQL dialect choice.
- `kpi-analyst` (auto-loaded) — fires when `apply-kpi-panel-answer` runs.
```

### 7. Do-not-do list

Things the previous agent learned NOT to do, so the next agent doesn't
repeat them. One-line bullets. Examples:

- "Don't paste DOB sample values into the response — PHI redaction is
  display-only; the panel file already handles it."
- "Don't re-run `onboard-workspace` — the workspace is fully onboarded;
  only re-run if `--force-onboard` is justified."

## Redaction rules

Before writing, scrub the document of:

- API keys, tokens, bearer headers, connection strings.
- `.env` content. `.databrickscfg` content. Cookies.
- PHI/PII: SSN, FirstName/LastName, Phone, Email, full Address, DOB,
  patient identifiers in raw form. Replace with `<redacted-pii>` or
  truncate to last-4 for IDs.
- File paths that include user home directories — keep `~/` or
  `$env:USERPROFILE` so the next agent can resolve.

When in doubt, redact. The handoff is a portable document; it might
end up pasted into another tool's chat history.

## Final output

After writing the file, output to the user:

```
Handoff written to <absolute path> (<line count> lines, <size> bytes).
Open it with: Get-Content -LiteralPath '<path>' -Raw  (PowerShell)
         or:  cat '<path>'                              (Bash)

Next session can start with:
  <copy-pasteable command that loads the handoff into the next CLI>
```

Do NOT print the handoff content in the chat. The whole point is that
it lives in a file the next session reads.

## Anti-patterns to refuse

- **Re-pasting the PRD / plan**: reference them by path. The next agent
  reads files, not chat transcripts.
- **Dumping the full diff**: reference `git status` / `git diff`. If the
  diff is uncommitted, suggest the user commit a WIP branch.
- **Writing to the working directory**: confuses git, pollutes the
  repo. Always use OS temp.
- **Inventing a focus**: if the user gave no argument and the session
  has no clear next step, ask one short question before writing.
