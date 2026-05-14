import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


KPI_HINTS = ("kpi", "metric", "registry")
DATA_MODEL_HINTS = ("data_model", "datamodel", "model")
DATA_EXTENSIONS = {".csv", ".parquet", ".json", ".jsonl", ".xlsx", ".xls"}
DOC_EXTENSIONS = {".md", ".txt", ".pdf", ".png", ".jpg", ".jpeg", ".xlsx", ".xls"}


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def list_workspace_files(
    repo_root: Path | str,
    workspace: str,
    *,
    max_files: int = 200,
) -> WorkspaceFileListing:
    root = Path(repo_root).resolve()
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
        and Path(file).suffix.lower() in {".xlsx", ".xls", ".csv", ".md"}
        and any(hint in Path(file).name.lower() for hint in KPI_HINTS)
    )


def _possible_data_model_files(files: list[str]) -> list[str]:
    return sorted(
        file
        for file in files
        if "/interns/" not in file
        and Path(file).suffix.lower() in DOC_EXTENSIONS
        and any(hint in Path(file).name.lower().replace(" ", "_") for hint in DATA_MODEL_HINTS)
    )


def _dataset_roots(files: list[str]) -> list[str]:
    roots = set()
    for file in files:
        if "/interns/" in file or "/datasets/" not in file:
            continue
        if Path(file).suffix.lower() not in DATA_EXTENSIONS:
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
        and "/docs/" in file
        and Path(file).suffix.lower() in DOC_EXTENSIONS
    )


def _display_path(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}


def main() -> None:
    parser = argparse.ArgumentParser(description="List workspace file paths without reading contents.")
    parser.add_argument(
        "--workspace",
        required=True,
        nargs="+",
        help="Workspace path or fuzzy workspace name. Multiple tokens are joined with spaces.",
    )
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--max-files", type=int, default=200, help="Maximum file paths to list.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
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
    _print_group("Possible KPI files", listing.possible_kpi_files)
    _print_group("Possible data model files", listing.possible_data_model_files)
    _print_group("Dataset roots", listing.dataset_roots)
    _print_group("Docs", listing.docs)
    _print_group("All files", listing.files)


def _print_group(title: str, values: list[str]) -> None:
    print(f"\n{title}:")
    if not values:
        print("- (none)")
        return
    for value in values:
        print(f"- {value}")


if __name__ == "__main__":
    main()
