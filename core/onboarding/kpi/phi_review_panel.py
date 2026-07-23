"""PHI/PII review-and-consent panel (Priority 1.1).

Detection today (``core.governance.phi_gate`` + ``_sensitive_columns_section``
in ``core.onboarding.workspace.onboarding``) is automatic and silent: a column
gets flagged ``is_sensitive`` in ``semantic_contract.json`` with zero human step
in between. This module is that missing human step, following the same
prepare -> present -> apply -> record shape already proven by
``core.onboarding.kpi.blocker_question_panel`` / ``blocker_cli``, scoped down
to what a PHI review actually needs (no KPI-resolution machinery).

For each detected-sensitive column the human picks one of two things:
  - it genuinely is not sensitive (override) -> written to the user-authored
    ``data_policy.json``'s ``not_sensitive_columns`` allowlist, exactly the
    mechanism that file already documents. This module writes it ON THE
    HUMAN'S BEHALF only after an explicit, real ``--confirmed-by`` answer --
    never silently, never agent-asserted.
  - it IS sensitive, and a disposition: ``hash_to_key`` (irreversible hash,
    join-key use only), ``pass_through_and_tag`` (kept readable, governed by
    catalog-layer masking downstream), or ``bronze_only`` (never selected past
    bronze). This is new state with no existing home -- ``data_policy.json``
    is documented as "declare what's sensitive", not "decide what to do with
    it" -- so it's recorded in a new generated+human-confirmed contract,
    ``interns/generated/contracts/phi_disposition.json``, the same family as
    ``kpi_feature_mapping.json`` / ``relationship_contracts.json``.

PHI classification is high-sensitivity enough that this panel's answers are
NOT eligible for a persisted default identity (Priority 4.3) -- every answer
requires a real ``--confirmed-by`` on the command itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.governance.data_policy import find_policy_path
from core.governance.phi_gate import identifier_category, pci_identifier_category
from core.governance.provenance import decision_source, is_agent_confirmer
from core.observability.cost_ledger import anchored
from core.storage.workspace_layout import WorkspaceLayout

PANEL_VERSION = 1
DISPOSITIONS: tuple[str, ...] = ("hash_to_key", "pass_through_and_tag", "bronze_only")
NOT_SENSITIVE_ANSWER = "not_sensitive"
VALID_ANSWERS: tuple[str, ...] = DISPOSITIONS + (NOT_SENSITIVE_ANSWER,)


@dataclass(frozen=True)
class PHIReviewPanelResult:
    output_dir: str
    current_json: str
    current_markdown: str
    column_count: int

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class PHIReviewPanelBuilder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.output_dir = self.layout.reports_dir / "phi_review_panel"

    def run(self) -> PHIReviewPanelResult:
        contract_path = self.layout.contracts_dir / "semantic_contract.json"
        contract = _load_json(contract_path)
        columns = contract.get("columns") if isinstance(contract.get("columns"), dict) else {}

        disposition_path = self.layout.contracts_dir / "phi_disposition.json"
        already_dispositioned = {
            record.get("column")
            for record in _load_json(disposition_path).get("columns", [])
            if isinstance(record, dict)
        }
        # A `not_sensitive` answer is written to data_policy.json's
        # not_sensitive_columns allowlist (_write_allowlist_override), not
        # phi_disposition.json -- that's a SEPARATE file from the one checked
        # above. semantic_contract.json's is_sensitive flag is a snapshot from
        # whenever onboarding last ran; it does not update just because an
        # answer was recorded, so relying on it alone means a `not_sensitive`
        # answer can never satisfy this gate -- the panel re-flags the same
        # column as pending forever, no matter how many times it's answered.
        # Consult the allowlist directly so an answer takes effect immediately,
        # regardless of contract staleness. This also honors a workspace
        # owner's own hand-authored allowlist entries (data_policy.json is
        # user-authored input per AGENTS.md), not only ones this panel wrote.
        policy_path = find_policy_path(self.workspace) or (self.workspace / "data_policy.json")
        already_not_sensitive = set(_load_json(policy_path).get("not_sensitive_columns") or [])

        pending = []
        for name, info in sorted(columns.items()):
            if not isinstance(info, dict) or not info.get("is_sensitive"):
                continue
            if name in already_dispositioned or name in already_not_sensitive:
                continue
            pending.append(
                {
                    "column": name,
                    "detected_category": str(info.get("category") or ""),
                }
            )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        current = {
            "artifact_type": "phi_review_panel/current.json",
            "version": PANEL_VERSION,
            "generated_by": "prepare-phi-review-panel",
            "workspace": _rel(self.workspace, self.repo_root),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "needs_user_answer" if pending else "no_pending_columns",
            "instruction": (
                "For each column, confirm it is not actually sensitive (option: "
                f"`{NOT_SENSITIVE_ANSWER}`), or confirm it IS sensitive and pick a "
                "disposition: `hash_to_key` (irreversible hash, join-key use only), "
                "`pass_through_and_tag` (kept readable, governed by catalog-layer "
                "masking downstream), or `bronze_only` (never selected past bronze). "
                "Every answer requires a real --confirmed-by name; this gate does not "
                "accept a persisted default identity."
            ),
            "columns": pending,
            "valid_answers": list(VALID_ANSWERS),
        }
        current_json = self.output_dir / "current.json"
        current_markdown = self.output_dir / "current.md"
        current_json.write_text(json.dumps(current, indent=2, default=str) + "\n", encoding="utf-8")
        current_markdown.write_text(_render_markdown(current), encoding="utf-8")

        return PHIReviewPanelResult(
            output_dir=_rel(self.output_dir, self.repo_root),
            current_json=_rel(current_json, self.repo_root),
            current_markdown=_rel(current_markdown, self.repo_root),
            column_count=len(pending),
        )


def apply_phi_review_answer(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    column: str,
    answer: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Apply one human answer for one column. Raises ValueError on a bad answer.

    Never writes anything when ``confirmed_by`` is agent-asserted (empty or an
    automation identity) -- this gate exists specifically so a PHI decision is
    never recorded as human-approved without a real human behind it.
    """
    if answer not in VALID_ANSWERS:
        raise ValueError(f"answer must be one of {VALID_ANSWERS!r}, got {answer!r}")
    if is_agent_confirmer(confirmed_by):
        raise ValueError(
            "PHI review requires a real --confirmed-by name; "
            f"{confirmed_by!r} is agent-asserted, not accepted for this gate"
        )

    repo_root = Path(repo_root).resolve()
    workspace_path = (repo_root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    confirmed_at = datetime.now(timezone.utc).isoformat()

    if answer == NOT_SENSITIVE_ANSWER:
        return _write_allowlist_override(
            workspace_path, column=column, confirmed_by=confirmed_by, confirmed_at=confirmed_at
        )

    disposition_path = layout.contracts_dir / "phi_disposition.json"
    data = _load_json(disposition_path)
    records = [r for r in data.get("columns", []) if isinstance(r, dict) and r.get("column") != column]
    records.append(
        {
            "column": column,
            "disposition": answer,
            "confirmed_by": confirmed_by,
            "confirmed_at": confirmed_at,
            "source": decision_source(confirmed_by),
        }
    )
    records.sort(key=lambda r: str(r.get("column") or ""))
    disposition_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_type": "phi_disposition.json",
        "version": PANEL_VERSION,
        "workspace": _rel(workspace_path, repo_root),
        "updated_at": confirmed_at,
        "columns": records,
    }
    disposition_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return {"column": column, "disposition": answer, "written_to": _rel(disposition_path, repo_root)}


