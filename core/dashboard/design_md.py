"""DESIGN.md design-language layer for dashboards.

The dashboard's *look* (palette, fonts) comes from a swappable markdown file, not
hardcoded CSS — so any workspace can carry its own `DESIGN.md` and the same engine
renders a different aesthetic with zero code change. This follows the Stitch /
awesome-design-md DESIGN.md convention: prose sections for humans/agents, plus a
machine-readable token block the renderer consumes deterministically.

Resolution order (first found wins):
  1. `workspaces/<ws>/DESIGN.md`         (per-workspace override)
  2. `core/dashboard/default_design.md`  (shipped repo default — editorial)
  3. built-in `_DEFAULTS` below          (always-available fallback)

Parsing is intentionally forgiving: it scans for `key: value` lines anywhere in
the file (in a fenced ```design-tokens block or a plain list). Any missing/garbled
token falls back to the default, so a partial or malformed DESIGN.md still renders
a coherent dashboard rather than crashing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path


_COLOR_KEYS = (
    "paper", "card", "ink", "ink_soft", "rule", "rule_soft", "accent", "accent_deep",
)
_FONT_KEYS = ("serif", "sans", "mono")
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


@dataclass(frozen=True)
class DesignTokens:
    # Editorial "data desk" defaults — the shipped baseline.
    paper: str = "#f3efe6"
    card: str = "#fbf9f3"
    ink: str = "#1b1a17"
    ink_soft: str = "#6f6a60"
    rule: str = "#d7d1c4"
    rule_soft: str = "#e7e2d6"
    accent: str = "#b4441c"
    accent_deep: str = "#2f4452"
    serif: str = "'Fraunces', Georgia, serif"
    sans: str = "'Hanken Grotesk', system-ui, sans-serif"
    mono: str = "'Spline Sans Mono', ui-monospace, monospace"
    # Colorblind-safe categorical ramp for multi-series (Okabe-Ito).
    categorical: tuple[str, ...] = (
        "#0072b2", "#d55e00", "#009e73", "#cc79a7",
        "#e69f00", "#56b4e9", "#f0e442", "#000000",
    )
    # Google Fonts families to request (display/body/mono), or empty if system.
    font_families: tuple[str, ...] = field(
        default=("Fraunces:opsz,wght@9..144,400;9..144,600;9..144,900",
                 "Hanken+Grotesk:wght@400;500;600;700",
                 "Spline+Sans+Mono:wght@400;500;600")
    )


_DEFAULTS = DesignTokens()


def _parse_value(line: str) -> tuple[str, str] | None:
    # `key: value` or `- key: value`; strip markdown bullet/backticks/quotes.
    m = re.match(r"\s*[-*]?\s*([a-zA-Z_][a-zA-Z0-9_ ]*?)\s*[:=]\s*(.+?)\s*$", line)
    if not m:
        return None
    key = m.group(1).strip().lower().replace(" ", "_")
    val = m.group(2).strip().strip("`").strip()
    return key, val


def parse_design_md(text: str) -> DesignTokens:
    """Parse DESIGN.md text into DesignTokens, falling back per-field on defaults."""
    colors: dict[str, str] = {}
    fonts: dict[str, str] = {}
    categorical: list[str] = []
    for raw in (text or "").splitlines():
        kv = _parse_value(raw)
        if not kv:
            continue
        key, val = kv
        if key in _COLOR_KEYS:
            hexes = _HEX_RE.findall(val)
            if hexes:
                colors[key] = hexes[0].lower()
        elif key in _FONT_KEYS and val:
            fonts[key] = val
        elif key in ("categorical", "categorical_palette", "series_palette"):
            found = _HEX_RE.findall(val)
            if found:
                categorical = [h.lower() for h in found]
    updates: dict[str, object] = {**colors, **fonts}
    if categorical:
        updates["categorical"] = tuple(categorical)
    return replace(_DEFAULTS, **updates) if updates else _DEFAULTS


def _default_design_path() -> Path:
    return Path(__file__).resolve().parent / "default_design.md"


def load_design_tokens(workspace_root: Path | str | None = None) -> DesignTokens:
    """Resolve and parse the active DESIGN.md (workspace override -> repo default
    -> built-in). Never raises; returns coherent tokens regardless of input."""
    candidates: list[Path] = []
    if workspace_root:
        candidates.append(Path(workspace_root) / "DESIGN.md")
    candidates.append(_default_design_path())
    for path in candidates:
        try:
            if path.is_file():
                return parse_design_md(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return _DEFAULTS


__all__ = ["DesignTokens", "parse_design_md", "load_design_tokens"]
