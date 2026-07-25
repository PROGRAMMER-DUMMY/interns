# Docs Index

**Scope: this is the platform's INTERNAL documentation** — our own reference material, plans,
preferences, and bug logs for building and maintaining the autoresearch/KPI platform itself. It is
NOT per-customer output. Anything generated for a specific workspace (profiles, KPI results,
contracts, the data-understanding artifact, etc.) lives under
`workspaces/<project>/interns/`, never here. Keep the two separate: `docs/` = how WE build the
product; `interns/` = what the product produces for a customer.

One place to learn what every doc is for. Read this before opening individual files, and
before adding a new one. Each entry is one line: `path — what it is — [status]`.

Status tags:
- `[reference]` — active, consumed by agents/code or used as ground truth during work.
- `[plan]` — implementation plan / roadmap. Aspirational, not active reference. May be parked.
- `[log]` — append-only record (bugs, sessions). Historical, not a spec.
- `[local]` — personal/ephemeral, gitignored or session-scoped. Not shared truth.

When statuses disagree with reality, fix the doc or its tag here — this index is the
source of truth for "what kind of doc is this".

---

## Reference — active, ground-truth material

- `reference/` — active, ground-truth reference set (medallion pipeline, Databricks production
  practices, token scopes, self-hosted lakehouse ops, dbt agentic CLI). See its own `index.md`. [reference]
- `repo_hygiene.md` — staging/commit hygiene (explicit paths, no `git add -A`). [reference]
- `session_snapshot.md` — session-scoped snapshot tooling doc. [reference]

## Agent knowledge — `agents/`

- `agents/DataEngineerTP.md` — principal-engineer data-engineering knowledge base (~64 KB). [reference]
- `agents/gemini-cli-reference.md` — Gemini CLI usage notes for this repo. [reference]
- `agents/data processing/` — the canonical data-engineering + schema knowledge set; grounds the
  data-understanding gate (quality tier + schema type). See its own `index.md`. [reference]
  - The gate runs inside `workspace-flow` and standalone via `uv run understand-data --workspace
    workspaces/<project>` (see `TOOLS.md`), writing `interns/reports/data_understanding/current.{json,md}`.

## Plans — implementation roadmaps (not active reference)

- `plans/` — implementation roadmaps (enterprise productionization, reliability platform, SQL
  coverage + reliability). Aspirational, may be parked. See its own `index.md`. [plan]

## Enterprise — custom requirements / specs

- `enterprise/` — enterprise-specific platform requirements (compliance, tenancy, auth/RBAC, custom
  specs). Platform-internal, not customer workspace output. See its own `index.md`. [plan]

## Logs — append-only records

- `bugs/` — bug logs. See its own `index.md` for the catalog; current session: `bugs/BUG_SESSION_REPORT.md`. [log]
- `core_audit/` — full-read `core/` audit (227 files, 44 docs): per-package findings, phased
  remediation ledger, and current launch status. Start at `core_audit/README.md`, which points to
  `GO_NO_GO_2026-07.md` (current status) then `SUMMARY.md`/`REMEDIATION_PLAN.md`. Closed findings,
  not live guidance -- several are already fixed per the remediation commits in git log. [log]

## Local / ephemeral

- `project_sections.local.md` — personal working notes (`.local.`, gitignored). [local]

---

## How to add a new doc or folder

1. Pick the status: `reference`, `plan`, `log`, or `local` (see tags above).
2. Place it accordingly:
   - reference -> `docs/reference/` (or a topic subfolder if a tighter cluster forms — e.g. `agents/`).
   - plan -> `docs/plans/`; tag `[plan]` so it is not mistaken for active reference.
   - log -> `docs/bugs/` (or a new `docs/<topic>-log/` if a new log type emerges).
   - local -> name it `*.local.md` so gitignore catches it.
3. Add exactly one line to the matching section above: `path — what it is — [status]`.
4. If you add a whole **subfolder**, give it its own `index.md` (like `agents/data processing/`)
   and add one line here pointing at that folder + its index. The folder's `index.md` lists its
   files; this top index only points to the folder. That keeps this file from growing unbounded.

Rule of thumb: a reader should be able to answer "is this doc active truth or a parked idea, and
where do related docs live?" from this file alone, without opening anything else.
