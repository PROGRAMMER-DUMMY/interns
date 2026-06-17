"""SQL connector backed by stdlib ``sqlite3``. The datasource ``path`` resolves
to a sqlite ``.db`` file; the DB table read is ``Table.file`` if set, else
``Table.name``. Set ``options={'query': '...'}`` to override the default SELECT.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import polars as pl

from minus.config.models import Table
from minus.data.connectors.base import Connector, register
from minus.data.connectors.csv import _coerce


@register
class SqlConnector(Connector):
    type = "sql"

    def _db_path(self) -> Path:
        return (self.root / self.source.path).resolve()

    def _table_name(self, table: Table) -> str:
        return table.file or table.name

    def _query_for(self, table: Table) -> str:
        query = self.source.options.get("query")
        if query:
            return query
        return f"SELECT * FROM {self._table_name(table)}"

    def read(self, table: Table) -> pl.DataFrame:
        path = self._db_path()
        if not path.exists():
            raise FileNotFoundError(
                f"SQLite DB not found for table {table.name!r}: {path}"
            )
        con = sqlite3.connect(str(path))
        try:
            df = pl.read_database(self._query_for(table), connection=con)
        finally:
            con.close()
        df = df.rename({c: str(c).strip() for c in df.columns})
        return _coerce(df)

    def list_columns(self, table: Table) -> list[str]:
        path = self._db_path()
        con = sqlite3.connect(str(path))
        try:
            head = pl.read_database(
                f"SELECT * FROM ({self._query_for(table)}) LIMIT 0", connection=con
            )
        finally:
            con.close()
        return [str(c).strip() for c in head.columns]
