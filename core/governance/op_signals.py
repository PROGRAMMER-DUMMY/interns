"""Normalized op-signal contract + signal->skill routing.

Every governed CLI funnels through ``run_workspace_command``. Historically that
envelope carried only mechanical concerns (lock, timing, idempotency); the
*judgement* concerns -- "is this result low-confidence / stuck / ambiguous?",
"should a skill fire here?" -- lived beside the pipeline, not inside it, so they
never fired on the live path. This module gives the envelope a uniform way to
read those signals out of any command's result and map them to the skill /
specialist that should be surfaced.

Generic and deterministic: signals are read from already-present result keys
(confidence facets, blocked/unresolved counts, status strings), never from a
workspace name or domain vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OpSignals:
    """Cross-cutting health signals derived from one command's result."""

    confidence: float | None = None
    blocked: bool = False
    empty_panel: bool = False
    stuck: bool = False
    ambiguous: bool = False
    user_state_changed: bool = False
    unresolved_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        """True when at least one signal warrants surfacing a skill/specialist."""
        return any(
            [
                self.blocked,
                self.empty_panel,
                self.stuck,
                self.ambiguous,
                self.user_state_changed,
                self.unresolved_count > 0,
                self.confidence is not None and self.confidence < _LOW_CONFIDENCE,
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "blocked": self.blocked,
            "empty_panel": self.empty_panel,
            "stuck": self.stuck,
            "ambiguous": self.ambiguous,
            "user_state_changed": self.user_state_changed,
            "unresolved_count": self.unresolved_count,
            "details": self.details,
        }


_LOW_CONFIDENCE = 0.6


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scan_confidence(payload: dict[str, Any]) -> float | None:
    """Lowest confidence facet found anywhere in the payload, if any.

    Confidence facets across the platform use the shape ``{"confidence": <num>}``
    (metric/cuts derivation, derived-feature options, etc.). We take the minimum
    so the weakest part of a result drives the signal. Bounded recursion.
    """
    found: list[float] = []

    def _walk(node: Any, depth: int) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            if "confidence" in node:
                value = _as_float(node.get("confidence"))
                if value is not None:
                    found.append(value)
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node[:50]:
                _walk(v, depth + 1)

    _walk(payload, 0)
    return min(found) if found else None


def derive_op_signals(command: str, payload: dict[str, Any]) -> OpSignals:
    """Read normalized signals from a command result envelope. Never raises."""
    if not isinstance(payload, dict):
        return OpSignals()

    status = str(payload.get("status") or "").lower()
    blocked_count = _as_int(
        payload.get("blocked_kpi_count")
        if payload.get("blocked_kpi_count") is not None
        else payload.get("blocked_count")
    )
    unresolved = _as_int(payload.get("unresolved_feature_count"))
    question_count = payload.get("question_count")

    blocked = (
        blocked_count > 0
        or status in {"blocked", "needs_user_answer", "awaiting_user_answer"}
        or bool(payload.get("blocked"))
    )
    # A panel that asks NOTHING while work is still blocked is the silent
    # dead-end: zero questions but a positive blocked count.
    empty_panel = (
        question_count is not None
        and _as_int(question_count) == 0
        and blocked_count > 0
    )
    stuck = empty_panel or (blocked and unresolved == 0 and question_count == 0)
    confidence = _scan_confidence(payload)
    ambiguous = bool(payload.get("ambiguous")) or status in {"ambiguous", "awaiting_user_answer"}

    return OpSignals(
        confidence=confidence,
        blocked=blocked,
        empty_panel=empty_panel,
        stuck=stuck,
        ambiguous=ambiguous,
        user_state_changed=bool(payload.get("user_state_changed")),
        unresolved_count=unresolved,
        details={
            "command": command,
            "status": status or None,
            "blocked_count": blocked_count,
            "unresolved_feature_count": unresolved,
        },
    )


# Generic signal -> skill/specialist table. Keyed on structural signals, never on
# a domain. Each entry: predicate(signals) -> (skill, specialist, why).
def signals_to_skills(signals: OpSignals) -> list[dict[str, Any]]:
    """Map signals to the skills/specialists that should be surfaced.

    Returns ``[{skill, specialist, why, when_to_invoke}]``. ``when_to_invoke`` is
    the skill's own "When to invoke" excerpt when the excerpt tool is available;
    absent excerpting degrades to an empty string (advisory only).
    """
    out: list[dict[str, Any]] = []

    def _add(skill: str, specialist: str, why: str) -> None:
        if any(item["skill"] == skill for item in out):
            return
        out.append(
            {
                "skill": skill,
                "specialist": specialist,
                "why": why,
                "when_to_invoke": _skill_when_to_invoke(skill),
            }
        )

    if signals.ambiguous:
        _add("grill-requirements", "business-analyst",
             "the result is ambiguous/underspecified; resolve before proceeding")
    if signals.empty_panel or signals.stuck:
        _add("kpi-analyst", "kpi-analyst",
             "the pipeline is blocked but surfaced no answerable question (dead-end)")
        _add("grill-requirements", "kpi-analyst",
             "interrogate the stuck state with evidence before assuming it is resolved")
    if signals.confidence is not None and signals.confidence < _LOW_CONFIDENCE:
        _add("kpi-analyst", "kpi-analyst",
             f"a derived value is low-confidence ({signals.confidence:.2f}); validate against evidence")
    if signals.unresolved_count > 0:
        _add("feature-derivation-library", "feature-derivation-library",
             "unresolved features may need a reusable derived/temporal/join pattern")
    if signals.user_state_changed:
        _add("workspace-governance", "validation-gatekeeper",
             "a user-decided marker changed during this op; confirm it was intended")
    return out


def _skill_when_to_invoke(skill: str) -> str:
    try:
        from tools.skill_excerpt import get_skill_excerpt

        excerpt = get_skill_excerpt(skill, section="When to invoke")
        return str(getattr(excerpt, "body", "") or "").strip()[:600]
    except Exception:  # pragma: no cover - excerpting is advisory only
        return ""


__all__ = ["OpSignals", "derive_op_signals", "signals_to_skills"]
