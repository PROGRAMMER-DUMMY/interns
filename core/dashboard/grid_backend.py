"""Governed Server-Side Row Model (SSRM) backend for the dashboard data grid.

Translates a Dash AG Grid ``getRowsRequest`` (``startRow``/``endRow``,
``filterModel``, ``sortModel``) into a SAFE, parameterized DuckDB query over a
registered relation — a raw workspace dataset OR a KPI result view. This is the
Power-BI-style slice-and-dice engine: the grid pulls only the rows on screen and
DuckDB does the filter/sort/limit server-side, so a 50GB / 200-column source
never ships whole to the browser.

Security (non-negotiable — the naive reference implementation got this wrong):
- Column identifiers are validated with ``assert_safe_identifier`` and quoted; an
  unknown/hostile column is REJECTED, never interpolated.
- Filter VALUES are bound as DuckDB query PARAMETERS (``?``), never string-formatted,
  so a value like ``' OR 1=1; DROP …`` cannot inject SQL.
- Sort directions and filter operators come from fixed allow-lists.

Governance:
- Columns the workspace marks sensitive (``data_policy`` + ``pii_redaction``) are
  DROPPED from the column defs and the projection, and cannot be filtered or
  sorted. The grid is a rendered surface, so it honors the same redaction as the
  Data table. A redacted value that somehow reaches output is scrubbed defensively.

The module is dependency-light (DuckDB + core safety/redaction) and operates on a
*relation name* the caller has registered, so it is fully unit-testable without a
live Dash server.
"""
from __future__ import annotations

from typing import Any

from core.sql_safety import (
    assert_safe_identifier,
    escape_sql_literal,
    is_safe_identifier,
    quote_ident_sql,
)

# DuckDB type family -> AG Grid column filter. Anything unrecognized falls back to
# text so the grid still offers a (safe) filter.
_NUMERIC_TOKENS = ("INT", "DECIMAL", "DOUBLE", "FLOAT", "REAL", "NUMERIC", "HUGEINT", "BIGINT")
_DATE_TOKENS = ("DATE", "TIMESTAMP", "TIME")

# Number-filter operators AG Grid sends -> SQL operator. A type not in this map is
# ignored (never interpolated), so a hostile ``type`` cannot inject an operator.
_NUM_OPS: dict[str, str] = {
    "equals": "=",
    "notEqual": "!=",
    "greaterThan": ">",
    "greaterThanOrEqual": ">=",
    "lessThan": "<",
    "lessThanOrEqual": "<=",
}
# Text-filter operators -> (sql_template using a bound ? param, value_wrapper).
_SORT_DIRS: dict[str, str] = {"asc": "ASC", "desc": "DESC"}

# Aggregation functions AG Grid sends on a valueCol -> SQL function. A func not in
# this fixed allow-list is rejected (defaults to SUM for numerics / falls back),
# so a hostile ``aggFunc`` can never be interpolated into the SELECT.
_AGG_FUNCS: dict[str, str] = {
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "count": "COUNT",
}

# Hard cap on a single block so a crafted request can't ask for millions of rows.
_MAX_BLOCK = 1000


def aggrid_filter_for_type(duck_type: object) -> str:
    """AG Grid filter id for a DuckDB column type (workspace-agnostic)."""
    t = str(duck_type or "").upper()
    if any(tok in t for tok in _DATE_TOKENS):
        return "agDateColumnFilter"
    if any(tok in t for tok in _NUMERIC_TOKENS):
        return "agNumberColumnFilter"
    return "agTextColumnFilter"


def _is_numeric_type(duck_type: object) -> bool:
    t = str(duck_type or "").upper()
    return any(tok in t for tok in _NUMERIC_TOKENS)


def _contract_sensitive_columns(workspace_root: object) -> set[str]:
    """Lowercased columns the semantic contract marks ``is_sensitive``.

    Defense-in-depth for the RAW-data grid: a column flagged sensitive in the
    contract (e.g. ``MedicaidID``/``NPI``) that no display regex happens to match
    is still hidden, so the grid never shows a column the rest of the system masks.
    Never raises (a missing/unreadable contract -> empty set)."""
    if workspace_root is None:
        return set()
    try:
        from pathlib import Path

        from core.onboarding.kpi.sensitive_masking import load_sensitive_columns
        from core.storage.workspace_layout import WorkspaceLayout

        return {
            c.lower()
            for c in load_sensitive_columns(WorkspaceLayout(project_root=Path(str(workspace_root))))
        }
    except Exception:
        return set()


