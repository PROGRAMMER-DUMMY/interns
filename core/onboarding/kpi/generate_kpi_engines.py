"""Per-KPI multi-engine code generation.

SQL is always the baseline. This command lets a user opt into more engines
without changing the SQL-only default of the individual generators. For each
KPI it selects one or more engines per the `--engine` option and calls the
matching generator(s):

  sql        → SQL only (the default, always the correctness baseline)
  polars     → Polars only
  pyspark    → PySpark only
  recommended→ per-KPI: SQL always, plus the recommender's recommended engine
               when it differs from SQL (hybrid ⇒ SQL + Polars). The
               recommender's reason is carried into the report.
  all        → SQL + Polars + PySpark for every KPI
  <list>     → a comma list (e.g. "sql,polars") = exactly those engines

When a generator raises for a given KPI (e.g. an unsupported window pattern or
a derived-formula feature the imperative engines cannot yet translate), that
engine is skipped for that KPI and the skip + reason is recorded — the run as a
whole does not crash.

Output is a summary report written to
``interns/reports/engine_generation/current.json`` and ``current.md``. It lists,
per KPI: the engines generated with their file paths, the recommended engine and
its reason, and any skips with reasons. No domain vocabulary is hardcoded; all
KPI selection comes from the feature mapping.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.onboarding.kpi.engine_recommender import KPIEngineRecommender
from core.onboarding.kpi.polars_generator import PolarsKPIGenerator
from core.onboarding.kpi.pyspark_generator import PySparkKPIGenerator
from core.onboarding.kpi.sql_generator import DuckDBKPISQLGenerator, _rel
from core.storage.workspace_layout import WorkspaceLayout

# The concrete engines a generator exists for. "hybrid" is an advisory label
# from the recommender that expands to these concrete engines.
_CONCRETE_ENGINES = ("sql", "polars", "pyspark")
_HYBRID_ENGINES = ("sql", "polars")
# Modes the CLI accepts beyond an explicit comma list of concrete engines.
_MODE_ALIASES = {
    "sql": ("sql",),
    "polars": ("polars",),
    "pyspark": ("pyspark",),
    "all": _CONCRETE_ENGINES,
}
_RECOMMENDED_MODE = "recommended"


@dataclass(frozen=True)
class EngineGenerationOutcome:
    """One engine attempt for one KPI."""

    engine: str
    status: str  # "generated" | "skipped"
    path: str = ""
    reason: str = ""

    def summary(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KPIGenerationOutcome:
    """All engine attempts for one KPI, plus the recommendation context."""

    kpi_id: str
    recommended_engine: str
    recommendation_reasons: list[str] = field(default_factory=list)
    engines_generated: list[str] = field(default_factory=list)
    outcomes: list[EngineGenerationOutcome] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "kpi_id": self.kpi_id,
            "recommended_engine": self.recommended_engine,
            "recommendation_reasons": list(self.recommendation_reasons),
            "engines_generated": list(self.engines_generated),
            "files": [o.path for o in self.outcomes if o.status == "generated"],
            "outcomes": [o.summary() for o in self.outcomes],
        }


def expand_engine_mode(mode: str, recommended_engine: str) -> list[str]:
    """Resolve the requested ``--engine`` mode into concrete engines for a KPI.

    ``recommended`` is per-KPI, so the recommended engine is passed in. Any
    "hybrid" advisory (from the mode or the recommender) expands to SQL+Polars.
    SQL is always included for the recommended mode so the baseline is never
    dropped. Order is preserved and de-duplicated.
    """
    raw = (mode or "sql").strip().lower()

    if raw == _RECOMMENDED_MODE:
        engines = ["sql"]
        engines.extend(_expand_label(recommended_engine))
        return _dedupe(engines)

    if raw in _MODE_ALIASES:
        return list(_MODE_ALIASES[raw])

    # Comma list of concrete engines / advisory labels.
    engines: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if token:
            engines.extend(_expand_label(token))
    if not engines:
        raise ValueError(f"No valid engines parsed from --engine {mode!r}")
    return _dedupe(engines)


def _expand_label(label: str) -> list[str]:
    label = (label or "").strip().lower()
    if label == "hybrid":
        return list(_HYBRID_ENGINES)
    if label in _CONCRETE_ENGINES:
        return [label]
    raise ValueError(f"Unsupported engine label: {label!r}")


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


class KPIMultiEngineGenerator:
    """Generate KPI code for the recommended engine, all engines, or a list."""

    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self._generators: dict[str, Callable[[str], Any]] = {
            "sql": lambda kpi_id: DuckDBKPISQLGenerator(self.repo_root, workspace).generate(kpi_id),
            "polars": lambda kpi_id: PolarsKPIGenerator(self.repo_root, workspace).generate(kpi_id),
            "pyspark": lambda kpi_id: PySparkKPIGenerator(self.repo_root, workspace).generate(kpi_id),
        }

    # ── generation ───────────────────────────────────────────────────────────

    def generate_all(
        self,
        *,
        engine: str = "sql",
        kpi_id: str | None = None,
        local_blocked: bool = False,
    ) -> list[KPIGenerationOutcome]:
        recommendations = {
            rec.kpi_id: rec
            for rec in KPIEngineRecommender(self.repo_root, self.workspace).recommend_all(
                local_blocked=local_blocked
            )
        }
        kpi_ids = self._kpi_ids()
        if kpi_id is not None:
            if kpi_id not in kpi_ids:
                raise ValueError(f"KPI not found in feature mapping: {kpi_id}")
            kpi_ids = [kpi_id]

        outcomes: list[KPIGenerationOutcome] = []
        for kid in kpi_ids:
            rec = recommendations.get(kid)
            recommended = rec.recommended_engine if rec else "sql"
            reasons = list(rec.reasons) if rec else []
            engines = expand_engine_mode(engine, recommended)
            outcomes.append(self._generate_one(kid, engines, recommended, reasons))
        return outcomes

    def _generate_one(
        self,
        kpi_id: str,
        engines: list[str],
        recommended_engine: str,
        recommendation_reasons: list[str],
    ) -> KPIGenerationOutcome:
        attempts: list[EngineGenerationOutcome] = []
        generated: list[str] = []
        for eng in engines:
            generator = self._generators.get(eng)
            if generator is None:
                attempts.append(
                    EngineGenerationOutcome(
                        engine=eng, status="skipped", reason=f"no generator for engine {eng!r}"
                    )
                )
                continue
            try:
                result = generator(kpi_id)
            except Exception as exc:  # generator-specific block (e.g. unsupported window)
                attempts.append(
                    EngineGenerationOutcome(engine=eng, status="skipped", reason=str(exc))
                )
                continue
            attempts.append(
                EngineGenerationOutcome(
                    engine=eng, status="generated", path=getattr(result, "path", "")
                )
            )
            generated.append(eng)
        return KPIGenerationOutcome(
            kpi_id=kpi_id,
            recommended_engine=recommended_engine,
            recommendation_reasons=recommendation_reasons,
            engines_generated=generated,
            outcomes=attempts,
        )

    # ── reporting ──────────────────────────────────────────────────────────────

    def write(
        self,
        *,
        engine: str = "sql",
        kpi_id: str | None = None,
        local_blocked: bool = False,
    ) -> dict[str, Any]:
        outcomes = self.generate_all(engine=engine, kpi_id=kpi_id, local_blocked=local_blocked)
        from core.onboarding.workspace.delegation import routing_for

        _routing = routing_for("engine_generation")
        payload = {
            "artifact_type": "engine_generation.json",
            "version": 1,
            "workspace": _rel(self.workspace, self.repo_root),
            "engine_mode": engine,
            "default_engine": "sql",
            "required_specialists": _routing["agents"],
            "suggested_skills": _routing["skills"],
            "kpis": [o.summary() for o in outcomes],
        }
        self.layout.ensure_runtime_dirs()
        report_dir = self.layout.reports_dir / "engine_generation"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "current.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (report_dir / "current.md").write_text(_render_md(engine, outcomes), encoding="utf-8")
        payload["report_json_path"] = _rel(report_dir / "current.json", self.repo_root)
        payload["report_markdown_path"] = _rel(report_dir / "current.md", self.repo_root)
        return payload

    def _kpi_ids(self) -> list[str]:
        path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        if not path.exists():
            raise FileNotFoundError(f"feature mapping not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(k.get("kpi_id") or "") for k in data.get("kpis", []) if k.get("kpi_id")]


def _render_md(engine: str, outcomes: list[KPIGenerationOutcome]) -> str:
    lines = [
        "# KPI Engine Generation",
        "",
        f"Engine mode: **{engine}**. SQL is always the baseline; heavier engines are opt-in.",
        "",
        "| KPI | Recommended | Engines generated | Files | Skips | Why |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for outcome in outcomes:
        files = "<br>".join(o.path for o in outcome.outcomes if o.status == "generated") or "—"
        skips = (
            "<br>".join(
                f"{o.engine}: {o.reason}" for o in outcome.outcomes if o.status == "skipped"
            )
            or "—"
        )
        why = "; ".join(outcome.recommendation_reasons) or "—"
        generated = ", ".join(outcome.engines_generated) or "—"
        lines.append(
            f"| {outcome.kpi_id} | {outcome.recommended_engine} | {generated} "
            f"| {files} | {skips} | {why} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate KPI code for the recommended engine (or all engines, or a chosen "
            "list) per KPI. SQL is always the baseline."
        )
    )
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to cwd.")
    parser.add_argument(
        "--engine",
        default="sql",
        help=(
            "sql (default) | polars | pyspark | recommended | all | a comma list "
            "such as sql,polars. 'recommended' is per-KPI (SQL + the recommended engine; "
            "hybrid => sql,polars)."
        ),
    )
    parser.add_argument("--kpi-id", default=None, help="Generate only this KPI (default: all).")
    parser.add_argument(
        "--local-blocked",
        action="store_true",
        help="Signal local execution is disallowed (steers 'recommended' toward PySpark).",
    )
    args = parser.parse_args(argv)
    payload = KPIMultiEngineGenerator(args.repo_root, args.workspace).write(
        engine=args.engine,
        kpi_id=args.kpi_id,
        local_blocked=args.local_blocked,
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
