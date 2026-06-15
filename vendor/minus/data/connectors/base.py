"""Connector interface. Add new backends (SQL, parquet, API) by subclassing.

A connector turns a (DataSource, Table) pair into a Polars DataFrame.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import polars as pl

from minus.config.models import DataSource, Table


class Connector(ABC):
    type: str = ""

    def __init__(self, source: DataSource, root: Path):
        self.source = source
        self.root = Path(root)

    @abstractmethod
    def read(self, table: Table) -> pl.DataFrame:
        """Return the table's rows as a DataFrame."""

    @abstractmethod
    def list_columns(self, table: Table) -> list[str]:
        """Return column names without loading the full table (best effort)."""

    def scan_source(self, table: Table) -> Optional[str]:
        """Return a DuckDB-scannable SQL source for this table, or None.

        File-backed connectors return e.g. ``read_parquet('/abs/path')`` so the
        pushdown engine can query the file IN PLACE (predicate/projection
        pushdown, larger-than-memory) instead of loading the whole table into
        Python memory first. Connectors that can't be scanned return None and the
        engine falls back to the in-memory frame.
        """
        return None


_REGISTRY: dict[str, type[Connector]] = {}


def register(cls: type[Connector]) -> type[Connector]:
    _REGISTRY[cls.type] = cls
    return cls


def get_connector(source: DataSource, root: Path) -> Connector:
    if source.type not in _REGISTRY:
        raise ValueError(
            f"no connector for datasource type {source.type!r}; "
            f"available: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[source.type](source, root)
