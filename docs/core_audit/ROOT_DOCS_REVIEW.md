# Root Docs Review
Audit date: 2026-06-14
Scope: README.md, AGENTS.md, CONTEXT.md, DEVELOPMENT.md, CODEX.md, GEMINI.md
Verdict key: [ok] accurate | [~] minor drift | [x] materially wrong/misleading

> **Resolved (2026-06-14, branch `follow-up/root-fixes`):** the high-priority README and
> AGENTS items below are now FIXED. README: the 3 dead harness commands now read
> `harness ai-app` / `harness ai-cli` / `harness workflow-guardrails`, and the dangling
> `workspace_scenarios.md` pointer was removed. AGENTS: the `clarify-ambiguity` skill
> references now point to `grill-requirements` (its merged-in mode). The remaining
> lower-priority CONTEXT/CODEX/GEMINI drift items are left as-is for a future pass.

---

## README.md [x]

### Findings

- **[x] Three CLI commands referenced that no longer exist as `[project.scripts]` entries.**

  README references these as standalone `uv run ...` commands:
  - `uv run run-ai-app-harness ...` (README line 161, line 217)
  - `uv run run-ai-cli-harness ...` (README line 216)
  - `uv run validate-workflow-guardrails ...` (README line 170)

  None of these appear in `pyproject.toml [project.scripts]`. Per `TOOLS.md` lines 424/465/501,
  these were renamed to subcommands of the unified `harness` entry point:
  - `uv run harness ai-app`
  - `uv run harness ai-cli`
  - `uv run harness workflow-guardrails`

  A new contributor who copies these commands verbatim will get `No such command` errors.

  Fix: update each occurrence to the `harness <subcommand>` form.

- **[x] `workspace_scenarios.md` referenced but does not exist at repo root.**

  README line 449: `"For agent-led conversations on the checked-in RCM workspace and fresh-start
  scenarios, see workspace_scenarios.md."` The file is absent from the repo root (only found
  inside `.claude/worktrees/` worktree copies, which are ephemeral). A reader who tries to open it
  will find nothing.

  Fix: remove the reference or replace it with the actual file (e.g., `docs/` if moved there).

- **[~] `core/onboarding/` layout in README omits `sources/external_discovery.py` module path.**

  README lines 47-57 list most subpackages, but omits `core/onboarding/pipeline_plan.py`,
  `core/onboarding/pipeline_deployment_plan.py`, `core/onboarding/pipeline_sql_generator.py`,
  `core/onboarding/catalog_contract.py`, and `core/onboarding/source_family_contracts.py` which
  all back significant CLI scripts. Minor omission; the list is illustrative, not exhaustive.

  Fix: add a `core/onboarding/` note: "pipeline_plan, catalog_contract, and
  source_family_contracts" to the layout table, or add a caveat that the list is non-exhaustive.

- **[~] README describes `uv run python dashboard.py` but project also has a
  `workspace-dashboard` CLI script.**

  README line 211 uses `uv run python dashboard.py`. The `workspace-dashboard` entry point in
  `pyproject.toml` (line 81: `tools.workspace_dashboard:main`) is the governed wrapper with
  `--export` / `--screen` options. The README does not mention it. Minor; the raw `python
  dashboard.py` still works, but new users may not discover the governed path.

---

## AGENTS.md [~]

### Findings

- **[~] Skill routing table references `clarify-ambiguity` skill that has no `SKILL.md` on disk.**

  AGENTS.md line 642: `"clarify-ambiguity: ask one targeted question only when ambiguity
  materially matters."` No `skills/clarify-ambiguity/SKILL.md` exists in the repo (confirmed by
  glob). All other skills listed in the same bullet block (`grill-requirements`,
  `stakeholder-memory`, `domain-model`, `data-engineering-pipeline-design`,
  `feature-derivation-library`, `to-solution-brief`, `task-onboarding`,
  `workspace-kpi-query-optimizer`, `workspace-governance`, `databricks-access-gates`,
  `evolution`) have corresponding `skills/*/SKILL.md` files.

  Fix: remove `clarify-ambiguity` from the routing table or create the missing skill file.

- **[~] `program.md` referenced but does not exist.**

  AGENTS.md line 22: `"5. program.md only when the active benchmark/task refers to it."` The file
  is not present at repo root. This may be intentional (it is workspace-specific per task config),
  but the bare reference without context will confuse a new contributor.

  Fix: add a parenthetical: `(workspace-specific; generated per task, not checked in)`.

- **[ok] Repo map, CLI envelope, stage index, and all major rule sections are accurate and
  consistent with the codebase.** `core/contracts/versioning.py`, `core/onboarding/workspace/
  cli_runner.py`, `core/onboarding/workspace/idempotency.py`, all enumerated `core/` subpackages,
  and `config/lock.toml` all exist as described.

---

## CONTEXT.md [~]

### Findings

