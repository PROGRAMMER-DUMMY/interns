"""Validate generated workspace artifacts before agents rely on them."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


PARSER_ARTIFACT_FEATURES = {
    "a",
    "an",
    "and",
    "commercial",
    "for",
    "medicaid",
    "medicare",
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
        self._validate_kpi_registry()
        mapping = self._validate_feature_mapping()
        self._validate_open_questions()
        self._validate_question_panel(mapping)
        self._validate_derived_reviews(mapping)
        return self.result

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            self._error("workspace", f"workspace does not exist: {_rel(self.workspace, self.repo_root)}")
            return
        if not self.workspace.is_relative_to(self.repo_root) or self.workspace == self.repo_root:
            self._error("workspace", "workspace must be inside the repo root and not equal to it")
        docs = self.workspace / "docs"
        datasets = self.workspace / "datasets"
        if not docs.exists():
            self._warning("workspace", "docs/ was not found under the workspace")
        if not datasets.exists():
            self._warning("workspace", "datasets/ was not found under the workspace")

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

    def _validate_profile_index(self) -> None:
        path = self.layout.profiles_dir / "profile_index.json"
        data = self._load_json(path, required=False)
        if not data:
            self._warning("profile_index", "profile_index.json is missing; run onboard-workspace")
            return
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

    def _validate_kpi_registry(self) -> None:
        path = self.layout.contracts_dir / "kpi_registry.json"
        data = self._load_json(path, required=False)
        if not data:
            self._warning("kpi_registry", "kpi_registry.json is missing; run onboard-workspace")
            return
        if data.get("generated_by") != "onboard-workspace":
            self._error(
                path,
                "kpi_registry.json missing `generated_by: onboard-workspace`; regenerate instead "
                "of manually editing generated contracts",
            )
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
