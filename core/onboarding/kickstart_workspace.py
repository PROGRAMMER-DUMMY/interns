"""Enterprise workspace kickstart.

Kickstart bridges a bare workspace path and the governed experiment loop. It
discovers likely enterprise inputs, runs local-safe onboarding, writes a hybrid
task entry, and records missing or ambiguous decisions under the workspace.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.config import Config
from core.governance.contracts import OptimizationPolicy
from core.onboarding.auto_bootstrap import AutoBootstrap
from core.onboarding.kpi_feature_resolver import KPIFeatureResolver
from core.storage.metadata_store import build_metadata_store
from core.storage.workspace_layout import WorkspaceLayout


DOC_SUFFIXES = {
    ".csv",
    ".doc",
    ".docx",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".svg",
    ".toml",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".yaml",
    ".yml",
}
DATA_SUFFIXES = {".csv", ".json", ".ndjson", ".parquet", ".pq"}
TEXT_SUFFIXES = {".csv", ".json", ".md", ".toml", ".yaml", ".yml"}

CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("kpi_registry", ("kpi", "metric", "registry", "measure")),
    ("sla", ("sla", "service_level", "service-level", "runtime", "latency", "availability")),
    ("policy", ("policy", "governance", "approval", "guardrail", "control", "risk")),
    ("data_contract", ("contract", "interface", "schema_contract", "quality", "expectation")),
    ("data_dictionary", ("dictionary", "glossary", "catalog", "metadata", "column")),
    ("data_model", ("model", "schema", "diagram", "erd", "star")),
    ("methodology", ("methodology", "spec", "definition", "calculation", "formula")),
)


@dataclass(frozen=True)
class DiscoveredFile:
    path: str
    category: str
    confidence: str
    reason: str
    suffix: str
    size_bytes: int


@dataclass(frozen=True)
class DiscoveredDataset:
    path: str
    format: str
    size_bytes: int
    inspection_mode: str = "metadata_schema_profiler"


@dataclass(frozen=True)
class KickstartResult:
    task: dict[str, Any]
    bootstrap: dict[str, Any]
    discovery_path: str
    task_config_path: str
    open_questions_path: str
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "bootstrap": self.bootstrap,
            "discovery_path": self.discovery_path,
            "task_config_path": self.task_config_path,
            "open_questions_path": self.open_questions_path,
            "warnings": self.warnings,
        }


class WorkspaceKickstarter:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        task_id: str | None = None,
        name: str | None = None,
        domain: str = "healthcare",
        set_active: bool = True,
        sample_rows: int = 100_000,
        cfg: Config | None = None,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.workspace_arg = Path(workspace)
        self.workspace = (self.repo_root / self.workspace_arg).resolve()
        self.task_id = task_id or _slug(self.workspace.name)
        self.name = name or f"{self.workspace.name} KPI Optimization"
        self.domain = domain
        self.set_active = set_active
        self.sample_rows = sample_rows
        self.cfg = cfg
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.metadata_store = build_metadata_store(self.layout, repo_root=self.repo_root)

    def run(self) -> KickstartResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        discovery = self.discover()
        seed_task = self._seed_task()
        bootstrap = AutoBootstrap(
            self.repo_root,
            seed_task,
            self.cfg,
            sample_rows=self.sample_rows,
        ).ensure_ready()
        feature_resolution = KPIFeatureResolver(
            self.repo_root,
            self.layout.project_root,
            domain=self.domain,
            include_candidates=False,
        ).run()
        task = self._task_from_bootstrap(
            seed_task,
            bootstrap.artifacts,
            discovery,
            feature_resolution.summary(),
        )
        task_config_path = self._upsert_task_config(task)
        discovery_path = self._write_discovery(discovery, task)
        open_questions_path = self._write_kickstart_questions(discovery)
        self._store_metadata("requirements", "enterprise_kickstart", {
            "discovery": discovery,
            "task": task,
            "feature_resolution": feature_resolution.summary(),
            "accepted_defaults": accepted_defaults(),
        })
        return KickstartResult(
            task=task,
            bootstrap=bootstrap.summary(),
            discovery_path=discovery_path,
            task_config_path=task_config_path,
            open_questions_path=open_questions_path,
            warnings=discovery["warnings"],
        )

    def discover(self) -> dict[str, Any]:
        docs = [self._classify_doc(path) for path in self._doc_candidates()]
        datasets = [self._dataset_summary(path) for path in self._dataset_candidates()]
        by_category: dict[str, list[dict[str, Any]]] = {}
        for doc in docs:
            by_category.setdefault(doc.category, []).append(asdict(doc))

        missing = [
            category
            for category in ("policy", "sla", "data_contract")
            if not by_category.get(category)
        ]
        ambiguous = [
            category
            for category, entries in by_category.items()
            if category in {"policy", "sla", "data_contract", "kpi_registry"}
            and len(entries) > 1
        ]
        unknown = [asdict(doc) for doc in docs if doc.category == "unknown"]
        warnings = []
        if missing:
            warnings.append("missing_enterprise_docs:" + ",".join(missing))
        if ambiguous:
            warnings.append("ambiguous_enterprise_docs:" + ",".join(ambiguous))
        if unknown:
            warnings.append(f"unknown_document_formats_or_names:{len(unknown)}")

        return {
            "workspace": _rel(self.workspace, self.repo_root),
            "documents": [asdict(doc) for doc in docs],
            "documents_by_category": by_category,
            "datasets": [asdict(dataset) for dataset in datasets],
            "missing_enterprise_docs": missing,
            "ambiguous_categories": ambiguous,
            "unknown_documents": unknown,
            "accepted_defaults": accepted_defaults(),
            "warnings": warnings,
        }

    def _seed_task(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "name": self.name,
            "domain": self.domain,
            "workspace": _rel(self.workspace, self.repo_root),
            "metric_key": "primary_metric",
            "direction": "higher",
            "optimization_policy": default_optimization_policy(),
        }

    def _task_from_bootstrap(
        self,
        seed_task: dict[str, Any],
        artifacts: dict[str, str],
        discovery: dict[str, Any],
        feature_resolution: dict[str, Any],
    ) -> dict[str, Any]:
        task = dict(seed_task)
        if artifacts.get("baseline_sql"):
            task["editable_file"] = artifacts["baseline_sql"]
            task["sql_file"] = artifacts["baseline_sql"]
        if artifacts.get("experiment"):
            task["experiment_cmd"] = ["uv", "run", "python", artifacts["experiment"]]
        if artifacts.get("evaluator"):
            task["evaluator_cmd"] = ["uv", "run", "python", artifacts["evaluator"]]

        semantic_contract = task.setdefault("semantic_contract", {})
        kpi_registry = _first_path(discovery, "kpi_registry")
        if kpi_registry:
            semantic_contract["kpi_registry"] = kpi_registry
        if artifacts.get("semantic_contract"):
            semantic_contract["methodology_json"] = artifacts["semantic_contract"]
        semantic_contract["feature_mapping"] = feature_resolution["mapping_path"]
        semantic_contract.setdefault("rules", [])

        task["enterprise_artifacts"] = {
            "policy_docs": _paths(discovery, "policy"),
            "sla_docs": _paths(discovery, "sla"),
            "data_contract_docs": _paths(discovery, "data_contract"),
            "data_dictionary_docs": _paths(discovery, "data_dictionary"),
            "data_model_docs": _paths(discovery, "data_model"),
            "methodology_docs": _paths(discovery, "methodology"),
            "discovery_json": _rel(self.layout.requirements_dir / "enterprise_discovery.json", self.repo_root),
            "feature_mapping": feature_resolution["mapping_path"],
            "open_questions": _rel(self.layout.reports_dir / "open_questions.md", self.repo_root),
        }
        task["feature_resolution"] = {
            "status": (
                "ready_for_sql"
                if feature_resolution["blocked_kpi_count"] == 0
                else "blocked_questions_pending"
            ),
            **feature_resolution,
        }
        return task

    def _upsert_task_config(self, task: dict[str, Any]) -> str:
        path = self.repo_root / "config" / "tasks.json"
        data = {"active_task": task["id"], "tasks": []}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                backup = path.with_suffix(".json.invalid")
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                data = {"active_task": task["id"], "tasks": []}

        tasks = data.setdefault("tasks", [])
        replaced = False
        for idx, existing in enumerate(tasks):
            if existing.get("id") == task["id"]:
                tasks[idx] = task
                replaced = True
                break
        if not replaced:
            tasks.append(task)
        if self.set_active:
            data["active_task"] = task["id"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return _rel(path, self.repo_root)

    def _write_discovery(self, discovery: dict[str, Any], task: dict[str, Any]) -> str:
        payload = {
            **discovery,
            "task_id": task["id"],
            "task_config_policy": "hybrid_config_with_workspace_metadata",
        }
        path = self.layout.requirements_dir / "enterprise_discovery.json"
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        return _rel(path, self.repo_root)

    def _write_kickstart_questions(self, discovery: dict[str, Any]) -> str:
        path = self.layout.reports_dir / "open_questions.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else "# Open Questions\n"
        lines = [
            existing.rstrip(),
            "",
            "## Enterprise Kickstart Questions",
            "",
        ]
        if discovery["missing_enterprise_docs"]:
            for category in discovery["missing_enterprise_docs"]:
                lines.append(
                    f"- Missing `{category}` document: using conservative defaults until a user provides one."
                )
        if discovery["ambiguous_categories"]:
            for category in discovery["ambiguous_categories"]:
                candidates = ", ".join(_paths(discovery, category))
                lines.append(f"- Confirm which `{category}` document is authoritative: {candidates}")
        if discovery["unknown_documents"]:
            for item in discovery["unknown_documents"]:
                lines.append(f"- Explain unknown document `{item['path']}` or mark it out of scope.")
        if not (
            discovery["missing_enterprise_docs"]
            or discovery["ambiguous_categories"]
            or discovery["unknown_documents"]
        ):
            lines.append("- No enterprise kickstart blockers detected.")
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return _rel(path, self.repo_root)

    def _doc_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        for root_name in ("docs", "documentation", "contracts", "policies"):
            root = self.workspace / root_name
            if root.exists():
                candidates.extend(
                    path
                    for path in root.rglob("*")
                    if path.is_file()
                    and _is_workspace_user_path(path, self.workspace)
                    and path.suffix.lower() in DOC_SUFFIXES
                )
        return sorted(set(candidates))

    def _dataset_candidates(self) -> list[Path]:
        datasets_root = self.workspace / "datasets"
        if not datasets_root.exists():
            return []
        candidates: list[Path] = []
        for path in datasets_root.rglob("*"):
            if not _is_workspace_user_path(path, self.workspace):
                continue
            if not self.layout.is_dataset_allowed(path):
                continue
            if path.is_dir() and (path / "_delta_log").exists():
                candidates.append(path)
            elif path.is_file() and path.suffix.lower() in DATA_SUFFIXES:
                candidates.append(path)
        return sorted(set(candidates))

    def _classify_doc(self, path: Path) -> DiscoveredFile:
        suffix = path.suffix.lower()
        haystack = f"{path.stem} {path.parent.name}".lower().replace(" ", "_")
        content_reason = ""
        if suffix in TEXT_SUFFIXES:
            snippet = _safe_read_snippet(path).lower()
            haystack = f"{haystack} {snippet}"
            content_reason = "filename_and_content"
        for category, tokens in CATEGORY_RULES:
            if any(token in haystack for token in tokens):
                return DiscoveredFile(
                    path=_rel(path, self.repo_root),
                    category=category,
                    confidence="medium" if content_reason else "low",
                    reason=content_reason or "filename",
                    suffix=suffix,
                    size_bytes=path.stat().st_size,
                )
        if suffix in {".png", ".jpg", ".jpeg", ".svg"}:
            category = "data_model" if any(token in haystack for token in ("model", "schema", "diagram", "erd")) else "unknown"
            return DiscoveredFile(
                path=_rel(path, self.repo_root),
                category=category,
                confidence="low",
                reason="image_requires_user_interpretation",
                suffix=suffix,
                size_bytes=path.stat().st_size,
            )
        return DiscoveredFile(
            path=_rel(path, self.repo_root),
            category="unknown",
            confidence="low",
            reason="unclassified_document_name_or_format",
            suffix=suffix,
            size_bytes=path.stat().st_size,
        )

    def _dataset_summary(self, path: Path) -> DiscoveredDataset:
        return DiscoveredDataset(
            path=_rel(path, self.repo_root),
            format=_detect_dataset_format(path),
            size_bytes=_path_size(path),
        )

    def _store_metadata(self, collection: str, document_id: str, payload: dict[str, Any]) -> None:
        self.metadata_store.upsert(
            collection,
            document_id,
            payload,
            workspace=str(self.workspace),
        )

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def default_optimization_policy() -> dict[str, Any]:
    return OptimizationPolicy.from_task({}).summary()


def accepted_defaults() -> dict[str, Any]:
    return {
        "task_config": "hybrid",
        "missing_policy_sla_contract": "use_conservative_defaults_and_record_open_questions",
        "document_binding": "ask_user_to_confirm_when_multiple_authoritative_candidates_exist",
        "dataset_inspection": "metadata_schema_profiler_first",
        "dataframe_library": "polars",
        "databricks_mode_order": ["sql", "sql_polars_hybrid", "pyspark", "polars"],
        "remote_execution": "requires_explicit_approval",
        "generated_output_root": "workspace_interns",
    }


def _paths(discovery: dict[str, Any], category: str) -> list[str]:
    return [item["path"] for item in discovery["documents_by_category"].get(category, [])]


def _first_path(discovery: dict[str, Any], category: str) -> str | None:
    paths = _paths(discovery, category)
    return paths[0] if len(paths) == 1 else None


def _detect_dataset_format(path: Path) -> str:
    if path.is_dir() and (path / "_delta_log").exists():
        return "delta"
    suffix = path.suffix.lower()
    if suffix in {".parquet", ".pq"}:
        return "parquet"
    if suffix == ".csv":
        return "csv"
    if suffix == ".json":
        return "json"
    if suffix == ".ndjson":
        return "ndjson"
    return "unknown"


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())


def _safe_read_snippet(path: Path, limit: int = 16_384) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _is_workspace_user_path(path: Path, workspace: Path) -> bool:
    try:
        parts = path.resolve().relative_to(workspace.resolve()).parts
    except ValueError:
        return False
    return not any(part == "interns" for part in parts) and not path.name.startswith(("~", "."))


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return slug or "workspace"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kickstart a governed enterprise workspace.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--task-id", help="Task id to create or update. Defaults to workspace folder name.")
    parser.add_argument("--name", help="Task display name. Defaults to '<workspace> KPI Optimization'.")
    parser.add_argument("--domain", default="healthcare", help="Task domain.")
    parser.add_argument("--sample-rows", type=int, default=100_000, help="Sample rows for profiling.")
    parser.add_argument("--no-set-active", action="store_true", help="Do not set this task active.")
    args = parser.parse_args(argv)

    result = WorkspaceKickstarter(
        args.repo_root,
        args.workspace,
        task_id=args.task_id,
        name=args.name,
        domain=args.domain,
        set_active=not args.no_set_active,
        sample_rows=args.sample_rows,
    ).run()
    print(json.dumps(result.summary(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
