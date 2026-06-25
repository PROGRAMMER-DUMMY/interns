"""Interview / whiteboard coach: walk the 6-step framework as a system-design
drill, and score a candidate's answer against what the framework expects.

Two modes:

* ``drill(scenario)`` -- emit the Socratic question set for each step, with the
  canonical approach and the most common miss, optionally framed by a scenario
  (e.g. "ride-share trips", "clickstream analytics").
* ``score(answer_text)`` -- check a free-text answer for coverage of the key
  concepts each step expects, and report what was hit and what was skipped.

This is a teaching tool: it does not pretend to grade nuance, only whether the
framework's load-bearing ideas were addressed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from minus_dataops.framework import FRAMEWORK


@dataclass(frozen=True)
class DrillStep:
    step_key: str
    title: str
    prompts: tuple[str, ...]            # Socratic questions to ask
    canonical: str                      # what a strong answer covers
    common_miss: str                    # the thing candidates skip
    # lowercase concept tokens that should appear in a strong answer:
    concepts: tuple[str, ...]


DRILL: tuple[DrillStep, ...] = (
    DrillStep(
        step_key="requirements",
        title="Step 1: Requirements Gathering",
        prompts=(
            "Who is the end user, and how will they access the data (SQL / BI / API)?",
            "What is the latency SLA -- seconds, minutes, or hours/days?",
            "What is the daily volume -- GB, TB, or PB? Do the back-of-envelope math.",
            "What are the availability and data-retention requirements?",
        ),
        canonical="Pin functional (who/what/how) and non-functional (latency, volume, "
                  "availability, retention) requirements, then size with "
                  "DAU x sessions x events x payload before drawing anything.",
        common_miss="Jumping to architecture boxes without the back-of-envelope volume "
                    "number that justifies the technology choices.",
        concepts=("user", "latency", "volume", "retention", "back-of-envelope",
                  "availability", "sla"),
    ),
    DrillStep(
        step_key="pipeline",
        title="Step 2: Pipeline Design",
        prompts=(
            "Batch or stream -- and why does the SLA force that choice?",
            "If streaming, where does buffering/decoupling happen (Kafka)?",
            "Do you need a hybrid (Lambda for history + speed, or Kappa for replay)?",
        ),
        canonical="Justify batch vs stream from the latency SLA; name an orchestrator for "
                  "batch (Airflow/Prefect/Dagster) or Kafka + Flink/Spark Streaming for "
                  "stream; reach for Lambda/Kappa only when both history and real time "
                  "are required.",
        common_miss="Defaulting to streaming when batch meets the SLA at a fraction of the "
                    "cost and complexity.",
        concepts=("batch", "stream", "kafka", "orchestrat", "airflow", "lambda", "kappa",
                  "spark"),
    ),
    DrillStep(
        step_key="modeling",
        title="Step 3: Data Modeling",
        prompts=(
            "Lay out the medallion layers -- what lives in Bronze, Silver, Gold?",
            "Star schema or One Big Table for the serving layer -- justify it.",
            "Which dimensions need history (SCD2) vs overwrite (SCD1)?",
            "How do you partition to avoid full-table scans?",
        ),
        canonical="Bronze (raw append) -> Silver (clean/de-dup/type-cast facts+dims) -> "
                  "Gold (pre-joined/aggregated); pick star vs OBT from the query pattern; "
                  "choose SCD1/2 per dimension; partition by event date.",
        common_miss="Drawing tables without layering, or ignoring SCD so historical "
                    "correctness silently breaks.",
        concepts=("bronze", "silver", "gold", "medallion", "star", "obt", "denormal",
                  "scd", "partition", "dimension", "fact"),
    ),
    DrillStep(
        step_key="storage",
        title="Step 4: Storage & File Format",
        prompts=(
            "Columnar (Parquet) or row (Avro) -- match it to the workload.",
            "Raw files or an open table format (Delta/Iceberg/Hudi) -- why?",
            "Relational or NoSQL for the serving store?",
        ),
        canonical="Parquet for read-heavy analytics, Avro for write-heavy streaming; an "
                  "open table format for ACID + schema enforcement + time travel + "
                  "compaction; relational for structure/ACID, NoSQL for flexibility/scale.",
        common_miss="Dumping raw Parquet into object storage and inheriting the small-file "
                    "problem and no ACID guarantees.",
        concepts=("parquet", "avro", "columnar", "delta", "iceberg", "hudi", "acid",
                  "relational", "nosql"),
    ),
    DrillStep(
        step_key="quality",
        title="Step 5: Data Quality & Observability",
        prompts=(
            "Which checks cover the five DQ dimensions?",
            "Where do data contracts sit, and how do they shift quality left?",
            "What pipeline observability (DAG times, failures, cluster metrics) is wired?",
        ),
        canonical="Concrete checks for Completeness, Accuracy, Consistency, Freshness, "
                  "Uniqueness; data contracts via schema registry / Great Expectations to "
                  "block bad schemas pre-lake; observability via orchestrator or DataDog/"
                  "CloudWatch.",
        common_miss="Treating quality as ad-hoc asserts instead of contracts that block "
                    "breakage before it enters the lake.",
        concepts=("completeness", "accuracy", "consistency", "freshness", "uniqueness",
                  "contract", "observability", "great expectations", "schema registry"),
    ),
    DrillStep(
        step_key="scalability",
        title="Step 6: Scalability, Backfills & DataOps",
        prompts=(
            "How do you guarantee idempotency on incremental loads?",
            "What is the backfill strategy when a definition changes?",
            "How does the pipeline handle upstream schema evolution?",
        ),
        canonical="MERGE/upsert for idempotency; atomic partition overwrite for backfills "
                  "(Delta/Iceberg); flexible Bronze (mergeSchema) but strict Silver/Gold "
                  "for schema evolution.",
        common_miss="Using flat INSERT (non-idempotent) so a rerun duplicates data, and "
                    "having no clean backfill path.",
        concepts=("idempoten", "merge", "upsert", "backfill", "partition overwrite",
                  "schema evolution", "mergeschema"),
    ),
)

_BY_KEY = {d.step_key: d for d in DRILL}


@dataclass
class StepScore:
    step_key: str
    title: str
    hit: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        total = len(self.hit) + len(self.missed)
        return (len(self.hit) / total) if total else 0.0


@dataclass
class ScoreCard:
    steps: list[StepScore]

    @property
    def overall(self) -> float:
        if not self.steps:
            return 0.0
        return sum(s.fraction for s in self.steps) / len(self.steps)


def drill(scenario: Optional[str] = None,
          step: Optional[str] = None) -> tuple[DrillStep, ...]:
    """Return the drill steps (optionally just one). ``scenario`` is advisory text
    the CLI prints as framing; it does not change the questions."""
    if step:
        from minus_dataops.framework import get_step
        key = get_step(step).key
        return (_BY_KEY[key],) if key in _BY_KEY else ()
    return DRILL


# generic words that aren't concepts on their own, filtered when mining the deep layer
_STOPWORDS = frozenset({
    "vs", "the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "by",
    "with", "data", "table", "format", "schema", "layer", "design", "based",
    "type", "types", "store", "model", "modeling", "system", "raw", "open",
    "big", "one", "only", "full", "load", "write", "read", "copy", "merge",
    "path", "side", "join", "key", "keys", "mode", "tier",
})


def _deep_tokens(step_key: str) -> tuple[str, ...]:
    """Mine extra concept tokens from a step's deep dossier. Uses comparison OPTION
    names only -- those are concrete choices/technologies (Parquet, Iceberg,
    Streaming, SCD2 ...) and make clean, matchable tokens, unlike descriptive topic
    titles. Returns () if the depth layer isn't available."""
    try:
        from minus_dataops.depth import get_deep
    except Exception:
        return ()
    deep = get_deep(step_key)
    if deep is None:
        return ()
    toks: list[str] = []
    for c in deep.comparisons:
        for o in c.options:
            for raw in (o.name.lower().replace("/", " ").replace("-", " ")
                        .replace("(", " ").replace(")", " ").split()):
                w = raw.strip(".,:;")
                if len(w) >= 4 and w not in _STOPWORDS and w.isalpha():
                    toks.append(w)
    return tuple(dict.fromkeys(toks))


