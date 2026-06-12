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

# ---------------------------------------------------------------------------
# PCI DSS cardholder-data column-name patterns (anchored, case-insensitive).
# Same workspace-agnostic, column-NAME-based approach as the HIPAA set: no raw
# values are ever read. Covers PAN, verification codes, expiry, cardholder
# name, and adjacent bank-account identifiers.
# ---------------------------------------------------------------------------

PCI_IDENTIFIER_PATTERNS: dict[str, tuple[str, ...]] = {
    "primary_account_number": (
        r"^pan$", r"^primary[_ ]?account[_ ]?number$",
        r"^(credit[_ ]?|debit[_ ]?)?card[_ ]?(no|num|number)$",
        r"^cc[_ ]?(no|num|number)$",
        r"^credit[_ ]?card$",
    ),
    "card_verification": (
        r"^cvv2?$", r"^cvc2?$", r"^cid$",
        r"^card[_ ]?verification([_ ]?(code|value|no|number))?$",
        r"^security[_ ]?code$",
    ),
    "card_expiry": (
        # Card-scoped only: a bare "expiry_date" is too generic to block on
        # (drug expiry, membership expiry, ...). "exp_month"/"exp_year" are
        # card-vocabulary specific.
        r"^card[_ ]?expir(y|ation)([_ ]?(date|month|year))?$",
        r"^exp[_ ]?(month|year)$",
    ),
    "cardholder_name": (r"^card[_ ]?holder([_ ]?name)?$",),
    "card_track_data": (r"^track[12]?[_ ]?data$", r"^magstripe([_ ]?data)?$"),
    "bank_account": (
        r"^iban$", r"^routing[_ ]?(no|num|number)$",
        r"^bank[_ ]?account([_ ]?(no|num|number))?$",
        r"^(aba|swift|bic)([_ ]?(code|no|number))?$",
    ),
}

_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    category: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    for category, patterns in HIPAA_IDENTIFIER_PATTERNS.items()
}

_COMPILED_PCI: dict[str, tuple[re.Pattern[str], ...]] = {
    category: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    for category, patterns in PCI_IDENTIFIER_PATTERNS.items()
}


def _match_category(
    column_name: str, compiled: dict[str, tuple[re.Pattern[str], ...]]
) -> str | None:
    if not isinstance(column_name, str) or not column_name.strip():
        return None
    name = column_name.strip()
    for category, regexes in compiled.items():
        for regex in regexes:
            if regex.match(name):
                return category
    return None


def identifier_category(column_name: str) -> str | None:
    """Return the HIPAA identifier category a column name matches, else None."""
    return _match_category(column_name, _COMPILED)


def pci_identifier_category(column_name: str) -> str | None:
    """Return the PCI identifier category a column name matches, else None."""
    return _match_category(column_name, _COMPILED_PCI)


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


def _iter_profile_columns(profile_index: dict[str, Any] | None):
    """Yield (dataset, column) pairs from a profile_index.json payload."""
    if not isinstance(profile_index, dict):
        return
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
            yield dataset, column


def detect_phi_columns(profile_index: dict[str, Any] | None) -> list[PHIFinding]:
    """Scan a profile_index.json payload for HIPAA-identifier columns (names only)."""
    findings: list[PHIFinding] = []
    for dataset, column in _iter_profile_columns(profile_index):
        category = identifier_category(column)
        if category:
            findings.append(PHIFinding(dataset=dataset, column=column, identifier_category=category))
    return findings


def detect_pci_columns(profile_index: dict[str, Any] | None) -> list[PHIFinding]:
    """Scan a profile_index.json payload for PCI cardholder-data columns (names only)."""
    findings: list[PHIFinding] = []
    for dataset, column in _iter_profile_columns(profile_index):
        category = pci_identifier_category(column)
        if category:
            findings.append(PHIFinding(dataset=dataset, column=column, identifier_category=category))
    return findings


def _load_profile_index(layout: Any) -> dict[str, Any] | None:
    path = layout.profiles_dir / "profile_index.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def assess_workspace_phi(layout: Any) -> PHIAssessment:
    """Assess a workspace's PHI tier from its profile_index.json.

    Missing/unreadable profile -> tier 'none' (cannot assert PHI without
    evidence). Any HIPAA-identifier column -> tier 'phi'.
    """
    profile_index = _load_profile_index(layout)
    if profile_index is None:
        return PHIAssessment(tier="none")
    findings = detect_phi_columns(profile_index)
    datasets = sorted({f.dataset for f in findings if f.dataset})
    return PHIAssessment(
        tier="phi" if findings else "none",
        findings=findings,
        datasets=datasets,
    )


