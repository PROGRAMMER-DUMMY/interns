from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class PipelineSQLResult:
    path: str
    status: str

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class PipelineSQLGenerator:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def generate(self) -> PipelineSQLResult:
        plan = _load_json(self.layout.contracts_dir / "pipeline_plan.json")
        if plan.get("blockers"):
            raise ValueError("pipeline plan has blockers")
        catalog = _load_json(self.layout.contracts_dir / "catalog_contract.json")
        table_format = plan.get("table_format", "local_parquet")

        # Raw dataset paths are allowed ONLY inside the catalog bootstrap block.
        # Every downstream medallion layer references the bootstrap view, never a
        # raw path, so the business SQL stays portable across engines/storage.
        bootstrap_lines: list[str] = ["-- BEGIN CATALOG BOOTSTRAP"]
        layer_lines: list[str] = []
        for obj in catalog.get("objects", []):
            source = _object_source(obj)
            stem = _object_stem(obj)
            if not stem:
                continue
            raw_view = f"catalog_raw_{stem}"
            reader = _reader_for(source, table_format)
            bootstrap_lines.append(
                f'CREATE OR REPLACE VIEW "{raw_view}" AS '
                f"SELECT * FROM {reader}('{source}');"
            )
            layer_lines.extend(
                [
                    f'CREATE OR REPLACE VIEW "bronze_{stem}" AS '
                    f'SELECT * FROM "{raw_view}";',
                    # P5 (T9-adjacent): plain SELECT * — NOT SELECT DISTINCT.
                    # Deduplication is approval_gated (pipeline_plan silver policy
                    # + bronze/silver standards list dedup as a forbidden,
                    # approval-required transform). The generator must not dedup
                    # silently; dedup belongs behind duplicate_decisions approval.
                    # Ref: core-audit onboarding-root.md.
                    f'CREATE OR REPLACE VIEW "silver_{stem}" AS '
                    f'SELECT * FROM "bronze_{stem}";',
                    f'CREATE OR REPLACE VIEW "gold_{stem}" AS '
                    f'SELECT * FROM "silver_{stem}";',
                ]
            )
        bootstrap_lines.append("-- END CATALOG BOOTSTRAP")

        lines = ["-- Generated pipeline layer SQL", ""]
        lines.extend(bootstrap_lines)
        lines.append("")
        lines.extend(layer_lines)
        out = self.layout.generated_dir / "pipeline" / "pipeline_layers.sql"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return PipelineSQLResult(_rel(out, self.repo_root), "generated")


def _object_source(obj: dict[str, Any]) -> str:
    """Raw dataset path for an object, tolerant of contract shape drift."""
    return str(obj.get("source_path") or obj.get("dataset") or "")


def _object_stem(obj: dict[str, Any]) -> str:
    """Stable, filesystem-safe stem used to name layer views.

    Prefer the catalog ``name``; fall back to the dataset filename stem so the
    generator works on minimal contracts that only carry ``dataset``.
    """
    name = obj.get("name")
    if not name:
        source = _object_source(obj)
        if source:
            name = Path(source.replace("\\", "/")).stem
    return _safe_name(str(name or ""))


def _reader_for(source: str, table_format: str) -> str:
    """Pick the DuckDB reader. File extension wins; otherwise table format."""
    lowered = str(source).lower()
    if lowered.endswith(".parquet"):
        return "read_parquet"
    if lowered.endswith(".csv"):
        return "read_csv_auto"
    return "read_parquet" if table_format in {"local_parquet", "parquet"} else "read_csv_auto"


def _safe_name(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_") or "source"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)
    print(json.dumps(PipelineSQLGenerator(args.repo_root, args.workspace).generate().summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
