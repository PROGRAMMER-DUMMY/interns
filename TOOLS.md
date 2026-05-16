# Project Tools

This repo has first-class project tools. Agents should inspect this file and
`.agents/tools.json` before writing ad hoc scripts or reading large inputs.

## Evidence Order

For dataset questions, use profile artifacts before raw data:

1. `workspaces/<project>/interns/generated/profiles/profile_index.json`
2. `workspaces/<project>/interns/generated/profiles/*.profile.json`
3. bounded samples only when profile evidence is insufficient
4. full raw dataset reads only with a concrete reason

Do not paste raw datasets into prompts. Prefer Polars for bounded data inspection.

## Secret Display Guardrail

Never print `.env`, `.databrickscfg`, private keys, tokens, shell environment dumps, connection
strings, bearer headers, cookies, or config/AST/tree output that includes secret values. Tool output
should report only existence/status or redacted key names, for example
`DATABRICKS_TOKEN=<redacted>`.

## Tools

### list-workspace-files

Command:

```powershell
uv run list-workspace-files --workspace workspaces/<project>
```

Use first for `set workspace` and active-workspace selection requests. It lists all workspace file
paths up to the cap and adds basic hint groups: possible KPI files, possible data model files,
dataset roots, docs, and `interns` state. The possible KPI/model groups are not ground truth. The
full `All files` section is the user confirmation boundary. It does not read file contents, parse
Excel, profile datasets, onboard, delete, or write files.

### onboard-workspace

Command:

```powershell
uv run onboard-workspace --workspace workspaces/<project>
```

Use when a workspace needs generated `interns/` artifacts: profiles, contracts,
reports, baseline runner/evaluator, and generated solution scaffolding.

### kickstart-workspace

Command:

```powershell
uv run kickstart-workspace --workspace workspaces/<project> --domain <domain>
```

Use when setting up or refreshing a governed enterprise workspace from raw
project inputs. It updates task config, discovery artifacts, feature mapping,
and open questions.

### prepare-kpi-generation

Command:

```powershell
uv run prepare-kpi-generation --workspace workspaces/<project>
```

Use after workspace confirmation when the user should choose between KPI generation and the usual
onboarding/mapping workflow. The tool always writes a two-choice route panel with a smart
recommendation based on detected KPI files, data models, datasets, optional context, and a combined
KPI quality/readiness score.

Outputs:

```text
workspaces/<project>/interns/generated/requirements/kpi_generation_session.json
workspaces/<project>/interns/reports/kpi_generation/current.json
workspaces/<project>/interns/reports/kpi_generation/current.md
```

Optional stakeholder context can be provided with repeated `--context-file` values. Context files
must stay inside the workspace.

### apply-kpi-generation-answer

Command:

```powershell
uv run apply-kpi-generation-answer --workspace workspaces/<project> --answer option_a
```

Use after the user answers the current KPI generation panel. It records the accepted option,
advances the deterministic interview, saves decisions, and eventually writes a draft KPI registry
preview with competitive advisor notes and per-KPI evidence-proof requirements under
`interns/generated/requirements/kpi_registry_draft.json`.

### finalize-kpi-generation

Command:

```powershell
uv run finalize-kpi-generation --workspace workspaces/<project> --approve-final-preview
```

Use only after the final KPI draft preview has been shown to the user and explicitly approved. It
writes the user-facing KPI registry, production-readiness proof, workspace memory, and team-level
preference memory. Without `--approve-final-preview`, it must fail. Existing registry outputs require
`--replace-existing`.

### prepare-data-model-generation

Command:

```powershell
uv run prepare-data-model-generation --workspace workspaces/<project>
```

Use after onboarding/profiling when data model docs are missing, weak, image-only, or need to be
converted into governed relationship proof. It writes a route panel under
`interns/reports/data_model_generation/` and does not finalize user-facing docs.

### apply-data-model-answer

Command:

```powershell
uv run apply-data-model-answer --workspace workspaces/<project> --answer option_b
```

Use after the user answers the current data-model panel. It writes a draft core model pack under
`interns/generated/requirements/` and readable draft reports under
`interns/reports/data_model_generation/`.

### finalize-data-model-generation

Command:

```powershell
uv run finalize-data-model-generation --workspace workspaces/<project> --approve-final-preview
```

