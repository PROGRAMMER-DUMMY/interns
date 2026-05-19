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

### prepare-workspace-workflow

Command:

```powershell
uv run prepare-workspace-workflow --workspace workspaces/<project> --mode local-safe --domain healthcare
```

Use after workspace confirmation when the user wants one governed checkpoint for the whole workflow.
It writes `interns/reports/workflow/current.json` and `current.md`, runs local-safe preparation
steps, and shows manual/local-safe/autopilot options. Modes:

- `plan`: inspect and write the checkpoint without generating missing artifacts.
- `local-safe`: run missing local-safe preparation, validation, and presentation export steps.
- `autopilot`: apply only bounded low-risk recommended answers, while still stopping before final
  approval, deletes, remote execution, relationship approval, docs promotion, and executable
  DDL/dbt/SQL generation.

### prepare-wiki-memory

Command:

```powershell
uv run prepare-wiki-memory --workspace workspaces/<project> --domain <domain>
```

Use when repeated KPI terms, data-model entities, grains, relationships, or workflow decisions
should be converted into governed reuse cards. V1 scans structured KPI/data-model/session artifacts
only and writes scoped memory under the workspace plus a repo-level team memory index.

Outputs:

```text
state/team_memory/wiki_memory_index.json
workspaces/<project>/interns/generated/memory/wiki_memory_candidates.json
workspaces/<project>/interns/reports/wiki_memory/current.json
workspaces/<project>/interns/reports/wiki_memory/current.md
```

Automation policy: exact approved matches may be used as draft prefill candidates, but executable
generation, final promotion, and relationship approval remain blocked until current-workspace
evidence or user approval exists.

### prepare-agent-benchmark

Command:

```powershell
uv run prepare-agent-benchmark --workspace workspaces/<project> --domain <domain>
```

Use when the workspace needs a project-native readiness proof and release gate before SQL, ETL,
medallion, autopilot, or production promotion. V1 scores existing governed artifacts rather than
running external TPC/Spider/BIRD suites.

Outputs:

```text
workspaces/<project>/interns/generated/contracts/agent_benchmark_scorecard.json
workspaces/<project>/interns/generated/contracts/release_gate_status.json
workspaces/<project>/interns/reports/benchmarks/current.json
workspaces/<project>/interns/reports/benchmarks/current.md
```

The scorecard separates core readiness from product maturity. Core readiness weights business
correctness first: KPI definitions, grain, filters, data-model readiness, relationship proof,
source-to-target readiness, and validation. Product maturity tracks presentation exports, wiki reuse,
workflow checkpoint status, and autopilot safety. Blockers route back to existing deterministic
tools such as KPI blocker panels, data-model blocker panels, relationship contracts, source-to-target
planning, validation, and wiki memory.

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

### prepare-data-model-blocker-panel

Command:

```powershell
uv run prepare-data-model-blocker-panel --workspace workspaces/<project>
```

Use after a data-model draft exists and the next unresolved model decision should be asked from a
JSON-backed panel. It ranks grain, primary-key, relationship, temporal-anchor, and SCD blockers and
writes `interns/reports/data_model_blocker_panel/current.json` and `current.md`.

### apply-data-model-blocker-answer

Command:

```powershell
uv run apply-data-model-blocker-answer --workspace workspaces/<project> --answer option_a
```

Use after the user answers the current data-model blocker panel. It resolves the option against
`current.json`, applies the structured operation to `data_model_draft.json`, writes the next blocker
panel, and keeps unresolved decisions blocked.

### export-data-model-diagram

Command:

```powershell
uv run export-data-model-diagram --workspace workspaces/<project>
```

Use when stakeholders need a presentable data-model diagram artifact. It reads finalized
`data_model_contract.json` when available, otherwise the draft data model or onboarded
`domain_model.json`, and writes native SVG plus Mermaid Markdown under
`interns/reports/presentation/`.

### export-kpi-registry-excel

Command:

```powershell
uv run export-kpi-registry-excel --workspace workspaces/<project>
```

Use when stakeholders need an Excel workbook for KPI review. It uses finalized KPI registry JSON
when present, otherwise the KPI generation draft, otherwise onboarded `kpi_registry.json`, and
writes a multi-sheet workbook under `interns/reports/presentation/kpi_registry.xlsx`.

### export-workspace-presentation

Command:

```powershell
uv run export-workspace-presentation --workspace workspaces/<project>
```

Use for a stakeholder-ready presentation bundle. It produces the data-model SVG/Mermaid export,
KPI Excel workbook, and `presentation_manifest.json` under `interns/reports/presentation/`.

### prepare-source-catalog

Command:

```powershell
uv run source-catalog plan --workspace workspaces/<project>
uv run prepare-source-catalog --workspace workspaces/<project>
```

Use when external sources should be selected before ingestion. Reusable source templates live under
`config/source_catalogs/`; workspace-approved selections live at
`workspaces/<project>/docs/source_selection.json`. The command writes a dry-run plan and report
under `workspaces/<project>/interns/` without fetching rows, copying files, or calling remote
catalog APIs.

