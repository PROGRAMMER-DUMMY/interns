from core.observability.cost_ledger import anchored
import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.external_data import (
    bounded_external_files,
    is_external_path,
    load_external_data_policy,
)


KPI_HINTS = ("kpi", "metric", "registry", "analytics_question", "analytics_questions", "question", "questions")
DATA_MODEL_HINTS = (
    "data_model",
    "datamodel",
    "model",
    "schema",
    "dictionary",
    "data_dictionary",
    "create_db",
    "create_database",
    "create_hospital_db",
)
DATA_EXTENSIONS = {".csv", ".parquet", ".json", ".jsonl", ".xlsx", ".xls"}

# Directories the platform WRITES inside a workspace. Their contents are
# emissions, not inputs -- reading them back as source data made WS-BUG-001 fire
# at critical on every cloud-native workspace once ingestion had been generated,
# hard-blocking the KPI path. (F18)
#
# `interns/` is already filtered upstream; these postdate that rule. Kept as a
# local copy rather than importing `sync_code.CODE_DIRS` because the selection
# path must stay import-cheap -- `tests/regressions/
# test_listing_ignores_platform_output.py` pins the two together against drift.
PLATFORM_OUTPUT_DIRS = ("ingestion", "dbt", "context", ".databricks")
DOC_EXTENSIONS = {
    ".csv",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".sql",
    ".toml",
    ".txt",
    ".xls",
    ".xlsx",
    ".yaml",
    ".yml",
}


@dataclass
class WorkspaceFileListing:
    workspace: str
    exists: bool
    file_count: int
    truncated: bool
    interns_state: str
    possible_kpi_files: list[str]
    possible_data_model_files: list[str]
    dataset_roots: list[str]
    docs: list[str]
    files: list[str]
    external_state: str = "workspace"
    classifications: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def list_workspace_files(
    repo_root: Path | str,
    workspace: str,
    *,
    max_files: int = 200,
) -> WorkspaceFileListing:
    root = Path(repo_root).resolve()
    policy = load_external_data_policy(root)
    requested_path = Path(workspace.strip().strip('"').strip("'")).expanduser()
    if requested_path.is_absolute() and is_external_path(requested_path, root, policy):
        return _list_external_files(root, requested_path.resolve(), policy, max_files=max_files)

    workspace_rel = _resolve_workspace(root, workspace)
    workspace_path = (root / workspace_rel).resolve()
    if not workspace_path.exists() or not workspace_path.is_dir():
        return WorkspaceFileListing(
            workspace=workspace_rel,
            exists=False,
            file_count=0,
            truncated=False,
            interns_state="missing_workspace",
            possible_kpi_files=[],
            possible_data_model_files=[],
            dataset_roots=[],
            docs=[],
            files=[],
        )

    files: list[Path] = []
    truncated = False
    for path in workspace_path.rglob("*"):
        if path.is_file():
            if len(files) >= max_files:
                truncated = True
                break
            files.append(path)

    rel_files = [_display_path(root, path) for path in files]
    classifications = _classify_files(rel_files)
    return WorkspaceFileListing(
        workspace=workspace_rel,
        exists=True,
        file_count=len(rel_files),
        truncated=truncated,
        interns_state=_interns_state(workspace_path, rel_files),
        possible_kpi_files=_possible_kpi_files(rel_files),
        possible_data_model_files=_possible_data_model_files(rel_files),
        dataset_roots=_dataset_roots(rel_files),
        docs=_docs(rel_files),
        files=rel_files,
        classifications=classifications,
    )


def _list_external_files(
    repo_root: Path,
    external_root: Path,
    policy,
    *,
    max_files: int,
) -> WorkspaceFileListing:
    if not external_root.exists() or not external_root.is_dir():
        return WorkspaceFileListing(
            workspace=external_root.as_posix(),
            exists=False,
            file_count=0,
            truncated=False,
            interns_state="external_missing",
            possible_kpi_files=[],
            possible_data_model_files=[],
            dataset_roots=[],
            docs=[],
            files=[],
            external_state="external_danger_root_missing",
        )
    cap = min(max_files, policy.max_paths)
    files, truncated = bounded_external_files(
        external_root,
        max_paths=cap,
        max_seconds=policy.max_seconds,
    )
    rel_files = [_display_path(repo_root, path) for path in files]
    classifications = _classify_files(rel_files)
    return WorkspaceFileListing(
        workspace=external_root.as_posix(),
        exists=True,
        file_count=len(rel_files),
        truncated=truncated,
        interns_state="external_bounded_listing_only",
        possible_kpi_files=_possible_kpi_files(rel_files),
        possible_data_model_files=_possible_data_model_files(rel_files),
        dataset_roots=_dataset_roots(rel_files),
        docs=_docs(rel_files),
        files=rel_files,
        external_state="external_danger_root_bounded_listing_only",
        classifications=classifications,
    )


