"""Back-of-the-envelope volume math (step 1) and evidence-based volume estimation
from a directory of data files.

The interview pro-tip is to size the system *before* choosing technology:

    bytes/day = daily_active_users x sessions/user x events/session x payload_bytes

That single number rules streaming in or out and forces (or rules out) a
distributed engine. This module computes it, and also estimates volume from real
files on disk so the workspace advisor grounds its math in evidence rather than a
guessed-at payload size.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from minus_dataops.decisions import (
    _GB, _PB, _TB, _human_bytes, batch_vs_stream, compute_engine,
)

# File-format families, by extension, for evidence scanning.
_FORMAT_FAMILIES: dict[str, str] = {
    ".parquet": "Parquet (columnar)",
    ".pq": "Parquet (columnar)",
    ".avro": "Avro (row)",
    ".orc": "ORC (columnar)",
    ".json": "JSON (raw/semi-structured)",
    ".ndjson": "JSON (raw/semi-structured)",
    ".jsonl": "JSON (raw/semi-structured)",
    ".csv": "CSV (text)",
    ".tsv": "CSV (text)",
    ".txt": "Text",
    ".delta": "Delta (table format)",
    ".xlsx": "Excel",
    ".xls": "Excel",
}
# Directory names that signal an open table format is already in use.
_TABLE_FORMAT_MARKERS = {"_delta_log": "Delta Lake", "metadata": "Iceberg/Hudi (maybe)"}


@dataclass(frozen=True)
class VolumeEstimate:
    bytes_per_day: Optional[float]
    bytes_per_month: Optional[float]
    bytes_retained: Optional[float]
    human: dict[str, str]
    method: str                          # "back-of-envelope" | "file-scan" | "unknown"
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "bytes_per_day": self.bytes_per_day,
            "bytes_per_month": self.bytes_per_month,
            "bytes_retained": self.bytes_retained,
            "human": self.human,
            "method": self.method,
            "detail": self.detail,
        }


def back_of_envelope(daily_active_users: float,
                     sessions_per_user: float,
                     events_per_session: float,
                     payload_bytes: float,
                     retention_days: int = 90) -> VolumeEstimate:
    """The canonical estimate: DAU x sessions x events x payload."""
    per_day = (float(daily_active_users) * float(sessions_per_user)
               * float(events_per_session) * float(payload_bytes))
    per_month = per_day * 30.0
    retained = per_day * float(retention_days)
    return VolumeEstimate(
        bytes_per_day=per_day, bytes_per_month=per_month, bytes_retained=retained,
        human={"per_day": _human_bytes(per_day), "per_month": _human_bytes(per_month),
               "retained": _human_bytes(retained)},
        method="back-of-envelope",
        detail={"daily_active_users": daily_active_users,
                "sessions_per_user": sessions_per_user,
                "events_per_session": events_per_session,
                "payload_bytes": payload_bytes,
                "retention_days": retention_days})


def estimate_from_path(path: Path | str, *, max_files: int = 50_000) -> VolumeEstimate:
    """Estimate total data volume by scanning a directory tree for data files.

    This is a *footprint* estimate (bytes currently on disk), not a per-day rate;
    we expose it as ``bytes_retained`` and leave per-day None unless callers know
    the time span. Format breakdown is the genuinely useful output here.
    """
    root = Path(path)
    total = 0
    by_format: dict[str, dict[str, float]] = {}
    table_formats: set[str] = set()
    seen = 0

    if root.exists():
        for dirpath, dirnames, filenames in os.walk(root):
            base = os.path.basename(dirpath)
            if base in _TABLE_FORMAT_MARKERS:
                table_formats.add(_TABLE_FORMAT_MARKERS[base])
            for name in filenames:
                seen += 1
                if seen > max_files:
                    break
                ext = os.path.splitext(name)[1].lower()
                fam = _FORMAT_FAMILIES.get(ext)
                if fam is None:
                    continue
                try:
                    size = os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    continue
                total += size
                slot = by_format.setdefault(fam, {"bytes": 0.0, "files": 0.0})
                slot["bytes"] += size
                slot["files"] += 1
            if seen > max_files:
                break

    human_formats = {k: {"bytes": _human_bytes(v["bytes"]), "files": int(v["files"])}
                     for k, v in sorted(by_format.items(),
                                        key=lambda kv: kv[1]["bytes"], reverse=True)}
    return VolumeEstimate(
        bytes_per_day=None, bytes_per_month=None, bytes_retained=float(total),
        human={"footprint": _human_bytes(total)},
        method="file-scan" if total else "unknown",
        detail={"root": str(root), "formats": human_formats,
                "table_formats_detected": sorted(table_formats),
                "files_scanned": min(seen, max_files)})


def implications(est: VolumeEstimate) -> dict:
    """Turn an estimate into the two volume-driven decisions (engine + track)."""
    per_day = est.bytes_per_day if est.bytes_per_day is not None else est.bytes_retained
    engine = compute_engine(per_day)
    # Volume alone can't pick batch/stream (that's latency), but flag the scale.
    scale = "unknown"
    if per_day is not None:
        scale = ("PB-scale" if per_day >= _PB else "TB-scale" if per_day >= _TB
                 else "GB-scale" if per_day >= _GB else "sub-GB")
    return {"scale": scale, "engine": engine}


__all__ = ["VolumeEstimate", "back_of_envelope", "estimate_from_path", "implications"]
