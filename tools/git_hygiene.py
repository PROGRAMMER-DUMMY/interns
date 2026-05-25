"""Validate staged or working-tree files before committing project artifacts."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[1]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from core.paths import PROJECT_ROOT  # noqa: E402

DEFAULT_MAX_BYTES = 25 * 1024 * 1024

RAW_DATA_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".parquet",
    ".pq",
    ".pdf",
    ".xlsx",
    ".xls",
    ".duckdb",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".log",
}

PROTECTED_DIR_PARTS = (
    ("state",),
    ("core", "agents", "state"),
)


@dataclass(frozen=True)
class HygieneIssue:
    path: str
    rule: str
    detail: str
    fix: str


def normalize_repo_path(path: str | Path) -> str:
    return str(path).replace("\\", "/").lstrip("./")


def path_parts(path: str | Path) -> tuple[str, ...]:
    normalized = normalize_repo_path(path)
    return tuple(part for part in normalized.split("/") if part)


def is_under_parts(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(parts) >= len(prefix) and parts[: len(prefix)] == prefix


def is_workspace_generated_path(path: str | Path) -> bool:
    parts = path_parts(path)
    return len(parts) >= 3 and parts[0] == "workspaces" and "interns" in parts[2:]


def classify_path(path: str | Path, *, repo_root: Path, max_bytes: int) -> list[HygieneIssue]:
    rel = normalize_repo_path(path)
    parts = path_parts(rel)
    issues: list[HygieneIssue] = []

    if is_workspace_generated_path(rel):
        issues.append(
            HygieneIssue(
                rel,
                "protected_workspace_generated_output",
                "workspace interns/generated/state output must stay out of Git",
                "unstage this path and regenerate it locally when needed",
            )
        )

    for protected in PROTECTED_DIR_PARTS:
        if is_under_parts(parts, protected):
            issues.append(
                HygieneIssue(
                    rel,
                    "protected_state_directory",
                    f"path is under {'/'.join(protected)}/",
                    "unstage this local runtime/state artifact",
                )
            )

    suffix = Path(rel).suffix.lower()
    if suffix in RAW_DATA_EXTENSIONS:
        issues.append(
            HygieneIssue(
                rel,
                "raw_or_large_data_extension",
                f"{suffix} files are treated as raw data, logs, or local databases",
                "move the file to external storage or keep it ignored",
            )
        )

    full_path = repo_root / rel
    if full_path.exists() and full_path.is_file():
        size = full_path.stat().st_size
        if size > max_bytes:
            issues.append(
                HygieneIssue(
                    rel,
                    "file_size_limit",
                    f"{size} bytes exceeds the {max_bytes} byte limit",
                    "keep large artifacts outside Git or document a separate storage path",
                )
            )

    return issues


def collect_staged_paths(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def collect_all_paths(repo_root: Path) -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    seen: set[str] = set()
    paths: list[str] = []
    for line in [*tracked.stdout.splitlines(), *untracked.stdout.splitlines()]:
        path = line.strip()
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def validate_paths(paths: Iterable[str], *, repo_root: Path, max_bytes: int) -> list[HygieneIssue]:
    issues: list[HygieneIssue] = []
    for path in paths:
        issues.extend(classify_path(path, repo_root=repo_root, max_bytes=max_bytes))
    return issues


def render_issues(issues: list[HygieneIssue]) -> str:
    if not issues:
        return "Git hygiene OK: no unsafe files found."
    lines = ["Git hygiene blockers:"]
    for issue in issues:
        lines.append(f"- {issue.path}")
        lines.append(f"  rule: {issue.rule}")
        lines.append(f"  detail: {issue.detail}")
        lines.append(f"  fix: {issue.fix}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate staged files for repo hygiene.")
    parser.add_argument("--all", action="store_true", help="audit tracked and untracked nonignored files")
    parser.add_argument(
        "--max-mb",
        type=float,
        default=float(os.environ.get("AUTORESEARCH_GIT_HYGIENE_MAX_MB", "25")),
        help="maximum allowed file size in MB before blocking",
    )
    args = parser.parse_args(argv)

    repo_root = PROJECT_ROOT
    max_bytes = int(args.max_mb * 1024 * 1024)
    paths = collect_all_paths(repo_root) if args.all else collect_staged_paths(repo_root)
    issues = validate_paths(paths, repo_root=repo_root, max_bytes=max_bytes)
    print(render_issues(issues))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
