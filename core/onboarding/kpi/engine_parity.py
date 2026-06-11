"""Polars-vs-SQL parity check for KPI results.

Generates the Polars variant of a KPI from the same ready feature mappings the
SQL generator used, executes it, and compares its Gold output row-for-row
against the canonical rows the DuckDB executor just produced. This turns every
results run into a cross-runtime self-check instead of parity being proven only
on synthetic tests.

Workspace- and domain-agnostic: driven entirely by generated artifacts
(feature mappings -> generated Polars script -> Gold Delta) and the canonical
rows passed in. Never fatal: every failure mode degrades to a recorded
status/reason in the result packet instead of breaking the results stage.

Statuses:
- ``match``     rows and columns agree after normalization.
- ``mismatch``  structural or value difference (reason says which).
- ``skipped``   parity not applicable here (runtime missing, KPI pattern not
                yet supported by the Polars renderer, result too large).
- ``error``     the generated script failed to run or its output was unreadable.

PySpark is intentionally not executed here: it needs a JDK 8/11/17 JVM and is
covered by the env-gated CI parity test instead.
"""
from __future__ import annotations

import math
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from core.storage.workspace_layout import WorkspaceLayout

# Floats on both sides are rounded to this many decimal places before
# comparison (the SQL side typically emits ROUND(x, 2) aggregates).
_FLOAT_TOLERANCE_DP = 2

# Above this row count the full-row comparison is skipped rather than pulling
# the entire result into memory twice.
MAX_PARITY_ROWS = 100_000


def polars_runtime_available() -> bool:
    """True when the in-process runtime can generate, run, and read back."""
    try:
        import polars  # noqa: F401
        import deltalake  # noqa: F401
    except Exception:
        return False
    return True


def _normalize_cell(value: Any) -> Any:
    """Engine-neutral cell form: dates as ISO days, floats rounded, rest str."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return round(value, _FLOAT_TOLERANCE_DP)
    if isinstance(value, int):
        return round(float(value), _FLOAT_TOLERANCE_DP)
    if isinstance(value, datetime):
        # date_trunc in SQL yields midnight timestamps where Polars yields
        # dates; collapse both to the ISO day.
        if value.time() == datetime.min.time():
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _normalize_rows(
    columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Order-insensitive form: columns sorted by lowercased name, rows sorted."""
    cols = [str(col).lower() for col in columns]
    order = sorted(range(len(cols)), key=lambda i: cols[i])
    normalized = [tuple(_normalize_cell(row[i]) for i in order) for row in rows]
    normalized.sort(key=lambda row: tuple((cell is None, str(cell)) for cell in row))
    return [cols[i] for i in order], normalized


def run_polars_parity(
    repo_root: str | Path,
    workspace_rel: str,
    kpi_id: str,
    canonical_columns: Sequence[str],
    canonical_rows: Sequence[Sequence[Any]],
    *,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Generate + run the Polars variant of one KPI and compare its rows.

    ``canonical_columns`` / ``canonical_rows`` are the full result the SQL
    executor just produced (the source of truth being checked against).
    """
    repo_root = Path(repo_root).resolve()
    result: dict[str, Any] = {"engine": "polars", "kpi_id": kpi_id, "status": "", "reason": ""}

    if len(canonical_rows) > MAX_PARITY_ROWS:
        result.update(
            status="skipped",
            reason=f"result has {len(canonical_rows)} rows (> {MAX_PARITY_ROWS} parity cap)",
        )
        return result
    if not polars_runtime_available():
        result.update(status="skipped", reason="polars/deltalake not installed in this runtime")
        return result

    # 1. Generate the Polars script from the same ready feature mappings.
    try:
        from core.onboarding.kpi.polars_generator import PolarsKPIGenerator

        generated = PolarsKPIGenerator(repo_root, workspace_rel).generate(kpi_id)
    except Exception as exc:
        # Unsupported pattern (derived formulas, window kinds...) — not a failure.
        result.update(status="skipped", reason=f"generation: {exc}")
        return result
    script_path = repo_root / generated.path

    # 2. Execute it with the same interpreter (venv has polars + deltalake).
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            # The generated scripts print Unicode tables; Windows' locale
            # default (cp1252) crashes the reader threads without this.
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        result.update(status="error", reason=f"script timed out after {timeout_seconds}s")
        return result
    except OSError as exc:
        result.update(status="error", reason=f"script launch failed: {exc}")
        return result
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        result.update(status="error", reason="script failed: " + " | ".join(tail))
        return result

    # 3. Read the Gold Delta the script wrote.
    layout = WorkspaceLayout(project_root=(repo_root / workspace_rel).resolve())
    gold_dir = layout.state_dir / "medallion" / "gold" / f"{kpi_id}_results"
    try:
        import polars as pl

        frame = pl.read_delta(str(gold_dir))
    except Exception as exc:
        result.update(status="error", reason=f"gold read ({gold_dir.name}): {exc}")
        return result

    # 4. Compare normalized forms.
    polars_cols, polars_rows = _normalize_rows(frame.columns, frame.rows())
    sql_cols, sql_rows = _normalize_rows(canonical_columns, canonical_rows)
    if polars_cols != sql_cols:
        result.update(
            status="mismatch",
            reason=f"column sets differ: sql={sql_cols} polars={polars_cols}",
        )
        return result
    if len(polars_rows) != len(sql_rows):
        result.update(
            status="mismatch",
            reason=f"row counts differ: sql={len(sql_rows)} polars={len(polars_rows)}",
        )
        return result
    if polars_rows != sql_rows:
        differing = sum(1 for a, b in zip(sql_rows, polars_rows) if a != b)
        result.update(
            status="mismatch",
            reason=f"{differing} of {len(sql_rows)} rows differ after normalization",
        )
        return result
    result.update(status="match", reason=f"{len(sql_rows)} rows x {len(sql_cols)} columns agree")
    return result


__all__ = ["MAX_PARITY_ROWS", "polars_runtime_available", "run_polars_parity"]
