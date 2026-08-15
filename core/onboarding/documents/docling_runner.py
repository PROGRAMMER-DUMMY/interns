"""Docling extraction runner -- executes INSIDE the isolated docling environment.

This module is deliberately standalone: it imports only ``docling`` and the stdlib, and
is invoked BY PATH (never ``-m``) because the isolated environment does not have this
repo installed. Keep it dependency-free with respect to ``core.*`` -- adding a repo
import here breaks the isolation boundary and the loader will fail at runtime, not at
import time.

Contract (the host side is :mod:`core.onboarding.documents.docling_loader`)::

    <isolated-python> core/onboarding/documents/docling_runner.py <input> --out <json>

Writes a JSON payload to ``--out`` rather than stdout, because docling emits progress
and model-loading chatter on stdout/stderr that would corrupt a piped payload.

Tables are exported structurally (columns + rows), not scraped from the Markdown:
docling's own serialization docs note that Markdown tables FLATTEN row/col spans, so a
spanned cell silently becomes empty. The Markdown is kept for prose; the tables are the
structured truth.

`export_to_dataframe(doc=...)` returns a pandas frame, but it never crosses the process
boundary -- it is converted to plain lists here and serialized as JSON, so the repo-side
DataFrame Rule (Polars, no pandas) is unaffected.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any


def _engine_version() -> str:
    try:
        import importlib.metadata as md

        return md.version("docling")
    except Exception:
        return "unknown"


def _table_payload(table: Any, document: Any, index: int) -> dict[str, Any]:
    """Structured table extraction. Falls back to a reason string per table."""
    try:
        frame = table.export_to_dataframe(doc=document)
    except TypeError:
        # Older docling signatures took no `doc` kwarg.
        frame = table.export_to_dataframe()
    columns = [str(c) for c in frame.columns]
    rows = [[None if v is None else str(v) for v in row] for row in frame.values.tolist()]
    return {
        "index": index,
        "columns": columns,
        "rows": rows,
        "num_rows": len(rows),
        "num_cols": len(columns),
    }


def extract(input_path: Path) -> dict[str, Any]:
    from docling.document_converter import DocumentConverter

    result = DocumentConverter().convert(str(input_path))
    document = result.document

    tables: list[dict[str, Any]] = []
    for index, table in enumerate(getattr(document, "tables", []) or []):
        try:
            tables.append(_table_payload(table, document, index))
        except Exception as exc:  # one bad table must not lose the whole document
            tables.append({"index": index, "error": f"{type(exc).__name__}: {exc}"})

    return {
        "ok": True,
        "engine": "docling",
        "engine_version": _engine_version(),
        "source_file": str(input_path),
        "markdown": document.export_to_markdown(),
        "tables": tables,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run docling inside its isolated env.")
    parser.add_argument("input", help="Path to the document to convert.")
    parser.add_argument("--out", required=True, help="Path to write the JSON payload.")
    args = parser.parse_args(argv)

    out_path = Path(args.out)
    try:
        payload = extract(Path(args.input))
    except Exception as exc:
        payload = {
            "ok": False,
            "engine": "docling",
            "reason": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=5),
        }

    try:
        out_path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        sys.stderr.write(f"docling_runner: could not write {out_path}: {exc}\n")
        return 1
    return 0 if payload.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