Supported source types:

- `api`: HTTP/JSON dataset or document endpoint with bounded pagination.
- `local`: approved local/workspace file source, copied into `datasets/` or `docs/` or registered
  as an external allowlist entry.
- `databricks_uc`: Unity Catalog table metadata source. It plans by default; remote metadata export
  requires explicit remote approval.

API sources support conservative runtime controls through the selection or template:

```json
{
  "fetch_policy": {
    "qps": 1.0,
    "attempts": 4,
    "timeout_seconds": 30,
    "backoff_initial_seconds": 1,
    "backoff_max_seconds": 30,
    "max_bytes": 50000000
  },
  "auth": {
    "type": "header",
    "header_name": "Authorization",
    "header_prefix": "Bearer",
    "header_env": "VENDOR_API_TOKEN"
  }
}
```

Only the environment variable name is stored in artifacts; secret values are never written. Runtime
checkpoints are written under `interns/state/source_catalog/checkpoints/`, and failed pages are
quarantined under `interns/generated/evidence/source_catalog/quarantine/`.

Outputs:

```text
workspaces/<project>/interns/generated/requirements/source_catalog_plan.json
workspaces/<project>/interns/reports/source_catalog_plan.md
```

The canonical controllable CLI uses subcommands:

```powershell
uv run source-catalog plan --workspace workspaces/<project>
uv run source-catalog preflight --workspace workspaces/<project>
uv run source-catalog api-fetch --workspace workspaces/<project> --source <source-id>
uv run source-catalog local-stage --workspace workspaces/<project> --source <source-id>
uv run source-catalog uc-inspect --workspace workspaces/<project> --source <source-id>
uv run source-catalog discover-docs --workspace workspaces/<project>
uv run source-catalog index-catalog --workspace workspaces/<project> --source <catalog-source-id>
uv run source-catalog match-catalog --workspace workspaces/<project> --source <catalog-source-id> --keyword claims
uv run source-catalog draft-selection --workspace workspaces/<project> --source <catalog-source-id>
uv run source-catalog finalize-selection --workspace workspaces/<project> --source <catalog-source-id> --approve-final-preview
uv run source-catalog process --workspace workspaces/<project>
uv run source-catalog validate --workspace workspaces/<project> --strict
uv run source-catalog run --workspace workspaces/<project>
```

Use the subcommands when debugging or controlling a source type independently. The `prepare-*` and
`ingest-*` commands remain compatibility wrappers for the dry-run and all-source apply paths.
`preflight` checks target boundaries, resource budget, URLs, rate-limit policy, auth environment variable presence,
local file existence, and Databricks remote approval state. `api-fetch` uses a concurrent scheduler
for multiple API sources, shares QPS throttling per host, resumes row APIs from checkpoints when
possible, enforces expected columns when configured, streams declared file/document responses through
`.part` files, and quarantines failed pages. `process` classifies materialized
outputs, stages CSV/JSON datasets to Parquet evidence, writes profile JSON, and records a basic
drift report against the previous profile. `validate --strict` treats partial fetches and fetch
failures as errors for production-style runs.

For large catalog payloads, do not paste the full JSON into chat or prompts. Use
`index-catalog` to write compact JSONL entries under
`interns/generated/requirements/source_catalog/`, `match-catalog` to score the index against
workspace dataset/doc names and optional keywords, then `draft-selection` to create
`docs/source_selection.generated.json`. JSONL/NDJSON and streamable JSON arrays are indexed without
loading the whole catalog. Draft selections use `approval: needs_approval`; promote a reviewed draft
with `finalize-selection --approve-final-preview`, which writes a backup of the previous
`docs/source_selection.json`.

### discover-external-sources

Command:

```powershell
uv run discover-external-sources --workspace workspaces/<project> --external-root D:\Cold_Storage
```

Use after a user points to a large external folder. Keep the repo workspace under
`workspaces/<project>` and treat the external folder as a source root. The command performs
metadata/path-only classification, groups related datasets and documents, detects raw files, docs,
Delta tables, DuckDB/SQLite files, logs, specs, system/session state, and writes:

```text
workspaces/<project>/interns/generated/requirements/external_source_discovery.json
workspaces/<project>/interns/reports/external_source_discovery.md
workspaces/<project>/docs/source_selection.generated.json
```

It recommends data-engineering strategies such as raw CSV medallion intake with dictionaries,
metadata-first profiling when docs are missing, Delta external-table inspection, database metadata
inspection, or exclusion for logs/runtime state. The generated source selection is review-gated with
`approval: needs_approval`; promote it only after review.

### prepare-external-source-intake

Command:

```powershell
uv run prepare-external-source-intake --external-root D:\Cold_Storage --proposed-workspace workspaces/cms
uv run apply-external-source-intake --external-root D:\Cold_Storage --proposed-workspace workspaces/cms --answer option_a
```

Use when the user gives an external path before choosing whether it belongs to an existing workspace
or a new workspace. The workflow writes a deterministic route panel, remembers repo-level defaults,
records per-workspace intake memory, runs metadata-only discovery after routing, then asks outcome
and source-group questions. Current panel files live at:

