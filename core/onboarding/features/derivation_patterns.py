"""Reusable derived-feature patterns the engine cannot express from a single
metric/cuts cell: a DERIVED DURATION bucket (start/stop -> threshold) and a
TEMPORAL SELF-JOIN recurrence (an event within N days of a prior event for the
same entity). These are exactly what the ``feature-derivation-library`` skill is
meant to own.

Each pattern emits a JSON-backed option in the same shape as
``core.onboarding.features.derived_evidence.derived_feature_options`` so the
blocker panel can render it and the user can confirm. Deterministic, evidence
-driven, no LLM, no domain vocabulary: detection rides on generic English
duration/recurrence phrasing plus profiled column evidence (sample-value date
shape, id/fk shape), never a baked keyword ladder.
"""
from __future__ import annotations

import re
from typing import Any

from core.onboarding.features.blockers import normalize
from core.sql_safety import UnsafeIdentifierError, assert_safe_identifier, quote_ident_backtick

# ISO-ish date / datetime value, used to recognize a temporal column profiled as
# a plain string (the common CSV case). Same shape used by metric_derivation.
_DATE_VALUE_RE = re.compile(
    r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}([T ]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?\s*$"
)
_DATE_DTYPE_TOKENS = ("date", "time", "timestamp", "datetime")

# "over / under / more than / exceeds N hours" -> threshold + unit. Covers the
# common comparator verbs and prepositions English uses for a duration cut,
# generically (no domain vocabulary).
_DURATION_RE = re.compile(
    r"\b(?:over|under|more than|less than|greater than|at least|at most|"
    r"longer than|shorter than|above|below|beyond|past|exceed(?:s|ing)?|"
    r"lasting (?:more|over)|up to|no more than|no less than)\s+"
    r"(\d+)\s+(hour|day|week|minute)s?\b",
    re.IGNORECASE,
)
# Explicit subtraction form: "STOP - START > 24 hours" / "end minus start over 2
# days" -> the interval between two operands compared to a threshold. The two
# operands are matched generically (any token) and resolved to temporal columns
# via profile evidence, not by name.
_DURATION_DIFF_RE = re.compile(
    r"\b[\w\"'.]+\s*(?:-|minus)\s*[\w\"'.]+\s*"
    r"(?:>=?|over|exceed(?:s|ing)?|more than|greater than|above|beyond|"
    r"at least|longer than)\s+(\d+)\s+(hour|day|week|minute)s?\b",
    re.IGNORECASE,
)
# "within N days of a previous / prior ..." -> window + unit.
_WINDOW_RE = re.compile(
    r"\bwithin\s+(\d+)\s+(hour|day|week|month)s?\b", re.IGNORECASE
)
# Recurrence semantics, generic. Word-stem regex so noun AND verb forms
# (recurrence/recurring/recurred; repeat/repeated; readmission/readmitted) all
# hit, plus the comparison words English uses for a "second/again" event. The
# "re-<stem>" alternatives are spelled out generically (a second occurrence of an
# action) rather than matching any word that starts with "re", which would
# false-positive on release/review/region. No domain vocabulary.
_RECURRENCE_RE = re.compile(
    r"\b(?:recur\w*|repeat\w*|reoccur\w*|readmi\w*|reorder\w*|"
    r"re-?(?:order|visit|admi\w*|occur\w*|purchase\w*|book\w*|turn\w*|"
    r"peat\w*|cur\w*)|"
    r"again|prior|previous|subsequent|another|repeated|back)\b",
    re.IGNORECASE,
)

_START_HINTS = ("start", "begin", "from", "open", "admit", "in")
_STOP_HINTS = ("stop", "end", "to", "close", "discharge", "out", "finish")

