"""Score the feature resolver against human-confirmed mappings.

Grades `contextual_column_candidates` -- the ranking function `_contextual_score`
feeds -- against the corpus in `core.dev.resolver_corpus`. That function IS the
inference under question; running the whole resolver instead would re-derive
profiles and measure onboarding.

Three outcomes per label, deliberately not two:

  * `correct`     -- the top candidate is the column the human confirmed.
  * `wrong`       -- a different column outranked it. The expensive failure.
  * `abstained`   -- no candidate cleared the score floor, so the resolver would
                    have raised a blocker instead of answering.

An abstention is NOT a miss. This platform's whole posture is that refusing
beats guessing, and folding abstentions into "wrong" would reward a resolver
that answers everything badly over one that answers less and knows it. They are
reported separately and gated separately: `wrong` must not increase, and
`correct` must not decrease.

`answer_rate` (correct+wrong over total) is the number to push UP over time;
`precision` (correct over answered) is the number that must never fall.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.dev.resolver_corpus import LabelledMapping, harvest_all
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

BASELINE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "resolver_accuracy_baseline.json"


@dataclass
class Grade:
    label: LabelledMapping
    outcome: str  # correct | wrong | abstained
    predicted_dataset: str = ""
    predicted_column: str = ""
    score: float = 0.0
    rank_of_expected: int | None = None  # 1-based; None = never surfaced

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.label.workspace,
            "kpi_id": self.label.kpi_id,
            "feature": self.label.feature,
            "expected": f"{_short(self.label.expected_dataset)}.{self.label.expected_column}",
            "predicted": (
                f"{_short(self.predicted_dataset)}.{self.predicted_column}"
                if self.predicted_column else ""
            ),
            "outcome": self.outcome,
            "score": round(self.score, 2),
            "rank_of_expected": self.rank_of_expected,
        }


@dataclass
class AccuracyReport:
    grades: list[Grade] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.grades)

    @property
    def correct(self) -> int:
        return sum(1 for g in self.grades if g.outcome == "correct")

    @property
    def wrong(self) -> int:
        return sum(1 for g in self.grades if g.outcome == "wrong")

    @property
    def abstained(self) -> int:
        return sum(1 for g in self.grades if g.outcome == "abstained")

    @property
    def answered(self) -> int:
        return self.correct + self.wrong

    @property
    def precision(self) -> float:
        """Of the ones it answered, how many were right. Must never fall."""
        return round(self.correct / self.answered, 4) if self.answered else 0.0

    @property
    def answer_rate(self) -> float:
        """How often it answered at all. The number to push up."""
        return round(self.answered / self.total, 4) if self.total else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "resolver_accuracy",
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total": self.total,
            "correct": self.correct,
            "wrong": self.wrong,
            "abstained": self.abstained,
            "precision": self.precision,
            "answer_rate": self.answer_rate,
            "grades": [g.to_dict() for g in self.grades],
        }


def _short(raw: str) -> str:
    from core.profiling.dataset_identity import dataset_display_stem

    return dataset_display_stem(str(raw or "")) or str(raw or "")


def _same_column(a_dataset: str, a_column: str, b_dataset: str, b_column: str) -> bool:
    """Compared on the SHORT dataset name: the corpus records a fully-qualified
    UC identifier while the schema index may carry either form."""
    return (
        a_column.strip("`").lower() == b_column.strip("`").lower()
        and _short(a_dataset).lower() == _short(b_dataset).lower()
    )


def grade(labels: list[LabelledMapping], repo_root: Path | str = PROJECT_ROOT) -> AccuracyReport:
    from core.onboarding.kpi.feature_resolver import contextual_column_candidates
    from core.onboarding.relationships.schema_alias_matching import load_schema_index

    root = Path(repo_root).resolve()
    report = AccuracyReport()
    index_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}

    for label in labels:
        if label.workspace not in index_cache:
            layout = WorkspaceLayout(project_root=(root / label.workspace).resolve())
            index_cache[label.workspace] = load_schema_index(
                layout.profiles_dir / "profile_index.json"
            )
        candidates = contextual_column_candidates(
            label.feature, label.context, index_cache[label.workspace]
        )
        if not candidates:
            report.grades.append(Grade(label=label, outcome="abstained"))
            continue
        top = candidates[0]
        rank = next(
            (
                i + 1
                for i, c in enumerate(candidates)
                if _same_column(
                    str(c.get("dataset") or ""), str(c.get("column") or ""),
                    label.expected_dataset, label.expected_column,
                )
            ),
            None,
        )
        hit = _same_column(
            str(top.get("dataset") or ""), str(top.get("column") or ""),
            label.expected_dataset, label.expected_column,
        )
        report.grades.append(Grade(
            label=label,
            outcome="correct" if hit else "wrong",
            predicted_dataset=str(top.get("dataset") or ""),
            predicted_column=str(top.get("column") or ""),
            score=float(top.get("score") or 0.0),
            rank_of_expected=rank,
        ))
    return report


def load_baseline() -> dict[str, Any]:
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_baseline(report: AccuracyReport) -> Path:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps({
            "artifact_type": "resolver_accuracy_baseline",
            "version": 1,
            "note": (
                "Recorded by `resolver-accuracy --write-baseline`. `wrong` must "
                "not increase and `correct` must not decrease; an abstention is "
                "not a miss. Small corpus -- enough to catch a regression, not "
                "enough to publish an accuracy figure from."
            ),
            "total": report.total,
            "correct": report.correct,
            "wrong": report.wrong,
            "abstained": report.abstained,
            "precision": report.precision,
            "answer_rate": report.answer_rate,
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return BASELINE_PATH


def regressions(report: AccuracyReport, baseline: dict[str, Any]) -> list[str]:
    """What got worse. Empty when nothing did (including an empty baseline)."""
    if not baseline:
        return []
    out: list[str] = []
    if report.wrong > int(baseline.get("wrong", 0)):
        out.append(
            f"wrong answers rose {baseline.get('wrong')} -> {report.wrong}: the "
            "resolver now confidently picks a column a human rejected"
        )
    if report.correct < int(baseline.get("correct", 0)):
        out.append(
            f"correct answers fell {baseline.get('correct')} -> {report.correct}"
        )
    return out


def render_markdown(report: AccuracyReport, baseline: dict[str, Any]) -> str:
    lines = [
        "# Resolver accuracy",
        "",
        f"- labels: **{report.total}** (human-confirmed only)",
        f"- correct: **{report.correct}** | wrong: **{report.wrong}** | "
        f"abstained: **{report.abstained}**",
        f"- precision (of answered): **{report.precision:.0%}** | "
        f"answer rate: **{report.answer_rate:.0%}**",
        "",
        "An abstention is a blocker raised, not a miss. Precision must never "
        "fall; answer rate is the number to push up.",
        "",
        "| KPI | Feature | Expected | Predicted | Outcome | Score | Rank of expected |",
        "|---|---|---|---|---|---|---|",
    ]
    for g in report.grades:
        d = g.to_dict()
        marker = {"correct": "[ok]", "wrong": "[x]", "abstained": "[~]"}[d["outcome"]]
        lines.append(
            f"| `{d['kpi_id'] or '-'}` | `{d['feature']}` | `{d['expected']}` | "
            f"`{d['predicted'] or '-'}` | {marker} {d['outcome']} | {d['score']} | "
            f"{d['rank_of_expected'] if d['rank_of_expected'] else '-'} |"
        )
    lines.append("")
    if baseline:
        lines += [
            f"Baseline: correct {baseline.get('correct')}, wrong "
            f"{baseline.get('wrong')}, abstained {baseline.get('abstained')}.",
            "",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grade the feature resolver against human-confirmed mappings.",
    )
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--write-baseline", action="store_true",
        help="record the current numbers as the non-regression floor",
    )
    args = parser.parse_args(argv)

    labels = harvest_all(args.repo_root)
    report = grade(labels, args.repo_root)
    baseline = load_baseline()

    if args.write_baseline:
        path = write_baseline(report)
        print(f"[ok] baseline written to {path}")
        return 0

    if args.json:
        print(json.dumps(report.summary(), indent=2))
    else:
        print(render_markdown(report, baseline))

    failures = regressions(report, baseline)
    for failure in failures:
        print(f"[x] {failure}")
    if not labels:
        # Not a pass: an empty corpus means nobody has confirmed anything, so
        # the resolver is ungraded. Silence here would read as "all good".
        print(
            "[~] no human-confirmed mappings found -- the resolver is UNGRADED. "
            "Answer a blocker panel with `--confirmed-by` to create a label."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
