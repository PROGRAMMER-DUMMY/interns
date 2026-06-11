"""Derive KPI metric / cuts / filters from a business question + evidence.

A workspace whose KPI source is plain business questions (e.g. a ``.sql`` file of
comment-only questions like ``-- How many total orders occurred each month?``)
arrives with ``metric`` and ``cuts`` empty. Nothing downstream can build SQL from
an empty measure/grain. This module derives a structured proposal from the
question text plus the *actual* profiled evidence for that workspace.

Design rules (matching repo norms):

- "Derive, don't curate." The intent verbs/grain phrases are generic English
  surface forms (count / average / sum / share / top-N, per / by / each), NOT a
  domain vocabulary. Every COLUMN choice is matched against the workspace's own
  profiled column names, dtypes, and sample values -- never against a baked
  keyword ladder for one domain. There is no per-industry default baked in here.
- Deterministic-first. No LLM/SDK calls. Ambiguous / low-confidence cases are
  surfaced (proposal + alternatives attached) for a human or orchestrator to
  resolve via the existing definition-blocker panel; this module never
  auto-resolves them.
- The proposal reuses the existing facet shape
  ``{value, confidence, evidence, alternatives}`` so panels can consume it.

Evidence contract (``columns`` argument): a list of normalized column-evidence
dicts. Each entry mirrors what ``profile_index.json`` / ``*.profile.json`` carry::

    {
        "column": "order_date",         # physical column name
        "dataset": "datasets/orders.csv",
        "dtype": "date",                # polars/duckdb dtype string
        "n_unique": 1234,               # distinct count (optional)
        "row_count": 5000,              # rows in the dataset (optional)
        "sample_values": ["2021-01-03", ...],   # bounded sample (optional)
        "dictionary_description": "...",          # data-dictionary gloss (optional)
    }

Profiles are bounded evidence, not raw data; callers should pass the already
shaped profile summaries, not full table scans.
"""
from __future__ import annotations

import re
from typing import Any

# Confidence at or above this is treated as "high enough to populate".
HIGH_CONFIDENCE_THRESHOLD = 0.6

# Generic English surface forms. These are grammatical intent markers, not
# domain terms -- they carry no business meaning and work for any workspace.
_COUNT_PHRASES = ("how many", "number of", "count of", "count the", "# of", "volume of")
_AVG_PHRASES = ("average", "avg ", "avg.", "mean ", "mean.", "typical")
_SUM_PHRASES = ("total ", "sum of", "sum the", "summed", "aggregate amount")
_SHARE_PHRASES = ("percentage", "percent of", "% of", "share of", "proportion of", "ratio of")
_TOPN_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)

# Grain markers. Time grain words are calendar units (generic), not domain terms.
_TIME_GRAIN_WORDS: dict[str, tuple[str, ...]] = {
    "year": ("year", "yearly", "annually", "annual", "per annum"),
    "quarter": ("quarter", "quarterly"),
    "month": ("month", "monthly", "per month"),
    "week": ("week", "weekly"),
    "day": ("day", "daily", "per day"),
}
_OVER_TIME_PHRASES = ("over time", "trend", "across time")

# "by X" / "per X" / "for each X" grain extraction.
_CATEGORICAL_GRAIN_RE = re.compile(
    r"\b(?:by|per|for each|grouped by|across each|broken down by)\s+"
    r"([a-z][a-z0-9_ ]*?)(?=\s+(?:and|by|per|over|in|with|where|each|$)|[.,?]|$)",
    re.IGNORECASE,
)

_NUMERIC_DTYPE_TOKENS = ("int", "float", "double", "decimal", "number", "numeric", "real", "long")
_DATE_DTYPE_TOKENS = ("date", "time", "timestamp", "datetime")

# A value that looks like an ISO-ish date or datetime: ``2018-08-31`` or
# ``2018-08-31T05:51:44Z`` / ``2018/08/31 05:51:44``. Used to recognize a date
# column that was profiled as a plain string (CSV columns are frequently typed
# ``String`` even when every value is a timestamp). Evidence-based and generic:
# the decision rides on the actual sampled values, not on a column-name vocabulary.
_DATE_VALUE_RE = re.compile(
    r"^\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}([T ]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?\s*$"
)
# At least this fraction of sampled values must parse as dates for the column to
# be treated as temporal on value evidence alone.
_DATE_VALUE_SAMPLE_RATIO = 0.6

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "of", "for", "each", "per", "by", "in", "on", "to",
        "and", "or", "with", "how", "many", "number", "count", "total", "sum",
        "average", "avg", "mean", "percentage", "percent", "share", "proportion",
        "ratio", "top", "what", "which", "is", "are", "was", "were", "do", "does",
        "did", "occurred", "occur", "happened", "happen", "value", "amount",
        "over", "time", "trend", "across", "all", "every", "their", "there",
    }
)


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _content_tokens(text: str) -> list[str]:
    return [t for t in _tokens(text) if t not in _STOPWORDS and len(t) > 1]