# Canonical physical-unit conversion factors to a per-dimension base unit. These
# are universal physical constants (mass / length / volume), NOT domain or
# business vocabulary -- the same set used to normalize a quantity stored in
# mixed units regardless of workspace. Each entry maps a canonical unit code to
# (dimension, factor-to-base). Base units: mass -> kg, length -> m, volume -> l.
_UNIT_TO_BASE: dict[str, tuple[str, float]] = {
    # mass (base kg)
    "kg": ("mass", 1.0),
    "g": ("mass", 0.001),
    "t": ("mass", 1000.0),
    "lb": ("mass", 0.45359237),
    "oz": ("mass", 0.028349523125),
    # length (base m)
    "km": ("length", 1000.0),
    "m": ("length", 1.0),
    "cm": ("length", 0.01),
    "mm": ("length", 0.001),
    "mi": ("length", 1609.344),
    "ft": ("length", 0.3048),
    "in": ("length", 0.0254),
    # volume (base l)
    "l": ("volume", 1.0),
    "ml": ("volume", 0.001),
    "gal": ("volume", 3.785411784),
}
# Word/abbreviation forms -> canonical unit code. Mirrors the dictionary-
# reconciliation unit lexicon (kept local so this module has no cross-package
# dependency); ambiguous single letters that need context are excluded.
_UNIT_CODE_BY_FORM: dict[str, str] = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
    "kilogram": "kg", "kilograms": "kg", "kilogramme": "kg", "kilogrammes": "kg",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "g": "g", "gram": "g", "grams": "g", "gramme": "g", "grammes": "g",
    "t": "t", "tonne": "t", "tonnes": "t", "ton": "t", "tons": "t",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "km": "km", "kilometer": "km", "kilometers": "km",
    "kilometre": "km", "kilometres": "km",
    "mi": "mi", "mile": "mi", "miles": "mi",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "cm": "cm", "mm": "mm", "ft": "ft", "foot": "ft", "feet": "ft",
    "in": "in", "inch": "in", "inches": "in",
    "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "ml": "ml", "gal": "gal", "gallon": "gal", "gallons": "gal",
}
# Unit-column NAME hints. A string column named like one of these is the unit-of
# -measure descriptor that governs a sibling quantity column.
_UOM_NAME_RE = re.compile(r"(?:^|_)(uom|unit|units|measure|currency|ccy)(?:$|_)", re.IGNORECASE)
# Numeric dtype tokens (a quantity column the UOM descriptor governs).
_NUMERIC_DTYPE_TOKENS = ("int", "float", "double", "decimal", "number", "numeric")


def _is_temporal(col: dict[str, Any]) -> bool:
    dtype = str(col.get("dtype") or "").lower()
    name = str(col.get("column") or "").lower()
    if any(tok in dtype for tok in _DATE_DTYPE_TOKENS) or any(tok in name for tok in _DATE_DTYPE_TOKENS):
        return True
    samples = [str(v).strip() for v in (col.get("sample_values") or []) if str(v).strip()]
    if not samples:
        return False
    return sum(1 for s in samples if _DATE_VALUE_RE.match(s)) / len(samples) >= 0.6


def _looks_like_id(col: dict[str, Any]) -> bool:
    name = str(col.get("column") or "").lower()
    return bool(re.search(r"(^|_)id$|(^|_)id_|_key$|^id$|uuid|guid", name)) or name.endswith("id")


def _name_has(col: dict[str, Any], hints: tuple[str, ...]) -> bool:
    # Segment-exact, not substring: a raw `h in name` match let 2-char hints
    # like "in"/"to"/"out" fire on any column whose name merely CONTAINS the
    # hint text (e.g. a name ending "...routing_number" contains "out"; one
    # starting "insured_..."/"origin_..." contains "in"; one starting
    # "total_..." contains "to"). Column names are underscore/word-delimited,
    # so split into segments and require an exact segment match instead.
    name = str(col.get("column") or "").lower()
    segments = set(re.split(r"[^a-z0-9]+", name))
    return any(h in segments for h in hints)


