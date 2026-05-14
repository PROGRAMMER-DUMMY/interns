# Agent CLI Launch Guide

Use these launch patterns to make Gemini, Claude, and Codex follow the governed workspace workflow
without asking data teams to type long internal commands.

## Gemini

Preferred interactive launch for workspace workflows:

```powershell
gemini --prompt-interactive "$(Get-Content -LiteralPath .gemini/workspace-workflow-prompt.md -Raw)"
```

Useful checks:

```powershell
gemini -p "Set rcm data as workspace, then stop after the workspace confirmation question." --output-format json
```

Avoid:

```powershell
gemini --yolo
gemini --approval-mode yolo
```

## Claude

Preferred interactive launch:

```powershell
claude --append-system-prompt "Read CLAUDE.md, AGENTS.md, TOOLS.md, and .agents/tools.json. Use prepare-kpi-blocker-panel and apply-kpi-panel-answer for KPI blocker workflows. Do not invent unsupported flags."
```

Use `--agents` only when the user explicitly asks for subagents or parallel agent work.

Avoid:

```powershell
claude --dangerously-skip-permissions
claude --allow-dangerously-skip-permissions
```

## Codex

Preferred launch from outside the repo:

```powershell
codex -C C:\Users\shubh\OneDrive\Desktop\interns
```

Safe default sandbox:

```powershell
codex -C C:\Users\shubh\OneDrive\Desktop\interns --sandbox workspace-write
```

Avoid:

```powershell
codex --dangerously-bypass-approvals-and-sandbox
```

## User-Level Intents

Agents should map these short intents to repo tools:

- `set rcm data`: list workspace files and ask for confirmation.
- `prepare blockers`: run `prepare-kpi-blocker-panel`.
- `show blocker json`: show the current blocker panel JSON artifact.
- `show blocker markdown`: show the current blocker panel Markdown artifact.
- `accept option A`: run `apply-kpi-panel-answer --answer option_a`.
- `start fresh`: run cleanup dry-run first, then require explicit delete confirmation.

Teams should not need to type full internal commands unless they ask for exact CLI syntax.
