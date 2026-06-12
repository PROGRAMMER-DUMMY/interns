"""Relationship-graph-based base (fact) source selection for KPI plans.

Replaces the old "largest referenced table is the fact" heuristic with a
scoring model built from workspace relationship evidence. The selector is the
single source of truth for WHICH dataset anchors a KPI's FROM clause; the
source-to-target planner, the SQL/Polars/PySpark generators, and the local
warehouse fact classification all delegate here so every engine starts from
the identical base.

Scoring model (workspace-agnostic; no table or domain name is special-cased):

1. ``coverage`` — fraction of the KPI's referenced datasets reachable from the
   candidate via executable relationship edges (or same source group). A base
   that cannot reach a required dataset cannot serve the KPI.
2. ``grain``    — fraction of the KPI's grain dimensions whose column resolves
   in the candidate's own schema (the base should carry the KPI's grain).
3. ``fan-out safety`` — joins that traverse an executable edge from the many
   ("left") side to the one ("right"/dimension) side preserve row grain; every
   edge that must be traversed one->many from the candidate multiplies rows
   and is penalised.
4. ``evidence`` — executable relationship edges where the candidate sits on
   the many/fact side (declared/dictionary/diagram-backed fact role), direct
   feature refs, and profile membership.

Row count participates ONLY in the final deterministic tiebreak, never in the
score. When the top two candidates score within ``NEAR_TIE_MARGIN`` the
selection is flagged ``near_tie`` so the planner can emit a blocker-panel
question instead of silently picking; a recorded human decision
(``pipeline_decisions.json`` -> ``base_source_decisions``) pins the choice.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Two candidates within this score distance are "near-tied": the structural
# evidence does not clearly separate them, so a human decision is required
# rather than a silent pick. Score units are the weighted points below.
NEAR_TIE_MARGIN = 0.35

_W_COVERAGE = 4.0
_W_GRAIN = 1.0
_W_FANOUT_PENALTY = 1.5
_W_FACT_EDGE = 0.25
_FACT_EDGE_CAP = 2
_W_DIRECT_REF = 0.2
_DIRECT_REF_CAP = 5
_W_PROFILED = 0.25

# pipeline_decisions.json facet that pins a per-KPI base-source choice.
BASE_SOURCE_DECISIONS_KEY = "base_source_decisions"


@dataclass
class CandidateScore:
    dataset: str
    score: float = 0.0
    coverage_ratio: float = 0.0
    covered_datasets: list[str] = field(default_factory=list)
    unreachable_datasets: list[str] = field(default_factory=list)
    grain_matches: list[str] = field(default_factory=list)
    grain_ratio: float = 0.0
    fan_out_joins: list[dict[str, str]] = field(default_factory=list)
    safe_joins: list[dict[str, str]] = field(default_factory=list)
    fact_side_edge_count: int = 0
    direct_ref_count: int = 0
    profiled: bool = False
    row_count: int = 0
    join_paths: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "score": round(self.score, 4),
            "coverage_ratio": round(self.coverage_ratio, 4),
            "covered_datasets": self.covered_datasets,
            "unreachable_datasets": self.unreachable_datasets,
            "grain_matches": self.grain_matches,
            "grain_ratio": round(self.grain_ratio, 4),
            "fan_out_joins": self.fan_out_joins,
            "safe_joins": self.safe_joins,
            "fact_side_edge_count": self.fact_side_edge_count,
            "direct_ref_count": self.direct_ref_count,
            "profiled": self.profiled,
            "row_count": self.row_count,
            "join_paths": self.join_paths,
        }


@dataclass
class BaseSourceSelection:
    base_source: str
    candidates: list[CandidateScore] = field(default_factory=list)
    near_tie: bool = False
    tie_candidates: list[str] = field(default_factory=list)
    decision_source: str = "relationship_graph_score"
    pinned_by_decision: bool = False

    def rationale(self) -> dict[str, Any]:
        """Evidence packet recorded in the source-to-target plan so the
        kpi-analyst review gate can see WHY this base was chosen."""
        return {
            "method": "relationship_graph_score",
            "decision_source": self.decision_source,
            "base_source": self.base_source,
            "near_tie": self.near_tie,
            "near_tie_margin": NEAR_TIE_MARGIN,
            "tie_candidates": self.tie_candidates,
            "scoring_model": {
                "coverage_weight": _W_COVERAGE,
                "grain_weight": _W_GRAIN,
                "fan_out_penalty_per_join": _W_FANOUT_PENALTY,
                "fact_side_edge_weight": _W_FACT_EDGE,
                "direct_ref_weight": _W_DIRECT_REF,
                "profiled_weight": _W_PROFILED,
                "row_count_role": "final_tiebreak_only",
            },
            "candidates": [candidate.summary() for candidate in self.candidates],
        }


def select_base_source(
    refs: list[dict[str, Any]],
    profile_map: dict[str, dict[str, Any]],
    relationships: list[dict[str, Any]] | None = None,
    *,
    grain_dimensions: list[str] | tuple[str, ...] = (),
    pinned: str | None = None,
) -> BaseSourceSelection:
    """Score every referenced dataset as a base-source candidate.

    ``refs``: flat ``{"dataset", "column", ...}`` feature source refs.
    ``relationships``: relationship contract dicts (executable edges drive the
    graph); ``None``/empty degrades to same-source-group coverage only.
    ``grain_dimensions``: KPI grain/cut tokens for grain-compatibility scoring.
    ``pinned``: a recorded human decision (dataset path) — honored verbatim
    when it is one of the referenced datasets.
    """
    relationships = relationships or []
    ref_counts: dict[str, int] = {}
    for ref in refs:
        dataset = str(ref.get("dataset") or "")
        if dataset:
            ref_counts[dataset] = ref_counts.get(dataset, 0) + 1
    if not ref_counts:
        return BaseSourceSelection(base_source="", decision_source="no_refs")

    datasets = sorted(ref_counts)
    if pinned:
        pinned_norm = _norm_path(pinned)
        for dataset in datasets:
            if _norm_path(dataset) == pinned_norm:
                selection = _score_candidates(
                    datasets, ref_counts, profile_map, relationships, grain_dimensions
                )
                selection.base_source = dataset
                selection.near_tie = False
                selection.tie_candidates = []
                selection.decision_source = "pinned_pipeline_decision"
                selection.pinned_by_decision = True
                return selection

    selection = _score_candidates(
        datasets, ref_counts, profile_map, relationships, grain_dimensions
    )
    ranked = selection.candidates
    selection.base_source = ranked[0].dataset
    if len(datasets) == 1:
        selection.decision_source = "single_candidate"
        return selection
    if len(ranked) > 1:
        delta = ranked[0].score - ranked[1].score
        if delta < NEAR_TIE_MARGIN:
            selection.near_tie = True
            top_score = ranked[0].score
            selection.tie_candidates = [
                candidate.dataset
                for candidate in ranked
                if top_score - candidate.score < NEAR_TIE_MARGIN
            ]
    return selection


def _score_candidates(
    datasets: list[str],
    ref_counts: dict[str, int],
    profile_map: dict[str, dict[str, Any]],
    relationships: list[dict[str, Any]],
    grain_dimensions: list[str] | tuple[str, ...],
) -> BaseSourceSelection:
    edges = _executable_edges(relationships, datasets)
    grain_tokens = [token for token in (str(g).strip() for g in grain_dimensions) if token]
    candidates: list[CandidateScore] = []
    for dataset in datasets:
        candidate = CandidateScore(dataset=dataset)
        candidate.direct_ref_count = ref_counts.get(dataset, 0)
        candidate.profiled = dataset in profile_map
        candidate.row_count = _row_count(profile_map.get(dataset))
        _score_coverage_and_fanout(candidate, dataset, datasets, edges)
        _score_grain(candidate, profile_map.get(dataset), grain_tokens)
        candidate.fact_side_edge_count = sum(
            1 for (left, _right) in edges if left == dataset
        )
        candidate.score = (
            _W_COVERAGE * candidate.coverage_ratio
            + _W_GRAIN * candidate.grain_ratio
            - _W_FANOUT_PENALTY * len(candidate.fan_out_joins)
            + _W_FACT_EDGE * min(candidate.fact_side_edge_count, _FACT_EDGE_CAP)
            + _W_DIRECT_REF * min(candidate.direct_ref_count, _DIRECT_REF_CAP)
            + (_W_PROFILED if candidate.profiled else 0.0)
        )
        candidates.append(candidate)
    # Deterministic ranking: score first; row count and name are TIEBREAKS only.
    candidates.sort(key=lambda c: (-c.score, -c.row_count, c.dataset))
    return BaseSourceSelection(base_source="", candidates=candidates)


def _score_coverage_and_fanout(
    candidate: CandidateScore,
    dataset: str,
    datasets: list[str],
    edges: dict[tuple[str, str], dict[str, Any]],
) -> None:
    """BFS the executable-edge graph from ``dataset`` to every other
    referenced dataset, tracking the traversal direction of each edge.

    Edges are oriented many("left") -> one("right") by the relationship
    builder; traversing one in reverse from the candidate means the candidate
    sits on the "one" side and the join multiplies its rows."""
    others = [other for other in datasets if other != dataset]
    covered = [dataset]
    unreachable: list[str] = []
    for other in others:
        path = _shortest_path(dataset, other, edges)
        if path is None and _source_group(dataset) == _source_group(other):
            covered.append(other)
            candidate.join_paths[other] = ["same_source_group"]
            continue
        if path is None:
            unreachable.append(other)
            continue
        covered.append(other)
        hops: list[str] = []
        for left, right, forward in path:
            hops.append(f"{left} -> {right} ({'many->one' if forward else 'one->many'})")
            join_info = {
                "left_dataset": left,
                "right_dataset": right,
                "direction": "many->one" if forward else "one->many",
            }
            if forward:
                if join_info not in candidate.safe_joins:
                    candidate.safe_joins.append(join_info)
            else:
                if join_info not in candidate.fan_out_joins:
                    candidate.fan_out_joins.append(join_info)
        candidate.join_paths[other] = hops
    candidate.covered_datasets = sorted(covered)
    candidate.unreachable_datasets = sorted(unreachable)
    total = len(datasets)
    candidate.coverage_ratio = (len(covered) / total) if total else 0.0


def _score_grain(
    candidate: CandidateScore,
    profile: dict[str, Any] | None,
    grain_tokens: list[str],
) -> None:
    if not grain_tokens:
        candidate.grain_ratio = 0.0
        return
    schema = _schema(profile or {})
    normalized_schema = {_norm(column) for column in schema}
    matches = []
    for token in grain_tokens:
        token_norm = _norm(_strip_function_wrappers(token))
        if not token_norm:
            continue
        if token_norm in normalized_schema or any(
            token_norm in column or column in token_norm
            for column in normalized_schema
            if column
        ):
            matches.append(token)
    candidate.grain_matches = matches
    candidate.grain_ratio = len(matches) / len(grain_tokens)


def _executable_edges(
    relationships: list[dict[str, Any]],
    datasets: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Executable relationship edges keyed by (left, right) dataset path.

    Keys are the caller's dataset paths (normalized matching), so BFS results
    align with the candidate set even when contracts carry differently-rooted
    paths."""
    by_norm = {_norm_path(dataset): dataset for dataset in datasets}
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for relationship in relationships:
        if not _executable_allowed(relationship):
            continue
        left = by_norm.get(_norm_path(str(relationship.get("left_dataset") or "")))
        right = by_norm.get(_norm_path(str(relationship.get("right_dataset") or "")))
        if not left or not right or left == right:
            continue
        edges[(left, right)] = relationship
    return edges


