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
        lines = ["-- Generated pipeline layer SQL", ""]
        for idx, obj in enumerate(catalog.get("objects", []), start=1):
            source = obj.get("source_path", "")
            view = f"catalog_raw_{idx:03d}_{obj.get('name', 'source')}"
            reader = "read_parquet" if table_format in {"local_parquet", "parquet"} else "read_csv_auto"
            if str(source).lower().endswith(".parquet"):
                reader = "read_parquet"
            lines.append(f'CREATE OR REPLACE VIEW {view} AS SELECT * FROM {reader}(\'{source}\');')
        lines.extend(
            [
                "",
                "CREATE OR REPLACE VIEW bronze_source AS SELECT * FROM catalog_raw_001_transactions;",
                "CREATE OR REPLACE VIEW silver_source AS SELECT DISTINCT * FROM bronze_source;",
                "CREATE OR REPLACE VIEW gold_kpi AS SELECT * FROM silver_source;",
            ]
        )
        out = self.layout.generated_dir / "pipeline" / "pipeline_layers.sql"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return PipelineSQLResult(_rel(out, self.repo_root), "generated")


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