def redacted_columns(columns: list[str], workspace_root: object) -> set[str]:
    """Lowercased names of columns that must NOT appear on the rendered grid.

    Union of two governance sources so the grid never leaks what the system
    protects elsewhere: (1) the workspace's effective DISPLAY-redaction patterns
    (built-in PII + ``data_policy`` widening), and (2) the columns the semantic
    contract marks ``is_sensitive`` (the SQL-mask set). Loading must never break
    the grid, so failures fall back to the built-in patterns.
    """
    from core.onboarding.kpi.pii_redaction import (
        DEFAULT_PII_COLUMN_PATTERNS,
        is_pii_column,
        workspace_redaction_patterns,
    )

    try:
        patterns = (
            workspace_redaction_patterns(workspace_root)
            if workspace_root is not None
            else DEFAULT_PII_COLUMN_PATTERNS
        )
    except Exception:
        patterns = DEFAULT_PII_COLUMN_PATTERNS
    by_pattern = {
        c.lower() for c in columns if isinstance(c, str) and is_pii_column(c, patterns=patterns)
    }
    contract = _contract_sensitive_columns(workspace_root)
    by_contract = {c.lower() for c in columns if isinstance(c, str) and c.lower() in contract}
    return by_pattern | by_contract


def table_columns(con: Any, relation: str) -> list[tuple[str, str]]:
    """Return ``[(column_name, duckdb_type), ...]`` for a registered relation.

    ``relation`` must be a safe identifier (a view/table the caller registered);
    an unsafe name is rejected rather than interpolated.
    """
    assert_safe_identifier(relation, context="grid relation")
    rows = con.execute("DESCRIBE " + quote_ident_sql(relation)).fetchall()
    # DESCRIBE -> (column_name, column_type, null, key, default, extra)
    return [(str(r[0]), str(r[1])) for r in rows]


def visible_columns(
    con: Any, relation: str, *, workspace_root: object = None
) -> list[tuple[str, str]]:
    """Columns the grid may show: schema columns minus redacted ones."""
    cols = table_columns(con, relation)
    redacted = redacted_columns([c for c, _ in cols], workspace_root)
    return [(c, t) for c, t in cols if c.lower() not in redacted]


def generate_column_defs(
    con: Any, relation: str, *, workspace_root: object = None
) -> list[dict[str, Any]]:
    """Dynamic AG Grid column defs from the DuckDB schema (redacted cols excluded).

    Dtype -> filter mapping so any schema is configured automatically. Sortable +
    floating filter on; the menu enables column pin/hide in the UI."""
    defs: list[dict[str, Any]] = []
    for name, duck_type in visible_columns(con, relation, workspace_root=workspace_root):
        defs.append(
            {
                "field": name,
                "headerName": name.replace("_", " ").title(),
                "filter": aggrid_filter_for_type(duck_type),
                "sortable": True,
                "resizable": True,
            }
        )
    return defs


def _build_where(
    request: dict[str, Any], allowed_cols: dict[str, str]
) -> tuple[str, list[Any]]:
    """The parameterized WHERE clause + bound params for a getRows request.

    Any filter referencing a column not in ``allowed_cols`` (e.g. a redacted one)
    is silently dropped, never interpolated. Filter VALUES become bound ``?``
    parameters. Shared by the flat and the grouped query builders.
    """
    where_parts: list[str] = []
    params: list[Any] = []
    filter_model = request.get("filterModel") or {}
    if isinstance(filter_model, dict):
        for col, details in filter_model.items():
            if col not in allowed_cols or not is_safe_identifier(col) or not isinstance(details, dict):
                continue
            qcol = quote_ident_sql(col)
            ftype = str(details.get("filterType") or "")
            if ftype == "number" and _is_numeric_type(allowed_cols[col]):
                op = _NUM_OPS.get(str(details.get("type") or ""))
                val = details.get("filter")
                if op is not None and isinstance(val, (int, float)):
                    where_parts.append(f"{qcol} {op} ?")
                    params.append(val)
            else:
                # Text family (default): contains/equals via a bound parameter.
                val = details.get("filter")
                if val is None:
                    continue
                if str(details.get("type") or "") == "equals":
                    where_parts.append(f"CAST({qcol} AS VARCHAR) = ?")
                    params.append(str(val))
                else:
                    where_parts.append(f"CAST({qcol} AS VARCHAR) ILIKE ?")
                    params.append(f"%{val}%")
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    return where_sql, params


