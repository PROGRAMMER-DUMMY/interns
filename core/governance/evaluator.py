"""
Governance evaluation for candidate optimization runs.

This module does not execute workloads. It evaluates the evidence produced by a
run and decides whether the candidate can be promoted, must be reviewed, or must
be rejected.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.governance.contracts import OptimizationPolicy
from core.governance.mode_policy import ModePlan
from core.governance.semantic_contract import SemanticContract


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    passed: bool
    severity: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertEvent:
    alert_id: str
    severity: str
    title: str
    message: str
    run_id: str
    task_id: str
    requires_human: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass(frozen=True)
class EvidencePack:
    run_id: str
    task_id: str
    artifact: str
    mode: str
    change_type: str
    change_types: list[str]
    baseline_metric: float | None
    candidate_metric: float | None
    metric_delta: float | None
    direction: str
    execution_time_seconds: float | None
    matching_score: float | None
    semantic_contract_summary: dict[str, Any]
    policy_summary: dict[str, Any]
    mode_plan: dict[str, Any]
    artifact_diff: str = ""
    planner_evidence: dict[str, Any] = field(default_factory=dict)
    profiler_evidence: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        data = asdict(self)
        diff = data.get("artifact_diff") or ""
        data["artifact_diff"] = diff[:12_000]
        data["artifact_diff_truncated"] = len(diff) > 12_000
        return data


@dataclass(frozen=True)
class GovernanceDecision:
    run_id: str
    task_id: str
    decision: str
    approval_state: str
    promotion_allowed: bool
    gates: list[GateResult]
    alerts: list[AlertEvent]
    evidence_pack: EvidencePack
    rationale: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "decision": self.decision,
            "approval_state": self.approval_state,
            "promotion_allowed": self.promotion_allowed,
            "rationale": self.rationale,
            "timestamp": self.timestamp,
            "gates": [asdict(gate) for gate in self.gates],
            "alerts": [asdict(alert) for alert in self.alerts],
            "evidence_pack": self.evidence_pack.summary(),
        }


class GovernanceEvaluator:
    def __init__(self, policy: OptimizationPolicy):
        self.policy = policy

    def evaluate(
        self,
        *,
        run_id: str,
        task: dict[str, Any],
        status: str,
        baseline_metric: float | None,
        candidate_metric: float | None,
        metric_delta: float | None,
        execution_time_seconds: float | None,
        matching_score: float | None,
        classification: Any,
        semantic_contract: SemanticContract,
        mode_plan: ModePlan,
        artifact_diff: str,
        planner_evidence: dict[str, Any] | None = None,
        profiler_evidence: dict[str, Any] | None = None,
    ) -> GovernanceDecision:
        task_id = task.get("id", "")
        gates = self._build_gates(
            status=status,
            candidate_metric=candidate_metric,
            execution_time_seconds=execution_time_seconds,
            matching_score=matching_score,
            semantic_contract=semantic_contract,
            mode_plan=mode_plan,
        )
        approval_required = self._approval_required(classification, mode_plan)
        failed_error_gates = [
            gate for gate in gates if not gate.passed and gate.severity == "error"
        ]

        if failed_error_gates or status in {"crash", "discard"}:
            decision = "rejected"
            approval_state = "not_applicable"
            promotion_allowed = False
            rationale = self._rejection_rationale(status, failed_error_gates)
        elif approval_required:
            decision = "needs_review"
            approval_state = "pending_human"
            promotion_allowed = False
            rationale = "Candidate improved but policy requires human review before promotion."
        elif self.policy.approval.auto_approve_safe_improvements:
            decision = "approved"
            approval_state = "auto_approved"
            promotion_allowed = mode_plan.promotion_allowed
            rationale = "Candidate passed all gates and safe auto-approval is enabled."
        else:
            decision = "needs_review"
            approval_state = "pending_human"
            promotion_allowed = False
            rationale = "Candidate passed gates; human approval is required by default."

        evidence_pack = EvidencePack(
            run_id=run_id,
            task_id=task_id,
            artifact=task.get("editable_file", ""),
            mode=mode_plan.active_mode,
            change_type=classification.primary_type,
            change_types=list(classification.change_types),
            baseline_metric=baseline_metric,
            candidate_metric=candidate_metric,
            metric_delta=metric_delta,
            direction=task.get("direction", "higher"),
            execution_time_seconds=execution_time_seconds,
            matching_score=matching_score,
            semantic_contract_summary=semantic_contract.summary(),
            policy_summary=self.policy.summary(),
            mode_plan=mode_plan.as_dict(),
            artifact_diff=artifact_diff,
            planner_evidence=planner_evidence or {},
            profiler_evidence=profiler_evidence or {},
        )
        alerts = self._build_alerts(
            run_id=run_id,
            task_id=task_id,
            decision=decision,
            failed_gates=failed_error_gates,
            approval_required=approval_required,
            classification=classification,
        )
        return GovernanceDecision(
            run_id=run_id,
            task_id=task_id,
            decision=decision,
            approval_state=approval_state,
            promotion_allowed=promotion_allowed,
            gates=gates,
            alerts=alerts,
            evidence_pack=evidence_pack,
            rationale=rationale,
        )

    def _build_gates(
        self,
        *,
        status: str,
        candidate_metric: float | None,
        execution_time_seconds: float | None,
        matching_score: float | None,
        semantic_contract: SemanticContract,
        mode_plan: ModePlan,
    ) -> list[GateResult]:
        sla = self.policy.sla
        gates = [
            GateResult(
                gate_id="metric_present",
                passed=candidate_metric is not None,
                severity="error",
                message="Candidate metric must be present.",
                evidence={"candidate_metric": candidate_metric},
            ),
            GateResult(
                gate_id="run_status",
                passed=status == "keep",
                severity="error",
                message="Only improved candidates can enter promotion review.",
                evidence={"status": status},
            ),
            GateResult(
                gate_id="semantic_contract_loaded",
                passed=bool(semantic_contract.rules),
                severity="warning",
                message="Semantic contract should contain enforceable rules.",
                evidence=semantic_contract.summary(),
            ),
            GateResult(
                gate_id="matching_score",
                passed=matching_score is None or matching_score >= sla.min_matching_score,
                severity="error",
                message=f"Matching score must be at least {sla.min_matching_score}.",
                evidence={"matching_score": matching_score},
            ),
            GateResult(
                gate_id="mode_promotion",
                passed=mode_plan.promotion_allowed,
                severity="error",
                message="Selected execution mode must allow promotion.",
                evidence=mode_plan.as_dict(),
            ),
        ]
        if sla.max_runtime_seconds is not None:
            gates.append(
                GateResult(
                    gate_id="runtime_sla",
                    passed=(
                        execution_time_seconds is not None
                        and execution_time_seconds <= sla.max_runtime_seconds
                    ),
                    severity="error",
                    message=f"Runtime must be <= {sla.max_runtime_seconds}s.",
                    evidence={"execution_time_seconds": execution_time_seconds},
                )
            )
        return gates

    def _approval_required(self, classification: Any, mode_plan: ModePlan) -> bool:
        approval = self.policy.approval
        if approval.required_for_production:
            return True
        required_types = set(approval.required_change_types)
        if mode_plan.active_mode == "global_exploration":
            return True
        return bool(required_types.intersection(set(classification.change_types)))

    def _build_alerts(
        self,
        *,
        run_id: str,
        task_id: str,
        decision: str,
        failed_gates: list[GateResult],
        approval_required: bool,
        classification: Any,
    ) -> list[AlertEvent]:
        alerts: list[AlertEvent] = []
        for gate in failed_gates:
            alerts.append(
                AlertEvent(
                    alert_id=f"{run_id}:{gate.gate_id}",
                    severity="critical",
                    title=f"Gate failed: {gate.gate_id}",
                    message=gate.message,
                    run_id=run_id,
                    task_id=task_id,
                    evidence=gate.evidence,
                )
            )
        if decision == "needs_review" or approval_required:
            alerts.append(
                AlertEvent(
                    alert_id=f"{run_id}:approval_required",
                    severity="review",
                    title="Human approval required",
                    message=(
                        "Review evidence pack before promotion. "
                        f"Primary change type: {classification.primary_type}."
                    ),
                    run_id=run_id,
                    task_id=task_id,
                    evidence={
                        "change_type": classification.primary_type,
                        "change_types": classification.change_types,
                    },
                )
            )
        return alerts

    def _rejection_rationale(self, status: str, failed_gates: list[GateResult]) -> str:
        if status in {"crash", "discard"}:
            return f"Candidate status is {status}; promotion is blocked."
        failed = ", ".join(gate.gate_id for gate in failed_gates)
        return f"Promotion blocked by failed gates: {failed}."
