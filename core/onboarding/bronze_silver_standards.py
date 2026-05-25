from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class BronzeSilverStandardsResult:
    standards_path: str
    manifest_path: str
    reroute_policy_path: str
    markdown_path: str

    def summary(self) -> dict[str, str]:
        return asdict(self)


class BronzeSilverStandardsBuilder:
    def __init__(self, repo_root: str | Path, workspace: str | Path, *, domain: str = "general") -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.domain = domain
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def build(self) -> BronzeSilverStandardsResult:
        self.layout.ensure_runtime_dirs()
        standards_path = self.layout.contracts_dir / "bronze_silver_standards.json"
        manifest_path = self.layout.contracts_dir / "transformation_manifest.json"
        reroute_path = self.layout.contracts_dir / "workflow_reroute_policy.json"
        markdown_path = self.layout.reports_dir / "bronze_silver_standards.md"

        standards = {
            "artifact_type": "bronze_silver_standards.json",
            "version": 1,
            "domain": self.domain,
            "generated_at": _now(),
            "baseline": {
                "bronze": {
                    "purpose": "Immutable landing layer preserving source fidelity.",
                    "allowed_transformations": [
                        "ingestion_metadata",
                        "source_file_provenance",
                        "raw_schema_capture",
                    ],
                    "forbidden_transformations": [
                        "deduplication_application",
                        "business_filtering",
                        "semantic_conformance",
                        "kpi_aggregation",
                    ],
                },
                "silver": {
                    "purpose": "Cleaned and conformed reusable entities/events.",
                    "allowed_transformations": [
                        "type_normalization",
                        "timestamp_normalization",
                        "null_handling",
                        "column_naming",
                        "schema_conformance",
                        "approved_reference_data_join",
                        "canonical_id_mapping",
                    ],
                    "forbidden_transformations": [
                        "kpi_specific_filters",
                        "metric_formula_application",
                        "business_aggregation",
                    ],
                },
                "gold": {
                    "purpose": "Business-facing KPI, mart, and serving outputs.",
                    "allowed_transformations": [
                        "metric_formula_application",
                        "business_aggregation",
                        "consumer_specific_shape",
                    ],
                },
            },
        }
        manifest = {
            "artifact_type": "transformation_manifest.json",
            "version": 1,
            "engine_parity_policy": {
                "target_engines": ["sql", "polars", "pyspark"],
                "block_on_unsupported_rule": True,
            },
            "layers": standards["baseline"],
        }
        reroute = {
            "artifact_type": "workflow_reroute_policy.json",
            "version": 1,
            "max_auto_reroutes_per_rule_per_session": 1,
            "rules": [
                {
                    "rule_id": "skip_data_quality_before_kpi_or_generation",
                    "replacement_command": f"uv run run-data-quality-harness --workspace {_rel(self.workspace, self.repo_root)}",
                },
                {
                    "rule_id": "code_generation_before_contracts_and_harnesses",
                    "replacement_command": f"uv run run-layered-pipeline-harness --workspace {_rel(self.workspace, self.repo_root)}",
                },
            ],
        }

        standards_path.write_text(json.dumps(standards, indent=2) + "\n", encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        reroute_path.write_text(json.dumps(reroute, indent=2) + "\n", encoding="utf-8")
        markdown_path.write_text(
            "# Bronze/Silver Standards\n\n"
            "- Bronze preserves source fidelity.\n"
            "- Silver performs technical normalization plus approved semantic conformance.\n"
            "- Gold owns KPI-specific formulas and aggregations.\n",
            encoding="utf-8",
        )

        return BronzeSilverStandardsResult(
            standards_path=_rel(standards_path, self.repo_root),
            manifest_path=_rel(manifest_path, self.repo_root),
            reroute_policy_path=_rel(reroute_path, self.repo_root),
            markdown_path=_rel(markdown_path, self.repo_root),
        )


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--domain", default="general")
    args = parser.parse_args(argv)
    result = BronzeSilverStandardsBuilder(args.repo_root, args.workspace, domain=args.domain).build()
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
