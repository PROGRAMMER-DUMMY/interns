"""Validate generated workspace artifacts before agents rely on them."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.onboarding.artifact_contracts import (
    BLOCKER_QUESTION_PANEL_CONTRACT,
    DOMAIN_MODEL_CONTRACT,
    KPI_FEATURE_MAPPING_CONTRACT,
    KPI_REGISTRY_CONTRACT,
    PROFILE_INDEX_CONTRACT,
    RELATIONSHIP_CONTRACTS_CONTRACT,
    SOURCE_TO_TARGET_PLAN_CONTRACT,
    WORKSPACE_FEATURE_DEFINITIONS_CONTRACT,
)
from core.onboarding.kpi.execution_harness import sql_defines_result_view
from core.onboarding.workspace.bugs import WorkspaceBugDetector
from core.storage.workspace_layout import WorkspaceLayout


PARSER_ARTIFACT_FEATURES = {
    "a",
    "an",
    "and",
    "commercial",
    "confirm",
    "dimension",
    "dimensions",
    "for",
    "grain",
    "medicaid",
    "medicare",
    "metric",
    "of",
    "percentage",
    "share",
    "the",
    "top",
    "trend",
}


@dataclass
class ArtifactValidationResult:
    workspace: str
    checked_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "ok": self.ok,
            "checked_file_count": len(self.checked_files),
            "checked_files": self.checked_files,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": self.errors,
            "warnings": self.warnings,
        }


class WorkspaceArtifactValidator:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.result = ArtifactValidationResult(workspace=_rel(self.workspace, self.repo_root))

    def run(self) -> ArtifactValidationResult:
        self._validate_workspace()
        self._validate_input_inventory()
        self._validate_profile_index()
        self._validate_domain_model()
        self._validate_kpi_registry()
        mapping = self._validate_feature_mapping()
        self._validate_workspace_definitions()
        self._validate_relationship_contracts()
        self._validate_source_to_target_plan()
        self._validate_open_questions()
        self._validate_question_panel(mapping)
        self._validate_derived_reviews(mapping)
        self._validate_kpi_sql_solutions(mapping)
        self._validate_kpi_execution_harness()
        self._validate_medallion_manifest()
        self._validate_workspace_bugs()
        return self.result

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            self._error("workspace", f"workspace does not exist: {_rel(self.workspace, self.repo_root)}")
            return
        if not self.workspace.is_relative_to(self.repo_root) or self.workspace == self.repo_root:
            self._error("workspace", "workspace must be inside the repo root and not equal to it")
        docs = self.workspace / "docs"
        datasets = self.workspace / "datasets"
        inventory = self._input_inventory()
        if not docs.exists() and not _inventory_has_docs(inventory):
            self._warning("workspace", "docs/ was not found under the workspace")
        if not datasets.exists() and not _inventory_has_data(inventory):
            self._warning("workspace", "datasets/ was not found under the workspace")

    def _validate_workspace_bugs(self) -> None:
        report = WorkspaceBugDetector(self.repo_root, _rel(self.workspace, self.repo_root)).run()
        for bug in report.bugs:
            message = f"{bug.bug_id}: {bug.title} ({bug.severity})"
            if bug.blocks_workflow:
                self._error("workspace_bug", message)
            else:
                self._warning("workspace_bug", message)

    def _validate_input_inventory(self) -> None:
        path = self.layout.requirements_dir / "input_inventory.json"
        data = self._load_json(path, required=False)
        if not data:
            self._warning("input_inventory", "input_inventory.json is missing; run onboard-workspace")
            return
        for key in ("workspace", "data_files", "kpi_registries", "data_models"):
            if key not in data:
                self._error(path, f"input_inventory.json missing `{key}`")
        for key in ("data_files", "kpi_registries", "data_models"):
            if key in data and not isinstance(data[key], list):
                self._error(path, f"input_inventory.json `{key}` must be a list")

    def _input_inventory(self) -> dict[str, Any]:
        path = self.layout.requirements_dir / "input_inventory.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _validate_profile_index(self) -> None:
        path = self.layout.profiles_dir / "profile_index.json"
        data = self._load_json(path, required=False)
        if not data:
            self._warning("profile_index", "profile_index.json is missing; run onboard-workspace")
            return
        self._validate_artifact_contract(path, data, PROFILE_INDEX_CONTRACT)
        profiles = data.get("profiles")
        if not isinstance(profiles, list):
            self._error(path, "profile_index.json `profiles` must be a list")
            return
        for idx, profile in enumerate(profiles, start=1):
            for key in ("path", "schema", "row_count", "profile_path"):
                if key not in profile:
                    self._error(path, f"profile #{idx} missing `{key}`")
            if not isinstance(profile.get("schema", {}), dict):
                self._error(path, f"profile #{idx} `schema` must be an object")
            if "columns" in profile and not isinstance(profile["columns"], list):
                self._error(path, f"profile #{idx} `columns` must be a list")

    def _validate_domain_model(self) -> None:
        path = self.layout.contracts_dir / "domain_model.json"
        data = self._load_json(path, required=False)
        if not data:
            self._warning("domain_model", "domain_model.json is missing; run onboard-workspace")
            return
        self._validate_artifact_contract(path, data, DOMAIN_MODEL_CONTRACT)
        if not isinstance(data.get("datasets", []), list):
            self._error(path, "domain_model.json `datasets` must be a list")
        if not isinstance(data.get("data_models", []), list):
            self._error(path, "domain_model.json `data_models` must be a list")

    def _validate_kpi_registry(self) -> None:
        path = self.layout.contracts_dir / "kpi_registry.json"
        data = self._load_json(path, required=False)
        if not data:
            self._warning("kpi_registry", "kpi_registry.json is missing; run onboard-workspace")
            return
        self._validate_artifact_contract(path, data, KPI_REGISTRY_CONTRACT)
        kpis = data.get("kpis")
        if not isinstance(kpis, list):
            self._error(path, "kpi_registry.json `kpis` must be a list")
            return
        for idx, kpi in enumerate(kpis, start=1):
            name = str(kpi.get("name") or "").strip()
            if not name:
                self._error(path, f"kpi #{idx} missing non-empty `name`")
            if _is_template_kpi_row(name):
                self._error(path, f"kpi #{idx} looks like a template/header row, not a KPI")
            for key in ("description", "cuts", "metric", "refinement_required", "source", "status"):
                if key not in kpi:
                    self._error(path, f"kpi #{idx} missing `{key}`")
            if not str(kpi.get("metric") or "").strip() and not str(kpi.get("cuts") or "").strip():
                self._warning(path, f"kpi #{idx} has empty `metric` and `cuts`; resolver may block it")

    def _validate_feature_mapping(self) -> dict[str, Any] | None:
        path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        data = self._load_json(path, required=False)
        if not data:
            self._warning("kpi_feature_mapping", "kpi_feature_mapping.json is missing; run resolve-kpi-features")
            return None
        for key in ("version", "workspace", "kpis", "summary", "blocker_clusters"):
            if key not in data:
                self._error(path, f"kpi_feature_mapping.json missing `{key}`")
        self._validate_artifact_contract(path, data, KPI_FEATURE_MAPPING_CONTRACT)
        if not isinstance(data.get("kpis", []), list):
            self._error(path, "kpi_feature_mapping.json `kpis` must be a list")
            return data
        for kpi in data.get("kpis", []):
            self._validate_mapped_kpi(path, kpi)
        summary = data.get("summary") or {}
        for key in ("kpi_count", "ready_kpi_count", "blocked_kpi_count", "unresolved_feature_count"):
            if not isinstance(summary.get(key), int):
                self._error(path, f"summary `{key}` must be an integer")
        return data

    def _validate_mapped_kpi(self, path: Path, kpi: dict[str, Any]) -> None:
        for key in ("kpi_id", "name", "status", "features", "open_questions"):
            if key not in kpi:
                self._error(path, f"mapped KPI missing `{key}`")
        if kpi.get("status") not in {"ready_for_sql", "blocked_questions_pending"}:
            self._error(path, f"{kpi.get('kpi_id', '<unknown>')} has unsupported status `{kpi.get('status')}`")
        if not isinstance(kpi.get("features", []), list):
            self._error(path, f"{kpi.get('kpi_id', '<unknown>')} `features` must be a list")
            return
        for feature in kpi.get("features", []):
            for key in ("feature", "state", "resolution_type", "source_columns", "evidence"):
                if key not in feature:
                    self._error(path, f"{kpi.get('kpi_id', '<unknown>')} feature missing `{key}`")
            feature_name = str(feature.get("feature") or "")
            if _is_parser_artifact_feature(feature_name) and feature.get("state") != "rejected":
                self._error(
                    path,
                    f"{kpi.get('kpi_id', '<unknown>')} feature `{feature_name}` looks like a "
                    "parser artifact; fix extraction before asking the user",
                )
            for option in feature.get("derived_feature_options") or []:
                if option.get("missing_inputs"):
                    self._warning(
                        path,
                        f"{kpi.get('kpi_id', '<unknown>')} feature `{feature.get('feature')}` has "
                        "an incomplete derived candidate; it must not be shown as selectable",
                    )
                    continue
                self._validate_derived_feature_option(path, option)

    def _validate_open_questions(self) -> None:
        path = self.layout.reports_dir / "open_questions.md"
        if not path.exists():
            self._warning(path, "open_questions.md is missing")
            return
        self._checked(path)

    def _validate_question_panel(self, mapping: dict[str, Any] | None) -> None:
        summary = (mapping or {}).get("summary") or {}
        blocked_count = int(summary.get("blocked_kpi_count") or 0)
        json_path = self.layout.reports_dir / "blocker_question_panel" / "current.json"
        md_path = self.layout.reports_dir / "blocker_question_panel" / "current.md"
        data = self._load_json(json_path, required=blocked_count > 0)
        if not data:
            return
        self._validate_artifact_contract(json_path, data, BLOCKER_QUESTION_PANEL_CONTRACT)
        for key in (
            "version",
            "workspace",
            "question_id",
            "feature",
            "status",
            "blocker",
            "question",
            "answer_type",
            "recommended_answer",
            "why",
            "options",
        ):
            if key not in data:
                self._error(json_path, f"question panel missing `{key}`")
        if data.get("status") == "needs_user_answer" and not data.get("question"):
            self._error(json_path, "question panel needs_user_answer but has empty question")
        if _is_parser_artifact_feature(str(data.get("feature") or "")):
            self._error(
                json_path,
                f"question panel feature `{data.get('feature')}` looks like a parser artifact; "
                "fix extraction before asking the user",
            )
        options = data.get("options") or []
        if data.get("status") == "needs_user_answer" and not options:
            self._error(json_path, "question panel needs_user_answer but has no options")
        for idx, option in enumerate(options, start=1):
            self._validate_panel_option(json_path, idx, option)
        if md_path.exists():
            self._checked(md_path)
        elif blocked_count > 0:
            self._error(md_path, "current.md is required when KPIs are blocked")

    def _validate_panel_option(self, path: Path, idx: int, option: dict[str, Any]) -> None:
        for key in ("option_id", "label", "business_summary", "json_backed"):
            if key not in option:
                self._error(path, f"question option #{idx} missing `{key}`")
        if not option.get("json_backed"):
            return
        if "derived_feature_option" in option:
            self._validate_derived_feature_option(path, option["derived_feature_option"])
            return
        if "physical_column_option" in option:
            physical = option["physical_column_option"]
            for key in (
                "dataset",
                "column",
                "evidence_state",
                "reason",
                "observed_values",
                "value_profile",
                "semantic_meaning_sources",
                "profile_path",
            ):
                if key not in physical:
                    self._error(path, f"physical option #{idx} missing `{key}`")
            if not isinstance(physical.get("observed_values", []), list):
                self._error(path, f"physical option #{idx} `observed_values` must be a list")
            if not isinstance(physical.get("value_profile", {}), dict):
                self._error(path, f"physical option #{idx} `value_profile` must be an object")
            return
        self._error(path, f"json-backed option #{idx} must include derived or physical evidence")

    def _validate_derived_reviews(self, mapping: dict[str, Any] | None) -> None:
        if not mapping:
            return
        has_derived = any(
            feature.get("derived_feature_options")
            for kpi in mapping.get("kpis", [])
            for feature in kpi.get("features", [])
        )
        if not has_derived:
            return
        review_root = self.layout.reports_dir / "derived_feature_reviews"
        index = review_root / "index.md"
        if not index.exists():
            self._warning(
                index,
                "derived feature options exist but derived_feature_reviews/index.md is missing; "
                "run derived-feature-markdown",
            )
            return
        self._checked(index)

    def _validate_workspace_definitions(self) -> None:
        path = self.layout.contracts_dir / "workspace_feature_definitions.json"
        data = self._load_json(path, required=False)
        if data:
            self._validate_artifact_contract(path, data, WORKSPACE_FEATURE_DEFINITIONS_CONTRACT)

    def _validate_relationship_contracts(self) -> None:
        path = self.layout.contracts_dir / "relationship_contracts.json"
        data = self._load_json(path, required=False)
        if not data:
            return
        self._validate_artifact_contract(path, data, RELATIONSHIP_CONTRACTS_CONTRACT)
        if not isinstance(data.get("relationships", []), list):
            self._error(path, "relationship_contracts.json `relationships` must be a list")
        summary = data.get("summary") or {}
        for key in ("relationship_count", "executable_relationship_count", "candidate_relationship_count"):
            if key in summary and not isinstance(summary[key], int):
                self._error(path, f"relationship summary `{key}` must be an integer")

    def _validate_source_to_target_plan(self) -> None:
        path = self.layout.contracts_dir / "source_to_target_plan.json"
        data = self._load_json(path, required=False)
        if not data:
            return
        self._validate_artifact_contract(path, data, SOURCE_TO_TARGET_PLAN_CONTRACT)
        if not isinstance(data.get("kpis", []), list):
            self._error(path, "source_to_target_plan.json `kpis` must be a list")
        if data.get("target_engine") not in {"sql", "polars", "pyspark", "hybrid"}:
            self._error(path, f"source_to_target_plan.json unsupported target_engine `{data.get('target_engine')}`")

    def _validate_kpi_sql_solutions(self, mapping: dict[str, Any] | None) -> None:
        if not self.layout.solutions_dir.exists():
            return
        sql_files = sorted(self.layout.solutions_dir.glob("kpi_*.sql"))
        sql_files = [path for path in sql_files if path.name != "kpi_metrics.sql"]
        if not sql_files:
            return
        ready_kpis = {
            str(kpi.get("kpi_id") or "")
            for kpi in (mapping or {}).get("kpis", [])
            if kpi.get("status") == "ready_for_sql"
        }
        for path in sql_files:
            self._checked(path)
            kpi_id = path.stem.split("_", 2)
            if len(kpi_id) >= 2:
                kpi_id_value = "_".join(kpi_id[:2])
            else:
                kpi_id_value = path.stem
            result_view = f"{kpi_id_value}_results"
            sql = path.read_text(encoding="utf-8")
            if not sql_defines_result_view(sql, result_view):
                self._error(
                    path,
                    f"generated KPI SQL must define final result view `{result_view}`; "
                    "feature/staging views alone do not prove execution output",
                )
            if ready_kpis and kpi_id_value not in ready_kpis:
                self._warning(path, f"generated SQL exists for `{kpi_id_value}` but mapping does not mark it ready_for_sql")

    def _validate_kpi_execution_harness(self) -> None:
        path = self.layout.evidence_dir / "kpi_execution_harness.json"
        data = self._load_json(path, required=False)
        if not data:
            return
        if data.get("artifact_type") != "kpi_execution_harness.json":
            self._error(path, "kpi_execution_harness.json missing `artifact_type: kpi_execution_harness.json`")
        if data.get("version") != 1:
            self._error(path, "kpi_execution_harness.json has unsupported or missing `version`")
        if data.get("generated_by") != "run-kpi-execution-harness":
            self._error(path, "kpi_execution_harness.json must be generated by run-kpi-execution-harness")
        if data.get("ok") is not True:
            self._error(path, "KPI execution harness did not pass")
        records = data.get("records")
        if not isinstance(records, list) or not records:
            self._error(path, "KPI execution harness must include non-empty `records`")
            return
        for idx, record in enumerate(records, start=1):
            kpi_id = str(record.get("kpi_id") or "")
            expected_view = f"{kpi_id}_results" if kpi_id else ""
            if record.get("result_view") != expected_view:
                self._error(path, f"harness record #{idx} does not target exact result view `{expected_view}`")
            if record.get("status") != "passed":
                self._error(path, f"harness record #{idx} for `{kpi_id}` did not pass")
            if not isinstance(record.get("columns"), list) or not record.get("columns"):
                self._error(path, f"harness record #{idx} for `{kpi_id}` has no result columns")
            elif _placeholder_result_columns(record.get("columns") or []):
                self._error(path, f"harness record #{idx} for `{kpi_id}` has only placeholder result columns")
            if not isinstance(record.get("sample_output_table"), str) or "|" not in record.get("sample_output_table", ""):
                self._error(path, f"harness record #{idx} for `{kpi_id}` missing table-form sample output")

    def _validate_derived_feature_option(self, path: Path, option: dict[str, Any]) -> None:
        required = [
            "derived_column_name",
            "business_meaning",
            "formula",
            "input_columns",
            "example",
            "evidence_sources",
            "derivation_reasoning",
            "evidence_state",
            "confidence",
            "needs_user_confirmation",
        ]
        missing = [key for key in required if key not in option]
        if missing:
            self._error(path, f"derived feature option missing required fields: {', '.join(missing)}")
            return
        if not option.get("formula"):
            self._error(path, "derived feature option must include a non-empty formula")
        if not isinstance(option.get("input_columns"), list) or not option["input_columns"]:
            self._error(path, "derived feature option must include input_columns")
        for column in option.get("input_columns") or []:
            for key in (
                "input_name",
                "column",
                "role",
                "observed_values",
                "value_profile",
                "semantic_meaning_sources",
                "reason",
            ):
                if key not in column:
                    self._error(path, f"derived input column missing `{key}`")
        reasoning = option.get("derivation_reasoning") or {}
        for key in ("why_this_formula", "why_not_ground_truth", "remaining_risk"):
            if key not in reasoning:
                self._error(path, f"derivation_reasoning missing `{key}`")
        if option.get("evidence_state") != "candidate_derivation_not_ground_truth":
            self._warning(path, "derived feature option evidence_state should remain candidate until accepted")

    def _validate_artifact_contract(self, path: Path, data: dict[str, Any], contract) -> None:
        error = contract.validate(data)
        if error:
            self._error(path, error)

    def _validate_medallion_manifest(self) -> None:
        """
        Medallion checks (PRD Section 16, P0 scope: checks 1–4).

        1. schema_version is known.
        2. inputs_hash matches a freshly recomputed hash.
        3. Every gold.derived_from references silver.* (PII-at-rest invariant).
        4. Every Bronze table declares natural_key + pii_columns (the field
           must be present, even if pii_columns is intentionally empty).
        """
        manifest_path = self.layout.generated_dir / "medallion" / "manifest.yaml"
        if not manifest_path.exists():
            return  # medallion is opt-in; only validate when present
        self._checked(manifest_path)

        try:
            import yaml
            manifest_dict = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest_dict, dict):
                self._error(manifest_path, "medallion manifest must be a YAML mapping")
                return
        except Exception as exc:
            self._error(manifest_path, f"could not parse medallion manifest: {exc}")
            return

        # check 1: schema_version known
        from core.medallion.manifest import SCHEMA_VERSION
        version = manifest_dict.get("schema_version")
        if version is None:
            self._error(manifest_path, "medallion manifest missing `schema_version`")
        elif version != SCHEMA_VERSION:
            self._error(
                manifest_path,
                f"medallion manifest schema_version={version} not recognized "
                f"(known: {SCHEMA_VERSION}); regenerate via design-medallion",
            )

        # check 2: inputs_hash matches
        from core.medallion.manifest import compute_inputs_hash
        recorded = manifest_dict.get("inputs_hash", "")
        candidates = [
            self.layout.contracts_dir / "domain_model.json",
            self.layout.contracts_dir / "kpi_registry.json",
            self.layout.contracts_dir / "kpi_feature_mapping.json",
            self.layout.contracts_dir / "semantic_contract.json",
            self.layout.contracts_dir / "workspace_feature_definitions.json",
            self.layout.profiles_dir / "profile_index.json",
        ]
        review_dir = self.layout.reports_dir / "derived_feature_reviews" / "json"
        if review_dir.exists():
            candidates.extend(sorted(review_dir.glob("*.json")))
        recomputed = compute_inputs_hash(candidates)
        if recorded != recomputed:
            self._error(
                manifest_path,
                f"manifest inputs_hash ({recorded[:20]}...) does not match recomputed "
                f"({recomputed[:20]}...); inputs have changed since last design-medallion run",
            )

        # check 3: gold.derived_from must reference silver.* only
        layers = manifest_dict.get("layers", {}) or {}
        for gold in layers.get("gold", []):
            name = gold.get("name", "<unnamed>")
            for src in gold.get("derived_from", []) or []:
                if not str(src).startswith("silver."):
                    self._error(
                        manifest_path,
                        f"gold.{name}.derived_from references `{src}`; Gold may only derive "
                        "from Silver (HIPAA Gold-from-Silver-only invariant)",
                    )

        # check 4: every bronze table declares natural_key + pii_columns
        for bronze in layers.get("bronze", []):
            name = bronze.get("name", "<unnamed>")
            if "natural_key" not in bronze:
                self._error(manifest_path, f"bronze.{name} missing `natural_key` field")
            elif not isinstance(bronze.get("natural_key"), list):
                self._error(manifest_path, f"bronze.{name}.natural_key must be a list")
            elif not bronze["natural_key"]:
                self._warning(manifest_path, f"bronze.{name}.natural_key is empty")
            if "pii_columns" not in bronze:
                self._error(manifest_path, f"bronze.{name} missing `pii_columns` field (use [] if none)")
            elif not isinstance(bronze.get("pii_columns"), list):
                self._error(manifest_path, f"bronze.{name}.pii_columns must be a list")

        # check 5 (P3): every Silver table has a silver_contract entry
        silver_contract_path = self.layout.generated_dir / "medallion" / "silver_contract.json"
        silver_contract: dict[str, Any] = {}
        if layers.get("silver"):
            if not silver_contract_path.exists():
                self._error(manifest_path, "manifest declares Silver tables but silver_contract.json is missing")
            else:
                sc_data = self._load_json(silver_contract_path, required=False) or {}
                silver_contract = sc_data.get("tables", sc_data)
                for s in layers.get("silver", []):
                    if s.get("name") not in silver_contract:
                        self._error(silver_contract_path, f"silver.{s.get('name')} has no contract entry")

        # check 6 (P3): every PII-marked column in semantic_contract appears in some silver pii_hash_columns
        sc_path = self.layout.contracts_dir / "semantic_contract.json"
        if sc_path.exists() and silver_contract:
            sc = self._load_json(sc_path, required=False) or {}
            pii_in_semantic = _collect_pii_columns_from_sc(sc)
            pii_in_silver: set[str] = set()
            for tname, tc in silver_contract.items():
                if isinstance(tc, dict):
                    for col in tc.get("pii_hash_columns", []) or []:
                        pii_in_silver.add(str(col).lower())
            missing_pii = [k for k in pii_in_semantic if k.split(".")[-1].lower() not in pii_in_silver]
            for k in missing_pii:
                self._error(silver_contract_path or sc_path,
                            f"PII column `{k}` is not hashed in any Silver table")

        # check 7 (P3): every fact table has a grain declaration
        star_path = self.layout.generated_dir / "medallion" / "star_schema.json"
        if star_path.exists():
            star = self._load_json(star_path, required=False) or {}
            for f in star.get("facts", []):
                if not f.get("grain"):
                    self._error(star_path, f"fact `{f.get('name', '?')}` has no grain declaration")

        # check 8 (P3): gold.dim_* / gold.fact_* may not reference bronze.* (belt-and-suspenders)
        for g in layers.get("gold", []):
            for src in g.get("derived_from", []) or []:
                if str(src).startswith("bronze."):
                    self._error(manifest_path,
                                f"gold.{g.get('name')} references {src} — Gold may only derive from Silver")

    def _load_json(self, path: Path, *, required: bool) -> dict[str, Any] | None:
        if not path.exists():
            if required:
                self._error(path, "required artifact is missing")
            return None
        self._checked(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self._error(path, f"invalid JSON: {exc}")
            return None
        if not isinstance(data, dict):
            self._error(path, "JSON artifact must be an object")
            return None
        return data

    def _checked(self, path: str | Path) -> None:
        label = str(path) if isinstance(path, str) else _rel(path, self.repo_root)
        if label not in self.result.checked_files:
            self.result.checked_files.append(label)

    def _error(self, path: str | Path, message: str) -> None:
        label = str(path) if isinstance(path, str) else _rel(path, self.repo_root)
        self.result.errors.append(f"{label}: {message}")

    def _warning(self, path: str | Path, message: str) -> None:
        label = str(path) if isinstance(path, str) else _rel(path, self.repo_root)
        self.result.warnings.append(f"{label}: {message}")


def _is_template_kpi_row(name: str) -> bool:
    normalized = "".join(ch if ch.isalnum() else " " for ch in name.lower())
    normalized = " ".join(normalized.split())
    return any(
        marker in normalized
        for marker in (
            "which key business question is the kpi metric feature meant to answer",
            "short description of the kpi metric feature",
        )
    )


def _is_parser_artifact_feature(name: str) -> bool:
    normalized = "".join(ch if ch.isalnum() else " " for ch in name.lower())
    normalized = " ".join(normalized.split())
    return normalized in PARSER_ARTIFACT_FEATURES


def _inventory_has_docs(inventory: dict[str, Any]) -> bool:
    return bool(inventory.get("kpi_registries") or inventory.get("data_models"))


def _inventory_has_data(inventory: dict[str, Any]) -> bool:
    return bool(inventory.get("data_files"))


def _placeholder_result_columns(columns: list[Any]) -> bool:
    normalized = {str(column).strip().lower() for column in columns}
    return bool(normalized) and normalized.issubset({"ready_marker"})


def _collect_pii_columns_from_sc(sc: dict[str, Any]) -> set[str]:
    """Return a set of 'dataset.column' strings marked pii=True in semantic_contract."""
    pii: set[str] = set()
    for ds in sc.get("datasets", []) or []:
        ds_name = ds.get("name", ds.get("path", "unknown"))
        for col, meta in (ds.get("columns") or {}).items():
            if isinstance(meta, dict) and meta.get("pii"):
                pii.add(f"{ds_name}.{col}")
        for col in ds.get("pii_columns", []) or []:
            pii.add(f"{ds_name}.{col}")
    return pii


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate generated workspace artifacts and question panel schema."
    )
    parser.add_argument("--workspace", required=True, help="Workspace path, for example workspaces/demo")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    args = parser.parse_args(argv)

    result = WorkspaceArtifactValidator(args.repo_root, args.workspace).run()
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
