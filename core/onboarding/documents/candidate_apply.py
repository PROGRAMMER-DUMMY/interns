"""Governed document candidate promotion (Phase 3: promote path, step 2).

`apply-document-candidate` CLI: accept or reject a single document-derived
candidate.  Human confirmation (`--confirmed-by`) is MANDATORY for acceptance;
the command refuses to apply without it so the gate-provenance rule is enforced.

On acceptance the human decision is appended to the DURABLE artifact:

    interns/generated/documents/accepted_candidates.json

This file is written under `interns/generated/documents/` -- NOT under
`interns/generated/contracts/` -- so onboarding re-runs (which clear and
regenerate `contracts/`) do NOT destroy accepted decisions.

On rejection a corresponding entry with `decision: rejected` is appended to the
same file so the rejection is auditable.

Neither path touches kpi_registry.json, workspace_lexicon.json, relationship_
contracts.json, or any other generated contract directly.  The `merge_accepted_
candidates()` helper returns entries ready for an onboarding caller to merge
in a separate, wired-in step.

data_model_candidate entries are flagged `executable: False` even after human
acceptance -- they still require profile RI proof (BUG-004/023 discipline).
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.documents.candidate_review import (
    _stable_candidate_id,
)
from core.storage.atomic_io import read_json_or_quarantine
from core.storage.workspace_layout import WorkspaceLayout

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACCEPTED_CANDIDATES_VERSION = 1

# Candidate types that must remain non-executable after acceptance
_NON_EXECUTABLE_TYPES: frozenset[str] = frozenset({"data_model_candidate"})


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

class ApplyDocumentCandidateResult:
    """Return value of apply_document_candidate()."""

    __slots__ = (
        "workspace",
        "candidate_id",
        "decision",
        "confirmed_by",
        "accepted_candidates_path",
        "status",
        "note",
    )

    def __init__(
        self,
        *,
        workspace: str,
        candidate_id: str,
        decision: str,
        confirmed_by: str,
        accepted_candidates_path: str,
        status: str,
        note: str,
    ) -> None:
        self.workspace = workspace
        self.candidate_id = candidate_id
        self.decision = decision
        self.confirmed_by = confirmed_by
        self.accepted_candidates_path = accepted_candidates_path
        self.status = status
        self.note = note

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "candidate_id": self.candidate_id,
            "decision": self.decision,
            "confirmed_by": self.confirmed_by,
            "accepted_candidates_path": self.accepted_candidates_path,
            "status": self.status,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def apply_document_candidate(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    candidate_id: str,
    confirmed_by: str,
    reject: bool = False,
    note: str = "",
) -> ApplyDocumentCandidateResult:
    """Accept or reject a document-derived candidate and record the decision durably.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    workspace:
        Path to the workspace directory (may be relative to repo_root).
    candidate_id:
        The stable ``candidate_id`` from the review panel (candidates.json).
    confirmed_by:
        Name/identity of the human reviewer.  REQUIRED for acceptance; the
        function returns status='refused' if this is blank.
    reject:
        If True, record a rejection instead of an acceptance.
    note:
        Optional free-text reason (recommended for rejections).

    Returns
    -------
    ApplyDocumentCandidateResult.
    """
    repo_root = Path(repo_root).resolve()
    workspace_path = (repo_root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    workspace_rel = _rel(workspace_path, repo_root)

    # -- Human confirmation gate (mandatory for promotion) -------------------
    if not reject and not confirmed_by:
        return ApplyDocumentCandidateResult(
            workspace=workspace_rel,
            candidate_id=candidate_id,
            decision="refused",
            confirmed_by="",
            accepted_candidates_path="",
            status="refused",
            note=(
                "[x] --confirmed-by is required to accept a document candidate. "
                "Candidates must have explicit human sign-off before promotion. "
                "Re-run with --confirmed-by <reviewer_name>."
            ),
        )

    # -- Locate the candidate ------------------------------------------------
    candidates_json = layout.generated_dir / "documents" / "candidates.json"
    if not candidates_json.exists():
        return ApplyDocumentCandidateResult(
            workspace=workspace_rel,
            candidate_id=candidate_id,
            decision="",
            confirmed_by=confirmed_by,
            accepted_candidates_path="",
            status="error",
            note=(
                f"[x] candidates.json not found at {candidates_json}. "
                "Run `uv run onboard-workspace --workspace <workspace>` first."
            ),
        )

    raw = json.loads(candidates_json.read_text(encoding="utf-8"))
    raw_candidates: list[dict[str, Any]] = raw.get("candidates") or []

    # Build ID -> candidate mapping (compute IDs on the fly, same algo as review)
    matched: dict[str, Any] | None = None
    for cand in raw_candidates:
        if _stable_candidate_id(cand) == candidate_id:
            matched = cand
            break

    if matched is None:
        return ApplyDocumentCandidateResult(
            workspace=workspace_rel,
            candidate_id=candidate_id,
            decision="",
            confirmed_by=confirmed_by,
            accepted_candidates_path="",
            status="error",
            note=(
                f"[x] candidate_id `{candidate_id}` not found in candidates.json. "
                "Re-run prepare-document-candidate-review to refresh the panel, "
                "then use the candidate_id from that output."
            ),
        )

    # -- Build the decision record -------------------------------------------
    candidate_type = str(matched.get("candidate_type") or "")
    decision = "rejected" if reject else "accepted"

    # Determine executability: data_model_candidate is NON-executable even after
    # human acceptance (profile RI proof still required per BUG-004/023 discipline).
    executable = False if candidate_type in _NON_EXECUTABLE_TYPES else (not reject)

    record: dict[str, Any] = {
        "candidate_id": candidate_id,
        "decision": decision,
        "source": "human",
        "confirmed_by": confirmed_by,
        "candidate_type": candidate_type,
        "target_type": candidate_type,
        "content": matched.get("content") or {},
        "source_document": matched.get("source_document") or matched.get("source") or "",
        "page": matched.get("page"),
        "bbox": matched.get("bbox"),
        "confidence": matched.get("confidence"),
        "reason_from_classifier": matched.get("reason") or "",
        "rejection_note": note if reject else "",
        "executable": executable,
        "provenance": {
            "source": "document_pdf",
            "confirmed_by": confirmed_by,
            "candidates_json": _rel(candidates_json, repo_root),
        },
    }

    if candidate_type in _NON_EXECUTABLE_TYPES and not reject:
        record["non_executable_reason"] = (
            "data_model_candidate requires profile RI proof before any join is executable "
            "(BUG-004/023 discipline + low-cardinality confidence cap). "
            "This acceptance records the human review decision only; "
            "run build-relationship-contracts with profile evidence to make it executable."
        )

    # -- Persist to durable artifact -----------------------------------------
    durable_path = layout.generated_dir / "documents" / "accepted_candidates.json"
    durable_path.parent.mkdir(parents=True, exist_ok=True)

    store = _load_durable(durable_path, workspace_rel)

    # Upsert by candidate_id: replace any prior decision for this candidate
    existing = store.get("decisions") or []
    updated = [d for d in existing if d.get("candidate_id") != candidate_id]
    updated.append(record)
    # Sort deterministically: by candidate_id then candidate_type (no timestamps
    # in ordering to keep the artifact reproducible across re-runs)
    updated.sort(key=lambda d: (d.get("candidate_id") or "", d.get("candidate_type") or ""))
    store["decisions"] = updated
    store["decision_count"] = len(updated)
    store["accepted_count"] = sum(1 for d in updated if d.get("decision") == "accepted")
    store["rejected_count"] = sum(1 for d in updated if d.get("decision") == "rejected")

    durable_path.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")

    decision_label = "rejected" if reject else "accepted"
    executable_tag = "" if reject else (
        " [NON-EXECUTABLE until profile RI proof]"
        if candidate_type in _NON_EXECUTABLE_TYPES
        else " [ok]"
    )
    return ApplyDocumentCandidateResult(
        workspace=workspace_rel,
        candidate_id=candidate_id,
        decision=decision_label,
        confirmed_by=confirmed_by,
        accepted_candidates_path=_rel(durable_path, repo_root),
        status="ok",
        note=(
            f"[ok] candidate `{candidate_id}` ({candidate_type}) "
            f"{decision_label} by {confirmed_by}.{executable_tag} "
            f"Recorded in {_rel(durable_path, repo_root)}."
        ),
    )


# ---------------------------------------------------------------------------
# Merge helper (for onboarding integration)
# ---------------------------------------------------------------------------

def merge_accepted_candidates(layout: WorkspaceLayout) -> dict[str, Any]:
    """Return accepted KPI/lexicon/relationship/open-question entries ready for merge.

    This is the integration seam: an onboarding caller reads this dict and
    merges each sub-list into the appropriate generated contract.  This function
    does NOT touch any contract; it only reads accepted_candidates.json.

    data_model_candidate entries are returned with ``executable: False``
    regardless of acceptance, mirroring the BUG-004/023 discipline.

    Returns
    -------
    dict with keys:
        kpi_registry_candidates      list[dict]
        lexicon_candidates           list[dict]
        data_model_candidates        list[dict]  (all non-executable)
        open_question_candidates     list[dict]
        raw_evidence_candidates      list[dict]
        accepted_count               int
        rejected_count               int
        source_path                  str
    """
    durable_path = layout.generated_dir / "documents" / "accepted_candidates.json"

    if not durable_path.exists():
        return _empty_merge_result(str(durable_path))

    # Fail loud (quarantine + raise) on a corrupt store rather than silently
    # returning an empty merge that drops every accepted human decision.
    # Ref: core-audit P6 (documents).
    store = read_json_or_quarantine(durable_path, default={}) or {}

    decisions: list[dict[str, Any]] = store.get("decisions") or []
    accepted = [d for d in decisions if d.get("decision") == "accepted"]

    kpi: list[dict[str, Any]] = []
    lexicon: list[dict[str, Any]] = []
    data_model: list[dict[str, Any]] = []
    open_question: list[dict[str, Any]] = []
    raw_evidence: list[dict[str, Any]] = []

    for entry in accepted:
        ct = str(entry.get("candidate_type") or "")
        # Ensure data_model is always non-executable regardless of what was stored
        if ct in _NON_EXECUTABLE_TYPES:
            entry = dict(entry)
            entry["executable"] = False

        if ct == "kpi_registry_candidate":
            kpi.append(entry)
        elif ct == "lexicon_candidate":
            lexicon.append(entry)
        elif ct == "data_model_candidate":
            data_model.append(entry)
        elif ct == "open_question_candidate":
            open_question.append(entry)
        else:
            raw_evidence.append(entry)

    return {
        "kpi_registry_candidates": kpi,
        "lexicon_candidates": lexicon,
        "data_model_candidates": data_model,
        "open_question_candidates": open_question,
        "raw_evidence_candidates": raw_evidence,
        "accepted_count": len(accepted),
        "rejected_count": sum(1 for d in decisions if d.get("decision") == "rejected"),
        "source_path": str(durable_path),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_durable(path: Path, workspace_rel: str) -> dict[str, Any]:
    """Load the durable accepted_candidates store, or create a fresh skeleton."""
    if path.exists():
        # Fail loud on corruption: a skeleton fallback here would be written back
        # and erase accepted decisions (the durability hole). Quarantine + raise.
        data = read_json_or_quarantine(path, default=None)
        if isinstance(data, dict) and "decisions" in data:
            return data
    return {
        "artifact_type": "accepted_document_candidates",
        "version": ACCEPTED_CANDIDATES_VERSION,
        "generated_by": "apply-document-candidate",
        "workspace": workspace_rel,
        "authoritative_usage_allowed": False,
        "note": (
            "Durable store of human-reviewed document candidate decisions. "
            "Never auto-populated; every entry requires --confirmed-by. "
            "Survives re-onboarding (NOT under contracts/). "
            "data_model_candidate entries are NON-executable until profile RI proof."
        ),
        "governance": {
            "source": "document_pdf",
            "promotion_requires": "human --confirmed-by flag",
            "data_model_executable_requires": "profile RI proof via build-relationship-contracts",
            "never_auto_promotes": True,
        },
        "decisions": [],
        "decision_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
    }


def _empty_merge_result(source_path: str) -> dict[str, Any]:
    return {
        "kpi_registry_candidates": [],
        "lexicon_candidates": [],
        "data_model_candidates": [],
        "open_question_candidates": [],
        "raw_evidence_candidates": [],
        "accepted_count": 0,
        "rejected_count": 0,
        "source_path": source_path,
    }


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


@anchored("apply-document-candidate")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apply-document-candidate",
        description=(
            "Accept or reject a document-derived candidate and record the decision "
            "in a durable artifact (interns/generated/documents/accepted_candidates.json). "
            "--confirmed-by is required for acceptance. "
            "Does NOT mutate kpi_registry.json, workspace_lexicon.json, or any other contract."
        ),
    )
    parser.add_argument(
        "--workspace", required=True,
        help="Workspace path (relative to --repo-root)",
    )
    parser.add_argument(
        "--candidate-id", required=True,
        help="Stable candidate_id from the review panel (candidates.json).",
    )
    parser.add_argument(
        "--confirmed-by", required=True,
        help=(
            "Name/identity of the human reviewer. "
            "REQUIRED for acceptance (records source: human). "
            "Providing an empty string is treated as agent-asserted and refused for promotion."
        ),
    )
    parser.add_argument(
        "--reject", action="store_true",
        help="Reject the candidate instead of accepting it.",
    )
    parser.add_argument(
        "--note", default="",
        help="Optional free-text note (recommended for rejections).",
    )
    parser.add_argument(
        "--repo-root", default=".",
        help="Repository root (default: .)",
    )
    args = parser.parse_args(argv)

    result = apply_document_candidate(
        repo_root=Path(args.repo_root),
        workspace=args.workspace,
        candidate_id=args.candidate_id,
        confirmed_by=args.confirmed_by,
        reject=args.reject,
        note=args.note,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
