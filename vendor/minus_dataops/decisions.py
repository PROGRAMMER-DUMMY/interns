"""Decision rules for the 6-step framework.

Each rule turns concrete inputs (latency SLA, daily volume, workload shape ...)
into a recommendation plus the trade-offs that justify it, tagged with the
framework step the decision belongs to. The rules are intentionally transparent
and rule-based -- no model, no hidden weights -- so a recommendation can always
be read back to the principle behind it.

Every recommender returns a :class:`Decision`. Unknown / ambiguous inputs never
fabricate certainty: they return ``confidence="low"`` and say what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    key: str
    question: str
    step: str                       # framework step key
    recommendation: str
    rationale: str
    confidence: str = "medium"      # high | medium | low
    alternatives: tuple[str, ...] = field(default_factory=tuple)
    tradeoffs: tuple[str, ...] = field(default_factory=tuple)
    inputs: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GB = 1_000_000_000
_TB = 1_000 * _GB
_PB = 1_000 * _TB


def _parse_latency_seconds(latency: str | float | int | None) -> Optional[float]:
    """Parse a latency SLA into seconds. Accepts numbers (seconds) or strings like
    ``"5m"``, ``"2 hours"``, ``"realtime"``, ``"daily"``. Returns None if unknown."""
    if latency is None:
        return None
    if isinstance(latency, (int, float)):
        return float(latency)
    s = str(latency).strip().lower()
    if not s:
        return None
    if s in {"realtime", "real-time", "real time", "instant", "subsecond", "sub-second"}:
        return 1.0
    if s in {"daily", "day"}:
        return 86_400.0
    if s in {"hourly", "hour"}:
        return 3_600.0
    units = {"s": 1, "sec": 1, "second": 1, "seconds": 1,
             "m": 60, "min": 60, "minute": 60, "minutes": 60,
             "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
             "d": 86400, "day": 86400, "days": 86400}
    num = ""
    unit = ""
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch.isalpha():
            unit += ch
    try:
        value = float(num) if num else 1.0
    except ValueError:
        return None
    return value * units.get(unit, 1)


def _parse_bytes(volume: str | float | int | None) -> Optional[float]:
    """Parse a volume/day into bytes. Accepts numbers (bytes) or ``"4TB"``,
    ``"500 gb"``, ``"2pb"``, ``"800mb"``. Returns None if unknown."""
    if volume is None:
        return None
    if isinstance(volume, (int, float)):
        return float(volume)
    s = str(volume).strip().lower().replace(" ", "")
    if not s:
        return None
    mult = 1.0
    for suffix, factor in (("pb", _PB), ("tb", _TB), ("gb", _GB),
                           ("mb", 1_000_000), ("kb", 1_000), ("b", 1)):
        if s.endswith(suffix):
            mult = factor
            s = s[: -len(suffix)]
            break
    try:
        return float(s) * mult
    except ValueError:
        return None


def _human_bytes(n: Optional[float]) -> str:
    if n is None:
        return "unknown"
    for unit, factor in (("PB", _PB), ("TB", _TB), ("GB", _GB),
                         ("MB", 1_000_000), ("KB", 1_000)):
        if n >= factor:
            return f"{n / factor:.2f} {unit}"
    return f"{n:.0f} B"


# ---------------------------------------------------------------------------
# Rules (step 1/2): batch vs stream
# ---------------------------------------------------------------------------


def batch_vs_stream(latency: str | float | None = None,
                    needs_history: Optional[bool] = None,
                    replayable: Optional[bool] = None) -> Decision:
    """Batch / stream / lambda / kappa, driven by the latency SLA.

    ``needs_history`` True + a sub-minute SLA suggests Lambda; ``replayable`` True
    (a single streaming pipeline kept as a replayable log) suggests Kappa.
    """
    secs = _parse_latency_seconds(latency)
    inputs = {"latency": latency, "latency_seconds": secs,
              "needs_history": needs_history, "replayable": replayable}

    if secs is None:
        return Decision(
            key="batch_vs_stream", step="pipeline",
            question="Batch or stream processing?",
            recommendation="Unknown -- ask for the latency SLA first",
            rationale="Latency is the deciding input and was not provided. Confirm how "
                      "fresh the data must be before choosing a track.",
            confidence="low",
            alternatives=("batch", "stream", "lambda", "kappa"),
            tradeoffs=("Streaming costs more to run and operate; only pay it if the SLA "
                       "truly needs sub-minute freshness.",),
            inputs=inputs)

    if secs < 60:
        if needs_history:
            rec, why = ("Lambda (batch + speed paths)",
                        "Sub-minute freshness is required AND accurate historical context "
                        "matters: run a batch path for history and a speed path for real "
                        "time, merging at the serving layer.")
        elif replayable:
            rec, why = ("Kappa (single streaming pipeline)",
                        "Sub-minute freshness with replay needs: one streaming pipeline, "
                        "Kafka kept as a long-term log to replay when logic changes.")
        else:
            rec, why = ("Stream processing",
                        "The SLA is under a minute, so batch cannot keep up. Use a streaming "
                        "ingestion + processing stack (Kafka + Flink / Spark Structured "
                        "Streaming) into a low-latency serving store.")
        conf = "high"
    elif secs <= 3_600:
        rec, why = ("Micro-batch or frequent batch",
                    "Minutes of latency are acceptable: schedule frequent/micro batches "
                    "rather than paying for always-on streaming.")
        conf = "medium"
    else:
        rec, why = ("Batch processing",
                    "Hours/days of latency are acceptable, so batch is cheaper and simpler: "
                    "Spark for compute, an orchestrator (Airflow/Prefect/Dagster) for "
                    "scheduling, landing in object storage.")
        conf = "high"

    return Decision(
        key="batch_vs_stream", step="pipeline",
        question="Batch or stream processing?",
        recommendation=rec, rationale=why, confidence=conf,
        alternatives=("batch", "stream", "lambda", "kappa"),
        tradeoffs=(
            "Stream: lowest latency, highest operational cost and complexity.",
            "Batch: cheapest and simplest, but data is only as fresh as the schedule.",
            "Lambda: real time + accurate history, but two codebases to keep in sync.",
            "Kappa: one codebase + replay, but everything must fit a streaming model.",
        ),
        inputs=inputs)


# ---------------------------------------------------------------------------
# Rules (step 1/4): compute engine by volume
# ---------------------------------------------------------------------------


def compute_engine(volume_per_day: str | float | None = None) -> Decision:
    """Single-node (Pandas/Polars) vs distributed (Spark), by daily volume."""
    b = _parse_bytes(volume_per_day)
    inputs = {"volume_per_day": volume_per_day, "bytes_per_day": b}
    if b is None:
        return Decision(
            key="compute_engine", step="requirements",
            question="Single-node or distributed compute?",
            recommendation="Unknown -- estimate daily volume first",
            rationale="Do the back-of-the-envelope volume math before choosing an engine.",
            confidence="low",
            alternatives=("single-node (Polars/Pandas/DuckDB)", "distributed (Spark)"),
            inputs=inputs)

    if b < 50 * _GB:
        rec, why, conf = ("Single-node (Polars / DuckDB / Pandas)",
                          f"At ~{_human_bytes(b)}/day a single node handles it comfortably; "
                          "a distributed engine would add cost and overhead for no gain.",
                          "high")
    elif b < _TB:
        rec, why, conf = ("Single-node, but plan the jump to distributed",
                          f"~{_human_bytes(b)}/day still fits a large single node (esp. "
                          "DuckDB/Polars out-of-core), but headroom is thin -- design so a "
                          "move to Spark is cheap.",
                          "medium")
    else:
        rec, why, conf = ("Distributed (Spark)",
                          f"~{_human_bytes(b)}/day exceeds a single node; use distributed "
                          "compute plus storage optimisations (partitioning, compaction).",
                          "high")
    return Decision(
        key="compute_engine", step="requirements",
        question="Single-node or distributed compute?",
        recommendation=rec, rationale=why, confidence=conf,
        alternatives=("single-node (Polars/DuckDB/Pandas)", "distributed (Spark)"),
        tradeoffs=("Single-node: simplest, cheapest, fastest to iterate -- until data "
                   "outgrows memory/one machine.",
                   "Distributed: scales to TB/PB and parallel shuffle, at the cost of "
                   "cluster ops, tuning, and minimum overhead."),
        inputs=inputs)


# ---------------------------------------------------------------------------
# Rules (step 4): storage format / table format / database type
# ---------------------------------------------------------------------------


def storage_format(workload: str = "analytics") -> Decision:
    """Parquet (read-heavy analytics) vs Avro (write-heavy streaming)."""
    w = (workload or "").strip().lower()
    streaming = any(t in w for t in ("stream", "write", "ingest", "kafka", "serialize"))
    inputs = {"workload": workload}
    if streaming:
        rec, why = ("Avro (row-based)",
                    "Write-heavy streaming ingestion serializes whole events in one shot; "
                    "a row format avoids columnar restructuring overhead on the hot path.")
    else:
        rec, why = ("Parquet (columnar)",
                    "Read-heavy analytical queries benefit from column pruning and "
                    "file-level statistics -- Parquet skips unused columns and whole files.")
    return Decision(
        key="storage_format", step="storage",
        question="Row-based or columnar file format?",
        recommendation=rec, rationale=why, confidence="high",
        alternatives=("Parquet (columnar)", "Avro (row-based)"),
        tradeoffs=("Parquet: fast analytical reads; not ideal as a streaming write buffer.",
                   "Avro: fast event writes + schema evolution; poor for wide analytical scans."),
        inputs=inputs)


def table_format(concurrent_writes: Optional[bool] = None,
                 needs_acid: Optional[bool] = None,
                 streaming_small_files: Optional[bool] = None,
                 time_travel: Optional[bool] = None) -> Decision:
    """Raw Parquet vs an open table format (Delta / Iceberg / Hudi)."""
    inputs = {"concurrent_writes": concurrent_writes, "needs_acid": needs_acid,
              "streaming_small_files": streaming_small_files, "time_travel": time_travel}
    drivers = [
        ("concurrent writers", concurrent_writes),
        ("ACID guarantees", needs_acid),
        ("a streaming small-file problem", streaming_small_files),
        ("time travel / history", time_travel),
    ]
    reasons = [name for name, flag in drivers if flag]
    if reasons:
        rec = "Open table format (Delta / Iceberg / Hudi)"
        why = ("Raw Parquet in object storage cannot give you " + ", ".join(reasons) +
               ". An open table format adds ACID transactions, schema enforcement, time "
               "travel, and compaction (OPTIMIZE) to bundle tiny files.")
        conf = "high"
    elif any(f is not None for _, f in drivers):
        rec = "Raw Parquet is acceptable"
        why = ("No concurrent writes, ACID, small-file, or time-travel needs were "
               "indicated, so plain Parquet files keep things simple. Revisit the moment "
               "any of those appear.")
        conf = "medium"
    else:
        rec = "Default to an open table format (Delta / Iceberg / Hudi)"
        why = ("Inputs unknown. In practice most lakes hit concurrent writes, the "
               "small-file problem, or a backfill needing atomic overwrites -- an open "
               "table format is the safe default and unlocks idempotent MERGE + partition "
               "overwrite later.")
        conf = "low"
    return Decision(
        key="table_format", step="storage",
        question="Raw Parquet files or an open table format?",
        recommendation=rec, rationale=why, confidence=conf,
        alternatives=("raw Parquet", "Delta Lake", "Apache Iceberg", "Apache Hudi"),
        tradeoffs=("Raw Parquet: zero extra machinery; no ACID, no compaction, corruption "
                   "risk on concurrent writes.",
                   "Open table format: ACID + time travel + OPTIMIZE; a metadata layer to "
                   "operate and a small write overhead."),
        inputs=inputs)


def database_type(structured: Optional[bool] = None,
                  needs_flexibility: Optional[bool] = None,
                  strict_consistency: Optional[bool] = None) -> Decision:
    """Relational vs NoSQL for a serving/operational store."""
    inputs = {"structured": structured, "needs_flexibility": needs_flexibility,
              "strict_consistency": strict_consistency}
    relational = bool(structured) or bool(strict_consistency)
    nosql = bool(needs_flexibility) and not strict_consistency
    if relational and not nosql:
        rec, why, conf = ("Relational",
                          "Highly structured data with rigid relationships and/or strict "
                          "ACID consistency fits a relational database.", "high")
    elif nosql and not relational:
        rec, why, conf = ("Non-relational (NoSQL)",
                          "Structural flexibility and rapid schema evolution, trading strict "
                          "consistency for speed/availability, fits a NoSQL store.", "high")
    else:
        rec, why, conf = ("Depends -- clarify consistency vs flexibility",
                          "Choose relational for structure + strict ACID; NoSQL for "
                          "flexibility + availability over strict consistency.", "low")
    return Decision(
        key="database_type", step="storage",
        question="Relational or non-relational store?",
        recommendation=rec, rationale=why, confidence=conf,
        alternatives=("relational (Postgres/MySQL)", "NoSQL (DynamoDB/Mongo/Cassandra)"),
        tradeoffs=("Relational: strong consistency + joins; schema rigidity, harder "
                   "horizontal scale.",
                   "NoSQL: flexible + horizontally scalable + highly available; weaker "
                   "consistency, joins pushed to the app."),
        inputs=inputs)


# ---------------------------------------------------------------------------
# Rules (step 3): modeling shape + SCD
# ---------------------------------------------------------------------------


def modeling_shape(predictable_dashboard: Optional[bool] = None,
                   many_consumers: Optional[bool] = None,
                   frequent_updates: Optional[bool] = None) -> Decision:
    """Star schema vs One Big Table (OBT / denormalized)."""
    inputs = {"predictable_dashboard": predictable_dashboard,
              "many_consumers": many_consumers, "frequent_updates": frequent_updates}
    if predictable_dashboard and not frequent_updates:
        rec, why, conf = ("One Big Table (OBT / denormalized) at Gold",
                          "A fixed, predictable dashboard query pattern with stable data "
                          "benefits from pre-joining everything flat -- it removes heavy "
                          "join/shuffle at query time.", "medium")
    elif frequent_updates or many_consumers:
        rec, why, conf = ("Star schema (facts + dimensions)",
                          "Frequent updates and/or many different consumers favour a star "
                          "schema: clean, standard for BI tools, and dimension updates don't "
                          "force rewriting one giant flat table.", "medium")
    else:
        rec, why, conf = ("Star schema (safe default), OBT for hot dashboards",
                          "Default to a star schema for flexibility; denormalise specific "
                          "Gold tables into OBT where the query pattern is fixed and "
                          "shuffle-heavy.", "low")
    return Decision(
        key="modeling_shape", step="modeling",
        question="Star schema or One Big Table?",
        recommendation=rec, rationale=why, confidence=conf,
        alternatives=("star schema", "OBT / denormalized"),
        tradeoffs=("Star: clean, flexible, BI-standard; joins/shuffle at query time.",
                   "OBT: no query-time joins, fast predictable dashboards; complex update "
                   "pipelines when source data changes."),
        inputs=inputs)


def scd_type(track_history: Optional[bool] = None,
             audit_required: Optional[bool] = None) -> Decision:
    """SCD Type 1 (overwrite) vs Type 2 (historical rows + validity timestamps)."""
    inputs = {"track_history": track_history, "audit_required": audit_required}
    if track_history or audit_required:
        rec, why, conf = ("SCD Type 2 (keep history)",
                          "History matters (point-in-time correctness or audit), so keep "
                          "prior rows with validity timestamps (valid_from / valid_to) "
                          "rather than overwriting.", "high")
    elif track_history is False:
        rec, why, conf = ("SCD Type 1 (overwrite)",
                          "Only the current value matters, so overwrite in place -- simpler "
                          "and cheaper, with no history.", "high")
    else:
        rec, why, conf = ("Clarify whether history is needed",
                          "SCD1 overwrites (current-only); SCD2 preserves history with "
                          "validity timestamps. The answer depends on whether anyone needs "
                          "the past state.", "low")
    return Decision(
        key="scd_type", step="modeling",
        question="How are dimension changes handled (SCD)?",
        recommendation=rec, rationale=why, confidence=conf,
        alternatives=("SCD Type 1 (overwrite)", "SCD Type 2 (history)"),
        tradeoffs=("SCD1: simple, small, fast; loses history entirely.",
                   "SCD2: full point-in-time history + audit; larger tables and "
                   "as-of join logic downstream."),
        inputs=inputs)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

RULES: dict[str, Callable[..., Decision]] = {
    "batch_vs_stream": batch_vs_stream,
    "compute_engine": compute_engine,
    "storage_format": storage_format,
    "table_format": table_format,
    "database_type": database_type,
    "modeling_shape": modeling_shape,
    "scd_type": scd_type,
}


def decide(rule: str, **kwargs) -> Decision:
    """Run a named decision rule. Raises KeyError for an unknown rule."""
    if rule not in RULES:
        raise KeyError(f"unknown decision {rule!r}; valid: {', '.join(RULES)}")
    return RULES[rule](**kwargs)


def rule_keys() -> tuple[str, ...]:
    return tuple(RULES)


__all__ = ["Decision", "decide", "rule_keys", "RULES",
           "batch_vs_stream", "compute_engine", "storage_format", "table_format",
           "database_type", "modeling_shape", "scd_type"]