def _input_col(col: dict[str, Any], role: str) -> dict[str, Any]:
    # Same shape as derived_evidence.derived_input_column — the blocker-panel
    # validator (derived_markdown REQUIRED_INPUT_COLUMN_FIELDS) requires the
    # full evidence form (role / observed_values / value_profile /
    # semantic_meaning_sources / reason); a thin column crashes panel prep.
    from core.onboarding.features.derived_evidence import (
        example_value,
        input_column_reason,
        semantic_meaning_sources,
        value_profile,
    )

    column = str(col.get("column") or "")
    return {
        "input_name": role,
        "column": col.get("column"),
        "dataset": col.get("dataset"),
        "dtype": col.get("dtype"),
        "role": "formula_input",
        "profile_path": col.get("profile_path"),
        "row_count": col.get("row_count"),
        "observed_values": col.get("sample_values") or [],
        "value_profile": value_profile(col),
        "semantic_meaning_sources": semantic_meaning_sources(role, column, col),
        "reason": input_column_reason(role, column, col),
        "example_value": example_value(role, col),
        "evidence_state": "profile_inferred",
    }


def _quote(name: Any) -> str:
    return '"' + str(name or "") + '"'


def _option(
    *,
    name: str,
    pattern_id: str,
    business_meaning: str,
    formula: str,
    input_columns: list[dict[str, Any]],
    reasoning: str,
    confidence: str,
    formula_templates: dict[str, str] | None = None,
    option_kind: str | None = None,
) -> dict[str, Any]:
    # Full option contract (derived_markdown REQUIRED_OPTION_FIELDS /
    # REQUIRED_EXAMPLE_FIELDS / REQUIRED_REASONING_FIELDS) — same shape as
    # derived_evidence.derived_feature_options, so the blocker panel renders it.
    example_inputs = {
        str(col.get("column")): col.get("example_value") for col in input_columns
    }
    option: dict[str, Any] = {
        "derived_column_name": name,
        "source_pattern_id": pattern_id,
        "business_meaning": business_meaning,
        "formula": formula,
        "input_columns": input_columns,
        "example": {
            "example_type": "synthetic_formula_example",
            "input": example_inputs,
            "output": {name: "apply the formula to the example inputs"},
            "substituted_formula": formula,
            "warning": (
                "Example demonstrates formula mechanics only; "
                "it is not workspace ground truth."
            ),
        },
        "evidence_sources": [
            {
                "file": col.get("profile_path")
                or "interns/generated/profiles/profile_index.json",
                "dataset": col.get("dataset"),
                "column": col.get("column"),
                "evidence_type": "profile_schema",
                "evidence": (
                    f"Input column `{col.get('column')}` is present in generated "
                    "profile evidence."
                ),
                "evidence_state": "schema_presence_only",
            }
            for col in input_columns
        ],
        "derivation_reasoning": {
            "summary": reasoning,
            "why_this_formula": reasoning,
            "why_not_ground_truth": (
                "No source artifact explicitly defines this derived column "
                "with this formula."
            ),
            "remaining_risk": (
                "Input columns are profile-backed, but the business meaning "
                "still needs confirmation."
            ),
            "evidence_state": "candidate_derivation_not_ground_truth",
        },
        "evidence_state": "candidate_derivation_not_ground_truth",
        "confidence": confidence,
        "needs_user_confirmation": True,
    }
    if formula_templates:
        option["formula_templates"] = formula_templates
    if option_kind:
        option["option_kind"] = option_kind
    return option


