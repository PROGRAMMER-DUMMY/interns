"""Classify external data roots into governed source-selection candidates."""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.external_data import (
    bounded_external_files,
    is_within_allowed_roots,
    load_external_data_policy,
)
from core.storage.workspace_layout import WorkspaceLayout


DATA_SUFFIXES = {".csv", ".parquet", ".pq", ".json", ".jsonl", ".ndjson"}
DOC_SUFFIXES = {".pdf", ".md", ".txt", ".doc", ".docx", ".html", ".htm", ".png", ".jpg", ".jpeg", ".xlsx", ".xls"}
DB_SUFFIXES = {".duckdb", ".db", ".sqlite", ".sqlite3"}
LOG_SUFFIXES = {".log", ".jsonl"}
SYSTEM_DIR_NAMES = {"system", "sessions", "state", "__pycache__"}


@dataclass(frozen=True)
class ExternalFileClass:
    path: str
    relative_path: str
    group: str
    class_name: str
    format: str
    size_bytes: int
    recommendation: str
    reason: str


@dataclass(frozen=True)
class ExternalGroup:
    group: str
    dataset_count: int = 0
    document_count: int = 0
    delta_table_count: int = 0
    database_count: int = 0
    log_count: int = 0
    other_count: int = 0
    total_size_bytes: int = 0
    strategy: str = "review"
    recommendation: str = "Review before ingestion."
    related_documents: list[str] = field(default_factory=list)
    candidate_datasets: list[str] = field(default_factory=list)
    candidate_delta_tables: list[str] = field(default_factory=list)
    candidate_databases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExternalDiscoveryResult:
    discovery_path: str
    report_path: str
    draft_selection_path: str
    group_count: int
    candidate_source_count: int
    truncated: bool
    warnings: list[str]

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class ExternalSourceDiscoverer:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        external_root: str | Path,
        *,
        max_files: int = 2000,
        max_seconds: float = 30.0,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.external_root = Path(external_root).expanduser().resolve()
        self.max_files = max_files
        self.max_seconds = max_seconds
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(self) -> ExternalDiscoveryResult:
        self._validate()
        self.layout.ensure_runtime_dirs()
        (self.workspace / "docs").mkdir(parents=True, exist_ok=True)
        files, truncated = bounded_external_files(
            self.external_root,
            max_paths=self.max_files,
            max_seconds=self.max_seconds,
        )
        classes = self._classify(files)
        groups = self._groups(classes)
        draft = self._draft_source_selection(groups)
        warnings = []
        if truncated:
            warnings.append(
                f"external_listing_truncated:max_files={self.max_files}:max_seconds={self.max_seconds}"
            )
        if any(group.log_count or group.database_count for group in groups):
            warnings.append("system_state_or_database_files_detected:not_auto_ingested")

        payload = {
            "artifact_type": "external_source_discovery.json",
            "version": 1,
            "generated_by": "discover-external-sources",
            "workspace": _rel(self.workspace, self.repo_root),
            "external_root": str(self.external_root),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scan_policy": {
                "max_files": self.max_files,
                "max_seconds": self.max_seconds,
                "content_read_policy": "metadata_and_paths_only",
            },
            "summary": {
                "file_count": len(classes),
                "group_count": len(groups),
                "candidate_source_count": len(draft["sources"]),
                "truncated": truncated,
            },
            "groups": [asdict(group) for group in groups],
            "files": [asdict(item) for item in classes],
            "warnings": warnings,
        }
        discovery_path = self.layout.requirements_dir / "external_source_discovery.json"
        report_path = self.layout.reports_dir / "external_source_discovery.md"
        draft_path = self.workspace / "docs" / "source_selection.generated.json"
        discovery_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        report_path.write_text(_render_report(payload), encoding="utf-8")
        draft_path.write_text(json.dumps(draft, indent=2, default=str) + "\n", encoding="utf-8")
        return ExternalDiscoveryResult(
            discovery_path=_rel(discovery_path, self.repo_root),
            report_path=_rel(report_path, self.repo_root),
            draft_selection_path=_rel(draft_path, self.repo_root),
            group_count=len(groups),
            candidate_source_count=len(draft["sources"]),
            truncated=truncated,
            warnings=warnings,
        )

    def _classify(self, files: list[Path]) -> list[ExternalFileClass]:
        delta_roots = _delta_roots(files, self.external_root)
        classes: list[ExternalFileClass] = []
        seen_delta_roots: set[Path] = set()
        for path in files:
            owning_delta_root = next(
                (delta_root for delta_root in delta_roots if _is_relative_to(path, delta_root)),
                None,
            )
            if owning_delta_root is not None:
                if owning_delta_root not in seen_delta_roots:
                    delta_root = owning_delta_root
                    seen_delta_roots.add(delta_root)
                    classes.append(self._file_class(delta_root, "delta_table", "delta", "register_delta_external", "directory_has_delta_log"))
                continue
            suffix = path.suffix.lower()
            if any(part.lower() in SYSTEM_DIR_NAMES for part in _relative_parts(path, self.external_root)):
                classes.append(self._file_class(path, "log_or_state", suffix.lstrip(".") or "state", "exclude_by_default", "system_or_log_path"))
            elif suffix in DATA_SUFFIXES:
                classes.append(self._file_class(path, "dataset", _format(path), "register_external_then_profile", "data_file_extension"))
            elif suffix in DOC_SUFFIXES:
                classes.append(self._file_class(path, "document", suffix.lstrip("."), "stage_as_doc_context", "document_extension"))
            elif suffix in DB_SUFFIXES:
                classes.append(self._file_class(path, "database", suffix.lstrip("."), "inspect_metadata_before_export", "database_file_extension"))
            elif suffix in LOG_SUFFIXES:
                classes.append(self._file_class(path, "log_or_state", suffix.lstrip(".") or "state", "exclude_by_default", "system_or_log_path"))
            else:
                classes.append(self._file_class(path, "other", suffix.lstrip(".") or "unknown", "review", "unclassified_extension"))
        return sorted(classes, key=lambda item: (item.group, item.class_name, item.relative_path))

    def _file_class(
        self,
        path: Path,
        class_name: str,
        fmt: str,
        recommendation: str,
        reason: str,
    ) -> ExternalFileClass:
        relative_path = _external_rel(path, self.external_root)
        return ExternalFileClass(
            path=str(path),
            relative_path=relative_path,
            group=_group_for(relative_path),
            class_name=class_name,
            format=fmt,
            size_bytes=_path_size(path),
            recommendation=recommendation,
            reason=reason,
        )

    def _groups(self, classes: list[ExternalFileClass]) -> list[ExternalGroup]:
        grouped: dict[str, list[ExternalFileClass]] = defaultdict(list)
        for item in classes:
            grouped[item.group].append(item)
        groups = []
        for name, items in grouped.items():
            docs = [item.path for item in items if item.class_name == "document"]
            datasets = [item.path for item in items if item.class_name == "dataset"]
            deltas = [item.path for item in items if item.class_name == "delta_table"]
            dbs = [item.path for item in items if item.class_name == "database"]
            logs = [item for item in items if item.class_name == "log_or_state"]
            others = [item for item in items if item.class_name == "other"]
            strategy, recommendation = _strategy_for(items)
            groups.append(
                ExternalGroup(
                    group=name,
                    dataset_count=len(datasets),
                    document_count=len(docs),
                    delta_table_count=len(deltas),
                    database_count=len(dbs),
                    log_count=len(logs),
                    other_count=len(others),
                    total_size_bytes=sum(item.size_bytes for item in items),
                    strategy=strategy,
                    recommendation=recommendation,
                    related_documents=docs[:20],
                    candidate_datasets=datasets[:50],
                    candidate_delta_tables=deltas[:20],
                    candidate_databases=dbs[:10],
                )
            )
        return sorted(groups, key=lambda group: (-group.dataset_count - group.delta_table_count, group.group))

    def _draft_source_selection(self, groups: list[ExternalGroup]) -> dict[str, Any]:
        sources = []
        for group in groups:
            if group.strategy in {"exclude_system_state", "review_only"}:
                continue
            for doc in group.related_documents[:12]:
                sources.append(
                    {
                        "id": _safe_id(f"{group.group}_doc_{Path(doc).stem}"),
                        "type": "local",
                        "approval": "needs_approval",
                        "path": doc,
                        "mode": "register",
                        "target_kind": "doc",
                        "target_name": _safe_id(Path(doc).stem),
                        "discovery_group": group.group,
                        "recommendation": "Use as dictionary/methodology/spec context before profiling related data.",
                    }
                )
            for dataset in group.candidate_datasets[:30]:
                sources.append(
                    {
                        "id": _safe_id(f"{group.group}_{Path(dataset).stem}"),
                        "type": "local",
                        "approval": "needs_approval",
                        "path": dataset,
                        "mode": "register",
                        "target_kind": "dataset",
                        "target_name": _safe_id(Path(dataset).stem),
                        "discovery_group": group.group,
                        "recommended_pipeline": group.strategy,
                        "recommendation": group.recommendation,
                    }
                )
            for delta in group.candidate_delta_tables[:10]:
                sources.append(
                    {
                        "id": _safe_id(f"{group.group}_delta_{Path(delta).name}"),
                        "type": "local",
                        "approval": "needs_approval",
                        "path": delta,
                        "mode": "register",
                        "target_kind": "dataset",
                        "target_name": _safe_id(Path(delta).name),
                        "discovery_group": group.group,
                        "recommended_pipeline": "delta_external_table_inspection",
                        "recommendation": "Register external Delta table, inspect metadata/schema, then decide bronze or silver placement.",
                    }
                )
        return {
            "artifact_type": "source_selection.generated.json",
            "version": 1,
            "generated_by": "discover-external-sources",
            # The catalog id finalize-selection infers when none is passed: the
            # workspace folder name. Keeps the draft -> finalize handoff coherent.
            "source_catalog_id": self.workspace.name,
            "external_root": str(self.external_root),
            "approval_policy": "review_required_before_finalize",
            "sources": sources,
        }

    def _validate(self) -> None:
        if not self.external_root.exists() or not self.external_root.is_dir():
            raise FileNotFoundError(f"external root not found: {self.external_root}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")
        # T8: enforce the external-root allowlist. Without this, ANY absolute host
        # path (C:/Windows, /etc, another user's home, a drive root) could be
        # enumerated and its full file tree written into workspace artifacts. The
        # root must be inside the repo or a configured external_data_root.
        # Ref: core-audit ob-sources.md.
        policy = load_external_data_policy(self.repo_root)
        if not is_within_allowed_roots(self.external_root, self.repo_root, policy):
            raise PermissionError(
                f"external root is not allow-listed: {self.external_root}. "
                "Add it to config/external_data_roots.local.json (or set the "
                "profile's local_root_env) before discovery."
            )


