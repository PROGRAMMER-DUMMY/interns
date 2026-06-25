"""The 6-step data-engineering system-design framework, codified as structured,
queryable data.

This is the *knowledge core* of MinusDataOps. Every other module (the decision
engine, the workspace advisor, the interview coach) reads from the same canonical
``FRAMEWORK`` defined here, so there is exactly one source of truth for "what good
looks like" -- no prose duplicated across files that can drift apart.

The content is deliberately tool- and cloud-agnostic: it names representative
technologies (Spark, Kafka, Delta, Airflow ...) as *examples* of a category, never
as the only valid choice. Nothing here is tied to a particular workspace or domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    """One labelled cluster of knowledge inside a step."""

    title: str
    points: tuple[str, ...]


@dataclass(frozen=True)
class Step:
    """One of the six stages of a data-platform system design."""

    num: int
    key: str
    title: str
    goal: str
    sections: tuple[Section, ...]
    # keys into ``minus_dataops.decisions`` that this step is where you make:
    decisions: tuple[str, ...] = field(default_factory=tuple)
    pro_tip: Optional[str] = None


# ---------------------------------------------------------------------------
# The framework
# ---------------------------------------------------------------------------


FRAMEWORK: tuple[Step, ...] = (
    Step(
        num=1,
        key="requirements",
        title="Requirements Gathering",
        goal=(
            "Before drawing a single box, spend the opening minutes asking clarifying "
            "questions. The architecture changes entirely with the answers."
        ),
        sections=(
            Section(
                "Functional -- what the system must do",
                (
                    "Who: identify the end user -- a SQL analyst, an ML team feeding a "
                    "feature store, or a product team watching real-time metrics. Each "
                    "implies a different architecture.",
                    "What: the specific data need -- raw event grain, aggregated metrics, "
                    "or historical snapshots.",
                    "How: the access path -- direct SQL, a BI tool (Tableau / Power BI), "
                    "or a REST API.",
                ),
            ),
            Section(
                "Non-functional -- how the system must behave",
                (
                    "Latency SLA: fresh within seconds/minutes (-> streaming) or "
                    "hours/days acceptable (-> batch).",
                    "Volume: GB, TB, or PB per day. This decides single-node tools "
                    "(Pandas/Polars) vs distributed compute (Spark) and storage layout.",
                    "Availability: the tolerance for downtime.",
                    "Data retention: how long data is kept -- forever vs archive/expire "
                    "after a window (e.g. 90 days) to control cost.",
                ),
            ),
        ),
        decisions=("batch_vs_stream", "compute_engine"),
        pro_tip=(
            "Back-of-the-envelope first: estimate volume as "
            "daily active users x sessions x events/session x payload size. The number "
            "justifies every later choice -- it can rule out streaming when batch "
            "suffices, or force a distributed engine."
        ),
    ),
    Step(
        num=2,
        key="pipeline",
        title="Pipeline Design",
        goal="Decide how data physically moves and transforms from source to destination.",
        sections=(
            Section(
                "Batch processing",
                (
                    "Best when cost matters more than latency (hourly/daily reporting).",
                    "Typical stack: Apache Spark for heavy lifting; Airflow / Prefect / "
                    "Dagster for orchestration.",
                    "Extract -> land in object storage (S3 / ADLS) -> move through layers.",
                ),
            ),
            Section(
                "Stream processing",
                (
                    "Required for low-latency needs under a minute (fraud detection, "
                    "real-time dashboards).",
                    "Typical stack: Kafka for ingestion / buffering / decoupling "
                    "producers; Spark Structured Streaming or Flink for processing.",
                    "Output to low-latency serving stores (Redis, DynamoDB).",
                ),
            ),
            Section(
                "Hybrid architectures",
                (
                    "Lambda: a batch path (accurate history) and a speed path (real time) "
                    "run in parallel and merge at the serving layer.",
                    "Kappa: a single streaming pipeline for everything; Kafka is kept as a "
                    "long-term log so events can be replayed when business logic changes.",
                ),
            ),
        ),
        decisions=("batch_vs_stream",),
    ),
    Step(
        num=3,
        key="modeling",
        title="Data Modeling",
        goal=(
            "Don't just draw tables -- organise data into layers and justify the schema "
            "pattern under scale."
        ),
        sections=(
            Section(
                "Medallion architecture",
                (
                    "Bronze: append raw data exactly as it arrives (JSON dumps, CDC logs), "
                    "no cleaning -- an immutable safety net.",
                    "Silver: clean, de-duplicate, type-cast, structure. The core validated "
                    "facts and dimensions live here.",
                    "Gold: pre-aggregated, pre-joined datasets tuned for fast BI/analyst "
                    "queries.",
                ),
            ),
            Section(
                "Star schema vs One Big Table (OBT)",
                (
                    "Star schema separates facts and dimensions -- clean and standard for "
                    "BI tools.",
                    "OBT pre-joins everything into one flat table to remove heavy "
                    "join/shuffle for predictable dashboard queries -- at the cost of "
                    "more complex update pipelines.",
                ),
            ),
            Section(
                "Slowly Changing Dimensions (SCD)",
                (
                    "Plan how state updates are handled (e.g. a user upgrading free -> "
                    "premium).",
                    "SCD Type 1 overwrites history; SCD Type 2 keeps historical rows with "
                    "validity timestamps.",
                ),
            ),
            Section(
                "Partitioning & optimization",
                (
                    "Partition logically (most often by event date) to avoid full-table "
                    "scans.",
                    "Liquid clustering can reorganise data dynamically without locking in "
                    "rigid physical partition keys up front.",
                ),
            ),
        ),
        decisions=("modeling_shape", "scd_type"),
    ),
    Step(
        num=4,
        key="storage",
        title="Storage & File Format",
        goal="Decide exactly where and in which format data lives -- it drives cost and query speed.",
        sections=(
            Section(
                "Row vs columnar formats",
                (
                    "Parquet: columnar, tuned for read-heavy analytics -- column pruning "
                    "(skip unused columns) and file-level statistics speed up queries.",
                    "Avro: row-based, tuned for write-heavy streaming ingestion -- events "
                    "serialize in one shot with no columnar restructuring overhead.",
                ),
            ),
            Section(
                "Open table formats (Delta / Iceberg / Hudi)",
                (
                    "Dumping raw Parquet into object storage loses ACID guarantees, risks "
                    "corruption from concurrent writes, and creates a small-file problem "
                    "from streaming.",
                    "Open table formats add ACID transactions, schema enforcement, time "
                    "travel, and compaction (OPTIMIZE) to bundle tiny files.",
                ),
            ),
            Section(
                "Database type",
                (
                    "Relational: highly structured data, rigid relationships, strict ACID.",
                    "Non-relational (NoSQL): structural flexibility, rapid evolution, high "
                    "speed/availability over strict consistency.",
                ),
            ),
        ),
        decisions=("storage_format", "table_format", "database_type"),
    ),
    Step(
        num=5,
        key="quality",
        title="Data Quality & Observability",
        goal="Prevent, detect, and handle corrupted data and failing infrastructure in production.",
        sections=(
            Section(
                "The five data-quality dimensions",
                (
                    "Completeness: no missing data (e.g. row count suddenly drops 30%).",
                    "Accuracy: data is correct (e.g. delivery time is not before order time).",
                    "Consistency: data agrees across systems (e.g. sales revenue matches "
                    "the payment processor).",
                    "Freshness: data is not stale relative to its SLA.",
                    "Uniqueness: no unexpected duplicate records.",
                ),
            ),
            Section(
                "Data contracts",
                (
                    "A formal schema agreement between producers (microservices) and "
                    "consumers (the pipeline).",
                    "Enforced via schema registries (Avro + Confluent) or validation tools "
                    "(Great Expectations) -- they shift quality left, blocking broken "
                    "schemas before they reach the lake.",
                ),
            ),
            Section(
                "Pipeline observability",
                (
                    "Monitor system health, DAG execution times, failures, and cluster "
                    "metrics -- via native orchestrator features or DataDog / CloudWatch.",
                ),
            ),
        ),
        decisions=(),
    ),
    Step(
        num=6,
        key="scalability",
        title="Scalability, Backfills & DataOps",
        goal="Make the system resilient and able to handle failure gracefully.",
        sections=(
            Section(
                "Idempotency -- the golden rule",
                (
                    "Running the pipeline once or five times on the same input yields the "
                    "same output -- no duplication, no destruction.",
                    "Achieved with MERGE (upsert) instead of flat INSERT for incremental "
                    "loads.",
                ),
            ),
            Section(
                "Backfill strategy",
                (
                    "A clean way to reprocess months/years of history when a bug is found "
                    "or a definition changes.",
                    "Modern table formats (Delta/Iceberg) overwrite specified historical "
                    "partitions atomically.",
                ),
            ),
            Section(
                "Schema evolution",
                (
                    "Handle upstream changes gracefully.",
                    "Be flexible at Bronze (e.g. Delta mergeSchema to capture new columns) "
                    "but strict at Silver/Gold so downstream dashboards and ML don't break.",
                ),
            ),
        ),
        decisions=(),
    ),
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

_BY_KEY = {s.key: s for s in FRAMEWORK}
_BY_NUM = {s.num: s for s in FRAMEWORK}


def steps() -> tuple[Step, ...]:
    """The six steps, in order."""
    return FRAMEWORK


def get_step(ref: str | int) -> Step:
    """Look up a step by number (1..6) or key (e.g. ``"modeling"``).

    Accepts loose forms: ``"3"``, ``"step3"``, ``"data modeling"``.
    """
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        n = int(ref)
        if n in _BY_NUM:
            return _BY_NUM[n]
        raise KeyError(f"no step numbered {n} (valid: 1..{len(FRAMEWORK)})")

    norm = str(ref).strip().lower().replace("-", " ").replace("_", " ")
    norm = norm.removeprefix("step").strip()
    if norm.isdigit():               # e.g. "step5" -> "5"
        n = int(norm)
        if n in _BY_NUM:
            return _BY_NUM[n]
    if norm in _BY_KEY:
        return _BY_KEY[norm]
    # fuzzy: match on key prefix or words in the title
    for s in FRAMEWORK:
        if s.key.startswith(norm) or norm in s.title.lower():
            return s
    raise KeyError(
        f"unknown step {ref!r}; valid keys: {', '.join(s.key for s in FRAMEWORK)}"
    )


def step_keys() -> tuple[str, ...]:
    return tuple(s.key for s in FRAMEWORK)


__all__ = ["Section", "Step", "FRAMEWORK", "steps", "get_step", "step_keys"]