def _shortest_path(
    start: str,
    goal: str,
    edges: dict[tuple[str, str], dict[str, Any]],
) -> list[tuple[str, str, bool]] | None:
    """BFS over executable edges (traversable both ways) from start to goal.

    Returns the hop list as (left, right, forward) where ``forward`` is True
    when the hop follows the edge's many->one orientation, or None when goal
    is unreachable."""
    if start == goal:
        return []
    adjacency: dict[str, list[tuple[str, str, str, bool]]] = {}
    for (left, right) in edges:
        adjacency.setdefault(left, []).append((right, left, right, True))
        adjacency.setdefault(right, []).append((left, left, right, False))
    queue: list[tuple[str, list[tuple[str, str, bool]]]] = [(start, [])]
    seen = {start}
    while queue:
        node, path = queue.pop(0)
        for neighbor, left, right, forward in adjacency.get(node, []):
            if neighbor in seen:
                continue
            next_path = path + [(left, right, forward)]
            if neighbor == goal:
                return next_path
            seen.add(neighbor)
            queue.append((neighbor, next_path))
    return None


def load_pinned_base_sources(contracts_dir: Path) -> dict[str, str]:
    """Recorded per-KPI base-source decisions from pipeline_decisions.json."""
    path = Path(contracts_dir) / "pipeline_decisions.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    decisions = data.get(BASE_SOURCE_DECISIONS_KEY)
    if not isinstance(decisions, dict):
        return {}
    return {str(k): str(v) for k, v in decisions.items() if v}


