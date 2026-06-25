"""Storage-efficiency measurement for the medallion build.

You cannot tune compression / partitioning / compaction without first MEASURING
storage. This module reads the materialized layers (DuckDB warehouse tables +
on-disk bronze Delta/Parquet) and reports per-table bytes, row counts, file
counts, average file size, and the raw->stored compression ratio.

Generic: no domain assumptions; degrades gracefully when a layer is absent.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def _dir_bytes_and_files(path: Path) -> tuple[int, int]:
    """Total bytes + parquet file count under a directory (a Delta table dir)."""
    total = 0
    files = 0
    if not path.exists():
        return 0, 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
            if p.suffix == ".parquet":
                files += 1
    return total, files


def _table_rows(con: Any, fqn: str) -> Optional[int]:
    try:
        return int(con.execute(f'SELECT COUNT(*) FROM {fqn}').fetchone()[0])
    except Exception:
        return None


def _duckdb_block_size(con: Any) -> int:
    try:
        return int(con.execute("PRAGMA database_size").df()["block_size"][0])
    except Exception:
        return 262144  # DuckDB default 256 KB


def _duckdb_table_bytes(con: Any, fqn: str, block_size: int) -> Optional[int]:
    """Real on-disk bytes for a DuckDB table = distinct allocated blocks x block
    size. ``duckdb_tables().estimated_size`` is CARDINALITY (rows), not bytes, so
    it must not be used here -- block accounting is the true storage measure."""
    try:
        blocks = con.execute(
            f"SELECT count(DISTINCT block_id) FROM pragma_storage_info('{fqn}') "
            "WHERE block_id >= 0"
        ).fetchone()[0]
        return int(blocks or 0) * block_size
    except Exception:
        return None


def build_storage_report(
    con: Any,
    *,
    workspace: Path,
    bronze_dir: Optional[Path] = None,
    raw_source_bytes: int = 0,
) -> dict[str, Any]:
    """Measure storage across the materialized layers. ``con`` is the open
    DuckDB warehouse connection; ``bronze_dir`` is the on-disk Delta bronze root
    (optional). ``raw_source_bytes`` is the total size of the raw source files,
    for the compression ratio."""
    # Flush in-memory/WAL data to disk blocks so block accounting + the file size
    # reflect the materialized tables (a fresh CREATE may not be checkpointed yet).
    try:
        con.execute("CHECKPOINT")
    except Exception:
        pass
    block_size = _duckdb_block_size(con)
    tables: list[dict[str, Any]] = []
    table_bytes_sum = 0
    try:
        rows = con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema IN ('bronze','silver','gold') ORDER BY 1,2"
        ).fetchall()
    except Exception:
        rows = []
    for schema, table in rows:
        fqn = f"{schema}.{table}"
        n_rows = _table_rows(con, fqn)
        nbytes = _duckdb_table_bytes(con, fqn, block_size) or 0
        # Prefer measured on-disk bytes for bronze Delta tables when available.
        files = None
        if schema == "bronze" and bronze_dir is not None:
            disk_bytes, n_files = _dir_bytes_and_files(bronze_dir / table)
            if disk_bytes:
                nbytes, files = disk_bytes, n_files
        table_bytes_sum += nbytes
        entry: dict[str, Any] = {
            "table": fqn,
            "layer": schema,
            "rows": n_rows,
            "stored_bytes": nbytes,
            "bytes_per_row": round(nbytes / n_rows, 2) if n_rows else None,
        }
        if files is not None:
            entry["parquet_files"] = files
            entry["avg_file_bytes"] = round(nbytes / files) if files else None
        tables.append(entry)

    # Authoritative total = the real warehouse file size on disk (block sums per
    # table over-count shared/partial blocks). Fall back to the per-table sum.
    db_file_bytes = 0
    try:
        for p in (workspace / "interns" / "state" / "medallion").glob("*.duckdb"):
            db_file_bytes += p.stat().st_size
    except OSError:
        pass
    total_stored = db_file_bytes or table_bytes_sum
    compression_ratio = (
        round(raw_source_bytes / total_stored, 3)
        if raw_source_bytes and total_stored else None
    )
    return {
        "artifact_type": "storage_report.json",
        "version": 1,
        "generated_by": "medallion-build",
        "summary": {
            "raw_source_bytes": raw_source_bytes,
            "total_stored_bytes": total_stored,
            "warehouse_file_bytes": db_file_bytes,
            "compression_ratio_raw_to_stored": compression_ratio,
            "table_count": len(tables),
            # Small-file heuristic: many tiny parquet files hurts I/O. Flag tables
            # whose average parquet file is under 16 MB (the classic threshold).
            "small_file_tables": [
                t["table"] for t in tables
                if t.get("avg_file_bytes") and t["avg_file_bytes"] < 16 * 1024 * 1024
                and (t.get("parquet_files") or 0) > 1
            ],
        },
        "tables": tables,
    }


def write_storage_report(report: dict[str, Any], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "storage_report.json"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    s = report["summary"]
    lines = [
        "# Storage Report (medallion build)",
        "",
        f"- Raw source: {_human(s['raw_source_bytes'])}",
        f"- Stored (bronze+silver+gold): {_human(s['total_stored_bytes'])}",
        f"- Compression ratio (raw -> stored): "
        f"{s['compression_ratio_raw_to_stored'] or 'n/a'}x",
        f"- Tables measured: {s['table_count']}",
        "",
        "| Table | Layer | Rows | Stored | Bytes/row |",
        "|-------|-------|------|--------|-----------|",
    ]
    for t in report["tables"]:
        lines.append(
            f"| `{t['table']}` | {t['layer']} | {t['rows']} | "
            f"{_human(t['stored_bytes'])} | {t['bytes_per_row'] or '-'} |"
        )
    if s["small_file_tables"]:
        lines += ["", "## Small-file warning (avg < 16 MB, >1 file)",
                  ""] + [f"- `{t}` -- candidate for OPTIMIZE/compaction"
                         for t in s["small_file_tables"]]
    (report_dir / "storage_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path


def _human(n: Optional[int]) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


__all__ = ["build_storage_report", "write_storage_report"]
