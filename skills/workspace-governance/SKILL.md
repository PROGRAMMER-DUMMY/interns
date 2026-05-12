---
name: workspace-governance
description: >
  Enforce workspace safety: keep project outputs under workspaces/<project>/interns/, avoid pushing
  raw data or generated artifacts, prevent secret leakage, and check staged files before commit/push.
  Use before git add/commit/push and whenever workspace files are modified.
---

# Workspace Governance

Protect project data and keep outputs organized.

## Step 0: Active Workflow Setup

Before checking or staging workspace files, confirm which workspace is active. If unclear, scan
`workspaces/` and `config/tasks.json`, summarize the likely project, then ask the user to confirm.

## Rules

- Project input lives under `workspaces/<project>/`.
- Platform output lives under `workspaces/<project>/interns/`.
- `workspaces/**/interns/` must remain ignored.
- Never stage raw data, `.env`, databases, profile outputs, or nested `.git` repos.
- Do not stage `config/lock.toml` unless explicitly requested.
- Prefer sanitized examples over real workspace contents.

## Before Commit

Run:

```powershell
git status --short
git diff --cached --stat
git diff --cached --name-only
```

Check for:

- `workspaces/<project>/interns/`
- `.env`
- `.duckdb`, `.db`, `.sqlite`
- `.csv`, `.parquet`, `.pdf`
- nested workspace directories with `.git`
- token-like strings in staged diff

If any appear, stop and explain the risk.