```text
workspaces/<project>/interns/reports/external_source_intake/current.json
workspaces/<project>/interns/reports/external_source_intake/current.md
```

The session and memory are written to:

```text
workspaces/<project>/interns/generated/requirements/external_source_intake_session.json
workspaces/<project>/interns/generated/memory/external_source_intake_memory.json
state/team_memory/external_source_intake_preferences.json
```

If a saved routing default exists and the user chooses a different route, the workflow asks for a
change reason before continuing. A one-off change does not update the default unless
`--save-as-default` is used.

### resource-preflight

Command:

```powershell
uv run resource-preflight --workspace workspaces/<project>
```

Writes local CPU, memory, disk, budget, recommended worker/API-concurrency, and resource mode
evidence under `interns/generated/evidence/resource_preflight.json` and
`interns/reports/resource_preflight.md`. Use it before heavyweight ingestion, profiling,
transformation, or local loading. `source-catalog preflight` calls the same resource layer and marks
disk/RAM budget blockers before fetching or staging data. `onboard-workspace` applies resource
profile settings by reducing sample rows and disabling expensive checks under pressure.
`build-medallion` uses strict local resource gating and returns a remote-execution recommendation
when the local build is unsafe. `plan-source-to-target` writes `resource_transform_settings` into
the generated plan, `generate-kpi-sql` includes the resource mode/strategy in SQL and blocks local
DuckDB generation when the plan requires remote execution, and local DuckDB execution records or
enforces the resource decision before subprocess launch.

### context-router

Command:

```powershell
uv run context-router build --workspace workspaces/<project> --task plan-source-to-target --budget standard
```

Builds a bounded context pack from canonical workspace artifacts without loading raw datasets into
chat or prompts. It writes a page index, JSONL page store, task manifest, and human wiki under:

```text
workspaces/<project>/interns/generated/context/context_index.json
workspaces/<project>/interns/generated/context/context_pages.jsonl
workspaces/<project>/interns/generated/context/manifests/<task>_<budget>.json
workspaces/<project>/interns/reports/context/<task>_<budget>.md
```

Use named budgets `small`, `standard`, or `deep`, optionally bounded further with
`--max-sections`, `--max-bytes`, and `--max-estimated-tokens`. The context layer is derived: source
artifacts such as profile indexes, KPI mappings, relationship contracts, source catalog selections,
resource evidence, and engine memory remain authoritative. `plan-source-to-target` now builds and
records a context manifest automatically.

### record-engine-evolution

Command:

```powershell
uv run record-engine-evolution --workspace workspaces/<project> --stage gold_kpi --engine polars --workload-signature csv_groupby --resource-mode local_streaming --elapsed-seconds 1.2
```

Records validated SQL/Polars/PySpark stage outcomes under
`interns/generated/memory/engine_evolution.json` and appends human-readable lessons to
`interns/generated/memory/evolution.md`. `plan-source-to-target --target-engine hybrid` reads these
lessons and records the current engine recommendation in the generated source-to-target plan.
Use the `--workload-shape-json`, `--decision-analysis-json`, `--bottlenecks-json`,
`--alternatives-json`, `--validation-json`, `--promotion-json`, and `--next-experiment-json` options
to store detailed learning evidence. The derived lesson keeps compact routing signals such as
workload family, common bottlenecks, rejected alternatives, confidence, promotion state, and next
experiment.

### ingest-source-catalog

Command:

```powershell
uv run source-catalog run --workspace workspaces/<project>
uv run ingest-source-catalog --workspace workspaces/<project>
```

Use after reviewing the source catalog plan and approving the workspace selection. API and local
sources write only under the workspace `datasets/` or `docs/` tree and create sidecar provenance
files with hashes. Databricks UC remains metadata-only and returns `planned_only` unless
`AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` is set; it does not mutate remote data.

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
panel. It also gates on blocking workspace product bugs detected by the shared bug detector. It is
read-only and exits nonzero on schema/format errors or Critical/High workspace bugs.

### prepare-workspace-bug-report

Command:

```powershell
uv run prepare-workspace-bug-report --workspace workspaces/<project>
```

Use when workspace selection, onboarding, validation, or kickstart behavior contradicts the evidence
available in the workspace. It writes a structured JSON bug report plus a human-readable Markdown
report. The first detector rule catches the dangerous case where `list-workspace-files` finds
dataset/KPI/data-model evidence but onboarding generates empty input, profile, or KPI artifacts.

Outputs:

```text
workspaces/<project>/interns/generated/evidence/bug_report.json
workspaces/<project>/interns/reports/bugs/current.md
```

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
workspaces/<project>/interns/generated/context/manifests/plan-source-to-target_standard.json
workspaces/<project>/interns/reports/context/plan-source-to-target_standard.md
```

The plan records selected and rejected datasets, feature-to-column mappings, join candidates, grain,
temporal anchors, medallion layers, validation checks, resource settings, context manifest, and
blockers. Treat blockers as hard stops before executable code generation.

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
