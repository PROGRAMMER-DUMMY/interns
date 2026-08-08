"""Phase 1: one intake interview, asked once per workspace.

Two research reports each proposed their own question list -- `end_users_data_
modeling_research.md` (14, consumer/modeling) and `engine_compute_selection_
research.md` (12, engine/compute). Asked back-to-back that is 26 questions and
a user who stops answering. They overlap heavily (both ask freshness, both ask
volume/growth, both ask ownership), so they are merged here into ONE
deduplicated set of 17, each question recording which report(s) it came from.

Two rules from the reports are enforced in code, not prose:

* **Measured beats asked.** A question the discovery scan already answered is
  marked `skipped_measured` and never shown. Today that is the volume question,
  whenever ``discovery.json`` carries a real ``working_set_estimate_bytes``.
* **Ask once, reuse everywhere.** Answers land in a durable workspace fact
  file, not in a per-KPI prompt.

The panel mirrors the KPI blocker panel's envelope (``current.json`` is the
machine contract, ``current.md`` is what a human reads, options carry ids and
one is recommended) so the orchestrator needs no new display mode.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.contracts.versioning import register_contract
from core.governance.provenance import decision_source as decision_source_for
from core.intake.discovery import ARRIVAL_UNKNOWN, implied_lane, load_discovery
from core.intake.domain_detect import (
    UNKNOWN_DOMAIN,
    DomainSignal,
    detect_domain,
    domain_labels,
    load_vocabulary,
    overlay_for,
)
from core.onboarding.panel_contract import attach_stage_routing
from core.onboarding.sources.external_discovery import DOC_SUFFIXES
from core.storage.workspace_layout import WorkspaceLayout

PANEL_VERSION = 1
ANSWERS_VERSION = 1
# STAGE_ROUTING key (core/onboarding/workspace/delegation.py) this panel belongs to.
ROUTING_STAGE = "intake_interview"
register_contract("intake_panel/current.json", current_version=PANEL_VERSION)

# `answerable_from` predicates a question can declare. The field is data so a new
# measurement can retire a question without touching the panel code.
DISCOVERY_PREDICATE_WORKING_SET = "working_set_measured"
DISCOVERY_PREDICATE_ARRIVAL_LANE = "arrival_pattern_and_sla_coherent"


def _opts(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"option_id": option_id, "label": label} for option_id, label in pairs]


DOMAIN_QUESTION_ID = "domain_confirm"
# The escape hatch, so an estate the vocabulary has never seen is still
# answerable. Never recommended -- it retunes nothing.
DOMAIN_OTHER_OPTION = "other"


def _domain_options() -> list[dict[str, str]]:
    """Every domain the vocabulary knows, plus `other`. Option ids are the
    vocabulary slugs, so adding a domain is a data edit, not a code edit."""
    return _opts(
        *sorted(domain_labels().items()),
        (DOMAIN_OTHER_OPTION, "Something else, or more than one -- say which in the notes"),
    )


# id, text, why_it_matters, answer_type, options, recommended_option_id,
# maps_to (the decision fields this feeds), merged_from (research provenance).
# Every option-backed question carries a recommendation and every free-text one
# carries a `suggested_answer`: an intake that asks 18 questions and defends none
# of them is an interrogation, not a recommendation engine. `domain_confirm` is
# the single exception, and only because its recommendation is EVIDENCE -- it is
# computed at panel time and stays blank when nothing matched.
QUESTIONS: list[dict[str, Any]] = [
    {
        "id": DOMAIN_QUESTION_ID,
        "text": "Which business domain is this workspace's data from?",
        "why_it_matters": (
            "The platform does not know which enterprise team it is talking to, and "
            "guessing is worse than asking: what counts as regulated, whether "
            "published numbers get restated, and whether lineage is mandatory all "
            "move with the domain. Answered first because it retunes the defaults "
            "of the questions below."
        ),
        "answer_type": "select",
        "options": _domain_options(),
        # Computed at panel time from the evidence -- see `_domain_entry`.
        "recommended_option_id": "",
        "recommended_at_panel_time": True,
        "maps_to": ["intake.domain", "governance.regime", "panel.defaults"],
        "merged_from": ["owner:domain_first"],
    },
    {
        "id": "consumers",
        "text": "Who consumes this workspace's output? Pick every class that applies.",
        "why_it_matters": (
            "Consumer class -- not data volume -- sets freshness, grain, serving "
            "surface and access model. Two or more classes is the default trigger "
            "for a conformed star rather than a one-off wide table. Recommended: "
            "a dashboard with known query shapes -- the narrowest class that still "
            "serves a real reader, so a second class is added when it actually "
            "exists instead of paying star-schema cost for a consumer nobody named."
        ),
        "answer_type": "multi_select",
        "options": _opts(
            ("exec_reporting", "Executive / fixed reporting packs"),
            ("self_serve_analyst", "Self-serve analysts writing their own queries"),
            ("bi_dashboard", "A dashboard with fixed, known query shapes"),
            ("ml_features", "Model training / feature consumption"),
            ("operational_app", "An operational app or reverse-ETL sync"),
            ("regulatory", "Regulatory or compliance reporting"),
            ("external_share", "An external party consuming a shared data product"),
        ),
        "recommended_option_id": "bi_dashboard",
        "maps_to": ["modeling.technique", "serving.surface", "dq.criticality_tier"],
        "merged_from": ["end_users:Q1"],
    },
    {
        "id": "decisions_driven",
        "text": "For each consumer above: what decision does the number drive, and what would they do differently if it moved?",
        "why_it_matters": (
            "A consumer who cannot name a decision does not need a pipeline. This "
            "is also the anchor that makes a freshness SLA arguable rather than a "
            "preference."
        ),
        "answer_type": "text",
        "options": [],
        "recommended_option_id": "",
        "suggested_answer": (
            "<consumer> reviews this <daily/weekly>; if it moved by more than "
            "<threshold> they would <the action they would take>. Nothing else "
            "changes today's decision."
        ),
        "maps_to": ["dq.criticality_tier", "compute.latency_class", "blueprint.header"],
        "merged_from": ["end_users:Q2", "engine:Q1"],
    },
    {
        "id": "delivery_surface",
        "text": "How is the output delivered to each consumer?",
        "why_it_matters": (
            "This is the dbt `exposure.type`, so it is recorded as first-class "
            "lineage metadata rather than as a comment."
        ),
        "answer_type": "multi_select",
        "options": _opts(
            ("dashboard", "Dashboard"),
            ("notebook", "Notebook"),
            ("analysis", "Ad-hoc analysis"),
            ("ml", "Model training / inference"),
            ("application", "Application or API"),
        ),
        "recommended_option_id": "dashboard",
        "maps_to": ["serving.surface", "dbt.exposures"],
        "merged_from": ["end_users:Q3"],
    },
    {
        "id": "owner_and_operations",
        "text": "Who owns this output, who is paged when it is wrong or late, and what tooling does that team already run?",
        "why_it_matters": (
            "Unowned assets rot. The second half matters too: the engine that wins "
            "a benchmark can still lose on operations if nobody on the receiving "
            "team runs it."
        ),
        "answer_type": "text",
        "options": [],
        "recommended_option_id": "",
        "suggested_answer": (
            "Owner: <named person or team, not a distribution list>. Paged when it "
            "is wrong or late: <the same team, in <tool>>. That team already runs "
            "<warehouse / orchestrator / BI tool>, so prefer a design they can "
            "operate over one that only benchmarks better."
        ),
        "maps_to": ["ownership.owner", "ownership.on_call", "engine.operability"],
        "merged_from": ["end_users:Q4", "engine:Q10"],
    },
    {
        "id": "freshness_sla",
        "text": "How stale can the number be before the decision it drives goes wrong? Say what breaks at 15 minutes and at 24 hours.",
        "why_it_matters": (
            "Latency class is a separate axis from size, and it is the single "
            "biggest cost lever. 'Real-time' without a named decision and a named "
            "cost of delay is not an SLA."
        ),
        "answer_type": "select",
        "options": _opts(
            ("sub_minute", "Under a minute (an operational decision waits on it)"),
            ("minutes", "Minutes"),
            ("hourly", "Hourly"),
            ("daily", "Daily / as of last night"),
            ("weekly_or_monthly", "Weekly, or at a monthly close"),
        ),
        "recommended_option_id": "daily",
        "maps_to": ["compute.latency_class", "orchestration.schedule", "engine.streaming"],
        "merged_from": ["end_users:Q5", "engine:Q1", "engine:Q2"],
    },
    {
        "id": "target_latency",
        "text": "What latency class should the pipeline actually run at?",
        "why_it_matters": (
            "Velocity is a routed lane, not a setting: it decides whether marts are "
            "rebuilt or merged incrementally, whether ingestion checkpoints, and "
            "whether late-arriving dimensions need inferred members. Asked only when "
            "the measured arrival pattern and the freshness answer do not already "
            "agree on a lane -- a guessed cadence would decide all three silently."
        ),
        "answer_type": "select",
        "options": _opts(
            ("batch_daily", "Batch -- one scheduled run per day (or slower)"),
            ("micro_batch_hourly", "Micro-batch -- hourly or more often, still scheduled"),
            ("streaming_continuous", "Streaming -- a continuously running, checkpointed job"),
            ("realtime_serving", "Real-time serving -- an online store answers point lookups"),
        ),
        "recommended_option_id": "batch_daily",
        "maps_to": ["lane.velocity", "dbt.materialization", "ingestion.checkpointing"],
        "merged_from": ["engine:Q1", "engine:Q2"],
        "answerable_from": DISCOVERY_PREDICATE_ARRIVAL_LANE,
    },
    {
        "id": "calendar_and_restatement",
        "text": "Is there a reporting calendar or cutoff, and do late-arriving facts restate an already-published number?",
        "why_it_matters": (
            "Restatement decides whether the pipeline can rebuild freely or must "
            "keep an as-published snapshot. It is also the difference between a "
            "cheap append and an expensive correction path."
        ),
        "answer_type": "select",
        "options": _opts(
            ("append_only", "No cutoff; late data simply appends"),
            ("restate_within_window", "Published numbers are restated within a known window"),
            ("frozen_after_close", "Numbers are frozen after a close and never restated"),
        ),
        "recommended_option_id": "append_only",
        "maps_to": ["orchestration.backfill", "modeling.snapshot_strategy", "dq.reconciliation"],
        "merged_from": ["end_users:Q6", "end_users:Q7"],
    },
    {
        "id": "grain_sentence",
        "text": "Finish this sentence: one row of the answer is one row per ____ per ____.",
        "why_it_matters": (
            "Grain is the one declaration that keeps a model from silently "
            "double-counting later. It becomes a machine-checked uniqueness test."
        ),
        "answer_type": "text",
        "options": [],
        "recommended_option_id": "",
        "suggested_answer": "one row per <business entity> per <calendar day>",
        "maps_to": ["modeling.grain", "dq.uniqueness_test"],
        "merged_from": ["end_users:Q8"],
    },
    {
        "id": "as_it_was_then",
        "text": "Do questions need attribute values as they were at event time, or only as they are now?",
        "why_it_matters": (
            "This is the SCD2 switch and it is binary. Defaulting it to yes pays "
            "history cost forever; defaulting it to no makes some questions "
            "unanswerable after the fact."
        ),
        "answer_type": "select",
        "options": _opts(
            ("as_it_is_now", "As it is now -- current values are fine"),
            ("as_it_was_then", "As it was then -- point-in-time values are required"),
        ),
        "recommended_option_id": "as_it_is_now",
        "maps_to": ["modeling.scd2", "modeling.keys"],
        "merged_from": ["end_users:Q9"],
    },
    {
        "id": "history_backfill_retention",
        "text": "How far back must history be queryable, how far back must we be able to reprocess, and how long must raw be retained?",
        "why_it_matters": (
            "Full-refresh cost, not steady-state cost, usually sets the compute "
            "tier. Retention floors are also the one requirement a regulated "
            "consumer can veto a design over."
        ),
        "answer_type": "text",
        "options": [],
        "recommended_option_id": "",
        # Placeholders are mandatory in a suggestion: fully-formed prose is a
        # hidden default wearing a prompt's clothes -- applied verbatim it books
        # a retention commitment nobody chose. 25 months is offered because it
        # spans a full year-on-year comparison; the retention floor is whatever
        # the operator's regulator or contract imposes, which only they know.
        "suggested_answer": (
            "Queryable: <25> months (this year plus last year, so a full "
            "year-on-year comparison always fits). Reprocessable: <the same 25> "
            "months. Raw retained: <7 years, or the longest retention a regulator "
            "or contract imposes on you -- whichever is longer>."
        ),
        "maps_to": ["orchestration.backfill_depth", "storage.retention", "compute.tier"],
        "merged_from": ["end_users:Q10", "engine:Q5"],
    },
    {
        "id": "hot_filters",
        "text": "Which filters or slicers will 80% of queries use, and is that set stable or exploratory?",
        "why_it_matters": (
            "Stable and narrow justifies a projection off the star and picks the "
            "clustering keys; exploratory means the star itself has to stay "
            "flexible."
        ),
        "answer_type": "text",
        "options": [],
        "recommended_option_id": "",
        "suggested_answer": (
            "Date range on every query, plus <the one or two attributes the "
            "readers slice by>. Treat the set as stable for now; say "
            "'exploratory' instead if analysts will invent their own cuts."
        ),
        "maps_to": ["modeling.obt_projection", "storage.clustering_keys"],
        "merged_from": ["end_users:Q11"],
    },
    {
        "id": "volume_and_growth",
        "text": (
            "Roughly how large is the largest source table today, what was it 12 months ago, "
            "and is a step change coming (new source system, new region, new client)?"
        ),
        "why_it_matters": (
            "Engine selection keys on the largest input and on whether its size is "
            "STABLE. Asked only when discovery could not measure it -- a fabricated "
            "size silently decides the tier, which is worse than a missing one."
        ),
        "answer_type": "text",
        "options": [],
        "recommended_option_id": "",
        "suggested_answer": (
            "Largest table roughly <N> GB today, roughly <M> GB a year ago, and "
            "no step change is planned. Give a range you are confident in rather "
            "than a precise number you are not -- an unstated size is worse than "
            "a wide one."
        ),
        "maps_to": ["engine.working_set_bytes", "engine.size_stability", "compute.tier"],
        "merged_from": ["end_users:Q12", "engine:Q3", "engine:Q4", "engine:Q12"],
        "answerable_from": DISCOVERY_PREDICATE_WORKING_SET,
    },
    {
        "id": "concurrency",
        "text": "At peak, how many people or dashboards query this simultaneously?",
        "why_it_matters": (
            "Warehouse sizing splits cleanly here: size UP for single-query speed, "
            "add CLUSTERS for more users. Concurrency is the second one and cannot "
            "be measured before the workspace has users."
        ),
        "answer_type": "text",
        "options": [],
        "recommended_option_id": "",
        # The NUMBER is the thing only the operator knows; stating it as settled
        # prose makes a warehouse-sizing input look agreed when nobody agreed it.
        "suggested_answer": (
            "Fewer than <10> at peak (one team opening the same dashboard in the "
            "same hour) -- correct the number if a whole department or an "
            "embedded app is the reader."
        ),
        "maps_to": ["compute.cluster_count", "compute.warehouse_size"],
        "merged_from": ["end_users:Q12", "engine:Q6"],
    },
    {
        "id": "pii_regulated",
        "text": "Which columns are personal or otherwise regulated, and who must NOT see them?",
        "why_it_matters": (
            "Masking, row filters and the PHI gate are generated from this answer. "
            "An unanswered version of this question is the one that ends in a "
            "disclosure."
        ),
        "answer_type": "text",
        "options": [],
        "recommended_option_id": "",
        "suggested_answer": (
            "Any column that names, contacts or uniquely numbers a person, plus "
            "anything that becomes identifying when joined to one of those; "
            "unmasked access limited to <the team that operates on the record>, "
            "everyone else sees masked or aggregated values."
        ),
        "maps_to": ["governance.masking", "governance.row_filters", "governance.phi_gate"],
        "merged_from": ["end_users:Q13"],
    },
    {
        "id": "lineage_reproducibility",
        "text": "Is attribute-level lineage or point-in-time reproducibility required?",
        "why_it_matters": (
            "Yes forces deterministic hash keys, immutable raw retention and an "
            "effective-dated silver layer. No leaves the cheaper default in place."
        ),
        "answer_type": "select",
        "options": _opts(
            ("not_required", "Not required"),
            ("required", "Required -- every number must be traceable and reproducible as of a date"),
        ),
        "recommended_option_id": "not_required",
        "maps_to": ["modeling.technique", "modeling.keys", "storage.retention"],
        "merged_from": ["end_users:Q14"],
    },
    {
        "id": "compute_budget_constraints",
        "text": "Is there a monthly compute ceiling, is paying for idle capacity acceptable, and must compute run inside your own network?",
        "why_it_matters": (
            "Serverless-first is the default because it starts in seconds and "
            "scales to zero, but a network-isolation requirement or a long steady "
            "job flips the answer to classic compute."
        ),
        "answer_type": "select",
        "options": _opts(
            ("serverless_ok", "Serverless is fine; prefer scale-to-zero over lowest unit rate"),
            ("cost_capped", "There is a hard monthly ceiling; idle capacity is acceptable if it is cheaper"),
            ("network_isolated", "Compute must run inside our own network / subscription"),
        ),
        "recommended_option_id": "serverless_ok",
        "maps_to": ["compute.tier", "compute.serverless", "compute.budget"],
        "merged_from": ["engine:Q7", "engine:Q8"],
    },
    {
        "id": "non_sql_logic",
        "text": "Does any output need model inference, custom libraries, or parsing that SQL cannot express?",
        "why_it_matters": (
            "This is the only signal that overrides size when choosing the "
            "transform engine: non-SQL logic routes to a general-purpose engine "
            "regardless of how small the data is."
        ),
        "answer_type": "select",
        "options": _opts(
            ("sql_only", "No -- everything is expressible in SQL"),
            ("non_sql_needed", "Yes -- name what it is in the notes"),
        ),
        "recommended_option_id": "sql_only",
        "maps_to": ["engine.choice"],
        "merged_from": ["engine:Q9"],
    },
    {
        "id": "failure_tolerance",
        "text": "If a run fails at 90%, is a full re-run acceptable, or must it resume from where it stopped?",
        "why_it_matters": (
            "Resumability is what buys the heavier tier -- single-node engines fail "
            "hard rather than degrading -- and it decides whether models are "
            "batched per time window."
        ),
        "answer_type": "select",
        "options": _opts(
            ("full_rerun_ok", "A full re-run is acceptable"),
            ("must_resume", "It must resume; a full re-run does not fit the window"),
        ),
        "recommended_option_id": "full_rerun_ok",
        "maps_to": ["engine.choice", "orchestration.retry", "dbt.materialization"],
        "merged_from": ["engine:Q11"],
    },
]

# System questions are recorded like any other answer but never shown in the
# panel and never counted as pending: they are gates the flow asks for, not
# information only the user has.
SYSTEM_QUESTIONS: list[dict[str, Any]] = [
    {
        "id": "playback_confirm",
        "text": "Confirm the understanding playback in interns/reports/intake_playback/current.md.",
        "why_it_matters": (
            "The playback restates what the platform believes it was told, tagged "
            "(measured)/(you said)/(default). Confirming it is the alignment gate "
            "`prepare-blueprint` refuses to run without."
        ),
        "answer_type": "select",
        "options": _opts(("confirmed", "Yes -- that is what I meant")),
        "recommended_option_id": "confirmed",
        "maps_to": ["intake.alignment_gate"],
        "merged_from": ["system:playback"],
        "hidden": True,
    },
]

QUESTIONS_BY_ID: dict[str, dict[str, Any]] = {
    question["id"]: question for question in QUESTIONS + SYSTEM_QUESTIONS
}

PLAYBACK_CONFIRM_QUESTION = "playback_confirm"


# --------------------------------------------------------------------------
# domain awareness: detect, offer, confirm, retune
# --------------------------------------------------------------------------

# ponytail: bounded walk, not an index. A workspace is a folder of source
# documents; if one ever holds a million files, feed the doc list from discovery
# instead of raising these.
_MAX_DOCUMENT_SCAN = 200
_MAX_WALK_ENTRIES = 5000
_MAX_KPI_TERMS = 400
_SKIP_SCAN_DIRS = {"interns", "dashboard", ".git", "__pycache__", ".venv"}


def _workspace_documents(workspace: Path) -> list[str]:
    """Document FILENAMES a user dropped in the workspace -- KPI workbooks, data
    dictionaries, specs. Names only; contents are never read here."""
    found: list[str] = []
    if not workspace.exists():
        return found
    for index, path in enumerate(workspace.rglob("*")):
        if index >= _MAX_WALK_ENTRIES or len(found) >= _MAX_DOCUMENT_SCAN:
            break
        if any(part in _SKIP_SCAN_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in DOC_SUFFIXES and path.is_file():
            found.append(path.name)
    return found


def _kpi_terms(layout: WorkspaceLayout) -> list[str]:
    """Every short string in the KPI registry, if one has been built."""
    path = layout.kpi_registry_path
    if not path.exists():
        return []
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    terms: list[str] = []
    stack: list[Any] = [data]
    while stack and len(terms) < _MAX_KPI_TERMS:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and 0 < len(node) <= 200:
            terms.append(node)
    return terms


def confirmed_domain(answers: dict[str, Any]) -> str:
    """The domain the USER confirmed, or "". Detection alone never retunes a
    question: a proposal the user has not seen is still an assumption."""
    entry = answers.get(DOMAIN_QUESTION_ID)
    answer = str(entry.get("answer") or "") if isinstance(entry, dict) else str(entry or "")
    return answer if answer in load_vocabulary() else ""


def _domain_entry(entry: dict[str, Any], signal: DomainSignal) -> dict[str, Any]:
    """The domain question with the detected domain offered first and marked
    recommended -- or, when nothing matched, with no recommendation at all."""
    entry = dict(entry)
    options = list(entry["options"])
    if signal.domain in {option["option_id"] for option in options}:
        entry["options"] = [o for o in options if o["option_id"] == signal.domain] + [
            o for o in options if o["option_id"] != signal.domain
        ]
        entry["recommended_option_id"] = signal.domain
        entry["why_it_matters"] += (
            f" Best case here: `{signal.domain}` (confidence {signal.confidence:.2f}), "
            "read off the workspace's own names -- "
            + "; ".join(signal.evidence[:4])
            + ". Confirm it or pick another; the evidence is listed so you can "
            "attack it rather than trust it."
        )
    else:
        entry["recommended_option_id"] = ""
        entry["why_it_matters"] += (
            " Nothing in the discovered table names, column names, document "
            "filenames or KPI terms matched a known domain, so NOTHING is "
            "recommended here -- the full list is offered and the choice is "
            "yours. A guessed domain would retune every default below."
        )
    entry["domain_signal"] = signal.to_dict()
    return entry


def _apply_domain_overlay(
    entry: dict[str, Any], overlay: dict[str, dict[str, Any]], domain: str
) -> dict[str, Any]:
    """Retune ONE question for the confirmed domain, from the vocabulary file.

    Labels, recommendations and suggested answers move; option IDS never do --
    ``core/blueprint/decisions.py::INTAKE_FACT_BRIDGE`` translates those ids, so
    a domain that renamed one would silently break the blueprint.
    """
    tune = overlay.get(entry["id"])
    if not tune:
        return entry
    entry = dict(entry)
    options = list(entry.get("options") or [])
    labels = tune.get("option_labels") or {}
    if labels and options:
        entry["options"] = [
            {**option, "label": str(labels.get(option["option_id"], option["label"]))}
            for option in options
        ]
    recommended = str(tune.get("recommended_option_id") or "")
    if recommended in {option["option_id"] for option in options}:
        entry["recommended_option_id"] = recommended
    suggested = str(tune.get("suggested_answer") or "")
    if suggested and not options:
        entry["suggested_answer"] = suggested
    why = str(tune.get("why") or "")
    if why:
        entry["why_it_matters"] = f"{entry['why_it_matters']} {why}"
    entry["domain_tuned_for"] = domain
    return entry


@lru_cache(maxsize=1)
def _overlay_labels() -> dict[str, dict[str, str]]:
    """``{question_id: {domain-tuned label: option_id}}`` -- so an answer typed
    off a domain-tuned panel still resolves to the stable option id."""
    labels: dict[str, dict[str, str]] = {}
    for slug in load_vocabulary():
        for question_id, tune in overlay_for(slug).items():
            for option_id, label in (tune.get("option_labels") or {}).items():
                labels.setdefault(question_id, {})[str(label).strip().lower()] = str(option_id)
    return labels


@dataclass(frozen=True)
class IntakePanelResult:
    current_json_path: str
    current_markdown_path: str
    playback_markdown_path: str
    status: str
    current_question_id: str
    answered_count: int
    pending_count: int
    skipped_count: int

    def summary(self) -> dict[str, Any]:
        return attach_stage_routing(asdict(self), ROUTING_STAGE)


class IntakePanelBuilder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def prepare(self) -> IntakePanelResult:
        self.layout.ensure_runtime_dirs()
        discovery = load_discovery(self.layout)
        answers = load_intake_answers(self.layout)
        # Detection runs on whatever evidence exists; none of it is required.
        signal = detect_domain(
            discovery,
            _workspace_documents(self.workspace),
            _kpi_terms(self.layout),
        )
        domain = confirmed_domain(answers)
        overlay = overlay_for(domain)

        entries: list[dict[str, Any]] = []
        for question in QUESTIONS:
            entry = dict(question)
            if entry["id"] == DOMAIN_QUESTION_ID:
                entry = _domain_entry(entry, signal)
            elif overlay:
                entry = _apply_domain_overlay(entry, overlay, domain)
            answer = answers.get(question["id"])
            measured = _measured_evidence(question, discovery, answers)
            if answer is not None:
                entry["state"] = "answered"
                entry["answer"] = answer.get("answer", "")
                entry["answered_by"] = answer.get("answered_by", "")
            elif measured is not None:
                entry["state"] = "skipped_measured"
                entry["measured_evidence"] = measured
            else:
                entry["state"] = "pending"
            entries.append(entry)

        pending = [entry for entry in entries if entry["state"] == "pending"]
        answered = [entry for entry in entries if entry["state"] == "answered"]
        skipped = [entry for entry in entries if entry["state"] == "skipped_measured"]
        workspace_rel = _rel(self.workspace, self.repo_root)

        panel = {
            "artifact_type": "intake_panel/current.json",
            "version": PANEL_VERSION,
            "generated_by": "prepare-intake-panel",
            "workspace": workspace_rel,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "needs_user_answer" if pending else "complete",
            "current_question_id": pending[0]["id"] if pending else "",
            "answered_count": len(answered),
            "pending_count": len(pending),
            "skipped_count": len(skipped),
            "discovery_status": str(discovery.get("status") or "missing"),
            "domain": {
                "confirmed": domain,
                "detected": signal.domain,
                "confidence": signal.confidence,
                "evidence": signal.evidence,
                "alternatives": signal.alternatives,
            },
            "questions": entries,
            "apply_command": (
                f"uv run apply-intake-answer --workspace {workspace_rel} "
                "--question <question_id> --answer <option_id_or_text> "
                '--answered-by "<name>"'
            ),
        }
        attach_stage_routing(panel, ROUTING_STAGE)

        panel_dir = self.layout.reports_dir / "intake_panel"
        panel_dir.mkdir(parents=True, exist_ok=True)
        current_json = panel_dir / "current.json"
        current_md = panel_dir / "current.md"
        current_json.write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(render_markdown(panel), encoding="utf-8")

        playback_dir = self.layout.reports_dir / "intake_playback"
        playback_dir.mkdir(parents=True, exist_ok=True)
        playback_md = playback_dir / "current.md"
        playback_md.write_text(
            render_playback(workspace_rel, discovery, answers, signal=signal, domain=domain),
            encoding="utf-8",
        )
        return IntakePanelResult(
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            playback_markdown_path=_rel(playback_md, self.repo_root),
            status=str(panel["status"]),
            current_question_id=str(panel["current_question_id"]),
            answered_count=len(answered),
            pending_count=len(pending),
            skipped_count=len(skipped),
        )


class IntakeAnswerRecorder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def apply(self, question_id: str, answer: str, *, answered_by: str = "") -> dict[str, Any]:
        question = QUESTIONS_BY_ID.get(str(question_id).strip())
        if question is None:
            raise ValueError(
                f"--question must be one of {', '.join(sorted(QUESTIONS_BY_ID))}, got {question_id!r}"
            )
        normalized = _normalize_answer(question, answer)

        # A `suggested_answer` is a TEMPLATE to edit. Stored verbatim it reaches
        # the blueprint as a real requirement -- a grain of "one row per
        # <business entity>" would design a pipeline on a sentence nobody wrote.
        # Refuse it here rather than let it flow downstream looking answered.
        if _PLACEHOLDER.search(normalized):
            raise ValueError(
                f"answer for `{question['id']}` is still a template: "
                f"{_PLACEHOLDER.findall(normalized)} must be replaced with real values. "
                "The suggested answer is a starting point to edit, not an answer."
            )

        path = self.layout.generated_dir / "intake" / "intake_answers.json"
        answers = load_intake_answers(self.layout)
        previous = answers.get(question["id"])
        answers[question["id"]] = {
            "answer": normalized,
            "answered_by": answered_by.strip(),
            "source": decision_source_for(answered_by),
            "at": datetime.now(timezone.utc).isoformat(),
        }

        # Re-intake: a requirement that CHANGED after the blueprint was confirmed
        # invalidates the alignment gate. The previous "yes, that is what I meant"
        # covered the previous requirement, so the playback is re-read and
        # re-confirmed before the changed plan can be rendered.
        confirmed_blueprint = (
            self.layout.reports_dir / "solution_blueprint" / "current.confirmed.json"
        )
        invalidated = bool(
            previous is not None
            and question["id"] != PLAYBACK_CONFIRM_QUESTION
            and str(previous.get("answer") or "") != normalized
            and confirmed_blueprint.exists()
            and answers.pop(PLAYBACK_CONFIRM_QUESTION, None) is not None
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(answers, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        next_panel = IntakePanelBuilder(self.repo_root, _rel(self.workspace, self.repo_root)).prepare()
        return {
            "answers_path": _rel(path, self.repo_root),
            "question_id": question["id"],
            "answer": normalized,
            "answered_by": answered_by.strip(),
            "source": decision_source_for(answered_by),
            "maps_to": question["maps_to"],
            "playback_invalidated": invalidated,
            "next_panel": next_panel.summary(),
        }


def load_intake_answers(layout: WorkspaceLayout) -> dict[str, dict[str, Any]]:
    path = layout.generated_dir / "intake" / "intake_answers.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        str(key): value
        for key, value in (data or {}).items()
        if isinstance(value, dict)
    }


def _normalize_answer(question: dict[str, Any], answer: str) -> str:
    """Option-backed questions accept an option id or its exact label (a
    multi-select accepts a comma-separated list of them); free-text questions
    accept any non-empty text."""
    text = str(answer or "").strip()
    if not text:
        raise ValueError(f"question `{question['id']}` needs a non-empty answer")
    options = question.get("options") or []
    if not options:
        return text

    by_id = {option["option_id"].lower(): option["option_id"] for option in options}
    by_label = {option["label"].strip().lower(): option["option_id"] for option in options}
    # A domain-tuned panel shows a different LABEL for the same id; accept it too.
    by_label.update(_overlay_labels().get(question["id"], {}))
    parts = [part.strip() for part in text.split(",")] if question["answer_type"] == "multi_select" else [text]
    resolved: list[str] = []
    for part in parts:
        key = part.lower()
        option_id = by_id.get(key) or by_label.get(key)
        if option_id is None:
            raise ValueError(
                f"question `{question['id']}` expects one of "
                f"{', '.join(sorted(by_id.values()))}, got {part!r}"
            )
        resolved.append(option_id)
    return ",".join(dict.fromkeys(resolved))


def _measured_evidence(
    question: dict[str, Any], discovery: dict[str, Any], answers: dict[str, Any]
) -> dict[str, Any] | None:
    """The measurement that makes this question unnecessary, or None."""
    predicate = question.get("answerable_from")
    if str(discovery.get("status") or "") != "ok":
        return None
    if predicate == DISCOVERY_PREDICATE_WORKING_SET:
        working_set = discovery.get("working_set_estimate_bytes")
        if not isinstance(working_set, int):
            return None
        return {
            "source": "interns/generated/intake/discovery.json",
            "working_set_estimate_bytes": working_set,
            "table_count": len(discovery.get("tables") or []),
        }
    if predicate == DISCOVERY_PREDICATE_ARRIVAL_LANE:
        return _arrival_lane_evidence(discovery, answers)
    return None


def _arrival_lane_evidence(
    discovery: dict[str, Any], answers: dict[str, Any]
) -> dict[str, Any] | None:
    """`target_latency` is skipped only when BOTH halves hold:

    1. every discovered table's ``arrival_pattern`` was MEASURED (no `unknown`), and
    2. the answered ``freshness_sla`` is coherent with every one of those patterns
       -- i.e. the pair implies one lane (``implied_lane``), and all tables imply
       the same one.

    Anything else -- an unlistable source, a contradiction (files that arrive once
    a day but a sub-minute SLA), or a mixed set of implied lanes -- asks the
    question instead of inventing a lane.
    """
    pattern = aggregate_arrival_pattern(discovery)
    if pattern == ARRIVAL_UNKNOWN:
        return None
    sla_entry = answers.get("freshness_sla") or {}
    sla = str(sla_entry.get("answer") or "") if isinstance(sla_entry, dict) else str(sla_entry)
    if not sla:
        return None
    lane = implied_lane(pattern, sla)
    if lane is None:
        return None
    return {
        "source": "interns/generated/intake/discovery.json + intake_answers.freshness_sla",
        "arrival_pattern": pattern,
        "freshness_sla": sla,
        "implied_lane": lane,
    }


def aggregate_arrival_pattern(discovery: dict[str, Any]) -> str:
    """One arrival pattern for the workspace, from the per-table measurements.

    Any continuously-arriving table makes the workspace continuous (it is the
    table that forces the lane). Otherwise one unmeasured table is enough to make
    the whole thing `unknown` -- the honest answer, and the one that asks.
    """
    tables = [t for t in (discovery.get("tables") or []) if isinstance(t, dict)]
    if not tables:
        return ARRIVAL_UNKNOWN
    patterns = [
        str(
            table.get("arrival_pattern")
            # Discovery payloads written before arrival measurement only carry the
            # derived flag; read it rather than re-labelling them unknown.
            or ("continuous" if table.get("is_streaming") else ARRIVAL_UNKNOWN)
        )
        for table in tables
    ]
    if "continuous" in patterns:
        return "continuous"
    if ARRIVAL_UNKNOWN in patterns:
        return ARRIVAL_UNKNOWN
    if "periodic" in patterns:
        return "periodic"
    return "one_shot"


# --------------------------------------------------------------------------
# understanding playback
# --------------------------------------------------------------------------

_MEASURED = "(measured)"
_YOU_SAID = "(you said)"
_DEFAULT = "(default)"
# An answer the PLATFORM supplied is not something the operator said. Tagging it
# "(you said)" turns the alignment gate into a rubber stamp: the reader confirms
# statements they never made. Found live -- a whole interview of platform
# recommendations was played back as the operator's own words.
_ASSUMED = "(assumed by the platform -- correct it if wrong)"
# A suggested_answer is a TEMPLATE to edit, not an answer. Applied verbatim it
# reaches the playback as e.g. "one row per <business entity>", and a grain like
# that would design a meaningless pipeline.
_PLACEHOLDER = re.compile(r"<[^<>]{1,80}>")


def render_playback(
    workspace: str,
    discovery: dict[str, Any],
    answers: dict[str, Any],
    *,
    signal: DomainSignal | None = None,
    domain: str = "",
) -> str:
    """"Did I get you right?" -- every requirement restated in plain English, each
    line tagged with where it came from, so a wrong assumption is visible before
    anything is designed on top of it."""

    def said(question_id: str) -> str:
        entry = answers.get(question_id)
        if not isinstance(entry, dict):
            return ""
        return str(entry.get("answer") or "").strip()

    def tag_for(question_id: str) -> str:
        """Attribute the line to whoever actually supplied it."""
        entry = answers.get(question_id)
        if not isinstance(entry, dict):
            return _DEFAULT
        answer = str(entry.get("answer") or "")
        if _PLACEHOLDER.search(answer):
            return "(UNANSWERED -- still a template, replace the <placeholders>)"
        return _YOU_SAID if entry.get("source") == "human" else _ASSUMED

    def line(label: str, question_id: str, default: str) -> str:
        answer = said(question_id)
        if answer:
            return f"- {label}: {answer.replace(',', ', ')} {tag_for(question_id)}"
        return f"- {label}: {default} {_DEFAULT}"

    pattern = aggregate_arrival_pattern(discovery)
    lane, lane_tag = _playback_lane(discovery, answers, pattern)
    scd2 = said("as_it_was_then")

    lines = [
        "# Did I get you right?",
        "",
        f"Workspace: `{workspace}`",
        "",
        "This is what the platform believes it was told. Every line says where it came "
        "from: " + f"{_MEASURED} from the discovery scan, {_YOU_SAID} from an intake "
        f"answer, {_DEFAULT} where nothing has been said yet and the platform's default "
        "applies.",
        "",
        "## Requirements",
        "",
        *_playback_domain_lines(domain, signal),
        line("Consumers", "consumers", "not stated"),
        line("Decisions the number drives", "decisions_driven", "not stated"),
        line("Grain (one row is)", "grain_sentence", "not stated"),
        line("Freshness SLA", "freshness_sla", "not stated"),
        line("Reporting calendar / restatement", "calendar_and_restatement", "not stated"),
        line("History and retention", "history_backfill_retention", "not stated"),
        line("Owner and on-call", "owner_and_operations", "not stated"),
        f"- Arrival pattern: {pattern} {_MEASURED if pattern != ARRIVAL_UNKNOWN else _DEFAULT}",
        f"- Velocity lane: {lane} {lane_tag}",
        (
            f"- Point-in-time history (SCD2): "
            f"{'yes' if scd2 == 'as_it_was_then' else 'no'} "
            f"{_YOU_SAID if scd2 else _DEFAULT}"
        ),
        "",
        "## If any line above is wrong",
        "",
        "Re-answer that question, then re-read this playback:",
        "",
        "```powershell",
        f"uv run apply-intake-answer --workspace {workspace} "
        '--question <question_id> --answer <option_id_or_text> --answered-by "<name>"',
        "```",
        "",
        "## If it is right",
        "",
        "```powershell",
        f"uv run apply-intake-answer --workspace {workspace} "
        '--question playback_confirm --answer confirmed --answered-by "<name>"',
        "```",
        "",
        "`prepare-blueprint` refuses until that confirmation exists: designing a "
        "pipeline on a misread requirement is more expensive than one more question.",
        "",
    ]
    return "\n".join(lines)


def _playback_domain_lines(domain: str, signal: DomainSignal | None) -> list[str]:
    """The confirmed domain and the evidence it rested on.

    Every later default in this playback was tuned by that answer, so the
    evidence belongs here rather than only in the panel that asked.
    """
    labels = domain_labels()
    detected = signal.domain if signal else UNKNOWN_DOMAIN
    evidence = list(signal.evidence) if signal else []
    if domain:
        lines = [f"- Business domain: {labels.get(domain, domain)} {_YOU_SAID}"]
    elif detected != UNKNOWN_DOMAIN:
        lines = [
            f"- Business domain: not confirmed yet; detected {labels.get(detected, detected)} "
            f"at confidence {signal.confidence:.2f} {_DEFAULT}"
            if signal
            else f"- Business domain: not confirmed yet {_DEFAULT}"
        ]
    else:
        lines = [
            "- Business domain: not confirmed, and no evidence matched a known "
            f"domain -- nothing below was domain-tuned {_DEFAULT}"
        ]
    if evidence:
        lines.append(
            f"  Evidence ({detected}): " + "; ".join(evidence[:6])
        )
    elif domain:
        lines.append(
            "  Evidence: none matched; the domain above is your answer, not an inference."
        )
    return lines


def _playback_lane(
    discovery: dict[str, Any], answers: dict[str, Any], pattern: str
) -> tuple[str, str]:
    entry = answers.get("target_latency")
    answered = str(entry.get("answer") or "") if isinstance(entry, dict) else ""
    if answered:
        return _LANE_BY_ANSWER.get(answered, answered), _YOU_SAID
    sla_entry = answers.get("freshness_sla")
    sla = str(sla_entry.get("answer") or "") if isinstance(sla_entry, dict) else ""
    lane = implied_lane(pattern, sla)
    if lane:
        return lane, _MEASURED
    return "not decided yet: answer `target_latency`", _DEFAULT


# `target_latency` option id -> lane. The decision tables read the same map.
_LANE_BY_ANSWER = {
    "batch_daily": "batch",
    "micro_batch_hourly": "micro_batch",
    "streaming_continuous": "streaming",
    "realtime_serving": "realtime_serving",
}


def render_markdown(panel: dict[str, Any]) -> str:
    lines = [
        "# Intake Interview",
        "",
        f"Workspace: `{panel.get('workspace', '')}` · "
        f"Answered: {panel.get('answered_count', 0)} · "
        f"Pending: {panel.get('pending_count', 0)} · "
        f"Measured (not asked): {panel.get('skipped_count', 0)}",
        "",
    ]
    current_id = str(panel.get("current_question_id") or "")
    questions = panel.get("questions") or []

    if current_id:
        current = next((q for q in questions if q["id"] == current_id), None)
        if current is not None:
            lines.extend(["## Answer next", "", f"`{current['id']}` — {current['text']}", ""])
            lines.extend([f"Why it matters: {current['why_it_matters']}", ""])
            lines.extend(_option_lines(current))
    else:
        lines.extend(["All questions are answered or already measured.", ""])

    lines.extend(["## All questions", ""])
    for question in questions:
        marker = _marker(str(question.get("state") or ""))
        lines.append(f"- {marker} `{question['id']}` — {question['text']}")
        if question.get("state") == "answered":
            lines.append(f"  Answer: `{question.get('answer', '')}`")
        elif question.get("state") == "skipped_measured":
            evidence = question.get("measured_evidence") or {}
            lines.append(f"  Measured from `{evidence.get('source', '')}`, not asked.")
    lines.extend(
        [
            "",
            "## Apply",
            "",
            "```powershell",
            str(panel.get("apply_command", "")),
            "```",
            "",
            "Machine contract: `current.json`.",
        ]
    )
    return "\n".join(lines) + "\n"


def _option_lines(question: dict[str, Any]) -> list[str]:
    options = question.get("options") or []
    if not options:
        suggested = str(question.get("suggested_answer") or "")
        lines = ["Free-text answer.", ""]
        if suggested:
            lines.extend(["Suggested starting point (edit it, do not just paste it):", "", f"> {suggested}", ""])
        return lines
    recommended = str(question.get("recommended_option_id") or "")
    multi = question.get("answer_type") == "multi_select"
    lines = ["Options:" + (" (comma-separate more than one)" if multi else ""), ""]
    for option in options:
        suffix = " [RECOMMENDED]" if option["option_id"] == recommended else ""
        lines.append(f"- `{option['option_id']}`{suffix} · {option['label']}")
    if not recommended:
        lines.append("")
        lines.append("No option is recommended: the evidence does not support one.")
    lines.append("")
    return lines


def _marker(state: str) -> str:
    if state == "answered":
        return "[ok]"
    if state == "skipped_measured":
        return "[~]"
    return "[x]"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "ANSWERS_VERSION",
    "DOMAIN_OTHER_OPTION",
    "DOMAIN_QUESTION_ID",
    "PANEL_VERSION",
    "PLAYBACK_CONFIRM_QUESTION",
    "QUESTIONS",
    "QUESTIONS_BY_ID",
    "SYSTEM_QUESTIONS",
    "IntakeAnswerRecorder",
    "IntakePanelBuilder",
    "IntakePanelResult",
    "aggregate_arrival_pattern",
    "confirmed_domain",
    "load_intake_answers",
    "render_markdown",
    "render_playback",
]