def score(answer_text: str, step: Optional[str] = None,
          deep: bool = False) -> ScoreCard:
    """Score a free-text answer for concept coverage against the framework.

    ``deep=True`` also checks coverage of concepts mined from the deep dossiers
    (technology/option names), so the grade reflects the full deep concept set,
    not just the short curated coach list.
    """
    text = (answer_text or "").lower()
    targets = drill(step=step) if step else DRILL
    cards: list[StepScore] = []
    for d in targets:
        sc = StepScore(step_key=d.step_key, title=d.title)
        concepts = list(d.concepts)
        if deep:
            for t in _deep_tokens(d.step_key):
                if t not in concepts:
                    concepts.append(t)
        for concept in concepts:
            (sc.hit if concept in text else sc.missed).append(concept)
        cards.append(sc)
    return ScoreCard(steps=cards)


def scenarios() -> tuple[str, ...]:
    """A few canonical practice scenarios (advisory framing only)."""
    return (
        "clickstream analytics (web events -> dashboards)",
        "ride-share trips (real-time + historical reporting)",
        "fraud detection (sub-second scoring)",
        "IoT sensor telemetry (high-volume time series)",
        "e-commerce orders (transactional + BI)",
    )


__all__ = ["DrillStep", "DRILL", "StepScore", "ScoreCard", "drill", "score", "scenarios"]