def _strategy_for(items: list[ExternalFileClass]) -> tuple[str, str]:
    class_names = {item.class_name for item in items}
    formats = {item.format for item in items}
    if class_names <= {"log_or_state", "other"}:
        return "exclude_system_state", "Exclude by default; keep only as operational evidence if explicitly needed."
    if "database" in class_names and not ({"dataset", "delta_table"} & class_names):
        return "database_metadata_first", "Inspect table metadata and export selected tables only after approval."
    if "delta_table" in class_names:
        return "delta_external_table_inspection", "Register external Delta tables, inspect metadata/schema, then decide bronze or silver placement."
    if "dataset" in class_names and "csv" in formats:
        if "document" in class_names:
            return "medallion_from_raw_csv_with_docs", "Use docs as dictionaries/methodology; register CSVs as bronze candidates, profile, then design silver/gold."
        return "medallion_from_raw_csv_needs_docs", "Register CSVs as bronze candidates, profile schemas, and request dictionaries/methodology before silver/gold."
    if "dataset" in class_names:
        return "metadata_first_dataset_profile", "Register data files, profile schemas, then choose ETL/ELT or medallion path from evidence."
    if "document" in class_names:
        return "requirements_context_only", "Use documents/specs as requirements/context; do not treat as data sources."
    return "review_only", "Review manually before ingestion."