- **[~] Package layout in CONTEXT.md omits several real `core/` subpackages.**

  CONTEXT.md "Core Package Layout" section (lines 34-40) lists:
  `orchestration`, `execution`, `governance`, `optimization`, `profiling`, `agents`,
  `observability`, `storage`. It omits `core/context/`, `core/medallion/`, `core/presentation/`,
  `core/resource/`, `core/onboarding/`, `core/contracts/`, `core/dev/`, and `core/skills/`.
  README.md (lines 58-68) lists the fuller set including these. The CONTEXT.md omissions are
  significant because `core/onboarding/` is the primary pipeline and `core/medallion/` is a
  major feature area.

  Fix: either extend the list to match README.md or add `# abbreviated` note explaining that
  the CONTEXT.md list covers the original design-time packages only.

- **[ok] All domain concepts (LLMEngine, Workspace, MetricParser, ExecutionBackend,
  TelemetryBackend, DatabricksClient, SemanticContract, etc.) match actual class names
  and module locations confirmed via grep.** Data flow diagram is consistent with current code
  structure.

---

## DEVELOPMENT.md [ok]

### Findings

- **[ok] All seven `develop_spec/` files referenced in the route table exist on disk:**
  `guidelines.md`, `harness.md`, `testing.md`, `tracing.md`, `path_to_production.md`,
  `changelog.md`, `follow_ups.md` — confirmed.
- **[ok] Non-negotiables (no domain words, `.venv` interpreter for tests, no hand-edits,
  no emojis, changelog logging) are consistent with the rest of the docs.**
- **[ok] Cross-references to AGENTS.md, CONTEXT.md, README.md, TOOLS.md, and CLAUDE.md are all
  accurate file names that exist.**

---

## CODEX.md [~]

### Findings

- **[~] Skills index path is potentially stale.**

  CODEX.md line 97: `"Repo skills are indexed in .agents/codex/SKILLS.md, but the canonical
  skill bodies live in skills/*/SKILL.md."` The file `.agents/codex/SKILLS.md` was found by grep
  only inside worktrees and referenced by the skills indexer, but `generate-skill-adapters`
  writes `.agents/codex/SKILLS.md` as a generated output. If adapters have not been regenerated
  recently, this file may be stale relative to the canonical `skills/` tree. Minor; it is a
  generated artifact, not a checked-in primary.

- **[ok] All CLI commands (`prepare-kpi-blocker-panel`, `apply-kpi-panel-answer`,
  `onboard-workspace`, `validate-workspace-artifacts`, `list-workspace-files`) exist in
  `pyproject.toml`.** Workspace selection, blocker, and panel rules match AGENTS.md exactly.

- **[ok] No standalone deprecated command names (`run-ai-app-harness` etc.) appear in CODEX.md.**
  CODEX.md avoids the stale command names found in README.md.

---

## GEMINI.md [~]

### Findings

- **[~] Skills index path mirrors the same generated-artifact caveat as CODEX.md.**

  GEMINI.md line 122: `"Repo skills are indexed in .agents/gemini/SKILLS.md, but the canonical
  skill bodies live in skills/*/SKILL.md."` Same concern as CODEX.md: the `.agents/gemini/
  SKILLS.md` is `generate-skill-adapters` output; if not regenerated, it may lag the canonical
  `skills/` directory.

- **[ok] `docs/agents/gemini-cli-reference.md` referenced in GEMINI.md line 8 exists on disk.**

- **[ok] No deprecated command names appear in GEMINI.md.** Human-friendly command mapping
  (lines 129-142) uses governed CLI wrappers correctly.

- **[ok] All CLI wrappers, blocker panel rules, JSON display rules, and agent delegation rules
  are consistent with AGENTS.md.**

---

## Recommended Fixes, Prioritized

### High

1. **README.md — three stale CLI command names** (lines 161, 170, 216-217).
   `run-ai-app-harness`, `run-ai-cli-harness`, and `validate-workflow-guardrails` no longer exist
   as scripts. Replace with `uv run harness ai-app`, `uv run harness ai-cli`, and
   `uv run harness workflow-guardrails`. Any new contributor following the README verbatim will
   hit `No such command` errors.

2. **README.md — `workspace_scenarios.md` reference to a non-existent file** (line 449).
   Remove or correct. Currently points to a file that only exists in ephemeral worktrees.

### Medium

3. **AGENTS.md — `clarify-ambiguity` skill listed in routing table but no SKILL.md exists.**
   Either create `skills/clarify-ambiguity/SKILL.md` or remove from the routing table. An agent
   that tries to follow the routing table will have no skill body to load.

4. **CONTEXT.md — package layout omits major `core/` packages** including `core/onboarding/`,
   `core/medallion/`, `core/context/`, and `core/resource/`. A developer reading CONTEXT.md as
   the architecture reference will have an incomplete picture of what is in `core/`.

### Low

5. **README.md — `workspace-dashboard` CLI not mentioned** (line 211). The README documents
   `uv run python dashboard.py` but not the governed `workspace-dashboard` entry point with
   its `--export` and `--screen` options.

6. **AGENTS.md — `program.md` reference** (line 22). The file does not exist at repo root.
   Clarify it is workspace-specific and generated per task config.

7. **CODEX.md / GEMINI.md — generated adapter files may lag canonical `skills/`** if
   `generate-skill-adapters` has not been run recently. Not a doc error per se, but a
   maintenance gap worth noting for teams that add new skills.
