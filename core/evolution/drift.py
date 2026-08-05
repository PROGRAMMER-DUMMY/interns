"""Diff two discovery snapshots.

Severity is the whole point of the module: adding a column or a table is
information, but LOSING one -- or having one change type underneath a
downstream cast -- is a decision a human has to make.

The honest-unknown rule from discovery carries through here. ``tables[].columns``
is optional in ``discovery.json`` (several connectors read sizes but not
schemas). When either side of a table has no column list, the column comparison
for that table is skipped and recorded as ``columns_unknown``. Treating "not
measured" as "removed" would raise a fake blocker on every workspace whose
connector cannot read columns yet.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITY_INFO = "info"
SEVERITY_ACTION = "action_needed"

KIND_ADDED_TABLE = "added_table"
KIND_REMOVED_TABLE = "removed_table"
KIND_ADDED_COLUMN = "added_column"
KIND_REMOVED_COLUMN = "removed_column"
KIND_TYPE_CHANGE = "type_change"

SEVERITY_BY_KIND: dict[str, str] = {
    KIND_ADDED_TABLE: SEVERITY_INFO,
    KIND_ADDED_COLUMN: SEVERITY_INFO,
    KIND_REMOVED_TABLE: SEVERITY_ACTION,
    KIND_REMOVED_COLUMN: SEVERITY_ACTION,
    KIND_TYPE_CHANGE: SEVERITY_ACTION,
}

# discovery.json does not pin the column-entry key names (no scanner populates
# them yet), so read the two spellings a scanner would plausibly emit and never
# guess a value that is absent.
_NAME_KEYS = ("name", "column", "column_name")
_TYPE_KEYS = ("type", "data_type", "dtype")


@dataclass(frozen=True)
class DriftFinding:
    kind: str
    table: str
    column: str = ""
    old_type: str = ""
    new_type: str = ""

    @property
    def severity(self) -> str:
        return SEVERITY_BY_KIND.get(self.kind, SEVERITY_ACTION)

    @property
    def is_column_scoped(self) -> bool:
        return bool(self.column)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "severity": self.severity}


@dataclass(frozen=True)
class DriftReport:
    added_tables: list[str] = field(default_factory=list)
    removed_tables: list[str] = field(default_factory=list)
    added_columns: list[DriftFinding] = field(default_factory=list)
    removed_columns: list[DriftFinding] = field(default_factory=list)
    type_changes: list[DriftFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list[DriftFinding]:
        return [
            *[DriftFinding(KIND_ADDED_TABLE, table) for table in self.added_tables],
            *[DriftFinding(KIND_REMOVED_TABLE, table) for table in self.removed_tables],
            *self.added_columns,
            *self.removed_columns,
            *self.type_changes,
        ]

    @property
    def action_needed(self) -> list[DriftFinding]:
        return [f for f in self.findings if f.severity == SEVERITY_ACTION]

    @property
    def informational(self) -> list[DriftFinding]:
        return [f for f in self.findings if f.severity == SEVERITY_INFO]

    @property
    def has_drift(self) -> bool:
        return bool(self.findings)

    def summary(self) -> dict[str, Any]:
        return {
            "added_tables": list(self.added_tables),
            "removed_tables": list(self.removed_tables),
            "added_columns": [f.to_dict() for f in self.added_columns],
            "removed_columns": [f.to_dict() for f in self.removed_columns],
            "type_changes": [f.to_dict() for f in self.type_changes],
            "notes": list(self.notes),
            "action_needed_count": len(self.action_needed),
            "info_count": len(self.informational),
        }


def detect_drift(prev: dict[str, Any], curr: dict[str, Any]) -> DriftReport:
    """Compare two ``discovery.json`` payloads."""
    previous = _tables(prev)
    current = _tables(curr)

    added_tables = sorted(set(current) - set(previous))
    removed_tables = sorted(set(previous) - set(current))

    added_columns: list[DriftFinding] = []
    removed_columns: list[DriftFinding] = []
    type_changes: list[DriftFinding] = []
    notes: list[str] = []

    for table in sorted(set(previous) & set(current)):
        before = _columns(previous[table])
        after = _columns(current[table])
        if before is None or after is None:
            notes.append(
                f"columns_unknown: table `{table}` has no column list on "
                f"{'both sides' if before is None and after is None else 'one side'}; "
                "column drift not compared"
            )
            continue
        for name in sorted(set(after) - set(before)):
            added_columns.append(DriftFinding(KIND_ADDED_COLUMN, table, name, new_type=after[name]))
        for name in sorted(set(before) - set(after)):
            removed_columns.append(
                DriftFinding(KIND_REMOVED_COLUMN, table, name, old_type=before[name])
            )
        for name in sorted(set(before) & set(after)):
            if before[name].strip().lower() != after[name].strip().lower():
                type_changes.append(
                    DriftFinding(
                        KIND_TYPE_CHANGE, table, name, old_type=before[name], new_type=after[name]
                    )
                )

    return DriftReport(
        added_tables=added_tables,
        removed_tables=removed_tables,
        added_columns=added_columns,
        removed_columns=removed_columns,
        type_changes=type_changes,
        notes=notes,
    )


def _tables(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tables: dict[str, dict[str, Any]] = {}
    for entry in (payload or {}).get("tables") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if name:
            tables[name] = entry
    return tables


def _columns(table: dict[str, Any]) -> dict[str, str] | None:
    """``{column_name: declared_type}``, or None when the scanner did not read
    columns. An empty list is a measured "no columns", not unknown."""
    raw = table.get("columns")
    if not isinstance(raw, list):
        return None
    columns: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(_first(entry, _NAME_KEYS)).strip()
        if name:
            columns[name] = str(_first(entry, _TYPE_KEYS)).strip()
    return columns


def _first(entry: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = entry.get(key)
        if value:
            return str(value)
    return ""


__all__ = [
    "KIND_ADDED_COLUMN",
    "KIND_ADDED_TABLE",
    "KIND_REMOVED_COLUMN",
    "KIND_REMOVED_TABLE",
    "KIND_TYPE_CHANGE",
    "SEVERITY_ACTION",
    "SEVERITY_BY_KIND",
    "SEVERITY_INFO",
    "DriftFinding",
    "DriftReport",
    "detect_drift",
]