def base_source_blocker(
    kpi_id: str,
    selection: BaseSourceSelection,
) -> dict[str, Any]:
    """Planner blocker payload for a near-tied base-source selection.

    Carries per-candidate evidence (tables, join paths, grain matches, row
    counts) so the blocker panel can render a JSON-backed question instead of
    a freehand prompt."""
    tie_set = set(selection.tie_candidates)
    return {
        "type": "base_source_ambiguous",
        "kpi_id": kpi_id,
        "message": (
            "Relationship-graph scoring cannot clearly separate the candidate "
            "base (fact) tables for this KPI; a human decision is required "
            "instead of a silent largest-table pick."
        ),
        "candidates": [
            candidate.summary()
            for candidate in selection.candidates
            if candidate.dataset in tie_set
        ],
        "resolution": {
            "panel": "blocker_question_panel",
            "answer_records_to": f"pipeline_decisions.json -> {BASE_SOURCE_DECISIONS_KEY}",
            "command": (
                "uv run apply-kpi-panel-answer --workspace <workspace> "
                "--answer <option_id>"
            ),
        },
    }


def base_source_panel_questions(
    repo_root: str | Path,
    workspace: str | Path,
) -> list[dict[str, Any]]:
    """Panel questions for every near-tied, un-pinned base-source selection.

    Reads the recorded ``base_source_selection`` blocks from
    ``source_to_target_plan.json``. Options carry an ``intent_facet`` payload
    (facet ``base_source``) so the existing apply-kpi-panel-answer path records
    the choice via ``record_intent_answer`` -> pipeline_decisions.json with no
    new answer plumbing. Never raises -> [] on missing artifacts."""
    from core.storage.workspace_layout import WorkspaceLayout

    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    plan_path = layout.contracts_dir / "source_to_target_plan.json"
    if not plan_path.exists():
        return []
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    pinned = load_pinned_base_sources(layout.contracts_dir)
    workspace_rel = _rel(workspace_path, root)
    questions: list[dict[str, Any]] = []
    for kpi in plan.get("kpis") or []:
        if not isinstance(kpi, dict):
            continue
        kpi_id = str(kpi.get("kpi_id") or "")
        selection = kpi.get("base_source_selection") or {}
        if not selection.get("near_tie") or kpi_id in pinned:
            continue
        questions.append(
            _base_source_panel_question(kpi_id, kpi, selection, workspace_rel)
        )
    return questions