Use only after the draft data model preview is reviewed and explicitly approved. It writes
user-facing `docs/data-model.md`, `docs/erd.md`, `docs/relationships.md`, and finalized
`interns/generated/contracts/data_model_contract.json`. Approved relationships can then be promoted
by `build-relationship-contracts` for executable SQL planning.

### resolve-kpi-features

Command:

```powershell
uv run resolve-kpi-features --workspace workspaces/<project> --domain <domain> --include-candidates
```

Use when KPI/query features must be mapped to schema/profile evidence,
workspace definitions, derivation candidates, or user-confirmed blockers.
Derived-feature options must be emitted as strict JSON evidence objects with
formula, input columns, observed/profiled column values, worked example, source
files, semantic meaning sources, per-column reasons, derivation reasoning,
evidence state, confidence, and confirmation status. Prose-only derived-column
options are invalid. Semantically mismatched candidates must be rejected instead
of offered as selectable options.
The command also writes the standardized blocker question panel and includes
`question_panel_path` and `question_panel_markdown_path` in its JSON output. If
`blocked_kpi_count` is nonzero, read the markdown panel before asking the user.

Apply one KPI-specific decision:

```powershell
uv run resolve-kpi-features --workspace workspaces/<project> --apply-decision --kpi-id kpi_001 --feature FeatureName --evidence-note "..."
```

Apply one reusable workspace definition:

```powershell
uv run resolve-kpi-features --workspace workspaces/<project> --apply-workspace-definition --feature FeatureName --definition "..." --evidence-note "..."
```

### prepare-kpi-blocker-panel

Command:

```powershell
uv run prepare-kpi-blocker-panel --workspace workspaces/<project> --domain <domain>
```

Preferred wrapper for KPI blocker preparation. It runs missing onboarding when needed, resolves KPI
features with candidates, renders derived-feature markdown, regenerates the blocker question panel,
and runs `validate-workspace-artifacts`. Agents should use this wrapper instead of hand-chaining the
lower-level commands when the next action is to ask a KPI blocker question. If validation fails, do
not ask the user; fix the parser/resolver or malformed artifact first.

### apply-kpi-panel-answer

Command:

```powershell
uv run apply-kpi-panel-answer --workspace workspaces/<project> --domain <domain> --answer option_a
```

Use after the user answers a blocker question from
`interns/reports/blocker_question_panel/current.json` or `current.md`. It resolves friendly answers
such as `option_a`, `Option A: PaidAmount`, an exact label, or an unambiguous recommended answer
against `current.json`, applies the selected physical-column or derived-formula definition through
supported resolver APIs, then prepares and validates the next panel. Do not invent unsupported flags
such as `--accept-option`.

### derived-feature-markdown

Command:

```powershell
uv run derived-feature-markdown --workspace workspaces/<project>
```

Use after `resolve-kpi-features --include-candidates` when business analysts,
product leads, or stakeholders need readable Markdown review files for strict
derived-feature JSON options. The converter validates required fields by
default and writes separated `.md` and `.json` files under:

```text
workspaces/<project>/interns/reports/derived_feature_reviews/md/
workspaces/<project>/interns/reports/derived_feature_reviews/json/
```

Multiple options for the same KPI feature are kept together in one Markdown file
and one JSON file.

### blocker-question-panel

Command:

```powershell
uv run blocker-question-panel --workspace workspaces/<project>
```

Use after `resolve-kpi-features --include-candidates` whenever an agent needs to
ask a stakeholder a KPI blocker question. This is mandatory for direct mappings,
source-of-truth choices, aliases, reusable workspace definitions, and
derived-feature questions. The tool writes a stable question panel with the
blocker, reuse scope, recommended answer, reason, answer shape, and JSON-backed
derived-feature option when one is valid. If no valid derived option exists, the
panel asks for a direct mapping, source-origin rule, data dictionary evidence, or
workspace business definition instead of inventing formula choices.

Outputs:

```text
workspaces/<project>/interns/reports/blocker_question_panel/current.json
workspaces/<project>/interns/reports/blocker_question_panel/current.md
workspaces/<project>/interns/reports/blocker_question_panel/index.json
```

### validate-workspace-artifacts

Command:

```powershell
uv run validate-workspace-artifacts --workspace workspaces/<project>
```

Use after `onboard-workspace`, `resolve-kpi-features`, `derived-feature-markdown`, or
`blocker-question-panel` before an agent relies on generated contracts. It validates generated JSON
shape, KPI registry provenance, feature-mapping summary fields, strict derived-feature evidence,
profile-backed physical-column option evidence, and whether blocked KPIs have a current question
panel. It is read-only and exits nonzero on schema/format errors.

