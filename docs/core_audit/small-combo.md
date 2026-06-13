# small-combo (contracts/dev/resource/presentation/profiling/skills/observability) — audit

## Purpose
Seven small support packages for the governed KPI/data-engineering control plane: per-artifact
contract versioning + forward migration, the portable test gate + harness dispatcher, local
hardware/resource preflight, stakeholder presentation exports (SVG/Mermaid/XLSX), the metadata-first
data-model profiler (Polars + DuckDB pushdown + downcast policy), tool-agnostic skill/subagent adapter
generation, and observability (JSONL events, metric parser, Local+Databricks telemetry dual-write).

## Files
| Package | File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- | --- |
| contracts | versioning.py | 124 | Central per-artifact version registry + forward-migration chain | `ContractVersion`, `register_contract`, `register_migration`, `contract_version`, `known_artifacts`, `migrate` |
| contracts | __init__.py | 1 | Package docstring only (no re-exports) | — |
| dev | green_gate.py | 240 | Portable CI green gate (curated+enterprise) + `--sweep` blast-radius classifier | `CURATED_MODULES`, `ENTERPRISE_MODULES`, `SWEEP_MODULES`, `KNOWN_BASELINE`, `_run`, `_failing_ids`, `main` |
| dev | harness_cli.py | 78 | One front-door dispatcher routing `harness <suite>` to nine suite mains | `SUITES`, `main` |
| dev | __init__.py | 1 | Package docstring only | — |
| resource | manager.py | 411 | Stdlib-only hardware detection + resource budget/decision + profiling/transform settings | `HardwareProfile`, `ResourceBudget`, `ResourceDecision`, `ProfilingResourceSettings`, `TransformationResourceSettings`, `ResourceReport`, `ResourceManager`, `detect_hardware` |
| resource | cli.py | 48 | CLI wrapper writing `resource_preflight.json`/`.md` | `main` |
| resource | __init__.py | 17 | Re-exports core dataclasses + manager | — |
| presentation | console_tables.py | 48 | Markdown table rendering for query/cursor results | `render_markdown_table`, `render_query_result_table`, `_format_cell`, `_pad` |
| presentation | exports.py | 521 | Stakeholder exports: data-model SVG/Mermaid, KPI XLSX, proof packet + manifest | `WorkspacePresentationExporter`, `_render_model_svg`, `_render_mermaid_markdown`, `_write_kpi_workbook`, `_domain_model_to_contract`, 3 CLI mains |
| presentation | __init__.py | 2 | Package docstring only (no re-exports) | — |
| profiling | data_model_profiler.py | 660 | Metadata-first profiler: parquet stats / DuckDB-CSV pushdown / Polars sample+exact / downcast | `DataModelProfiler`, `ColumnProfile`, `DatasetProfile`, `DowncastRecommendation`, `_smallest_integer_dtype`, dtype helpers |
| profiling | __init__.py | 15 | Re-exports profiler dataclasses | — |
| skills | adapter_generator.py | 891 | Generate `.agents/<tool>/SKILLS.md` + indexes + native subagents from `skills/*/SKILL.md` | `SkillAdapterGenerator`, `SkillDefinition`, `ToolRegistry`, `SubagentDefinition`, `_render_*`, `main` |
| skills | __init__.py | 1 | Package docstring only | — |
| observability | events.py | 123 | Best-effort stdlib JSONL event emitter + `time_command` CM | `emit_event`, `time_command`, `_safe_details`, `_events_path` |
| observability | parser.py | 48 | MetricParser strategy + regex/stub implementations | `MetricParser`, `RegexLogParser`, `StubMetricParser` |
| observability | telemetry_backend.py | 247 | Local (SQLite) + Databricks (MLflow3) telemetry; additive dual-write | `TelemetryBackend`, `LocalTelemetry`, `DatabricksTelemetry`, `build_telemetry_backend` |
| observability | __init__.py | 19 | Re-exports parser + telemetry symbols | — |

