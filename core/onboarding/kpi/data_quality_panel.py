"""Data-quality rules authored the same way KPI features already are: ask,
in plain English, with evidence-backed JSON options, human-gated with
Human-Gate Provenance -- not a separate, bespoke DQ questionnaire.

Fires per (dataset, column) actually used by a KPI ready for dbt generation,
only when profiling evidence makes the column's validity rule genuinely
ambiguous (observed nulls, or a small enough observed distinct-value set that
an accepted-values list is a real candidate) -- the same "ask, don't assume"
trigger AGENTS.md already mandates for KPI blockers, not a blanket
questionnaire over every column. Mirrors data_source_panel.py's builder/
recorder shape and blocker_question_panel.py's JSON-backed-options panel
envelope (current.json/current.md, register_contract, run_workspace_command).

An accepted answer is recorded into data_quality_decisions.json (a durable,
human-gated contract, like workspace_feature_definitions.json) --
dbt_project_generator.py reads it to emit real dbt schema tests
(not_null/accepted_values), never hand-maintained YAML a human edits
directly. Severity is tiered from the answer, not hardcoded: an
accepted-values/not-null check backing a KPI's actual join/aggregation key
is `severity: error` (a violation would make the KPI number wrong); anything
else defaults to `severity: warn` (surfaces drift without blocking the mart).
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.contracts.versioning import register_contract
from core.governance.provenance import decision_source as decision_source_for
from core.onboarding.kpi.sql_generator import READY_STATES
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

PANEL_VERSION = 1
register_contract("data_quality_panel/current.json", current_version=PANEL_VERSION)

# A column's role in a KPI decides the default severity a human is offered --
# not a free-standing setting. A key used to JOIN or GROUP BY is where a
# violation silently makes the number wrong; a plain projected value is where
# a violation is drift worth surfacing but not blocking on.
_KEY_LIKE_SUFFIXES = ("id", "key", "code")


@dataclass(frozen=True)
class DataQualityPanelResult:
    current_json_path: str
    current_markdown_path: str
    status: str
    remaining_count: int

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


class DataQualityPanelBuilder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def prepare(self) -> DataQualityPanelResult:
        self.layout.ensure_runtime_dirs()
        candidates = self._candidates()
        decided = _decided_keys(self.layout)
        pending = [c for c in candidates if _candidate_key(c) not in decided]

        panel_dir = self.layout.reports_dir / "data_quality_panel"
        panel_dir.mkdir(parents=True, exist_ok=True)
        current_json = panel_dir / "current.json"
        current_md = panel_dir / "current.md"

        if not pending:
            panel = {
                "artifact_type": "data_quality_panel/current.json",
                "version": PANEL_VERSION,
                "generated_by": "prepare-data-quality-panel",
                "status": "no_pending_checks",
                "kpi_id": "",
                "dataset": "",
                "column": "",
            }
            current_json.write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
            current_md.write_text("# Data Quality\n\nNo pending data-quality checks.\n", encoding="utf-8")
            return DataQualityPanelResult(
                _rel(current_json, self.repo_root), _rel(current_md, self.repo_root),
                "no_pending_checks", 0,
            )

        candidate = pending[0]
        panel = _build_panel(candidate)
        current_json.write_text(json.dumps(panel, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(_render_markdown(panel), encoding="utf-8")
        return DataQualityPanelResult(
            _rel(current_json, self.repo_root), _rel(current_md, self.repo_root),
            "needs_user_answer", len(pending),
        )

    def _candidates(self) -> list[dict[str, Any]]:
        """(kpi_id, dataset, column) triples from every ready KPI's resolved
        features, deduped by (dataset, column) -- a column shared by two
        KPIs gets one question, not two. Only columns with a real ambiguity
        signal in the profile (nulls observed, or a small enough observed
        distinct set) are candidates; a clean, unambiguous column is not
        asked about at all.
        """
        mapping_path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        if not mapping_path.exists():
            return []
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        profile_by_path = _profile_by_path(self.layout)

        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for kpi in mapping.get("kpis", []):
            features = kpi.get("features") or []
            if not features or any(f.get("state") not in READY_STATES for f in features):
                continue
            kpi_id = str(kpi.get("kpi_id") or "")
            for feature in features:
                for sc in feature.get("source_columns") or []:
                    dataset = str((sc or {}).get("dataset") or "")
                    column = str((sc or {}).get("column") or "")
                    if not dataset or not column or (dataset, column) in seen:
                        continue
                    seen.add((dataset, column))
                    profile = profile_by_path.get(dataset)
                    if not profile:
                        continue
                    ambiguity = _ambiguity_signal(profile, column)
                    if not ambiguity:
                        continue
                    out.append(
                        {
                            "kpi_id": kpi_id,
                            "dataset": dataset,
                            "column": column,
                            "feature": str(feature.get("feature") or column),
                            "is_key_like": column.lower().endswith(_KEY_LIKE_SUFFIXES),
                            **ambiguity,
                        }
                    )
        return out


class DataQualityAnswerRecorder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def apply(self, answer: str, *, confirmed_by: str = "") -> dict[str, Any]:
        panel_path = self.layout.reports_dir / "data_quality_panel" / "current.json"
        if not panel_path.exists():
            raise FileNotFoundError(
                "no data_quality_panel/current.json -- run prepare-data-quality-panel first"
            )
        panel = json.loads(panel_path.read_text(encoding="utf-8"))
        if panel.get("status") != "needs_user_answer":
            raise ValueError("data_quality_panel has no pending question to answer")
        options = {opt["option_id"]: opt for opt in panel.get("options") or []}
        normalized = answer.strip().lower()
        option = options.get(normalized) or next(
            (o for o in options.values() if str(o.get("label") or "").strip().lower() == normalized),
            None,
        )
        if option is None:
            raise ValueError(f"--answer must be one of {sorted(options)}, got {answer!r}")

        confirmed_by = (confirmed_by or "").strip()
        decision_source = decision_source_for(confirmed_by)
        now = datetime.now(timezone.utc).isoformat()

        path = self.layout.contracts_dir / "data_quality_decisions.json"
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                data = {}
        decisions = data.get("decisions")
        if not isinstance(decisions, list):
            decisions = []
        decisions.append(
            {
                "kpi_id": panel.get("kpi_id", ""),
                "dataset": panel.get("dataset", ""),
                "column": panel.get("column", ""),
                "check_type": option.get("check_type", ""),
                "check_config": option.get("check_config", {}),
                "severity": option.get("severity", "warn"),
                "answer": normalized,
                "source": decision_source,
                "confirmed_by": confirmed_by,
                "confirmed_at": now,
            }
        )
        data.update(
            {
                "artifact_type": "data_quality_decisions.json",
                "version": 1,
                "generated_by": "apply-data-quality-answer",
                "workspace": _rel(self.workspace, self.repo_root),
                "decisions": decisions,
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        next_panel = DataQualityPanelBuilder(self.repo_root, _rel(self.workspace, self.repo_root)).prepare()
        return {
            "decisions_path": _rel(path, self.repo_root),
            "check_type": option.get("check_type", ""),
            "severity": option.get("severity", "warn"),
            "source": decision_source,
            "confirmed_by": confirmed_by,
            "next_panel": next_panel.summary(),
        }


def _profile_by_path(layout: WorkspaceLayout) -> dict[str, dict[str, Any]]:
    if not layout.profile_index_path.exists():
        return {}
    try:
        data = json.loads(layout.profile_index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out = {}
    for profile in data.get("profiles") or []:
        if isinstance(profile, dict) and profile.get("path"):
            out[str(profile["path"])] = profile
    return out


def _column_stats(profile: dict[str, Any], column: str) -> dict[str, Any] | None:
    for entry in profile.get("columns") or []:
        if isinstance(entry, dict) and str(entry.get("name") or "") == column:
            return entry
    return None


# A column's dtype string comes from either profiler: the local profiler
# emits Polars-style names ("String", "Float64"); databricks_table_profiler
# emits raw Spark SQL DESCRIBE types ("string", "double", "decimal(10,2)").
# Different casing/vocabulary, same purpose here: exclude anything numeric/
# temporal/boolean from the accepted-values candidate signal below -- a
# CONTINUOUS column's small sample in a small dataset is an artifact of
# sample size, not evidence it's genuinely categorical. Found live: a
# currency column (PaidAmount) in a 2-row test fixture had exactly 2 sample
# values, which would otherwise look identical to a real 2-value category.
_NON_CATEGORICAL_DTYPE_MARKERS = (
    "int", "float", "double", "decimal", "long", "bigint", "numeric",
    "real", "bool", "date", "time",
)


def _looks_categorical(dtype: str) -> bool:
    lowered = str(dtype or "").lower()
    return bool(lowered) and not any(marker in lowered for marker in _NON_CATEGORICAL_DTYPE_MARKERS)


def _ambiguity_signal(profile: dict[str, Any], column: str) -> dict[str, Any] | None:
    """Evidence-based candidate signal for one column, or None if the
    column's profile shows nothing ambiguous worth asking about.
    Workspace-agnostic: pure profile-stat thresholds, no column/table name
    special-cased.
    """
    stats = _column_stats(profile, column)
    if not stats:
        return None
    null_count = stats.get("null_count")
    sample_values = [v for v in (stats.get("sample_values") or []) if v not in (None, "")]
    row_count = profile.get("row_count")
    if isinstance(null_count, int) and null_count > 0:
        null_rate = (null_count / row_count) if isinstance(row_count, int) and row_count else None
        return {
            "signal": "nulls_observed",
            "evidence": {
                "null_count": null_count,
                "row_count": row_count,
                "null_rate": round(null_rate, 4) if null_rate is not None else None,
            },
        }
    # A small observed distinct-value set (bounded profiling sample, so this
    # is a CANDIDATE accepted-values list, not proof of exhaustiveness -- the
    # panel says so explicitly and the human confirms or widens it). Only
    # offered for columns that actually look categorical (string-typed).
    if _looks_categorical(str(stats.get("dtype") or "")) and 0 < len(sample_values) <= 8:
        return {
            "signal": "small_observed_value_set",
            "evidence": {"sample_values": sample_values, "row_count": row_count},
        }
    return None


def _candidate_key(candidate: dict[str, Any]) -> tuple[str, str]:
    return (candidate["dataset"], candidate["column"])


def _decided_keys(layout: WorkspaceLayout) -> set[tuple[str, str]]:
    path = layout.contracts_dir / "data_quality_decisions.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {
        (str(d.get("dataset") or ""), str(d.get("column") or ""))
        for d in (data.get("decisions") or [])
        if isinstance(d, dict)
    }


def _build_panel(candidate: dict[str, Any]) -> dict[str, Any]:
    # error/warn are ALWAYS both offered as real, independent choices --
    # is_key_like only marks which one is labeled "recommended" (a join/
    # grain key breaking silently makes a KPI number wrong; a plain
    # projected value is lower stakes), it must never eliminate the error
    # choice for a column whose name doesn't happen to end in id/key/code.
    # Found live: an earlier version made option_a's severity conditional
    # on is_key_like, which meant a non-key-like column (e.g. a KPI's own
    # GROUP BY cut, "LineOfBusiness") had NO way to pick error severity at
    # all -- both options silently collapsed to warn.
    dataset, column = candidate["dataset"], candidate["column"]
    recommended = "option_a" if candidate["is_key_like"] else "option_b"
    options: list[dict[str, Any]] = []

    if candidate["signal"] == "nulls_observed":
        ev = candidate["evidence"]
        options = [
            {
                "option_id": "option_a",
                "label": f"Reject nulls -- {column} must always be present",
                "description": f"Observed {ev['null_count']} null row(s) out of {ev.get('row_count')}. "
                "A not_null test fails the build if this recurs.",
                "check_type": "not_null",
                "check_config": {},
                "severity": "error",
            },
            {
                "option_id": "option_b",
                "label": f"Nulls are expected -- warn only, don't block",
                "description": "Same not_null test, but as a warning: surfaces drift without blocking the mart.",
                "check_type": "not_null",
                "check_config": {},
                "severity": "warn",
            },
            {
                "option_id": "skip",
                "label": "No check needed for this column",
                "description": "Skip -- no data-quality test generated for this column.",
                "check_type": "",
                "check_config": {},
                "severity": "warn",
            },
        ]
    else:  # small_observed_value_set
        ev = candidate["evidence"]
        values = ev["sample_values"]
        options = [
            {
                "option_id": "option_a",
                "label": f"Accepted values are exactly: {', '.join(str(v) for v in values)}",
                "description": "An accepted_values test fails the build if any other value appears.",
                "check_type": "accepted_values",
                "check_config": {"values": values},
                "severity": "error",
            },
            {
                "option_id": "option_b",
                "label": "These are common values but not exhaustive -- warn only, don't block",
                "description": "Same accepted_values test, but as a warning: new values are expected over time.",
                "check_type": "accepted_values",
                "check_config": {"values": values},
                "severity": "warn",
            },
            {
                "option_id": "skip",
                "label": "No check needed for this column",
                "description": "Skip -- no data-quality test generated for this column.",
                "check_type": "",
                "check_config": {},
                "severity": "warn",
            },
        ]

    return {
        "artifact_type": "data_quality_panel/current.json",
        "version": PANEL_VERSION,
        "generated_by": "prepare-data-quality-panel",
        "status": "needs_user_answer",
        "kpi_id": candidate["kpi_id"],
        "dataset": dataset,
        "column": column,
        "feature": candidate["feature"],
        "question": f"What does 'valid' mean for `{dataset}`.`{column}`?",
        "evidence": candidate["evidence"],
        "recommended_option_id": recommended,
        "options": options,
        "apply_command": (
            "uv run apply-data-quality-answer --workspace <workspace> "
            "--answer <option_id> --confirmed-by \"<name>\""
        ),
    }


def _render_markdown(panel: dict[str, Any]) -> str:
    lines = [
        f"# Data Quality: `{panel['dataset']}`.`{panel['column']}`",
        "",
        f"KPI: `{panel['kpi_id']}` · Feature: `{panel['feature']}`",
        "",
        "## Decide",
        "",
        panel["question"],
        "",
        "## Evidence",
        "",
        "```json",
        json.dumps(panel["evidence"], indent=2),
        "```",
        "",
        "## Options",
        "",
    ]
    for option in panel["options"]:
        lines.append(
            f"- `{option['option_id']}` [{option['severity']}]: {option['label']} -- {option['description']}"
        )
    lines.extend(["", "## Apply", "", f"```powershell\n{panel['apply_command']}\n```", ""])
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@anchored("prepare-data-quality-panel")
def panel_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="prepare-data-quality-panel",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: DataQualityPanelBuilder(args.repo_root, args.workspace).prepare(),
        validation="validate-workspace-artifacts",
    )


@anchored("apply-data-quality-answer")
def apply_main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--answer", required=True)
    parser.add_argument("--confirmed-by", default="")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="apply-data-quality-answer",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: DataQualityAnswerRecorder(args.repo_root, args.workspace).apply(
            args.answer, confirmed_by=args.confirmed_by
        ),
        op_args={"workspace": args.workspace, "answer": args.answer},
        allow_replay=args.allow_replay,
        decision=args.answer,
        metadata={"answer": args.answer, "confirmed_by": args.confirmed_by},
        record_idempotent=True,
    )


if __name__ == "__main__":
    raise SystemExit(panel_main())
