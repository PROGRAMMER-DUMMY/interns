"""Governed document candidate review panel (Phase 3: promote path, step 1).

Reads `interns/generated/documents/candidates.json` produced by onboard-workspace
(via classify_document) and writes a human-review panel:

  interns/reports/documents/candidates.md    -- display panel (render verbatim)
  interns/reports/documents/candidates.json  -- machine-readable panel (stable IDs)

The panel is DISPLAY-ONLY; it does not promote, mutate, or apply anything.
Promotion is done by apply-document-candidate (candidate_apply.py).

Panel style mirrors blocker_question_panel (ASCII markers, no emojis, no freehand
prompts).  Each candidate gets a stable `candidate_id` derived deterministically
from its type + source_document + page + content hash so the IDs survive re-runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import textwrap
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout

# ---------------------------------------------------------------------------
# Routing-target labels (mirrors pdf_ingestion.md Section 6)
# ---------------------------------------------------------------------------

_ROUTE_LABELS: dict[str, str] = {
    "kpi_registry_candidate": "kpi_registry.json (proposed)",
    "lexicon_candidate": "workspace_lexicon.json (proposed)",
    "data_model_candidate": "build-relationship-contracts (profile RI proof still required)",
    "open_question_candidate": "open_questions.md / semantic_contract.json (proposed)",
    "raw_evidence": "(no auto-route; raw evidence only)",
}

_CANDIDATE_TYPES_ORDERED = [
    "kpi_registry_candidate",
    "lexicon_candidate",
    "data_model_candidate",
    "open_question_candidate",
    "raw_evidence",
]

PANEL_VERSION = 1


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

class DocumentCandidateReviewResult:
    """Return value of prepare_document_candidate_review()."""

    __slots__ = (
        "workspace",
        "candidates_json_path",
        "panel_json_path",
        "panel_md_path",
        "candidate_count",
        "status",
        "note",
    )

    def __init__(
        self,
        *,
        workspace: str,
        candidates_json_path: str,
        panel_json_path: str,
        panel_md_path: str,
        candidate_count: int,
        status: str,
        note: str,
    ) -> None:
        self.workspace = workspace
        self.candidates_json_path = candidates_json_path
        self.panel_json_path = panel_json_path
        self.panel_md_path = panel_md_path
        self.candidate_count = candidate_count
        self.status = status
        self.note = note

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "candidates_json_path": self.candidates_json_path,
            "panel_json_path": self.panel_json_path,
            "panel_md_path": self.panel_md_path,
            "candidate_count": self.candidate_count,
            "status": self.status,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def prepare_document_candidate_review(
    repo_root: str | Path,
    workspace: str | Path,
) -> DocumentCandidateReviewResult:
    """Read candidates.json and write the human-review panel files.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    workspace:
        Path to the workspace directory (may be relative to repo_root).

    Returns
    -------
    DocumentCandidateReviewResult with panel artifact paths and candidate_count.
    """
    repo_root = Path(repo_root).resolve()
    workspace_path = (repo_root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    workspace_rel = _rel(workspace_path, repo_root)

    candidates_json = layout.generated_dir / "documents" / "candidates.json"

    if not candidates_json.exists():
        return DocumentCandidateReviewResult(
            workspace=workspace_rel,
            candidates_json_path=str(candidates_json),
            panel_json_path="",
            panel_md_path="",
            candidate_count=0,
            status="no_candidates",
            note=(
                "candidates.json not found. "
                "Run `uv run onboard-workspace --workspace <workspace>` first "
                "to generate PDF-derived candidates."
            ),
        )

    raw = json.loads(candidates_json.read_text(encoding="utf-8"))
    raw_candidates: list[dict[str, Any]] = raw.get("candidates") or []

    # Enrich each candidate with a stable candidate_id
    enriched: list[dict[str, Any]] = []
    for cand in raw_candidates:
        enriched.append(_enrich(cand))

    # Write output dir
    report_dir = layout.reports_dir / "documents"
    report_dir.mkdir(parents=True, exist_ok=True)

    panel_json_path = report_dir / "candidates.json"
    panel_md_path = report_dir / "candidates.md"

    panel_json: dict[str, Any] = {
        "artifact_type": "document_candidate_review_panel",
        "version": PANEL_VERSION,
        "generated_by": "prepare-document-candidate-review",
        "workspace": workspace_rel,
        "source_candidates_json": _rel(candidates_json, repo_root),
        "authoritative_usage_allowed": False,
        "interaction_contract": {
            "display_mode": "document_candidate_review_panel",
            "primary_artifact": "candidates.md",
            "answer_source": "candidates.json",
            "generic_answer_picker_allowed": False,
            "instruction": (
                "Render candidates.md verbatim or list candidates from candidates.json. "
                "Do NOT add options outside this panel. "
                "Promote only via `apply-document-candidate --candidate-id <id> --confirmed-by <name>`."
            ),
        },
        "candidate_count": len(enriched),
        "candidates": enriched,
    }

    panel_json_path.write_text(
        json.dumps(panel_json, indent=2) + "\n", encoding="utf-8"
    )
    panel_md_path.write_text(
        _render_markdown(workspace_rel, enriched, _rel(candidates_json, repo_root)),
        encoding="utf-8",
    )

    return DocumentCandidateReviewResult(
        workspace=workspace_rel,
        candidates_json_path=_rel(candidates_json, repo_root),
        panel_json_path=_rel(panel_json_path, repo_root),
        panel_md_path=_rel(panel_md_path, repo_root),
        candidate_count=len(enriched),
        status="ok",
        note=(
            f"{len(enriched)} candidate(s) listed. "
            "Review the panel and promote with "
            "`apply-document-candidate --candidate-id <id> --confirmed-by <reviewer>`."
        ),
    )


# ---------------------------------------------------------------------------
# candidate_id derivation (stable, deterministic)
# ---------------------------------------------------------------------------

def _stable_candidate_id(candidate: dict[str, Any]) -> str:
    """Derive a stable, deterministic candidate_id from immutable fields.

    Combines: candidate_type + source_document + page + repr(content).
    Uses the first 12 hex chars of SHA-256 so IDs are short but collision-safe
    within a workspace.
    """
    key_parts = "|".join([
        str(candidate.get("candidate_type") or ""),
        str(candidate.get("source_document") or candidate.get("source") or ""),
        str(candidate.get("page") or ""),
        repr(candidate.get("content") or {}),
    ])
    digest = hashlib.sha256(key_parts.encode("utf-8")).hexdigest()
    return f"doc_cand_{digest[:12]}"


def _enrich(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the candidate with candidate_id and routing_target added."""
    enriched = dict(candidate)
    enriched["candidate_id"] = _stable_candidate_id(candidate)
    enriched["routing_target"] = _ROUTE_LABELS.get(
        str(candidate.get("candidate_type") or ""),
        "(unknown route)",
    )
    # Provenance convenience fields surfaced at top level for panel display
    enriched.setdefault("source_document", candidate.get("source_document") or "")
    return enriched


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _render_markdown(
    workspace_rel: str,
    candidates: list[dict[str, Any]],
    source_path: str,
) -> str:
    lines: list[str] = []
    lines += [
        f"# Document Candidate Review Panel",
        "",
        f"Workspace: `{workspace_rel}`",
        f"Source: `{source_path}`",
        f"Candidate count: {len(candidates)}",
        "",
        "All candidates are **propose-only** (`authoritative: False`). "
        "Nothing is promoted automatically.",
        "",
        "To accept a candidate:",
        "",
        "```",
        "apply-document-candidate --workspace <ws> --candidate-id <id> --confirmed-by <reviewer>",
        "```",
        "",
        "To reject a candidate:",
        "",
        "```",
        "apply-document-candidate --workspace <ws> --candidate-id <id> --confirmed-by <reviewer> --reject --note \"<why>\"",
        "```",
        "",
        "---",
        "",
    ]

    if not candidates:
        lines.append("_No candidates found._")
        return "\n".join(lines).rstrip() + "\n"

    # Group by candidate_type for readability
    by_type: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        ct = str(c.get("candidate_type") or "unknown")
        by_type.setdefault(ct, []).append(c)

    # Render in the standard routing order
    ordered_types = [t for t in _CANDIDATE_TYPES_ORDERED if t in by_type]
    remaining = [t for t in by_type if t not in ordered_types]
    for ct in ordered_types + remaining:
        group = by_type[ct]
        route = _ROUTE_LABELS.get(ct, "(unknown route)")
        lines += [
            f"## {ct} ({len(group)})",
            "",
            f"Routing target: {route}",
            "",
        ]
        for idx, cand in enumerate(group, start=1):
            cid = cand.get("candidate_id", "?")
            page = cand.get("page")
            source_doc = cand.get("source_document") or ""
            confidence = cand.get("confidence", "?")
            reason = cand.get("reason") or ""
            bbox = cand.get("bbox")
            content = cand.get("content") or {}

            lines += [
                f"### [{idx}] `{cid}`",
                "",
                f"- candidate_id: `{cid}`",
                f"- type: `{ct}`",
                f"- confidence: `{confidence}`",
                f"- source_document: `{source_doc}`",
                f"- page: `{page}`",
            ]
            if bbox:
                lines.append(f"- bbox: `{bbox}`")
            lines += [
                f"- routing_target: {route}",
                f"- reason: {reason}",
                "",
            ]

            # Show a trimmed content summary
            if content:
                content_str = json.dumps(content, ensure_ascii=False)
                if len(content_str) > 400:
                    content_str = content_str[:397] + "..."
                # indent as a code block
                lines += [
                    "**Content (sample)**:",
                    "",
                    "```json",
                    content_str,
                    "```",
                    "",
                ]

            if ct == "data_model_candidate":
                lines += [
                    "> [~] data_model_candidate: profile RI proof still required "
                    "before any join is executable (BUG-004/023 + low-cardinality confidence cap). "
                    "Accepting this candidate records it as NON-executable evidence only.",
                    "",
                ]

            lines.append("---")
            lines.append("")

    lines += [
        "## Governance",
        "",
        "- `authoritative_usage_allowed: False` for all PDF-derived candidates.",
        "- Candidates must pass existing proof/human gates before any contract is updated.",
        "- `source: document_pdf` — treated as agent-asserted evidence by gate-provenance and "
        "intent-coverage harnesses until a human confirms.",
        "- `data_model_candidate` entries require profile RI proof even after human acceptance.",
        "",
    ]

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prepare-document-candidate-review",
        description=(
            "Read candidates.json and write a human-review panel "
            "(interns/reports/documents/candidates.md + candidates.json). "
            "Display-only; does not promote or mutate any contract."
        ),
    )
    parser.add_argument("--workspace", required=True, help="Workspace path (relative to --repo-root)")
    parser.add_argument("--repo-root", default=".", help="Repository root (default: .)")
    args = parser.parse_args(argv)

    result = prepare_document_candidate_review(
        repo_root=Path(args.repo_root),
        workspace=args.workspace,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.status in {"ok", "no_candidates"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
