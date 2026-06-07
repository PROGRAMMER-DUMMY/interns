"""
Data-understanding classifier core (BUG-010 data-understanding gate).

Pure, importable, side-effect-free library. Given workspace *profile* artifacts
(``interns/generated/profiles/*.profile.json``) and inferred *relationship*
contracts (``relationship_contracts.json`` -> ``relationships``), this module
classifies:

1. ``classify_quality_tier``  -> medallion data-quality tier (raw/bronze, silver, gold)
2. ``classify_schema_type``   -> schema type (star / snowflake / galaxy / flat / OBT /
                                 3NF / hierarchical / ...)
3. ``scoped_processing_options`` -> the data-processing options RELEVANT to the
                                 detected tier (not the full menu).

Doc grounding:
- Quality tier heuristics: ``docs/agents/data processing/data_engineering_guide_part2.md``
  section 8 (Medallion Architecture). Bronze = raw/append, source schema, lowest trust,
  bad records preserved (nulls/quarantine), strings-not-cast. Silver = cleansed/conformed,
  typed, deduplicated, keys complete, referential integrity holding. Gold = business-ready,
  denormalized/aggregated, highest trust. The Bronze->Silver and Silver->Gold quality gates
  (PK completeness > 99.9%, cast success > 99%, no orphan facts) drive the thresholds here.
- Schema type heuristics: ``docs/agents/data processing/schema_types_identification_guide.md``
  decision framework (section 20) + key-signals cheat sheet (section 21). Snowflake is
  detected via "dimension table references ANOTHER dimension table" (nested dimension),
  which is exactly the RCM ``DataModel.png`` shape (Dim_Provider -> Dim_Department -> ...).

Design rules:
- Workspace-agnostic: NO hardcoded business words. Heuristics operate purely on
  profile / relationship dict shapes (row counts, null rates, dtype consistency,
  key uniqueness, fact/dimension topology by structure).
- Deterministic, no I/O, no prints, no argparse.
- Tolerant of missing/partial fields: missing evidence lowers confidence and is
  surfaced in ``evidence`` rather than raising.
"""
from __future__ import annotations

from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Tier constants (medallion, part2 section 8)
# ---------------------------------------------------------------------------
TIER_RAW = "raw/bronze"
TIER_SILVER = "silver"
TIER_GOLD = "gold"

# Quality-gate thresholds, grounded in part2 "Data Quality Gates Between Layers".
# Bronze->Silver gate wants PK completeness > 99.9% and cast success > 99%.
_SILVER_MAX_NULL_RATE = 0.01          # > ~1% nulls overall -> still bronze-ish
_SILVER_MAX_KEY_NULL_RATE = 0.001     # key columns: >0.1% null fails the PK-completeness gate
_GOLD_MAX_NULL_RATE = 0.0001          # gold expects near-zero nulls
_DUP_TIER_THRESHOLD = 0.999           # key uniqueness ratio below this => dedup not done (bronze)
_GOLD_KEY_UNIQUE_THRESHOLD = 0.9999

# Distinct-count keys a profiler might emit (mirrors relationships/contracts.py).
_DISTINCT_KEYS = ("distinct_count", "n_unique", "unique_count", "approx_distinct", "cardinality")
_NULL_KEYS = ("null_count", "nulls", "num_nulls", "null_total")

# Tokens that hint a column is a key/identifier (structure-only, language-agnostic:
# these are generic data-modeling suffixes/words, not business/domain words).
_KEY_TOKENS = ("id", "key", "code", "no", "num", "uuid", "guid", "pk", "fk")

# String dtypes that, when dominant across a "should-be-typed" dataset, indicate
# raw/bronze (part2 Bronze principle: "Keep as string -- don't cast yet").
_STRING_DTYPES = ("string", "str", "object", "utf8", "varchar", "text", "categorical")


# ---------------------------------------------------------------------------
# Small, defensive accessors (tolerate partial / differently-keyed profiles)
# ---------------------------------------------------------------------------
def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    return []