### validate-git-hygiene

Command:

```powershell
uv run validate-git-hygiene
```

Use before commits. By default it checks staged files and blocks raw data extensions, oversized
files, generated workspace output under `workspaces/**/interns/`, runtime state, logs, and local
databases. Use `--all` for a broader tracked/untracked audit and `--max-mb` to override the default
25 MB file-size threshold.

### generate-kpi-sql

Command:

```powershell
uv run generate-kpi-sql --workspace workspaces/<project> --kpi-id kpi_001
```

Use only after required KPI features are proven or user-confirmed and the selected source datasets,
joins, grain, filters, and date anchors match the data model/profile evidence. If the user asks for
Polars, PySpark, ETL/ELT, or medallion-layer loading, first produce or inspect a data-model-backed
source-to-target plan; do not translate KPI text directly into executable code.

### plan-source-to-target

Command:

```powershell
uv run plan-source-to-target --workspace workspaces/<project> --target-engine sql
```

Use before generating SQL, Polars, PySpark, or medallion/ETL logic. It reads the KPI feature mapping,
domain model, and profiles, then writes:

```text
workspaces/<project>/interns/generated/contracts/source_to_target_plan.json
workspaces/<project>/interns/reports/source_to_target_plan.md
```

The plan records selected and rejected datasets, feature-to-column mappings, join candidates, grain,
temporal anchors, medallion layers, validation checks, and blockers. Treat blockers as hard stops
before executable code generation.

### build-relationship-contracts

Command:

```powershell
uv run build-relationship-contracts --workspace workspaces/<project>
```

Use before multi-dataset SQL, Polars, PySpark, ETL/ELT, medallion generation, or production KPI
proof. It writes a production-grade FK/relationship contract with data-model evidence, profile
evidence, confidence, approval state, cardinality/null/uniqueness/referential-integrity checks,
grain impact, source-system scope, lifecycle review dates, promotion policy, and executable usage
policy.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/relationship_contracts.json
workspaces/<project>/interns/reports/relationship_contracts.md
```

Only relationships with executable-approved states such as `proven_data_model` or `user_confirmed`
may be used by trusted executable generation. Profile-only relationships remain advisory
`profile_validated` candidates and should trigger blocker grilling before SQL/code generation.

### cleanup-workspace-references

Dry run:

```powershell
uv run cleanup-workspace-references --workspace workspaces/<project> --all-references
```

Apply:

```powershell
uv run cleanup-workspace-references --workspace workspaces/<project> --all-references --apply --confirm-delete workspaces/<project>
```

Use when a workspace needs a fresh start and stale generated references must be
removed from `workspaces/<project>/interns`, repo runtime state, Databricks
deployment state, and task config. It must not remove workspace `docs/` or
`datasets/`. Any deletion path requires `--confirm-delete` with the exact
workspace path after reviewing the dry run.

### loop

Command:

```powershell
uv run loop --task <task-id>
```

Use to run the governed optimization loop. Remote execution is approval-gated.

### profiler.py

Command:

```powershell
uv run python tools/profiler.py --input <path> --pct 5 --engine auto --out <dir>
```

Use for sampling, profiling, representation checks, null audits, distribution
checks, and model-transfer diagnostics. Prefer generated profile artifacts when
they already answer the question.

### optimizer_finder.py

Command:

```powershell
uv run python tools/optimizer_finder.py --target <file.sql|file.py> --mode auto
```

Use when SQL or Python is slow, timing out, or needs hotspot evidence.

### methodology_parser.py

Command:

```powershell
uv run python tools/methodology_parser.py --doc <file> --out <schema.json>
```

Use when a methodology document, data dictionary, or contract must be converted
into semantic schema JSON.

### Databricks Tools

Commands:

```powershell
uv run prepare-databricks-assets --workspace workspaces/<project>
uv run prepare-genie-workspace --workspace workspaces/<project>
uv run deploy-databricks-workspace --workspace workspaces/<project>
uv run python tools/databricks_setup.py
```

Use Databricks setup/deployment tools only when the user explicitly asks for
Databricks validation, planning, or approved remote mutation. Do not run remote
execution just because credentials exist.

### generate-skill-adapters

Command:

```powershell
uv run generate-skill-adapters
```

Use after changing `skills/*/SKILL.md` or cross-tool skill routing.
