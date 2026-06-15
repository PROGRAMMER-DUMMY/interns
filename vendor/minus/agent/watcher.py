"""Event-driven config file watcher (optional alternative to client polling).

``ConfigWatcher`` uses the ``watchdog`` library to watch the parent directories
of a set of config files. When any watched file is modified / created / moved /
deleted, it debounces a burst of events and then sets a thread-safe dirty flag
and (optionally) invokes a callback. A long-poll or Dash Interval can then call
:meth:`ConfigWatcher.consume_dirty` to decide whether to reload promptly.

Design notes
------------
* watchdog watches *directories*, so we watch the parents of each config file
  and filter events down to the files we care about (matched by resolved path,
  or by being a ``*.yml`` / ``*.yaml`` file inside a watched directory).
* The set of paths may be supplied as a callable so the watcher can track files
  that appear/disappear in the dashboards directory over time.
* Graceful degradation: if watchdog cannot start for any reason, ``start()``
  catches the failure and leaves ``self.active`` False. The caller can then fall
  back to its existing polling strategy.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

# watchdog is optional (vendored into interns): if absent, ConfigWatcher.start()
# degrades to inactive and the app falls back to its cfg-poll Interval.
try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
    _HAS_WATCHDOG = True
except Exception:  # pragma: no cover - exercised only when watchdog is missing
    _HAS_WATCHDOG = False

    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

    FileSystemEvent = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]

_YAML_SUFFIXES = (".yml", ".yaml")

PathsArg = list[Path] | Callable[[], list[Path]]


class _Handler(FileSystemEventHandler):
    """Forwards relevant filesystem events to the owning watcher."""

    def __init__(self, watcher: "ConfigWatcher"):
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._watcher._handle_event(event)


class ConfigWatcher:
    """Watch config files and flag when any of them changes.

    Parameters
    ----------
    paths:
        Either a static list of file paths, or a zero-arg callable returning the
        current list of file paths. A callable lets the watcher track files that
        come and go (e.g. new dashboards in a folder).
    on_change:
        Optional zero-arg callback invoked (from the debounce timer thread) after
        a change is detected. Exceptions raised by it are swallowed so a bad
        callback cannot kill the observer.
    debounce:
        Seconds to collapse a burst of events into a single notification.
    """

    def __init__(
        self,
        paths: PathsArg,
        on_change: Callable[[], None] | None = None,
        debounce: float = 0.3,
    ):
        self._paths_arg = paths
        self._on_change = on_change
        self._debounce = max(0.0, float(debounce))

        self._lock = threading.Lock()
        self._dirty = False

        self._observer: Observer | None = None
        self._timer: threading.Timer | None = None
        self._started = False
        self.active = False

    # -- path resolution --------------------------------------------------
    def _current_paths(self) -> list[Path]:
        raw = self._paths_arg() if callable(self._paths_arg) else self._paths_arg
        out: list[Path] = []
        for p in raw or []:
            try:
                out.append(Path(p).resolve())
            except OSError:
                continue
        return out

    def _watch_dirs(self) -> set[Path]:
        dirs: set[Path] = set()
        for f in self._current_paths():
            parent = f.parent
            try:
                if parent.exists():
                    dirs.add(parent.resolve())
            except OSError:
                continue
        return dirs

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        """Start the observer. Returns True if active, False if it degraded.

        Idempotent: calling start() while already active is a no-op.
        """
        if self._started:
            return self.active

        self._started = True
        if not _HAS_WATCHDOG:
            # No watchdog available -> stay inactive; caller polls instead.
            self.active = False
            return False
        dirs = self._watch_dirs()
        if not dirs:
            # Nothing to watch (e.g. config dir does not exist yet); degrade.
            self.active = False
            return False

        try:
            observer = Observer()
            handler = _Handler(self)
            for d in dirs:
                observer.schedule(handler, str(d), recursive=False)
            observer.start()
        except Exception:
            # watchdog failed to start; degrade gracefully to polling.
            self._observer = None
            self.active = False
            return False

        self._observer = observer
        self.active = True
        return True

    def stop(self) -> None:
        """Stop and join the observer. Idempotent and safe if never started."""
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()

        observer = self._observer
        self._observer = None
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=5)
            except Exception:
                pass

        self.active = False
        self._started = True  # remain stopped; do not auto-restart on start()

    # -- dirty flag -------------------------------------------------------
    def is_dirty(self) -> bool:
        """Thread-safe: True if a change occurred since the last consume."""
        with self._lock:
            return self._dirty

    def consume_dirty(self) -> bool:
        """Thread-safe: return the dirty state and clear the flag."""
        with self._lock:
            was = self._dirty
            self._dirty = False
            return was

    # -- internals --------------------------------------------------------
    def _is_relevant(self, raw_path: str | bytes) -> bool:
        if not raw_path:
            return False
        if isinstance(raw_path, bytes):
            raw_path = raw_path.decode("utf-8", "ignore")
        try:
            path = Path(raw_path).resolve()
        except OSError:
            return False

        # Exact match against a tracked config file.
        if path in set(self._current_paths()):
            return True

        # Any *.yml / *.yaml inside one of the watched directories.
        if path.suffix.lower() in _YAML_SUFFIXES and path.parent in self._watch_dirs():
            return True
        return False

    def _handle_event(self, event: FileSystemEvent) -> None:
        if getattr(event, "is_directory", False):
            return
        paths = [event.src_path]
        dest = getattr(event, "dest_path", None)
        if dest:
            paths.append(dest)
        if not any(self._is_relevant(p) for p in paths):
            return
        self._schedule_notify()

    def _schedule_notify(self) -> None:
        fire_now = False
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._debounce <= 0:
                fire_now = True
            else:
                self._timer = threading.Timer(self._debounce, self._fire)
                self._timer.daemon = True
                self._timer.start()
        if fire_now:
            self._fire()

    def _fire(self) -> None:
        with self._lock:
            self._dirty = True
            self._timer = None
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                # A faulty callback must not crash the timer thread.
                pass

    # -- context manager --------------------------------------------------
    def __enter__(self) -> "ConfigWatcher":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
