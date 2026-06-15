"""Governance for the live dashboard: redaction parity + loopback-token auth.

The richer Power BI UI exposes more surfaces than the old static export (free
slicers, drillable tables, CSV download). Each is a new way to leak a column the
KPI charts deliberately avoid. This module is the single gate:

- a display-redacted (PII/PHI per defaults + workspace data_policy.json) column
  must NEVER appear as a chart axis/series, a slicer field, a table column, or a
  CSV export column. We DROP such columns from those surfaces entirely (the live
  app shows aggregated charts, so a placeholder string is not enough -- the
  column must not be selectable at all).
- non-loopback binds require a bearer/query token (ported from
  tools/workspace_dashboard.py) because dashboards render row-level workspace data.

Wires to the existing redaction contract (core.onboarding.kpi.pii_redaction) so
the dashboard hides exactly what the result packet + verifier hide.
"""
from __future__ import annotations

import hmac

from core.onboarding.kpi.pii_redaction import (
    is_pii_column,
    workspace_redaction_patterns,
)

DASHBOARD_TOKEN_ENV = "AUTORESEARCH_DASHBOARD_TOKEN"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class WorkspaceRedaction:
    """Workspace-aware redaction policy for dashboard surfaces."""

    def __init__(self, workspace_path: object):
        self._patterns = workspace_redaction_patterns(workspace_path)

    def is_redacted(self, column: str) -> bool:
        return is_pii_column(column, patterns=self._patterns)

    def visible_columns(self, columns: list[str]) -> list[str]:
        """Columns safe to display/aggregate/slice/export (redacted dropped)."""
        return [c for c in columns if not self.is_redacted(c)]

    def panel_is_safe(self, panel: dict) -> bool:
        """True only if none of a panel's axes/series are redacted columns.

        A panel that would plot a redacted column is refused outright rather than
        rendered with a masked axis (a masked categorical axis is meaningless and
        still reveals cardinality).
        """
        for key in ("x", "y", "color", "lat", "lon"):
            val = panel.get(key)
            if val and self.is_redacted(str(val)):
                return False
        return True


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip().lower()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def attach_token_auth(app, token: str) -> None:
    """Require a bearer/query token on every request (non-loopback binds).

    Ported from tools/workspace_dashboard.py so the live app has the same guard.
    """
    from flask import abort, request

    @app.server.before_request
    def _require_dashboard_token():  # pragma: no cover - exercised via test client
        supplied = ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[len("Bearer "):]
        if not supplied:
            supplied = request.args.get("token", "")
        if not hmac.compare_digest(supplied, token):
            abort(401)


__all__ = [
    "DASHBOARD_TOKEN_ENV",
    "WorkspaceRedaction",
    "attach_token_auth",
    "is_loopback_host",
]
