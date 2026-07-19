"""CLI for the second step of the CLI-agent proposal flow.

When the orchestrating CLI agent applies an answer with ``--via-cli-agent``,
the workspace records the resulting mapping with ``state="cli_agent_proposed"``
rather than ``"user_confirmed"``. That keeps the KPI off the ready-for-SQL
list until a human reviews the proposal. This CLI flips the proposed entry
to its final state once the user has reviewed it:

* ``--decision confirm`` -> ``state="user_confirmed"``
* ``--decision reject`` -> ``state="cli_agent_rejected"`` and the prior
  workspace-definition entry is preserved for audit but cleared from the
  mapping (KPI rows revert to their pre-proposal blocker state on the next
  ``prepare-kpi-blocker-panel`` run).
"""

from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.memory.workspace_definitions import (
    apply_workspace_definitions_to_mapping,
    load_workspace_definitions,
    recompute_mapping_status,
    workspace_definitions_path,
)
from core.onboarding.workspace.cli_runner import run_workspace_command
from core.storage.atomic_io import write_text_atomic
from core.storage.workspace_layout import WorkspaceLayout


PROPOSED_STATE = "cli_agent_proposed"
CONFIRMED_STATE = "user_confirmed"
REJECTED_STATE = "cli_agent_rejected"


@dataclass(frozen=True)
class ConfirmResult:
    workspace: str
    feature: str
    previous_state: str
    new_state: str
    decision: str
    note: str
    mapping_path: str
    definitions_path: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_feature(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _find_proposed_entry(definitions: dict[str, Any], feature: str) -> dict[str, Any] | None:
    target = _normalize_feature(feature)
    candidates: list[dict[str, Any]] = []
    for entry in definitions.get("definitions", []):
        if not isinstance(entry, dict):
            continue
        if _normalize_feature(entry.get("feature", "")) != target:
            continue
        if entry.get("state") == PROPOSED_STATE or entry.get("status") == PROPOSED_STATE:
            candidates.append(entry)
    if not candidates:
        return None
    # Latest first.
    candidates.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return candidates[0]


def confirm_cli_agent_proposal(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    feature: str,
    decision: str,
    note: str = "",
) -> ConfirmResult:
    """Flip a previously-proposed mapping to ``user_confirmed`` or ``rejected``."""

    if decision not in {"confirm", "reject"}:
        raise ValueError(f"--decision must be `confirm` or `reject`, got `{decision}`")

    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    definitions_path = workspace_definitions_path(layout)
    if not definitions_path.exists():
        raise FileNotFoundError(
            f"workspace_feature_definitions.json not found: {definitions_path}"
        )
    mapping_path = layout.contracts_dir / "kpi_feature_mapping.json"
    if not mapping_path.exists():
        raise FileNotFoundError(f"kpi_feature_mapping.json not found: {mapping_path}")

    definitions = load_workspace_definitions(layout)
    entry = _find_proposed_entry(definitions, feature)
    if entry is None:
        raise ValueError(
            f"No CLI-agent proposal awaiting confirmation for feature `{feature}`. "
            "Either the proposal was never applied, or it was already confirmed/rejected."
        )

    previous_state = str(entry.get("state") or "")
    new_state = CONFIRMED_STATE if decision == "confirm" else REJECTED_STATE
    timestamp = datetime.now(timezone.utc).isoformat()
    entry["state"] = new_state
    entry["status"] = new_state
    entry["updated_at"] = timestamp
    entry.setdefault("evidence", []).append(
        {
            "type": "cli_agent_confirmation",
            "source": "cli",
            "detail": note or f"user {decision}ed CLI-agent proposal",
            "timestamp": timestamp,
        }
    )

    definitions["updated_at"] = timestamp
    # P4d (T6): atomic writes so an interrupted confirm can't corrupt the
    # definitions/mapping stores. Ref: core-audit ob-kpi-c.md, storage.md.
    write_text_atomic(
        definitions_path, json.dumps(definitions, indent=2, default=str) + "\n"
    )

    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    target_feature = _normalize_feature(feature)
    for kpi in mapping.get("kpis", []):
        for item in kpi.get("features", []):
            if _normalize_feature(item.get("feature", "")) != target_feature:
                continue
            if item.get("state") != PROPOSED_STATE:
                continue
            if decision == "confirm":
                item["state"] = CONFIRMED_STATE
                item.setdefault("decision_history", []).append(
                    {
                        "state": CONFIRMED_STATE,
                        "resolution_type": item.get("resolution_type", "workspace_definition"),
                        "note": note or "User confirmed CLI-agent proposal.",
                        "timestamp": timestamp,
                    }
                )
            else:
                # On reject, revert the row to a blocked state so the next
                # blocker panel re-asks. We don't try to reconstruct the
                # pre-proposal state — the blocker workflow regenerates it.
                item["state"] = "blocked_missing_evidence"
                item["resolution_type"] = "rejected_cli_agent_proposal"
                item["question"] = (
                    item.get("question")
                    or f"User rejected CLI-agent proposal for `{feature}`; rerun the panel."
                )
                item.setdefault("decision_history", []).append(
                    {
                        "state": REJECTED_STATE,
                        "resolution_type": "rejected_cli_agent_proposal",
                        "note": note or "User rejected CLI-agent proposal.",
                        "timestamp": timestamp,
                    }
                )

    # Reapply all confirmed/proven workspace definitions and recompute KPI
    # status so the mapping summary reflects the new state.
    apply_workspace_definitions_to_mapping(mapping, definitions)
    recompute_mapping_status(mapping)
    write_text_atomic(mapping_path, json.dumps(mapping, indent=2, default=str) + "\n")

    return ConfirmResult(
        workspace=str(workspace),
        feature=feature,
        previous_state=previous_state,
        new_state=new_state,
        decision=decision,
        note=note,
        mapping_path=str(mapping_path),
        definitions_path=str(definitions_path),
    )



@anchored("confirm-cli-agent-proposal")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Confirm or reject a CLI-agent-proposed KPI feature mapping. "
            "Required after `apply-kpi-panel-answer --via-cli-agent`."
        )
    )
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--feature", required=True)
    parser.add_argument(
        "--decision",
        required=True,
        choices=["confirm", "reject"],
    )
    parser.add_argument("--note", default="")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="confirm-cli-agent-proposal",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: confirm_cli_agent_proposal(
            args.repo_root,
            args.workspace,
            feature=args.feature,
            decision=args.decision,
            note=args.note,
        ),
        op_args={
            "workspace": args.workspace,
            "feature": args.feature,
            "decision": args.decision,
            "note": args.note,
        },
        allow_replay=args.allow_replay,
        decision=args.decision,
        metadata={"feature": args.feature, "decision": args.decision},
        record_idempotent=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
