"""Polars KPI generator — produces a streaming-capable Python/Polars script.

Renders from the shared engine-neutral intent (`kpi_intent.parse_intent`) so it
agrees with the SQL and PySpark paths on metric, cuts, time buckets, age
derivation, and filters. Output uses the Polars lazy API so it can stream
large Parquet/CSV. Paths are anchored on ``__file__`` (portable — no absolute
machine paths baked in).
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from core.onboarding.kpi.feature_resolver import READY_STATES
from core.onboarding.kpi.kpi_intent import KPIIntent, parse_intent
from core.onboarding.relationships.contracts import (
    find_executable_relationship,
    load_relationship_contracts,
)
from core.onboarding.kpi.sql_generator import (
    _choose_base_source,
    _feature_source_refs,
    _unique_preserve_order,
    _repo_path,
    _rel,
    _safe_name,
)
from core.storage.workspace_layout import WorkspaceLayout

_PL_EVERY = {"year": "1y", "quarter": "1q", "month": "1mo", "week": "1w", "day": "1d"}
_PL_OPS = {"=": "==", "==": "==", "!=": "!=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}


@dataclass(frozen=True)
class PolarsGenerationResult:
    path: str
    kpi_id: str
    status: str
    engine: str = "polars"

    def summary(self) -> dict[str, Any]:
        return {**self.__dict__}


class PolarsKPIGenerator:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        dialect: str = "local",   # local | databricks
        catalog: str = "workspace",
        schema: str = "autoresearch",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.dialect = dialect
        self.catalog = catalog
        self.schema = schema

    def generate(self, kpi_id: str) -> PolarsGenerationResult:
        mapping = self._load_mapping()
        kpi = next((k for k in mapping.get("kpis", []) if k.get("kpi_id") == kpi_id), None)
        if not kpi:
            raise ValueError(f"KPI not found: {kpi_id}")
        blocked = [f for f in kpi.get("features", []) if f.get("state") not in READY_STATES]
        if blocked:
            names = ", ".join(f.get("feature", "") for f in blocked)
            raise ValueError(f"KPI {kpi_id} has unresolved features: {names}")
        derived = [f for f in kpi.get("features", []) if f.get("resolution_type") == "derived_formula"]
        if derived:
            names = ", ".join(f.get("feature", "") for f in derived)
            raise ValueError(
                f"KPI {kpi_id} uses derived-formula feature(s) `{names}`; Polars generation does "
                "not yet translate derived formulas. Generate SQL for this KPI or add a Polars "
                "derivation rule before emitting a Polars script."
            )

        profile_map = self._profile_map()
        relationships = load_relationship_contracts(
            self.repo_root, _rel(self.workspace, self.repo_root)
        )
        feature_refs = [
            ref
            for feature in kpi.get("features", [])
            for ref in _feature_source_refs(feature, self.repo_root)
        ]
        base_source = _choose_base_source(feature_refs, profile_map)
        if not base_source:
            raise ValueError(f"Cannot determine base source for KPI {kpi_id}")
        required_sources, source_aliases = self._build_source_plan(base_source, feature_refs)

        intent = parse_intent(kpi)
        if intent.unsupported_window:
            raise ValueError(
                f"KPI {kpi_id} uses window pattern `{intent.unsupported_window}` not yet supported "
                "in Polars generation. Generate SQL for this KPI, or extend the Polars renderer."
            )
        code = self._emit_script(
            kpi, kpi_id, intent, required_sources, source_aliases, base_source, relationships
        )
        suffix = "" if self.dialect == "local" else f"_{self.dialect}"
        out = self.layout.solutions_dir / f"{kpi_id}_polars{suffix}.py"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(code, encoding="utf-8")
        return PolarsGenerationResult(path=_rel(out, self.repo_root), kpi_id=kpi_id, status="generated")

    # ── code emission ──────────────────────────────────────────────────────────

    def _emit_script(
        self,
        kpi: dict[str, Any],
        kpi_id: str,
        intent: KPIIntent,
        required_sources: list[str],
        source_aliases: dict[str, str],
        base_source: str,
        relationships: list[dict[str, Any]],
    ) -> str:
        lines: list[str] = [
            "# Polars KPI script — generated from feature mappings + shared KPI intent.",
            f"# KPI: {kpi.get('name', kpi_id)}",
            f"# Metric: {kpi.get('metric', '')}",
            f"# Cuts: {kpi.get('cuts', '')}",
            f"# Generated: {date.today().isoformat()}",
            "# Streaming-capable via lazy API. Paths are anchored on __file__ (portable).",
            "",
            "import sys",
            "import polars as pl",
            "from datetime import date",
            "from pathlib import Path",
            "",
            "try:  # portable preview on Windows consoles (cp1252) — Polars uses Unicode tables",
            '    sys.stdout.reconfigure(encoding="utf-8")',
            "except Exception:",
            "    pass",
            "",
            "_HERE = Path(__file__).resolve()",
            "REPO_ROOT = _HERE.parents[5]                       # repo/workspaces/<proj>/interns/generated/solutions/<file>",
            "INTERNS = _HERE.parents[2]                         # workspaces/<proj>/interns",
            'BRONZE = INTERNS / "state" / "medallion" / "bronze"',
            'SILVER = INTERNS / "state" / "medallion" / "silver"',
            'GOLD = INTERNS / "state" / "medallion" / "gold"',
            "",
        ]

        lines.append("# ── Source readers (lazy) ─────────────────────────────────────────────────")
        for source in required_sources:
            alias = source_aliases[source]
            lines.extend(self._reader(alias, source))
        lines.append("")

        lines.append("# ── Join chain ───────────────────────────────────────────────────────────")
        base_alias = source_aliases[base_source]
        lines.append(f"features = {base_alias}")
        for source in required_sources[1:]:
            alias = source_aliases[source]
            rel = find_executable_relationship(relationships, base_source, source)
            if not rel:
                raise ValueError(f"No executable relationship: {base_source} → {source}")
            lc, rc = rel.get("left_column", ""), rel.get("right_column", "")
            lines.append(
                f'features = features.join({alias}, left_on="{lc}", right_on="{rc}", how="left")'
            )
        lines.append("")

        needed = self._needed_columns(intent)
        if needed:
            lines.append("# ── Keep only the columns this KPI needs ─────────────────────────────────")
            cols = ", ".join(f'"{c}"' for c in needed)
            lines.append(f"features = features.select([{cols}])")
            lines.append("")

        lines.append("# ── KPI result ───────────────────────────────────────────────────────────")
        lines.extend(self._result_lines(intent))
        lines.append("")

        lines += [
            "# ── Write Silver (features) and Gold (result) → Delta ─────────────────────",
            "SILVER.mkdir(parents=True, exist_ok=True)",
            "GOLD.mkdir(parents=True, exist_ok=True)",
            "# schema_mode=overwrite: regeneration may rename/add columns; without it a",
            "# stale Delta schema from a prior run fails the write (SchemaMismatchError).",
            '_DELTA_OPTS = {"schema_mode": "overwrite"}',
            f'features.collect().write_delta(str(SILVER / "{kpi_id}_features"), mode="overwrite", delta_write_options=_DELTA_OPTS)',
            "result_df = result.collect()",
            f'result_df.write_delta(str(GOLD / "{kpi_id}_results"), mode="overwrite", delta_write_options=_DELTA_OPTS)',
            f'print("Silver + Gold written for {kpi_id}")',
            "print(result_df)",
            "",
        ]
        return "\n".join(lines)

    def _reader(self, alias: str, source: str) -> list[str]:
        if self.dialect == "databricks":
            table = f"{self.catalog}.{self.schema}.{_safe_name(Path(source).stem)}"
            return [f'{alias} = pl.scan_delta("{table}")']
        ext = Path(source).suffix.lower()
        if ext == ".parquet":
            return [f'{alias} = pl.scan_parquet(REPO_ROOT / "{source}")']
        return [f'{alias} = pl.scan_csv(REPO_ROOT / "{source}")']

    def _needed_columns(self, intent: KPIIntent) -> list[str]:
        needed: list[str] = []
        if intent.metric and intent.metric.column:
            needed.append(intent.metric.column)
        if intent.share:
            if intent.share.metric.column:
                needed.append(intent.share.metric.column)
            if intent.share.partition:
                needed.append(intent.share.partition)
        for pair in (intent.ratio or ()):
            if pair.column:
                needed.append(pair.column)
        for dim in intent.dims:
            if dim.column:
                needed.append(dim.column)
        for filt in intent.filters:
            if filt.target != "__age__":
                needed.append(filt.target)
        return _unique_preserve_order(needed)

    def _result_lines(self, intent: KPIIntent) -> list[str]:
        if intent.share:
            return self._share_lines(intent)
        if intent.ratio:
            return self._ratio_lines(intent)
        lines = ["lf = features"] + self._derive_dim_lines(intent) + self._filter_lines(intent)
        group_exprs = self._group_exprs(intent)
        agg_expr, agg_alias = self._agg_expr(intent.metric)
        if group_exprs:
            lines.append("lf = lf.group_by([" + ", ".join(group_exprs) + "]).agg([" + agg_expr + "])")
        else:
            lines.append("lf = lf.select([" + agg_expr + "])")
        lines.append(f'lf = lf.sort("{agg_alias}", descending=True)')
        if intent.top_n:
            lines.append(f"lf = lf.head({intent.top_n})")
        lines.append("result = lf")
        return lines

    def _share_lines(self, intent: KPIIntent) -> list[str]:
        share = intent.share
        assert share is not None
        m = share.metric
        lines = ["lf = features"] + self._derive_dim_lines(intent) + self._filter_lines(intent)
        if share.kind == "mismatched_grain_percentage":
            per = (
                f'pl.col("{m.column}").n_unique().over("{share.partition}")'
                if m.distinct else f'pl.col("{m.column}").{m.fn}().over("{share.partition}")'
            )
            total = (
                f'pl.col("{m.column}").n_unique()'
                if m.distinct else f'pl.col("{m.column}").{m.fn}()'
            )
            lines.append(
                f'lf = lf.with_columns([{per}.alias("share_per_group"), {total}.alias("share_total")])'
            )
            lines.append(
                'lf = lf.with_columns((pl.col("share_per_group") / pl.col("share_total") * 100)'
                '.alias("percentage_share"))'
            )
            select_cols = self._dim_select_cols(intent) + [
                '"share_per_group"', '"share_total"', '"percentage_share"'
            ]
            lines.append("lf = lf.select([" + ", ".join(select_cols) + "])")
            lines.append("result = lf")
            return lines
        # percent_of_total / percent_of_group — grouped then scoped division
        group_exprs = self._group_exprs(intent)
        base = self._metric_agg_expr(m)
        lines.append(
            "lf = lf.group_by([" + ", ".join(group_exprs) + f"]).agg([{base}.alias(\"group_val\")])"
        )
        if share.kind == "percent_of_group" and share.partition:
            lines.append(
                f'lf = lf.with_columns((pl.col("group_val") / pl.col("group_val").sum()'
                f'.over("{share.partition}") * 100).alias("{share.alias}"))'
            )
        else:
            lines.append(
                'lf = lf.with_columns((pl.col("group_val") / pl.col("group_val").sum() * 100)'
                f'.alias("{share.alias}"))'
            )
        lines.append(f'lf = lf.sort("{share.alias}", descending=True)')
        lines.append("result = lf")
        return lines

    def _ratio_lines(self, intent: KPIIntent) -> list[str]:
        num, den = intent.ratio  # type: ignore[misc]
        lines = ["lf = features"] + self._derive_dim_lines(intent) + self._filter_lines(intent)
        group_exprs = self._group_exprs(intent)
        aggs = f'{self._metric_agg_expr(num)}.alias("{num.alias}"), {self._metric_agg_expr(den)}.alias("{den.alias}")'
        if group_exprs:
            lines.append("lf = lf.group_by([" + ", ".join(group_exprs) + f"]).agg([{aggs}])")
        else:
            lines.append(f"lf = lf.select([{aggs}])")
        lines.append(
            f'lf = lf.with_columns((pl.col("{num.alias}") / pl.col("{den.alias}")).alias("ratio"))'
        )
        lines.append("result = lf")
        return lines

    def _derive_dim_lines(self, intent: KPIIntent) -> list[str]:
        # BUG-005 (cross-engine): date arithmetic must be anchored to the KPI's
        # event-date column when one exists — exactly like the SQL path — and
        # fall back to today only when the grain has no event date. Anchoring
        # Polars to `date.today()` while SQL anchors to the event date produced
        # different age values and broke row parity.
        event_date_col = next((d.column for d in intent.dims if d.kind == "time" and d.column), "")
        if event_date_col:
            as_of_year = f'pl.col("{event_date_col}").cast(pl.Date, strict=False).dt.year()'
            as_of_date = f'pl.col("{event_date_col}").cast(pl.Date, strict=False)'
        else:
            as_of_year = "pl.lit(date.today().year)"
            as_of_date = "pl.lit(date.today())"
        with_cols: list[str] = []
        for dim in intent.dims:
            if dim.kind == "time":
                every = _PL_EVERY.get(dim.unit, "1mo")
                with_cols.append(
                    f'pl.col("{dim.column}").cast(pl.Date, strict=False)'
                    f'.dt.truncate("{every}").alias("{dim.alias}")'
                )
            elif dim.kind == "age":
                # calendar-year difference to match SQL date_diff('year', dob, as_of)
                with_cols.append(
                    f'({as_of_year} - pl.col("{dim.column}").cast(pl.Date, strict=False)'
                    f'.dt.year()).cast(pl.Int32).alias("{dim.alias}")'
                )
            elif dim.kind == "days_since":
                with_cols.append(
                    f'({as_of_date} - pl.col("{dim.column}").cast(pl.Date, strict=False))'
                    f'.dt.total_days().alias("{dim.alias}")'
                )
        return ["lf = lf.with_columns([" + ", ".join(with_cols) + "])"] if with_cols else []

    def _filter_lines(self, intent: KPIIntent) -> list[str]:
        exprs = []
        age_alias = next((d.alias for d in intent.dims if d.kind == "age"), None)
        for filt in intent.filters:
            if filt.target == "__age__":
                if age_alias:
                    exprs.append(f'(pl.col("{age_alias}") {filt.op} {filt.value})')
                continue
            op = _PL_OPS.get(filt.op, "==")
            value = f'"{filt.value}"' if filt.is_literal else filt.value
            exprs.append(f'(pl.col("{filt.target}") {op} {value})')
        return ["lf = lf.filter(" + " & ".join(exprs) + ")"] if exprs else []

    def _group_exprs(self, intent: KPIIntent) -> list[str]:
        return [
            f'pl.col("{d.column}")' if d.kind == "column" else f'pl.col("{d.alias}")'
            for d in intent.dims
        ]

    def _dim_select_cols(self, intent: KPIIntent) -> list[str]:
        return [
            f'"{d.column}"' if d.kind == "column" else f'"{d.alias}"'
            for d in intent.dims
        ]

    def _metric_agg_expr(self, m) -> str:
        col = m.column
        if m.fn == "sum":
            return f'pl.col("{col}").sum().round(2)'
        if m.fn == "avg":
            return f'pl.col("{col}").mean().round(2)'
        if m.fn == "count" and m.distinct:
            return f'pl.col("{col}").n_unique()'
        if m.fn == "count":
            return "pl.len()"
        if m.fn in {"min", "max"}:
            return f'pl.col("{col}").{m.fn}()'
        return f'pl.col("{col}").sum()'

    def _agg_expr(self, metric) -> tuple[str, str]:
        if not metric:
            return 'pl.len().alias("row_count")', "row_count"
        alias = metric.alias
        return f"{self._metric_agg_expr(metric)}.alias(\"{alias}\")", alias

    def _build_source_plan(
        self, base_source: str, feature_refs: list[dict[str, str]]
    ) -> tuple[list[str], dict[str, str]]:
        required = _unique_preserve_order(
            [base_source]
            + [
                ref["dataset"]
                for ref in feature_refs
                if ref.get("dataset") and ref["dataset"] != base_source
            ]
        )
        aliases = {src: f"df_{_safe_name(Path(src).stem)}" for src in required}
        return required, aliases

    def _profile_map(self) -> dict[str, dict[str, Any]]:
        index = self.layout.profiles_dir / "profile_index.json"
        if not index.exists():
            return {}
        data = json.loads(index.read_text(encoding="utf-8"))
        return {
            _repo_path(str(p.get("path") or ""), self.repo_root): p
            for p in data.get("profiles", [])
            if p.get("path")
        }

    def _load_mapping(self) -> dict[str, Any]:
        path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        if not path.exists():
            raise FileNotFoundError(f"feature mapping not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Polars KPI script from feature mappings.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--kpi-id", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--dialect", default="local", choices=["local", "databricks"])
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--schema", default="autoresearch")
    args = parser.parse_args(argv)
    gen = PolarsKPIGenerator(
        repo_root=args.repo_root,
        workspace=args.workspace,
        dialect=args.dialect,
        catalog=args.catalog,
        schema=args.schema,
    )
    result = gen.generate(args.kpi_id)
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
