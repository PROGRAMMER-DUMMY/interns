"""Apply a human-confirmed KPI definition (metric / grain / filters).

Natural-language-question workspaces arrive with KPIs that carry a business
question but no measurable ``metric``/``cuts``. Evidence-based derivation
(``metric_derivation``) fills the unambiguous ones; the genuinely-ambiguous ones
(share %, top-N, derived durations, readmission windows) are surfaced at the
``kpi_definition_required`` blocker for a human to define. Until now there was no
way to APPLY that human answer back into the registry, so those KPIs could be
asked about forever but never complete.

This module closes that loop the same way the rest of the platform applies human
answers: the decision is persisted (with provenance), re-applied on every
onboard so it survives regeneration, and mirrored into the live contract for
immediate effect. It never fabricates a definition and never hand-edits a
generated contract outside this deterministic apply path.

Decision store: ``<ws>/interns/generated/decisions/kpi_definitions.json``, keyed
by normalized business question (stable across re-onboards, which re-derive the
``kpi_001`` ids).
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout

STORE_VERSION = 1
_STORE_RELATIVE = ("decisions", "kpi_definitions.json")


def kpi_definition_key(business_question: str) -> str:
    """Normalized identity for a KPI business question (matches the dedupe key)."""
    return re.sub(
        r"\s+", " ", re.sub(r"[^a-z0-9\s]+", " ", str(business_question or "").lower())
    ).strip()


def _store_path(layout: WorkspaceLayout) -> Path:
    return layout.generated_dir.joinpath(*_STORE_RELATIVE)


def load_kpi_definition_store(layout: WorkspaceLayout) -> dict[str, Any]:
    path = _store_path(layout)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    defs = data.get("definitions")
    return defs if isinstance(defs, dict) else {}


def apply_accepted_definitions_to_kpis(kpis: list[Any], store: dict[str, Any]) -> list[Any]:
    """Override empty metric/cuts on KpiDefinition rows from accepted decisions.

    A human-confirmed definition is authoritative: it fills the cell even over a
    derived guess. Operates on the onboarding ``KpiDefinition`` dataclass; unknown
    KPIs pass through untouched. No-op when the store is empty.
    """
    if not store:
        return list(kpis)
    from dataclasses import replace

    out = []
    for kpi in kpis:
        decision = store.get(kpi_definition_key(getattr(kpi, "name", "")))
        if not isinstance(decision, dict):
            out.append(kpi)
            continue
        metric = str(decision.get("metric") or "").strip()
        cuts = str(decision.get("cuts") or "").strip()
        changes: dict[str, Any] = {}
        if metric:
            changes["metric"] = metric
        if cuts:
            changes["cuts"] = cuts
        out.append(replace(kpi, **changes) if changes else kpi)
    return out


@dataclass(frozen=True)
class ApplyKpiDefinitionResult:
    workspace: str
    kpi_id: str
    business_question: str
    metric: str
    cuts: str
    store_path: str
    contract_patched: bool

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "kpi_id": self.kpi_id,
            "business_question": self.business_question,
            "metric": self.metric,
            "cuts": self.cuts,
            "store_path": self.store_path,
            "contract_patched": self.contract_patched,
            "next_step": (
                "Re-run the KPI pipeline (or resolve-kpi-features) to generate SQL "
                "and results for this KPI."
            ),
        }


def _resolve_business_question(
    registry: dict[str, Any], kpi_id: str
) -> tuple[str, int] | None:
    for idx, kpi in enumerate(registry.get("kpis") or [], start=1):
        if not isinstance(kpi, dict):
            continue
        if (kpi.get("kpi_id") or f"kpi_{idx:03d}") == kpi_id:
            return str(kpi.get("name") or kpi.get("business_question") or ""), idx - 1
    return None


def apply_kpi_definition(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    kpi_id: str = "",
    business_question: str = "",
    metric: str = "",
    cuts: str = "",
    confirmed_by: str = "",
    note: str = "",
) -> ApplyKpiDefinitionResult:
    metric = str(metric or "").strip()
    cuts = str(cuts or "").strip()
    if not metric and not cuts:
        raise ValueError("provide at least one of --metric or --cuts")
    if not confirmed_by.strip():
        raise ValueError(
            "a human-confirmed KPI definition requires --confirmed-by "
            "(records provenance source: human)"
        )

    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    registry_path = layout.kpi_registry_path
    registry = _read_json(registry_path)

    resolved_index: int | None = None
    if kpi_id:
        found = _resolve_business_question(registry, kpi_id)
        if found is None:
            raise ValueError(f"kpi_id not found in registry: {kpi_id}")
        business_question, resolved_index = found
    if not business_question.strip():
        raise ValueError("provide --kpi-id or --business-question")

    key = kpi_definition_key(business_question)

    # 1) Persist the decision (durable; re-applied on every onboard).
    store_path = _store_path(layout)
    store_doc = _read_json(store_path)
    definitions = store_doc.get("definitions")
    if not isinstance(definitions, dict):
        definitions = {}
    definitions[key] = {
        "business_question": business_question,
        "metric": metric,
        "cuts": cuts,
        "source": "human",
        "confirmed_by": confirmed_by.strip(),
        "note": note.strip(),
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "artifact_type": "kpi_definitions.json",
                "version": STORE_VERSION,
                "generated_by": "apply-kpi-definition",
                "workspace": _rel(workspace_path, root),
                "definitions": definitions,
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )

    # 2) Mirror into the live contract so the next pipeline run sees it without a
    #    full re-onboard. Match by kpi_id (if given) else by business question.
    contract_patched = False
    kpis = registry.get("kpis")
    if isinstance(kpis, list):
        for idx, kpi in enumerate(kpis):
            if not isinstance(kpi, dict):
                continue
            matches_id = resolved_index is not None and idx == resolved_index
            matches_q = kpi_definition_key(
                str(kpi.get("name") or kpi.get("business_question") or "")
            ) == key
            if matches_id or matches_q:
                if metric:
                    kpi["metric"] = metric
                if cuts:
                    kpi["cuts"] = cuts
                kpi["definition_source"] = "human_confirmed"
                kpi["definition_confirmed_by"] = confirmed_by.strip()
                contract_patched = True
        if contract_patched:
            registry_path.write_text(
                json.dumps(registry, indent=2, default=str) + "\n", encoding="utf-8"
            )

    return ApplyKpiDefinitionResult(
        workspace=_rel(workspace_path, root),
        kpi_id=kpi_id or "",
        business_question=business_question,
        metric=metric,
        cuts=cuts,
        store_path=_rel(store_path, root),
        contract_patched=contract_patched,
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply a human-confirmed KPI definition (metric/grain) into the registry."
    )
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--kpi-id", default="", help="KPI id (e.g. kpi_004). Resolves the business question.")
    parser.add_argument("--business-question", default="", help="Business question (alternative to --kpi-id).")
    parser.add_argument("--metric", default="", help="Metric expression, e.g. 'count(*)' or 'avg(BASE_COST)'.")
    parser.add_argument("--cuts", default="", help="Grain/filters, comma-separated, e.g. 'DESCRIPTION' or 'PAYER_COVERAGE = 0'.")
    parser.add_argument("--confirmed-by", default="", help="Human reviewer name (records provenance source: human).")
    parser.add_argument("--note", default="", help="Optional rationale recorded with the decision.")
    args = parser.parse_args(argv)
    result = apply_kpi_definition(
        args.repo_root,
        args.workspace,
        kpi_id=args.kpi_id,
        business_question=args.business_question,
        metric=args.metric,
        cuts=args.cuts,
        confirmed_by=args.confirmed_by,
        note=args.note,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
