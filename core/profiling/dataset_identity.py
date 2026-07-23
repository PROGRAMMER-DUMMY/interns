"""Identify whether a profile's ``dataset``/``path`` string is a local
filesystem path or a Unity Catalog fully-qualified table name, and derive a
display "stem" (short name) that works correctly for either.

``core.profiling.data_model_profiler`` profiles local files, where ``path``
is a filesystem path and ``Path(path).stem`` is the right way to get a short
display name. ``core.profiling.databricks_table_profiler`` profiles Unity
Catalog tables, where ``path`` is instead a backtick-quoted fqn string
(`` `catalog`.`schema`.`table` ``, see ``databricks_table_profiler
._qualified_name``) -- both profilers deliberately produce the same
``DatasetProfile`` shape so ~25 downstream consumers work unmodified, but
``Path(fqn).stem`` silently does the wrong thing for that string: it strips
everything after the LAST dot, treating the quoted table segment as if it
were a file extension, and returns the catalog+schema portion instead of the
table name. Found live: this exact bug in
``core.onboarding.kpi.feature_resolver``/``sql_generator`` before this
module existed.

Every caller that today does ``Path(dataset).stem``/``.name`` on a profile's
``path``/``dataset`` field should use ``dataset_display_stem``/
``dataset_display_name`` instead -- both are byte-identical to the
``Path(...)`` equivalent for every local path, and correct for a UC fqn.
"""
from __future__ import annotations

import re
from pathlib import Path

# Mirrors databricks_table_profiler._qualified_name's output exactly: three
# backtick-quoted identifier segments (each already validated as a safe bare
# identifier by assert_safe_identifier before quoting -- see core.sql_safety
# ._IDENT_RE -- so embedded backticks/dots inside a segment never occur in
# practice), joined by dots: `catalog`.`schema`.`table`.
_UC_FQN_RE = re.compile(
    r"^`([A-Za-z_][A-Za-z0-9_]*)`\.`([A-Za-z_][A-Za-z0-9_]*)`\.`([A-Za-z_][A-Za-z0-9_]*)`$"
)


def is_uc_fqn(raw: str) -> bool:
    """True when ``raw`` is a Unity Catalog `` `catalog`.`schema`.`table` `` fqn."""
    return bool(_UC_FQN_RE.match(str(raw or "")))


def uc_fqn_parts(raw: str) -> tuple[str, str, str] | None:
    """``(catalog, schema, table)`` for a UC fqn, else ``None``."""
    match = _UC_FQN_RE.match(str(raw or ""))
    return (match.group(1), match.group(2), match.group(3)) if match else None


def dataset_display_stem(raw: str) -> str:
    """Short display name for a profile's ``dataset``/``path`` value.

    The unquoted table name for a UC fqn; ``Path(raw).stem`` unchanged for
    every local path (CSV/Parquet/etc.) -- identical to today's behavior for
    every existing local-file workspace.
    """
    parts = uc_fqn_parts(raw)
    if parts is not None:
        return parts[2]
    return Path(str(raw or "")).stem


def dataset_display_name(raw: str) -> str:
    """Short display name including extension/table, for callers that today
    use ``Path(raw).name`` (e.g. for a case-preserving basename key). The
    table name for a UC fqn (a UC table has no file extension to preserve);
    ``Path(raw).name`` unchanged for every local path.
    """
    parts = uc_fqn_parts(raw)
    if parts is not None:
        return parts[2]
    return Path(str(raw or "")).name