def _resolve_workspace(repo_root: Path, requested: str) -> str:
    normalized = requested.replace("\\", "/").strip().strip('"').strip("'").rstrip("/")
    if normalized.startswith("workspaces/"):
        return normalized

    workspaces_root = repo_root / "workspaces"
    if not workspaces_root.exists():
        return f"workspaces/{normalized}"

    candidates = [path for path in workspaces_root.iterdir() if path.is_dir()]
    for path in candidates:
        if path.name.lower() == normalized.lower():
            return path.relative_to(repo_root).as_posix()

    requested_tokens = _tokens(normalized)
    scored: list[tuple[int, str]] = []
    for path in candidates:
        score = len(requested_tokens.intersection(_tokens(path.name)))
        if score:
            scored.append((score, path.relative_to(repo_root).as_posix()))
    if scored:
        scored.sort(key=lambda item: (-item[0], item[1]))
        return scored[0][1]

    return f"workspaces/{normalized}"


def _interns_state(workspace_path: Path, rel_files: list[str]) -> str:
    if not (workspace_path / "interns").exists():
        return "missing"
    if any("/interns/" in file for file in rel_files):
        return "present_with_files"
    return "present_empty"


def _possible_kpi_files(files: list[str]) -> list[str]:
    return sorted(
        file
        for file in files
        if "/interns/" not in file
        and Path(file).suffix.lower() in {".xlsx", ".xls", ".csv", ".json", ".md", ".sql", ".toml", ".txt", ".yaml", ".yml"}
        and _has_kpi_filename_signal(file)
    )


def _possible_data_model_files(files: list[str]) -> list[str]:
    return sorted(
        file
        for file in files
        if "/interns/" not in file
        and Path(file).suffix.lower() in DOC_EXTENSIONS
        and _has_data_model_filename_signal(file)
    )


def _classify_files(files: list[str]) -> list[dict[str, Any]]:
    classifications = []
    for file in sorted(files):
        if "/interns/" in file:
            continue
        roles, reasons = _file_roles_and_reasons(file)
        if not roles:
            continue
        classifications.append(
            {
                "path": file,
                "roles": roles,
                "reasons": reasons,
                "conflicts": [],
            }
        )
    return classifications


def _is_platform_output(file: str) -> bool:
    """True when the path sits inside a directory this platform generates.

    Matched on whole path SEGMENTS, so a user dataset merely NAMED like one of
    them (`datasets/context.csv`) still registers as real input evidence."""
    return any(part in PLATFORM_OUTPUT_DIRS for part in Path(file).parts[:-1])


def _file_roles_and_reasons(file: str) -> tuple[list[str], list[str]]:
    if Path(file).name == "workspace_settings.json":
        return [], []
    roles: list[str] = []
    reasons: list[str] = []
    suffix = Path(file).suffix.lower()
    normalized = _normalized_name(file)

    if suffix in {".xlsx", ".xls", ".csv", ".json", ".md", ".sql", ".toml", ".txt", ".yaml", ".yml"} and _has_kpi_filename_signal(file):
        roles.append("kpi_input")
        reasons.append(_kpi_reason(normalized, suffix))

    if suffix in DOC_EXTENSIONS and _has_data_model_filename_signal(file):
        roles.append("data_model_input")
        reasons.append(_data_model_reason(normalized, suffix))

    if suffix in DATA_EXTENSIONS and "/docs/" not in file and not _is_platform_output(file):
        roles.append("dataset_evidence")
        reasons.append(f"{suffix} file outside docs/interns")

    if suffix in DOC_EXTENSIONS and ("/docs/" in file or _looks_like_root_context_file(file)):
        roles.append("context_doc")
        reasons.append("documentation/context filename signal")

    return roles, reasons


def _kpi_reason(normalized: str, suffix: str) -> str:
    matched = [hint for hint in KPI_HINTS if hint in normalized]
    if matched:
        return f"filename contains {matched[0]}"
    if "analytics" in normalized and suffix == ".sql":
        return "SQL filename contains analytics"
    return "filename matches KPI input pattern"


def _data_model_reason(normalized: str, suffix: str) -> str:
    matched = [hint for hint in DATA_MODEL_HINTS if hint in normalized]
    if matched:
        return f"filename contains {matched[0]}"
    if suffix == ".sql" and normalized.startswith("create_"):
        return "SQL filename starts with create"
    if suffix == ".sql" and "ddl" in normalized:
        return "SQL filename contains ddl"
    if suffix == ".sql" and "database" in normalized:
        return "SQL filename contains database"
    if suffix == ".sql" and normalized.endswith("_db"):
        return "SQL filename ends with db"
    return "filename matches data model pattern"


def _has_kpi_filename_signal(file: str) -> bool:
    normalized = _normalized_name(file)
    return any(hint in normalized for hint in KPI_HINTS) or (
        "analytics" in normalized and "sql" == Path(file).suffix.lower().lstrip(".")
    )


