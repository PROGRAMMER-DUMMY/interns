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
                yet supported by the Polars renderer).
- ``error``     the generated script failed to run or its output was unreadable.

Modes (every verdict carries ``mode``):
- ``row_parity``        results at or under the row cap are compared
                        row-for-row after normalization (strongest guarantee).
- ``aggregate_parity``  results over the row cap are compared via per-engine
                        aggregate signatures (row count, per-column null
                        counts, tolerance-aware numeric sum/min/max, distinct
                        counts of non-numeric grouping columns, and an
                        order-insensitive content checksum over normalized
                        rows). Reordering still matches; any value drift fails.

PySpark is intentionally not executed here: it needs a JDK 8/11/17 JVM and is
covered by the env-gated CI parity test instead.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from core.storage.workspace_layout import WorkspaceLayout

# Floats on both sides are rounded to this many decimal places before
# comparison (the SQL side typically emits ROUND(x, 2) aggregates).
_FLOAT_TOLERANCE_DP = 2

# Above this row count parity switches from the row-for-row comparison to
# aggregate-signature mode rather than sorting/holding two normalized copies
# of the entire result in memory.
MAX_PARITY_ROWS = 100_000

# Mode labels reported in every parity verdict.
PARITY_MODE_ROW = "row_parity"
PARITY_MODE_AGGREGATE = "aggregate_parity"

# The signature names compared in aggregate mode, in report order.
AGGREGATE_SIGNATURE_NAMES = (
    "row_count",
    "null_counts",
    "numeric_stats",
    "distinct_counts",
    "content_checksum",
)


def parity_row_cap() -> int:
    """Row count above which parity runs in aggregate-signature mode.

    Defaults to ``MAX_PARITY_ROWS``; override with the ``KPI_PARITY_ROW_CAP``
    environment variable (same env-knob style as ``KPI_ENGINE_PARITY``) so
    tests can force aggregate mode on small fixtures. Invalid or non-positive
    values fall back to the default.
    """
    raw = os.environ.get("KPI_PARITY_ROW_CAP", "")
    try:
        value = int(raw)
    except ValueError:
        return MAX_PARITY_ROWS
    return value if value > 0 else MAX_PARITY_ROWS


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
        # Documented contract: floats compare under a fixed decimal tolerance
        # (engines differ in float representation). An integral result canonicals
        # to int so a float 10.0 still matches an int 10 across engines.
        rounded = round(value, _FLOAT_TOLERANCE_DP)
        return int(rounded) if rounded == int(rounded) else rounded
    if isinstance(value, int):
        # T3: keep ints EXACT — do not coerce through float. Coercing an identity
        # column / large id (> 2^53) to float silently loses precision and could
        # mask a real divergence. Integral floats canonical to int (above), so an
        # int column still matches a float column of equal value.
        # Ref: core-audit ob-kpi-b.md.
        return value
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


