"""Shared prep for the dashboard export drivers (MinusDeck PPTX / MinusPress PDF).

Both drivers operate on a workspace, not a raw minus root: they (optionally)
regenerate the certified MinusAnalyst project + data exactly like
``workspace-dashboard`` does, then hand the generated root to a vendor exporter.
DQ-gated: if regeneration is blocked, the last-good snapshot is exported (with a
warning) so an export never silently ships stale-and-unflagged data.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from core.storage.workspace_layout import WorkspaceLayout


def add_vendor_to_path(repo_root: Path | str) -> None:
    """Put the vendored packages (minus, minus_deck, minus_press) on sys.path."""
    vendor = str(Path(repo_root).resolve() / "vendor")
    if vendor not in sys.path:
        sys.path.insert(0, vendor)


def prepare_minus_root(repo_root: Path | str, workspace_rel: str, *,
                       force: bool = False, no_refresh: bool = False
                       ) -> tuple[WorkspaceLayout, Path, Optional[dict], list[str]]:
    """Ensure a generated MinusAnalyst root exists for the workspace.

    Returns (layout, minus_root, generate_result_or_None, warnings).
    Raises FileNotFoundError / RuntimeError on unrecoverable problems.
    """
    repo_root = Path(repo_root).resolve()
    workspace = (repo_root / workspace_rel).resolve()
    if not workspace.exists():
        raise FileNotFoundError(f"workspace not found: {workspace}")
    layout = WorkspaceLayout(project_root=workspace)

    from core.dashboard.minus_adapter import generate, minus_root

    root = minus_root(layout)
    warnings: list[str] = []
    res: Optional[dict] = None

    if no_refresh:
        if not (root / "project.yaml").exists():
            raise RuntimeError(
                f"no generated dashboard at {root}; run once without --no-refresh first")
        return layout, root, None, warnings

    # Mirror `workspace-dashboard`: refresh per-KPI spec defaults, then (re)build
    # the certified conformed project + data.
    try:
        from core.dashboard.spec import refresh_workspace_dashboard
        refresh_workspace_dashboard(layout)
    except Exception as exc:  # spec refresh is best-effort
        warnings.append(f"spec refresh skipped: {exc}")

    res = generate(layout, force=force)
    if not res.get("ok"):
        if (root / "project.yaml").exists():
            warnings.append(
                f"regeneration blocked ({res.get('reason')}); exporting last-good snapshot. "
                "Use --force to publish despite DQ findings.")
        else:
            detail = "; ".join(
                f"{c.get('check')}: {c.get('detail')}" for c in (res.get("failed") or [])
            )
            raise RuntimeError(
                f"cannot build dashboard data: {res.get('reason')}"
                + (f" ({detail})" if detail else ""))
    return layout, root, res, warnings


def default_out(workspace: Path, ext: str) -> Path:
    """Default export path: <workspace>/dashboard/exports/<name>.<ext>."""
    workspace = Path(workspace)
    return workspace / "dashboard" / "exports" / f"{workspace.name}.{ext}"


def open_file(path: Path | str) -> None:
    """Best-effort open in the OS default app (Windows/macOS/Linux)."""
    path = str(path)
    try:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception:
        pass


__all__ = ["add_vendor_to_path", "prepare_minus_root", "default_out", "open_file"]