def _group_request(
    request: dict[str, Any], allowed_cols: dict[str, str]
) -> tuple[list[str], list[dict[str, str]], list[Any]]:
    """Resolve an AG Grid row-grouping/pivot request against the allow-list.

    Returns ``(group_cols, value_aggs, group_keys)`` where:
    * ``group_cols`` — the grouping columns still to EXPAND (the next level): the
      ``rowGroupCols`` AFTER the already-open ``groupKeys``, allow-list-filtered.
    * ``value_aggs`` — ``[{"col", "func"}]`` measures to aggregate; ``func`` is a
      key from the fixed ``_AGG_FUNCS`` allow-list (hostile funcs dropped).
    * ``group_keys`` — the already-open group key VALUES (bound as params), in the
      same order as the consumed ``rowGroupCols`` so the caller can build the
      equality predicates.

    Every column is checked against ``allowed_cols`` (redacted columns excluded),
    so a redacted column can never be grouped or aggregated. Returns empty
    ``group_cols`` when the request is not a grouping request (flat mode).
    """
    raw_group = request.get("rowGroupCols") or []
    group_field_order: list[str] = []
    if isinstance(raw_group, list):
        for g in raw_group:
            field = g.get("id") if isinstance(g, dict) else g
            if isinstance(field, str) and field in allowed_cols and is_safe_identifier(field):
                group_field_order.append(field)
    if not group_field_order:
        return [], [], []

    raw_keys = request.get("groupKeys") or []
    group_keys = list(raw_keys) if isinstance(raw_keys, list) else []
    # One group level is expanded per already-open key; the NEXT level is the
    # first un-opened rowGroupCol. Deeper requests than we have group cols -> none.
    depth = len(group_keys)
    next_cols = group_field_order[depth: depth + 1]

    value_aggs: list[dict[str, str]] = []
    raw_values = request.get("valueCols") or []
    if isinstance(raw_values, list):
        for v in raw_values:
            field = v.get("field") if isinstance(v, dict) else None
            if not (isinstance(field, str) and field in allowed_cols and is_safe_identifier(field)):
                continue
            func = str((v.get("aggFunc") if isinstance(v, dict) else "") or "").lower()
            if func not in _AGG_FUNCS:
                # Unknown/hostile agg func: default to SUM for a numeric, else skip
                # (never interpolate the client value).
                func = "sum" if _is_numeric_type(allowed_cols[field]) else "count"
            value_aggs.append({"col": field, "func": func})
    return next_cols, value_aggs, group_keys


