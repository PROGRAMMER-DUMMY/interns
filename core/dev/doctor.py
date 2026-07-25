"""doctor: one command that answers "is my local setup actually ready to use this
platform right now" -- Python/uv, PySpark's Java requirement, Databricks/dbt/Airflow,
and git hygiene, all in one pass.

Read-only, no side effects. Adds no new checking logic beyond Python/uv/Java --
Databricks/dbt/Airflow reuse core.platform_readiness.check(), git hygiene reuses
tools.git_hygiene, exactly the same checks those commands already run on their own.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT

_MIN_PYTHON = (3, 10)
_MAX_SUPPORTED_JAVA = 17  # Spark 3.5 supports Java 8/11/17 only (see pyspark_generator.py)


def _check_python() -> dict[str, Any]:
    v = sys.version_info
    if (v.major, v.minor) < _MIN_PYTHON:
        return {
            "status": "blocked",
            "detail": f"Python {v.major}.{v.minor} found, need >= {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}",
        }
    return {"status": "ready", "detail": f"Python {v.major}.{v.minor}.{v.micro}"}


def _check_uv_venv() -> dict[str, Any]:
    uv_path = shutil.which("uv")
    if uv_path is None:
        return {"status": "blocked", "detail": "uv not on PATH -- install: https://docs.astral.sh/uv/"}
    if not (PROJECT_ROOT / ".venv").exists():
        return {"status": "partial", "detail": "uv found, but no .venv/ at repo root yet -- run `uv sync`"}
    return {"status": "ready", "detail": f"uv at {uv_path}, .venv/ present"}


def _check_java() -> dict[str, Any]:
    """Only the PySpark engine needs a JVM -- SQL and Polars work with nothing here."""
    try:
        proc = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return {
            "status": "not_installed",
            "detail": "No Java on PATH -- only blocks the PySpark engine (SQL/Polars unaffected). "
            "Install JDK 17: https://adoptium.net/temurin/releases/?version=17",
        }
    except Exception as exc:  # pragma: no cover - environment dependent
        return {"status": "unknown", "detail": f"could not run `java -version`: {exc}"}
    text = proc.stderr or proc.stdout
    match = re.search(r'"(\d+)(?:\.\d+)?', text)
    if not match:
        return {"status": "unknown", "detail": f"could not parse Java version from: {text.strip()[:200]}"}
    major = int(match.group(1))
    if major > _MAX_SUPPORTED_JAVA:
        return {
            "status": "blocked",
            "detail": f"Java {major} found, but Spark 3.5 needs Java 8/11/17 -- PySpark scripts "
            "will fail to start. SQL/Polars unaffected.",
        }
    return {"status": "ready", "detail": f"Java {major} (PySpark-compatible)"}


def _check_git_hygiene() -> dict[str, Any]:
    from tools.git_hygiene import collect_all_paths, validate_paths

    paths = collect_all_paths(PROJECT_ROOT)
    issues = validate_paths(paths, repo_root=PROJECT_ROOT, max_bytes=25 * 1024 * 1024)
    if issues:
        return {
            "status": "blocked",
            "detail": f"{len(issues)} issue(s) -- run `uv run validate-git-hygiene --all` for details",
        }
    return {"status": "ready", "detail": "no unsafe/oversized files found"}


@dataclass
class DoctorReport:
    python: dict[str, Any]
    uv_venv: dict[str, Any]
    java: dict[str, Any]
    databricks: dict[str, Any]
    dbt: dict[str, Any]
    airflow: dict[str, Any]
    git_hygiene: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return asdict(self)

    def blockers(self) -> list[str]:
        return [
            f"{name}: {result['detail']}"
            for name, result in self.summary().items()
            if result["status"] == "blocked"
        ]


def check(*, workspace: str = "", enterprise_id: str = "", repo_root: Path | None = None) -> DoctorReport:
    from core.platform_readiness import check as check_platform
    from core.storage.workspace_layout import WorkspaceLayout

    root = repo_root or PROJECT_ROOT
    eid = enterprise_id
    if not eid and workspace:
        eid = WorkspaceLayout(project_root=root / workspace).enterprise_id()
    platform = check_platform(eid)

    return DoctorReport(
        python=_check_python(),
        uv_venv=_check_uv_venv(),
        java=_check_java(),
        databricks=platform.databricks,
        dbt=platform.dbt,
        airflow=platform.airflow,
        git_hygiene=_check_git_hygiene(),
    )


_MARKS = {"ready": "[ok]", "blocked": "[x]"}


@anchored("doctor")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="doctor: one read-only pass over Python/uv/Java, Databricks/dbt/Airflow, "
        "and git hygiene -- everything else in this repo checks one piece; this checks all of them."
    )
    parser.add_argument("--workspace", default="", help="Resolve enterprise_id from this workspace, if known.")
    parser.add_argument("--enterprise-id", default="", help="Explicit override for --workspace's resolution.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = check(workspace=args.workspace, enterprise_id=args.enterprise_id)
    if args.json:
        print(json.dumps(report.summary(), indent=2))
        return 1 if report.blockers() else 0

    for name, result in report.summary().items():
        mark = _MARKS.get(result["status"], "[~]")
        print(f"{mark} {name}: {result['status']} -- {result['detail']}")

    blockers = report.blockers()
    if blockers:
        print(f"\n[x] {len(blockers)} blocker(s) -- fix these before relying on the affected path(s).")
    else:
        print("\n[ok] no blockers -- any [~] items above are optional paths you're just not using yet.")
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
