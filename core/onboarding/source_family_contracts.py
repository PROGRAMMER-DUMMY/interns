from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class SourceFamilyContractResult:
    json_path: str
    markdown_path: str
    family_count: int
    profile_count: int
    schema_version_count: int

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class SourceFamilyContractBuilder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def build(self) -> SourceFamilyContractResult:
        self.layout.ensure_runtime_dirs()
        profiles = _load_json(self.layout.profiles_dir / "profile_index.json").get("profiles", [])
        source_selection = _load_json(self.workspace / "docs" / "source_selection.json")
        group_by_path = {
            str(source.get("path") or ""): str(source.get("discovery_group") or "")
            for source in source_selection.get("sources", [])
            if isinstance(source, dict)
        }
        docs_by_group: dict[str, list[dict[str, Any]]] = {}
        for source in source_selection.get("sources", []):
            if not isinstance(source, dict) or source.get("target_kind") != "doc":
                continue
            group = str(source.get("discovery_group") or "")
            docs_by_group.setdefault(group, []).append(
                {
                    "path": source.get("path"),
                    "target_name": source.get("target_name") or source.get("id"),
                }
            )
        groups: dict[str, list[dict[str, Any]]] = {}
        for profile in profiles:
            path = str(profile.get("path") or "")
            group = group_by_path.get(path) or _family_name(path)
            groups.setdefault(group, []).append(profile)
        families = []
        for name, items in groups.items():
            schemas = [set((item.get("schema") or {}).keys()) for item in items]
            schema_signatures = {
                json.dumps(item.get("schema") or {}, sort_keys=True)
                for item in items
            }
            common = set.intersection(*schemas) if schemas else set()
            union = set.union(*schemas) if schemas else set()
            types_by_column: dict[str, set[str]] = {}
            for item in items:
                for column, dtype in (item.get("schema") or {}).items():
                    types_by_column.setdefault(column, set()).add(str(dtype))
            type_drift = {
                column: sorted(types)
                for column, types in types_by_column.items()
                if len(types) > 1
            }
            families.append(
                {
                    "family": _safe_family_name(name),
                    "source_group": name,
                    "source_count": len(items),
                    "sources": [str(item.get("path") or "") for item in items],
                    "common_columns": sorted(common),
                    "schema_drift_columns": sorted(union - common),
                    "schema_drift": {
                        "has_schema_drift": len(schema_signatures) > 1,
                        "has_type_drift": bool(type_drift),
                        "type_drift": type_drift,
                    },
                    "observed_release_tokens": {
                        "data_year": sorted(
                            {
                                year
                                for item in items
                                for year in [_extract_data_year(str(item.get("path") or ""))]
                                if year is not None
                            }
                        )
                    },
                    # T12: derive partition columns from columns ACTUALLY present
                    # in the family schema (year/period-like), never a hardcoded
                    # `report_year` that may not exist. Empty when no such column.
                    # Ref: SUMMARY.md T12, onboarding-root.md.
                    "bronze_plan": {"partition_columns": _partition_columns(items)},
                    "documentation": docs_by_group.get(name, []),
                    "profiles": [
                        {
                            "path": item.get("path"),
                            "schema_column_count": len(item.get("schema") or {}),
                        }
                        for item in items
                    ],
                }
            )
        schema_version_count = len(
            {
                json.dumps(profile.get("schema") or {}, sort_keys=True)
                for profile in profiles
            }
        )
        payload = {
            "artifact_type": "source_family_contracts.json",
            "summary": {"family_count": len(families)},
            "profile_payloads_duplicated": False,
            "families": families,
        }
        json_path = self.layout.contracts_dir / "source_family_contracts.json"
        md_path = self.layout.reports_dir / "source_family_contracts.md"
        json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        md_path.write_text("# Source Family Contracts\n\n" + "\n".join(f"- {f['family']}" for f in families) + "\n", encoding="utf-8")
        return SourceFamilyContractResult(
            _rel(json_path, self.repo_root),
            _rel(md_path, self.repo_root),
            len(families),
            len(profiles),
            schema_version_count,
        )


def _family_name(path: str) -> str:
    stem = Path(path.replace("\\", "/")).stem
    return re.sub(r"[_-]?(?:CY)?20\d{2}Q\d|[_-]?\d{4}[_-]?q\d", "", stem, flags=re.I) or stem


def _extract_data_year(path: str) -> int | None:
    match = re.search(r"(?:DY|CY)(\d{2,4})", path, flags=re.I)
    if not match:
        return None
    value = int(match.group(1))
    return 2000 + value if value < 100 else value


_PARTITION_COLUMN_RE = re.compile(
    r"(?i)^(report_)?year$|^.*_year$|^fiscal_year$|^data_year$|^period$|^.*_period$"
)


def _partition_columns(items: list[dict[str, Any]]) -> list[str]:
    """Year/period-like columns present in the family's schema, derived from
    evidence (never fabricated). Empty when no such column exists. T12."""
    columns: set[str] = set()
    for item in items:
        schema = item.get("schema")
        if isinstance(schema, dict):
            columns.update(str(c) for c in schema.keys())
    return sorted(c for c in columns if _PARTITION_COLUMN_RE.match(c))


def _safe_family_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower() or "source"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()



@anchored("build-source-family-contracts")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)
    print(json.dumps(SourceFamilyContractBuilder(args.repo_root, args.workspace).build().summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
