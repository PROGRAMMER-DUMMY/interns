"""PHI gate: identifier detection -> PHI tier -> block non-covered remote upload/exec.

The systemic guardrail behind the 2026-06 incident (real PHI uploaded into a
non-HIPAA Databricks trial). It is the *enforced* counterpart to the advisory
display-only `pii_redaction` helper:

  1. ``detect_phi_columns`` / ``assess_workspace_phi`` -- scan a workspace's
     profile schema for HIPAA-18 identifier columns (column-NAME based, so no
     raw PHI values are read). Derived from workspace evidence, not curated per
     workspace -- workspace-agnostic.
  2. ``PHITier`` -- a data-understanding tier (``none`` | ``phi``).
  3. ``databricks_phi_covered`` -- is the configured Databricks target
     HIPAA-covered (BAA / compliance security profile)? A trial is NOT covered.
  4. ``enforce_remote_phi_gate`` -- returns a blocking StructuredFailure when a
     PHI workspace would be pushed to a non-covered remote target and the data
     is not de-identified. ``None`` means clear-to-proceed.

LOCAL execution (DuckDB) is always allowed -- PHI never leaves the box there.
Only NON-COVERED REMOTE upload/exec is gated.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.failures import StructuredFailure, remote_denied

# ---------------------------------------------------------------------------
# HIPAA-18 identifier column-name patterns (anchored, case-insensitive).
# Keyed by identifier category. Values are generic HIPAA categories, NOT
# workspace/domain vocabulary -- the gate stays workspace-agnostic.
# ---------------------------------------------------------------------------

HIPAA_IDENTIFIER_PATTERNS: dict[str, tuple[str, ...]] = {
    "name": (r"^(first|last|middle|full|patient|member|provider|guarantor)?[_ ]?name$",),
    "ssn": (r"^ssn$", r"^social[_ ]?security([_ ]?(no|number))?$"),
    "date_of_birth": (r"^dob$", r"^date[_ ]?of[_ ]?birth$", r"^birth[_ ]?date$"),
    "phone": (r"^(phone|mobile|cell|fax)([_ ]?(no|number))?$",),
    "email": (r"^e[_ ]?mail([_ ]?address)?$",),
    "address": (
        r"^address(_?line\d*)?$", r"^street$", r"^city$",
        r"^zip([_ ]?code)?$", r"^postal([_ ]?code)?$",
    ),
    "medical_record_number": (r"^mrn$", r"^medical[_ ]?record([_ ]?(no|number))?$"),
    "account_number": (r"^account([_ ]?(no|number))?$", r"^acct([_ ]?(no|number))?$"),
    "health_plan_beneficiary": (
        r"^medicaid([_ ]?id)?$", r"^medicare([_ ]?id)?$",
        r"^beneficiary([_ ]?id)?$", r"^subscriber([_ ]?id)?$",
        r"^health[_ ]?plan([_ ]?id)?$",
    ),
    "certificate_or_license": (
        r"^(license|licence|certificate)([_ ]?(no|number))?$", r"^npi$",
    ),
    "device_identifier": (r"^device([_ ]?(id|serial))?$", r"^serial([_ ]?(no|number))?$"),
    "ip_address": (r"^ip([_ ]?address)?$",),
    "url": (r"^url$", r"^website$"),
    "vehicle_identifier": (r"^vin$", r"^(license[_ ]?)?plate([_ ]?(no|number))?$"),
    "biometric": (r"^(fingerprint|biometric|retina|voiceprint)([_ ]?id)?$",),
}

_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    category: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    for category, patterns in HIPAA_IDENTIFIER_PATTERNS.items()
}


def identifier_category(column_name: str) -> str | None:
    """Return the HIPAA identifier category a column name matches, else None."""
    if not isinstance(column_name, str) or not column_name.strip():
        return None
    name = column_name.strip()
    for category, regexes in _COMPILED.items():
        for regex in regexes:
            if regex.match(name):
                return category
    return None


@dataclass(frozen=True)
class PHIFinding:
    dataset: str
    column: str
    identifier_category: str

    def to_dict(self) -> dict[str, str]:
        return {
            "dataset": self.dataset,
            "column": self.column,
            "identifier_category": self.identifier_category,
        }


@dataclass(frozen=True)
class PHIAssessment:
    tier: str  # "none" | "phi"
    findings: list[PHIFinding] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)

    @property
    def is_phi(self) -> bool:
        return self.tier == "phi"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "is_phi": self.is_phi,
            "finding_count": len(self.findings),
            "datasets": sorted(self.datasets),
            "findings": [f.to_dict() for f in self.findings],
        }


def detect_phi_columns(profile_index: dict[str, Any] | None) -> list[PHIFinding]:
    """Scan a profile_index.json payload for HIPAA-identifier columns (names only)."""
    if not isinstance(profile_index, dict):
        return []
    findings: list[PHIFinding] = []
    for profile in profile_index.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        dataset = str(profile.get("path") or profile.get("dataset") or "")
        schema = profile.get("schema")
        columns: list[str]
        if isinstance(schema, dict):
            columns = [c for c in schema.keys() if isinstance(c, str)]
        elif isinstance(schema, list):
            columns = [
                str(c.get("name"))
                for c in schema
                if isinstance(c, dict) and c.get("name")
            ]
        else:
            columns = []
        for column in columns:
            category = identifier_category(column)
            if category:
                findings.append(PHIFinding(dataset=dataset, column=column, identifier_category=category))
    return findings


def assess_workspace_phi(layout: Any) -> PHIAssessment:
    """Assess a workspace's PHI tier from its profile_index.json.

    Missing/unreadable profile -> tier 'none' (cannot assert PHI without
    evidence). Any HIPAA-identifier column -> tier 'phi'.
    """
    path = layout.profiles_dir / "profile_index.json"
    if not path.exists():
        return PHIAssessment(tier="none")
    try:
        profile_index = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return PHIAssessment(tier="none")
    findings = detect_phi_columns(profile_index)
    datasets = sorted({f.dataset for f in findings if f.dataset})
    return PHIAssessment(
        tier="phi" if findings else "none",
        findings=findings,
        datasets=datasets,
    )


def databricks_phi_covered(cfg: Any) -> bool:
    """True only when the configured Databricks target is HIPAA-covered.

    Accepts either a full Config (has ``.databricks``) or a DatabricksConfig
    directly (has ``.phi_covered``). Defaults to False -- a trial / default
    workspace has no BAA and no compliance security profile, so it is NOT
    HIPAA-covered.
    """
    db = getattr(cfg, "databricks", None)
    if db is None:
        db = cfg  # cfg may itself be the DatabricksConfig
    return bool(getattr(db, "phi_covered", False))


def enforce_remote_phi_gate(
    layout: Any,
    cfg: Any,
    *,
    operation: str = "remote_upload",
    deidentified: bool = False,
) -> StructuredFailure | None:
    """Return a blocking StructuredFailure when PHI would reach a non-covered
    remote target; None when clear to proceed.

    BLOCK when: workspace tier == 'phi' AND target is not HIPAA-covered AND the
    data is not de-identified. De-identified data (Safe Harbor / Expert
    Determination) and BAA-covered targets are allowed.
    """
    assessment = assess_workspace_phi(layout)
    if not assessment.is_phi:
        return None
    if deidentified:
        return None
    if databricks_phi_covered(cfg):
        return None

    categories = sorted({f.identifier_category for f in assessment.findings})
    sample = ", ".join(
        f"{f.dataset}:{f.column}" for f in assessment.findings[:6]
    )
    return remote_denied(
        f"phi_gate.{operation}",
        (
            "PHI gate: refused — this workspace contains HIPAA identifier "
            f"column(s) [{', '.join(categories)}] (e.g. {sample}) and the "
            "configured Databricks target is NOT HIPAA-covered (no BAA / no "
            "compliance security profile). Pushing identifiable PHI to a "
            "non-covered target is a reportable breach."
        ),
        next_command=(
            "Use local DuckDB (safe), de-identify the data (Safe Harbor / Expert "
            "Determination) and re-run, OR set [databricks].phi_covered = true in "
            "config/lock.toml only after a signed BAA + compliance security profile "
            "are in place."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: assess-workspace-phi --workspace <ws>.

    Prints the PHI assessment + whether non-covered remote upload/exec would be
    blocked. Read-only; never reads raw data values (column names only).
    """
    import argparse

    from core.storage.workspace_layout import WorkspaceLayout

    parser = argparse.ArgumentParser(
        prog="assess-workspace-phi",
        description="Assess a workspace's HIPAA-identifier (PHI) exposure from its profiles.",
    )
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root")
    parser.add_argument("--repo-root", default=".", help="Repository root (default: .)")
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    workspace_path = (root / args.workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    assessment = assess_workspace_phi(layout)

    try:
        from core.config import load as load_config

        cfg = load_config()
    except Exception:
        cfg = None

    covered = databricks_phi_covered(cfg) if cfg is not None else False
    blocked = assessment.is_phi and not covered
    payload = {
        **assessment.to_dict(),
        "databricks_phi_covered": covered,
        "remote_upload_blocked": blocked,
    }
    print(json.dumps(payload, indent=2))
    if assessment.is_phi:
        marker = "[blocked]" if blocked else "[ok]"
        print(
            f"{marker} PHI tier with {len(assessment.findings)} identifier column(s); "
            f"non-covered remote upload/exec is {'BLOCKED' if blocked else 'allowed (covered)'}."
        )
    else:
        print("[ok] No HIPAA-identifier columns detected; PHI gate is not triggered.")
    return 0


__all__ = [
    "HIPAA_IDENTIFIER_PATTERNS",
    "identifier_category",
    "PHIFinding",
    "PHIAssessment",
    "detect_phi_columns",
    "assess_workspace_phi",
    "databricks_phi_covered",
    "enforce_remote_phi_gate",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