def _singularize(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ses") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def _is_numeric(col: dict[str, Any]) -> bool:
    dtype = str(col.get("dtype") or "").lower()
    return any(tok in dtype for tok in _NUMERIC_DTYPE_TOKENS)


def _is_temporal(col: dict[str, Any]) -> bool:
    dtype = str(col.get("dtype") or "").lower()
    if any(tok in dtype for tok in _DATE_DTYPE_TOKENS):
        return True
    # Fall back to the column name when dtype is opaque (e.g. string-typed dates).
    name = str(col.get("column") or "").lower()
    if any(tok in name for tok in _DATE_DTYPE_TOKENS):
        return True
    # Last resort: the sampled VALUES look like dates/timestamps even though the
    # profiler typed the column as a plain string and the name carries no date
    # token (e.g. ``START`` / ``STOP`` holding ``2018-08-31T05:51:44Z``). This is
    # the common CSV case and is the only signal that recovers it. Generic — it
    # inspects the workspace's own sampled values, never a column-name vocabulary.
    return _values_look_temporal(col)


def _values_look_temporal(col: dict[str, Any]) -> bool:
    samples = [str(v).strip() for v in (col.get("sample_values") or []) if str(v).strip()]
    if not samples:
        return False
    matches = sum(1 for value in samples if _DATE_VALUE_RE.match(value))
    return matches / len(samples) >= _DATE_VALUE_SAMPLE_RATIO


def _cardinality_ratio(col: dict[str, Any]) -> float | None:
    n_unique = col.get("n_unique")
    row_count = col.get("row_count")
    try:
        if n_unique is None or not row_count:
            return None
        return float(n_unique) / float(row_count)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _looks_like_id(col: dict[str, Any]) -> bool:
    name = str(col.get("column") or "").lower()
    if re.search(r"(^|_)id$|(^|_)id_|_key$|^id$|uuid|guid", name):
        return True
    # High cardinality alone implies an identity column ONLY for non-numeric
    # columns. A numeric measure (e.g. a monetary amount) can also be highly
    # distinct without being an id, so cardinality must not exclude it here.
    if _is_numeric(col):
        return False
    ratio = _cardinality_ratio(col)
    return ratio is not None and ratio > 0.8


def _name_token_set(col: dict[str, Any]) -> set[str]:
    raw = _tokens(str(col.get("column") or ""))
    return {_singularize(t) for t in raw}


def _dataset_token_set(col: dict[str, Any]) -> set[str]:
    """Singularized tokens of the column's source table/file name.

    The dataset/table name is profile evidence too: a question about
    ``orders`` should prefer columns from ``orders.csv`` over an
    identically-named column in another table. Generic -- it tokenizes the
    file's basename, never a domain vocabulary.
    """
    raw = str(col.get("dataset") or "")
    base = re.split(r"[\\/]", raw)[-1]
    base = re.sub(r"\.[a-z0-9]+$", "", base, flags=re.IGNORECASE)
    return {_singularize(t) for t in _tokens(base)}


def _dataset_term_bonus(col: dict[str, Any], terms: set[str]) -> tuple[float, str]:
    """A small additive score when the column's TABLE name matches a question
    term. Deliberately modest so it breaks ties toward the right table without
    overriding a strong column-name match."""
    hits = terms & _dataset_token_set(col)
    if hits:
        return 0.08, f"source table name shares token(s) {sorted(hits)} with the question"
    return 0.0, ""


def _value_token_set(col: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for value in col.get("sample_values") or []:
        for tok in _tokens(str(value)):
            out.add(_singularize(tok))
    return out


def _dictionary_token_set(col: dict[str, Any]) -> set[str]:
    return {_singularize(t) for t in _content_tokens(str(col.get("dictionary_description") or ""))}


def _evidence_ref(col: dict[str, Any], reason: str) -> dict[str, Any]:
    """One evidence citation, in the repo's profile-backed shape."""
    return {
        "column": col.get("column"),
        "dataset": col.get("dataset"),
        "dtype": col.get("dtype"),
        "reason": reason,
        "evidence_state": (
            "dictionary_and_profile_backed"
            if col.get("dictionary_description") and col.get("dataset")
            else "profile_backed"
        ),
        "profile_source": col.get("profile_path") or col.get("dataset"),
    }


def _score_column_against_terms(col: dict[str, Any], terms: set[str]) -> tuple[float, list[str]]:
    """Score how well a column matches the given noun terms, evidence-only.

    Returns (score in [0,1], list of human-readable match reasons). Scoring is
    purely structural/evidential: name-token overlap, dictionary-gloss overlap,
    sample-value overlap. No domain word list is consulted.
    """
    if not terms:
        return 0.0, []
    reasons: list[str] = []
    score = 0.0

    name_tokens = _name_token_set(col)
    name_hits = terms & name_tokens
    if name_hits:
        score = max(score, 0.55 + 0.1 * min(len(name_hits), 3))
        reasons.append(
            f"column name `{col.get('column')}` shares token(s) {sorted(name_hits)} with the question"
        )

    dict_tokens = _dictionary_token_set(col)
    dict_hits = terms & dict_tokens
    if dict_hits:
        score = max(score, 0.5 + 0.1 * min(len(dict_hits), 3))
        reasons.append(
            f"data-dictionary description matches token(s) {sorted(dict_hits)}"
        )

    value_tokens = _value_token_set(col)
    value_hits = terms & value_tokens
    if value_hits:
        score = max(score, 0.45 + 0.1 * min(len(value_hits), 3))
        reasons.append(f"sample values match token(s) {sorted(value_hits)}")

    return min(score, 0.95), reasons


def _measure_specificity(col: dict[str, Any], terms: set[str]) -> int:
    """How many question terms this column matches that are NOT just the entity/
    table name. Used only as a RANKING tie-break for measures: a token that names
    the table (true of every column there) does not say which column is the
    measure, so a column that also matches a *specific* term (e.g. "net" for
    ``net_value``) outranks one that matches only the entity word (e.g. "order"
    for ``order_value``) when several measures sit in the same table. Generic,
    evidence-only."""
    entity_tokens = _dataset_token_set(col)
    matched = (
        (terms & _name_token_set(col))
        | (terms & _dictionary_token_set(col))
        | (terms & _value_token_set(col))
    )
    return len(matched - entity_tokens)


def _facet(value: Any, confidence: float, evidence: list[dict[str, Any]], alternatives: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": round(float(confidence), 3),
        "evidence": evidence,
        "alternatives": alternatives,
    }


def _empty_facet() -> dict[str, Any]:
    return {"value": "", "confidence": 0.0, "evidence": [], "alternatives": []}


def _classify_intent(question: str) -> str:
    low = f" {question.lower().strip()} "
    if _TOPN_RE.search(low):
        return "top_n"
    if any(p in low for p in _SHARE_PHRASES):
        return "share"
    if any(p in low for p in _AVG_PHRASES):
        return "average"
    # "total"/"sum" of a measure -> sum; "total <entity>" with a count phrase is
    # still a count, handled by ordering count after we confirm a measure exists.
    if any(p in low for p in _COUNT_PHRASES):
        return "count"
    if any(p in low for p in _SUM_PHRASES):
        return "sum"
    return "unknown"


def _best_measure_column(
    question: str,
    columns: list[dict[str, Any]],
    *,
    require_numeric: bool,
) -> tuple[dict[str, Any] | None, float, list[str], list[dict[str, Any]]]:
    """Pick the column whose evidence best matches the measure noun in the question."""
    terms = {_singularize(t) for t in _content_tokens(question)}
    ranked: list[tuple[float, int, dict[str, Any], list[str]]] = []
    for col in columns:
        if require_numeric and not _is_numeric(col):
            continue
        if _looks_like_id(col):
            continue
        score, reasons = _score_column_against_terms(col, terms)
        if require_numeric and _is_numeric(col):
            score = min(0.95, score + 0.1)
            reasons.append(f"profile dtype `{col.get('dtype')}` is numeric (measurable)")
        bonus, bonus_reason = _dataset_term_bonus(col, terms)
        if score > 0 and bonus:
            score = min(0.95, score + bonus)
            reasons.append(bonus_reason)
        if score > 0:
            specificity = _measure_specificity(col, terms)
            if specificity:
                reasons.append(
                    f"matches {specificity} specific (non-entity) term(s) -> stronger measure signal"
                )
            ranked.append((score, specificity, col, reasons))
    # Primary sort by evidence score; TIE-BREAK by specificity so a column that
    # matches a non-entity term (e.g. "net") outranks one that matched only the
    # entity word (e.g. "order") once their scores have saturated.
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked:
        return None, 0.0, [], []
    best_score, _spec, best_col, best_reasons = ranked[0]
    alternatives = [
        {"column": c.get("column"), "dataset": c.get("dataset"), "confidence": round(s, 3), "reasons": r}
        for s, _sp, c, r in ranked[1:4]
    ]
    return best_col, best_score, best_reasons, alternatives


def _count_entity_column(
    question: str,
    columns: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, float, list[str], list[dict[str, Any]]]:
    """For count intents, find a high-cardinality id column matching the entity.

    Returns ``(best_col, score, reasons, ambiguous_peers)``. ``ambiguous_peers``
    is non-empty when MORE THAN ONE id column has term evidence (its own table
    or name matches the question's nouns) from DIFFERENT tables at a comparable
    score — e.g. "customers who reordered ... a previous order" matches both
    ``customers.id`` and ``orders.id``. Counting the wrong one silently answers
    a different question (events counted as entities), so the caller must treat
    that as a human decision, not a coin flip.
    """
    terms = {_singularize(t) for t in _content_tokens(question)}
    ranked: list[tuple[float, bool, dict[str, Any], list[str], set[str]]] = []
    for col in columns:
        if not _looks_like_id(col):
            continue
        score, reasons = _score_column_against_terms(col, terms)
        term_backed = score > 0
        # The question nouns this candidate matched — its entity identity. Two
        # candidates that matched the SAME noun count the same entity (an fk
        # column named after the entity table is the same count, not a rival).
        matched_terms = (terms & _name_token_set(col)) | (terms & _dataset_token_set(col))
        ratio = _cardinality_ratio(col)
        if ratio is not None and ratio > 0.8:
            score = max(score, 0.5)
            reasons.append(
                f"profile shows high cardinality (n_unique/row_count={ratio:.2f}) -> identity column"
            )
        # A table named after the counted entity is strong evidence that its id
        # column is the count grain (e.g. ``orders.id`` for "how many orders",
        # ``customers.id`` for "how many customers"). This is the only signal that
        # recovers the count grain when the profile carries no distinct-count
        # (n_unique) to confirm the id by cardinality — the common sample-profile
        # case. Generic: it matches the question's nouns against the column's own
        # table name, never a domain vocabulary.
        dataset_hits = terms & _dataset_token_set(col)
        if dataset_hits:
            score = max(score, 0.6)
            term_backed = True
            reasons.append(
                f"table is named after the counted entity {sorted(dataset_hits)} "
                "-> its id column is the count grain"
            )
        else:
            # Otherwise a table-name token overlap is just a tie-breaker between
            # identically-named id columns in different tables.
            bonus, bonus_reason = _dataset_term_bonus(col, terms)
            if score > 0 and bonus:
                score = min(0.95, score + bonus)
                reasons.append(bonus_reason)
        if score > 0:
            ranked.append((score, term_backed, col, reasons, matched_terms))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return None, 0.0, [], []
    best_score, best_term_backed, best_col, best_reasons, best_matched = ranked[0]
    # Entity ambiguity: another TERM-BACKED id column from a DIFFERENT table at
    # a comparable score that matched DIFFERENT question nouns means the
    # question names two countable entities. Cardinality-only competitors do
    # not count (generic id-ness is not evidence the question meant that
    # entity), and candidates that matched the SAME noun are the same entity
    # seen through an fk — also not ambiguous.
    ambiguous_peers = [
        {
            "column": col.get("column"),
            "dataset": col.get("dataset"),
            "confidence": round(score, 3),
            "reasons": reasons,
        }
        for score, term_backed, col, reasons, matched in ranked[1:4]
        if term_backed
        and best_term_backed
        and col.get("dataset") != best_col.get("dataset")
        and best_score - score < 0.15
        and matched.isdisjoint(best_matched)
    ]
    return best_col, best_score, best_reasons, ambiguous_peers


def _derive_metric(
    question: str,
    intent: str,
    columns: list[dict[str, Any]],
) -> dict[str, Any]:
    if intent == "count":
        entity_col, score, reasons, ambiguous_peers = _count_entity_column(question, columns)
        if entity_col is not None and ambiguous_peers:
            # Two countable entities match the question's nouns (e.g. customers
            # AND orders). Counting the wrong one silently answers a different
            # question, so this is a human decision: keep value="" below the
            # threshold so the definition-blocker gate asks.
            return _facet(
                "",
                0.45,
                [_evidence_ref(entity_col, "; ".join(reasons))],
                [
                    {
                        "value": f"count(distinct {entity_col.get('column')})",
                        "confidence": round(score, 3),
                        "reason": (
                            f"counts the `{entity_col.get('dataset')}` entity; "
                            + ("; ".join(reasons))
                        ),
                    },
                    *[
                        {
                            "value": f"count(distinct {p['column']})",
                            "confidence": p["confidence"],
                            "reason": (
                                f"counts the `{p['dataset']}` entity; "
                                + "; ".join(p.get("reasons") or [])
                            ),
                        }
                        for p in ambiguous_peers
                    ],
                ],
            )
        if entity_col is not None and score >= 0.5:
            return _facet(
                f"count(distinct {entity_col.get('column')})",
                min(0.9, score + 0.2),
                [_evidence_ref(entity_col, "; ".join(reasons) or "count distinct of matched id column")],
                [{"value": "count(*)", "confidence": 0.4, "reason": "row-count fallback if id grain is wrong"}],
            )
        # No id evidence -> count(*) is a safe but lower-confidence default.
        return _facet(
            "count(*)",
            0.55,
            [],
            [],
        )

    if intent in ("average", "sum"):
        col, score, reasons, alts = _best_measure_column(question, columns, require_numeric=True)
        if col is not None and score >= HIGH_CONFIDENCE_THRESHOLD:
            fn = "avg" if intent == "average" else "sum"
            return _facet(
                f"{fn}({col.get('column')})",
                score,
                [_evidence_ref(col, "; ".join(reasons))],
                [
                    {"value": f"{fn}({a['column']})", "confidence": a["confidence"], "reasons": a["reasons"]}
                    for a in alts
                ],
            )
        if col is not None:
            fn = "avg" if intent == "average" else "sum"
            return _facet(
                "",
                score,
                [_evidence_ref(col, "; ".join(reasons))],
                [
                    {"value": f"{fn}({col.get('column')})", "confidence": round(score, 3), "reasons": reasons}
                ],
            )
        return _empty_facet()

    if intent == "share":
        col, score, reasons, alts = _best_measure_column(question, columns, require_numeric=False)
        evidence = [_evidence_ref(col, "; ".join(reasons))] if col is not None else []
        # Share metrics need a numerator/denominator decision a human must confirm;
        # we always keep these low-confidence so the blocker gate asks.
        return _facet(
            "",
            min(score, 0.5),
            evidence,
            [
                {
                    "value": "share = matched_subset / total",
                    "confidence": 0.4,
                    "reason": "percentage/share needs an explicit numerator + denominator decision",
                }
            ],
        )

    if intent == "top_n":
        match = _TOPN_RE.search(question)
        n = int(match.group(1)) if match else 0
        col, score, reasons, alts = _best_measure_column(question, columns, require_numeric=True)
        # A "top N <grain> by <measure>" ranking needs TWO coordinated decisions
        # that this evidence pass cannot make reliably: (a) the grain is the noun
        # right after "top N", while the "by <measure>" clause names the RANKING
        # measure -- the generic grain extractor mis-reads "by <measure>" as a cut
        # (see Phase 4 e2e proof); and (b) the executable form is the bare measure
        # plus an ORDER BY/LIMIT the result-view builder derives from the question
        # NAME, not a "top N by ..." metric wrapper. So, like ``share``, top-N is
        # deliberately kept below the threshold: the proposal rides along as an
        # alternative and the definition-blocker panel asks. Never auto-emit a
        # ranking the generator cannot execute.
        proposal = (
            f"top {n} by sum({col.get('column')})" if col is not None else f"top {n} by <measure>"
        )
        return _facet(
            "",
            min(score, 0.45),
            [_evidence_ref(col, "; ".join(reasons))] if col is not None else [],
            [
                {
                    "value": proposal,
                    "confidence": 0.4,
                    "reason": (
                        "top-N ranking needs an explicit grain (the ranked entity) + "
                        "ranking measure decision; left for the definition panel"
                    ),
                }
            ],
        )

    return _empty_facet()


def _resolve_time_grain(question: str) -> str | None:
    low = question.lower()
    for grain, words in _TIME_GRAIN_WORDS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
            return grain
    if any(phrase in low for phrase in _OVER_TIME_PHRASES):
        return "month"  # generic default grain for "over time"
    return None


# Passive event phrasing: "<entities> were admitted/shipped/signed up ...".
# A passive construction names an event done TO the counted entity, which is
# generic English grammar evidence (not domain vocabulary) that the event
# series may be recorded in a table referencing the entity rather than in the
# entity table's own attribute dates.
_PASSIVE_EVENT_RE = re.compile(r"\b(?:was|were|is|are|got|get|been)\s+(?:re)?\w+(?:ed|en)\b", re.IGNORECASE)


def _referencing_event_datasets(
    measure_dataset: str,
    columns: list[dict[str, Any]],
    temporal_pool: list[dict[str, Any]],
) -> set[str]:
    """Datasets that REFERENCE the measure table and carry their own dates.

    FK-shape evidence: a column in another table whose name tokens overlap the
    measure table's name tokens (e.g. an entity-named column in an event
    table). Such a table holding typed temporal columns is where an event
    series about the entity would live.
    """
    measure_tokens = _dataset_token_set({"dataset": measure_dataset})
    if not measure_tokens:
        return set()
    temporal_datasets = {str(c.get("dataset") or "") for c in temporal_pool}
    out: set[str] = set()
    for col in columns:
        dataset = str(col.get("dataset") or "")
        if not dataset or dataset == measure_dataset or dataset not in temporal_datasets:
            continue
        if measure_tokens & _name_token_set(col):
            out.add(dataset)
    return out


def _best_temporal_column(
    columns: list[dict[str, Any]], preferred_dataset: str = ""
) -> dict[str, Any] | None:
    temporal = [c for c in columns if _is_temporal(c)]
    if not temporal:
        return None
    # Prefer dtype-typed date/timestamp columns over name-only matches.
    typed = [c for c in temporal if any(tok in str(c.get("dtype") or "").lower() for tok in _DATE_DTYPE_TOKENS)]
    pool = typed or temporal
    # The time grain must come from the SAME fact table as the measure: an
    # "orders each year" count is dated by the order date in the orders table,
    # not by a date column in another table that happens to sort first globally.
    if preferred_dataset:
        same_table = [c for c in pool if c.get("dataset") == preferred_dataset]
        if same_table:
            return same_table[0]
    return pool[0]


def _scored_temporal_anchor(
    question: str,
    columns: list[dict[str, Any]],
    *,
    preferred_dataset: str = "",
    exclude_terms: set[str] | None = None,
) -> tuple[dict[str, Any] | None, float, list[str], list[dict[str, Any]]]:
    """Pick the temporal anchor with question-term evidence and honest ambiguity.

    Returns ``(col, confidence, reasons, ambiguous_candidates)``.

    The anchor question is "WHICH event does the time grain date?" — and the
    entity noun the measure already consumed is NOT evidence for it ("customers
    signed up each quarter" must not date by the customer table's attribute
    date just because the entity noun matches its gloss; that buckets an event
    series by a static entity attribute). So:

    1. Score temporal columns against the question terms MINUS the measure's
       entity terms. A unique term-backed winner is a confident anchor.
    2. No term evidence + only ONE table has temporal columns -> that table's
       column is safe (the "orders each year" case).
    3. No term evidence + the measure table has its own date column -> still
       safe when the counted rows ARE the dated events ("orders occurred each
       year"). But a PASSIVE event phrasing ("<entities> were <verb>ed each
       quarter") names an event done TO the entity — and when another table
       references the entity (an FK-shaped column named after the measure
       table) and carries its own typed date, that referencing table is where
       the event series lives. Then the anchor is genuinely ambiguous: return
       no pick with the candidates listed, so the definition gate asks instead
       of silently dating an event series by a static entity attribute.
    """
    temporal = [c for c in columns if _is_temporal(c)]
    if not temporal:
        return None, 0.0, [], []
    typed = [
        c for c in temporal
        if any(tok in str(c.get("dtype") or "").lower() for tok in _DATE_DTYPE_TOKENS)
    ]
    pool = typed or temporal

    anchor_terms = {_singularize(t) for t in _content_tokens(question)} - (exclude_terms or set())
    scored: list[tuple[float, dict[str, Any], list[str]]] = []
    for col in pool:
        score, reasons = _score_column_against_terms(col, anchor_terms)
        bonus, bonus_reason = _dataset_term_bonus(col, anchor_terms)
        if score > 0 and bonus:
            score = min(0.95, score + bonus)
            reasons.append(bonus_reason)
        scored.append((score, col, reasons))
    scored.sort(key=lambda item: item[0], reverse=True)

    best_score, best_col, best_reasons = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score > 0 and best_score > runner_up:
        return (
            best_col,
            0.8,
            best_reasons
            + ["question terms (excluding the measure's entity) match this date column"],
            [],
        )

    datasets = {str(c.get("dataset") or "") for _, c, _ in scored}
    if len(datasets) <= 1:
        pick = _best_temporal_column(columns, preferred_dataset)
        return (
            pick,
            0.8,
            ["only one table carries temporal evidence; its date column is the anchor"],
            [],
        )

    same_table = [c for _, c, _ in scored if str(c.get("dataset") or "") == preferred_dataset]
    if same_table and preferred_dataset:
        passive_event = bool(_PASSIVE_EVENT_RE.search(question or ""))
        referencing_event_tables = (
            _referencing_event_datasets(preferred_dataset, columns, pool)
            if passive_event
            else set()
        )
        if not referencing_event_tables:
            return (
                same_table[0],
                0.8,
                ["the measure table's own date column anchors its events"],
                [],
            )
        # Passive event + FK-shaped referencing table(s) with their own dates:
        # the event series may live there, not in the entity's attribute date.

    # Ambiguous anchor: no term evidence and no safe structural pick.
    candidates = [
        {
            "column": c.get("column"),
            "dataset": c.get("dataset"),
            "confidence": round(s, 3),
        }
        for s, c, _ in scored[:4]
    ]
    return None, 0.45, [], candidates


def _categorical_grain_phrases(question: str) -> list[str]:
    phrases: list[str] = []
    for match in _CATEGORICAL_GRAIN_RE.finditer(question):
        phrase = match.group(1).strip()
        # Drop trailing time words so "by region over time" -> "region".
        phrase = re.sub(r"\b(over time|trend|each year|each month|year|month|quarter|week|day)\b", "", phrase, flags=re.IGNORECASE).strip()
        if phrase:
            phrases.append(phrase)
    return phrases


def _best_categorical_column(
    phrase: str,
    columns: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, float, list[str], list[dict[str, Any]]]:
    terms = {_singularize(t) for t in _content_tokens(phrase)}
    ranked: list[tuple[float, dict[str, Any], list[str]]] = []
    for col in columns:
        if _is_temporal(col):
            continue
        score, reasons = _score_column_against_terms(col, terms)
        # A categorical cut should not be a free-running id / high-cardinality key.
        if _looks_like_id(col):
            score *= 0.3
        bonus, bonus_reason = _dataset_term_bonus(col, terms)
        if score > 0 and bonus:
            score = min(0.95, score + bonus)
            reasons.append(bonus_reason)
        if score > 0:
            ranked.append((score, col, reasons))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return None, 0.0, [], []
    best_score, best_col, best_reasons = ranked[0]
    alts = [
        {"column": c.get("column"), "confidence": round(s, 3), "reasons": r}
        for s, c, r in ranked[1:4]
    ]
    return best_col, best_score, best_reasons, alts


def _derive_cuts(
    question: str,
    columns: list[dict[str, Any]],
    *,
    measure_dataset: str = "",
    measure_terms: set[str] | None = None,
) -> dict[str, Any]:
    cut_values: list[str] = []
    evidence: list[dict[str, Any]] = []
    alternatives: list[dict[str, Any]] = []
    confidences: list[float] = []

    grain = _resolve_time_grain(question)
    if grain is not None:
        time_col, t_conf, t_reasons, t_candidates = _scored_temporal_anchor(
            question,
            columns,
            preferred_dataset=measure_dataset,
            exclude_terms=measure_terms,
        )
        if time_col is not None:
            cut_values.append(f"{grain}({time_col.get('column')})")
            evidence.append(
                _evidence_ref(
                    time_col,
                    f"question grain `{grain}` anchored: " + "; ".join(t_reasons),
                )
            )
            confidences.append(t_conf)
        elif t_candidates:
            # Temporal anchor is ambiguous across tables (e.g. an entity
            # attribute date vs an event date). Dating by the wrong one
            # silently changes the question (an event series bucketed by a
            # static entity attribute), so keep this below the threshold and
            # list the candidates for the definition gate.
            confidences.append(t_conf)
            alternatives.extend(
                {
                    "value": f"{grain}({c['column']})",
                    "confidence": c["confidence"],
                    "reason": (
                        f"temporal anchor ambiguous: `{c['column']}` in "
                        f"`{c['dataset']}` is one of several event-date candidates; "
                        "confirm WHICH event the time grain refers to"
                    ),
                }
                for c in t_candidates
            )
        else:
            # Time grain requested but no date column found -> ambiguous.
            confidences.append(0.25)
            alternatives.append(
                {"value": f"{grain}(<date_column>)", "confidence": 0.25, "reason": "no date/timestamp column found in profiles"}
            )

    for phrase in _categorical_grain_phrases(question):
        col, score, reasons, alts = _best_categorical_column(phrase, columns)
        if col is not None and score >= HIGH_CONFIDENCE_THRESHOLD:
            cut_values.append(col.get("column"))
            evidence.append(_evidence_ref(col, f"grain phrase `{phrase}` matched column; " + "; ".join(reasons)))
            confidences.append(score)
            alternatives.extend(
                {"value": a["column"], "confidence": a["confidence"], "reasons": a["reasons"], "for_phrase": phrase}
                for a in alts
            )
        else:
            confidences.append(min(score, 0.4) if col is not None else 0.2)
            alternatives.append(
                {
                    "value": (col.get("column") if col is not None else f"<column for '{phrase}'>"),
                    "confidence": round(score, 3),
                    "reason": f"grain phrase `{phrase}` had no strong column match",
                    "for_phrase": phrase,
                }
            )

    if not cut_values:
        # No grain detected at all (or none resolvable). Empty but not an error.
        return _facet("", min(confidences) if confidences else 0.0, evidence, alternatives)

    overall = min(confidences) if confidences else 0.0
    return _facet(", ".join(cut_values), overall, evidence, alternatives)


def derive_metric_and_cuts(
    question: str,
    columns: list[dict[str, Any]] | None = None,
    *,
    dictionary_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Derive a metric/cuts/filters proposal from a question + workspace evidence.

    Args:
        question: the business question string (e.g. "How many orders each month?").
        columns: normalized column-evidence dicts (see module docstring). Pass the
            shaped profile evidence for THIS workspace; column choices are matched
            against it, never against a baked domain vocabulary.
        dictionary_entries: optional extra data-dictionary glosses, each
            ``{"column": ..., "description": ...}``; merged into the matching
            column's ``dictionary_description`` when present.

    Returns:
        ``{"metric": facet, "cuts": facet, "filters": [...], "intent": str,
        "high_confidence": bool}`` where each facet is
        ``{value, confidence, evidence, alternatives}``. No LLM is called; low
        confidence cases keep ``value=""`` so the definition-blocker gate asks.
    """
    columns = list(columns or [])
    if dictionary_entries:
        gloss = {str(e.get("column") or "").lower(): str(e.get("description") or "") for e in dictionary_entries}
        for col in columns:
            key = str(col.get("column") or "").lower()
            if key in gloss and not col.get("dictionary_description"):
                col = dict(col)
                col["dictionary_description"] = gloss[key]

    question = (question or "").strip()
    intent = _classify_intent(question)
    metric = _derive_metric(question, intent, columns)
    # Anchor the time grain to the SAME table the measure came from, so an
    # event count is dated by its own event date, not a stray date column.
    # The measure's OWN terms (its column + table tokens) are excluded from
    # temporal-anchor evidence: the entity noun matching an entity-attribute
    # date's gloss is what bucketed an event series ("customers signed up each
    # quarter") by the entity's static attribute date instead of the event date.
    measure_dataset = ""
    measure_terms: set[str] = set()
    metric_evidence = metric.get("evidence") or []
    if metric_evidence:
        measure_dataset = str(metric_evidence[0].get("dataset") or "")
        measure_ref = {
            "column": metric_evidence[0].get("column"),
            "dataset": metric_evidence[0].get("dataset"),
        }
        measure_terms = _name_token_set(measure_ref) | _dataset_token_set(measure_ref)
    cuts = _derive_cuts(
        question, columns, measure_dataset=measure_dataset, measure_terms=measure_terms
    )

    # For a top-N ranking the generic "by <X>" grain extractor latches onto the
    # ranking MEASURE (e.g. "by total order value" -> the value column), not the
    # ranked grain. Rather than emit a wrong cut, demote it to a panel
    # alternative -- top-N is resolved as a whole at the definition gate.
    if intent == "top_n" and cuts.get("value"):
        cuts = _facet(
            "",
            min(cuts.get("confidence", 0.0), 0.45),
            cuts.get("evidence", []),
            [
                {
                    "value": cuts.get("value"),
                    "confidence": round(float(cuts.get("confidence", 0.0)), 3),
                    "reason": "ambiguous for top-N: the 'by <measure>' clause is the ranking measure, not the grain",
                },
                *cuts.get("alternatives", []),
            ],
        )

    high_confidence = (
        bool(metric.get("value"))
        and metric.get("confidence", 0.0) >= HIGH_CONFIDENCE_THRESHOLD
    )

    return {
        "intent": intent,
        "metric": metric,
        "cuts": cuts,
        "filters": [],  # filter derivation deferred to a later phase / human gate
        "high_confidence": high_confidence,
    }


def columns_from_profile_index(profile_index: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a ``profile_index.json`` payload into the evidence column list.

    Mirrors ``schema_index_from_profiles`` / ``column_profile_summary`` so this
    module consumes the same generated artifacts the rest of the platform does.
    Returns an empty list when no profiles are present.
    """
    columns: list[dict[str, Any]] = []
    for profile in profile_index.get("profiles") or []:
        dataset = profile.get("path", "")
        row_count = profile.get("row_count")
        col_summaries = {str(c.get("name")): c for c in (profile.get("columns") or [])}
        for name, dtype in (profile.get("schema") or {}).items():
            summary = col_summaries.get(name, {})
            columns.append(
                {
                    "column": name,
                    "dataset": dataset,
                    "dtype": str(dtype),
                    "row_count": row_count,
                    "n_unique": summary.get("n_unique") or summary.get("distinct_count"),
                    "sample_values": summary.get("sample_values") or [],
                    "profile_path": profile.get("profile_path"),
                    "dictionary_description": summary.get("dictionary_description"),
                }
            )
        # Datasets sometimes carry only the ``columns`` list (no schema map).
        if not profile.get("schema"):
            for c in profile.get("columns") or []:
                columns.append(
                    {
                        "column": c.get("name"),
                        "dataset": dataset,
                        "dtype": str(c.get("dtype") or ""),
                        "row_count": row_count,
                        "n_unique": c.get("n_unique") or c.get("distinct_count"),
                        "sample_values": c.get("sample_values") or [],
                        "profile_path": profile.get("profile_path"),
                        "dictionary_description": c.get("dictionary_description"),
                    }
                )
    return columns