def build_group_query(
    relation: str,
    request: dict[str, Any],
    allowed_cols: dict[str, str],
) -> tuple[str, list[Any], str, list[Any]]:
    """Parameterized GROUP BY query for one row-grouping/pivot level.

    Expands exactly one grouping level (the first ``rowGroupCol`` after the
    already-open ``groupKeys``), aggregating each ``valueCol`` with its allow-list
    agg func. The open ``groupKeys`` become bound equality predicates (cast to
    VARCHAR so a numeric group key from the client matches regardless of dtype).
    Identifiers are validated + quoted; values are bound ``?`` params.
    """
    assert_safe_identifier(relation, context="grid relation")
    rel = quote_ident_sql(relation)
    next_cols, value_aggs, group_keys = _group_request(request, allowed_cols)

    where_sql, params = _build_where(request, allowed_cols)
    # Open group keys -> equality predicates on the consumed group columns.
    raw_group = request.get("rowGroupCols") or []
    group_field_order = [
        (g.get("id") if isinstance(g, dict) else g)
        for g in raw_group
        if isinstance(g, (dict, str))
    ]
    group_field_order = [
        f for f in group_field_order
        if isinstance(f, str) and f in allowed_cols and is_safe_identifier(f)
    ]
    key_parts: list[str] = []
    key_params: list[Any] = []
    for i, key_val in enumerate(group_keys):
        if i >= len(group_field_order):
            break
        qcol = quote_ident_sql(group_field_order[i])
        key_parts.append(f"CAST({qcol} AS VARCHAR) = ?")
        key_params.append(str(key_val))
    if key_parts:
        glue = " AND " if where_sql else " WHERE "
        where_sql = where_sql + glue + " AND ".join(key_parts)
        params = params + key_params

    if not next_cols:
        # All grouping levels opened -> a leaf request: return the flat rows
        # (filtered to the open keys) so the deepest level shows detail rows.
        return build_rows_query(relation, request, allowed_cols, _where_override=(where_sql, params))

    gcol = next_cols[0]
    qgroup = quote_ident_sql(gcol)
    select_parts = [qgroup]
    for agg in value_aggs:
        func = _AGG_FUNCS[agg["func"]]
        select_parts.append(f"{func}({quote_ident_sql(agg['col'])}) AS {quote_ident_sql(agg['col'])}")
    select_sql = ", ".join(select_parts)

    # Sort within the level: honor a sortModel entry on the group col / a value col.
    order_parts: list[str] = []
    sort_model = request.get("sortModel") or []
    groupable = {gcol} | {a["col"] for a in value_aggs}
    if isinstance(sort_model, list):
        for s in sort_model:
            if not isinstance(s, dict):
                continue
            col = s.get("colId")
            direction = _SORT_DIRS.get(str(s.get("sort") or "").lower())
            if col in groupable and is_safe_identifier(col) and direction:
                order_parts.append(f"{quote_ident_sql(col)} {direction}")
    order_sql = (" ORDER BY " + ", ".join(order_parts)) if order_parts else f" ORDER BY {qgroup} ASC"

    start = max(0, int(request.get("startRow") or 0))
    end = int(request.get("endRow") or (start + 100))
    limit = max(0, min(_MAX_BLOCK, end - start))

    rows_sql = (
        f"SELECT {select_sql} FROM {rel}{where_sql} "
        f"GROUP BY {qgroup}{order_sql} LIMIT {limit} OFFSET {start}"
    )
    count_sql = f"SELECT COUNT(*) FROM (SELECT {qgroup} FROM {rel}{where_sql} GROUP BY {qgroup})"
    return rows_sql, params, count_sql, list(params)


def build_rows_query(
    relation: str,
    request: dict[str, Any],
    allowed_cols: dict[str, str],
    *,
    _where_override: tuple[str, list[Any]] | None = None,
) -> tuple[str, list[Any], str, list[Any]]:
    """Build the parameterized (rows_sql, params, count_sql, count_params).

    ``allowed_cols`` maps the columns the grid may touch -> their DuckDB type
    (already excludes redacted columns). Any filter/sort referencing a column not
    in ``allowed_cols`` is silently dropped — never interpolated. Filter values
    are returned as bound parameters, never embedded in SQL text.

    ``_where_override`` is an internal seam used by the grouped-leaf path to reuse
    an already-built (where_sql, params) that includes the open group-key
    predicates; external callers never pass it.
    """
    assert_safe_identifier(relation, context="grid relation")
    rel = quote_ident_sql(relation)

    if _where_override is not None:
        where_sql, params = _where_override
    else:
        where_sql, params = _build_where(request, allowed_cols)

    order_parts: list[str] = []
    sort_model = request.get("sortModel") or []
    if isinstance(sort_model, list):
        for s in sort_model:
            if not isinstance(s, dict):
                continue
            col = s.get("colId")
            direction = _SORT_DIRS.get(str(s.get("sort") or "").lower())
            if col in allowed_cols and is_safe_identifier(col) and direction:
                order_parts.append(f"{quote_ident_sql(col)} {direction}")
    order_sql = (" ORDER BY " + ", ".join(order_parts)) if order_parts else ""

    start = max(0, int(request.get("startRow") or 0))
    end = int(request.get("endRow") or (start + 100))
    limit = max(0, min(_MAX_BLOCK, end - start))

    select_cols = ", ".join(quote_ident_sql(c) for c in allowed_cols)
    rows_sql = f"SELECT {select_cols} FROM {rel}{where_sql}{order_sql} LIMIT {limit} OFFSET {start}"
    count_sql = f"SELECT COUNT(*) FROM {rel}{where_sql}"
    return rows_sql, params, count_sql, list(params)