def _temporal_pair(columns: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """A (start, stop) temporal column pair from the SAME dataset, generically."""
    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for col in columns:
        if _is_temporal(col):
            by_dataset.setdefault(str(col.get("dataset") or ""), []).append(col)
    for temporal in by_dataset.values():
        if len(temporal) < 2:
            continue
        start = next((c for c in temporal if _name_has(c, _START_HINTS)), None)
        stop = next((c for c in temporal if _name_has(c, _STOP_HINTS)), None)
        if start and stop and start is not stop:
            return start, stop
        # Fall back to the first two temporal columns in spec order.
        return temporal[0], temporal[1]
    return None


def detect_duration_bucket(question: str, columns: list[dict[str, Any]]) -> dict[str, Any] | None:
    match = _DURATION_RE.search(question or "") or _DURATION_DIFF_RE.search(question or "")
    if not match:
        return None
    n, unit = int(match.group(1)), match.group(2).lower()
    pair = _temporal_pair(columns)
    if not pair:
        return None
    start, stop = pair
    formula = (
        f"date_diff('{unit}', CAST({_quote(start.get('column'))} AS TIMESTAMP), "
        f"CAST({_quote(stop.get('column'))} AS TIMESTAMP)) >= {n}"
    )
    return _option(
        name=f"over_{n}_{unit}",
        pattern_id="duration_bucket",
        business_meaning=(
            f"Whether the elapsed time between {start.get('column')} and "
            f"{stop.get('column')} is at least {n} {unit}(s)."
        ),
        formula=formula,
        input_columns=[_input_col(start, "start"), _input_col(stop, "stop")],
        reasoning=(
            f"Question implies a {n}-{unit} duration threshold; "
            f"{start.get('column')}/{stop.get('column')} are the profiled "
            "start/stop timestamps of the same table."
        ),
        confidence="medium",
    )


def detect_recurrence_within_window(
    question: str, columns: list[dict[str, Any]]
) -> dict[str, Any] | None:
    match = _WINDOW_RE.search(question or "")
    if not match or not _RECURRENCE_RE.search(question or ""):
        return None
    n, unit = int(match.group(1)), match.group(2).lower()
    entity = next((c for c in columns if _looks_like_id(c)), None)
    event = next((c for c in columns if _is_temporal(c)), None)
    if entity is None or event is None:
        return None
    ec, tc = _quote(entity.get("column")), _quote(event.get("column"))
    formula = (
        f"EXISTS (SELECT 1 FROM <self> p WHERE p.{ec} = {ec} "
        f"AND p.{tc} < {tc} "
        f"AND {tc} <= p.{tc} + INTERVAL {n} {unit.upper()})"
    )
    return _option(
        name=f"recurred_within_{n}_{unit}",
        pattern_id="recurrence_within_window",
        business_meaning=(
            f"Whether the same {entity.get('column')} has a prior event within "
            f"{n} {unit}(s) before this one (a self-join over the event table)."
        ),
        formula=formula,
        input_columns=[_input_col(entity, "entity"), _input_col(event, "event_time")],
        reasoning=(
            f"Question implies recurrence within {n} {unit}(s) of a prior event; "
            f"needs a self-join on {entity.get('column')} ordered by "
            f"{event.get('column')}. Candidate only -- confirm the window semantics."
        ),
        confidence="low",
    )


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _is_numeric(col: dict[str, Any]) -> bool:
    dtype = str(col.get("dtype") or "").lower()
    if any(tok in dtype for tok in _NUMERIC_DTYPE_TOKENS):
        return True
    samples = [str(v).strip() for v in (col.get("sample_values") or []) if str(v).strip()]
    if not samples:
        return False
    numeric = 0
    for s in samples:
        try:
            float(s)
            numeric += 1
        except ValueError:
            pass
    return numeric / len(samples) >= 0.8


def _observed_unit_codes(col: dict[str, Any]) -> dict[str, int]:
    """Canonical unit codes observed in a column's sample values -> count."""
    counts: dict[str, int] = {}
    for value in col.get("sample_values") or []:
        token = re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())
        code = _UNIT_CODE_BY_FORM.get(token)
        if code:
            counts[code] = counts.get(code, 0) + 1
    return counts


def _strip_uom_affix(name: str) -> str:
    """`wgt_uom` -> `wgt`; `weight_unit` -> `weight`. Used to pair a unit column
    with the quantity column it describes by naming relationship."""
    stem = re.sub(r"(?:_)?(uom|units?|measure|currency|ccy)$", "", name, flags=re.IGNORECASE)
    return stem.rstrip("_") or name


