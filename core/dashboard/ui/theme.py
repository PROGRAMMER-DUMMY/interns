"""Theme assembly for the live dashboard (ported from MinusAnalyst).

base.css (structure, colors via CSS vars) + one variables file per theme
(claude.css light, dark.css). Assembled and injected inline via the Dash
index_string so exactly one theme applies. A Python copy of the few colors the
server-side Plotly figures need is kept in sync with the CSS variable files.
"""
from __future__ import annotations

from pathlib import Path

THEME_DIR = Path(__file__).resolve().parent / "theme"

# Warm editorial palette (terracotta lead + muted complements), matches the
# Claude accent family. Used as the Plotly colorway.
PALETTE = ["#C2603C", "#3F6B5E", "#C9952F", "#6E7F94",
           "#9C5A6B", "#7E8B5A", "#D9A877", "#4A453D"]

THEME_COLORS = {
    "claude": {
        "ink": "#211D18", "muted": "#6C6358", "surface": "#FCFAF5",
        "grid": "rgba(120,100,70,0.13)", "dim": "#DBD3C4",
        "accent": "#C2603C", "accent_bright": "#DA7756",
    },
    "dark": {
        "ink": "#F1EADE", "muted": "#ABA193", "surface": "#211D19",
        "grid": "rgba(240,234,222,0.12)", "dim": "#3C362E",
        "accent": "#DD8763", "accent_bright": "#E59370",
    },
}

_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    'family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&'
    'family=Hanken+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">'
)


def chart_colors(theme: str = "claude") -> dict:
    return dict(THEME_COLORS.get((theme or "claude").strip(), THEME_COLORS["claude"]))


def available_themes() -> list[str]:
    return sorted(p.stem for p in THEME_DIR.glob("*.css") if p.stem != "base")


def build_theme_css(theme: str = "claude") -> str:
    base = (THEME_DIR / "base.css").read_text(encoding="utf-8")
    name = (theme or "claude").strip()
    theme_file = THEME_DIR / f"{name}.css"
    if not theme_file.exists():
        theme_file = THEME_DIR / "claude.css"
    theme_css = theme_file.read_text(encoding="utf-8")
    # theme vars define :root; base consumes them.
    return "\n".join([theme_css, base])


def index_string(theme_css: str) -> str:
    return (
        "<!DOCTYPE html>\n<html>\n<head>\n"
        "{%metas%}\n<title>{%title%}</title>\n{%favicon%}\n{%css%}\n"
        f"{_FONT_LINKS}\n"
        f"<style>\n{theme_css}\n</style>\n"
        "</head>\n<body>\n{%app_entry%}\n"
        "<footer>{%config%}{%scripts%}{%renderer%}</footer>\n"
        "</body>\n</html>"
    )


__all__ = [
    "PALETTE", "THEME_COLORS", "build_theme_css", "chart_colors",
    "available_themes", "index_string",
]