def serve_rows(
    con: Any,
    relation: str,
    request: dict[str, Any],
    *,
    workspace_root: object = None,
) -> dict[str, Any]:
    """Execute one SSRM block request -> ``{"rowData": [...], "rowCount": N}``.

    Only non-redacted columns are projected; values are returned as plain dicts.
    Defensive: even if a redacted column were somehow in ``allowed_cols`` it would
    have been excluded by ``visible_columns``, so no masked value reaches output.
    """
    allowed = {c: t for c, t in visible_columns(con, relation, workspace_root=workspace_root)}
    if not allowed:
        return {"rowData": [], "rowCount": 0}
    # Phase C: a grouping/pivot request (allow-listed rowGroupCols present) goes
    # through the GROUP BY builder. build_group_query expands one un-opened level,
    # or — when every level is already opened (groupKeys) — returns the leaf rows
    # filtered to those open keys. A request with NO allow-listed rowGroupCols is a
    # plain flat read.
    next_cols, _value_aggs, _keys = _group_request(request, allowed)
    raw_group = request.get("rowGroupCols") or []
    is_grouping = bool(next_cols) or (
        bool(_keys) and any(
            (g.get("id") if isinstance(g, dict) else g) in allowed for g in raw_group
            if isinstance(g, (dict, str))
        )
    )
    if is_grouping:
        rows_sql, params, count_sql, count_params = build_group_query(relation, request, allowed)
    else:
        rows_sql, params, count_sql, count_params = build_rows_query(relation, request, allowed)
    cur = con.execute(rows_sql, params)
    col_names = [d[0] for d in cur.description]
    records = [dict(zip(col_names, row)) for row in cur.fetchall()]
    total = con.execute(count_sql, count_params).fetchone()[0]
    return {"rowData": records, "rowCount": int(total)}


# --- Source discovery + registration (Phase B) ----------------------------
# A grid "source" is one of:
#   * a raw workspace dataset (CSV / Delta) -> registered with read_csv_auto /
#     delta_scan, the SAME read path the generated KPI SQL uses; or
#   * a KPI result view -> materialized by executing the KPI's generated SQL.
# Each source has a stable, SAFE relation name (assert_safe_identifier-clean) that
# the SSRM callback hands to serve_rows. Discovery is dynamic (filesystem scan +
# KPI registry), so it is fully workspace-agnostic.

_CSV_SUFFIXES = (".csv",)


def _safe_relation(prefix: str, raw: str) -> str:
    """A safe, unique relation name from an arbitrary path stem (workspace-agnostic).

    Non-identifier characters collapse to ``_`` so any dataset filename yields a
    valid, injection-free DuckDB view name.
    """
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in raw)
    cleaned = cleaned.strip("_") or "src"
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return f"{prefix}_{cleaned}"


