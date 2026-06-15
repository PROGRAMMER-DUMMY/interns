"""Semantic model: loads tables, caches them, and assembles joined frames.

The key method is :meth:`assemble`, which — given a base table and a set of
fields possibly spanning other tables — follows declared relationships to build
a single DataFrame whose columns are named ``"table.column"``. This is what lets
a widget pull a measure from the fact table and a dimension from a joined table,
Power-BI style.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

import polars as pl

from minus.config.models import Project, Relationship
from minus.data.connectors.base import get_connector


def split_field(ref: str) -> tuple[str, str]:
    """'claims.ClaimAmount' -> ('claims', 'ClaimAmount')."""
    table, _, col = ref.partition(".")
    return table, col


class SemanticModel:
    def __init__(self, project: Project, root: Path | str):
        self.project = project
        self.root = Path(root)
        self._cache: dict[str, pl.DataFrame] = {}
        self._connectors: dict[str, object] = {}

    # -- raw table access -------------------------------------------------
    def _connector_for(self, name: str):
        tbl = self.project.table(name)
        ds = self.project.datasource(tbl.source)
        if ds.name not in self._connectors:
            self._connectors[ds.name] = get_connector(ds, self.root)
        return self._connectors[ds.name], tbl

    def table_df(self, name: str) -> pl.DataFrame:
        if name not in self._cache:
            conn, tbl = self._connector_for(name)
            self._cache[name] = conn.read(tbl)
        return self._cache[name]

    def scan_source(self, name: str):
        """A DuckDB-scannable SQL source (e.g. read_parquet('...')) for a table,
        or None when the connector can't be scanned in place. Lets the pushdown
        engine query files directly instead of loading them into memory."""
        conn, tbl = self._connector_for(name)
        try:
            return conn.scan_source(tbl)
        except Exception:
            return None

    def clear_cache(self) -> None:
        self._cache.clear()

    def columns(self, name: str) -> list[str]:
        return list(self.table_df(name).columns)

    # -- join graph -------------------------------------------------------
    def _neighbors(self, table: str) -> list[tuple[str, Relationship]]:
        out: list[tuple[str, Relationship]] = []
        for r in self.project.relationships:
            if r.from_table == table:
                out.append((r.to_table, r))
            elif r.to_table == table:
                out.append((r.from_table, r))
        return out

    def _path(self, base: str, target: str) -> list[tuple[str, str, Relationship]]:
        """BFS shortest path base->target as (from, to, relationship) hops."""
        if base == target:
            return []
        seen = {base}
        queue: deque[tuple[str, list]] = deque([(base, [])])
        while queue:
            node, hops = queue.popleft()
            for nbr, rel in self._neighbors(node):
                if nbr in seen:
                    continue
                new_hops = hops + [(node, nbr, rel)]
                if nbr == target:
                    return new_hops
                seen.add(nbr)
                queue.append((nbr, new_hops))
        raise KeyError(
            f"no relationship path from {base!r} to {target!r}; "
            f"add a relationship to project.yaml"
        )

    # -- assembly ---------------------------------------------------------
    def assemble(self, base: str, fields: list[str]) -> pl.DataFrame:
        """Return a frame with columns named 'table.column' covering ``fields``.

        ``base`` should be the table the measures aggregate over (the fact).
        """
        needed = {split_field(f)[0] for f in fields if "." in f}
        needed.add(base)

        df = _prefix(self.table_df(base), base)
        included = {base}

        # expand to each needed table along the shortest relationship path
        for target in needed:
            if target in included:
                continue
            for src, dst, rel in self._path(base, target):
                if dst in included:
                    continue
                df = self._merge_hop(df, dst, rel)
                included.add(dst)

        keep = [f"{t}.{c}" for f in fields for t, c in [split_field(f)] if "." in f]
        seen: set[str] = set()
        ordered = [k for k in keep if not (k in seen or seen.add(k))]
        missing = [k for k in ordered if k not in df.columns]
        if missing:
            raise KeyError(f"fields not found after join: {missing}")
        return df.select(ordered) if ordered else df

    def _merge_hop(self, df: pl.DataFrame, new_table: str, rel: Relationship) -> pl.DataFrame:
        right = _prefix(self.table_df(new_table), new_table)
        left_key = f"{rel.from_table}.{rel.from_column}"
        right_key = f"{rel.to_table}.{rel.to_column}"
        # one side of the keys lives in df, the other in `right`
        if left_key in df.columns and right_key in right.columns:
            return df.join(right, how="left", left_on=left_key, right_on=right_key)
        if right_key in df.columns and left_key in right.columns:
            return df.join(right, how="left", left_on=right_key, right_on=left_key)
        raise KeyError(
            f"cannot join {new_table!r}: keys {left_key}/{right_key} not aligned"
        )


def _prefix(df: pl.DataFrame, table: str) -> pl.DataFrame:
    return df.rename({c: f"{table}.{c}" for c in df.columns})
