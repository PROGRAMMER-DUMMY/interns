from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class LayeredPipelineHarnessResult:
    current_json_path: str
    current_markdown_path: str
    ok: bool

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class LayeredPipelineHarness:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(self) -> LayeredPipelineHarnessResult:
        plan_path = self.layout.contracts_dir / "pipeline_plan.json"
        plan = _load_json(plan_path)
        findings = []
        if not (self.layout.contracts_dir / "catalog_contract.json").exists():
            findings.append({"code": "missing_catalog_contract"})
        if not (self.layout.contracts_dir / "data_engineering_route.json").exists():
            findings.append({"code": "missing_data_engineering_route"})
        if not plan:
            findings.append({"code": "missing_pipeline_plan"})
        if plan.get("blockers"):
            findings.append({"code": "pipeline_plan_blocked"})
            for blocker in plan.get("blockers", []):
                blocker_type = str(blocker.get("type") or "").strip()
                if blocker_type:
                    findings.append({"code": f"pipeline_blocker_{blocker_type}"})
        for layer in plan.get("layers", []):
            for obj in layer.get("objects", []):
                if layer.get("layer") == "silver" and obj.get("deduplication", {}).get("application") != "approval_gated":
                    findings.append({"code": "dedup_not_approval_gated"})
        ok = not findings
        payload = {
            "ok": ok,
            "summary": {
                "track": plan.get("selected_track", ""),
                "layers": len(plan.get("layers", [])),
            },
            "findings": findings,
        }
        out = self.layout.reports_dir / "layered_pipeline_harness"
        ev = self.layout.evidence_dir / "layered_pipeline_harness"
        out.mkdir(parents=True, exist_ok=True)
        ev.mkdir(parents=True, exist_ok=True)
        current_json = out / "current.json"
        current_md = out / "current.md"
        current_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        (ev / "current.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(f"# Layered Pipeline Harness\n\nOK: `{ok}`\n", encoding="utf-8")
        return LayeredPipelineHarnessResult(_rel(current_json, self.repo_root), _rel(current_md, self.repo_root), ok)


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
    print(json.dumps(LayeredPipelineHarness(args.repo_root, args.workspace).run().summary(), indent=2))
    return 0
