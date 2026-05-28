"""Execute LIMIT-bounded preview SQL for the blocker question panel.

Runs a caller-supplied SQL statement in an in-memory DuckDB connection with a
wall-clock budget enforced via a ``ThreadPoolExecutor``. Returns a frozen
``PreviewResult`` whose ``summary()`` payload is JSON-serializable so it can be
embedded directly under panel options or in evidence artifacts. The executor
deliberately never raises: every code path — including timeouts, invalid SQL,
empty result sets, and disallowed dataset paths — surfaces as a structured
``PreviewResult``. Caching, PHI redaction, and rendering are intentionally out
of scope: this module just executes.
"""

from __future__ import annotations

import datetime as _dt
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


__all__ = ["PreviewResult", "execute_preview"]


@dataclass(frozen=True)
class PreviewResult:
    """Outcome of a single preview SQL execution.

    ``status`` is one of ``"ok"``, ``"error"``, ``"timeout"``, ``"empty"``.
    ``rows`` is always at most ``max_rows`` items; truncation past that cap is
    still reported as ``status="ok"`` (the cap is by design, not a failure).
    """

    status: str
    sql: str
    duration_ms: int
    rows: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> dict:
        """Return a JSON-serializable dict view of the result."""

        return asdict(self)


def _json_safe(value: Any) -> Any:
    """Coerce a single DuckDB cell into a JSON-serializable scalar."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (Decimal, _dt.date, _dt.datetime, _dt.time, _dt.timedelta)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:
            return str(value)
    if isinstance(value, str):
        return value
    # Lists/tuples/dicts and any exotic DuckDB / pyarrow type fall through to
    # ``str`` so the resulting payload is unambiguously JSON-safe.
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _run_query(sql: str, max_rows: int) -> tuple[list[str], list[tuple]]:
    """Execute ``sql`` in a fresh in-memory DuckDB; return columns + rows."""

    import duckdb  # imported lazily so the module import is cheap

    conn = duckdb.connect(":memory:")
    try:
        cur = conn.execute(sql)
        description = cur.description or []
        columns = [str(col[0]) for col in description]
        fetched = cur.fetchmany(max_rows + 1)
        return columns, list(fetched)
    finally:
        conn.close()


def execute_preview(
    *,
    sql: str,
    repo_root: Path,
    dataset_paths: list[Path],
    timeout_seconds: float = 3.0,
    max_rows: int = 5,
) -> PreviewResult:
    """Execute ``sql`` in DuckDB ``:memory:`` with a wall-clock budget.

    ``dataset_paths`` is treated as a documentation / future-cache key: each
    path is validated to exist under ``repo_root`` but is **not** loaded; the
    SQL itself is expected to use ``read_csv_auto('relative/path.csv')``.

    This function never raises. Every failure mode returns a ``PreviewResult``
    with an appropriate ``status`` and one-line ``error``.
    """

    repo_root = Path(repo_root).resolve()

    # 1. Validate dataset paths up front.
    for path in dataset_paths:
        candidate = Path(path)
        try:
            resolved = (
                candidate
                if candidate.is_absolute()
                else (repo_root / candidate)
            ).resolve()
        except OSError as exc:
            return PreviewResult(
                status="error",
                sql=sql,
                duration_ms=0,
                error=f"Dataset path resolution failed: {exc}"[:200],
            )
        if not resolved.exists():
            return PreviewResult(
                status="error",
                sql=sql,
                duration_ms=0,
                error=f"Dataset path does not exist: {candidate}"[:200],
            )
        try:
            resolved.relative_to(repo_root)
        except ValueError:
            return PreviewResult(
                status="error",
                sql=sql,
                duration_ms=0,
                error=f"Dataset path not under repo_root: {candidate}"[:200],
            )

    # 2. chdir into repo_root so relative CSV paths in the SQL resolve, and
    #    submit the query to a worker thread so we can enforce a wall-clock
    #    budget via ``future.result(timeout=...)``.
    old_cwd = Path.cwd()
    start = time.perf_counter()
    try:
        try:
            os.chdir(repo_root)
        except OSError as exc:
            return PreviewResult(
                status="error",
                sql=sql,
                duration_ms=0,
                error=f"Could not chdir to repo_root: {exc}"[:200],
            )

        # Run the query in a *daemon* thread so a hung DuckDB query cannot
        # block process exit. ``ThreadPoolExecutor`` creates non-daemon
        # workers, which means Python's atexit waits for them — that's how a
        # single runaway preview can keep the workspace_lock held until the
        # OS kills the process. A bare ``threading.Thread(daemon=True)``
        # plus a 1-slot queue gives us the same timeout semantics without
        # the shutdown trap.
        result_q: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def _worker() -> None:
            try:
                cols, rows = _run_query(sql, max_rows)
                result_q.put(("ok", (cols, rows)))
            except BaseException as exc:  # noqa: BLE001
                result_q.put(("error", exc))

        worker = threading.Thread(
            target=_worker,
            name="panel-preview-duckdb",
            daemon=True,
        )
        worker.start()
        try:
            kind, payload = result_q.get(timeout=timeout_seconds)
        except queue.Empty:
            duration_ms = int((time.perf_counter() - start) * 1000)
            return PreviewResult(
                status="timeout",
                sql=sql,
                duration_ms=duration_ms,
                error=f"Exceeded {timeout_seconds}s budget",
            )
        if kind == "error":
            duration_ms = int((time.perf_counter() - start) * 1000)
            return PreviewResult(
                status="error",
                sql=sql,
                duration_ms=duration_ms,
                error=str(payload)[:200],
            )
        columns, fetched = payload
    finally:
        try:
            os.chdir(old_cwd)
        except OSError:
            pass

    duration_ms = int((time.perf_counter() - start) * 1000)

    # 3. Shape the results.
    if not fetched:
        return PreviewResult(
            status="empty",
            sql=sql,
            duration_ms=duration_ms,
            rows=[],
            columns=list(columns),
            error="Query returned 0 rows",
        )

    truncated = fetched[:max_rows]
    rows = [
        {col: _json_safe(value) for col, value in zip(columns, row)}
        for row in truncated
    ]
    return PreviewResult(
        status="ok",
        sql=sql,
        duration_ms=duration_ms,
        rows=rows,
        columns=list(columns),
        error=None,
    )