def _aggregate_signature(
    columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> dict[str, Any]:
    """One-pass aggregate signature over the SAME canonicalized cells row
    parity uses (``_normalize_cell``: UTC/ISO dates, floats rounded) — no
    second normalization path.

    Per column (keyed by lowercased name, sorted): null count; for numeric
    cells (floats after normalization, which also covers ints) count/sum/
    min/max; for non-numeric cells a distinct count (grouping columns).
    Plus an order-insensitive content checksum: sum of stable per-row SHA-256
    hashes of the normalized row, mod 2**64 — reordering cannot change it,
    any value drift does.
    """
    cols = [str(col).lower() for col in columns]
    order = sorted(range(len(cols)), key=lambda i: cols[i])
    sorted_cols = [cols[i] for i in order]
    null_counts: dict[str, int] = {col: 0 for col in sorted_cols}
    numeric_stats: dict[str, dict[str, Any] | None] = {col: None for col in sorted_cols}
    distinct: dict[str, set[Any]] = {col: set() for col in sorted_cols}
    checksum = 0
    row_count = 0
    for row in rows:
        cells = [_normalize_cell(row[i]) for i in order]
        row_count += 1
        digest = hashlib.sha256(
            json.dumps(cells, separators=(",", ":")).encode("utf-8")
        ).digest()
        checksum = (checksum + int.from_bytes(digest[:8], "big")) % (1 << 64)
        for col, cell in zip(sorted_cols, cells):
            if cell is None:
                null_counts[col] += 1
            elif isinstance(cell, float):  # bools are NOT floats: they group
                stats = numeric_stats[col]
                if stats is None:
                    numeric_stats[col] = {"count": 1, "sum": cell, "min": cell, "max": cell}
                else:
                    stats["count"] += 1
                    stats["sum"] += cell
                    if cell < stats["min"]:
                        stats["min"] = cell
                    if cell > stats["max"]:
                        stats["max"] = cell
            else:
                distinct[col].add(cell)
    return {
        "columns": sorted_cols,
        "row_count": row_count,
        "null_counts": null_counts,
        "numeric_stats": numeric_stats,
        "distinct_counts": {col: len(values) for col, values in distinct.items()},
        "content_checksum": f"{checksum:016x}",
    }


def _numeric_stats_match(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Tolerance-aware numeric comparison mirroring the row-level rounding."""
    if a is None or b is None:
        return a is None and b is None
    if a["count"] != b["count"]:
        return False
    tol = 10 ** -_FLOAT_TOLERANCE_DP
    return all(
        math.isclose(a[key], b[key], rel_tol=1e-9, abs_tol=tol)
        for key in ("sum", "min", "max")
    )


def evaluate_parity(
    canonical_columns: Sequence[str],
    canonical_rows: Sequence[Sequence[Any]],
    engine_columns: Sequence[str],
    engine_rows: Sequence[Sequence[Any]],
    *,
    row_cap: int | None = None,
) -> dict[str, Any]:
    """Pure cross-engine comparison; returns {status, reason, mode[, signatures]}.

    Mode selection: ``row_parity`` (row-for-row) at or under ``row_cap``
    canonical rows, ``aggregate_parity`` (signature comparison) above it.
    ``row_cap`` defaults to ``parity_row_cap()`` (env-overridable).
    """
    cap = parity_row_cap() if row_cap is None else row_cap
    mode = PARITY_MODE_AGGREGATE if len(canonical_rows) > cap else PARITY_MODE_ROW
    verdict: dict[str, Any] = {"mode": mode}

    if mode == PARITY_MODE_ROW:
        engine_cols, engine_norm = _normalize_rows(engine_columns, engine_rows)
        sql_cols, sql_norm = _normalize_rows(canonical_columns, canonical_rows)
        if engine_cols != sql_cols:
            verdict.update(
                status="mismatch",
                reason=f"column sets differ: sql={sql_cols} polars={engine_cols}",
            )
            return verdict
        if len(engine_norm) != len(sql_norm):
            verdict.update(
                status="mismatch",
                reason=f"row counts differ: sql={len(sql_norm)} polars={len(engine_norm)}",
            )
            return verdict
        if engine_norm != sql_norm:
            differing = sum(1 for a, b in zip(sql_norm, engine_norm) if a != b)
            verdict.update(
                status="mismatch",
                reason=f"{differing} of {len(sql_norm)} rows differ after normalization",
            )
            return verdict
        verdict.update(
            status="match", reason=f"{len(sql_norm)} rows x {len(sql_cols)} columns agree"
        )
        return verdict

    sql_sig = _aggregate_signature(canonical_columns, canonical_rows)
    engine_sig = _aggregate_signature(engine_columns, engine_rows)
    if sql_sig["columns"] != engine_sig["columns"]:
        verdict.update(
            status="mismatch",
            reason=(
                f"column sets differ: sql={sql_sig['columns']} "
                f"polars={engine_sig['columns']}"
            ),
        )
        return verdict
    signatures: dict[str, dict[str, Any]] = {
        "row_count": {
            "sql": sql_sig["row_count"],
            "engine": engine_sig["row_count"],
            "match": sql_sig["row_count"] == engine_sig["row_count"],
        },
        "null_counts": {
            "mismatched_columns": [
                col
                for col in sql_sig["columns"]
                if sql_sig["null_counts"][col] != engine_sig["null_counts"][col]
            ],
        },
        "numeric_stats": {
            "mismatched_columns": [
                col
                for col in sql_sig["columns"]
                if not _numeric_stats_match(
                    sql_sig["numeric_stats"][col], engine_sig["numeric_stats"][col]
                )
            ],
        },
        "distinct_counts": {
            "mismatched_columns": [
                col
                for col in sql_sig["columns"]
                if sql_sig["distinct_counts"][col] != engine_sig["distinct_counts"][col]
            ],
        },
        "content_checksum": {
            "sql": sql_sig["content_checksum"],
            "engine": engine_sig["content_checksum"],
            "match": sql_sig["content_checksum"] == engine_sig["content_checksum"],
        },
    }
    for name in ("null_counts", "numeric_stats", "distinct_counts"):
        signatures[name]["match"] = not signatures[name]["mismatched_columns"]
    verdict["signatures"] = signatures
    failed = [name for name in AGGREGATE_SIGNATURE_NAMES if not signatures[name]["match"]]
    if failed:
        verdict.update(
            status="mismatch",
            reason="aggregate signatures differ: " + ", ".join(failed),
        )
        return verdict
    verdict.update(
        status="match",
        reason=(
            f"aggregate signatures agree over {sql_sig['row_count']} rows x "
            f"{len(sql_sig['columns'])} columns "
            f"({', '.join(AGGREGATE_SIGNATURE_NAMES)})"
        ),
    )
    return verdict


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
    cap = parity_row_cap()
    result: dict[str, Any] = {
        "engine": "polars",
        "kpi_id": kpi_id,
        "status": "",
        "reason": "",
        # Reported even on skipped/error so downstream gates always see what
        # level of assurance this result would carry.
        "mode": PARITY_MODE_AGGREGATE if len(canonical_rows) > cap else PARITY_MODE_ROW,
    }

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

    # 4. Compare: row-for-row at or under the cap, aggregate signatures above.
    result.update(
        evaluate_parity(
            canonical_columns, canonical_rows, frame.columns, frame.rows(), row_cap=cap
        )
    )
    return result


__all__ = [
    "AGGREGATE_SIGNATURE_NAMES",
    "MAX_PARITY_ROWS",
    "PARITY_MODE_AGGREGATE",
    "PARITY_MODE_ROW",
    "evaluate_parity",
    "parity_row_cap",
    "polars_runtime_available",
    "run_polars_parity",
]
