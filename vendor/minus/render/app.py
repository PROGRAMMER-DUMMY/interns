"""Dash app factory + live config state.

``AppState`` owns the loaded project, pages, semantic model, and query engine,
and hot-reloads them when the YAML on disk changes. A :class:`ConfigWatcher`
reloads eagerly on filesystem events; a low-frequency client Interval is the
safety net that lets the browser pick the change up. The active theme is
injected inline via the Dash ``index_string`` (see ``theme.py``).
"""

from __future__ import annotations

import threading
from pathlib import Path

import dash

from minus.agent.watcher import ConfigWatcher
from minus.config.loader import ConfigError, load_dashboards, load_project
from minus.data.model import SemanticModel
from minus.query.engine import QueryEngine
from minus.render.theme import build_theme_css, index_string

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


class AppState:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.error: str | None = None
        self._lock = threading.RLock()
        self._watcher: ConfigWatcher | None = None
        self.load()

    # -- (re)load ---------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                self.project = load_project(self.root)
                self.pages = load_dashboards(self.root, self.project)
                self.model = SemanticModel(self.project, self.root)
                self.engine = QueryEngine(self.project, self.model)
                self.error = None
            except ConfigError as exc:
                self.error = str(exc)
                raise

    # -- watching ---------------------------------------------------------
    def start_watching(self) -> bool:
        """Begin eager reloads on config file changes. Safe to call once."""
        if self._watcher is not None:
            return self._watcher.active
        self._watcher = ConfigWatcher(self._config_files, on_change=self.maybe_reload)
        return self._watcher.start()

    def stop_watching(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def _config_files(self) -> list[Path]:
        files = [self.root / "project.yaml"]
        if hasattr(self, "project"):
            folder = self.root / self.project.dashboards_dir
            if folder.exists():
                files += list(folder.glob("*.y*ml"))
        return [f for f in files if f.exists()]

    @property
    def version(self) -> str:
        """A fingerprint of config mtimes; changes trigger a client refresh."""
        return str(max((f.stat().st_mtime_ns for f in self._config_files()), default=0))

    def maybe_reload(self) -> bool:
        """Reload if config changed on disk. Returns True if reloaded.

        Thread-safe: callable from both the watcher thread and poll callback.
        """
        with self._lock:
            before = self.version
            # cheap check: only reload when fingerprint differs from last applied
            if before == getattr(self, "_applied_version", None):
                return False
            try:
                self.load()
            except ConfigError:
                # keep last-good config; error surfaced elsewhere
                self._applied_version = before
                return False
            self._applied_version = before
            return True

    def page_by_id(self, pid: str | None):
        if not self.pages:
            return None
        for p in self.pages:
            if p.id == pid:
                return p
        return self.pages[0]


def create_app(root: Path | str):
    """Build and return (dash.Dash, AppState)."""
    state = AppState(root)
    app = dash.Dash(
        __name__,
        assets_folder=str(ASSETS_DIR),
        title=state.project.name,
        suppress_callback_exceptions=True,
        update_title=None,
    )
    # Inject the active theme inline (theme switches take effect on restart).
    app.index_string = index_string(build_theme_css(state.project))

    from minus.render.layout import app_shell  # local import avoids cycle
    from minus.render.callbacks import register_callbacks

    state._applied_version = state.version
    app.layout = lambda: app_shell(state)
    register_callbacks(app, state)
    state.start_watching()  # eager reload on config changes (falls back to poll)
    return app, state
