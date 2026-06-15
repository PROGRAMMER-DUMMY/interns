"""Theme assembly.

Themes are split into a shared ``base.css`` (structure, all colors via CSS
variables) plus one variables file per theme (``claude.css``, ``dark.css``, …).
We assemble the active theme + any project overrides and inject it inline via the
Dash ``index_string`` so exactly one theme is applied (Dash would otherwise
auto-load every ``*.css`` in an assets folder).
"""

from __future__ import annotations

from pathlib import Path

from minus.config.models import Project

THEMES_DIR = Path(__file__).resolve().parent.parent / "themes"

# Plotly figures are built server-side without access to the CSS, so we keep a
# Python copy of the few colors charts need, per theme. Keep these in sync with
# the matching *.css variable files.
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


def chart_colors(project) -> dict:
    """Resolve the active theme's chart colors (honors an accent override)."""
    name = (project.theme.name or "claude").strip()
    colors = dict(THEME_COLORS.get(name, THEME_COLORS["claude"]))
    if getattr(project.theme, "accent", None):
        colors["accent"] = project.theme.accent
        colors["accent_bright"] = project.theme.accent
    return colors


def available_themes(themes_dir: Path = THEMES_DIR) -> list[str]:
    return sorted(p.stem for p in themes_dir.glob("*.css") if p.stem != "base")


def build_theme_css(project: Project, themes_dir: Path = THEMES_DIR) -> str:
    """Return the full stylesheet for the project's active theme + overrides."""
    base = (themes_dir / "base.css").read_text(encoding="utf-8")
    name = (project.theme.name or "claude").strip()
    theme_file = themes_dir / f"{name}.css"
    if not theme_file.exists():
        theme_file = themes_dir / "claude.css"
    theme = theme_file.read_text(encoding="utf-8")

    overrides: list[str] = []
    if project.theme.accent:
        overrides += [f"--accent: {project.theme.accent};",
                      f"--accent-ink: {project.theme.accent};"]
    if project.theme.canvas:
        overrides.append(f"--canvas: {project.theme.canvas};")
    override_css = (":root { " + " ".join(overrides) + " }") if overrides else ""

    # theme vars define :root; base consumes them; overrides come last and win.
    return "\n".join([theme, base, override_css])


# Fraunces (optical serif) for display + numerals; Hanken Grotesk for UI/body.
_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?'
    'family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&'
    'family=Hanken+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">'
)


def index_string(theme_css: str) -> str:
    """A Dash index template with web fonts + the assembled theme injected inline."""
    return (
        "<!DOCTYPE html>\n<html>\n<head>\n"
        "{%metas%}\n<title>{%title%}</title>\n{%favicon%}\n{%css%}\n"
        f"{_FONT_LINKS}\n"
        f"<style>\n{theme_css}\n</style>\n"
        "</head>\n<body>\n{%app_entry%}\n"
        "<footer>{%config%}{%scripts%}{%renderer%}</footer>\n"
        "</body>\n</html>"
    )