def _first_present(d: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d.get(k)
    return None


def _profile_table_name(profile: dict) -> str:
    """Best-effort stable table name from a profile dict."""
    for key in ("table", "table_name", "name", "dataset", "relation"):
        val = profile.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    path = profile.get("path") or profile.get("profile_path") or ""
    if isinstance(path, str) and path:
        # last path segment, strip known suffixes
        leaf = path.replace("\\", "/").rstrip("/").split("/")[-1]
        for suffix in (".profile.json", ".csv", ".parquet", ".json", ".tsv"):
            if leaf.lower().endswith(suffix):
                leaf = leaf[: -len(suffix)]
        return leaf or "table"
    return "table"


def _columns(profile: dict) -> list[dict]:
    cols = _as_list(profile.get("columns"))
    if cols:
        return [c for c in cols if isinstance(c, dict)]
    # Fall back to schema mapping {name: dtype}
    schema = profile.get("schema")
    if isinstance(schema, dict):
        return [{"name": str(n), "dtype": str(t)} for n, t in schema.items()]
    return []


def _row_count(profile: dict) -> int | None:
    rc = _first_present(profile, ("row_count", "rows", "n_rows", "record_count"))
    try:
        return int(rc) if rc is not None else None
    except (TypeError, ValueError):
        return None


def _col_name(col: dict) -> str:
    return str(col.get("name") or col.get("column") or "")


def _col_dtype(col: dict) -> str:
    return str(col.get("dtype") or col.get("type") or col.get("data_type") or "").lower()


def _is_string_dtype(dtype: str) -> bool:
    return any(tok in dtype for tok in _STRING_DTYPES)


def _is_key_like(name: str) -> bool:
    low = name.lower()
    # token boundary-ish check: split on non-alnum and look for whole-token matches,
    # plus common suffix matches (e.g. "PatientID" -> "id").
    tokens = []
    cur = ""
    for ch in low:
        if ch.isalnum():
            cur += ch
        else:
            if cur:
                tokens.append(cur)
            cur = ""
    if cur:
        tokens.append(cur)
    if any(tok in _KEY_TOKENS for tok in tokens):
        return True
    return any(low.endswith(tok) for tok in _KEY_TOKENS)


def _null_count(col: dict) -> int | None:
    nc = _first_present(col, _NULL_KEYS)
    try:
        return int(nc) if nc is not None else None
    except (TypeError, ValueError):
        return None


def _distinct_count(col: dict) -> int | None:
    dc = _first_present(col, _DISTINCT_KEYS)
    if dc is None:
        # fall back to distinct of sample_values if present
        samples = col.get("sample_values")
        if isinstance(samples, list) and samples:
            return len({str(s) for s in samples})
        return None
    try:
        return int(dc)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. Quality tier
# ---------------------------------------------------------------------------
def classify_quality_tier(profiles: list[dict]) -> dict:
    """Classify the workspace data-quality tier from profile evidence.

    Returns ``{"tier", "confidence", "evidence": [str, ...]}`` where ``tier`` is
    one of ``raw/bronze`` / ``silver`` / ``gold``.

    Heuristic (grounded in part2 medallion + quality gates):
      * High null rates / nulls in key columns           -> raw/bronze (failed PK gate).
      * Non-unique key columns (dedup not applied)        -> raw/bronze (Silver dedups).
      * Everything stored as string in a multi-table set  -> raw/bronze (Bronze keeps strings).
      * Clean + typed + unique keys + low nulls           -> silver.
      * Silver signals + few wide denormalized/aggregated -> gold (business-ready shape).
    """
    profiles = [p for p in (profiles or []) if isinstance(p, dict)]
    evidence: list[str] = []

    if not profiles:
        return {
            "tier": TIER_RAW,
            "confidence": 0.1,
            "evidence": ["No profile evidence supplied; defaulting to raw/bronze (cannot prove quality)."],
        }

    total_cells = 0
    total_nulls = 0
    null_measured_cols = 0
    missing_null_cols = 0

    key_cols_total = 0
    key_cols_unique = 0
    key_cols_with_nulls = 0
    key_uniqueness_unmeasured = 0

    string_cols = 0
    typed_cols = 0
    total_cols = 0

    bronze_metadata_hit = False  # _ingestion_*, _source_*, _is_quarantined (part2 bronze design)

    for prof in profiles:
        rc = _row_count(prof)
        cols = _columns(prof)
        for col in cols:
            total_cols += 1
            name = _col_name(col)
            dtype = _col_dtype(col)

            if _is_string_dtype(dtype):
                string_cols += 1
            elif dtype:
                typed_cols += 1

            low = name.lower()
            if low.startswith("_ingestion") or low.startswith("_source") or "quarantine" in low or low.startswith("_record_hash"):
                bronze_metadata_hit = True

            nc = _null_count(col)
            if nc is not None and rc:
                total_cells += rc
                total_nulls += nc
                null_measured_cols += 1
            else:
                missing_null_cols += 1

            if _is_key_like(name):
                key_cols_total += 1
                if nc is not None and nc > 0:
                    key_cols_with_nulls += 1
                dc = _distinct_count(col)
                if dc is not None and rc:
                    uniq_ratio = dc / rc if rc else 0.0
                    if uniq_ratio >= _DUP_TIER_THRESHOLD:
                        key_cols_unique += 1
                else:
                    key_uniqueness_unmeasured += 1

    overall_null_rate = (total_nulls / total_cells) if total_cells else None
    string_ratio = (string_cols / total_cols) if total_cols else None

    # ---- evidence narration ----
    if overall_null_rate is not None:
        evidence.append(
            f"Overall null rate {overall_null_rate:.4%} across {null_measured_cols} column(s) with measured null_count."
        )
    else:
        evidence.append("No null_count present in profiles; null-rate signal unavailable (lowers confidence).")

    if key_cols_total:
        evidence.append(
            f"{key_cols_total} key-like column(s) detected; {key_cols_unique} measurably unique, "
            f"{key_cols_with_nulls} with nulls, {key_uniqueness_unmeasured} with no distinct-count."
        )
    else:
        evidence.append("No key-like columns detected by name; uniqueness/dedup signal unavailable.")

    if string_ratio is not None:
        evidence.append(f"{string_ratio:.0%} of columns are string-typed ({string_cols}/{total_cols}).")
    if bronze_metadata_hit:
        evidence.append("Bronze ingestion-metadata columns present (_ingestion/_source/quarantine).")

    # ---- decision ----
    bronze_signals: list[str] = []
    if bronze_metadata_hit:
        bronze_signals.append("explicit bronze ingestion-metadata columns")
    if overall_null_rate is not None and overall_null_rate > _SILVER_MAX_NULL_RATE:
        bronze_signals.append(f"null rate {overall_null_rate:.2%} exceeds silver bound {_SILVER_MAX_NULL_RATE:.2%}")
    if key_cols_with_nulls:
        bronze_signals.append(f"{key_cols_with_nulls} key column(s) contain nulls (fails PK-completeness gate)")
    # dedup-not-applied: we found key cols, measured at least one, and none were unique
    measured_keys = key_cols_total - key_uniqueness_unmeasured
    if measured_keys > 0 and key_cols_unique == 0:
        bronze_signals.append("measured key column(s) are non-unique (deduplication not applied)")
    if string_ratio is not None and string_ratio >= 0.95 and len(profiles) >= 1 and typed_cols == 0 and total_cols >= 3:
        bronze_signals.append("all columns string-typed (raw, un-cast -> Bronze 'keep as string' principle)")

    # gold signals: business-ready denormalized/aggregated shape on top of clean data
    gold_signals: list[str] = []
    wide_or_aggregated = _looks_business_ready(profiles)
    if wide_or_aggregated:
        gold_signals.append(wide_or_aggregated)

    if bronze_signals:
        # Confidence scales with how many independent bronze signals fired and how
        # much was measurable.
        measured_fraction = _measured_fraction(null_measured_cols, missing_null_cols, key_uniqueness_unmeasured, key_cols_total)
        confidence = round(min(0.95, 0.55 + 0.12 * len(bronze_signals)) * (0.6 + 0.4 * measured_fraction), 3)
        evidence.append("Bronze/raw signals: " + "; ".join(bronze_signals) + ".")
        return {"tier": TIER_RAW, "confidence": confidence, "evidence": evidence}

    # No bronze signals fired. Need positive proof for silver/gold.
    clean_proven = (
        overall_null_rate is not None
        and overall_null_rate <= _SILVER_MAX_NULL_RATE
        and (measured_keys == 0 or key_cols_unique >= 1)
        and key_cols_with_nulls == 0
    )

    if not clean_proven:
        # Insufficient evidence to prove cleanliness -> conservative raw/bronze, low confidence.
        evidence.append(
            "Insufficient measured evidence to prove silver/gold cleanliness (missing null/distinct stats); "
            "defaulting conservatively to raw/bronze."
        )
        return {"tier": TIER_RAW, "confidence": 0.3, "evidence": evidence}

    # Clean + typed -> at least silver.
    gold_clean = (
        overall_null_rate is not None
        and overall_null_rate <= _GOLD_MAX_NULL_RATE
        and (measured_keys == 0 or key_cols_unique == measured_keys)
    )
    if gold_signals and gold_clean:
        evidence.append("Gold signals: " + "; ".join(gold_signals) + ".")
        confidence = round(min(0.92, 0.6 + 0.1 * len(gold_signals)), 3)
        return {"tier": TIER_GOLD, "confidence": confidence, "evidence": evidence}

    evidence.append(
        "Data is typed, key(s) unique, and null rate within the silver bound -> cleansed/conformed (silver)."
    )
    return {"tier": TIER_SILVER, "confidence": 0.8, "evidence": evidence}


def _measured_fraction(null_measured: int, null_missing: int, key_unmeasured: int, key_total: int) -> float:
    parts = []
    if (null_measured + null_missing) > 0:
        parts.append(null_measured / (null_measured + null_missing))
    if key_total > 0:
        parts.append((key_total - key_unmeasured) / key_total)
    if not parts:
        return 0.0
    return sum(parts) / len(parts)


def _looks_business_ready(profiles: list[dict]) -> str:
    """Return an evidence string if the profile set looks gold (denormalized/aggregated)."""
    # Aggregated/summary shape: a single (or very few) table(s) whose columns include
    # measure-like names (sum/avg/total/count/revenue/rate) -- structural hint, not domain.
    measure_tokens = ("sum", "avg", "average", "total", "count", "rate", "ratio", "amount", "_per_", "summary")
    table_count = len(profiles)
    if table_count == 1:
        cols = _columns(profiles[0])
        measure_hits = sum(1 for c in cols if any(t in _col_name(c).lower() for t in measure_tokens))
        if measure_hits >= 2:
            return f"single wide table with {measure_hits} measure/aggregate-named column(s) (business-ready summary)"
    return ""


# ---------------------------------------------------------------------------
# 2. Schema type
# ---------------------------------------------------------------------------
def classify_schema_type(profiles: list[dict], relationships: Any) -> dict:
    """Classify schema type from columns + inferred relationships.

    ``relationships`` may be the raw contract dict ``{"relationships": [...]}`` or the
    list of relationship records directly. Each record is expected to expose
    ``left_dataset/left_column`` and ``right_dataset/right_column`` (the shape emitted by
    ``core/onboarding/relationships/contracts.py``); legacy ``from_table/to_table`` keys
    are also tolerated.

    Returns ``{"schema_type", "confidence", "evidence": [...]}``.

    Detects (decision framework, schema_types_identification_guide.md sections 20-21):
      * flat / OBT      -> one table, no relationships.
      * hierarchical    -> a table whose FK references itself (parent_id -> id).
      * star            -> one fact + dims; dimensions do NOT reference each other.
      * snowflake       -> dimension references ANOTHER dimension (nested) -- the RCM shape.
      * galaxy          -> 2+ fact tables sharing dimensions.
      * 3NF/relational  -> many tables, FKs, no clear single fact (normalized OLTP).
    """
    profiles = [p for p in (profiles or []) if isinstance(p, dict)]
    rels = _normalize_relationships(relationships)
    evidence: list[str] = []

    table_names = [_profile_table_name(p) for p in profiles]
    n_tables = len(profiles)

    # ---- 0/1-table cases -> flat or OBT ----
    if n_tables <= 1:
        if n_tables == 0:
            return {"schema_type": "unknown", "confidence": 0.1,
                    "evidence": ["No profiles supplied; cannot classify schema type."]}
        cols = _columns(profiles[0])
        n_cols = len(cols)
        evidence.append(f"Single table '{table_names[0]}' with {n_cols} column(s); no inter-table relationships.")
        if n_cols >= 50:
            evidence.append("Column count >= 50 -> One Big Table (OBT): fully denormalized wide analytical table.")
            return {"schema_type": "OBT", "confidence": 0.8, "evidence": evidence}
        evidence.append("Few columns, single table, no joins -> flat schema (CSV/export shape).")
        return {"schema_type": "flat", "confidence": 0.75, "evidence": evidence}

    if not rels:
        evidence.append(
            f"{n_tables} tables but no inferred relationships; cannot prove fact/dimension topology."
        )
        # Multiple disconnected tables, no FKs -> treat as flat collection (low confidence).
        return {"schema_type": "flat", "confidence": 0.3, "evidence": evidence}

    # ---- build directed FK graph: child(referencing) -> parent(referenced dimension) ----
    # By contract convention left=fact/child side, right=dimension/parent (PK) side.
    # outdeg[table] = # of distinct dimensions this table references (fact-ness signal)
    # indeg[table]  = # of distinct tables referencing this table (dimension-ness signal)
    edges: list[tuple[str, str]] = []
    self_ref: set[str] = set()
    for rel in rels:
        child = _rel_child(rel)
        parent = _rel_parent(rel)
        if not child or not parent:
            continue
        if child == parent:
            self_ref.add(child)
            continue
        edges.append((child, parent))

    if self_ref and not edges:
        tbl = sorted(self_ref)[0]
        evidence.append(f"Self-referencing foreign key on '{tbl}' (parent->same table) -> hierarchical schema.")
        return {"schema_type": "hierarchical", "confidence": 0.8, "evidence": evidence}

    children = {c for c, _ in edges}
    parents = {p for _, p in edges}
    outdeg: dict[str, int] = {}
    indeg: dict[str, int] = {}
    for c, p in edges:
        outdeg.setdefault(c, 0)
        indeg.setdefault(p, 0)
    # count distinct
    out_targets: dict[str, set] = {}
    in_sources: dict[str, set] = {}
    for c, p in edges:
        out_targets.setdefault(c, set()).add(p)
        in_sources.setdefault(p, set()).add(c)
    outdeg = {t: len(s) for t, s in out_targets.items()}
    indeg = {t: len(s) for t, s in in_sources.items()}

    # Facts: reference 2+ dimensions and are not themselves referenced (or barely).
    fact_tables = sorted(t for t, d in outdeg.items() if d >= 2 and indeg.get(t, 0) == 0)
    # Dimensions referenced by something.
    dimension_tables = set(parents)

    # Snowflake/nested signal: a table that is BOTH referenced (a dimension) AND
    # references another table (a sub-dimension). i.e. dim -> dim chain.
    nested_dims = sorted(t for t in dimension_tables if t in children)
    if nested_dims:
        chains = []
        for nd in nested_dims:
            for parent in sorted(out_targets.get(nd, set())):
                chains.append(f"{nd} -> {parent}")
        evidence.append(
            "Nested dimension(s): dimension table references another dimension "
            f"({'; '.join(chains)})."
        )

    if not fact_tables:
        # No clear central fact. Could be normalized OLTP (3NF) or hierarchical chains.
        if nested_dims:
            evidence.append(
                f"{n_tables} tables linked by FKs with dimension->dimension chains but no single high-fan-out "
                "fact -> normalized relational (3NF) with hierarchy."
            )
            return {"schema_type": "3NF", "confidence": 0.55, "evidence": evidence}
        evidence.append(
            f"{n_tables} tables linked by foreign keys; no single high-fan-out fact table detected "
            "-> normalized relational (3NF)."
        )
        return {"schema_type": "3NF", "confidence": 0.55, "evidence": evidence}

    evidence.append(
        f"Fact table(s) (reference >=2 dimensions, referenced by none): {', '.join(fact_tables)}."
    )
    evidence.append(f"Dimension table(s): {', '.join(sorted(dimension_tables))}.")

    if len(fact_tables) >= 2:
        # Galaxy if facts share dimensions.
        shared = _shared_dimensions(fact_tables, out_targets)
        if shared:
            evidence.append(
                f"Multiple fact tables sharing dimension(s) {sorted(shared)} -> galaxy (fact constellation)."
            )
            return {"schema_type": "galaxy", "confidence": 0.78, "evidence": evidence}
        evidence.append("Multiple fact tables but no shared dimensions -> treated as galaxy (multi-fact).")
        return {"schema_type": "galaxy", "confidence": 0.6, "evidence": evidence}

    # Single fact table.
    if nested_dims:
        evidence.append(
            "Single fact + normalized (nested) dimensions -> snowflake schema."
        )
        confidence = 0.85 if any(d in dimension_tables for d in nested_dims) else 0.7
        return {"schema_type": "snowflake", "confidence": confidence, "evidence": evidence}

    evidence.append(
        "Single fact + dimensions that do NOT reference each other (denormalized) -> star schema."
    )
    return {"schema_type": "star", "confidence": 0.82, "evidence": evidence}


def _normalize_relationships(relationships: Any) -> list[dict]:
    if isinstance(relationships, dict):
        relationships = relationships.get("relationships")
    return [r for r in _as_list(relationships) if isinstance(r, dict)]


def _rel_child(rel: dict) -> str:
    """The referencing (fact/child) side of a relationship."""
    for key in ("left_dataset", "left_table", "from_dataset", "from_table", "child", "from"):
        val = rel.get(key)
        if isinstance(val, dict):
            val = val.get("table_name") or val.get("table") or val.get("name")
        if isinstance(val, str) and val.strip():
            return _norm_table(val)
    return ""


def _rel_parent(rel: dict) -> str:
    """The referenced (dimension/parent PK) side of a relationship."""
    for key in ("right_dataset", "right_table", "to_dataset", "to_table", "parent", "to"):
        val = rel.get(key)
        if isinstance(val, dict):
            val = val.get("table_name") or val.get("table") or val.get("name")
        if isinstance(val, str) and val.strip():
            return _norm_table(val)
    return ""


def _norm_table(value: str) -> str:
    leaf = value.replace("\\", "/").rstrip("/").split("/")[-1]
    for suffix in (".profile.json", ".csv", ".parquet", ".json", ".tsv"):
        if leaf.lower().endswith(suffix):
            leaf = leaf[: -len(suffix)]
    return leaf.strip()


def _shared_dimensions(fact_tables: list[str], out_targets: dict[str, set]) -> set:
    sets = [out_targets.get(f, set()) for f in fact_tables]
    if len(sets) < 2:
        return set()
    shared = set(sets[0])
    for s in sets[1:]:
        shared &= s
    return shared


# ---------------------------------------------------------------------------
# 3. Scoped processing options
# ---------------------------------------------------------------------------
def scoped_processing_options(tier: str, profiles: Any, schema_type: str | None = None) -> list[dict]:
    """Return the data-processing options RELEVANT to ``tier`` (not the full menu).

    Grounded in part2 medallion layer transitions + part1/part2 cleaning patterns:
      * raw/bronze -> offer the Bronze->Silver cleansing/conformance steps
        (type casting, null/key validation, dedup, schema conformance, quarantine).
      * silver     -> offer the Silver->Gold promotion steps
        (denormalize/aggregate, business metrics, SLA/serving shape).
      * gold       -> already business-ready; offer maintenance/optimization only.

    Each option: ``{id, label, description, applies_when}``.
    Schema-type-specific options are appended when relevant (e.g. snowflake ->
    pre-join dimensions when promoting to gold).
    """
    norm = (tier or "").strip().lower()
    profiles = [p for p in _as_list(profiles) if isinstance(p, dict)]
    schema_type = (schema_type or "").strip().lower()

    # Profile-derived flags so option *descriptions* are concrete to the workspace.
    has_string_dates = _has_string_date_columns(profiles)
    has_float_money = _has_float_columns(profiles)
    has_dup_risk = _has_unmeasured_or_nonunique_keys(profiles)

    options: list[dict] = []

    if norm in (TIER_RAW, "raw", "bronze", "raw/bronze"):
        # Bronze -> Silver cleansing/conformance menu.
        options.append({
            "id": "cast_types",
            "label": "Cast raw string columns to typed columns",
            "description": (
                "Apply schema-on-write: cast string-stored numerics/dates/decimals to their "
                "proper types, nulling un-parseable values (Bronze 'keep as string' -> Silver typed)."
                + (" Detected string-typed date-like columns to cast." if has_string_dates else "")
            ),
            "applies_when": "raw/bronze",
        })
        options.append({
            "id": "validate_keys",
            "label": "Validate key completeness and reject/quarantine null keys",
            "description": (
                "Enforce the Bronze->Silver PK-completeness gate (>99.9%): flag rows with null "
                "business/primary keys and route them to a quarantine instead of silver."
            ),
            "applies_when": "raw/bronze",
        })
        options.append({
            "id": "deduplicate",
            "label": "Deduplicate to one row per business key",
            "description": (
                "Keep the latest record per key (row_number over key ordered by modified/updated "
                "timestamp), removing duplicate rows so silver has a unique grain."
                + (" Key columns show duplicate/unverified uniqueness." if has_dup_risk else "")
            ),
            "applies_when": "raw/bronze",
        })
        options.append({
            "id": "conform_schema",
            "label": "Conform to a standardized schema (rename, standardize, UTC timestamps)",
            "description": (
                "Standardize column names, units, and timezones; parse nested/embedded structures; "
                "produce the conformed silver schema."
            ),
            "applies_when": "raw/bronze",
        })
        if has_float_money:
            options.append({
                "id": "fix_precision",
                "label": "Cast approximate float measures to fixed-precision decimals",
                "description": (
                    "Float-typed measure columns can drift in precision; cast to DECIMAL on promotion "
                    "to silver to make sums/aggregations exact."
                ),
                "applies_when": "raw/bronze",
            })
        return options

    if norm == TIER_SILVER:
        # Silver -> Gold promotion menu.
        options.append({
            "id": "denormalize",
            "label": "Denormalize/join into business-ready fact+dimension wide tables",
            "description": (
                "Pre-join conformed silver tables into wide gold tables shaped for the consuming "
                "use case, so analysts query without multi-table joins."
            ),
            "applies_when": "silver",
        })
        options.append({
            "id": "aggregate_metrics",
            "label": "Compute documented business metrics / aggregates",
            "description": (
                "Materialize the gold metric layer (e.g. summed/averaged measures grouped by "
                "business dimensions) with documented metric definitions."
            ),
            "applies_when": "silver",
        })
        options.append({
            "id": "referential_integrity_gate",
            "label": "Enforce the Silver->Gold referential-integrity gate",
            "description": (
                "Verify no orphan fact rows (every fact key resolves to a dimension) before "
                "publishing gold; hold the previous gold version if the check fails."
            ),
            "applies_when": "silver",
        })
        options.append({
            "id": "serving_sla",
            "label": "Define serving shape and freshness SLA",
            "description": (
                "Partition/cluster the gold tables for the read pattern and attach a freshness SLA "
                "so the serving layer is governed."
            ),
            "applies_when": "silver",
        })
        if schema_type == "snowflake":
            options.append({
                "id": "prejoin_nested_dimensions",
                "label": "Pre-join nested (snowflaked) dimensions",
                "description": (
                    "The schema is snowflake (dimension->dimension chains); flatten the nested "
                    "dimension hierarchy into denormalized gold dimensions to cut join depth."
                ),
                "applies_when": "silver",
            })
        return options

    if norm == TIER_GOLD:
        # Already business-ready: maintenance/optimization only (no re-cleaning flood).
        options.append({
            "id": "optimize_layout",
            "label": "Optimize storage layout (clustering/partitioning, compaction)",
            "description": (
                "Data is already business-ready; tune file layout (liquid clustering / Z-order, "
                "small-file compaction) for query performance rather than re-cleaning."
            ),
            "applies_when": "gold",
        })
        options.append({
            "id": "freshness_monitoring",
            "label": "Monitor freshness and reconciliation against SLA",
            "description": (
                "Add freshness/reconciliation checks (row counts, metric z-score bounds) so stale "
                "or anomalous gold data is caught before consumers see it."
            ),
            "applies_when": "gold",
        })
        return options

    # Unknown tier -> minimal, honest fallback (do not flood).
    return [{
        "id": "profile_first",
        "label": "Re-profile to establish a quality tier",
        "description": (
            f"Tier '{tier}' is not a recognized medallion tier; generate/refresh profiles so a "
            "tier (raw/bronze, silver, gold) can be classified before choosing processing steps."
        ),
        "applies_when": "unknown",
    }]


# --- profile-derived flags for concrete option descriptions ---
def _has_string_date_columns(profiles: list[dict]) -> bool:
    date_tokens = ("date", "_dt", "datetime", "timestamp", "_ts", "dob", "time")
    for prof in profiles:
        for col in _columns(prof):
            name = _col_name(col).lower()
            if _is_string_dtype(_col_dtype(col)) and any(t in name for t in date_tokens):
                return True
    return False


def _has_float_columns(profiles: list[dict]) -> bool:
    for prof in profiles:
        for col in _columns(prof):
            if "float" in _col_dtype(col) or "double" in _col_dtype(col):
                return True
    return False


def _has_unmeasured_or_nonunique_keys(profiles: list[dict]) -> bool:
    for prof in profiles:
        rc = _row_count(prof)
        for col in _columns(prof):
            if not _is_key_like(_col_name(col)):
                continue
            dc = _distinct_count(col)
            if dc is None:
                return True
            if rc and dc / rc < _DUP_TIER_THRESHOLD:
                return True
    return False
