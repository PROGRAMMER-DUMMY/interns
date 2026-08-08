"""Blueprint decision engine: evaluate the decision tables against measured
facts and recorded intake answers, and record which rule fired.

The tables in ``core/blueprint/tables/*.yaml`` are the asset; this module is the
boring evaluator around them. Three properties it exists to guarantee:

* **Nothing is guessed.** A rule that needs a fact the workspace does not have
  BLOCKS its decision and names the missing fact. The predecessor
  (``core/onboarding/kpi/engine_recommender.py``) fabricated a size as
  ``rows * cols * 16`` when bytes were unknown, which silently decided the tier.
  A missing measurement becomes a named intake question here, never a number.
* **Every choice is attackable.** Each decision carries ``rule_id``, the
  ``evidence`` (each fact, its value, and where it came from), the rule's
  rationale, and the vendor URL the threshold came from. A reviewer can disagree
  with a specific sentence.
* **Alternatives are priced.** Each rule declares what else was on the table and
  what choosing it would cost, so the single confirmation is informed.

Condition evaluation is three-valued (true / false / unknown). ``AND`` with one
false branch is false even if another branch is unknown -- the rule simply does
not match and evaluation moves on. ``AND`` with an unknown branch and no false
branch is unknown, and unknown blocks. That is what keeps "we did not measure
it" from being read as "it is not true".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from core.intake.discovery import implied_lane
from core.intake.interview import aggregate_arrival_pattern
from core.onboarding.panel_contract import attach_stage_routing
from core.storage.workspace_layout import WorkspaceLayout

DECISIONS_VERSION = 1
DECISIONS_ARTIFACT = "blueprint_decisions.json"
# STAGE_ROUTING key (core/onboarding/workspace/delegation.py) the lane choice belongs to.
VELOCITY_ROUTING_STAGE = "velocity_lane_choice"

TABLES_DIR = Path(__file__).resolve().parent / "tables"
TABLE_FILES = {
    "engine": "engine.yaml",
    "compute": "compute.yaml",
    "velocity": "velocity.yaml",
    "modeling": "modeling.yaml",
    "dq": "dq_placement.yaml",
}

# Connectors that expose checkpointable progress (file notification or offsets),
# which is what a continuously running job needs to resume without replaying.
_CHECKPOINTABLE_CONNECTORS = {"s3", "adls", "gcs", "kafka"}

SOURCE_MEASURED = "measured"
SOURCE_ASKED = "asked"
SOURCE_DEFAULT = "default"

STATUS_DECIDED = "decided"
STATUS_BLOCKED = "blocked"

# Connector types that mean "no cloud plane at all". Everything else (s3, adls,
# gcs, jdbc, sftp, kafka, unity_catalog, ...) is a cloud workspace.
_LOCAL_CONNECTORS = {"local_files", "local", "file", "files", "filesystem"}

_TRUE_WORDS = {"yes", "y", "true", "t", "1"}
_FALSE_WORDS = {"no", "n", "false", "f", "0"}


@dataclass(frozen=True)
class Fact:
    """One input to the tables, with where it came from."""

    name: str
    value: Any
    source: str  # measured | asked | default
    origin: str  # human-readable provenance, e.g. "discovery.working_set_estimate_bytes"

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "source": self.source, "origin": self.origin}


@dataclass
class Decision:
    decision: str
    choice: str | None
    rule_id: str
    evidence: list[str] = field(default_factory=list)
    source: str = SOURCE_DEFAULT
    alternatives: list[dict[str, str]] = field(default_factory=list)
    status: str = STATUS_DECIDED
    missing_facts: list[str] = field(default_factory=list)
    rule_summary: str = ""
    citation: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "choice": self.choice,
            "rule_id": self.rule_id,
            "evidence": list(self.evidence),
            "source": self.source,
            "alternatives": [dict(a) for a in self.alternatives],
            "status": self.status,
            "missing_facts": list(self.missing_facts),
            "rule_summary": self.rule_summary,
            "citation": self.citation,
            "notes": list(self.notes),
        }


@dataclass
class DecisionsResult:
    decisions_path: str
    decided_count: int
    blocked_count: int
    missing_facts: list[str]
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return {
            "decisions_path": self.decisions_path,
            "decided_count": self.decided_count,
            "blocked_count": self.blocked_count,
            "missing_facts": list(self.missing_facts),
            "ok": self.ok,
        }


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------


@lru_cache(maxsize=None)
def load_table(name: str) -> dict[str, Any]:
    """Load one decision table by name (``engine``/``compute``/``modeling``/``dq``)."""
    filename = TABLE_FILES.get(name)
    if filename is None:
        raise KeyError(f"unknown decision table {name!r}; known: {sorted(TABLE_FILES)}")
    data = yaml.safe_load((TABLES_DIR / filename).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"decision table {filename} did not parse as a mapping")
    return data


def load_tables() -> dict[str, dict[str, Any]]:
    return {name: load_table(name) for name in TABLE_FILES}


# --------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------


def _normalize(value: Any) -> Any:
    """Map yes/no/true/false answers onto booleans; leave everything else alone."""
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE_WORDS:
            return True
        if low in _FALSE_WORDS:
            return False
        return low
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def _answer(intake: dict[str, Any], question_id: str) -> Any:
    entry = intake.get(question_id)
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get("answer")
    return entry


def _bytes_human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _join_complexity(depth: int) -> str:
    if depth >= 3:
        return "heavy"
    if depth == 2:
        return "moderate"
    return "simple"


# --------------------------------------------------------------------------
# intake bridge
# --------------------------------------------------------------------------
#
# The interview (``core/intake/interview.py``) owns the question ids and the
# option ids a user may answer with. The tables here are written in terms of
# decision facts. This map is the only place the two vocabularies meet.
#
# A translator returns ``None`` when the recorded answer is not one it can read
# (unrecognised option id, unparseable free text). ``None`` means the fact is
# ABSENT, which blocks whichever rule needs it -- never a coerced default. If
# the interview's option ids drift, decisions block loudly instead of silently
# flipping to the wrong branch.

_FRESHNESS = {"sub_minute", "minutes", "hourly", "daily", "weekly_or_monthly"}
_BUDGET = {"serverless_ok", "cost_capped", "network_isolated"}
_RESUMABILITY = {"full_rerun_ok", "must_resume"}
_RESTATEMENT = {"append_only", "restate_within_window", "frozen_after_close"}
# `target_latency` option id -> the lane fact the velocity table reads.
_VELOCITY_LANES = {
    "batch_daily": "batch",
    "micro_batch_hourly": "micro_batch",
    "streaming_continuous": "streaming",
    "realtime_serving": "realtime_serving",
}
_CONSUMERS = {
    "exec_reporting",
    "self_serve_analyst",
    "bi_dashboard",
    "ml_features",
    "operational_app",
    "regulatory",
    "external_share",
}


def _one_of(allowed: set[str]):
    return lambda answer: answer if answer in allowed else None


def _multi_select(allowed: set[str]):
    """`multi_select` answers are persisted as a comma-joined option-id string."""

    def parse(answer: Any) -> list[str] | None:
        parts = [p.strip() for p in str(answer or "").split(",") if p.strip()]
        chosen = [p for p in parts if p in allowed]
        return chosen or None

    return parse


def _query_shape(answer: Any) -> str | None:
    """`hot_filters` is free text, but the question asks the user in so many
    words whether the filter set is "stable or exploratory". Reading back the
    word they typed is not an inference; anything else leaves the fact absent.
    """
    # ponytail: keyword read of the user's own wording. If this proves too
    # brittle, make hot_filters a select in the interview -- do not add parsing.
    text = str(answer or "").lower()
    stable, exploratory = "stable" in text, "explorator" in text
    if stable == exploratory:
        return None
    return "stable" if stable else "exploratory"


def _first_int(answer: Any) -> int | None:
    if isinstance(answer, bool):
        return None
    if isinstance(answer, int):
        return answer
    for token in re.findall(r"\d+", str(answer or "")):
        return int(token)
    return None


# intake question id -> the decision facts it feeds.
INTAKE_FACT_BRIDGE: dict[str, tuple[tuple[str, Any], ...]] = {
    "consumers": (("consumer_classes", _multi_select(_CONSUMERS)),),
    "freshness_sla": (("latency_sla", _one_of(_FRESHNESS)),),
    "target_latency": (("target_latency", lambda a: _VELOCITY_LANES.get(a)),),
    "calendar_and_restatement": (("restatement_mode", _one_of(_RESTATEMENT)),),
    "as_it_was_then": (
        ("scd2_required", lambda a: {"as_it_was_then": True, "as_it_is_now": False}.get(a)),
    ),
    "hot_filters": (("query_shape", _query_shape),),
    "concurrency": (("peak_concurrent_queries", _first_int),),
    "lineage_reproducibility": (
        ("lineage_required", lambda a: {"required": True, "not_required": False}.get(a)),
    ),
    "compute_budget_constraints": (
        # A hard cost ceiling is a preference for classic compute, not a ban on
        # serverless; only network isolation actually forbids it.
        ("serverless_allowed", lambda a: a != "network_isolated" if a in _BUDGET else None),
        ("budget_posture", _one_of(_BUDGET)),
    ),
    "non_sql_logic": (
        ("non_sql_logic", lambda a: {"non_sql_needed": True, "sql_only": False}.get(a)),
    ),
    "failure_tolerance": (("resumability", _one_of(_RESUMABILITY)),),
}

# Intake questions with no fact here (`decisions_driven`, `delivery_surface`,
# `owner_and_operations`, `grain_sentence`,
# `history_backfill_retention`, `volume_and_growth`, `pii_regulated`) feed other
# stages -- the blueprint header, dbt exposures, the PHI gate, backfill depth.
# `volume_and_growth` is free text and deliberately NOT parsed into bytes: a
# prose size turned into a number is the fabricated size this slice deleted.


def build_facts(
    *,
    discovery: dict[str, Any] | None = None,
    intake_answers: dict[str, Any] | None = None,
    kpi_summary: dict[str, Any] | None = None,
    source_declaration: dict[str, Any] | None = None,
) -> dict[str, Fact]:
    """Derive the fact set the tables evaluate against.

    Measured facts win over asked ones for the same name: if discovery measured
    it, nobody should have been asked. A fact that cannot be derived is simply
    absent -- absence is what blocks a decision, so it must never be filled in
    with a placeholder.
    """
    discovery = discovery or {}
    intake_answers = intake_answers or {}
    kpi_summary = kpi_summary or {}
    source_declaration = source_declaration or {}
    facts: dict[str, Fact] = {}

    def put(name: str, value: Any, source: str, origin: str) -> None:
        if value is None:
            return
        facts[name] = Fact(name=name, value=_normalize(value), source=source, origin=origin)

    # --- asked (intake answers, translated by the bridge) -----------------
    for question_id, targets in INTAKE_FACT_BRIDGE.items():
        answer = _answer(intake_answers, question_id)
        if answer is None:
            continue
        for fact_name, translate in targets:
            put(fact_name, translate(answer), SOURCE_ASKED, f"intake_answers.{question_id}")
    classes = facts.get("consumer_classes")
    if classes is not None and isinstance(classes.value, list):
        put(
            "consumer_class_count",
            len(classes.value),
            SOURCE_ASKED,
            "derived from intake_answers.consumers",
        )

    # --- measured (source declaration) ------------------------------------
    connector = str(source_declaration.get("type") or discovery.get("connector") or "").strip().lower()
    if connector:
        put(
            "is_cloud_workspace",
            connector not in _LOCAL_CONNECTORS,
            SOURCE_MEASURED,
            f"source_declaration.type = {connector}",
        )
        put(
            "checkpointable_source",
            connector in _CHECKPOINTABLE_CONNECTORS,
            SOURCE_MEASURED,
            f"source_declaration.type = {connector}",
        )

    # --- measured (discovery scan) ----------------------------------------
    tables = [t for t in (discovery.get("tables") or []) if isinstance(t, dict)]
    if tables:
        put("table_count", len(tables), SOURCE_MEASURED, "discovery.tables")
        sizes = [t.get("size_bytes") for t in tables]
        if sizes and all(isinstance(s, (int, float)) for s in sizes):
            put(
                "largest_table_bytes",
                int(max(sizes)),
                SOURCE_MEASURED,
                "max(discovery.tables[].size_bytes)",
            )
        # A partially-sized table list cannot name the largest input; leaving the
        # fact absent is the whole point (no `rows * cols * 16`).
        arrival = aggregate_arrival_pattern(discovery)
        put("arrival_pattern", arrival, SOURCE_MEASURED, "discovery.tables[].arrival_pattern")
        if arrival != "unknown":
            is_continuous = arrival == "continuous"
            origin = f"measured arrival_pattern = {arrival}"
            put("has_streaming_source", is_continuous, SOURCE_MEASURED, origin)
            # A continuously-arriving source IS event-stream-shaped; the modeling
            # table's R6 asks nothing more, and there is no intake question for it.
            put("event_shaped", is_continuous, SOURCE_MEASURED, origin)
    working_set = discovery.get("working_set_estimate_bytes")
    if isinstance(working_set, (int, float)):
        put(
            "working_set_bytes",
            int(working_set),
            SOURCE_MEASURED,
            "discovery.working_set_estimate_bytes",
        )

    # The velocity lane: the answer wins, and where the interview SKIPPED the
    # question (measured arrival pattern coherent with the stated SLA) the same
    # implication that justified the skip supplies the fact. Neither one -> absent
    # -> the velocity table blocks naming `target_latency`.
    if "target_latency" not in facts:
        arrival_fact, sla_fact = facts.get("arrival_pattern"), facts.get("latency_sla")
        implied = implied_lane(
            str(arrival_fact.value) if arrival_fact is not None else "",
            str(sla_fact.value) if sla_fact is not None else "",
        )
        put(
            "target_latency",
            implied,
            SOURCE_ASKED,
            "implied by measured arrival_pattern + intake_answers.freshness_sla",
        )

    # --- measured (KPI registry summary) ----------------------------------
    kpis = [k for k in (kpi_summary.get("kpis") or []) if isinstance(k, dict)]
    if kpis:
        put("kpi_count", len(kpis), SOURCE_MEASURED, "kpi_registry.kpis")
    depth = kpi_summary.get("max_join_depth")
    if isinstance(depth, int):
        put(
            "join_complexity",
            _join_complexity(depth),
            SOURCE_MEASURED,
            f"max join depth {depth} across KPIs",
        )
    return facts


# --------------------------------------------------------------------------
# condition evaluation (three-valued)
# --------------------------------------------------------------------------


def _compare(op: str, left: Any, right: Any) -> bool:
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "in":
        return left in (right or [])
    if op == "not_in":
        return left not in (right or [])
    if op == "contains":
        return right in (left or [])
    raise ValueError(f"unsupported condition operator {op!r}")


def _eval_leaf(name: str, spec: Any, facts: dict[str, Fact]) -> tuple[bool | None, list[str]]:
    fact = facts.get(name)
    if fact is None:
        return None, [name]
    if not isinstance(spec, dict):
        spec = {"eq": spec}
    for op, expected in spec.items():
        if not _compare(op, fact.value, _normalize(expected)):
            return False, []
    return True, []


def eval_condition(cond: Any, facts: dict[str, Fact]) -> tuple[bool | None, list[str]]:
    """Return ``(True|False|None, missing_fact_names)``. ``None`` means unknown."""
    if not cond:
        return True, []
    missing: list[str] = []
    unknown = False
    for key, spec in cond.items():
        if key == "any_of":
            results = [eval_condition(branch, facts) for branch in (spec or [])]
            if any(r[0] is True for r in results):
                continue
            if all(r[0] is False for r in results):
                return False, []
            unknown = True
            for _, names in results:
                missing.extend(names)
            continue
        result, names = _eval_leaf(key, spec, facts)
        if result is False:
            return False, []
        if result is None:
            unknown = True
            missing.extend(names)
    if unknown:
        return None, sorted(dict.fromkeys(missing))
    return True, []


def _condition_facts(cond: Any) -> list[str]:
    names: list[str] = []
    for key, spec in (cond or {}).items():
        if key == "any_of":
            for branch in spec or []:
                names.extend(_condition_facts(branch))
        else:
            names.append(key)
    return list(dict.fromkeys(names))


# --------------------------------------------------------------------------
# rule -> decision
# --------------------------------------------------------------------------


def _summarize(text: str) -> str:
    return " ".join(str(text or "").split())


def _decision_from_rule(
    decision: str, rule: dict[str, Any], facts: dict[str, Fact], table: dict[str, Any]
) -> Decision:
    used = _condition_facts(rule.get("when"))
    evidence = [
        f"{name} = {_render_value(name, facts[name].value)} "
        f"({facts[name].source}: {facts[name].origin})"
        for name in used
        if name in facts
    ]
    rationale = _summarize(rule.get("rationale", ""))
    if rationale:
        evidence.append(f"rule {rule.get('id')}: {rationale}")
    citation = str(rule.get("citation") or "")
    if citation:
        evidence.append(f"threshold source: {citation}")

    sources = {facts[name].source for name in used if name in facts}
    if not sources:
        source = SOURCE_DEFAULT
    elif SOURCE_ASKED in sources:
        source = SOURCE_ASKED
    else:
        source = SOURCE_MEASURED

    notes = [_summarize(n) for n in (rule.get("notes") or [])]
    choice = str(rule.get("choice") or "")
    for constraint in table.get("constraints") or []:
        if choice in (constraint.get("applies_to") or []):
            notes.append(f"{constraint.get('id')}: {_summarize(constraint.get('note', ''))}")
    for key in ("layer", "severity", "generated_as"):
        if rule.get(key):
            notes.append(f"{key}: {_summarize(rule[key])}")
    if rule.get("checks"):
        notes.append("checks: " + ", ".join(str(c) for c in rule["checks"]))

    return Decision(
        decision=decision,
        choice=choice,
        rule_id=str(rule.get("id") or ""),
        evidence=evidence,
        source=source,
        alternatives=[
            {"choice": str(a.get("choice") or ""), "cost_note": _summarize(a.get("cost_note", ""))}
            for a in (rule.get("alternatives") or [])
        ],
        rule_summary=rationale,
        citation=citation,
        notes=notes,
    )


def _render_value(name: str, value: Any) -> str:
    if name.endswith("_bytes") and isinstance(value, int):
        return f"{value} ({_bytes_human(value)})"
    return json.dumps(value) if isinstance(value, (list, dict)) else str(value)


def _blocked(decision: str, rule: dict[str, Any], missing: list[str]) -> Decision:
    names = ", ".join(missing)
    return Decision(
        decision=decision,
        choice=None,
        rule_id=str(rule.get("id") or ""),
        evidence=[
            f"rule {rule.get('id')} cannot be evaluated: no value for {names}.",
            "Nothing was assumed. Measure it in discovery or answer it in the intake "
            "interview, then re-run the blueprint.",
        ],
        source=SOURCE_DEFAULT,
        status=STATUS_BLOCKED,
        missing_facts=list(missing),
        rule_summary=_summarize(rule.get("rationale", "")),
        citation=str(rule.get("citation") or ""),
    )


def _decisive_fallback(
    decision_name: str,
    later_rules: list[dict[str, Any]],
    facts: dict[str, Fact],
    table: dict[str, Any],
) -> Decision | None:
    """The decision a first-match table reaches regardless of one unknown.

    Returns a Decision only when EVERY later rule that can be evaluated on
    known facts agrees on the same choice, and at least one of them fires. Any
    disagreement -- or another unknown -- means the missing fact really is
    load-bearing, so the caller blocks instead.
    """
    firing: list[dict[str, Any]] = []
    for rule in later_rules:
        result, _ = eval_condition(rule.get("when"), facts)
        if result is None:
            return None  # a second unknown: the outcome is genuinely undetermined
        if result is True:
            firing.append(rule)
    if not firing:
        return None
    choices = {str(rule.get("choice") or "") for rule in firing}
    if len(choices) != 1:
        return None
    decision = _decision_from_rule(decision_name, firing[0], facts, table)
    return replace(
        decision,
        evidence=list(decision.evidence)
        + [
            "an earlier rule needed a fact nobody has measured yet, but every "
            "later rule that applies agrees on this choice, so the missing fact "
            "cannot change it -- decided rather than blocked",
        ],
    )


def evaluate_table(
    name: str, facts: dict[str, Fact], table: dict[str, Any] | None = None
) -> list[Decision]:
    """Evaluate one table. ``first_match`` stops at the first matching rule;
    ``all`` emits every matching rule (placement, not selection)."""
    table = table if table is not None else load_table(name)
    decision_name = str(table.get("decision") or name)
    mode = str(table.get("mode") or "first_match")
    out: list[Decision] = []

    rules = list(table.get("rules") or [])
    for index, rule in enumerate(rules):
        result, missing = eval_condition(rule.get("when"), facts)
        if result is None:
            # An unknown only blocks when it could CHANGE the answer. In a
            # first-match table, if every later rule that fires on fully-known
            # facts yields the same choice, then resolving this fact either way
            # lands in the same place -- so blocking asks the operator for a
            # measurement that cannot matter. Found live: `join_complexity` was
            # unmeasurable until data landed, but landing data needed a confirmed
            # blueprint, which needed this decision -- a real deadlock, on a fact
            # where two later rules already agreed on `sql_dbt_warehouse`.
            # `hard_block: true` marks a CAPABILITY refusal rather than a
            # measurement gap -- the fact is unknown because the platform cannot
            # do the thing at all (e.g. no online-store serving edge). Falling
            # back past one would quietly plan a pipeline that cannot be built.
            fallback = (
                None
                if rule.get("hard_block")
                else _decisive_fallback(decision_name, rules[index + 1:], facts, table)
            )
            if mode == "first_match" and fallback is not None:
                out.append(fallback)
                break
            out.append(_blocked(decision_name, rule, missing))
            if mode == "first_match":
                return out
            continue
        if result is False:
            continue
        out.append(_decision_from_rule(decision_name, rule, facts, table))
        if mode == "first_match":
            break

    for rule in table.get("modifiers") or []:
        result, missing = eval_condition(rule.get("when"), facts)
        if result is None:
            out.append(_blocked(decision_name, rule, missing))
        elif result is True:
            out.append(_decision_from_rule(decision_name, rule, facts, table))
    return out


def evaluate_all(facts: dict[str, Fact]) -> list[Decision]:
    """Engine first: the compute table reads the chosen engine as a fact."""
    decisions = evaluate_table("engine", facts)
    chosen = next(
        (d for d in decisions if d.decision == "engine" and d.status == STATUS_DECIDED), None
    )
    compute_facts = dict(facts)
    if chosen is not None and chosen.choice:
        compute_facts["engine"] = Fact(
            name="engine",
            value=chosen.choice,
            source=SOURCE_MEASURED,
            origin=f"engine decision {chosen.rule_id}",
        )
    decisions += evaluate_table("compute", compute_facts)

    # Velocity before modeling: the lane is what tells R10 whether facts can
    # arrive before their dimension members.
    velocity = evaluate_table("velocity", facts)
    decisions += velocity
    modeling_facts = dict(facts)
    lane = next(
        (d for d in velocity if d.decision == "velocity_lane" and d.status == STATUS_DECIDED), None
    )
    if lane is not None and lane.choice:
        modeling_facts["velocity_lane"] = Fact(
            name="velocity_lane",
            value=lane.choice,
            source=SOURCE_MEASURED,
            origin=f"velocity decision {lane.rule_id}",
        )
    decisions += evaluate_table("modeling", modeling_facts)
    decisions += evaluate_table("dq", facts)
    return decisions


# --------------------------------------------------------------------------
# artifact
# --------------------------------------------------------------------------


def build_payload(
    *,
    workspace: str,
    discovery: dict[str, Any] | None = None,
    intake_answers: dict[str, Any] | None = None,
    kpi_summary: dict[str, Any] | None = None,
    source_declaration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    facts = build_facts(
        discovery=discovery,
        intake_answers=intake_answers,
        kpi_summary=kpi_summary,
        source_declaration=source_declaration,
    )
    decisions = evaluate_all(facts)
    blocked = [d for d in decisions if d.status == STATUS_BLOCKED]
    missing = sorted({m for d in blocked for m in d.missing_facts})
    entries = [d.to_dict() for d in decisions]
    # The lane is a user-facing choice, not just a table result: route its own
    # roster onto the entry so `velocity_lane_choice` reaches the orchestrator.
    for entry in entries:
        if entry.get("decision") == "velocity_lane":
            attach_stage_routing(entry, VELOCITY_ROUTING_STAGE)
    return {
        "artifact_type": DECISIONS_ARTIFACT,
        "version": DECISIONS_VERSION,
        "generated_by": "prepare-blueprint",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(workspace),
        "facts": {name: fact.to_dict() for name, fact in sorted(facts.items())},
        "decisions": entries,
        "blocked_facts": missing,
        "revisit_triggers": load_table("compute").get("revisit_triggers") or [],
    }


def load_workspace_inputs(layout: WorkspaceLayout) -> dict[str, Any]:
    """Read the Slice-1 intake artifacts. Absent inputs stay absent."""
    intake_dir = layout.generated_dir / "intake"
    settings = layout.load_settings()
    kpi_registry = _read_json(layout.kpi_registry_path)
    kpis = [k for k in (kpi_registry.get("kpis") or []) if isinstance(k, dict)]
    join_depth = _max_join_depth(_read_json(layout.kpi_feature_mapping_path))
    return {
        "discovery": _read_json(intake_dir / "discovery.json"),
        "intake_answers": _read_json(intake_dir / "intake_answers.json"),
        "kpi_summary": {
            "kpis": [
                {
                    "kpi_id": str(k.get("kpi_id") or ""),
                    "name": str(k.get("name") or ""),
                    "source_tables": [
                        str(t) for t in (k.get("source_tables") or []) if isinstance(t, str)
                    ],
                }
                for k in kpis
            ],
            **({"max_join_depth": join_depth} if join_depth is not None else {}),
        },
        "source_declaration": settings.get("source_declaration") or {},
    }


def _max_join_depth(mapping: dict[str, Any]) -> int | None:
    """Deepest join across the workspace's KPIs, from the feature mapping.

    One engine serves every KPI, so the heaviest KPI sets the shape (research
    Q2). Absent mapping -> ``None`` -> the fact is absent -> the rule that needs
    it blocks and names it, which resolves by running feature resolution.
    """
    kpis = [k for k in (mapping.get("kpis") or []) if isinstance(k, dict)]
    if not kpis:
        return None
    depths = []
    for kpi in kpis:
        datasets = {
            # ponytail: basename match, so two same-named files in different
            # folders count once. This feeds a simple/moderate/heavy tier, not a
            # number; use the sql_generator ref resolver if that ever matters.
            Path(str(source.get("dataset") or "")).name
            for feature in (kpi.get("features") or [])
            if isinstance(feature, dict)
            for source in (feature.get("source_columns") or [])
            if isinstance(source, dict) and source.get("dataset")
        }
        depths.append(max(0, len(datasets) - 1))
    return max(depths) if depths else None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def write_decisions(repo_root: str | Path, workspace: str | Path) -> DecisionsResult:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())
    layout.ensure_runtime_dirs()
    payload = build_payload(workspace=str(workspace), **load_workspace_inputs(layout))
    path = layout.contracts_dir / DECISIONS_ARTIFACT
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    decided = sum(1 for d in payload["decisions"] if d["status"] == STATUS_DECIDED)
    blocked = sum(1 for d in payload["decisions"] if d["status"] == STATUS_BLOCKED)
    return DecisionsResult(
        decisions_path=_rel(path, repo_root),
        decided_count=decided,
        blocked_count=blocked,
        missing_facts=list(payload["blocked_facts"]),
        ok=blocked == 0,
    )


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "DECISIONS_ARTIFACT",
    "DECISIONS_VERSION",
    "Decision",
    "DecisionsResult",
    "Fact",
    "build_facts",
    "build_payload",
    "eval_condition",
    "evaluate_all",
    "evaluate_table",
    "load_table",
    "load_tables",
    "load_workspace_inputs",
    "write_decisions",
]
