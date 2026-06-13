"""Dictionary-vs-profile evidence reconciliation.

A workspace data dictionary is *documentation evidence*, not ground truth: it
can be stale, partial, or simply wrong. This module cross-checks every
dictionary claim against generated profile evidence and emits structured
``dictionary_conflicts`` so downstream stages (feature resolution, the blocker
question panel, validation) can stop a documented-but-false claim from
silently shaping KPI logic.

Checks are derived purely from evidence shapes -- dictionary text plus profile
statistics -- never from curated business vocabulary:

1. ``enum_mismatch`` -- the description declares an enumeration of code values
   (``X / Y / Z`` or ``X or Y``) but the profiled values of that column do not
   match the declared vocabulary. Zero overlap is an error; observed values
   outside a partially-matching declared set are a warning.
2. ``unit_mismatch`` -- the description claims a single unit/qualifier in a
   measurement context ("in kilograms", "per mile", "in GBP", "(kg)") while a
   sibling code column in the same dataset observes that qualifier MIXED with
   at least one other qualifier-shaped value. The documented single-unit
   claim provably does not hold for every row.
3. ``phantom_column`` -- the dictionary documents a column that exists in no
   profiled dataset (error), or not in the documented table while a
   same-named column exists elsewhere (``misplaced_column`` warning).
4. ``misattributed_claim`` -- the description of ``T1.C`` is worded around a
   term that names a DIFFERENT profiled dataset which carries its own column
   ``C`` (and the description does not reference that dataset as a join
   target). The documented meaning may belong to the other dataset's column,
   so the claim is surfaced as a warning instead of being trusted.

The contract is written by the KPI feature resolver to
``interns/generated/contracts/dictionary_conflicts.json``. Platform code never
reads hostile-workspace scoring files; everything here derives from
``data_dictionary``-shaped CSVs and ``profile_index.json``.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.contracts.versioning import register_contract
from core.storage.workspace_layout import WorkspaceLayout

LOGGER = logging.getLogger(__name__)

ARTIFACT_TYPE = "dictionary_conflicts.json"
ARTIFACT_VERSION = 1
CONFLICTS_FILENAME = "dictionary_conflicts.json"

register_contract(ARTIFACT_TYPE, current_version=ARTIFACT_VERSION)

CONFLICT_TYPES = (
    "enum_mismatch",
    "unit_mismatch",
    "phantom_column",
    "misplaced_column",
    "misattributed_claim",
)
BLOCKING_SEVERITY = "error"
DICTIONARY_CONFLICT_RESOLUTION_TYPE = "dictionary_conflict"
# Mirrors core.onboarding.memory.workspace_definitions.READY_STATES without
# importing it (this module sits below the memory layer in the import graph).
_READY_STATES = {
    "proven_direct",
    "proven_alias",
    "proven_join",
    "proven_formula",
    "proven_taxonomy",
    "user_confirmed",
}

# Universal measurement-unit word forms -> canonical short code. These are
# standard physical units (mass / length / volume / duration), not business or
# domain vocabulary. Ambiguous single letters ("t", "m", "l", "g") and the
# English preposition "in" are deliberately excluded; observed VALUES are
# matched through the same map so "KG" and "kilograms" canonicalize
# identically. Explicit code tokens in descriptions (e.g. an uppercase
# currency code) need no lexicon at all -- they match observed codes verbatim.
_UNIT_WORD_FORMS: dict[str, str] = {
    # mass
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
    "kilogram": "kg", "kilogramme": "kg", "kilogrammes": "kg", "kilograms": "kg",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "gram": "g", "grams": "g", "gramme": "g", "grammes": "g",
    "tonne": "t", "tonnes": "t", "ton": "t", "tons": "t",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    # length / distance
    "km": "km", "kilometer": "km", "kilometers": "km",
    "kilometre": "km", "kilometres": "km",
    "mi": "mi", "mile": "mi", "miles": "mi",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "ft": "ft", "foot": "ft", "feet": "ft",
    "cm": "cm", "mm": "mm", "inch": "in", "inches": "in",
    # volume
    "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "ml": "ml", "gal": "gal", "gallon": "gal", "gallons": "gal",
    # duration
    "hr": "hr", "hrs": "hr", "hour": "hr", "hours": "hr",
    "minute": "min", "minutes": "min", "mins": "min",
    "second": "sec", "seconds": "sec", "secs": "sec",
    "day": "day", "days": "day", "week": "week", "weeks": "week",
    "month": "month", "months": "month", "year": "year", "years": "year",
}

_ENUM_RUN_RE = re.compile(
    r"\b[A-Z][A-Z0-9_]*\b(?:\s*/\s*\b[A-Z][A-Z0-9_]*\b)+"
)
_ENUM_OR_RE = re.compile(r"\b([A-Z][A-Z0-9_]+)\s+or\s+([A-Z][A-Z0-9_]+)\b")
_CODE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]{1,7}\b")
_CODE_VALUE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,11}$")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# A unit WORD only counts as a single-unit claim in a measurement context:
# "in kilograms", "per mile", or a parenthesized "(kg)". Bare temporal words
# in ordinary prose ("time of day", "start date") are not unit claims.
_MEASUREMENT_CONTEXT_RE = re.compile(
    r"\b(?:in|per)\s+([A-Za-z]+)\b|\(\s*([A-Za-z]+)\s*\)",
    re.IGNORECASE,
)
# "joins to <table>.<column>" / any qualified <table>.<column> reference: the
# referenced table is a deliberate cross-reference, not a misattribution.
_QUALIFIED_REF_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b"
)

_MAX_QUALIFIER_CARDINALITY = 12
_MAX_ENUM_OBSERVED_CARDINALITY = 12
_MIN_ATTRIBUTION_WORD_LEN = 4


# ---------------------------------------------------------------------------
# Dictionary loading (shared with the KPI feature resolver)
# ---------------------------------------------------------------------------

def load_data_dictionary_rows(
    workspace: Path,
    repo_root: Path,
    layout: WorkspaceLayout | None = None,
) -> list[dict[str, Any]]:
    """Parse every data-dictionary-shaped CSV in the workspace into rows.

    A dictionary CSV has a table/entity column plus a field/column column; a
    description column is optional. Rows are ``{table, field, description,
    path}`` with ``path`` repo-relative. Unreadable files contribute nothing.
    """
    workspace = Path(workspace)
    repo_root = Path(repo_root)
    layout = layout or WorkspaceLayout(project_root=workspace)
    paths: list[Path] = []
    inventory_path = layout.requirements_dir / "input_inventory.json"
    if inventory_path.exists():
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            inventory = {}
        for item in inventory.get("data_models") or []:
            path = (repo_root / str(item)).resolve()
            if path.suffix.lower() == ".csv" and "dictionary" in path.stem.lower():
                paths.append(path)
    paths.extend(
        path
        for path in workspace.rglob("*dictionary*.csv")
        if path.is_file() and "/interns/" not in path.as_posix()
    )
    rows: list[dict[str, Any]] = []
    for path in sorted(set(paths)):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fieldnames = {
                    str(name).strip().lower(): name for name in (reader.fieldnames or [])
                }
                table_key = _first_present(fieldnames, ["table", "entity", "dataset", "file"])
                field_key = _first_present(fieldnames, ["field", "column", "name"])
                description_key = _first_present(
                    fieldnames, ["description", "definition", "meaning"]
                )
                if not table_key or not field_key:
                    continue
                for row in reader:
                    table = str(row.get(table_key) or "").strip()
                    field = str(row.get(field_key) or "").strip()
                    if not table or not field:
                        continue
                    rows.append(
                        {
                            "table": table,
                            "field": field,
                            "description": str(row.get(description_key) or "").strip()
                            if description_key
                            else "",
                            "path": _rel(path, repo_root),
                        }
                    )
        except OSError:
            continue
    return rows


def _first_present(fieldnames: dict[str, str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in fieldnames:
            return fieldnames[candidate]
    return None


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile_dictionary_claims(
    rows: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Cross-check dictionary rows against profile evidence.

    Returns structured conflict dicts (see module docstring for types). The
    check is conservative: a claim is only flagged when profile evidence
    actively contradicts it -- absence of evidence is never a conflict.
    """
    datasets = _dataset_views(profiles)
    if not datasets:
        return []
    all_columns: set[str] = set()
    for view in datasets:
        all_columns.update(view["columns_norm"])

    conflicts: list[dict[str, Any]] = []
    for row in rows:
        table_norm = _norm(str(row.get("table") or ""))
        field_norm = _norm(str(row.get("field") or ""))
        if not table_norm or not field_norm:
            continue
        resolved = _resolve_table(table_norm, datasets)
        if not resolved:
            # No profiled dataset corresponds to the documented table: there
            # is no evidence either way, so nothing is asserted.
            continue
        present = [
            view for view in resolved if field_norm in view["columns_norm"]
        ]
        if not present:
            conflicts.extend(_missing_column_conflict(row, resolved, field_norm, all_columns))
            continue
        for view in present:
            column_name = view["columns_norm"][field_norm]
            column_payload = view["column_payloads"].get(column_name) or {}
            conflicts.extend(_enum_conflicts(row, view, column_name, column_payload))
            conflicts.extend(_qualifier_conflicts(row, view, column_name))
            conflicts.extend(
                _attribution_conflicts(row, view, column_name, field_norm, datasets)
            )

    for index, conflict in enumerate(conflicts, start=1):
        conflict["conflict_id"] = f"dict_conflict_{index:03d}"
    return conflicts


