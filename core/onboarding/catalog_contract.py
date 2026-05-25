from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class CatalogContractResult:
    json_path: str
    markdown_path: str
    object_count: int

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class CatalogContractBuilder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def build(self) -> CatalogContractResult:
        self.layout.ensure_runtime_dirs()
        profiles = _load_json(self.layout.profiles_dir / "profile_index.json").get("profiles", [])
        objects = []
        for profile in profiles:
            path = _repo_path(str(profile.get("path") or ""), self.repo_root)
            if not path:
                continue
            name = _safe_name(Path(path.replace("\\", "/")).stem)
            objects.append(
                {
                    "name": name,
                    "logical_name": f"raw.{name}",
                    "dataset": path,
                    "source_path": path,
                    "profile_path": _rel(self.layout.profiles_dir / f"{Path(path).name}.profile.json", self.repo_root),
                    "format": profile.get("format") or Path(path).suffix.lstrip(".") or "csv",
                    "schema": profile.get("schema") or {},
                    "layer": "raw",
                    "read_policy": "catalog_bootstrap_only",
                    "physical_bindings": [
                        {
                            "binding_type": "local_file",
                            "path": path,
                            "usage": "ingestion_bootstrap",
                        },
                        {
                            "binding_type": "duckdb_view",
                            "name": f"catalog_raw.{name}",
                            "usage": "business_logic_reference",
                        },
                    ],
                }
            )
        contract = {
            "artifact_type": "catalog_contract.json",
            "version": 1,
            "workspace": _rel(self.workspace, self.repo_root),
            "objects": objects,
            "object_count": len(objects),
        }
        json_path = self.layout.contracts_dir / "catalog_contract.json"
        md_path = self.layout.reports_dir / "catalog_contract.md"
        json_path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
        md_path.write_text("# Catalog Contract\n\n" + "\n".join(f"- `{o['name']}`" for o in objects) + "\n", encoding="utf-8")
        return CatalogContractResult(_rel(json_path, self.repo_root), _rel(md_path, self.repo_root), len(objects))


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _safe_name(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_") or "source"


def _repo_path(value: str, root: Path) -> str:
    if not value:
        return ""
    normalized = value.replace("\\", "/")
    marker = "workspaces/"
    if marker in normalized:
        return normalized[normalized.index(marker):]
    path = Path(value)
    if path.is_absolute():
        return _rel(path, root)
    return normalized


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)
    print(json.dumps(CatalogContractBuilder(args.repo_root, args.workspace).build().summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