def _has_data_model_filename_signal(file: str) -> bool:
    normalized = _normalized_name(file)
    suffix = Path(file).suffix.lower()
    if any(hint in normalized for hint in DATA_MODEL_HINTS):
        return True
    return suffix == ".sql" and (
        normalized.startswith("create_")
        or "ddl" in normalized
        or "database" in normalized
        or normalized.endswith("_db")
    )


def _normalized_name(file: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", Path(file).stem.lower()).strip("_")


def _dataset_roots(files: list[str]) -> list[str]:
    roots = set()
    for file in files:
        if "/interns/" in file:
            continue
        if Path(file).name == "workspace_settings.json":
            continue
        if Path(file).suffix.lower() not in DATA_EXTENSIONS:
            continue
        if "/datasets/" not in file:
            parent = str(Path(file).parent).replace("\\", "/")
            if "/docs" not in parent:
                roots.add(parent)
            continue
        parts = Path(file).parts
        try:
            idx = parts.index("datasets")
        except ValueError:
            roots.add(str(Path(file).parent).replace("\\", "/"))
            continue
        root_parts = parts[: idx + 2] if len(parts) > idx + 2 else parts[: idx + 1]
        roots.add("/".join(root_parts))
    return sorted(roots)


def _docs(files: list[str]) -> list[str]:
    return sorted(
        file
        for file in files
        if "/interns/" not in file
        and ("/docs/" in file or _looks_like_root_context_file(file))
        and Path(file).suffix.lower() in DOC_EXTENSIONS
    )


def _looks_like_root_context_file(file: str) -> bool:
    path = Path(file)
    name = path.name.lower()
    if len(path.parts) != 3 or path.parts[0] != "workspaces":
        return False
    return (
        "dictionary" in name
        or "question" in name
        or "analytics" in name
        or "schema" in name
        or "db" in name
        or "citation" in name
    )


def _display_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}



@anchored("list-workspace-files")
def main() -> None:
    parser = argparse.ArgumentParser(description="List workspace file paths without reading contents.")
    parser.add_argument(
        "--workspace",
        required=True,
        nargs="+",
        help="Workspace path or fuzzy workspace name. Multiple tokens are joined with spaces.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Repository root. Defaults to detected project root.",
    )
    parser.add_argument("--max-files", type=int, default=200, help="Maximum file paths to list.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    parser.add_argument(
        "--quiet", action="store_true",
        help="Omit the full 'All files' dump; print counts, dataset roots, and classified KPI/model/doc hints only.",
    )
    args = parser.parse_args()

    workspace = " ".join(args.workspace)
    listing = list_workspace_files(Path(args.repo_root), workspace, max_files=args.max_files)
    if args.json:
        print(json.dumps(listing.to_dict(), indent=2))
        return

    print(f"Workspace: {listing.workspace}")
    print(f"Exists: {listing.exists}")
    print(f"Files listed: {listing.file_count}")
    print(f"Truncated: {listing.truncated}")
    print(f"Interns state: {listing.interns_state}")
    print(f"External state: {listing.external_state}")
    _print_classified_group(
        "Possible KPI files",
        listing.possible_kpi_files,
        listing.classifications,
        "kpi_input",
    )
    _print_classified_group(
        "Possible data model files",
        listing.possible_data_model_files,
        listing.classifications,
        "data_model_input",
    )
    _print_group("Dataset roots", listing.dataset_roots)
    _print_classified_group("Docs", listing.docs, listing.classifications, "context_doc")
    if args.quiet:
        print(f"\nAll files: {listing.file_count} (omitted in --quiet; re-run without --quiet or with --json for the full list)")
    else:
        _print_group("All files", listing.files)


def _print_group(title: str, values: list[str]) -> None:
    print(f"\n{title}:")
    if not values:
        print("- (none)")
        return
    for value in values:
        print(f"- {value}")


def _print_classified_group(
    title: str,
    values: list[str],
    classifications: list[dict[str, Any]],
    role: str,
) -> None:
    by_path = {item["path"]: item for item in classifications}
    print(f"\n{title}:")
    if not values:
        print("- (none)")
        return
    for value in values:
        reasons = [
            reason
            for reason in by_path.get(value, {}).get("reasons", [])
            if _reason_matches_role(reason, role)
        ]
        reason_text = reasons[0] if reasons else _first_reason(by_path.get(value, {}))
        suffix = f" [reason: {reason_text}]" if reason_text else ""
        print(f"- {value}{suffix}")


def _reason_matches_role(reason: str, role: str) -> bool:
    if role == "kpi_input":
        return "KPI" in reason or "analytics" in reason or "question" in reason or "metric" in reason
    if role == "data_model_input":
        return (
            "data model" in reason
            or "dictionary" in reason
            or "schema" in reason
            or "create" in reason
            or "database" in reason
            or "ddl" in reason
            or "db" in reason
        )
    if role == "context_doc":
        return "documentation/context" in reason
    return False


def _first_reason(classification: dict[str, Any]) -> str:
    reasons = classification.get("reasons", [])
    if not reasons:
        return ""
    return str(reasons[0])


if __name__ == "__main__":
    main()
