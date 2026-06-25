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
from core.onboarding.kpi.kpi_intent import KPIIntent, column_dim_renamed, parse_intent
from core.onboarding.kpi.sensitive_masking import (
    POLARS_MASK_HELPER,
    load_sensitive_columns,
    polars_mask_helper_lines,
)
from core.sql_safety import (
    is_safe_identifier,
    map_comparison_op,
    render_python_scalar_literal,
)
from core.onboarding.relationships.contracts import (
    find_executable_relationship,
    load_relationship_contracts,
)
from core.onboarding.kpi.sql_generator import (
    _unique_preserve_order,
    _repo_path,
    _rel,
    _safe_name,
    plan_required_sources,
)
from core.onboarding.relationships.base_source_selector import (
    load_pinned_base_sources,
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
        # SAME source plan as the SQL generator (one chosen ref per feature),
        # so cross-engine parity starts from identical sources. Unioning every
        # candidate ref pulled in datasets with no executable relationship.
        base_source, required_sources, _refs = plan_required_sources(
            kpi,
            profile_map,
            self.repo_root,
            relationships,
            pinned=load_pinned_base_sources(self.layout.contracts_dir).get(kpi_id),
        )
        if not base_source:
            raise ValueError(f"Cannot determine base source for KPI {kpi_id}")
        source_aliases = {
            src: f"df_{_safe_name(Path(src).stem)}" for src in required_sources
        }

        intent = parse_intent(kpi)
        if intent.unsupported_window:
            raise ValueError(
                f"KPI {kpi_id} uses window pattern `{intent.unsupported_window}` not yet supported "
                "in Polars generation. Generate SQL for this KPI, or extend the Polars renderer."
            )
        # Per-source column pre-selection from the CHOSEN refs (mirrors the SQL
        # catalog views, which SELECT only each source's needed columns).
        # Without it, identically-named columns in joined tables (Id, START...)
        # silently resolve to the wrong table after the join — the SQL side
        # alias-qualifies (s0/s1) so the engines diverge or fail at select.
        needed_by_source: dict[str, set[str]] = {src: set() for src in required_sources}
        for ref in _refs:
            if ref.get("dataset") in needed_by_source and ref.get("column"):
                needed_by_source[ref["dataset"]].add(ref["column"])
        join_keys: dict[str, tuple[str, str]] = {}
        for source in required_sources[1:]:
            rel = find_executable_relationship(relationships, base_source, source)
            if rel:
                left_col = str(rel.get("left_column") or "")
                right_col = str(rel.get("right_column") or "")
                if left_col and right_col:
                    join_keys[source] = (left_col, right_col)
                    needed_by_source[base_source].add(left_col)

        # Same grain-bucketing decision the SQL path consumes: a banded
        # continuous cut (age -> "20-29") must band in EVERY engine or the
        # result grains diverge (sql age_band vs polars exact age).
        band_width = self._grain_band_width(kpi_id)

        # Sensitive columns to mask (SHA-256 hex) so this engine never writes raw
        # PHI/PCI to Silver/Gold and stays parity-consistent with SQL. Single-
        # sourced in sensitive_masking. Ref: core-audit ob-kpi-b.md (T2).
        # A sensitive column consumed only as a RAW date-arithmetic input (e.g.
        # DOB feeding an age band) is left unmasked here EXACTLY as the SQL path
        # does — masking it would break the date diff and diverge from SQL
        # (row parity). Its raw value is a derivation input, never written out.
        from core.onboarding.kpi.result_view_builder import raw_date_input_columns
        sensitive_cols = load_sensitive_columns(self.layout) - raw_date_input_columns(kpi)

        code = self._emit_script(
            kpi, kpi_id, intent, required_sources, source_aliases, base_source,
            relationships, needed_by_source=needed_by_source, join_keys=join_keys,
            band_width=band_width, sensitive_cols=sensitive_cols,
        )
        suffix = "" if self.dialect == "local" else f"_{self.dialect}"
        out = self.layout.solutions_dir / f"{kpi_id}_polars{suffix}.py"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(code, encoding="utf-8")
        return PolarsGenerationResult(path=_rel(out, self.repo_root), kpi_id=kpi_id, status="generated")

    def _grain_band_width(self, kpi_id: str) -> int | None:
        """The recorded grain-bucketing band width for this KPI, or None.

        Reads the same ``pipeline_decisions.json`` contract the SQL generator
        consumes so banding decisions apply identically across engines.
        """
        from core.onboarding.kpi.result_view_builder import _band_width_from_decision

        path = self.layout.contracts_dir / "pipeline_decisions.json"
        if not path.exists():
            return None
        try:
            decisions = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(decisions, dict):
            return None
        grain = (decisions.get("grain_bucketing_decisions") or {}).get(kpi_id)
        return _band_width_from_decision(grain)

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
        *,
        needed_by_source: dict[str, set[str]] | None = None,
        join_keys: dict[str, tuple[str, str]] | None = None,
        band_width: int | None = None,
        sensitive_cols: set[str] | None = None,
    ) -> str:
        needed_by_source = needed_by_source or {}
        join_keys = join_keys or {}
        sensitive_cols = sensitive_cols or set()
        lines: list[str] = [
            "# Polars KPI script — generated from feature mappings + shared KPI intent.",
            f"# KPI: {kpi.get('name', kpi_id)}",
            f"# Metric: {kpi.get('metric', '')}",
            f"# Cuts: {kpi.get('cuts', '')}",
            f"# Generated: {date.today().isoformat()}",
            "# Streaming-capable via lazy API. Paths are anchored on __file__ (portable).",
            "",
            "import hashlib",
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
            "",
            "def _as_date(col: str) -> pl.Expr:",
            '    """Date coercion matching SQL CAST(x AS DATE): plain dates AND',
            '    ISO datetime strings (2017-03-16T07:47:14Z) both coerce; a bare',
            "    cast(pl.Date) nulls datetime strings and collapses time buckets.",
            "    The fallback slices the ISO day prefix with an explicit format so",
            "    timezone suffixes cannot break format inference.\"\"\"",
            "    return pl.coalesce(",
            "        [",
            "            pl.col(col).cast(pl.Date, strict=False),",
            '            pl.col(col).cast(pl.Utf8, strict=False).str.slice(0, 10)',
            '            .str.to_date("%Y-%m-%d", strict=False),',
            "        ]",
            "    )",
            "",
        ]
        # SHA-256 masking helper (matches DuckDB/Spark), used to scrub sensitive
        # columns before Silver/Gold writes. Emitted unconditionally; harmless if
        # the KPI has no sensitive columns.
        lines.extend(polars_mask_helper_lines())

        lines.append("# ── Source readers (lazy) ─────────────────────────────────────────────────")
        for source in required_sources:
            alias = source_aliases[source]
            lines.extend(self._reader(alias, source))
            # Pre-select each source to ITS chosen columns (+ its join key),
            # mirroring the SQL catalog views. This is what keeps an
            # identically-named column in another table from being read after
            # the join (the cross-engine wrong-table bug).
            keep = set(needed_by_source.get(source) or set())
            if source in join_keys:
                keep.add(join_keys[source][1])
            if keep:
                cols = ", ".join(f'"{c}"' for c in sorted(keep))
                lines.append(f"{alias} = {alias}.select([{cols}])")
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
            needed_here = needed_by_source.get(source) or set()
            if rc in needed_here:
                # polars drops the right join key from the output; when that
                # column is ALSO a needed feature (count distinct of the joined
                # table's id), join on a duplicated key so the data column
                # survives.
                dup = f"__join_{rc}"
                lines.append(
                    f'features = features.join({alias}.with_columns(pl.col("{rc}").alias("{dup}")), '
                    f'left_on="{lc}", right_on="{dup}", how="left")'
                )
            else:
                lines.append(
                    f'features = features.join({alias}, left_on="{lc}", right_on="{rc}", how="left")'
                )
        lines.append("")

        needed = self._needed_columns(intent)
        if needed_by_source:
            # The select can only reference columns the join actually produced.
            # Intent fields can carry RAW question tokens (e.g. a misspelled
            # group word the SQL side resolves via the BUG-011 group-column
            # resolution); selecting those crashes with ColumnNotFoundError.
            available = set().union(*needed_by_source.values()) if needed_by_source else set()
            needed = [c for c in needed if c in available]
        if needed:
            lines.append("# ── Keep only the columns this KPI needs ─────────────────────────────────")
            cols = ", ".join(f'"{c}"' for c in needed)
            lines.append(f"features = features.select([{cols}])")
            lines.append("")

        # Mask sensitive columns BEFORE the result is computed (mirrors the SQL
        # path, which masks in the _features view that the result view reads), so
        # grouping/aggregation operate on the masked hex exactly like SQL.
        frame_cols = needed if needed else sorted(
            set().union(*needed_by_source.values()) if needed_by_source else set()
        )
        sensitive_present = [
            c for c in frame_cols if isinstance(c, str) and c.lower() in sensitive_cols
        ]
        if sensitive_present:
            lines.append("# ── Mask sensitive columns (SHA-256, parity-consistent with SQL) ──────────")
            mask_exprs = ", ".join(f'{POLARS_MASK_HELPER}("{c}")' for c in sensitive_present)
            lines.append(f"features = features.with_columns([{mask_exprs}])")
            lines.append("")

        lines.append("# ── KPI result ───────────────────────────────────────────────────────────")
        lines.extend(self._result_lines(intent, band_width=band_width))
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
        # Read the SAME source the SQL path reads: when the medallion Bronze
        # Delta for this dataset exists, SQL's catalog views delta_scan() it
        # (typed columns) — reading the raw CSV here instead diverges on type
        # semantics (e.g. timestamp parsing) and breaks row parity. PySpark
        # already reads Bronze; this aligns Polars.
        stem = _safe_name(Path(source).stem)
        bronze_dir = self.layout.state_dir / "medallion" / "bronze" / stem
        if (bronze_dir / "_delta_log").exists():
            return [f'{alias} = pl.scan_delta(str(BRONZE / "{stem}"))']
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

    @staticmethod
    def _dim_out_name(dim, band_width: int | None) -> str:
        """The dim's OUTPUT column name: banded continuous cuts emit `<alias>_band`
        (exactly like the SQL path), everything else its column/alias."""
        if band_width and dim.kind in ("age", "days_since"):
            return f"{dim.alias}_band"
        if dim.kind == "column":
            # A renamed column dim (Name -> department_name) is materialized
            # under its alias by _derive_dim_lines; a normal column keeps its
            # physical name so existing output schemas are unchanged.
            return dim.alias if column_dim_renamed(dim) else dim.column
        return dim.alias

    def _result_lines(self, intent: KPIIntent, *, band_width: int | None = None) -> list[str]:
        if intent.share:
            return self._share_lines(intent, band_width=band_width)
        if intent.ratio:
            return self._ratio_lines(intent)
        lines = (
            ["lf = features"]
            + self._derive_dim_lines(intent, band_width=band_width)
            + self._filter_lines(intent)
        )
        group_exprs = self._group_exprs(intent, band_width=band_width)
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

    def _share_lines(self, intent: KPIIntent, *, band_width: int | None = None) -> list[str]:
        share = intent.share
        assert share is not None
        m = share.metric
        lines = (
            ["lf = features"]
            + self._derive_dim_lines(intent, band_width=band_width)
            + self._filter_lines(intent)
        )
        if share.kind == "mismatched_grain_percentage":
            if m.distinct:
                # Single attribution — mirrors the SQL share_attribution CTE:
                # one row (= one grain cell) per entity (most recent event
                # first when the grain has an event date, grain columns as
                # deterministic tiebreakers), then a plain group count whose
                # shares sum to ~100% by construction. Counting an entity in
                # every cell it touches made shares total >100% (BUG: 229%
                # measured on real data).
                time_col = next(
                    (d.column for d in intent.dims if d.kind == "time" and d.column), ""
                )
                sort_exprs: list[str] = []
                descending: list[str] = []
                if time_col:
                    sort_exprs.append(f'_as_date("{time_col}")')
                    descending.append("True")
                for d in intent.dims:
                    sort_exprs.append(f'pl.col("{self._dim_out_name(d, band_width)}")')
                    descending.append("False")
                lines.append(
                    f'lf = lf.sort([{", ".join(sort_exprs)}], '
                    f'descending=[{", ".join(descending)}], nulls_last=True)'
                )
                lines.append(
                    f'lf = lf.unique(subset=["{m.column}"], keep="first", maintain_order=True)'
                )
                group_exprs = self._group_exprs(intent, band_width=band_width)
                lines.append(
                    "lf = lf.group_by([" + ", ".join(group_exprs) + "])"
                    '.agg([pl.len().alias("__attributed")])'
                )
                lines.append(
                    'lf = lf.with_columns((pl.col("__attributed").cast(pl.Float64) '
                    '/ pl.col("__attributed").sum() * 100).alias("percentage_share"))'
                )
                select_cols = self._dim_select_cols(intent, band_width=band_width) + ['"percentage_share"']
                lines.append("lf = lf.select([" + ", ".join(select_cols) + "])")
                lines.append("result = lf")
                return lines
            # Row-based shares only (distinct-entity shares returned above):
            # every row belongs to exactly one cell. Mirrors the SQL form:
            # numerator per FULL grain cell, denominator per the share's
            # partition when that partition is a grain dimension (within-period
            # share, e.g. "for year") else the grand total; scaffolding inlined,
            # one row per cell (SQL's SELECT DISTINCT == .unique()).
            cells = [self._dim_out_name(d, band_width) for d in intent.dims]
            cell_list = ", ".join(f'"{c}"' for c in cells)
            per = f'pl.col("{m.column}").{m.fn}().over([{cell_list}])' if cells else f'pl.col("{m.column}").{m.fn}()'
            if share.partition and share.partition in cells:
                total = f'pl.col("{m.column}").{m.fn}().over("{share.partition}")'
            else:
                total = f'pl.col("{m.column}").{m.fn}()'
            lines.append(
                f'lf = lf.with_columns(({per} / {total} * 100).alias("percentage_share"))'
            )
            select_cols = [f'"{c}"' for c in cells] + ['"percentage_share"']
            lines.append(
                "lf = lf.select([" + ", ".join(select_cols) + "]).unique(maintain_order=True)"
            )
            lines.append("result = lf")
            return lines
        # percent_of_total / percent_of_group — grouped then scoped division.
        if (
            share.kind != "percent_of_group"
            and intent.filters
            and not intent.dims
        ):
            # Filtered percent-of-total ("how many X had <condition>, and what
            # percentage of all X"): the filter scopes the NUMERATOR only —
            # mirroring the SQL FILTER (WHERE ...) rewrite. Note _filter_lines
            # was already applied above, which would have filtered the
            # denominator too; rebuild without it.
            cond = " & ".join(self._filter_exprs(intent))
            col = m.column
            if m.distinct:
                num = f'pl.col("{col}").filter({cond}).n_unique()'
                den = f'pl.col("{col}").n_unique()'
            elif m.fn == "count":
                num = f'({cond}).cast(pl.UInt32).sum()'
                den = "pl.len()"
            else:
                num = f'pl.col("{col}").filter({cond}).{m.fn}()'
                den = f'pl.col("{col}").{m.fn}()'
            lines = ["lf = features"] + self._derive_dim_lines(intent, band_width=band_width)
            lines.append(
                "lf = lf.select(["
                f'{num}.alias("{m.alias}"), '
                f'({num} / {den} * 100).alias("{share.alias}")'
                "])"
            )
            lines.append("result = lf")
            return lines
        group_exprs = self._group_exprs(intent, band_width=band_width)
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
        # Scaffolding (group_val) is inlined on the SQL side; emit only the
        # grain + the share column.
        share_select = self._dim_select_cols(intent, band_width=band_width) + [f'"{share.alias}"']
        lines.append(f'lf = lf.sort("{share.alias}", descending=True)')
        lines.append("lf = lf.select([" + ", ".join(share_select) + "])")
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

    def _derive_dim_lines(self, intent: KPIIntent, *, band_width: int | None = None) -> list[str]:
        # BUG-005 (cross-engine): date arithmetic must be anchored to the KPI's
        # event-date column when one exists — exactly like the SQL path — and
        # fall back to today only when the grain has no event date. Anchoring
        # Polars to `date.today()` while SQL anchors to the event date produced
        # different age values and broke row parity.
        event_date_col = next((d.column for d in intent.dims if d.kind == "time" and d.column), "")
        if event_date_col:
            as_of_year = f'_as_date("{event_date_col}").dt.year()'
            as_of_date = f'_as_date("{event_date_col}")'
        else:
            as_of_year = "pl.lit(date.today().year)"
            as_of_date = "pl.lit(date.today())"
        with_cols: list[str] = []
        for dim in intent.dims:
            if column_dim_renamed(dim):
                # Emit the renamed dimension under its alias (Name ->
                # department_name) so the engine's output column matches SQL.
                with_cols.append(f'pl.col("{dim.column}").alias("{dim.alias}")')
                continue
            if dim.kind == "time":
                every = _PL_EVERY.get(dim.unit, "1mo")
                with_cols.append(
                    f'_as_date("{dim.column}")'
                    f'.dt.truncate("{every}").alias("{dim.alias}")'
                )
            elif dim.kind == "age":
                # calendar-year difference to match SQL date_diff('year', dob, as_of)
                raw = (
                    f'({as_of_year} - _as_date("{dim.column}")'
                    f".dt.year()).cast(pl.Int32)"
                )
                with_cols.append(f'{raw}.alias("{dim.alias}")')
                if band_width:
                    # Grain-bucketing decision: band like the SQL path —
                    # FLOOR(value/width)*width labeled "lo-hi" (e.g. "20-29").
                    lo = f"({raw}.floordiv({band_width}) * {band_width})"
                    with_cols.append(
                        f'pl.format("{{}}-{{}}", {lo}, {lo} + {band_width - 1})'
                        f'.alias("{dim.alias}_band")'
                    )
            elif dim.kind == "days_since":
                raw = f'({as_of_date} - _as_date("{dim.column}")).dt.total_days()'
                with_cols.append(f'{raw}.alias("{dim.alias}")')
                if band_width:
                    lo = f"({raw}.floordiv({band_width}) * {band_width})"
                    with_cols.append(
                        f'pl.format("{{}}-{{}}", {lo}, {lo} + {band_width - 1})'
                        f'.alias("{dim.alias}_band")'
                    )
        return ["lf = lf.with_columns([" + ", ".join(with_cols) + "])"] if with_cols else []

    def _filter_exprs(self, intent: KPIIntent) -> list[str]:
        exprs = []
        age_alias = next((d.alias for d in intent.dims if d.kind == "age"), None)
        for filt in intent.filters:
            # T4: map the op through _PL_OPS (never inline a raw op) and render
            # the value as a safe Python literal (repr for strings) so a crafted
            # value/op cannot inject code into the generated, subprocess-executed
            # script. is_literal=True means the value is a (non-numeric) string.
            op = map_comparison_op(filt.op, _PL_OPS)
            if filt.target == "__age__":
                if age_alias:
                    value = render_python_scalar_literal(
                        filt.value, treat_as_string=filt.is_literal
                    )
                    exprs.append(f'(pl.col("{age_alias}") {op} {value})')
                continue
            if not is_safe_identifier(filt.target):
                # A filter target must be a real column; skip rather than inline
                # an unvalidated token into pl.col("...").
                continue
            value = render_python_scalar_literal(
                filt.value, treat_as_string=filt.is_literal
            )
            exprs.append(f'(pl.col("{filt.target}") {op} {value})')
        return exprs

    def _filter_lines(self, intent: KPIIntent) -> list[str]:
        exprs = self._filter_exprs(intent)
        return ["lf = lf.filter(" + " & ".join(exprs) + ")"] if exprs else []

    def _group_exprs(self, intent: KPIIntent, *, band_width: int | None = None) -> list[str]:
        return [
            f'pl.col("{self._dim_out_name(d, band_width)}")' for d in intent.dims
        ]

    def _dim_select_cols(self, intent: KPIIntent, *, band_width: int | None = None) -> list[str]:
        return [f'"{self._dim_out_name(d, band_width)}"' for d in intent.dims]

    def _metric_agg_expr(self, m) -> str:
        col = m.column
        if m.fn == "sum":
            return f'pl.col("{col}").sum().round(2)'
        if m.fn == "avg":
            return f'pl.col("{col}").mean().round(2)'
        if m.fn == "median":
            return f'pl.col("{col}").median().round(2)'
        if m.fn == "stddev":
            return f'pl.col("{col}").std().round(2)'      # sample std (ddof=1)
        if m.fn == "variance":
            return f'pl.col("{col}").var().round(2)'      # sample variance (ddof=1)
        if m.fn == "count" and m.distinct:
            return f'pl.col("{col}").n_unique()'
        if m.fn == "count":
            return "pl.len()"
        if m.fn in {"min", "max"}:
            return f'pl.col("{col}").{m.fn}()'
        # No silent fall-through to sum: an unhandled function would otherwise be
        # computed as a SUM, silently producing wrong numbers + breaking parity.
        raise ValueError(f"unsupported aggregation function for polars: {m.fn!r}")

    def _agg_expr(self, metric) -> tuple[str, str]:
        if not metric:
            return 'pl.len().alias("row_count")', "row_count"
        alias = metric.alias
        return f"{self._metric_agg_expr(metric)}.alias(\"{alias}\")", alias


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
