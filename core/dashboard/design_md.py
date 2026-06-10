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

# Maps OUR token -> the synonym color names real DESIGN.md files use (Stitch /
# awesome-design-md vocabulary varies per brand). First synonym present wins.
# Names are matched after normalizing to lowercase with '-' separators.
_COLOR_SYNONYMS: dict[str, tuple[str, ...]] = {
    "accent": ("accent", "primary", "brand", "cta", "action", "accent-1"),
    "accent_deep": ("accent-deep", "primary-deep", "primary-dark", "primary-press",
                    "brand-dark", "brand-dark-900", "secondary", "accent-2"),
    "paper": ("paper", "canvas", "background", "bg", "page", "base", "surface-0"),
    "card": ("card", "canvas-soft", "surface", "surface-1", "elevated", "panel",
             "background-secondary", "canvas-cream"),
    "ink": ("ink", "foreground", "fg", "text", "on-canvas", "on-background", "content"),
    "ink_soft": ("ink-soft", "ink-secondary", "ink-mute", "muted", "text-secondary",
                 "text-muted", "foreground-muted", "secondary-text"),
    "rule": ("rule", "hairline", "border", "divider", "line", "outline"),
    "rule_soft": ("rule-soft", "hairline-soft", "hairline-input", "border-subtle",
                  "divider-soft", "border-muted"),
}


def _norm_key(k: str) -> str:
    return re.sub(r"[\s_]+", "-", str(k).strip().lower())


def _first_hex(val: object) -> str:
    m = _HEX_RE.search(str(val))
    return m.group(0).lower() if m else ""


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


def _font_family_stack(typography: dict) -> dict[str, str]:
    """From a real DESIGN.md `typography:` block (token -> {fontFamily,...}), pick a
    display font (serif slot), a body font (sans slot), and a mono/tabular font.
    Most brands use one family across tokens — that's fine; the masthead then uses
    the brand font too. Returns only the slots we could resolve."""
    if not isinstance(typography, dict):
        return {}
    def fam(tok: dict) -> str:
        return str(tok.get("fontFamily") or tok.get("font_family") or "").strip() if isinstance(tok, dict) else ""
    items = {str(k).lower(): v for k, v in typography.items() if isinstance(v, dict)}
    out: dict[str, str] = {}
    # body/sans: prefer a body-* token, else the most common family.
    body = next((fam(v) for k, v in items.items() if "body" in k and fam(v)), "")
    if not body:
        fams = [fam(v) for v in items.values() if fam(v)]
        body = max(set(fams), key=fams.count) if fams else ""
    if body:
        out["sans"] = body
    # display/serif slot: a display/heading token's family (often same as body).
    disp = next((fam(v) for k, v in items.items() if ("display" in k or "heading" in k) and fam(v)), "")
    if disp:
        out["serif"] = disp
    # mono: a tabular/mono/code token if present.
    mono = next((fam(v) for k, v in items.items()
                 if any(t in k for t in ("mono", "code", "tabular")) and fam(v)), "")
    if mono:
        out["mono"] = mono
    return out


def _parse_frontmatter(text: str) -> DesignTokens | None:
    """Parse a real awesome-design-md / Stitch DESIGN.md: YAML frontmatter between
    leading `---` fences with `colors:` (+ optional `typography:`). Maps the brand's
    semantic vocabulary onto our tokens. Returns None if there's no usable frontmatter
    so the caller can fall back to the loose key:value parser (our own format)."""
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n", text or "", re.DOTALL)
    if not m:
        return None
    try:
        import yaml
        data = yaml.safe_load(m.group(1))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    colors_raw = data.get("colors")
    if not isinstance(colors_raw, dict):
        return None
    colors = {_norm_key(k): _first_hex(v) for k, v in colors_raw.items()}
    colors = {k: v for k, v in colors.items() if v}
    if not colors:
        return None
    updates: dict[str, object] = {}
    for token, synonyms in _COLOR_SYNONYMS.items():
        for syn in synonyms:
            if syn in colors:
                updates[token] = colors[syn]
                break
    # if no distinct `card` surface was found, derive it from the page background.
    if "card" not in updates and "paper" in updates:
        updates["card"] = updates["paper"]
    updates.update(_font_family_stack(data.get("typography") or {}))
    # Real brand fonts are often proprietary (Sohne etc.) with system fallbacks in
    # the family stack; don't try to Google-Fonts-load them — let the stack fall back.
    if any(k in updates for k in ("serif", "sans", "mono")):
        updates["font_families"] = ()
    return replace(_DEFAULTS, **updates) if updates else None


def parse_design_md(text: str) -> DesignTokens:
    """Parse DESIGN.md text into DesignTokens, falling back per-field on defaults.

    Tries the real awesome-design-md YAML-frontmatter schema first; if absent, uses
    the loose `key: value` parser (our own default_design.md token-block format)."""
    fm = _parse_frontmatter(text)
    if fm is not None:
        return fm
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
