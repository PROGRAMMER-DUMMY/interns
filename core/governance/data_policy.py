"""User-supplied workspace data policy (PII / PHI / PCI / custom).

A workspace owner can drop a ``data_policy.json`` at the workspace root (or
under ``docs/``) declaring THEIR data-protection rules on top of the built-in
HIPAA-18 / PCI detection in ``core.governance.phi_gate`` and the display
redaction in ``core.onboarding.kpi.pii_redaction``:

    {
      "artifact_type": "workspace_data_policy.json",
      "version": 1,
      "sensitive_columns": ["LoyaltyCode", "InternalRiskScore"],
      "sensitive_column_patterns": ["^emp(loyee)?[_ ]?id$"],
      "categories": {"trade_secret": ["^route[_ ]?cost[_ ]?model$"]},
      "not_sensitive_columns": ["Name"],
      "tier_override": "phi",
      "aggregate_ages_over_89": true,
      "notes": "Loyalty codes are contractually confidential."
    }

Semantics:
- ``sensitive_columns``      exact column names (case-insensitive) to protect.
- ``sensitive_column_patterns``  anchored regexes matched case-insensitively.
- ``categories``             named custom categories -> regex lists; findings
                             carry ``policy:<category>`` so reports show WHY.
- ``not_sensitive_columns``  allowlist: suppress a built-in detection the
                             owner has reviewed (e.g. ``Name`` on a product
                             table). Applies to built-in findings only — a
                             column cannot be both declared and allowlisted.
- ``tier_override``          force the workspace PHI tier (``"phi"`` to treat
                             everything as PHI even if no identifier matched;
                             ``"none"`` is intentionally NOT honored for
                             downgrades when identifiers were detected — the
                             allowlist is the per-column escape hatch).
- ``aggregate_ages_over_89`` ``true`` (default) keeps the HIPAA Safe Harbor rule
                             that renders ages over 89 as ``"90+"`` on displayed
                             tables. An owner whose data is synthetic / not real
                             PHI may set it ``false`` to show exact ages. Opt-in
                             only: absent ⇒ the safeguard stays on.

The policy is user-authored input (like datasets), never a generated artifact:
generators must not write it, and validation reports problems instead of
"fixing" the file.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POLICY_FILENAME = "data_policy.json"
POLICY_SEARCH_SUBDIRS: tuple[str, ...] = ("", "docs")
POLICY_CATEGORY_PREFIX = "policy:"


@dataclass(frozen=True)
class WorkspaceDataPolicy:
    source_path: str
    sensitive_columns: tuple[str, ...] = ()
    sensitive_column_patterns: tuple[str, ...] = ()
    categories: dict[str, tuple[str, ...]] = field(default_factory=dict)
    not_sensitive_columns: tuple[str, ...] = ()
    tier_override: str = ""
    # HIPAA Safe Harbor aggregates ages over 89 to "90+" on rendered tables.
    # True (default) keeps that safeguard on; an owner whose data is synthetic /
    # not real PHI can set it False to render exact ages. Opt-in only — the safe
    # default stays on for every workspace that does not declare otherwise.
    aggregate_ages_over_89: bool = True
    notes: str = ""
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def is_empty(self) -> bool:
        return not (
            self.sensitive_columns
            or self.sensitive_column_patterns
            or self.categories
            or self.not_sensitive_columns
            or self.tier_override
            or not self.aggregate_ages_over_89
        )

    def summary(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "sensitive_column_count": len(self.sensitive_columns),
            "sensitive_pattern_count": len(self.sensitive_column_patterns),
            "custom_categories": sorted(self.categories),
            "allowlisted_column_count": len(self.not_sensitive_columns),
            "tier_override": self.tier_override,
            "aggregate_ages_over_89": self.aggregate_ages_over_89,
            "errors": list(self.errors),
        }


def _str_tuple(value: Any, *, errors: list[str], key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"`{key}` must be a list of strings")
        return ()
    return tuple(v.strip() for v in value if v.strip())


def _validate_patterns(patterns: tuple[str, ...], *, errors: list[str], key: str) -> tuple[str, ...]:
    valid: list[str] = []
    for pattern in patterns:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            errors.append(f"`{key}` pattern {pattern!r} is not a valid regex: {exc}")
            continue
        valid.append(pattern)
    return tuple(valid)


def find_policy_path(workspace_root: str | Path) -> Path | None:
    root = Path(workspace_root)
    for subdir in POLICY_SEARCH_SUBDIRS:
        candidate = (root / subdir / POLICY_FILENAME) if subdir else (root / POLICY_FILENAME)
        if candidate.is_file():
            return candidate
    return None


def load_workspace_data_policy(workspace_root: str | Path) -> WorkspaceDataPolicy | None:
    """Load the workspace's user-authored data policy, or None when absent.

    A malformed file returns a policy object with ``errors`` populated (and
    only the parseable parts honored) so callers can surface the problem as a
    blocker instead of silently ignoring the owner's intent.
    """
    path = find_policy_path(workspace_root)
    if path is None:
        return None
    errors: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return WorkspaceDataPolicy(
            source_path=str(path),
            errors=(f"data policy is not valid JSON: {exc}",),
        )
    if not isinstance(data, dict):
        return WorkspaceDataPolicy(
            source_path=str(path),
            errors=("data policy root must be a JSON object",),
        )

    sensitive_columns = _str_tuple(
        data.get("sensitive_columns"), errors=errors, key="sensitive_columns"
    )
    patterns = _validate_patterns(
        _str_tuple(
            data.get("sensitive_column_patterns"),
            errors=errors,
            key="sensitive_column_patterns",
        ),
        errors=errors,
        key="sensitive_column_patterns",
    )
    allowlist = _str_tuple(
        data.get("not_sensitive_columns"), errors=errors, key="not_sensitive_columns"
    )

    categories: dict[str, tuple[str, ...]] = {}
    raw_categories = data.get("categories") or {}
    if not isinstance(raw_categories, dict):
        errors.append("`categories` must be an object of name -> pattern list")
    else:
        for name, raw_patterns in raw_categories.items():
            cat_patterns = _validate_patterns(
                _str_tuple(raw_patterns, errors=errors, key=f"categories.{name}"),
                errors=errors,
                key=f"categories.{name}",
            )
            if cat_patterns:
                categories[str(name)] = cat_patterns

    tier_override = str(data.get("tier_override") or "").strip().lower()
    if tier_override and tier_override not in {"phi", "none"}:
        errors.append("`tier_override` must be \"phi\" or \"none\"")
        tier_override = ""

    aggregate_ages_over_89 = True
    if "aggregate_ages_over_89" in data:
        raw_flag = data.get("aggregate_ages_over_89")
        if isinstance(raw_flag, bool):
            aggregate_ages_over_89 = raw_flag
        else:
            errors.append("`aggregate_ages_over_89` must be true or false")

    declared = {column.lower() for column in sensitive_columns}
    conflicted = [
        column for column in allowlist if column.lower() in declared
    ]
    if conflicted:
        errors.append(
            "columns cannot be both sensitive and allowlisted: "
            + ", ".join(sorted(conflicted))
        )
        allowlist = tuple(c for c in allowlist if c.lower() not in declared)

    return WorkspaceDataPolicy(
        source_path=str(path),
        sensitive_columns=sensitive_columns,
        sensitive_column_patterns=patterns,
        categories=categories,
        not_sensitive_columns=allowlist,
        tier_override=tier_override,
        aggregate_ages_over_89=aggregate_ages_over_89,
        notes=str(data.get("notes") or ""),
        errors=tuple(errors),
    )


def policy_redaction_patterns(policy: WorkspaceDataPolicy | None) -> tuple[str, ...]:
    """Extra column-name regex patterns for display redaction.

    Exact declared names become anchored escaped regexes; declared patterns and
    custom-category patterns pass through. Suitable for the ``patterns=``
    parameter of ``core.onboarding.kpi.pii_redaction`` helpers (appended to the
    defaults — the policy widens redaction, the allowlist never narrows the
    rendered surface).
    """
    if policy is None:
        return ()
    extras: list[str] = [
        rf"^{re.escape(column)}$" for column in policy.sensitive_columns
    ]
    extras.extend(policy.sensitive_column_patterns)
    for patterns in policy.categories.values():
        extras.extend(patterns)
    return tuple(extras)


def policy_category_for_column(
    policy: WorkspaceDataPolicy | None, column_name: str
) -> str | None:
    """The ``policy:<category>`` tag a column matches, else None."""
    if policy is None or not isinstance(column_name, str) or not column_name.strip():
        return None
    name = column_name.strip()
    lowered = name.lower()
    if any(lowered == column.lower() for column in policy.sensitive_columns):
        return POLICY_CATEGORY_PREFIX + "sensitive"
    for pattern in policy.sensitive_column_patterns:
        if re.match(pattern, name, re.IGNORECASE):
            return POLICY_CATEGORY_PREFIX + "sensitive"
    for category, patterns in policy.categories.items():
        for pattern in patterns:
            if re.match(pattern, name, re.IGNORECASE):
                return POLICY_CATEGORY_PREFIX + category
    return None


def is_allowlisted(policy: WorkspaceDataPolicy | None, column_name: str) -> bool:
    """True when the owner explicitly marked this column not-sensitive."""
    if policy is None or not isinstance(column_name, str):
        return False
    lowered = column_name.strip().lower()
    return any(lowered == column.lower() for column in policy.not_sensitive_columns)


__all__ = [
    "POLICY_CATEGORY_PREFIX",
    "POLICY_FILENAME",
    "WorkspaceDataPolicy",
    "find_policy_path",
    "is_allowlisted",
    "load_workspace_data_policy",
    "policy_category_for_column",
    "policy_redaction_patterns",
]
