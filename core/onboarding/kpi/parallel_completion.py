"""Dependency-aware parallel KPI completion planning.

When a workspace has many KPIs, resolving them one at a time is slow. But naive
fan-out is wrong: two KPIs that share an unresolved feature (e.g. the same
"encounter date" mapping) or the same join must be resolved *together* so the
decision is made once and reused — not asked twice by two workers racing each
other.

This module computes the inter-KPI dependency graph from the already-generated
``kpi_feature_mapping.json`` and produces a correctness-preserving parallel plan:

  Phase 1 (serial)   -- resolve SHARED blockers once; the existing
                        workspace-definition mechanism reuses each decision for
                        every KPI it applies to.
  Phase 2 (parallel) -- KPIs that share no blocker are independent; fan them out
                        across N workers (N scales with the number of independent
                        units). KPIs that DO share a blocker live in one connected
                        component and are kept on the same worker, so a shared
                        decision is never made twice.

The module is deterministic and workspace-agnostic: edges come from shared
feature names / join keys in the mapping, never a domain vocabulary. It emits a
plan artifact; actual concurrent dispatch is performed by the orchestrating
CLI agent (or workspace flow) using the worker assignments and delegation
roster in the plan.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.features.blockers import normalize as _normalize
from core.storage.workspace_layout import WorkspaceLayout

READY_STATES = {
    "proven_direct",
    "proven_alias",
    "proven_join",
    "proven_formula",
    "proven_taxonomy",
    "user_confirmed",
}

PLAN_VERSION = 1

# Above this many ready KPIs, `run-kpi-pipeline` fans completion out across
# workers via the `parallel_kpi_completion` delegation route instead of resolving
# them one at a time. At or below it, behavior is unchanged (sequential).
# Overridable per call (``threshold=``) or via the env var below. The default is
# conservative: it lines up with the worker ladder's first fan-out step
# (>3 independent units -> >=2 workers) so we only parallelize once there is
# enough independent work to be worth the coordination.
DEFAULT_PARALLEL_KPI_THRESHOLD = 3
PARALLEL_KPI_THRESHOLD_ENV = "AUTORESEARCH_PARALLEL_KPI_THRESHOLD"


def resolve_parallel_threshold(threshold: int | None = None) -> int:
    """Resolve the ready-KPI fan-out threshold.

    Precedence: explicit ``threshold`` arg > ``AUTORESEARCH_PARALLEL_KPI_THRESHOLD``
    env var > ``DEFAULT_PARALLEL_KPI_THRESHOLD``. A non-integer or negative env
    value is ignored (falls back to the default) so a malformed override can never
    silently disable or invert the gate.
    """
    if threshold is not None:
        return max(0, int(threshold))
    raw = os.environ.get(PARALLEL_KPI_THRESHOLD_ENV)
    if raw is not None:
        try:
            parsed = int(str(raw).strip())
        except (TypeError, ValueError):
            return DEFAULT_PARALLEL_KPI_THRESHOLD
        if parsed >= 0:
            return parsed
    return DEFAULT_PARALLEL_KPI_THRESHOLD


def count_ready_kpis(mapping: dict[str, Any]) -> int:
    """Count KPIs that have no unresolved feature/join blockers left.

    A KPI is "ready" for completion when every one of its features is in a
    READY_STATE (proven/confirmed). Derived from the mapping only -- no domain
    vocabulary.
    """
    ready = 0
    for kpi in mapping.get("kpis") or []:
        if not isinstance(kpi, dict):
            continue
        if not _unresolved_features(kpi) and not _join_keys(kpi):
            ready += 1
    return ready


def decide_worker_count(parallel_units: int, *, max_workers: int = 6) -> int:
    """Map the number of independent execution units to a worker count.

    Conservative, capped scaling (the 2/4/6 ladder): tiny workloads stay serial;
    large ones cap at ``max_workers`` so we never spawn more workers than there
    is independent work to do.
    """
    if parallel_units <= 3:
        return 1
    if parallel_units <= 6:
        return min(2, max_workers)
    if parallel_units <= 12:
        return min(4, max_workers)
    return max_workers


def _unresolved_features(kpi: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for feature in kpi.get("features") or []:
        state = str(feature.get("state") or "")
        if state in READY_STATES:
            continue
        name = _normalize(str(feature.get("feature") or ""))
        if name:
            out.append(name)
    return out


def _join_keys(kpi: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for join in kpi.get("join_candidates") or []:
        key = _normalize(str(join.get("key") or ""))
        datasets = sorted({str(d) for d in (join.get("datasets") or []) if d})
        if key and len(datasets) > 1:
            # Key the dependency on (join key + the dataset pair) so two KPIs only
            # link when they need the *same* join, not merely a same-named id.
            keys.append(key + "|" + "|".join(datasets))
    return keys


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


@dataclass(frozen=True)
class ParallelPlanResult:
    plan_path: str
    markdown_path: str
    kpi_count: int
    worker_count: int
    component_count: int
    shared_blocker_count: int

    def summary(self) -> dict[str, Any]:
        return {
            "plan_path": self.plan_path,
            "markdown_path": self.markdown_path,
            "kpi_count": self.kpi_count,
            "worker_count": self.worker_count,
            "component_count": self.component_count,
            "shared_blocker_count": self.shared_blocker_count,
        }


def build_completion_graph(mapping: dict[str, Any]) -> dict[str, Any]:
    """Connected-component graph of KPIs linked by shared blockers/joins.

    Returns the components (each a list of kpi_ids), the shared-blocker token ->
    kpi_ids index, and per-KPI unresolved-feature/join keys. KPIs with no shared
    token are singleton components (independent, freely parallelizable).
    """
    kpis = [k for k in mapping.get("kpis") or [] if isinstance(k, dict)]
    kpi_ids = [str(k.get("kpi_id") or f"kpi_{i:03d}") for i, k in enumerate(kpis, start=1)]

    token_to_kpis: dict[str, list[str]] = {}
    per_kpi_tokens: dict[str, list[str]] = {}
    for kpi_id, kpi in zip(kpi_ids, kpis):
        tokens = _unresolved_features(kpi) + _join_keys(kpi)
        per_kpi_tokens[kpi_id] = tokens
        for token in tokens:
            token_to_kpis.setdefault(token, [])
            if kpi_id not in token_to_kpis[token]:
                token_to_kpis[token].append(kpi_id)

    uf = _UnionFind(kpi_ids)
    for kpis_sharing in token_to_kpis.values():
        first = kpis_sharing[0]
        for other in kpis_sharing[1:]:
            uf.union(first, other)

    components_map: dict[str, list[str]] = {}
    for kpi_id in kpi_ids:
        components_map.setdefault(uf.find(kpi_id), []).append(kpi_id)
    # Deterministic ordering: by first kpi_id in each component.
    components = sorted(components_map.values(), key=lambda ids: ids[0])

    shared_blockers = {
        token: ids for token, ids in token_to_kpis.items() if len(ids) > 1
    }
    return {
        "kpi_ids": kpi_ids,
        "components": components,
        "shared_blockers": shared_blockers,
        "per_kpi_tokens": per_kpi_tokens,
    }


def _assign_components_to_workers(
    components: list[list[str]], worker_count: int
) -> list[dict[str, Any]]:
    """Balance whole components across workers by KPI count (largest-first).

    A component is never split: KPIs that share a blocker stay on one worker so
    the shared decision is resolved once. Workers therefore operate on disjoint
    KPI sets and never contend on the same blocker.
    """
    workers: list[dict[str, Any]] = [
        {"worker_id": f"worker_{i+1}", "component_ids": [], "kpi_ids": [], "_load": 0}
        for i in range(max(1, worker_count))
    ]
    indexed = sorted(
        enumerate(components), key=lambda item: (-len(item[1]), item[1][0])
    )
    for comp_index, kpi_ids in indexed:
        target = min(workers, key=lambda w: w["_load"])
        target["component_ids"].append(f"component_{comp_index+1}")
        target["kpi_ids"].extend(kpi_ids)
        target["_load"] += len(kpi_ids)
    for worker in workers:
        worker.pop("_load", None)
    return [w for w in workers if w["kpi_ids"]]


def plan_parallel_completion(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    mapping: dict[str, Any] | None = None,
    max_workers: int = 6,
) -> ParallelPlanResult:
    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    mapping_path = layout.contracts_dir / "kpi_feature_mapping.json"
    if mapping is None:
        if not mapping_path.exists():
            raise FileNotFoundError(
                f"kpi_feature_mapping.json not found: {_rel(mapping_path, root)}; "
                "run resolve-kpi-features first."
            )
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))

    graph = build_completion_graph(mapping)
    components = graph["components"]
    worker_count = decide_worker_count(len(components), max_workers=max_workers)
    worker_assignments = _assign_components_to_workers(components, worker_count)

    # Shared blockers (token in >1 KPI) become Phase-1 serial work items. Prefer
    # the resolver's risk-ranked blocker_clusters when present for human-readable
    # questions; fall back to the raw shared-token index.
    cluster_by_feature = {
        _normalize(str(c.get("feature") or "")): c
        for c in (mapping.get("blocker_clusters") or [])
    }
    shared_items: list[dict[str, Any]] = []
    for token, ids in sorted(graph["shared_blockers"].items(), key=lambda kv: -len(kv[1])):
        feature = token.split("|", 1)[0]
        cluster = cluster_by_feature.get(feature, {})
        shared_items.append(
            {
                "shared_token": token,
                "feature": cluster.get("feature") or feature,
                "applies_to_kpis": ids,
                "risk": cluster.get("risk", "unknown"),
                "recommended_question": cluster.get("recommended_question")
                or f"Resolve `{feature}` once; the decision is reused for {len(ids)} KPI(s).",
                "reuse": "Apply once as a workspace-level definition; reused for every listed KPI.",
            }
        )

    from core.onboarding.workspace.delegation import routing_for

    component_payload = [
        {
            "component_id": f"component_{i+1}",
            "kpi_ids": kpi_ids,
            "independent": len(kpi_ids) == 1,
            "shared_within_component": sorted(
                {
                    tok.split("|", 1)[0]
                    for kpi_id in kpi_ids
                    for tok in graph["per_kpi_tokens"].get(kpi_id, [])
                    if len(graph["shared_blockers"].get(tok, [])) > 1
                }
            ),
        }
        for i, kpi_ids in enumerate(components)
    ]

    plan = {
        "artifact_type": "parallel_completion_plan",
        "version": PLAN_VERSION,
        "generated_by": "plan-kpi-completion",
        "workspace": _rel(workspace_path, root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpi_count": len(graph["kpi_ids"]),
        "component_count": len(components),
        "worker_count": worker_count,
        "worker_count_rationale": (
            f"{len(components)} independent component(s) -> {worker_count} worker(s) "
            "(ladder: <=3:1, <=6:2, <=12:4, else 6)."
        ),
        "strategy": "shared_blocker_reuse_then_parallel",
        "execution": {
            "phase_1_serial": {
                "what": "Resolve shared blockers once (coordinator).",
                "items": shared_items,
            },
            "phase_2_parallel": {
                "what": "Dispatch independent components across workers concurrently.",
                "worker_assignments": worker_assignments,
            },
        },
        "components": component_payload,
        "delegation": routing_for("parallel_kpi_completion"),
        "notes": [
            "A component is never split across workers: KPIs sharing a blocker are "
            "resolved together so the decision is made once.",
            "Workers operate on disjoint KPI sets and disjoint solution outputs; "
            "the shared registry/mapping is written once by the coordinator.",
            "Run the kpi-analyst review gate once after the parallel phase joins.",
        ],
    }

    out_dir = layout.reports_dir / "parallel_completion"
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir / "current.json"
    markdown_path = out_dir / "current.md"
    plan_path.write_text(json.dumps(plan, indent=2, default=str) + "\n", encoding="utf-8")
    markdown_path.write_text(_render_markdown(plan), encoding="utf-8")

    return ParallelPlanResult(
        plan_path=_rel(plan_path, root),
        markdown_path=_rel(markdown_path, root),
        kpi_count=plan["kpi_count"],
        worker_count=worker_count,
        component_count=len(components),
        shared_blocker_count=len(shared_items),
    )


@dataclass(frozen=True)
class DispatchDecision:
    """Outcome of the run-kpi-pipeline fan-out decision.

    ``mode`` is ``"parallel"`` when the ready-KPI count exceeds the threshold AND
    the dependency plan actually has independent work to spread (>1 worker);
    otherwise ``"sequential"`` and the caller proceeds exactly as today. The plan
    artifact is always written so the decision is auditable either way.
    """

    mode: str
    ready_kpi_count: int
    threshold: int
    plan: ParallelPlanResult
    route: dict[str, list[str]]
    reason: str

    @property
    def fan_out(self) -> bool:
        return self.mode == "parallel"

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fan_out": self.fan_out,
            "ready_kpi_count": self.ready_kpi_count,
            "threshold": self.threshold,
            "reason": self.reason,
            "route": self.route,
            "plan": self.plan.summary(),
        }


def dispatch_parallel_completion(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    mapping: dict[str, Any] | None = None,
    threshold: int | None = None,
    max_workers: int = 6,
) -> DispatchDecision:
    """Decide whether run-kpi-pipeline should fan KPI completion out in parallel.

    Always builds the dependency-aware completion plan (so the artifact exists and
    the decision is auditable), then compares the ready-KPI count against the
    resolved threshold:

      ready_kpi_count > threshold AND plan.worker_count > 1
          -> mode="parallel": the caller dispatches the plan's worker
             assignments concurrently using the ``parallel_kpi_completion``
             delegation route.
      otherwise
          -> mode="sequential": the caller proceeds one KPI at a time as today.

    Deterministic and workspace-agnostic. Performs no remote mutation and spawns
    no workers itself -- it returns the decision + plan + delegation route for the
    orchestrating caller (run-kpi-pipeline / workspace flow) to act on.
    """
    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    if mapping is None:
        layout = WorkspaceLayout(project_root=workspace_path)
        mapping_path = layout.contracts_dir / "kpi_feature_mapping.json"
        if not mapping_path.exists():
            raise FileNotFoundError(
                f"kpi_feature_mapping.json not found: {_rel(mapping_path, root)}; "
                "run resolve-kpi-features first."
            )
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))

    resolved_threshold = resolve_parallel_threshold(threshold)
    ready = count_ready_kpis(mapping)
    plan = plan_parallel_completion(
        root, workspace, mapping=mapping, max_workers=max_workers
    )

    # Import locally to avoid any import-order coupling with the delegation module.
    from core.onboarding.workspace.delegation import routing_for

    route = routing_for("parallel_kpi_completion")

    over_threshold = ready > resolved_threshold
    has_parallel_work = plan.worker_count > 1
    if over_threshold and has_parallel_work:
        reason = (
            f"{ready} ready KPI(s) > threshold {resolved_threshold} and "
            f"{plan.worker_count} workers across {plan.component_count} independent "
            "component(s): fan out via parallel_kpi_completion."
        )
        return DispatchDecision(
            mode="parallel",
            ready_kpi_count=ready,
            threshold=resolved_threshold,
            plan=plan,
            route=route,
            reason=reason,
        )

    if over_threshold and not has_parallel_work:
        reason = (
            f"{ready} ready KPI(s) > threshold {resolved_threshold}, but the plan "
            "has a single worker (no independent work to spread): run sequentially."
        )
    else:
        reason = (
            f"{ready} ready KPI(s) <= threshold {resolved_threshold}: "
            "run sequentially."
        )
    return DispatchDecision(
        mode="sequential",
        ready_kpi_count=ready,
        threshold=resolved_threshold,
        plan=plan,
        route=route,
        reason=reason,
    )


def _render_markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Parallel KPI Completion Plan",
        "",
        f"- Workspace: `{plan['workspace']}`",
        f"- KPIs: {plan['kpi_count']}",
        f"- Independent components: {plan['component_count']}",
        f"- Workers: {plan['worker_count']} ({plan['worker_count_rationale']})",
        f"- Strategy: `{plan['strategy']}`",
        "",
        "## Phase 1 — Resolve shared blockers once (serial)",
        "",
    ]
    shared = plan["execution"]["phase_1_serial"]["items"]
    if shared:
        lines.append("| Feature | Applies to KPIs | Risk |")
        lines.append("| --- | --- | --- |")
        for item in shared:
            lines.append(
                f"| {item['feature']} | {', '.join(item['applies_to_kpis'])} | {item['risk']} |"
            )
    else:
        lines.append("No shared blockers — every KPI is independent.")
    lines += ["", "## Phase 2 — Parallel workers", ""]
    lines.append("| Worker | KPIs |")
    lines.append("| --- | --- |")
    for worker in plan["execution"]["phase_2_parallel"]["worker_assignments"]:
        lines.append(f"| {worker['worker_id']} | {', '.join(worker['kpi_ids'])} |")
    lines += ["", "## Notes", ""]
    lines += [f"- {note}" for note in plan["notes"]]
    return "\n".join(lines) + "\n"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)



@anchored("plan-kpi-completion")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan dependency-aware parallel KPI completion."
    )
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--max-workers", type=int, default=6, help="Cap on parallel workers (default 6).")
    parser.add_argument("--json", action="store_true", help="Print the plan summary as JSON.")
    args = parser.parse_args(argv)
    result = plan_parallel_completion(
        args.repo_root, args.workspace, max_workers=args.max_workers
    )
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
