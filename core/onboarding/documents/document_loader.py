"""Governed PDF ingestion: scan_document() + Java preflight + review-gated sidecar.

Design: docs/design/pdf_ingestion.md
Pattern: mirrors core/onboarding/data_model/image_parser.py (review-gated sidecar)
Layout:  core/storage/workspace_layout.py (WorkspaceLayout)

Key guarantees
--------------
- free mode (default) is the only local-safe mode; hybrid requires an explicit
  decision artifact and is never enabled by default.
- opendataloader_pdf is imported lazily; if absent the call returns a BLOCKER
  result without raising.
- Java 11+ is detected before any JVM invocation; if missing/incompatible the
  call returns a BLOCKER result without raising.
- PHI redaction (via core.onboarding.kpi.pii_redaction) runs on all extracted
  text/JSON before anything is written under interns/.
- Same PDF + free mode -> identical sidecar JSON hash (excluding timestamps).
- authoritative_usage_allowed is always False by default.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout

# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentScanResult:
    """Return value of scan_document()."""

    workspace: str
    input_path: str
    status: str  # "ok" | "blocker" | "no_op"
    sidecar_path: str
    report_path: str
    mode: str
    source_sha256: str
    engine_version: str
    blocker_reason: str
    next_step: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

SUPPORTED_MODES = {"free", "hybrid"}
_HYBRID_DECISION_ENV = "AUTORESEARCH_ALLOW_HYBRID_PDF"

_PHI_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # SSN: 123-45-6789
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Phone: 123-456-7890 or (123) 456-7890
    re.compile(r"\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}\b"),
    # Email
    re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
)

# Column-name patterns whose *values* are redacted in JSON row objects.
# Reuse pii_redaction logic for structured data; this regex covers free text.
_PHI_COLUMN_NAMES: tuple[str, ...] = (
    "ssn", "first_name", "last_name", "middle_name", "full_name", "name",
    "phone", "phone_number", "email", "email_address", "address", "street",
    "zip", "zip_code", "postal_code", "dob", "date_of_birth", "birth_date",
)


def scan_document(
    repo_root: str | Path,
    workspace: str | Path,
    input_path: str | Path,
    *,
    mode: str = "free",
) -> DocumentScanResult:
    """Scan a PDF and produce a review-gated sidecar + report panel.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.
    workspace:
        Path to the workspace directory (may be relative to repo_root).
    input_path:
        Path to the PDF file to ingest.
    mode:
        "free" (default, deterministic, local-safe) or "hybrid" (requires an
        explicit decision artifact -- never enabled by default).

    Returns
    -------
    DocumentScanResult with status "ok", "blocker", or "no_op".
    """
    repo_root = Path(repo_root).resolve()
    workspace_path = (repo_root / workspace).resolve()
    input_path = Path(input_path).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)

    # -- Validate mode -------------------------------------------------------
    if mode not in SUPPORTED_MODES:
        return _blocker(
            repo_root, workspace_path, input_path, mode,
            reason=f"Unknown mode '{mode}'. Supported: {sorted(SUPPORTED_MODES)}.",
            next_step="Use --mode free (default) or obtain a hybrid decision artifact first.",
        )

    if mode == "hybrid":
        import os
        if not os.environ.get(_HYBRID_DECISION_ENV):
            return _blocker(
                repo_root, workspace_path, input_path, mode,
                reason=(
                    "hybrid mode is non-deterministic and requires an explicit decision artifact. "
                    f"Set {_HYBRID_DECISION_ENV}=1 in the environment after recording the decision."
                ),
                next_step=(
                    f"Record a hybrid-mode decision artifact and set {_HYBRID_DECISION_ENV}=1, "
                    "then re-run. Or use --mode free (default, local-safe)."
                ),
            )

    # -- Input validation ----------------------------------------------------
    if not input_path.exists():
        return _blocker(
            repo_root, workspace_path, input_path, mode,
            reason=f"Input file not found: {_rel(input_path, repo_root)}",
            next_step="Check the --input path and re-run.",
        )

    suffix = input_path.suffix.lower()
    if suffix != ".pdf":
        return _blocker(
            repo_root, workspace_path, input_path, mode,
            reason=f"opendataloader-pdf supports PDF only. Got: {suffix!r}",
            next_step="Supply a .pdf file. For .xlsx use the KPI workbook path; for .png use parse-data-model-images.",
        )

    # -- Source hash (for determinism) ---------------------------------------
    source_sha256 = _sha256(input_path)

    # -- Java preflight ------------------------------------------------------
    java_check = _check_java()
    if java_check["status"] != "ok":
        return _blocker(
            repo_root, workspace_path, input_path, mode,
            reason=java_check["reason"],
            next_step=java_check["next_step"],
        )
    java_version_str = java_check["version_string"]

    # -- Lazy import ---------------------------------------------------------
    try:
        import opendataloader_pdf  # type: ignore[import]
    except ImportError:
        return _blocker(
            repo_root, workspace_path, input_path, mode,
            reason=(
                "opendataloader-pdf is not installed. "
                "Install it with: pip install opendataloader-pdf"
            ),
            next_step="pip install opendataloader-pdf  (deployment step; Java 11+ required)",
        )

    # -- Resolve engine version ----------------------------------------------
    engine_version = _get_engine_version(opendataloader_pdf)

    # -- Prepare output dirs -------------------------------------------------
    layout.ensure_runtime_dirs()
    sidecar_dir = layout.generated_dir / "documents"
    report_dir = layout.reports_dir / "documents"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    # -- Run conversion in a temp dir ----------------------------------------
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            opendataloader_pdf.convert(
                input_path=[str(input_path)],
                output_dir=tmp_dir,
                format="json,markdown",
            )
        except Exception as exc:
            return _blocker(
                repo_root, workspace_path, input_path, mode,
                reason=f"opendataloader_pdf.convert() failed: {exc}",
                next_step="Check Java is reachable and the PDF is not password-protected.",
            )

        # Collect raw JSON and markdown outputs from the temp dir
        tmp_path = Path(tmp_dir)
        json_files = sorted(tmp_path.glob("*.json"))
        md_files = sorted(tmp_path.glob("*.md"))

        raw_json: dict[str, Any] = {}
        if json_files:
            try:
                raw_json = json.loads(json_files[0].read_text(encoding="utf-8"))
            except Exception:
                raw_json = {}

        raw_md = ""
        if md_files:
            try:
                raw_md = md_files[0].read_text(encoding="utf-8")
            except Exception:
                raw_md = ""

    # -- PHI redaction BEFORE writing ----------------------------------------
    redacted_json = _redact_json(raw_json)
    redacted_md = _redact_text(raw_md)

    # -- Build sidecar -------------------------------------------------------
    doc_stem = _safe_stem(input_path)
    sidecar_path = sidecar_dir / f"{doc_stem}.doc.json"
    report_path = report_dir / "current.md"

    sidecar: dict[str, Any] = {
        "version": 1,
        "artifact_type": "document_pdf_sidecar",
        "generated_by": "scan-document",
        "generated_at": _now(),
        "workspace": _rel(workspace_path, repo_root),
        "source_document": _rel(input_path, repo_root),
        "source_sha256": source_sha256,
        "engine_version": engine_version,
        "java_version": java_version_str,
        "mode": mode,
        "phi_redaction_applied": True,
        "authoritative_usage_allowed": False,
        "approval_state": "needs_user_review",
        "extracted_content": redacted_json,
        "promotion_policy": {
            "candidate_usage": (
                "All PDF-derived content is evidence only. Candidates must pass the existing "
                "proof/human gates before any contract is updated."
            ),
            "authoritative_contract": (
                "authoritative_usage_allowed remains False until a human reviewer approves "
                "this sidecar via the workspace review gate."
            ),
        },
        "review_actions": [
            "approve_sidecar_for_candidate_promotion",
            "reject_sidecar",
            "flag_phi_residual",
        ],
        "warnings": _build_warnings(mode, redacted_json),
    }

    # Write sidecar (contains PHI-redacted content)
    sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")

    # Write review panel markdown
    report_path.write_text(
        _render_report(sidecar, redacted_md, repo_root),
        encoding="utf-8",
    )

    return DocumentScanResult(
        workspace=_rel(workspace_path, repo_root),
        input_path=_rel(input_path, repo_root),
        status="ok",
        sidecar_path=_rel(sidecar_path, repo_root),
        report_path=_rel(report_path, repo_root),
        mode=mode,
        source_sha256=source_sha256,
        engine_version=engine_version,
        blocker_reason="",
        next_step=(
            f"Review {_rel(report_path, repo_root)}. "
            "All PDF-derived candidates are non-authoritative until the review gate is cleared."
        ),
    )


# ---------------------------------------------------------------------------
# Java preflight
# ---------------------------------------------------------------------------

_MIN_JAVA_MAJOR = 11


def _check_java() -> dict[str, Any]:
    """Detect Java 11+ on PATH.  Returns dict with status/reason/version_string."""
    java_exe = shutil.which("java")
    if not java_exe:
        return {
            "status": "blocker",
            "reason": (
                "Java not found on PATH. opendataloader-pdf requires Java 11+. "
                "Install a JDK (e.g. OpenJDK 11, 17, or 21) and ensure 'java' is on PATH."
            ),
            "next_step": "Install Java 11+ and re-run scan-document.",
            "version_string": "",
        }

    try:
        # java -version prints to stderr
        completed = subprocess.run(
            [java_exe, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        version_output = (completed.stderr or completed.stdout or "").strip()
    except Exception as exc:
        return {
            "status": "blocker",
            "reason": f"Failed to run 'java -version': {exc}",
            "next_step": "Ensure Java is properly installed and accessible.",
            "version_string": "",
        }

    major = _parse_java_major(version_output)
    if major is None:
        return {
            "status": "blocker",
            "reason": f"Could not parse Java version from output: {version_output[:200]!r}",
            "next_step": "Install Java 11+ and ensure 'java' is on PATH.",
            "version_string": version_output,
        }

    if major < _MIN_JAVA_MAJOR:
        return {
            "status": "blocker",
            "reason": (
                f"Java {major} detected; opendataloader-pdf requires Java {_MIN_JAVA_MAJOR}+. "
                f"Version output: {version_output[:200]!r}"
            ),
            "next_step": f"Upgrade to Java {_MIN_JAVA_MAJOR}+ (OpenJDK 11/17/21 recommended).",
            "version_string": version_output,
        }

    return {
        "status": "ok",
        "reason": "",
        "next_step": "",
        "version_string": version_output,
    }


def _parse_java_major(version_output: str) -> int | None:
    """Parse major Java version from 'java -version' stderr output.

    Handles both old-style (1.8.0_xxx -> 8) and new-style (11.0.x, 17, 21, ...).
    """
    # New-style: "openjdk version \"21.0.1\" ..." or "java version \"11.0.20\""
    # Old-style: "java version \"1.8.0_381\""
    match = re.search(r'"(\d+)(?:\.(\d+))?', version_output)
    if not match:
        return None
    first = int(match.group(1))
    second = match.group(2)
    # Old-style 1.x -> x is the major
    if first == 1 and second is not None:
        return int(second)
    return first


# ---------------------------------------------------------------------------
# PHI redaction helpers
# ---------------------------------------------------------------------------

def _redact_text(text: str) -> str:
    """Apply conservative regex-based PHI redaction to a plain text string,
    then neutralize prompt-injection patterns (extracted document text is
    untrusted and flows into LLM-facing panels/sidecars)."""
    result = text
    for pattern in _PHI_TEXT_PATTERNS:
        result = pattern.sub("<redacted-pii>", result)
    try:
        from core.governance.injection_guard import neutralize_text

        result = neutralize_text(result)
    except ImportError:
        pass
    return result


def _redact_json(obj: Any) -> Any:
    """Recursively walk extracted JSON and redact PHI column values + text strings."""
    if isinstance(obj, dict):
        return _redact_dict(obj)
    if isinstance(obj, list):
        return [_redact_json(item) for item in obj]
    if isinstance(obj, str):
        return _redact_text(obj)
    return obj


def _redact_dict(obj: dict[str, Any]) -> dict[str, Any]:
    """Redact a JSON object.

    If a key is a known PHI column name, replace string values with the
    placeholder. Otherwise recurse.
    """
    # Import the project's PII redaction for column-name matching.
    try:
        from core.onboarding.kpi.pii_redaction import is_pii_column, REDACTION_PLACEHOLDER
    except ImportError:
        # Fallback: use our local column-name check
        is_pii_column = _local_is_pii_column  # type: ignore[assignment]
        REDACTION_PLACEHOLDER = "<redacted-pii>"

    result: dict[str, Any] = {}
    for key, value in obj.items():
        if isinstance(key, str) and is_pii_column(key):
            result[key] = None if value is None else REDACTION_PLACEHOLDER
        else:
            result[key] = _redact_json(value)
    return result


def _local_is_pii_column(column_name: str) -> bool:
    """Fallback PII column detector when pii_redaction module is unavailable."""
    if not isinstance(column_name, str):
        return False
    norm = re.sub(r"[^a-z0-9]+", "", column_name.lower())
    return norm in {re.sub(r"[^a-z0-9]+", "", n) for n in _PHI_COLUMN_NAMES}


# ---------------------------------------------------------------------------
# Engine version extraction
# ---------------------------------------------------------------------------

def _get_engine_version(module: Any) -> str:
    """Extract version string from the opendataloader_pdf module, if available."""
    for attr in ("__version__", "VERSION", "version"):
        val = getattr(module, attr, None)
        if isinstance(val, str) and val:
            return val
    # Try importlib.metadata
    try:
        import importlib.metadata
        return importlib.metadata.version("opendataloader-pdf")
    except Exception:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_stem(path: Path) -> str:
    stem = path.stem.lower()
    return "".join(c if c.isalnum() else "_" for c in stem).strip("_") or "document"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_warnings(mode: str, content: Any) -> list[str]:
    warnings: list[str] = []
    warnings.append("document_derived_content_non_authoritative")
    warnings.append("phi_redaction_applied_before_write")
    if mode == "hybrid":
        warnings.append("hybrid_mode_non_deterministic")
    if not content:
        warnings.append("no_content_extracted")
    return warnings


def _blocker(
    repo_root: Path,
    workspace_path: Path,
    input_path: Path,
    mode: str,
    *,
    reason: str,
    next_step: str,
) -> DocumentScanResult:
    return DocumentScanResult(
        workspace=_rel(workspace_path, repo_root),
        input_path=_rel(input_path, repo_root),
        status="blocker",
        sidecar_path="",
        report_path="",
        mode=mode,
        source_sha256="",
        engine_version="",
        blocker_reason=reason,
        next_step=next_step,
    )


def _render_report(
    sidecar: dict[str, Any],
    redacted_md: str,
    repo_root: Path,
) -> str:
    source = sidecar.get("source_document", "")
    mode = sidecar.get("mode", "")
    engine_version = sidecar.get("engine_version", "")
    sha = sidecar.get("source_sha256", "")
    approval = sidecar.get("approval_state", "")

    lines = [
        f"# Document Review Panel: {source}",
        "",
        f"- Source: `{source}`",
        f"- Mode: `{mode}`",
        f"- Engine version: `{engine_version}`",
        f"- Source SHA-256: `{sha}`",
        f"- Approval state: `{approval}`",
        f"- authoritative usage allowed: `False`",
        f"- PHI redaction applied: `True`",
        "",
        "## Governance",
        "",
        "- All PDF-derived content is *evidence only*; nothing auto-promotes to any contract.",
        "- Candidates from this document require profile/catalog proof or explicit human approval.",
        "- PHI redaction was applied to all extracted text and JSON before persistence.",
        "",
        "## Warnings",
        "",
    ]
    for warning in sidecar.get("warnings", []):
        lines.append(f"- `{warning}`")
    lines.extend([
        "",
        "## Extracted Content (Markdown preview, PHI-redacted)",
        "",
        redacted_md[:4000] if redacted_md else "_No markdown content extracted._",
        "",
        "## Review Actions",
        "",
        "- `approve_sidecar_for_candidate_promotion`",
        "- `reject_sidecar`",
        "- `flag_phi_residual`",
        "",
        "## Next Step",
        "",
        (
            "Review extracted content above. After approval, run "
            "`classify_document()` to route candidates into review-gated "
            "kpi_registry, lexicon, data-model, or open-question targets."
        ),
    ])
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


@anchored("scan-document")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scan-document",
        description="Scan a PDF and produce a review-gated sidecar + report panel.",
    )
    parser.add_argument("--workspace", required=True, help="Workspace path (relative to --repo-root)")
    parser.add_argument("--input", required=True, help="Path to the PDF file")
    parser.add_argument("--repo-root", default=".", help="Repository root (default: .)")
    parser.add_argument(
        "--mode",
        choices=sorted(SUPPORTED_MODES),
        default="free",
        help=(
            "Extraction mode. 'free' (default) is deterministic and local-safe. "
            "'hybrid' requires an explicit decision artifact."
        ),
    )
    args = parser.parse_args(argv)
    result = scan_document(
        repo_root=Path(args.repo_root),
        workspace=args.workspace,
        input_path=Path(args.input),
        mode=args.mode,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.status in {"ok", "no_op"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