def grid_sources(workspace_root: object) -> list[dict[str, str]]:
    """Selectable grid sources for a workspace: raw datasets + KPI result views.

    Returns ``[{"id", "label", "kind", "path"|"kpi_id"}]``. ``kind`` is ``dataset``
    (CSV/Delta) or ``kpi``. ``id`` is a safe relation name. Pure discovery — no
    DuckDB connection needed and never raises (a broken workspace -> fewer
    sources, not a crash)."""
    from pathlib import Path

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    if workspace_root is None:
        return out
    ws = Path(str(workspace_root))

    # (a) raw datasets under datasets/ — CSV files + Delta table dirs (_delta_log).
    datasets_dir = ws / "datasets"
    if datasets_dir.is_dir():
        try:
            csvs = sorted(p for p in datasets_dir.rglob("*") if p.suffix.lower() in _CSV_SUFFIXES)
        except OSError:
            csvs = []
        for p in csvs:
            rel = _safe_relation("ds", p.stem)
            base, i = rel, 2
            while rel in seen:
                rel = f"{base}_{i}"; i += 1
            seen.add(rel)
            try:
                label = str(p.relative_to(datasets_dir))
            except ValueError:
                label = p.name
            out.append({"id": rel, "label": f"dataset · {label}", "kind": "dataset",
                        "path": str(p), "fmt": "csv"})
        try:
            delta_dirs = sorted(
                p.parent for p in datasets_dir.rglob("_delta_log") if p.is_dir()
            )
        except OSError:
            delta_dirs = []
        for d in delta_dirs:
            rel = _safe_relation("ds", d.name)
            base, i = rel, 2
            while rel in seen:
                rel = f"{base}_{i}"; i += 1
            seen.add(rel)
            try:
                label = str(d.relative_to(datasets_dir))
            except ValueError:
                label = d.name
            out.append({"id": rel, "label": f"dataset · {label} (delta)", "kind": "dataset",
                        "path": str(d), "fmt": "delta"})

    # (b) KPI result views — one per KPI that has generated SQL on disk.
    solutions = ws / "interns" / "generated" / "solutions"
    if solutions.is_dir():
        try:
            sqls = sorted(solutions.glob("kpi_*.sql"))
        except OSError:
            sqls = []
        for sql_path in sqls:
            kpi_id = sql_path.stem
            rel = _safe_relation("kpi", kpi_id)
            if rel in seen:
                continue
            seen.add(rel)
            out.append({"id": rel, "label": f"KPI · {kpi_id}", "kind": "kpi",
                        "kpi_id": kpi_id, "path": str(sql_path), "fmt": "sql"})
    return out


def register_source(con: Any, source: dict[str, str], *, repo_root: object = None) -> str:
    """Register the chosen grid source as a DuckDB relation; return its view name.

    * ``dataset`` (csv)  -> ``CREATE VIEW <id> AS SELECT * FROM read_csv_auto(?)``.
    * ``dataset`` (delta)-> ``... FROM delta_scan(?)`` (INSTALL/LOAD delta first).
    * ``kpi``            -> execute the KPI's generated SQL (which itself registers
      ``<kpi>_results``), then alias it to the source ``id``.

    The relation name is asserted safe. DuckDB cannot bind a ``?`` parameter inside
    a table function in a ``CREATE VIEW``, so the PATH is rendered as a quote-escaped
    SQL string LITERAL (``escape_sql_literal``) — an embedded quote can no longer
    break out. Paths come from filesystem discovery (``grid_sources``), not client
    input. KPI SQL is trusted generated text, executed under repo_root cwd like the
    renderer."""
    rel = source.get("id") or ""
    assert_safe_identifier(rel, context="grid source relation")
    qrel = quote_ident_sql(rel)
    kind = source.get("kind")
    fmt = source.get("fmt")
    if kind == "dataset" and fmt == "csv":
        path_lit = escape_sql_literal(source.get("path"))
        con.execute(
            f"CREATE OR REPLACE VIEW {qrel} AS SELECT * FROM read_csv_auto({path_lit})"
        )
        return rel
    if kind == "dataset" and fmt == "delta":
        try:
            con.execute("INSTALL delta; LOAD delta;")
        except Exception:
            pass
        path_lit = escape_sql_literal(source.get("path"))
        con.execute(
            f"CREATE OR REPLACE VIEW {qrel} AS SELECT * FROM delta_scan({path_lit})"
        )
        return rel
    if kind == "kpi":
        from pathlib import Path

        from core.dashboard.renderer import _at_repo_root

        kpi_id = source.get("kpi_id") or ""
        assert_safe_identifier(kpi_id, context="grid kpi id")
        sql_text = Path(str(source.get("path"))).read_text(encoding="utf-8")
        results_view = f"{kpi_id}_results"
        assert_safe_identifier(results_view, context="grid kpi results view")
        root = Path(str(repo_root)) if repo_root is not None else Path.cwd()
        with _at_repo_root(root):
            con.execute(sql_text)
        con.execute(
            f"CREATE OR REPLACE VIEW {qrel} AS SELECT * FROM {quote_ident_sql(results_view)}"
        )
        return rel
    raise ValueError(f"unknown grid source kind: {kind!r}")


__all__ = [
    "aggrid_filter_for_type",
    "build_group_query",
    "build_rows_query",
    "generate_column_defs",
    "grid_sources",
    "redacted_columns",
    "register_source",
    "serve_rows",
    "table_columns",
    "visible_columns",
]
