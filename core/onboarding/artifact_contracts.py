from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArtifactContract:
    artifact_type: str
    version_field: str
    supported_version: int
    regenerate_command: str
    required_generated_by: str = ""
    require_artifact_type: bool = True

    def validate(self, data: dict[str, Any]) -> str | None:
        if self.require_artifact_type and data.get("artifact_type") != self.artifact_type:
            return f"{self.artifact_type} missing `artifact_type: {self.artifact_type}`"
        if self.required_generated_by and data.get("generated_by") != self.required_generated_by:
            return (
                f"{self.artifact_type} missing `generated_by: {self.required_generated_by}`; "
                f"regenerate via {self.regenerate_command}"
            )
        version = data.get(self.version_field)
        if version is None:
            return f"{self.artifact_type} missing `{self.version_field}`"
        if version != self.supported_version:
            return (
                f"{self.artifact_type} {self.version_field}={version} is not supported "
                f"(supported: {self.supported_version}); regenerate via {self.regenerate_command}"
            )
        return None


KPI_FEATURE_MAPPING_CONTRACT = ArtifactContract(
    artifact_type="kpi_feature_mapping.json",
    version_field="version",
    supported_version=2,
    regenerate_command="resolve-kpi-features",
    required_generated_by="resolve-kpi-features",
)

BLOCKER_QUESTION_PANEL_CONTRACT = ArtifactContract(
    artifact_type="blocker_question_panel/current.json",
    version_field="version",
    supported_version=1,
    regenerate_command="blocker-question-panel",
    required_generated_by="blocker-question-panel",
)

DATA_SOURCE_PANEL_CONTRACT = ArtifactContract(
    artifact_type="data_source_panel/current.json",
    version_field="version",
    supported_version=1,
    regenerate_command="prepare-data-source-panel",
    required_generated_by="prepare-data-source-panel",
)

WORKSPACE_FEATURE_DEFINITIONS_CONTRACT = ArtifactContract(
    artifact_type="workspace_feature_definitions.json",
    version_field="version",
    supported_version=1,
    regenerate_command="resolve-kpi-features --apply-workspace-definition",
    required_generated_by="apply-kpi-panel-answer",
)

KPI_REGISTRY_CONTRACT = ArtifactContract(
    artifact_type="kpi_registry.json",
    version_field="version",
    supported_version=1,
    regenerate_command="onboard-workspace",
    required_generated_by="onboard-workspace",
)

DOMAIN_MODEL_CONTRACT = ArtifactContract(
    artifact_type="domain_model.json",
    version_field="version",
    supported_version=1,
    regenerate_command="onboard-workspace",
    required_generated_by="onboard-workspace",
)

PROFILE_INDEX_CONTRACT = ArtifactContract(
    artifact_type="profile_index.json",
    version_field="version",
    supported_version=1,
    regenerate_command="onboard-workspace",
    required_generated_by="onboard-workspace",
)

RELATIONSHIP_CONTRACTS_CONTRACT = ArtifactContract(
    artifact_type="relationship_contracts.json",
    version_field="version",
    supported_version=1,
    regenerate_command="build-relationship-contracts",
    required_generated_by="build-relationship-contracts",
)

SOURCE_TO_TARGET_PLAN_CONTRACT = ArtifactContract(
    artifact_type="source_to_target_plan.json",
    version_field="version",
    supported_version=1,
    regenerate_command="plan-source-to-target",
    required_generated_by="plan-source-to-target",
)

CATALOG_CONTRACT_CONTRACT = ArtifactContract(
    artifact_type="catalog_contract.json",
    version_field="version",
    supported_version=1,
    regenerate_command="build-catalog-contract",
    required_generated_by="build-catalog-contract",
)

DATA_ENGINEERING_ROUTE_CONTRACT = ArtifactContract(
    artifact_type="data_engineering_route.json",
    version_field="version",
    supported_version=1,
    regenerate_command="prepare-data-engineering-route",
    required_generated_by="prepare-data-engineering-route",
)

PIPELINE_PLAN_CONTRACT = ArtifactContract(
    artifact_type="pipeline_plan.json",
    version_field="version",
    supported_version=1,
    regenerate_command="prepare-pipeline-plan",
    required_generated_by="prepare-pipeline-plan",
)

PIPELINE_DECISIONS_CONTRACT = ArtifactContract(
    artifact_type="pipeline_decisions.json",
    version_field="version",
    supported_version=1,
    regenerate_command="apply-pipeline-decision",
    required_generated_by="apply-pipeline-decision",
)

DATA_QUALITY_CONTRACT = ArtifactContract(
    artifact_type="data_quality_contract.json",
    version_field="version",
    supported_version=1,
    regenerate_command="run-data-quality-harness",
    required_generated_by="run-data-quality-harness",
)

DUPLICATE_DECISIONS_CONTRACT = ArtifactContract(
    artifact_type="duplicate_decisions.json",
    version_field="version",
    supported_version=1,
    regenerate_command="apply-duplicate-review-answer",
    required_generated_by="apply-duplicate-review-answer",
)
