"""Parquet connector. The datasource ``path`` is either a single .parquet file
or a folder of .parquet files; ``Table.file`` selects the file within a folder.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from minus.config.models import Table
from minus.data.connectors.base import Connector, register
from minus.data.connectors.csv import _coerce


@register
class ParquetConnector(Connector):
    type = "parquet"

    def _path_for(self, table: Table) -> Path:
        base = (self.root / self.source.path).resolve()
        if base.is_dir():
            if not table.file:
                raise ValueError(
                    f"table {table.name!r} reads from folder datasource "
                    f"{self.source.name!r} but has no 'file'"
                )
            return base / table.file
        return base

    def read(self, table: Table) -> pl.DataFrame:
        path = self._path_for(table)
        if not path.exists():
            raise FileNotFoundError(
                f"Parquet not found for table {table.name!r}: {path}"
            )
        df = pl.read_parquet(path, **self.source.options)
        df = df.rename({c: str(c).strip() for c in df.columns})
        return _coerce(df)

    def list_columns(self, table: Table) -> list[str]:
        path = self._path_for(table)
        # Cheap: read the schema only (no row groups).
        names = pl.scan_parquet(path).collect_schema().names()
        return [str(c).strip() for c in names]

    def scan_source(self, table: Table):
        """DuckDB scans the parquet file directly (no full Python load)."""
        path = self._path_for(table)
        if not path.exists():
            return None
        p = path.resolve().as_posix().replace("'", "''")
        return f"read_parquet('{p}')"
