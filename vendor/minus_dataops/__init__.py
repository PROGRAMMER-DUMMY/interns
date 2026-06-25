"""MinusDataOps -- data-engineering system-design knowledge, codified.

A sibling vendor to ``minus`` (the live Dash app) and the ``minus_deck`` /
``minus_press`` exporters. Where those *render* a generated dashboard, MinusDataOps
*reasons about the data platform behind it* using a single 6-step framework:

  1. Requirements gathering   (who/what/how + latency/volume/availability/retention)
  2. Pipeline design          (batch / stream / Lambda / Kappa)
  3. Data modeling            (medallion, star vs OBT, SCD, partitioning)
  4. Storage & file format    (Parquet/Avro, Delta/Iceberg/Hudi, relational/NoSQL)
  5. Data quality & observability (the five DQ dimensions, contracts, monitoring)
  6. Scalability, backfills & DataOps (idempotency, backfills, schema evolution)

Three ways to use it (all from one ``minus-dataops`` CLI / this API):

  * **Knowledge engine** -- ``framework`` exposes the 6 steps as structured data you
    can query and explain.
  * **Decision engine** -- ``decisions`` turns concrete inputs (latency SLA, daily
    volume, workload shape) into a recommendation + the trade-offs behind it.
  * **Workspace advisor** -- ``assess.review`` reads a generated MinusAnalyst project
    and/or scans a data directory and emits a system-design review (``report`` -> MD/JSON).
  * **Interview coach** -- ``coach`` walks the 6 steps as a whiteboard drill and scores
    a candidate answer against the framework.

Workspace-agnostic and domain-free: technologies are named as category examples,
never as the only valid choice. Depends only on its sibling ``minus`` package (for
reading a generated project), never on ``core``.
"""

from minus_dataops.assess import DataOpsReview, review
from minus_dataops.decisions import Decision, decide, rule_keys
from minus_dataops.depth import available as deep_available
from minus_dataops.depth import deep_steps, get_deep
from minus_dataops.framework import FRAMEWORK, get_step, steps
from minus_dataops.report import to_json, to_markdown
from minus_dataops.volume import back_of_envelope, estimate_from_path

__all__ = [
    "FRAMEWORK", "steps", "get_step",
    "Decision", "decide", "rule_keys",
    "DataOpsReview", "review",
    "to_markdown", "to_json",
    "back_of_envelope", "estimate_from_path",
    "deep_steps", "get_deep", "deep_available",
]