def _delta_roots(files: list[Path], external_root: Path) -> set[Path]:
    roots = set()
    for path in files:
        parts = _relative_parts(path, external_root)
        if "_delta_log" in parts:
            idx = parts.index("_delta_log")
            roots.add((external_root.joinpath(*parts[:idx])).resolve())
    return roots


def _group_for(relative_path: str) -> str:
    parts = Path(relative_path).parts
    if not parts:
        return "root"
    if parts[0].lower() in {"raw", "warehouse", "specs", "docs", "system"} and len(parts) > 1:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def _format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in {".parquet", ".pq"}:
        return "parquet"
    if suffix == ".json":
        return "json"
    if suffix in {".jsonl", ".ndjson"}:
        return "ndjson"
    return suffix.lstrip(".") or "unknown"


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        return path.resolve().relative_to(root.resolve()).parts
    except ValueError:
        return path.parts


def _external_rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_").lower()
    return safe[:100] or "source"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# External Source Discovery",
        "",
        f"- Workspace: `{payload.get('workspace', '')}`",
        f"- External root: `{payload.get('external_root', '')}`",
        f"- Files classified: `{payload.get('summary', {}).get('file_count', 0)}`",
        f"- Groups: `{payload.get('summary', {}).get('group_count', 0)}`",
        f"- Candidate sources: `{payload.get('summary', {}).get('candidate_source_count', 0)}`",
        f"- Truncated: `{payload.get('summary', {}).get('truncated', False)}`",
        "",
        "## Groups",
        "",
    ]
    for group in payload.get("groups", []):
        lines.extend(
            [
                f"### {group.get('group')}",
                "",
                f"- Strategy: `{group.get('strategy')}`",
                f"- Datasets: `{group.get('dataset_count')}`",
                f"- Documents: `{group.get('document_count')}`",
                f"- Delta tables: `{group.get('delta_table_count')}`",
                f"- Databases: `{group.get('database_count')}`",
                f"- Logs/state: `{group.get('log_count')}`",
                f"- Recommendation: {group.get('recommendation')}",
                "",
            ]
        )
        for doc in group.get("related_documents", [])[:5]:
            lines.append(f"- Doc: `{doc}`")
        for dataset in group.get("candidate_datasets", [])[:8]:
            lines.append(f"- Data: `{dataset}`")
        for delta in group.get("candidate_delta_tables", [])[:5]:
            lines.append(f"- Delta: `{delta}`")
        lines.append("")
    if payload.get("warnings"):
        lines.extend(["## Warnings", ""])
        for warning in payload["warnings"]:
            lines.append(f"- `{warning}`")
    return "\n".join(lines).rstrip() + "\n"



@anchored("discover-external-sources")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover and classify an external data root.")
    parser.add_argument("--workspace", required=True, help="Repo workspace for generated artifacts.")
    parser.add_argument("--external-root", required=True, help="External folder to classify.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--max-files", type=int, default=2000)
    parser.add_argument("--max-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    result = ExternalSourceDiscoverer(
        args.repo_root,
        args.workspace,
        args.external_root,
        max_files=args.max_files,
        max_seconds=args.max_seconds,
    ).run()
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
