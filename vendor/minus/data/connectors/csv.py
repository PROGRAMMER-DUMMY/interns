"""CSV connector. The datasource ``path`` is either a single .csv file or a
folder of .csv files; ``Table.file`` selects the file within a folder.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from minus.config.models import Table
from minus.data.connectors.base import Connector, register


@register
class CsvConnector(Connector):
    type = "csv"

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
            raise FileNotFoundError(f"CSV not found for table {table.name!r}: {path}")
        df = pl.read_csv(path, infer_schema_length=10000, **self.source.options)
        df = df.rename({c: str(c).strip() for c in df.columns})
        return _coerce(df)

    def list_columns(self, table: Table) -> list[str]:
        path = self._path_for(table)
        head = pl.read_csv(path, n_rows=0, **self.source.options)
        return [str(c).strip() for c in head.columns]

    def scan_source(self, table: Table):
        """DuckDB scans the CSV file directly (no full Python load)."""
        path = self._path_for(table)
        if not path.exists():
            return None
        p = path.resolve().as_posix().replace("'", "''")
        return f"read_csv_auto('{p}')"


def _coerce(df: pl.DataFrame) -> pl.DataFrame:
    """Best-effort typing: parse obvious date columns so date filters work."""
    for col in df.columns:
        if df.schema[col] != pl.String:
            continue
        if not any(tok in col.lower() for tok in ("date", "dob")):
            continue
        parsed = df.get_column(col).str.to_datetime(strict=False, exact=False)
        n = len(parsed)
        if n and parsed.is_not_null().sum() / n > 0.8:  # mostly parseable -> dates
            df = df.with_columns(parsed.alias(col))
    return df
