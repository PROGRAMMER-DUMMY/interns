"""Optimization playbook loader/validator/consult (cloud-first restructure, slice 5).

Pure functions over ``config/optimization_playbook.yaml``. No I/O beyond reading that
file. Every rule in the yaml is a symptom -> detect -> ordered remedies mapping copied
from a cited vendor doc; this module just loads it, checks the schema, and matches it
against symptoms/metrics. See docs/reference/engine_compute_selection_research.md (Q4/Q5)
and docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md Section 5
("When to revisit") + Section 13 slice 5.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ENGINES = {"spark", "dbsql", "polars", "dbt", "any"}
_CONFIDENCE = {"high", "medium"}
_COMPARATORS = {">", ">=", "<", "<=", "==", "!=", "in", "crosses"}

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "optimization_playbook.yaml"


class PlaybookError(ValueError):
    """Raised when the playbook yaml fails schema validation."""


def load_playbook(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load and validate config/optimization_playbook.yaml.

    Fails loud (``PlaybookError``) on any rule missing a threshold or source_url, or
    with any other schema gap -- a rule with an invented/missing number is a bug, not
    something to silently skip.
    """
    p = Path(path) if path is not None else _DEFAULT_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise PlaybookError(f"{p}: expected a non-empty 'rules' list")
    for rule in rules:
        _validate_rule(rule, p)
    return rules


def _validate_rule(rule: dict[str, Any], path: Path) -> None:
    rid = rule.get("id")
    if not rid or not isinstance(rid, str):
        raise PlaybookError(f"{path}: rule missing a string 'id': {rule!r}")
    if rule.get("engine") not in _ENGINES:
        raise PlaybookError(
            f"{path}: rule '{rid}' has invalid engine {rule.get('engine')!r} "
            f"(must be one of {sorted(_ENGINES)})"
        )
    if not rule.get("symptom"):
        raise PlaybookError(f"{path}: rule '{rid}' missing 'symptom'")

    detect = rule.get("detect")
    if not isinstance(detect, dict):
        raise PlaybookError(f"{path}: rule '{rid}' missing 'detect' block")
    for field_name in ("metric", "source", "comparator"):
        if not detect.get(field_name):
            raise PlaybookError(f"{path}: rule '{rid}' detect missing '{field_name}'")
    if detect.get("comparator") not in _COMPARATORS:
        raise PlaybookError(
            f"{path}: rule '{rid}' detect has invalid comparator {detect.get('comparator')!r} "
            f"(must be one of {sorted(_COMPARATORS)})"
        )
    if detect.get("threshold") is None or detect.get("threshold") == "":
        raise PlaybookError(f"{path}: rule '{rid}' detect missing 'threshold'")

    remedies = rule.get("remedies")
    if not isinstance(remedies, list) or not remedies:
        raise PlaybookError(f"{path}: rule '{rid}' missing non-empty 'remedies'")
    for remedy in remedies:
        if not isinstance(remedy, dict) or not remedy.get("action"):
            raise PlaybookError(f"{path}: rule '{rid}' has a remedy without an 'action': {remedy!r}")

    if not rule.get("source_url"):
        raise PlaybookError(f"{path}: rule '{rid}' missing 'source_url'")
    if rule.get("confidence") not in _CONFIDENCE:
        raise PlaybookError(
            f"{path}: rule '{rid}' has invalid confidence {rule.get('confidence')!r} "
            "(must be 'high' or 'medium')"
        )


def consult(
    symptoms: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
    *,
    rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return playbook rules matching the given symptoms and/or measured metrics.

    ``symptoms`` matches a rule's ``id`` or ``symptom`` text (case-insensitive).
    ``metrics`` is a ``{metric_name: value}`` dict evaluated against each rule's
    ``detect`` (comparator/threshold); rules whose threshold is not a plain scalar
    (e.g. the Q5 relative triggers, or compound AQE-skew thresholds) are not
    metric-matchable and only fire on an explicit symptom match.

    Each returned rule keeps its authored remedies in cheapest-first order. An
    unknown symptom (or no match at all) returns ``[]`` -- never ``None`` and never
    raises.
    """
    rules = rules if rules is not None else load_playbook()
    symptoms_lc = {s.strip().lower() for s in (symptoms or []) if s}
    matched: list[dict[str, Any]] = []
    for rule in rules:
        hit = bool(symptoms_lc) and (
            rule["id"].lower() in symptoms_lc or rule["symptom"].lower() in symptoms_lc
        )
        if not hit and metrics:
            hit = _metric_matches(rule["detect"], metrics)
        if hit:
            matched.append(rule)
    return matched


def _metric_matches(detect: dict[str, Any], metrics: dict[str, Any]) -> bool:
    if detect["metric"] not in metrics:
        return False
    value = metrics[detect["metric"]]
    comparator = detect["comparator"]
    threshold = detect["threshold"]
    try:
        if comparator == ">":
            return value > threshold
        if comparator == ">=":
            return value >= threshold
        if comparator == "<":
            return value < threshold
        if comparator == "<=":
            return value <= threshold
        if comparator == "==":
            return value == threshold
        if comparator == "!=":
            return value != threshold
        if comparator == "in":
            return value in threshold
    except TypeError:
        return False
    return False  # "crosses" and any other non-scalar comparator: not evaluable from one reading
