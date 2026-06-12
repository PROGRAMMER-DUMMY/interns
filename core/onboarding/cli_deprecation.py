"""Soft-deprecation hint helper for low-level workflow CLIs.

Many internal-stage commands (`resolve-kpi-features`, `derived-feature-markdown`,
`blocker-question-panel`, ...) are still useful for one-off debugging but should
not be the primary entry point for routine use. The preferred wrappers
(`workspace-flow`, `prepare-kpi-blocker-panel`, `prepare-kpi-generation`, ...)
serialise mutations, run validation, and avoid the inconsistency that comes
from running stage commands in the wrong order.

This helper emits a single, opt-out, stderr-only deprecation hint when the CLI
is invoked directly. The hint is suppressed when ``AUTORESEARCH_INTERNAL_CALL=1``
is set in the environment (wrappers should set this before importing the
stage's workflow) or when ``AUTORESEARCH_SUPPRESS_CLI_DEPRECATION=1`` is set
(useful in CI).

The hint is informational only — the command still runs.
"""
from __future__ import annotations

import os
import sys


def is_internal_cli_call() -> bool:
    """True when a governed wrapper invoked the stage CLI internally.

    Wrappers set ``AUTORESEARCH_INTERNAL_CALL=1`` before delegating to stage
    logic; redirecting in that case would recurse back into the wrapper.
    """

    return os.environ.get("AUTORESEARCH_INTERNAL_CALL") == "1"


def announce_deprecated_cli_redirect(command: str, *, prefer: str, reason: str = "") -> None:
    """Print a single-line redirect notice to stderr.

    Unlike :func:`warn_soft_deprecated_cli`, this announces that the deprecated
    CLI is delegating to the canonical wrapper instead of running its own stage
    logic. Stage-only flags (and ``AUTORESEARCH_INTERNAL_CALL=1``) keep the
    legacy behavior, so debugging workflows are unaffected.
    """

    if os.environ.get("AUTORESEARCH_SUPPRESS_CLI_DEPRECATION") == "1":
        return
    suffix = f" — {reason}" if reason else ""
    message = (
        f"[deprecated] `{command}` now delegates to `{prefer}`{suffix}. "
        f"Call `{prefer}` directly; stage-only flags keep the legacy behavior."
    )
    try:
        print(message, file=sys.stderr)
    except Exception:  # pragma: no cover - defensive
        pass


def warn_soft_deprecated_cli(command: str, *, prefer: str, reason: str = "") -> None:
    """Print a single-line deprecation hint to stderr.

    Args:
        command: The CLI name being invoked (e.g. ``resolve-kpi-features``).
        prefer: The preferred wrapper to recommend (e.g.
            ``prepare-kpi-blocker-panel``).
        reason: Optional one-clause rationale appended to the hint.
    """

    if os.environ.get("AUTORESEARCH_INTERNAL_CALL") == "1":
        return
    if os.environ.get("AUTORESEARCH_SUPPRESS_CLI_DEPRECATION") == "1":
        return
    suffix = f" — {reason}" if reason else ""
    message = (
        f"[deprecation hint] `{command}` is a stage command; prefer "
        f"`{prefer}` for routine use{suffix}. "
        f"Suppress with AUTORESEARCH_SUPPRESS_CLI_DEPRECATION=1."
    )
    try:
        print(message, file=sys.stderr)
    except Exception:  # pragma: no cover - defensive
        pass


__all__ = [
    "announce_deprecated_cli_redirect",
    "is_internal_cli_call",
    "warn_soft_deprecated_cli",
]