def _dataset_views(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        path = str(profile.get("path") or "")
        schema = profile.get("schema") or {}
        if not path or not isinstance(schema, dict):
            continue
        columns_norm = {
            _norm(str(column)): str(column)
            for column in schema.keys()
            if str(column).strip()
        }
        column_payloads = {
            str(item.get("name") or ""): item
            for item in (profile.get("columns") or [])
            if isinstance(item, dict)
        }
        stem = _path_stem(path)
        views.append(
            {
                "path": path,
                "name": _path_name(path),
                "stem_norm": _norm(stem),
                "stem_words": _identifier_words(stem),
                "profile_path": str(profile.get("profile_path") or ""),
                "row_count": profile.get("row_count"),
                "columns_norm": columns_norm,
                "column_payloads": column_payloads,
            }
        )
    return views


def _resolve_table(table_norm: str, datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exact = [view for view in datasets if view["stem_norm"] == table_norm]
    if exact:
        return exact
    # Prefixed physical names (e.g. a source-system or folder prefix glued to
    # the documented table name) still resolve by suffix.
    return [
        view
        for view in datasets
        if view["stem_norm"].endswith(table_norm) and len(table_norm) >= 3
    ]


def _missing_column_conflict(
    row: dict[str, Any],
    resolved: list[dict[str, Any]],
    field_norm: str,
    all_columns: set[str],
) -> list[dict[str, Any]]:
    table_names = ", ".join(sorted(view["name"] for view in resolved))
    base = _base_conflict(row, resolved[0])
    if field_norm in all_columns:
        return [
            {
                **base,
                "type": "misplaced_column",
                "severity": "warning",
                "column": "",
                "detail": (
                    f"The dictionary documents `{row.get('table')}.{row.get('field')}`, "
                    f"but the resolved dataset(s) ({table_names}) do not contain that "
                    "column; a column with that name exists in a different dataset. "
                    "The dictionary entry may be stale or misfiled."
                ),
                "resolution_hint": (
                    "Confirm which dataset actually carries this documented field, "
                    "or correct the dictionary entry."
                ),
            }
        ]
    return [
        {
            **base,
            "type": "phantom_column",
            "severity": "error",
            "column": "",
            "detail": (
                f"The dictionary documents `{row.get('table')}.{row.get('field')}`, "
                "but no profiled dataset contains that column. The documented "
                "column does not exist (renamed, dropped, or never shipped)."
            ),
            "resolution_hint": (
                "Identify which physical column (if any) replaced this documented "
                "field, or mark the dictionary entry as obsolete."
            ),
        }
    ]


def _enum_conflicts(
    row: dict[str, Any],
    view: dict[str, Any],
    column_name: str,
    column_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    declared = declared_enum_values(str(row.get("description") or ""))
    if len(declared) < 2:
        return []
    observed = _observed_code_values(column_payload)
    if not observed:
        return []
    declared_norm = {_norm(value) for value in declared}
    observed_norm = {_norm(value) for value in observed}
    overlap = declared_norm & observed_norm
    base = _base_conflict(row, view)
    if not overlap:
        return [
            {
                **base,
                "type": "enum_mismatch",
                "severity": "error",
                "column": column_name,
                "declared_values": declared,
                "observed_values": observed,
                "detail": (
                    f"The dictionary declares values {declared} for "
                    f"`{row.get('table')}.{row.get('field')}`, but profiled values of "
                    f"`{view['name']}.{column_name}` are {observed} -- none of the "
                    "documented vocabulary exists in the data."
                ),
                "resolution_hint": (
                    "Map each observed code to its business meaning, or correct the "
                    "dictionary; the documented vocabulary cannot be used in filters."
                ),
            }
        ]
    undeclared = sorted(
        value for value in observed if _norm(value) not in declared_norm
    )
    if undeclared:
        return [
            {
                **base,
                "type": "enum_mismatch",
                "severity": "warning",
                "column": column_name,
                "declared_values": declared,
                "observed_values": observed,
                "undeclared_values": undeclared,
                "detail": (
                    f"Profiled values of `{view['name']}.{column_name}` include "
                    f"{undeclared} which the dictionary's declared set {declared} "
                    "does not document."
                ),
                "resolution_hint": "Confirm the meaning of the undocumented values.",
            }
        ]
    return []


def _qualifier_conflicts(
    row: dict[str, Any],
    view: dict[str, Any],
    column_name: str,
) -> list[dict[str, Any]]:
    description = str(row.get("description") or "")
    declared = {_norm(value) for value in declared_enum_values(description)}
    claims = claimed_qualifiers(description, exclude=declared)
    if not claims:
        return []
    conflicts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for sibling_name, sibling_payload in view["column_payloads"].items():
        if not sibling_name or sibling_name == column_name:
            continue
        values = _observed_code_values(sibling_payload)
        if len(values) < 2 or len(values) > _MAX_QUALIFIER_CARDINALITY:
            continue
        canonical_values = {_canonical_token(value): value for value in values}
        for claim in claims:
            claim_canonical = claim["canonical"]
            if claim_canonical not in canonical_values:
                continue
            others = sorted(
                value
                for canonical, value in canonical_values.items()
                if canonical != claim_canonical
            )
            if not others:
                continue
            if not _qualifier_shaped_alternatives(claim, others):
                # The sibling mixes the claimed token with values that do not
                # look like alternative units/qualifiers (e.g. a category code
                # that happens to share a word) -- not a unit conflict.
                continue
            key = (sibling_name, claim_canonical)
            if key in seen:
                continue
            seen.add(key)
            base = _base_conflict(row, view)
            conflicts.append(
                {
                    **base,
                    "type": "unit_mismatch",
                    "severity": "error",
                    "column": column_name,
                    "claimed_qualifier": claim["token"],
                    "claimed_qualifier_canonical": claim_canonical,
                    "qualifier_column": sibling_name,
                    "observed_values": values,
                    "detail": (
                        f"The dictionary describes `{row.get('table')}.{row.get('field')}` "
                        f"with the single unit/qualifier claim `{claim['token']}`, but the "
                        f"sibling column `{view['name']}.{sibling_name}` observes mixed "
                        f"values {values}. The documented single-unit claim does not "
                        "hold for every row."
                    ),
                    "resolution_hint": (
                        f"Values of `{column_name}` must be interpreted per-row via "
                        f"`{sibling_name}` (e.g. normalized to one unit) before "
                        "aggregation; using the raw column as documented is unsafe."
                    ),
                }
            )
    return conflicts


def _qualifier_shaped_alternatives(claim: dict[str, str], others: list[str]) -> bool:
    """Whether the non-claimed sibling values look like alternative qualifiers.

    A word-form unit claim ("kilograms") needs at least one other value that
    is itself a unit word/code (LB, OZ, ...). An explicit-code claim (a
    currency-style uppercase token) needs at least one other all-caps code of
    the same length -- the shape a code list (GBP/EUR/USD) has. This keeps a
    column of unrelated category codes from being mistaken for a unit column.
    """
    if claim["origin"] == "unit_word":
        return any(_norm(value) in _UNIT_WORD_FORMS for value in others)
    token = claim["token"]
    return any(
        value.isupper() and value.isalpha() and len(value) == len(token)
        for value in others
    ) or any(_norm(value) in _UNIT_WORD_FORMS for value in others)


def _attribution_conflicts(
    row: dict[str, Any],
    view: dict[str, Any],
    column_name: str,
    field_norm: str,
    datasets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Description worded around a term that names a DIFFERENT dataset.

    Fires only on the strict evidence shape: a description word (outside any
    explicit ``table.column`` cross-reference) stems to another profiled
    dataset's name, AND that dataset carries its own column with the same
    name as the documented field. The documented meaning may belong to that
    other column, so the claim is surfaced (warning) instead of trusted.
    """
    description = str(row.get("description") or "")
    if not description:
        return []
    referenced_tables = {
        _norm(match.group(1)) for match in _QUALIFIED_REF_RE.finditer(description)
    }
    own_words = set(view["stem_words"]) | _identifier_words(str(row.get("table") or ""))
    own_stems = _stem_set(own_words)
    field_stems = _stem_set(_identifier_words(str(row.get("field") or "")))
    claim_stems: set[str] = set()
    for word in _WORD_RE.findall(description.lower()):
        if len(word) < _MIN_ATTRIBUTION_WORD_LEN:
            continue
        stems = _stem_set({word})
        if stems & own_stems or stems & field_stems:
            continue
        claim_stems.update(stems)
    if not claim_stems:
        return []
    conflicts: list[dict[str, Any]] = []
    for other in datasets:
        if other["stem_norm"] == view["stem_norm"]:
            continue
        if other["stem_norm"] in referenced_tables:
            continue
        if field_norm not in other["columns_norm"]:
            continue
        other_stems = _stem_set(set(other["stem_words"]) | {other["stem_norm"]})
        matched = claim_stems & other_stems
        if not matched:
            continue
        base = _base_conflict(row, view)
        conflicts.append(
            {
                **base,
                "type": "misattributed_claim",
                "severity": "warning",
                "column": column_name,
                "suspected_dataset": other["name"],
                "suspected_column": other["columns_norm"][field_norm],
                "matched_terms": sorted(matched),
                "detail": (
                    f"The dictionary describes `{row.get('table')}.{row.get('field')}` "
                    f"using wording that names a different dataset "
                    f"(`{other['name']}`), which carries its own "
                    f"`{other['columns_norm'][field_norm]}` column. The documented "
                    "meaning may belong to that other column; treating "
                    f"`{view['name']}.{column_name}` as documented risks using the "
                    "wrong source."
                ),
                "resolution_hint": (
                    f"Confirm which column actually holds what the description "
                    f"claims: `{view['name']}.{column_name}` or "
                    f"`{other['name']}.{other['columns_norm'][field_norm]}`."
                ),
            }
        )
    return conflicts


def _base_conflict(row: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    return {
        "dictionary_table": str(row.get("table") or ""),
        "dictionary_field": str(row.get("field") or ""),
        "dictionary_path": str(row.get("path") or ""),
        "claim": str(row.get("description") or ""),
        "dataset_name": view["name"],
        "profile_path": view["profile_path"],
        "row_count": view.get("row_count"),
    }


# ---------------------------------------------------------------------------
# Description parsing
# ---------------------------------------------------------------------------

def declared_enum_values(description: str) -> list[str]:
    """Code values an enumeration-shaped description declares.

    Two textual shapes count as an enumeration claim: a slash-separated run of
    uppercase code tokens (``X / Y / Z``) and an ``X or Y`` pair of uppercase
    code tokens. A single bare token is never an enumeration.
    """
    values: list[str] = []
    for match in _ENUM_RUN_RE.finditer(description or ""):
        for token in re.split(r"\s*/\s*", match.group(0)):
            token = token.strip()
            if token and token not in values:
                values.append(token)
    for left, right in _ENUM_OR_RE.findall(description or ""):
        for token in (left, right):
            if token not in values:
                values.append(token)
    return values if len(values) >= 2 else []


def claimed_qualifiers(
    description: str, exclude: set[str] | None = None
) -> list[dict[str, str]]:
    """Single unit/qualifier claims a description makes.

    Returns ``[{canonical, token, origin}]``. Claims come ONLY from a
    measurement context -- "in <unit>", "per <unit>", or a parenthesized
    "(<unit>)" -- in two shapes:

    - ``unit_word``: a universal measurement-unit word ("in kilograms",
      "per mile", "(kg)"). Bare temporal/unit words in ordinary prose
      ("time of day") do not count.
    - ``code_token``: an uppercase code token of length 2-8 in the same
      contexts (e.g. a currency code: "in GBP").

    Bare uppercase tokens elsewhere in a description (acronyms, name
    suffixes, system names) are NOT unit claims. Tokens that belong to an
    extracted enumeration are excluded (the enum check owns them).
    """
    exclude = exclude or set()
    claims: dict[str, dict[str, str]] = {}
    for match in _MEASUREMENT_CONTEXT_RE.finditer(description or ""):
        token = match.group(1) or match.group(2) or ""
        norm = _norm(token)
        if not norm or norm in exclude:
            continue
        unit_canonical = _UNIT_WORD_FORMS.get(token.lower())
        if unit_canonical is not None:
            claims[unit_canonical] = {
                "canonical": unit_canonical,
                "token": token,
                "origin": "unit_word",
            }
        elif _CODE_TOKEN_RE.fullmatch(token):
            claims.setdefault(
                norm,
                {"canonical": norm, "token": token, "origin": "code_token"},
            )
    return list(claims.values())


def _observed_code_values(column_payload: dict[str, Any]) -> list[str]:
    values = [
        str(value).strip()
        for value in (column_payload.get("sample_values") or [])
        if str(value).strip()
    ]
    unique = sorted(set(values))
    if not unique or len(unique) > _MAX_ENUM_OBSERVED_CARDINALITY:
        return []
    if not all(_CODE_VALUE_RE.match(value) for value in unique):
        return []
    return unique


def _canonical_token(value: str) -> str:
    norm = _norm(value)
    return _UNIT_WORD_FORMS.get(norm, norm)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _path_name(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _path_stem(path: str) -> str:
    name = _path_name(path)
    return name.rsplit(".", 1)[0] if "." in name else name


def _identifier_words(identifier: str) -> set[str]:
    return {
        word.lower()
        for word in re.findall(r"[A-Za-z][a-z0-9]*|[A-Z]+", str(identifier))
        if len(word) >= 2
    }


def _stem_set(words: set[str]) -> set[str]:
    """Light suffix-stripped variants so 'invoiced' meets 'invoices'."""
    stems: set[str] = set()
    for raw in words:
        word = _norm(raw)
        if not word:
            continue
        stems.add(word)
        if word.endswith("ies") and len(word) > 4:
            stems.add(word[:-3] + "y")
        if word.endswith("ing") and len(word) > 5:
            stems.add(word[:-3])
        if word.endswith("es") and len(word) > 4:
            stems.add(word[:-2])
        if word.endswith("ed") and len(word) > 4:
            stems.add(word[:-2])
        if word.endswith("s") and len(word) > 3:
            stems.add(word[:-1])
    return stems


# ---------------------------------------------------------------------------
# Contract emission + lookup indices
# ---------------------------------------------------------------------------

def write_dictionary_conflicts_contract(
    layout: WorkspaceLayout,
    repo_root: Path,
    workspace_rel: str,
    rows: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    *,
    generated_by: str = "resolve-kpi-features",
) -> dict[str, Any]:
    """Reconcile and write ``dictionary_conflicts.json``; returns the payload."""
    conflicts = reconcile_dictionary_claims(rows, profiles)
    errors = [c for c in conflicts if c.get("severity") == BLOCKING_SEVERITY]
    payload = {
        "artifact_type": ARTIFACT_TYPE,
        "version": ARTIFACT_VERSION,
        "generated_by": generated_by,
        "workspace": workspace_rel,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dictionary_sources": sorted(
            {str(row.get("path") or "") for row in rows if row.get("path")}
        ),
        "dictionary_row_count": len(rows),
        "rule": (
            "Dictionary descriptions are documentation evidence, not ground truth. "
            "Each conflict records a documented claim that generated profile "
            "evidence contradicts; error-severity conflicts must block any KPI "
            "that consumes the conflicted column until a human decides."
        ),
        "summary": {
            "conflict_count": len(conflicts),
            "error_count": len(errors),
            "warning_count": len(conflicts) - len(errors),
        },
        "conflicts": conflicts,
    }
    layout.contracts_dir.mkdir(parents=True, exist_ok=True)
    (layout.contracts_dir / CONFLICTS_FILENAME).write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return payload


def load_dictionary_conflicts(layout: WorkspaceLayout) -> dict[str, Any] | None:
    path = layout.contracts_dir / CONFLICTS_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning("dictionary_conflicts_unreadable:%s:%s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def column_conflict_index(
    conflicts: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Conflicts keyed by ``(normalized dataset stem, normalized column)``."""
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for conflict in conflicts or []:
        column = str(conflict.get("column") or "")
        dataset_name = str(conflict.get("dataset_name") or "")
        if not column or not dataset_name:
            continue
        key = (_norm(_path_stem(dataset_name)), _norm(column))
        index.setdefault(key, []).append(conflict)
    return index


def conflicts_for_source_column(
    index: dict[tuple[str, str], list[dict[str, Any]]],
    dataset: str,
    column: str,
) -> list[dict[str, Any]]:
    """Conflicts a (dataset path/name, column) pair hits in a column index."""
    return index.get((_norm(_path_stem(str(dataset))), _norm(str(column))), [])


def phantom_conflict_index(
    conflicts: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Phantom/misplaced conflicts keyed by normalized dictionary field name."""
    index: dict[str, list[dict[str, Any]]] = {}
    for conflict in conflicts or []:
        if conflict.get("type") not in {"phantom_column", "misplaced_column"}:
            continue
        field_norm = _norm(str(conflict.get("dictionary_field") or ""))
        if field_norm:
            index.setdefault(field_norm, []).append(conflict)
    return index


# ---------------------------------------------------------------------------
# Mapping application (called by the KPI feature resolver)
# ---------------------------------------------------------------------------

def apply_conflicts_to_mapping(
    mapping: dict[str, Any],
    conflicts: list[dict[str, Any]],
) -> int:
    """Attach conflicts to mapped KPI features; demote tainted proven mappings.

    A feature whose resolved source column carries an error-severity conflict
    was proven through evidence the dictionary contradicts, so it must not
    stay silently ready: it is demoted to ``blocked_ambiguous`` with
    ``resolution_type: dictionary_conflict`` and an answerable question. A
    ``user_confirmed`` feature is a human decision and is never demoted
    (conflicts are still attached for visibility). Phantom/misplaced-column
    conflicts attach as evidence to unresolved features whose name matches
    the documented-but-missing field. Returns the number of demoted features.
    """
    by_column = column_conflict_index(conflicts)
    by_phantom_field = phantom_conflict_index(conflicts)
    demoted = 0
    for kpi in mapping.get("kpis") or []:
        if not isinstance(kpi, dict):
            continue
        changed = False
        for feature in kpi.get("features") or []:
            if not isinstance(feature, dict):
                continue
            hits: list[dict[str, Any]] = []
            for source in feature.get("source_columns") or []:
                if not isinstance(source, dict):
                    continue
                key = (
                    _norm(_path_stem(str(source.get("dataset") or ""))),
                    _norm(str(source.get("column") or "")),
                )
                hits.extend(by_column.get(key, []))
            state = str(feature.get("state") or "")
            if state not in _READY_STATES:
                hits.extend(
                    by_phantom_field.get(_norm(str(feature.get("feature") or "")), [])
                )
            if not hits:
                continue
            _attach_conflicts(feature, hits)
            errors = [hit for hit in hits if hit.get("severity") == BLOCKING_SEVERITY]
            if errors and state in _READY_STATES and state != "user_confirmed":
                _demote_feature(feature, errors)
                demoted += 1
                changed = True
        if changed:
            _recompute_kpi_status(kpi)
    return demoted


def _attach_conflicts(feature: dict[str, Any], hits: list[dict[str, Any]]) -> None:
    existing = feature.setdefault("conflicts", [])
    known = {
        str(item.get("conflict_id") or "")
        for item in existing
        if isinstance(item, dict)
    }
    evidence = feature.setdefault("evidence", [])
    for hit in hits:
        conflict_id = str(hit.get("conflict_id") or "")
        if conflict_id and conflict_id in known:
            continue
        known.add(conflict_id)
        existing.append(hit)
        evidence.append(
            {
                "type": "dictionary_profile_conflict",
                "source": hit.get("dictionary_path", ""),
                "conflict_id": conflict_id,
                "conflict_type": hit.get("type", ""),
                "severity": hit.get("severity", ""),
                "detail": hit.get("detail", ""),
            }
        )


def _demote_feature(feature: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    first = errors[0]
    feature["state"] = "blocked_ambiguous"
    feature["resolution_type"] = DICTIONARY_CONFLICT_RESOLUTION_TYPE
    feature["blocker_label"] = DICTIONARY_CONFLICT_RESOLUTION_TYPE
    feature["question"] = (
        f"The data dictionary's documented claim about "
        f"`{first.get('dataset_name')}.{first.get('column')}` conflicts with "
        f"profiled evidence: {first.get('detail')} Should the observed data be "
        "trusted over the dictionary (and how should the column be "
        "interpreted), or is the dictionary right and the data needs "
        "normalization first?"
    )
    feature.setdefault("decision_history", []).append(
        {
            "state": "blocked_ambiguous",
            "resolution_type": DICTIONARY_CONFLICT_RESOLUTION_TYPE,
            "note": (
                "Demoted: profile evidence contradicts the data dictionary claim "
                f"({', '.join(sorted({str(e.get('conflict_id') or '') for e in errors}))})."
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


def _recompute_kpi_status(kpi: dict[str, Any]) -> None:
    features = [f for f in kpi.get("features") or [] if isinstance(f, dict)]
    blocked = [f for f in features if f.get("state") not in _READY_STATES]
    kpi["status"] = (
        "ready_for_sql" if features and not blocked else "blocked_questions_pending"
    )
    kpi["open_questions"] = [
        f.get("question") for f in blocked if f.get("question")
    ]


def _rel(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()