## Findings
| Tag | Location (pkg/file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [DEAD] | contracts/versioning.py:73 `migrate` | `migrate()` is never called by any production reader — grep shows only `tests/test_contract_versioning.py` invokes it. Eight writers `register_contract` at import, but no read path upgrades a payload. The whole forward-migration machinery (`register_migration`, `_MIGRATIONS`, the step loop) is currently exercised only by tests. | Wire `migrate()` into the artifact read/load helpers (e.g. metadata store / contract loaders) so on-disk payloads are actually upgraded, or document it as a not-yet-consumed seam. |
| [MISSING] | contracts/versioning.py (registry) | No `register_migration` is ever called in production; every registered contract is at `current_version=1`. The registry has zero registered migrations, so the migrate chain step (`no migration registered` ValueError) can only fire once a v2 contract ships — there is no v1→v2 in-tree to prove the chain end-to-end outside tests. | Acceptable while all contracts are v1, but add a CI assertion that any contract whose `current_version>1` has a contiguous migration chain registered. |
| [INTEGRATION] | resource/manager.py:206 / cli.py | `ResourceReport` sets `artifact_type="resource_preflight.json", version=1` but `resource_preflight.json` is NOT passed to `register_contract` anywhere. It mimics the contract shape without joining the versioning registry, so `migrate()` would pass it through unchanged forever. | Call `register_contract("resource_preflight.json", current_version=1)` at module import for consistency, or drop the `artifact_type/version` fields to avoid implying registry membership. |
| [INTEGRATION] | presentation/exports.py:180 | `presentation_manifest.json` likewise declares `artifact_type`+`version=PRESENTATION_VERSION` but is never `register_contract`'d. Same orphaned-contract pattern as resource preflight. | Register it, or document that these are self-versioned artifacts outside the migration registry. |
| [BUG] | presentation/console_tables.py:12-19 | `render_markdown_table` indexes `row[idx]` for every header index across all rows. A ragged row (fewer cells than headers) raises `IndexError`; an over-long row silently drops trailing cells. The `max(..., *(len(row[idx]) ...))` width expr also assumes every row has all columns. | Pad/truncate each row to `len(headers)` (e.g. `row[idx] if idx < len(row) else ""`) before width calc and rendering. |
| [NOT-PROD] | profiling/data_model_profiler.py:646-659 `_safe_min/_safe_max` | Cross-row-group parquet min/max merge uses raw `<=`/`>=` on `stats.min`/`stats.max` values whose Python types are decoder-dependent (bytes for binary, mixed for some logical types). Comparing incomparable types raises `TypeError` mid-profile with no guard, unlike the Polars/DuckDB paths which are wrapped. | Wrap the parquet metadata merge in try/except appending a `warning` like the other profile paths, or coerce/skip non-orderable stat types. |
| [NOT-PROD] | profiling/data_model_profiler.py:607-619 `_smallest_integer_dtype` | Downcast bound search ignores the declared signedness vs. observed sign mismatch: for an `Int64` column with `lo<0` it picks a signed bound (correct), but for declared-unsigned dtype with an observed negative `lo` it still forces `UNSIGNED_BOUNDS` (`unsigned or lo>=0`), recommending a UInt that cannot hold the negative min. Practically rare (unsigned source with negatives) but a lossy recommendation. | When `unsigned` is declared but `lo<0`, fall back to signed bounds (or emit `needs_review`) instead of an unsigned target. |
| [NOT-PROD] | observability/telemetry_backend.py:189 `log_evaluation` | `DatabricksTelemetry.log_evaluation` reads `self._active_run.info.run_id` but the whole body is in a broad `except Exception: print(...)`. If `_active_run` is None (eval called outside a begin/end_run window) it still starts a nested run with `run_id=None`; failures are swallowed to stdout only — no event/telemetry_partial signal. | Guard for `_active_run is None` explicitly and surface failures via the events emitter or a structured `telemetry_partial` rather than `print`. |
| [NOT-PROD] | observability/telemetry_backend.py:222 / 203 | `write_to_delta` and `log_evaluation` report failures only via `print(..., flush=True)` to stdout (not logging, not events.jsonl). In a governed control plane these silent-ish partial-telemetry failures are invisible to the observability pipeline they belong to. | Route through `logging`/`emit_event` so Delta-write and eval failures are captured as structured `telemetry_partial` events. |
| [NOT-PROD] | observability/events.py:28-44 `_safe_details` | Strict probe `json.dumps(details)` is run, then on emit a second `json.dumps(record, default=str)` runs — details that pass the strict probe are fine, but details rejected by the probe become `{"_repr": str}` while the final fallback at :77 re-`str()`s the *original* `details` (post-`_safe_details`). Minor double-handling; behavior is correct but the two fallbacks are redundant. | Collapse to one serialization attempt; keep the `_repr` shape from `_safe_details` only. |
| [BUG] | observability/parser.py:13-21 `RegexLogParser.parse_metric` | `line.startswith(f"{metric_key}:")` matches any line whose key is a prefix of another (`loss:` also matches `loss_total:`? no — colon guards that, but `acc:` matches `acc:` only). Real risk: leading whitespace lines (`  acc: 0.9`) never match because `startswith` requires column-0. `parse_all_metrics` (block form) handles indentation via `partition`, so the two parsers disagree on indented logs. | Use `line.strip().startswith` (or a regex anchored after optional whitespace) in `parse_metric` for parity with `parse_all_metrics`. |
| [DEAD] | observability/telemetry_backend.py:208 `write_to_delta` | `DatabricksTelemetry.write_to_delta` is not called anywhere in `core/` (grep: defined here, no callers; loop/intern_bus use begin/end_run + log_intern_trace only). The "profiler + intern logs → Delta" use case in the docstring is unwired. | Either wire it into the loop/profiler Delta path or mark it explicitly as a planned seam. |
| [DUP] | observability/telemetry_backend.py:91 vs intern_bus.py:140 | `LocalTelemetry.log_intern_trace` calls `workspace.log_intern_activity`, and `InternBus` (line 140) ALSO calls `workspace.log_intern_activity` directly for the same intern call — so when `db_telemetry` is the one passed to the bus, the local activity log is written by the bus, not the LocalTelemetry instance. The two write paths overlap and `LocalTelemetry.log_intern_trace` is effectively bypassed for bus-driven interns. | Pick one owner of `log_intern_activity` (bus or LocalTelemetry) to avoid double/divergent intern-activity rows. |
| [NOT-PROD] | profiling/data_model_profiler.py:215-232 | Polars fallback path: when pushdown is off AND `pl is None` AND `files` is empty, neither branch runs and no warning is appended — a non-CSV/non-parquet path (e.g. `.json`) with polars present but zero matched files silently yields an empty schema with no diagnostic. | Append an explicit warning when `not columns and not schema` after all paths (no profiling source succeeded). |
| [DEAD] | resource/manager.py:183 `check_disk_budget` | Thin alias delegating to `decide(...)`; no caller in repo (grep of `check_disk_budget` finds only the definition). `profiling_settings`/`transformation_settings`/`write_report`/`decide` are used by tests + onboarding; this wrapper is dead. | Remove or document; not load-bearing. |

## Cross-package coupling
- contracts ← onboarding writers (8 modules `register_contract` at import). The registry is populated as
  an import side-effect, so `known_artifacts()`/`migrate()` only see types whose writer modules were imported
  this session — an ordering hazard if a reader calls `migrate()` before the writer module loads. Today moot
  because no reader calls `migrate()`.
- observability/telemetry → orchestration/loop.py (builds both backends; local always, db conditional — dual-write
  confirmed: `local_telemetry.begin_run/end_run` at loop.py:317/398/476/592 run unconditionally, `db_telemetry`
  guarded by `if self.db_telemetry`). intern_bus + mutator receive `db_telemetry` only.
- profiling → governance.contracts.DowncastPolicy (downcast gating); consumed by onboarding (data_understanding,
  catalog, source_to_target_planner, proof_packet) and dashboard.py.
- resource → storage.workspace_layout (output dirs); consumed by execution/backend.py and context/router.py.
- presentation → storage.workspace_layout + lazy onboarding.kpi.proof_packet; xlsxwriter optional dep guarded.
- skills/adapter_generator → reads `skills/*/SKILL.md` (17 present) + `.agents/tools.json` + `skills/*/agents/*.yaml`;
  emits `.agents/{generic,claude,gemini,codex}/SKILLS.md` (all present and in sync with current outputs per grep).
- dev/green_gate + harness_cli reference test modules and `core.onboarding.harness.*` mains; all nine harness
  suite modules referenced by harness_cli exist on disk.

## Verdict
- contracts: structurally production-ready and well-tested, but the migration path is dead in production (`migrate()`
  unused outside tests) — top risk: a future v2 contract ships with no reader actually migrating on load.
- dev: production-ready; pure orchestration over existing suites; main risk is the hardcoded curated-module list
  drifting from real CI.
- resource: production-ready and conservative; minor dead alias + two orphaned self-versioned artifacts.
- presentation: production-ready for the happy path; top risk is the ragged-row `IndexError` in console_tables and
  orphaned manifest versioning.
- profiling: largely production-ready (DuckDB pushdown well-guarded with Polars fallback); top risk is unguarded
  parquet cross-row-group min/max comparison (`TypeError` mid-profile) and a lossy unsigned-downcast edge.
- skills: production-ready and in sync with generated adapters; strict validation throughout; no dead symbols.
- observability: dual-write is correct, but Databricks-side failures are stdout-only (no structured telemetry_partial),
  `write_to_delta` is unwired, and parser indentation handling diverges between the two methods — top risk: silent
  Databricks telemetry/Delta failures invisible to the observability pipeline.
