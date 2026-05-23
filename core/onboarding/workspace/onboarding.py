"""Fresh workspace onboarding for KPI/query optimization tasks.

The onboarder treats ``workspaces/<project>`` as user input and writes every
generated artifact under ``workspaces/<project>/interns``.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from core.onboarding.kpi.text_parser import (
    KPI_CUTS_HEADERS,
    cell_at,
    clean_cell,
    extract_kpis_from_sql,
    first_existing,
    first_index,
    infer_metric_and_cuts,
    is_template_kpi_row,
)
from core.resource.manager import ResourceManager
from core.profiling.data_model_profiler import DataModelProfiler
from core.storage.external_data import is_external_path, load_external_data_policy
from core.storage.metadata_store import MetadataStore, build_metadata_store
from core.storage.workspace_layout import WorkspaceLayout

try:
    import polars as pl
except ImportError:  # pragma: no cover - optional at runtime
    pl = None


DATA_SUFFIXES = {".csv", ".parquet", ".pq", ".json", ".ndjson"}
REGISTRY_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".json", ".md", ".sql", ".txt", ".yaml", ".yml", ".toml"}
MODEL_SUFFIXES = {".csv", ".md", ".png", ".jpg", ".jpeg", ".svg", ".json", ".sql", ".txt", ".yaml", ".yml"}


@dataclass(frozen=True)
class WorkspaceInputs:
    workspace: str
    data_files: list[str] = field(default_factory=list)
    kpi_registries: list[str] = field(default_factory=list)
    data_models: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class KpiDefinition:
    name: str
    description: str = ""
    cuts: str = ""
    metric: str = ""
    refinement_required: str = ""
    source: str = ""
    status: str = "needs_mapping"


@dataclass(frozen=True)
class OnboardingResult:
    workspace: str
    interns_dir: str
    inputs: WorkspaceInputs
    kpi_count: int
    profile_count: int
    artifacts: dict[str, str]
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "interns_dir": self.interns_dir,
            "inputs": asdict(self.inputs),
            "kpi_count": self.kpi_count,
            "profile_count": self.profile_count,
            "artifacts": self.artifacts,
            "warnings": self.warnings,
        }


class WorkspaceOnboarder:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        exact_profile: bool = False,
        sample_rows: int = 100_000,
        metadata_store: MetadataStore | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.exact_profile = exact_profile
        self.sample_rows = sample_rows
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.profiler = DataModelProfiler()
        self.metadata_store = metadata_store or build_metadata_store(self.layout, repo_root=self.repo_root)

    def run(self) -> OnboardingResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        self._clear_onboarding_artifacts()
        inputs = self.discover_inputs()
        estimated_profile_bytes = _total_existing_bytes(inputs.data_files, self.repo_root)
        resource_manager = ResourceManager(self.workspace, repo_root=self.repo_root)
        resource_artifacts = resource_manager.write_report(
            estimated_bytes=estimated_profile_bytes,
            workload="profile",
        )
        profiling_settings = resource_manager.profiling_settings(
            requested_sample_rows=self.sample_rows,
            requested_exact=self.exact_profile,
            estimated_bytes=estimated_profile_bytes,
        )
        kpis, kpi_warnings = self.load_kpis(inputs.kpi_registries)
        profiles, profile_warnings = self.profile_inputs(
            inputs.data_files,
            sample_rows=profiling_settings.sample_rows,
            exact_profile=profiling_settings.exact_profile,
            expensive_checks=profiling_settings.expensive_checks,
            resource_mode=profiling_settings.mode,
        )

        artifacts = {
            "input_inventory": self._write_json(
                self.layout.requirements_dir / "input_inventory.json",
                asdict(inputs),
            ),
            "kpi_registry": self._write_json(
                self.layout.contracts_dir / "kpi_registry.json",
                {
                    "artifact_type": "kpi_registry.json",
                    "version": 1,
                    "generated_by": "onboard-workspace",
                    "workspace": _rel(self.workspace, self.repo_root),
                    "source_registries": inputs.kpi_registries,
                    "kpis": [asdict(kpi) for kpi in kpis],
                },
            ),
            "domain_model": self._write_json(
                self.layout.contracts_dir / "domain_model.json",
                self._build_domain_model(inputs, profiles),
            ),
            "semantic_contract": self._write_json(
                self.layout.contracts_dir / "semantic_contract.json",
                self._build_semantic_contract(kpis, inputs),
            ),
            "open_questions": self._write_open_questions(kpis, kpi_warnings + profile_warnings),
            "stakeholder_interview": self._write_stakeholder_interview(inputs, kpis),
            "baseline_sql": self._write_baseline_sql(kpis),
            "experiment": self._write_experiment_script(),
            "evaluator": self._write_evaluator_script(),
            "onboarding_report": self._write_report(inputs, kpis, profiles),
            **resource_artifacts,
        }
        profile_index = self._write_json(
            self.layout.profiles_dir / "profile_index.json",
            {
                "artifact_type": "profile_index.json",
                "version": 1,
                "generated_by": "onboard-workspace",
                    "workspace": _rel(self.workspace, self.repo_root),
                    "profiles": profiles,
                    "resource_profile_settings": profiling_settings.to_dict(),
                },
            )
        artifacts["profile_index"] = profile_index
        artifacts["generated_file_readability"] = self._write_generated_file_readability()

        return OnboardingResult(
            workspace=str(self.workspace),
            interns_dir=str(self.layout.interns_dir),
            inputs=inputs,
            kpi_count=len(kpis),
            profile_count=len(profiles),
            artifacts=artifacts,
            warnings=kpi_warnings + profile_warnings,
        )

    def discover_inputs(self) -> WorkspaceInputs:
        classified = self._classified_workspace_inputs()
        data_files: list[Path] = [
            path
            for path, roles in classified
            if "dataset_evidence" in roles
            and "kpi_input" not in roles
            and "data_model_input" not in roles
            and path.suffix.lower() in DATA_SUFFIXES
            and self.layout.is_dataset_allowed(path)
        ]
        data_files.extend(self._external_data_files())

        kpi_registries = [
            path
            for path, roles in classified
            if "kpi_input" in roles and path.suffix.lower() in REGISTRY_SUFFIXES
        ]
        data_models = [
            path
            for path, roles in classified
            if "data_model_input" in roles and path.suffix.lower() in MODEL_SUFFIXES
        ]

        return WorkspaceInputs(
            workspace=str(self.workspace),
            data_files=[_rel(path, self.repo_root) for path in sorted(set(data_files))],
            kpi_registries=[_rel(path, self.repo_root) for path in sorted(set(kpi_registries))],
            data_models=[_rel(path, self.repo_root) for path in sorted(set(data_models))],
        )

    def _classified_workspace_inputs(self) -> list[tuple[Path, set[str]]]:
        from tools.list_workspace_files import list_workspace_files

        listing = list_workspace_files(self.repo_root, _rel(self.workspace, self.repo_root))
        classified: list[tuple[Path, set[str]]] = []
        for item in listing.classifications:
            raw_path = item.get("path")
            if not isinstance(raw_path, str):
                continue
            path = (self.repo_root / raw_path).resolve()
            if not path.exists() or not path.is_file():
                continue
            roles = {str(role) for role in item.get("roles") or []}
            if roles:
                classified.append((path, roles))
        return classified

    def load_kpis(self, registry_paths: list[str]) -> tuple[list[KpiDefinition], list[str]]:
        kpis: list[KpiDefinition] = []
        warnings: list[str] = []
        for registry in registry_paths:
            path = self.repo_root / registry
            try:
                if path.suffix.lower() in {".xlsx", ".xlsm"}:
                    kpis.extend(_read_excel_kpis(path))
                elif path.suffix.lower() == ".csv" and pl:
                    kpis.extend(_read_tabular_kpis(pl.read_csv(path), source=_rel(path, self.repo_root)))
                elif path.suffix.lower() == ".json":
                    kpis.extend(_read_json_kpis(path, self.repo_root))
                elif path.suffix.lower() == ".md":
                    kpis.extend(_read_markdown_kpis(path, self.repo_root))
                elif path.suffix.lower() == ".sql":
                    kpis.extend(_read_sql_comment_kpis(path, self.repo_root))
                else:
                    warnings.append(f"unsupported_registry_format:{_rel(path, self.repo_root)}")
            except Exception as exc:
                warnings.append(
                    f"kpi_registry_read_failed:{_rel(path, self.repo_root)}:"
                    f"{type(exc).__name__}:{exc}"
                )
        return kpis, warnings

    def profile_inputs(
        self,
        data_files: list[str],
        *,
        sample_rows: int | None = None,
        exact_profile: bool | None = None,
        expensive_checks: bool = True,
        resource_mode: str = "local_standard",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        profiles: list[dict[str, Any]] = []
        warnings: list[str] = []
        effective_sample_rows = self.sample_rows if sample_rows is None else sample_rows
        effective_exact = self.exact_profile if exact_profile is None else exact_profile
        for file in data_files:
            path = self.repo_root / file
            try:
                profile = self.profiler.profile_path(
                    path,
                    sample_rows=effective_sample_rows,
                    exact=effective_exact,
                )
                profile_path = self.layout.profiles_dir / f"{_safe_stem(path, self.workspace)}.profile.json"
                profile_path.write_text(profile.to_json(), encoding="utf-8")
                summary = profile.summary()
                summary["profile_path"] = _rel(profile_path, self.repo_root)
                summary["resource_mode"] = resource_mode
                summary["resource_sample_rows"] = effective_sample_rows
                summary["resource_exact_profile"] = effective_exact
                summary["resource_expensive_checks"] = expensive_checks
                if not expensive_checks:
                    summary.setdefault("warnings", []).append("resource_expensive_checks_skipped")
                profiles.append(summary)
                self._store_metadata(
                    "profiles",
                    _safe_stem(path, self.workspace),
                    summary,
                )
            except Exception as exc:
                warnings.append(f"profile_failed:{_rel(path, self.repo_root)}:{type(exc).__name__}:{exc}")
        return profiles, warnings

    def _build_domain_model(
        self,
        inputs: WorkspaceInputs,
        profiles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "artifact_type": "domain_model.json",
            "version": 1,
            "generated_by": "onboard-workspace",
            "workspace": inputs.workspace,
            "data_models": inputs.data_models,
            "datasets": [
                {
                    "path": profile["path"],
                    "format": profile["format"],
                    "row_count": profile["row_count"],
                    "schema": profile["schema"],
                    "profile_path": profile.get("profile_path"),
                }
                for profile in profiles
            ],
            "status": "generated_from_workspace_inputs",
        }

    def _build_semantic_contract(
        self,
        kpis: list[KpiDefinition],
        inputs: WorkspaceInputs,
    ) -> dict[str, Any]:
        return {
            "workspace": inputs.workspace,
            "kpi_count": len(kpis),
            "term_resolution_order": [
                "kpi_registry",
                "data_model_docs_or_diagrams",
                "dataset_schema_profile_evidence",
                "data_dictionary_or_metadata_files",
                "catalog_metadata_if_connected",
                "stakeholder_or_user_clarification",
            ],
            "rules": [
                {
                    "id": f"kpi_{idx:03d}",
                    "name": kpi.name,
                    "metric": kpi.metric,
                    "grain_or_cuts": kpi.cuts,
                    "status": kpi.status,
                    "refinement_required": kpi.refinement_required,
                }
                for idx, kpi in enumerate(kpis, start=1)
            ],
            "guardrails": [
                "preserve_kpi_semantics_before_runtime_optimization",
                "record_assumptions_for_ambiguous_or_missing_fields",
                "mark_unmapped_kpis_as_needs_review",
                "ask_for_missing_dictionary_metadata_catalog_contract_or_sla_files_when_required",
            ],
        }

    def _write_open_questions(self, kpis: list[KpiDefinition], warnings: list[str]) -> str:
        lines = [
            "# Open Questions",
            "",
            "These questions were generated during workspace onboarding.",
            "",
            "Before asking the user, resolve KPI terms from the KPI registry, data model,",
            "dataset profiles, data dictionaries or metadata files, catalog metadata, then",
            "stakeholder clarification.",
            "",
        ]
        for idx, kpi in enumerate(kpis, start=1):
            if kpi.refinement_required:
                lines.append(f"{idx}. **{kpi.name}**: {kpi.refinement_required}")
        if warnings:
            lines.extend(["", "## Warnings", ""])
            lines.extend(f"- {warning}" for warning in warnings)
        path = self.layout.reports_dir / "open_questions.md"
        return self._write_text(path, "\n".join(lines).rstrip() + "\n")

    def _write_stakeholder_interview(
        self,
        inputs: WorkspaceInputs,
        kpis: list[KpiDefinition],
    ) -> str:
        lines = [
            "# Stakeholder Interview Summary",
            "",
            "## Detected Inputs",
            "",
            f"- Workspace: `{inputs.workspace}`",
            f"- KPI registries: {len(inputs.kpi_registries)}",
            f"- Data model files: {len(inputs.data_models)}",
            f"- Data files: {len(inputs.data_files)}",
            "",
            "## Recommended Task Options",
            "",
            "1. Build and optimize KPI/query logic from registry, data model, and datasets. Recommended.",
            "2. Profile datasets and extract schema/metadata.",
            "3. Generate semantic contracts, assumptions, and open questions.",
            "4. Validate an existing solution/evaluator.",
            "",
            "## Accepted Defaults",
            "",
            "- Use Polars for dataframe/file work.",
            "- Keep all generated outputs under `interns/`.",
            "- Generate baseline before optimization.",
            "- Record ambiguous KPI mappings for review.",
            "",
            f"## KPI Count\n\n{kpis.__len__()}",
        ]
        path = self.layout.requirements_dir / "stakeholder_interview.md"
        return self._write_text(path, "\n".join(lines).rstrip() + "\n")

    def _write_baseline_sql(self, kpis: list[KpiDefinition]) -> str:
        values = []
        for idx, kpi in enumerate(kpis, start=1):
            values.append(
                "("
                f"{idx}, "
                f"'{_sql_escape(kpi.name)}', "
                f"'{_sql_escape(kpi.metric)}', "
                f"'{_sql_escape(kpi.cuts)}', "
                f"'{_sql_escape(kpi.status)}'"
                ")"
            )
        if not values:
            values.append("(0, 'No KPI registry found', '', '', 'needs_review')")
        sql = "\n".join([
            "-- Generated baseline KPI manifest.",
            "-- Replace manifest-only rows with executable KPI logic as mappings are approved.",
            "CREATE OR REPLACE TABLE kpi_baseline_manifest AS",
            "SELECT * FROM (VALUES",
            ",\n".join(f"  {value}" for value in values),
            ") AS t(kpi_id, kpi_name, metric_expression, grain_or_cuts, status);",
            "",
        ])
        return self._write_text(self.layout.solutions_dir / "kpi_metrics.sql", sql)

    def _write_experiment_script(self) -> str:
        content = '''from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
INTERNS_ROOT = WORKSPACE_ROOT / "interns"
DB_PATH = INTERNS_ROOT / "state" / "analytics.duckdb"
SQL_FILE = INTERNS_ROOT / "generated" / "solutions" / "kpi_metrics.sql"
RESULT_PATH = INTERNS_ROOT / "runs" / "baseline_result.json"


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(DB_PATH))
    started = time.perf_counter()
    success = False
    error = ""
    try:
        conn.execute(SQL_FILE.read_text(encoding="utf-8"))
        success = True
    except Exception as exc:
        error = str(exc)
    elapsed = time.perf_counter() - started
    conn.execute("DROP TABLE IF EXISTS sql_execution_time")
    conn.execute(
        "CREATE TABLE sql_execution_time AS SELECT ? AS execution_time_seconds, ? AS success, ? AS error",
        [elapsed, success, error],
    )
    conn.close()
    RESULT_PATH.write_text(
        json.dumps(
            {"execution_time_seconds": elapsed, "success": success, "error": error},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"execution_time_seconds: {elapsed:.4f}")
    print(f"success: {success}")
    if error:
        print(f"error: {error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
'''
        return self._write_text(self.layout.evaluation_dir / "experiment.py", content)

    def _write_evaluator_script(self) -> str:
        content = '''from __future__ import annotations

from pathlib import Path

import duckdb

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = WORKSPACE_ROOT / "interns" / "state" / "analytics.duckdb"


def main() -> None:
    conn = duckdb.connect(str(DB_PATH))
    row = conn.execute("SELECT execution_time_seconds, success FROM sql_execution_time").fetchone()
    manifest_count = conn.execute("SELECT count(*) FROM kpi_baseline_manifest").fetchone()[0]
    ready_count = conn.execute(
        "SELECT count(*) FROM kpi_baseline_manifest WHERE status = 'ready'"
    ).fetchone()[0]
    conn.close()
    execution_time, success = (float(row[0]), bool(row[1])) if row else (100.0, False)
    matching_score = (
        round((ready_count / manifest_count) * 100.0, 4)
        if success and manifest_count > 0
        else 0.0
    )
    time_score = max(0.0, 10.0 - execution_time) / 10.0 * 5.0 if matching_score == 100.0 else 0.0
    primary_metric = round((matching_score / 100.0) * 5.0 + time_score, 4)
    print("---")
    print(f"primary_metric: {primary_metric}")
    print(f"execution_time_seconds: {execution_time:.4f}")
    print(f"matching_score: {matching_score}")
    print(f"kpi_count: {manifest_count}")
    print(f"ready_kpi_count: {ready_count}")
    print("---")


if __name__ == "__main__":
    main()
'''
        return self._write_text(self.layout.evaluation_dir / "evaluator.py", content)

    def _write_report(
        self,
        inputs: WorkspaceInputs,
        kpis: list[KpiDefinition],
        profiles: list[dict[str, Any]],
    ) -> str:
        lines = [
            "# Workspace Onboarding Report",
            "",
            f"- Workspace: `{inputs.workspace}`",
            f"- KPI definitions: {len(kpis)}",
            f"- Profiled datasets: {len(profiles)}",
            "- Baseline status: manifest generated; executable KPI-specific SQL needs mapping approval.",
            "",
            "## Next Steps",
            "",
            "1. Review `open_questions.md`.",
            "2. Approve or refine KPI-to-column mappings.",
            "3. Replace manifest rows with executable KPI SQL.",
            "4. Run baseline evaluator before optimization.",
        ]
        return self._write_text(self.layout.reports_dir / "onboarding_report.md", "\n".join(lines) + "\n")

    def _write_generated_file_readability(self) -> str:
        workspace = _rel(self.workspace, self.repo_root)
        lines = [
            "# Generated File Readability Map",
            "",
            f"This report classifies files for `{workspace}` by whether they are meant for human",
            "review, machine/tool use, or runtime/cache storage. Paths are relative to the project root.",
            "",
            "## Human-Readable Files",
            "",
            "Human-readable files are mainly Markdown reports, SQL, Python scripts, CSV dictionaries,",
            "and source docs.",
            "",
            "| Path | Readable by human? | What it is |",
            "|---|---:|---|",
            f"| `{workspace}/docs/*.md` | Yes | Workspace source documentation, if present |",
            f"| `{workspace}/wiki/features/*.md` | Yes | Feature notes, if present |",
            f"| `{workspace}/interns/generated/solutions/kpi_metrics.sql` | Yes | Baseline KPI SQL manifest/metadata |",
            f"| `{workspace}/interns/generated/solutions/kpi_*.sql` | Yes | Generated KPI queries after SQL generation |",
            f"| `{workspace}/interns/reports/onboarding_report.md` | Yes | Onboarding summary |",
            f"| `{workspace}/interns/reports/open_questions.md` | Yes | Questions/blockers |",
            f"| `{workspace}/interns/reports/relationship_contracts.md` | Yes | Join/relationship proof, when generated |",
            f"| `{workspace}/interns/reports/source_to_target_plan.md` | Yes | KPI-to-source logic plan, when generated |",
            f"| `{workspace}/interns/reports/blocker_question_panel/current.md` | Yes | Current blocker question, when generated |",
            f"| `{workspace}/interns/reports/bugs/current.md` | Yes | Bug report, when generated |",
            f"| `{workspace}/interns/reports/context/*.md` | Yes | Routed context summaries, when generated |",
            f"| `{workspace}/interns/reports/data_model_generation/*.md` | Yes | Generated data-model review docs, when generated |",
            f"| `{workspace}/interns/reports/derived_feature_reviews/**/*.md` | Yes | Derived feature review docs, when generated |",
            f"| `{workspace}/interns/reports/kpi_generation/current.md` | Yes | KPI generation/review panel, when generated |",
            f"| `{workspace}/interns/generated/memory/*.md` | Yes | Accepted decisions/history, when generated |",
            f"| `{workspace}/interns/evaluation/evaluator.py` | Mostly | Evaluation code |",
            f"| `{workspace}/interns/evaluation/experiment.py` | Mostly | Experiment runner code |",
            "",
            "## Machine-Readable But Inspectable",
            "",
            "These files are mostly for tools and agents, but a reviewer can inspect them when they",
            "need exact structured evidence.",
            "",
            "| Path | Human-readable? | What it is |",
            "|---|---:|---|",
            f"| `{workspace}/interns/generated/contracts/*.json` | Partly | Core contracts for tools/agents |",
            f"| `{workspace}/interns/generated/profiles/*.profile.json` | Partly | Dataset profiles/statistics |",
            f"| `{workspace}/interns/generated/profiles/profile_index.json` | Partly | Index of profile files and profiled datasets |",
            f"| `{workspace}/interns/generated/requirements/*.json` | Partly | Generated requirement/session state |",
            f"| `{workspace}/interns/generated/context/*.json`, `.jsonl` | Partly | Bounded context index/pages |",
            f"| `{workspace}/interns/reports/*/current.json` | Partly | UI/panel backing data |",
            f"| `{workspace}/interns/generated/evidence/*.json` | Partly | Evidence/debug reports |",
            "",
            "## Runtime And Cache Files",
            "",
            "These files are not normal manual review targets. They support local execution,",
            "metadata storage, or repeatable workflow state.",
            "",
            "| Path | Readable? | What it is |",
            "|---|---:|---|",
            f"| `{workspace}/interns/state/*.duckdb` | No | Local DuckDB databases |",
            f"| `{workspace}/interns/state/*.db` | No | Local SQLite databases |",
            f"| `{workspace}/interns/state/delta_metadata/**/*.parquet` | No | Metadata table data |",
            f"| `{workspace}/interns/state/delta_metadata/**/_delta_log/*.json` | Low | Delta transaction log |",
            f"| `{workspace}/interns/state/metadata_store/**/*.json` | Low | JSON fallback metadata cache |",
            "",
            "## Normal Review Starting Point",
            "",
            "For normal KPI review, start with these files:",
            "",
            "```text",
            f"{workspace}/interns/reports/source_to_target_plan.md",
            f"{workspace}/interns/reports/relationship_contracts.md",
            f"{workspace}/interns/generated/solutions/kpi_001.sql",
            f"{workspace}/interns/reports/open_questions.md",
            "```",
            "",
            "Use the Markdown reports first, then inspect JSON contracts only when you need exact",
            "machine-readable evidence.",
        ]
        return self._write_text(
            self.layout.reports_dir / "generated_file_readability.md",
            "\n".join(lines) + "\n",
        )

    def _write_json(self, path: Path, payload: dict[str, Any]) -> str:
        collection = _metadata_collection_for_path(path)
        if collection:
            self._store_metadata(collection, path.stem, payload)
        return self._write_text(path, json.dumps(payload, indent=2, default=str) + "\n")

    def _write_text(self, path: Path, content: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return _rel(path, self.repo_root)

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        policy = load_external_data_policy(self.repo_root)
        if is_external_path(self.workspace, self.repo_root, policy):
            raise ValueError(
                "workspace must be a repo workspace, not an external data root: "
                f"{self.workspace}. Use workspaces/<project> as the workspace and configure "
                "external data through dataset_allowlist."
            )
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _external_data_files(self) -> list[Path]:
        data_files: list[Path] = []
        for allowed in self.layout.external_dataset_allowlist_paths():
            if allowed.is_file() and allowed.suffix.lower() in DATA_SUFFIXES:
                data_files.append(allowed)
            elif allowed.is_dir():
                data_files.extend(
                    path
                    for path in allowed.rglob("*")
                    if path.is_file()
                    and path.suffix.lower() in DATA_SUFFIXES
                    and self.layout.is_dataset_allowed(path)
                )
        return sorted(set(data_files))

    def _clear_onboarding_artifacts(self) -> None:
        for path in self.layout.profiles_dir.glob("*.profile.json"):
            path.unlink()
        for path in [
            self.layout.profiles_dir / "profile_index.json",
            self.layout.requirements_dir / "input_inventory.json",
            self.layout.requirements_dir / "stakeholder_interview.md",
            self.layout.contracts_dir / "kpi_registry.json",
            self.layout.contracts_dir / "domain_model.json",
            self.layout.contracts_dir / "semantic_contract.json",
            self.layout.solutions_dir / "kpi_metrics.sql",
            self.layout.evaluation_dir / "experiment.py",
            self.layout.evaluation_dir / "evaluator.py",
            self.layout.reports_dir / "open_questions.md",
            self.layout.reports_dir / "onboarding_report.md",
            self.layout.reports_dir / "generated_file_readability.md",
        ]:
            if path.exists():
                path.unlink()
        metadata_root = self.layout.state_dir / "metadata_store"
        if metadata_root.exists():
            for path in metadata_root.rglob("*.json"):
                path.unlink()
        delta_root = self.layout.state_dir / "delta_metadata"
        if delta_root.exists():
            shutil.rmtree(delta_root)

    def _store_metadata(
        self,
        collection: str,
        document_id: str,
        payload: dict[str, Any],
    ) -> None:
        result = self.metadata_store.upsert(
            collection,
            document_id,
            payload,
            workspace=str(self.workspace),
        )
        if result.warning:
            warning_path = self.layout.reports_dir / "metadata_store_warnings.log"
            warning_path.parent.mkdir(parents=True, exist_ok=True)
            with warning_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{collection}/{document_id}: {result.warning}\n")


def find_root_artifact_violations(workspace: str | Path) -> list[str]:
    root = Path(workspace)
    forbidden_suffixes = {".duckdb", ".db", ".sqlite", ".log"}
    forbidden_names = {
        "kpi_metrics.sql",
        "analytics.duckdb",
        "evaluator.py",
        "experiment.py",
        "run_kpi_solution.py",
    }
    violations = []
    for path in root.iterdir() if root.exists() else []:
        if path.name == "interns":
            continue
        if path.is_file() and (path.name in forbidden_names or path.suffix.lower() in forbidden_suffixes):
            violations.append(str(path))
    return violations


def _read_excel_kpis(path: Path) -> list[KpiDefinition]:
    if pl:
        try:
            frame = pl.read_excel(path)
            return _read_tabular_kpis(frame, source=str(path))
        except Exception:
            pass
    return _read_xlsx_xml_kpis(path)


def _read_tabular_kpis(frame: Any, source: str) -> list[KpiDefinition]:
    columns = list(frame.columns)
    lowered = {col.lower().strip(): col for col in columns}
    name_col = _first_existing(lowered, ["key business question", "kpi", "kpi name", "metric", "name"])
    desc_col = _first_existing(lowered, ["description", "definition"])
    cuts_col = _first_existing(lowered, KPI_CUTS_HEADERS)
    metric_col = _first_existing(lowered, ["metric", "formula", "expression"])
    refine_col = _first_existing(
        lowered,
        ["data model refinement required", "refinement required", "open questions"],
    )
    if not name_col:
        return []
    kpis = []
    for row in frame.iter_rows(named=True):
        name = _clean_cell(row.get(name_col))
        if not name or _is_template_kpi_row(name):
            continue
        metric = _clean_cell(row.get(metric_col)) if metric_col else ""
        cuts = _clean_cell(row.get(cuts_col)) if cuts_col else ""
        if not metric and not cuts:
            metric, cuts = _infer_metric_and_cuts(name, _clean_cell(row.get(desc_col)) if desc_col else "")
        kpis.append(
            KpiDefinition(
                name=name,
                description=_clean_cell(row.get(desc_col)) if desc_col else "",
                cuts=cuts,
                metric=metric,
                refinement_required=_clean_cell(row.get(refine_col)) if refine_col else "",
                source=source,
            )
        )
    return kpis


def _read_xlsx_xml_kpis(path: Path) -> list[KpiDefinition]:
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in root.findall("main:sheetData/main:row", ns):
        values = []
        for cell in row.findall("main:c", ns):
            text = "".join(node.text or "" for node in cell.findall(".//main:t", ns)).strip()
            values.append(text)
        rows.append(values)
    if not rows:
        return []
    headers = [value.lower().strip() for value in rows[0]]
    index = {header: idx for idx, header in enumerate(headers)}
    name_idx = _first_index(index, ["key business question", "kpi", "kpi name", "metric", "name"])
    if name_idx is None:
        return []
    desc_idx = _first_index(index, ["description", "definition"])
    cuts_idx = _first_index(index, KPI_CUTS_HEADERS)
    metric_idx = _first_index(index, ["metric", "formula", "expression"])
    refine_idx = _first_index(index, ["data model refinement required", "refinement required"])
    kpis = []
    for row in rows[1:]:
        name = _cell_at(row, name_idx)
        if not name or _is_template_kpi_row(name):
            continue
        metric = _cell_at(row, metric_idx)
        cuts = _cell_at(row, cuts_idx)
        if not metric and not cuts:
            metric, cuts = _infer_metric_and_cuts(name, _cell_at(row, desc_idx))
        kpis.append(
            KpiDefinition(
                name=name,
                description=_cell_at(row, desc_idx),
                cuts=cuts,
                metric=metric,
                refinement_required=_cell_at(row, refine_idx),
                source=str(path),
            )
        )
    return kpis


def _read_json_kpis(path: Path, repo_root: Path) -> list[KpiDefinition]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("kpis", data if isinstance(data, list) else [])
    kpis = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = _clean_cell(item.get("name") or item.get("kpi") or item.get("question") or item.get("kpi_name") or item.get("business_question"))
        if name:
            description = _clean_cell(item.get("description") or item.get("definition"))
            cuts = _clean_cell(item.get("cuts") or item.get("grain") or item.get("dimensions"))
            metric = _clean_cell(item.get("metric") or item.get("formula"))
            if not metric and not cuts:
                metric, cuts = _infer_metric_and_cuts(name, description)
            kpis.append(
                KpiDefinition(
                    name=name,
                    description=description,
                    cuts=cuts,
                    metric=metric,
                    refinement_required=_clean_cell(item.get("refinement_required")),
                    source=_rel(path, repo_root),
                )
            )
    return kpis


def _read_markdown_kpis(path: Path, repo_root: Path) -> list[KpiDefinition]:
    text = path.read_text(encoding="utf-8")
    kpis = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("|---") or stripped.startswith("| :"):
            continue
        if stripped.startswith("|") and "kpi" not in stripped.lower():
            cells = [cell.strip(" *`") for cell in stripped.strip("|").split("|")]
            if cells and cells[0]:
                kpis.append(KpiDefinition(name=cells[0], description=" | ".join(cells[1:]), source=_rel(path, repo_root)))
        elif re.match(r"^#{1,4}\s+", stripped) and "kpi" in stripped.lower():
            kpis.append(KpiDefinition(name=re.sub(r"^#{1,4}\s+", "", stripped), source=_rel(path, repo_root)))
    return kpis


def _read_sql_comment_kpis(path: Path, repo_root: Path) -> list[KpiDefinition]:
    return [
        KpiDefinition(
            name=_clean_cell(item.get("name")),
            description=_clean_cell(item.get("description")),
            cuts=_clean_cell(item.get("cuts")),
            metric=_clean_cell(item.get("metric")),
            source=_rel(path, repo_root),
        )
        for item in extract_kpis_from_sql(path.read_text(encoding="utf-8"), _rel(path, repo_root))
        if _clean_cell(item.get("name"))
    ]


def _first_existing(lowered: dict[str, str], candidates: list[str]) -> str | None:
    return first_existing(lowered, candidates)


def _is_template_kpi_row(name: str) -> bool:
    return is_template_kpi_row(name)


def _infer_metric_and_cuts(name: str, description: str = "") -> tuple[str, str]:
    return infer_metric_and_cuts(name, description)


def _first_index(index: dict[str, int], candidates: list[str]) -> int | None:
    return first_index(index, candidates)


def _cell_at(row: list[str], idx: int | None) -> str:
    return cell_at(row, idx)


def _clean_cell(value: Any) -> str:
    return clean_cell(value)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _total_existing_bytes(paths: list[str], repo_root: Path) -> int:
    total = 0
    for raw in paths:
        path = repo_root / raw
        if path.exists() and path.is_file():
            total += path.stat().st_size
    return total


def _safe_stem(path: Path, root: Path) -> str:
    try:
        rel = str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        rel = str(path).replace("\\", "/")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", rel).strip("_")


def _metadata_collection_for_path(path: Path) -> str | None:
    name = path.name
    parent = path.parent.name
    if parent == "contracts":
        return "contracts"
    if parent == "requirements":
        return "requirements"
    if parent == "profiles":
        return "profiles"
    if name == "bootstrap_manifest.json":
        return "bootstrap"
    if name == "bootstrap_status.json":
        return "bootstrap"
    return None


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Onboard a workspace into interns/ artifacts.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--exact-profile", action="store_true", help="Run exact scans for profile bounds.")
    parser.add_argument("--sample-rows", type=int, default=100_000, help="Sample rows for profiling.")
    args = parser.parse_args(argv)

    onboarder = WorkspaceOnboarder(
        args.repo_root,
        args.workspace,
        exact_profile=args.exact_profile,
        sample_rows=args.sample_rows,
    )
    result = onboarder.run()
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