def assess_workspace_pci(layout: Any) -> PHIAssessment:
    """Assess a workspace's PCI tier from its profile_index.json.

    Missing/unreadable profile -> tier 'none'. Any PCI cardholder-data
    column -> tier 'pci'.
    """
    profile_index = _load_profile_index(layout)
    if profile_index is None:
        return PHIAssessment(tier="none")
    findings = detect_pci_columns(profile_index)
    datasets = sorted({f.dataset for f in findings if f.dataset})
    return PHIAssessment(
        tier="pci" if findings else "none",
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


def databricks_pci_covered(cfg: Any) -> bool:
    """True only when the configured Databricks target is attested PCI DSS
    in-scope for cardholder data. Defaults to False -- same posture as PHI:
    a trial / default workspace is NOT a compliant cardholder-data environment.
    """
    db = getattr(cfg, "databricks", None)
    if db is None:
        db = cfg
    return bool(getattr(db, "pci_covered", False))


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


def enforce_remote_pci_gate(
    layout: Any,
    cfg: Any,
    *,
    operation: str = "remote_upload",
    deidentified: bool = False,
) -> StructuredFailure | None:
    """Return a blocking StructuredFailure when cardholder data would reach a
    non-PCI-compliant remote target; None when clear to proceed.

    BLOCK when: workspace tier == 'pci' AND target is not PCI-covered AND the
    data is not de-identified (tokenized/truncated per PCI DSS).
    """
    assessment = assess_workspace_pci(layout)
    if assessment.tier != "pci":
        return None
    if deidentified:
        return None
    if databricks_pci_covered(cfg):
        return None

    categories = sorted({f.identifier_category for f in assessment.findings})
    sample = ", ".join(
        f"{f.dataset}:{f.column}" for f in assessment.findings[:6]
    )
    return remote_denied(
        f"pci_gate.{operation}",
        (
            "PCI gate: refused — this workspace contains cardholder-data "
            f"column(s) [{', '.join(categories)}] (e.g. {sample}) and the "
            "configured Databricks target is NOT an attested PCI DSS "
            "environment. Pushing cardholder data to a non-compliant target "
            "violates PCI DSS scope controls."
        ),
        next_command=(
            "Use local DuckDB (safe), tokenize/truncate the cardholder data and "
            "re-run, OR set [databricks].pci_covered = true in config/lock.toml "
            "only after the target is attested PCI DSS in-scope."
        ),
    )


def enforce_remote_sensitive_gate(
    layout: Any,
    cfg: Any,
    *,
    operation: str = "remote_upload",
    deidentified: bool = False,
) -> StructuredFailure | None:
    """Combined sensitive-data gate: PHI first, then PCI. First block wins.

    This is the single entry point remote upload/exec paths should call so
    new sensitive-data categories gate every remote path automatically.
    """
    failure = enforce_remote_phi_gate(
        layout, cfg, operation=operation, deidentified=deidentified
    )
    if failure is not None:
        return failure
    return enforce_remote_pci_gate(
        layout, cfg, operation=operation, deidentified=deidentified
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

    pci_assessment = assess_workspace_pci(layout)
    covered = databricks_phi_covered(cfg) if cfg is not None else False
    pci_covered = databricks_pci_covered(cfg) if cfg is not None else False
    blocked = assessment.is_phi and not covered
    pci_blocked = pci_assessment.tier == "pci" and not pci_covered
    payload = {
        **assessment.to_dict(),
        "databricks_phi_covered": covered,
        "remote_upload_blocked": blocked,
        "pci": {
            **pci_assessment.to_dict(),
            "databricks_pci_covered": pci_covered,
            "remote_upload_blocked": pci_blocked,
        },
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
    if pci_assessment.tier == "pci":
        marker = "[blocked]" if pci_blocked else "[ok]"
        print(
            f"{marker} PCI tier with {len(pci_assessment.findings)} cardholder-data column(s); "
            f"non-covered remote upload/exec is {'BLOCKED' if pci_blocked else 'allowed (covered)'}."
        )
    else:
        print("[ok] No PCI cardholder-data columns detected; PCI gate is not triggered.")
    return 0


__all__ = [
    "HIPAA_IDENTIFIER_PATTERNS",
    "PCI_IDENTIFIER_PATTERNS",
    "identifier_category",
    "pci_identifier_category",
    "PHIFinding",
    "PHIAssessment",
    "detect_phi_columns",
    "detect_pci_columns",
    "assess_workspace_phi",
    "assess_workspace_pci",
    "databricks_phi_covered",
    "databricks_pci_covered",
    "enforce_remote_phi_gate",
    "enforce_remote_pci_gate",
    "enforce_remote_sensitive_gate",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