def _base_source_panel_question(
    kpi_id: str,
    kpi: dict[str, Any],
    selection: dict[str, Any],
    workspace_rel: str,
) -> dict[str, Any]:
    tie_candidates = [str(c) for c in selection.get("tie_candidates") or []]
    by_dataset = {
        str(candidate.get("dataset") or ""): candidate
        for candidate in selection.get("candidates") or []
        if isinstance(candidate, dict)
    }
    letters = "abcdefghij"
    options: list[dict[str, Any]] = []
    for idx, dataset in enumerate(tie_candidates):
        oid = f"option_{letters[idx]}" if idx < len(letters) else f"option_{idx}"
        candidate = by_dataset.get(dataset, {})
        options.append(
            {
                "option_id": oid,
                "label": dataset,
                "business_summary": (
                    f"Use `{dataset}` as the base (fact) table for `{kpi_id}`."
                ),
                "json_backed": False,
                "evidence": {
                    "score": candidate.get("score"),
                    "row_count": candidate.get("row_count"),
                    "coverage_ratio": candidate.get("coverage_ratio"),
                    "grain_matches": candidate.get("grain_matches"),
                    "join_paths": candidate.get("join_paths"),
                    "fan_out_joins": candidate.get("fan_out_joins"),
                    "safe_joins": candidate.get("safe_joins"),
                },
                "intent_facet": {
                    "kpi_id": kpi_id,
                    "facet": "base_source",
                    "value": dataset,
                },
            }
        )
    options.append(
        {
            "option_id": "custom",
            "label": "Other dataset / business rule",
            "business_summary": (
                f"Name the dataset that anchors `{kpi_id}` if none of the "
                "candidates is the correct fact table."
            ),
            "expected_answer_shape": "repo-relative dataset path",
            "json_backed": False,
            "intent_facet": {"kpi_id": kpi_id, "facet": "base_source", "value": ""},
        }
    )
    recommended = "option_a" if tie_candidates else "custom"
    return {
        "artifact_type": "blocker_question_panel/current.json",
        "version": 1,
        "generated_by": "blocker-question-panel",
        "routed_by": "base-source-selector",
        "workspace": workspace_rel,
        "question_id": f"base_source_{kpi_id}",
        "feature": f"base_source::{kpi_id}",
        "kpi_id": kpi_id,
        "kpi_name": str(kpi.get("business_question") or ""),
        "applies_to_kpis": [kpi_id] if kpi_id else [],
        "reuse_scope": "kpi_specific",
        "stage": "base_source_selection",
        "status": "needs_user_answer",
        "facet": "base_source",
        "blocker": (
            f"KPI `{kpi_id}` has {len(tie_candidates)} near-tied base (fact) "
            "table candidates; relationship-graph evidence cannot separate them."
        ),
        "question": (
            f"Which dataset is the base (fact) table for `{kpi_id}` "
            f"({kpi.get('business_question') or 'KPI'})?"
        ),
        "answer_type": "base_source_selection",
        "grain": kpi.get("grain") or {},
        "candidate_evidence": [by_dataset.get(d, {"dataset": d}) for d in tie_candidates],
        "interaction_contract": {
            "display_mode": "project_blocker_panel",
            "generic_answer_picker_allowed": False,
            "instruction": (
                "Show current.md verbatim or render options from current.json. "
                "Apply the answer with apply-kpi-panel-answer."
            ),
        },
        "recommended_option_id": recommended,
        "recommended_answer": tie_candidates[0] if tie_candidates else "",
        "why": (
            "The base table decides the KPI's row grain. Joining from the wrong "
            "side of a one-to-many relationship multiplies rows and corrupts "
            "every aggregate; the candidates' structural evidence is too close "
            "to decide automatically."
        ),
        "options": options,
    }