def _write_allowlist_override(
    workspace_path: Path, *, column: str, confirmed_by: str, confirmed_at: str
) -> dict[str, Any]:
    policy_path = find_policy_path(workspace_path) or (workspace_path / "data_policy.json")
    data: dict[str, Any] = _load_json(policy_path)
    if not data:
        data = {"artifact_type": "workspace_data_policy.json", "version": 1}
    allowlist = list(dict.fromkeys(data.get("not_sensitive_columns") or []))
    if column not in allowlist:
        allowlist.append(column)
    data["not_sensitive_columns"] = allowlist
    # Provenance is additive metadata alongside the documented schema -- the
    # existing loader ignores unknown keys, so this doesn't change the
    # user-authored file's documented shape or how it's read elsewhere.
    provenance = data.get("not_sensitive_columns_confirmed_by")
    if not isinstance(provenance, dict):
        provenance = {}
    provenance[column] = {"confirmed_by": confirmed_by, "confirmed_at": confirmed_at}
    data["not_sensitive_columns_confirmed_by"] = provenance
    policy_path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    return {"column": column, "answer": NOT_SENSITIVE_ANSWER, "written_to": str(policy_path)}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _render_markdown(current: dict[str, Any]) -> str:
    lines = [
        "# PHI/PII Review Panel",
        "",
        f"Workspace: `{current.get('workspace', '')}`",
        f"Status: `{current.get('status', '')}`",
        "",
        str(current.get("instruction", "")),
        "",
    ]
    columns = current.get("columns") or []
    if not columns:
        lines.append("No pending sensitive columns.")
    else:
        lines.append("| column | detected category |")
        lines.append("| --- | --- |")
        for item in columns:
            lines.append(f"| {item.get('column', '')} | {item.get('detected_category', '')} |")
    return "\n".join(lines) + "\n"


@anchored("prepare-phi-review-panel")
def prepare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prepare-phi-review-panel")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = PHIReviewPanelBuilder(args.repo_root, args.workspace).run()
    print(json.dumps(result.summary(), indent=2))
    return 0


@anchored("apply-phi-review-answer")
def apply_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apply-phi-review-answer")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--column", required=True)
    parser.add_argument("--answer", required=True, choices=list(VALID_ANSWERS))
    parser.add_argument("--confirmed-by", default="")
    args = parser.parse_args(argv)
    try:
        result = apply_phi_review_answer(
            args.repo_root,
            args.workspace,
            column=args.column,
            answer=args.answer,
            confirmed_by=args.confirmed_by,
        )
    except ValueError as exc:
        print(f"[apply-phi-review-answer] ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


__all__ = [
    "DISPOSITIONS",
    "NOT_SENSITIVE_ANSWER",
    "VALID_ANSWERS",
    "PHIReviewPanelBuilder",
    "PHIReviewPanelResult",
    "apply_main",
    "apply_phi_review_answer",
    "prepare_main",
]