def _question_target_unit(question: str, dimension: str) -> str | None:
    """A unit code named in the question that belongs to `dimension`, e.g. the
    'kilogram' in 'cost per kilogram' -> 'kg'. None when the prose names none."""
    for word in re.findall(r"[A-Za-z]+", question or ""):
        code = _UNIT_CODE_BY_FORM.get(word.lower())
        if code and _UNIT_TO_BASE.get(code, (None,))[0] == dimension:
            return code
    return None


def detect_uom_normalization(
    question: str, columns: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """A quantity stored in MIXED units needs normalization before it can be
    summed or averaged. Detect a string unit-of-measure column (e.g. ``wgt_uom``
    with values KG/LB) governing a numeric sibling quantity column in the same
    dataset, and propose a CASE that normalizes the quantity to one unit.

    Generic and evidence-driven: the hazard is recognized from the data shape
    (a numeric column + a sibling column whose observed values are mixed
    physical-unit codes), and the conversion uses universal physical constants
    -- no domain vocabulary, no per-workspace column list. Returns None when no
    unit column is mixed (a single observed unit needs no normalization) or when
    the question does not reference the quantity or its unit dimension.
    """
    by_dataset: dict[str, list[dict[str, Any]]] = {}
    for col in columns:
        by_dataset.setdefault(str(col.get("dataset") or ""), []).append(col)

    best: dict[str, Any] | None = None
    best_rank: tuple[int, int] = (-1, -1)
    for dataset, cols in by_dataset.items():
        for unit_col in cols:
            name = str(unit_col.get("column") or "")
            counts = _observed_unit_codes(unit_col)
            name_is_unit = bool(_UOM_NAME_RE.search(name))
            # A unit column is one named like a UOM descriptor, or any string
            # column whose observed values are predominantly unit codes.
            if not name_is_unit and not counts:
                continue
            # Mixed-unit hazard: >=2 distinct observed codes in one dimension.
            dims: dict[str, dict[str, int]] = {}
            for code, n in counts.items():
                dim = _UNIT_TO_BASE.get(code, (None,))[0]
                if dim:
                    dims.setdefault(dim, {})[code] = n
            mixed = {dim: codes for dim, codes in dims.items() if len(codes) >= 2}
            if not mixed:
                continue
            dimension, observed = next(iter(mixed.items()))
            # Pair with the quantity column it governs: the numeric sibling whose
            # name is this column minus its UOM affix, else the first numeric
            # column in the dataset.
            stem = _strip_uom_affix(name).lower()
            quantity = next(
                (c for c in cols if _is_numeric(c) and str(c.get("column") or "").lower() == stem),
                None,
            ) or next((c for c in cols if _is_numeric(c)), None)
            if quantity is None:
                continue
            qname = str(quantity.get("column") or "")
            # Prose gate: the question must reference the quantity column, the
            # unit column, or a unit word of this dimension -- otherwise this is
            # an unrelated table's quantity and proposing it would be noise.
            target = _question_target_unit(question, dimension)
            q_norm = _norm(question)
            relevant = (
                target is not None
                or (_norm(qname) and _norm(qname) in q_norm)
                or (_norm(stem) and len(_norm(stem)) >= 3 and _norm(stem) in q_norm)
            )
            if not relevant:
                continue
            target = target or _base_unit_for_dimension(dimension, observed)
            # Rank: prefer a name-paired quantity and more observed unit variety.
            rank = (1 if str(quantity.get("column") or "").lower() == stem else 0, len(observed))
            if rank <= best_rank:
                continue
            best_rank = rank
            best = _uom_option(question, quantity, unit_col, dimension, observed, target)
    return best


def _base_unit_for_dimension(dimension: str, observed: dict[str, int]) -> str:
    """Target unit when the prose names none: the most frequently observed unit
    in the dimension (ties broken by canonical code order for determinism)."""
    return sorted(observed.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _uom_option(
    question: str,
    quantity: dict[str, Any],
    unit_col: dict[str, Any],
    dimension: str,
    observed: dict[str, int],
    target: str,
) -> dict[str, Any] | None:
    qname = str(quantity.get("column") or "")
    uname = str(unit_col.get("column") or "")
    target_factor = _UNIT_TO_BASE[target][1]
    branches = []
    for code in sorted(observed):
        factor = _UNIT_TO_BASE[code][1] / target_factor
        # Render an exact 1.0 factor plainly; otherwise full precision.
        scale = f"{_quote(qname)}" if factor == 1.0 else f"{_quote(qname)} * {factor:.11g}"
        branches.append(f"WHEN '{code.upper()}' THEN {scale}")
    if not branches:
        return None
    formula = (
        f"CASE upper(trim({_quote(uname)})) "
        + " ".join(branches)
        + f" ELSE NULL END"
    )
    name = f"{qname}_{target}"
    observed_label = ", ".join(f"{code.upper()} (x{n})" for code, n in sorted(observed.items()))
    return _option(
        name=name,
        pattern_id="uom_normalization",
        business_meaning=(
            f"`{qname}` is stored in mixed {dimension} units (observed: "
            f"{observed_label}); this normalizes every row to {target.upper()} "
            f"using `{uname}` so the values can be summed or averaged."
        ),
        formula=formula,
        input_columns=[_input_col(quantity, "quantity"), _input_col(unit_col, "unit")],
        reasoning=(
            f"`{uname}` carries more than one {dimension} unit code "
            f"({observed_label}), so raw `{qname}` mixes units and cannot be "
            f"aggregated directly; conversion to {target.upper()} uses universal "
            f"physical constants. Confirm the target unit and that `{uname}` "
            f"governs `{qname}`."
        ),
        confidence="medium",
    )


def _raw_source_entry(
    schema_index: dict[str, list[dict[str, Any]]],
    raw_column: str,
    leaf_entry: dict[str, Any],
) -> dict[str, Any]:
    """The physical bronze column's own schema_index entry for the same
    dataset as ``leaf_entry`` -- real dtype/profile_path/row_count evidence
    for the input-column contract. Falls back to a synthesized entry from the
    leaf's own evidence when the raw column isn't separately indexed (should
    not normally happen -- schema_index_from_profiles always indexes every
    top-level column -- but stay evidence-honest rather than raising)."""
    dataset = leaf_entry.get("dataset")
    for candidate in schema_index.get(normalize(raw_column), []):
        if not candidate.get("is_nested_leaf") and candidate.get("dataset") == dataset:
            return candidate
    return {
        "column": raw_column,
        "dataset": dataset,
        "dtype": "unknown",
        "row_count": leaf_entry.get("row_count"),
        "profile_path": leaf_entry.get("profile_path"),
        "sample_values": [],
    }


def _polars_struct_chain_text(raw_column: str, dot_path: str) -> str:
    chain = f'pl.col("{raw_column}")'
    for seg in dot_path.split(".")[1:]:
        chain += f'.struct.field("{seg.split("[")[0]}")'
    return chain


def detect_json_leaf_promotion_candidates(
    feature_token: str, schema_index: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """A feature token that names a field nested inside a JSON payload column
    (e.g. ``ssn`` matching a profiled ``metadata.contact.ssn`` leaf --
    core.profiling.data_model_profiler's nested-leaf discovery, indexed by
    schema_index_from_profiles/columns_from_profile_index with
    ``is_nested_leaf: True``) becomes a promotable derived-column candidate:
    extract that leaf into a real typed silver column via a per-dialect
    JSON-path formula.

    Bronze representation (JSON string vs. an already-parsed struct/variant)
    is a real, unresolved ambiguity across DuckDB/Spark/Polars -- recorded
    explicitly in ``derivation_reasoning`` rather than guessed. Same
    JSON-backed option contract (_option) every other pattern in this module
    uses, so it flows through the SAME human-confirmation panel as
    duration-bucket/recurrence/uom -- no silent promotion. Deterministic,
    never fatal: an unsafe/unparseable identifier just skips that candidate.
    """
    # Nested leaves are indexed under their FULL dot-path (e.g.
    # "metadatacontactssn"), never under a bare leaf-segment key like "ssn" --
    # deliberately, so the resolver's pre-existing direct-column branch (which
    # keys off the exact same schema_index) never auto-treats a JSON leaf as
    # "proven_direct" with no human review. This detector therefore scans and
    # matches on the leaf's OWN last segment instead of doing a dict lookup.
    token_norm = normalize(feature_token)
    matches: list[dict[str, Any]] = []
    for entries in schema_index.values():
        for entry in entries:
            if not entry.get("is_nested_leaf"):
                continue
            dot_path = str(entry.get("column") or "")
            if not dot_path:
                continue
            last_segment = dot_path.rsplit(".", 1)[-1].split("[")[0]
            if normalize(last_segment) == token_norm:
                matches.append(entry)
    options: list[dict[str, Any]] = []
    for entry in matches:
        dot_path = str(entry.get("column") or "")
        raw_column = str(entry.get("raw_source_column") or "")
        if not dot_path or not raw_column:
            continue
        try:
            assert_safe_identifier(raw_column, context="json_leaf_promotion_source_column")
        except UnsafeIdentifierError:
            continue
        json_path = "$." + ".".join(
            seg.split("[")[0] for seg in dot_path.split(".")[1:] if seg.split("[")[0]
        )
        raw_entry = _raw_source_entry(schema_index, raw_column, entry)
        # Column/dataset routing comes from the RAW physical column (what the
        # formula actually reads and what design.py's host-table resolution
        # needs); the review-facing EVIDENCE (observed_values etc.) comes from
        # the LEAF's own profiled stats -- a Struct's own "sample values"
        # aren't meaningful, but "contact.ssn looked like 123-45-6789" is
        # exactly what a human confirming this promotion needs to see.
        evidence_entry = dict(raw_entry)
        evidence_entry["sample_values"] = entry.get("sample_values") or raw_entry.get("sample_values") or []
        derived_name = re.sub(r"[^a-z0-9]+", "_", dot_path.lower()).strip("_")
        duckdb_sql = f"json_extract_string({_quote(raw_column)}, '{json_path}')"
        spark_sql = f"get_json_object({quote_ident_backtick(raw_column)}, '{json_path}')"
        polars = _polars_struct_chain_text(raw_column, dot_path)
        options.append(
            _option(
                name=derived_name,
                pattern_id="json_leaf_promotion",
                business_meaning=(
                    f"Promotes the `{dot_path}` field nested inside the "
                    f"`{raw_column}` JSON payload column to a real typed "
                    "silver column."
                ),
                formula=duckdb_sql,
                input_columns=[_input_col(evidence_entry, "json_leaf_source")],
                reasoning=(
                    f"`{feature_token}` matched a field discovered nested "
                    f"inside the profiled `{raw_column}` payload column at "
                    f"path `{dot_path}`. assumes_bronze_representation: "
                    "json_string -- if bronze instead stores an "
                    "already-parsed struct/variant, the DuckDB/Spark "
                    "formulas need dot/get_json_object adjustment at review "
                    "time."
                ),
                confidence="medium",
                formula_templates={
                    "duckdb_sql": duckdb_sql,
                    "spark_sql": spark_sql,
                    "polars": polars,
                },
                option_kind="json_leaf_promotion",
            )
        )
    return options


def detect_derivation_patterns(
    question: str, columns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return any reusable derived-feature options that match the question +
    profiled columns. Deterministic and generic; empty list when none apply."""
    options: list[dict[str, Any]] = []
    for detector in (
        detect_duration_bucket,
        detect_recurrence_within_window,
        detect_uom_normalization,
    ):
        try:
            option = detector(question, columns)
        except Exception:  # pragma: no cover - detection is advisory, never fatal
            option = None
        if option:
            options.append(option)
    return options


__all__ = [
    "detect_derivation_patterns",
    "detect_duration_bucket",
    "detect_recurrence_within_window",
    "detect_uom_normalization",
    "detect_json_leaf_promotion_candidates",
]