# ---------------------------------------------------------------------------
# Small local helpers (kept dependency-free; mirror contracts.py semantics)
# ---------------------------------------------------------------------------


def _executable_allowed(relationship: dict[str, Any]) -> bool:
    policy = relationship.get("executable_usage_policy") or {}
    if "allowed_in_sql_generation" in policy:
        return bool(policy.get("allowed_in_sql_generation"))
    return str(relationship.get("state") or "") in {"proven_data_model", "user_confirmed"}


def _row_count(profile: dict[str, Any] | None) -> int:
    try:
        return int((profile or {}).get("row_count") or 0)
    except (TypeError, ValueError):
        return 0


def _schema(profile: dict[str, Any]) -> dict[str, Any]:
    schema = profile.get("schema") or {}
    return schema if isinstance(schema, dict) else {}


def _source_group(source: str) -> str:
    normalized = source.replace("\\", "/")
    parts = normalized.split("/")
    if "datasets" in parts:
        idx = parts.index("datasets")
        if len(parts) > idx + 2:
            return "/".join(parts[: idx + 3])
    return normalized


def _strip_function_wrappers(token: str) -> str:
    """``Month(ServiceDate)`` -> ``ServiceDate``; plain tokens pass through."""
    match = re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*\(\s*(.+?)\s*\)\s*$", token)
    return match.group(1) if match else token


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _norm_path(value: str) -> str:
    return str(value).replace("\\", "/").lower()


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)
