"""A labelled feature->column corpus, derived from decisions humans already made.

The feature resolver has ~2,000 lines of scoring with hand-tuned weights
(`+30.0`, `-30.0`, `min(4.0, ...)`) and, until this module, no way to say
whether any of it was right. Every change to it was a guess.

The labels are DERIVED, never curated: a human answering a blocker panel or
confirming a definition has already stated "this feature means this column."
Those statements are the ground truth, and they are sitting in artifacts on
disk. Curating a second list by hand would be no more trustworthy than the
heuristics it grades.

**Only human-confirmed entries count.** `proven_direct` / `proven_alias` /
`proven_join` are the resolver's OWN output; grading against them measures
self-consistency and would report high accuracy for a resolver that is
confidently wrong. Two sources qualify:

  * a feature whose `state` is `user_confirmed` in `kpi_feature_mapping.json`
  * a `workspace_definitions.json` entry (a reusable workspace-level answer)

Small by construction -- it grows one human decision at a time. That is the
honest ceiling: it is enough to catch a regression, not enough to publish an
accuracy figure from. See `core.dev.resolver_accuracy`.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

# The states a HUMAN put there. Everything else is the resolver's own verdict.
HUMAN_CONFIRMED_STATES = ("user_confirmed",)


@dataclass(frozen=True)
class LabelledMapping:
    """One human-confirmed "this feature means this column" statement."""

    workspace: str
    kpi_id: str
    feature: str
    context: str          # the KPI text the resolver scores against
    expected_dataset: str
    expected_column: str
    label_source: str     # user_confirmed | workspace_definition

    def key(self) -> str:
        return f"{self.workspace}|{self.kpi_id}|{self.feature}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _kpi_context(kpi: dict[str, Any]) -> str:
    """The same context string the resolver builds. Imported rather than
    re-derived so the corpus never grades against a different input than
    production uses."""
    from core.onboarding.kpi.feature_resolver import _kpi_context as production_context

    return production_context(kpi)


def _first_source_column(feature: dict[str, Any]) -> tuple[str, str] | None:
    for source in feature.get("source_columns") or []:
        if not isinstance(source, dict):
            continue
        column = str(source.get("column") or "")
        if column:
            return str(source.get("dataset") or ""), column
    return None


def harvest_workspace(repo_root: Path, workspace_rel: str) -> list[LabelledMapping]:
    """Every human-confirmed mapping in one workspace."""
    layout = WorkspaceLayout(project_root=(Path(repo_root) / workspace_rel).resolve())
    out: list[LabelledMapping] = []

    mapping_path = layout.kpi_feature_mapping_path
    if mapping_path.exists():
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            mapping = {}
        for kpi in mapping.get("kpis", []) or []:
            if not isinstance(kpi, dict):
                continue
            context = _kpi_context(kpi)
            kpi_id = str(kpi.get("kpi_id") or "")
            for feature in kpi.get("features", []) or []:
                if not isinstance(feature, dict):
                    continue
                if str(feature.get("state") or "") not in HUMAN_CONFIRMED_STATES:
                    continue
                pair = _first_source_column(feature)
                if not pair:
                    continue
                out.append(LabelledMapping(
                    workspace=workspace_rel,
                    kpi_id=kpi_id,
                    feature=str(feature.get("feature") or ""),
                    context=context,
                    expected_dataset=pair[0],
                    expected_column=pair[1],
                    label_source="user_confirmed",
                ))

    definitions_path = layout.generated_dir / "memory" / "workspace_definitions.json"
    if definitions_path.exists():
        try:
            data = json.loads(definitions_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        for record in data.get("definitions", []) or []:
            if not isinstance(record, dict):
                continue
            pair = _first_source_column(record)
            if not pair:
                continue
            # A workspace definition is deliberately KPI-independent; it applies
            # wherever the feature appears. Recorded once, with no KPI context,
            # so the accuracy harness scores it on the feature name alone --
            # which is exactly the claim a workspace-level definition makes.
            out.append(LabelledMapping(
                workspace=workspace_rel,
                kpi_id="",
                feature=str(record.get("feature") or ""),
                context=str(record.get("definition") or record.get("feature") or ""),
                expected_dataset=pair[0],
                expected_column=pair[1],
                label_source="workspace_definition",
            ))

    # Same feature confirmed twice (once per KPI, once workspace-wide) is one
    # label, not two -- otherwise a workspace that answered one question
    # thoroughly would outweigh one that answered five.
    seen: dict[str, LabelledMapping] = {}
    for item in out:
        seen.setdefault(f"{item.key()}|{item.expected_column}", item)
    return sorted(seen.values(), key=lambda m: (m.workspace, m.kpi_id, m.feature))


def harvest_all(repo_root: Path | str = PROJECT_ROOT) -> list[LabelledMapping]:
    """Every human-confirmed mapping across every workspace."""
    root = Path(repo_root).resolve()
    workspaces_dir = root / "workspaces"
    if not workspaces_dir.is_dir():
        return []
    out: list[LabelledMapping] = []
    for child in sorted(workspaces_dir.iterdir()):
        if child.is_dir() and not child.name.startswith((".", "_")):
            out.extend(harvest_workspace(root, f"workspaces/{child.name}"))
    return out
