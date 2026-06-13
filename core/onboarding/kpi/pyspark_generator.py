"""PySpark KPI generator — produces a PySpark DataFrame API script.

Renders from the shared engine-neutral intent (`kpi_intent.parse_intent`) so it
agrees with the SQL and Polars paths. Applies the self-hosted lakehouse ops
rules from `docs/reference/self_hosted_lakehouse_ops_reference.md`:
  - read typed Bronze Delta on the hot path (no `inferSchema` downstream — Rule 3);
  - add provenance columns at Bronze ingest;
  - broadcast dimension tables in joins;
  - Adaptive Query Execution + Delta session config.
Runs locally (Spark standalone) or on Databricks — only the reader differs.
Paths are anchored on __file__ (portable).
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
from core.onboarding.kpi.sensitive_masking import load_sensitive_columns
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
)
from core.onboarding.relationships.base_source_selector import (
    load_pinned_base_sources,
)
from core.storage.workspace_layout import WorkspaceLayout

_SPARK_TRUNC = {"year": "year", "quarter": "quarter", "month": "month", "week": "week", "day": "day"}
_SPARK_OPS = {"=": "==", "==": "==", "!=": "!=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}


@dataclass(frozen=True)
class PySparkGenerationResult:
    path: str
    kpi_id: str
    status: str
    engine: str = "pyspark"

    def summary(self) -> dict[str, Any]:
        return {**self.__dict__}


class PySparkKPIGenerator:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        dialect: str = "local",   # local | databricks
        catalog: str = "workspace",
        schema: str = "autoresearch",
        app_name: str = "autoresearch_kpi",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.dialect = dialect
        self.catalog = catalog
        self.schema = schema
        self.app_name = app_name

    def generate(self, kpi_id: str) -> PySparkGenerationResult:
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
                f"KPI {kpi_id} uses derived-formula feature(s) `{names}`; PySpark generation does "
                "not yet translate derived formulas. Generate SQL for this KPI or add a PySpark "
                "derivation rule before emitting a PySpark script."
            )

        profile_map = self._profile_map()
        relationships = load_relationship_contracts(
            self.repo_root, _rel(self.workspace, self.repo_root)
        )
        # SAME source plan as the SQL generator (one chosen ref per feature),
        # so cross-engine parity starts from identical sources. Unioning every
        # candidate ref pulled in datasets with no executable relationship.
        from core.onboarding.kpi.sql_generator import plan_required_sources

        base_source, required_sources, _refs = plan_required_sources(
            kpi,
            profile_map,
            self.repo_root,
            relationships,
            pinned=load_pinned_base_sources(self.layout.contracts_dir).get(kpi_id),
        )
        if not base_source:
            raise ValueError(f"Cannot determine base source for KPI {kpi_id}")
        source_aliases = {src: f"df_{_safe_name(Path(src).stem)}" for src in required_sources}

        intent = parse_intent(kpi)
        if intent.unsupported_window:
            raise ValueError(
                f"KPI {kpi_id} uses window pattern `{intent.unsupported_window}` not yet supported "
                "in PySpark generation. Generate SQL for this KPI, or extend the PySpark renderer."
            )
        # Per-source column pre-selection from the CHOSEN refs (mirrors the
        # Polars emitter and the SQL catalog views): identically-named columns
        # in joined tables otherwise become ambiguous after the join.
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
        # Same grain-bucketing decision the SQL/Polars paths consume.
        from core.onboarding.kpi.result_view_builder import _band_width_from_decision

        band_width = None
        decisions_path = self.layout.contracts_dir / "pipeline_decisions.json"
        if decisions_path.exists():
            try:
                decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
                if isinstance(decisions, dict):
                    band_width = _band_width_from_decision(
                        (decisions.get("grain_bucketing_decisions") or {}).get(kpi_id)
                    )
            except (json.JSONDecodeError, OSError):
                band_width = None

        # Sensitive columns to mask (SHA-256 hex) so PySpark never writes raw
        # PHI/PCI to Silver/Gold and stays parity-consistent with SQL/Polars.
        # Single-sourced in sensitive_masking. Ref: core-audit ob-kpi-b.md (T2).
        sensitive_cols = load_sensitive_columns(self.layout)

        code = self._emit_script(
            kpi, kpi_id, intent, required_sources, source_aliases, base_source,
            relationships, needed_by_source=needed_by_source, join_keys=join_keys,
            band_width=band_width, sensitive_cols=sensitive_cols,
        )
        suffix = "" if self.dialect == "local" else f"_{self.dialect}"
        out = self.layout.solutions_dir / f"{kpi_id}_pyspark{suffix}.py"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(code, encoding="utf-8")
        return PySparkGenerationResult(path=_rel(out, self.repo_root), kpi_id=kpi_id, status="generated")

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
            "# PySpark KPI script — generated from feature mappings + shared KPI intent.",
            f"# KPI: {kpi.get('name', kpi_id)}",
            f"# Metric: {kpi.get('metric', '')}",
            f"# Cuts: {kpi.get('cuts', '')}",
            f"# Generated: {date.today().isoformat()}",
            "# Ops rules applied: typed Bronze Delta reads, broadcast dims, provenance at ingest.",
            "",
            "import os",
            "import sys",
            "from pyspark.sql import SparkSession",
            "from pyspark.sql import functions as F",
            "from pyspark.sql.window import Window",
            "from pathlib import Path",
            "",
            "_HERE = Path(__file__).resolve()",
            "REPO_ROOT = _HERE.parents[5]",
            "INTERNS = _HERE.parents[2]",
            'BRONZE = INTERNS / "state" / "medallion" / "bronze"',
            'SILVER = INTERNS / "state" / "medallion" / "silver"',
            'GOLD = INTERNS / "state" / "medallion" / "gold"',
            "",
        ]

        lines.append("# ── SparkSession ─────────────────────────────────────────────────────────")
        if self.dialect == "databricks":
            lines.append("spark = SparkSession.builder.getOrCreate()  # Databricks provides this")
        else:
            lines += [
                "# Preflight FIRST (before any JDK_JAVA_OPTIONS): Spark 3.5 supports Java 8/11/17",
                "# only. A newer JDK (18+) crashes the gateway cryptically — fail early with setup help.",
                "import subprocess as _sp",
                "_JDK_HELP = (",
                '    "How to fix:\\n"',
                '    "  1. Install JDK 17 (free, no sign-in): https://adoptium.net/temurin/releases/?version=17\\n"',
                '    "     or Oracle archive: https://www.oracle.com/java/technologies/javase/jdk17-archive-downloads.html\\n"',
                '    "  2. Set JAVA_HOME to the JDK 17 folder (e.g. C:/Program Files/Java/jdk-17), reopen the terminal.\\n"',
                '    "  3. Re-run. (The SQL and Polars engines need no JVM and work right now.)"',
                ")",
                "def _require_supported_java():",
                "    try:",
                "        _v = _sp.run(['java', '-version'], capture_output=True, text=True).stderr",
                "    except FileNotFoundError:",
                "        raise SystemExit('No Java found on PATH. PySpark needs JDK 17.\\n' + _JDK_HELP)",
                "    _tok = _v.split('\"')[1] if '\"' in _v else ''",
                "    _major = int(_tok.split('.')[0]) if _tok[:1].isdigit() else 0",
                "    if _major > 17:",
                "        raise SystemExit('Spark 3.5 needs Java 8/11/17 but found Java ' + str(_major) + '.\\n' + _JDK_HELP)",
                "_require_supported_java()",
                "def _require_winutils_on_windows():",
                "    if os.name != 'nt':",
                "        return",
                "    _hh = os.environ.get('HADOOP_HOME', '')",
                "    if _hh and os.path.exists(os.path.join(_hh, 'bin', 'winutils.exe')):",
                "        return",
                "    raise SystemExit(",
                "        'On Windows, Spark needs winutils.exe + hadoop.dll (HADOOP_HOME is unset or missing them).\\n'",
                "        'How to fix:\\n'",
                "        '  1. Download from https://github.com/cdarlint/winutils (use the hadoop-3.x/bin folder:\\n'",
                "        '     winutils.exe and hadoop.dll).\\n'",
                "        '  2. Put both in a folder, e.g. C:/hadoop/bin\\n'",
                "        '  3. Set HADOOP_HOME to that folder (C:/hadoop) and add %HADOOP_HOME%/bin to PATH; reopen terminal.\\n'",
                "        '  4. Re-run. (SQL and Polars need no JVM/Hadoop and work right now.)'",
                "    )",
                "_require_winutils_on_windows()",
                "# Pin Spark workers to THIS interpreter — a bare `python` on Windows can resolve",
                "# to a different global Python (e.g. Store 3.13) and crash workers (SparkEnv NPE).",
                'os.environ["PYSPARK_PYTHON"] = sys.executable',
                'os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable',
                "# Force a valid driver host. A machine hostname with an underscore (e.g. `My_PC`)",
                "# produces an `Invalid Spark URL` and the session fails to start.",
                'os.environ.setdefault("SPARK_LOCAL_IP", "127.0.0.1")',
                "# Spark 3.5 on Java 17 may still need the security manager re-allowed.",
                '_jopts = os.environ.get("JDK_JAVA_OPTIONS", "")',
                'if "java.security.manager" not in _jopts:',
                '    os.environ["JDK_JAVA_OPTIONS"] = (_jopts + " -Djava.security.manager=allow").strip()',
                "_builder = (",
                "    SparkSession.builder",
                f'    .appName("{self.app_name}_{kpi_id}")',
                '    .config("spark.driver.host", "127.0.0.1")',
                '    .config("spark.driver.bindAddress", "127.0.0.1")',
                '    .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")',
                '    .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")',
                '    .config("spark.sql.adaptive.enabled", "true")',
                '    .config("spark.sql.adaptive.coalescePartitions.enabled", "true")',
                ")",
                "# Wire the Delta Lake JVM jars via Maven coordinates matching the installed Spark.",
                "# We use only the DataFrame `format('delta')` API, so the JVM jars suffice — no",
                "# dependency on the (version-fragile) delta-spark Python package.",
                "import pyspark as _ps, glob as _glob",
                '_mj = tuple(int(x) for x in _ps.__version__.split(".")[:2])',
                "# Match the Delta artifact to the Spark build's Scala version and Spark major,",
                "# else you get NoClassDefFoundError (Scala 2.12/2.13 mismatch).",
                '_jars_dir = os.path.join(os.path.dirname(_ps.__file__), "jars")',
                '_scala = "2.13" if _glob.glob(os.path.join(_jars_dir, "scala-library-2.13*")) else "2.12"',
                '_delta_ver = "4.0.0" if _mj >= (4, 0) else ("3.2.0" if _mj >= (3, 5) else "3.1.0")',
                '_builder = _builder.config("spark.jars.packages", f"io.delta:delta-spark_{_scala}:{_delta_ver}")',
                "spark = _builder.getOrCreate()",
            ]
        lines.append("")

        if self.dialect != "databricks":
            lines.append("# ── Bronze ingestion (CSV → Delta, idempotent, with provenance) ──────────")
            lines.append("# NOTE (ops Rule 3): inferSchema is used only for one-time bootstrap; supply an")
            lines.append("#   explicit StructType for production. Downstream reads use typed Delta.")
            for source in required_sources:
                stem = _safe_name(Path(source).stem)
                lines += [
                    f'_bronze_{stem} = str(BRONZE / "{stem}")',
                    f'if not (BRONZE / "{stem}" / "_delta_log").exists():',
                    f'    _df = spark.read.csv(str(REPO_ROOT / "{source}"), header=True, inferSchema=True)',
                    '    _df = _df.withColumn("_ingest_timestamp", F.current_timestamp()) \\',
                    '             .withColumn("_source_file", F.input_file_name())',
                    f'    _df.write.format("delta").mode("overwrite").save(_bronze_{stem})',
                ]
            lines.append("")

        lines.append("# ── Source readers (typed Bronze Delta — no inferSchema) ─────────────────")
        for source in required_sources:
            alias = source_aliases[source]
            lines.extend(self._reader(alias, source))
            # Pre-select each source to ITS chosen columns (+ its join key),
            # mirroring the SQL catalog views / Polars emitter: identically
            # named columns in joined tables otherwise become ambiguous.
            keep = set(needed_by_source.get(source) or set())
            if source in join_keys:
                keep.add(join_keys[source][1])
            if keep:
                cols = ", ".join(f'"{c}"' for c in sorted(keep))
                lines.append(f"{alias} = {alias}.select({cols})")
        lines.append("")

        lines.append("# ── Join chain (dimensions broadcast) ────────────────────────────────────")
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
                # The right join key is ALSO a needed feature column: keep the
                # data column by joining on a duplicated key (Polars-mirror of
                # the dropped-right-key semantics).
                dup = f"__join_{rc}"
                lines.append(
                    f'features = features.join('
                    f'F.broadcast({alias}.withColumn("{dup}", F.col("{rc}"))), '
                    f'features["{lc}"] == F.col("{dup}"), "left").drop("{dup}")'
                )
            else:
                lines.append(
                    f'features = features.join(F.broadcast({alias}), '
                    f'features["{lc}"] == {alias}["{rc}"], "left").drop({alias}["{rc}"])'
                )
        lines.append("")

        needed = self._needed_columns(intent)
        if needed_by_source:
            # The select can only reference columns the join produced (raw
            # question tokens the SQL side resolves via group-column
            # resolution would otherwise crash here).
            available = set().union(*needed_by_source.values()) if needed_by_source else set()
            needed = [c for c in needed if c in available]
        if needed:
            lines.append("# ── Keep only the columns this KPI needs ─────────────────────────────────")
            cols = ", ".join(f'"{c}"' for c in needed)
            lines.append(f"features = features.select({cols})")
            lines.append("")

        # Mask sensitive columns BEFORE the result is computed (mirrors the SQL
        # _features view + the Polars path), with SHA-256 hex so masked values
        # match DuckDB sha256(CAST(col AS VARCHAR)) for the same string input.
        frame_cols = needed if needed else sorted(
            set().union(*needed_by_source.values()) if needed_by_source else set()
        )
        sensitive_present = [
            c for c in frame_cols if isinstance(c, str) and c.lower() in sensitive_cols
        ]
        if sensitive_present:
            lines.append("# ── Mask sensitive columns (SHA-256, parity-consistent with SQL) ──────────")
            for col in sensitive_present:
                lines.append(
                    f'features = features.withColumn("{col}", '
                    f'F.sha2(F.col("{col}").cast("string"), 256))'
                )
            lines.append("")

        lines.append("# ── KPI result ───────────────────────────────────────────────────────────")
        lines.extend(self._result_lines(intent, band_width=band_width))
        lines.append("")

        lines += [
            "# ── Write Silver (features) and Gold (result) → Delta ─────────────────────",
            "# overwriteSchema=true: the target Delta path may have been written by another engine",
            "# (e.g. Polars) with slightly different inferred types — replace the schema cleanly.",
            'features.write.format("delta").mode("overwrite").option("overwriteSchema", "true")'
            f'.save(str(SILVER / "{kpi_id}_features"))',
            'result.write.format("delta").mode("overwrite").option("overwriteSchema", "true")'
            f'.save(str(GOLD / "{kpi_id}_results"))',
            f'print("Silver + Gold written for {kpi_id}")',
            "result.show(50, truncate=False)",
            "spark.stop()",
            "",
        ]
        return "\n".join(lines)

    def _reader(self, alias: str, source: str) -> list[str]:
        if self.dialect == "databricks":
            table = f"{self.catalog}.{self.schema}.{_safe_name(Path(source).stem)}"
            return [f'{alias} = spark.table("{table}")']
        stem = _safe_name(Path(source).stem)
        return [f'{alias} = spark.read.format("delta").load(str(BRONZE / "{stem}"))']

    def _needed_columns(self, intent: KPIIntent) -> list[str]:
        needed: list[str] = []
        if intent.metric and intent.metric.column:
            needed.append(intent.metric.column)
        for dim in intent.dims:
            if dim.column:
                needed.append(dim.column)
        for filt in intent.filters:
            if filt.target != "__age__":
                needed.append(filt.target)
        return _unique_preserve_order(needed)

    def _result_lines(self, intent: KPIIntent, *, band_width: int | None = None) -> list[str]:
        if intent.share:
            return self._share_lines(intent, band_width=band_width)
        if intent.ratio:
            return self._ratio_lines(intent)
        lines = ["result = features"] + self._derive_dim_lines(intent, band_width=band_width) + self._filter_lines(intent)
        group_cols = self._group_cols(intent, band_width=band_width)
        agg_expr, agg_alias = self._agg_expr(intent.metric)
        if group_cols:
            lines.append(f"result = result.groupBy({', '.join(group_cols)}).agg({agg_expr})")
        else:
            lines.append(f"result = result.agg({agg_expr})")
        lines.append(f'result = result.orderBy(F.col("{agg_alias}").desc())')
        if intent.top_n:
            lines.append(f"result = result.limit({intent.top_n})")
        return lines

    def _share_lines(self, intent: KPIIntent, *, band_width: int | None = None) -> list[str]:
        share = intent.share
        assert share is not None
        m = share.metric
        lines = ["result = features"] + self._derive_dim_lines(intent, band_width=band_width) + self._filter_lines(intent)
        if share.kind == "mismatched_grain_percentage":
            if m.distinct:
                # Single attribution — mirrors the SQL share_attribution CTE and
                # the Polars sort+unique path: each entity is counted in exactly
                # ONE grain cell (most recent event first when the grain has an
                # event date, grain columns as deterministic tiebreakers), so
                # shares sum to ~100% by construction instead of >100% when
                # entities appear in multiple cells.
                time_col = next(
                    (d.column for d in intent.dims if d.kind == "time" and d.column), ""
                )
                order_exprs: list[str] = []
                if time_col:
                    order_exprs.append(
                        f'F.to_date(F.col("{time_col}")).desc_nulls_last()'
                    )
                for d in intent.dims:
                    col = d.column if d.kind == "column" else d.alias
                    order_exprs.append(f'F.col("{col}").asc_nulls_last()')
                lines.append(
                    f'_attr_w = Window.partitionBy("{m.column}")'
                    f'.orderBy({", ".join(order_exprs)})'
                )
                lines.append(
                    'result = result.withColumn("__attribution_rn", '
                    "F.row_number().over(_attr_w))"
                )
                lines.append('result = result.filter(F.col("__attribution_rn") == 1)')
                group_cols = self._group_cols(intent, band_width=band_width)
                lines.append(
                    f'result = result.groupBy({", ".join(group_cols)})'
                    '.agg(F.count(F.lit(1)).alias("__attributed"))'
                )
                lines.append(
                    'result = result.withColumn("percentage_share", '
                    'F.col("__attributed") / F.sum("__attributed")'
                    ".over(Window.partitionBy()) * 100)"
                )
                select_cols = self._dim_select_cols(intent, band_width=band_width) + ['"percentage_share"']
                lines.append(f"result = result.select({', '.join(select_cols)})")
                return lines
            # Row-based shares only (distinct-entity shares returned above):
            # every row belongs to exactly one cell, so these sum to ~100%.
            per = f'F.{m.fn}(F.col("{m.column}")).over(Window.partitionBy("{share.partition}"))'
            total = f'F.{m.fn}(F.col("{m.column}")).over(Window.partitionBy())'
            lines.append(f'result = result.withColumn("share_per_group", {per})')
            lines.append(f'result = result.withColumn("share_total", {total})')
            lines.append(
                'result = result.withColumn("percentage_share", '
                'F.col("share_per_group") / F.col("share_total") * 100)'
            )
            select_cols = self._dim_select_cols(intent) + [
                '"share_per_group"', '"share_total"', '"percentage_share"'
            ]
            lines.append(f"result = result.select({', '.join(select_cols)})")
            return lines
        group_cols = self._group_cols(intent)
        base = self._metric_agg_expr(m)
        lines.append(f'result = result.groupBy({", ".join(group_cols)}).agg({base}.alias("group_val"))')
        if share.kind == "percent_of_group" and share.partition:
            lines.append(
                f'result = result.withColumn("{share.alias}", '
                f'F.col("group_val") / F.sum("group_val").over(Window.partitionBy("{share.partition}")) * 100)'
            )
        else:
            lines.append(
                f'result = result.withColumn("{share.alias}", '
                'F.col("group_val") / F.sum("group_val").over(Window.partitionBy()) * 100)'
            )
        return lines

    def _ratio_lines(self, intent: KPIIntent) -> list[str]:
        num, den = intent.ratio  # type: ignore[misc]
        lines = ["result = features"] + self._derive_dim_lines(intent) + self._filter_lines(intent)
        group_cols = self._group_cols(intent)
        aggs = (
            f'{self._metric_agg_expr(num)}.alias("{num.alias}"), '
            f'{self._metric_agg_expr(den)}.alias("{den.alias}")'
        )
        if group_cols:
            lines.append(f"result = result.groupBy({', '.join(group_cols)}).agg({aggs})")
        else:
            lines.append(f"result = result.agg({aggs})")
        lines.append(
            f'result = result.withColumn("ratio", F.col("{num.alias}") / F.col("{den.alias}"))'
        )
        return lines

    def _derive_dim_lines(self, intent: KPIIntent, *, band_width: int | None = None) -> list[str]:
        # BUG-005 (cross-engine): date arithmetic must be anchored to the KPI's
        # event-date column when one exists — exactly like the SQL and Polars
        # paths — and fall back to today only when the grain has no event date.
        event_date_col = next(
            (d.column for d in intent.dims if d.kind == "time" and d.column), ""
        )
        if event_date_col:
            as_of_year = f'F.year(F.to_date(F.col("{event_date_col}")))'
        else:
            as_of_year = "F.year(F.current_date())"
        lines: list[str] = []
        for dim in intent.dims:
            if dim.kind == "time":
                unit = _SPARK_TRUNC.get(dim.unit, "month")
                lines.append(
                    f'result = result.withColumn("{dim.alias}", '
                    f'F.date_trunc("{unit}", F.to_date(F.col("{dim.column}"))))'
                )
            elif dim.kind == "age":
                # calendar-year difference to match SQL date_diff('year', dob, as_of)
                lines.append(
                    f'result = result.withColumn("{dim.alias}", '
                    f'({as_of_year} - F.year(F.to_date(F.col("{dim.column}")))).cast("int"))'
                )
                if band_width:
                    lines.append(
                        f'result = result.withColumn("{dim.alias}_band", '
                        f'F.format_string("%d-%d", '
                        f'(F.floor(F.col("{dim.alias}") / {band_width}) * {band_width}).cast("long"), '
                        f'(F.floor(F.col("{dim.alias}") / {band_width}) * {band_width} + {band_width - 1}).cast("long")))'
                    )
            elif dim.kind == "days_since":
                as_of_date = (
                    f'F.to_date(F.col("{event_date_col}"))'
                    if event_date_col else "F.current_date()"
                )
                lines.append(
                    f'result = result.withColumn("{dim.alias}", '
                    f'F.datediff({as_of_date}, F.to_date(F.col("{dim.column}"))))'
                )
        return lines

    def _filter_lines(self, intent: KPIIntent) -> list[str]:
        exprs = []
        age_alias = next((d.alias for d in intent.dims if d.kind == "age"), None)
        for filt in intent.filters:
            # T4: map the op through _SPARK_OPS and render the value as a safe
            # Python literal (repr for strings) — never inline a raw op/value into
            # the generated, subprocess-executed script. is_literal=True means the
            # value is a (non-numeric) string.
            op = map_comparison_op(filt.op, _SPARK_OPS)
            if filt.target == "__age__":
                if age_alias:
                    value = render_python_scalar_literal(
                        filt.value, treat_as_string=filt.is_literal
                    )
                    exprs.append(f'(F.col("{age_alias}") {op} {value})')
                continue
            if not is_safe_identifier(filt.target):
                continue
            value = render_python_scalar_literal(
                filt.value, treat_as_string=filt.is_literal
            )
            exprs.append(f'(F.col("{filt.target}") {op} {value})')
        return ["result = result.filter(" + " & ".join(exprs) + ")"] if exprs else []

    @staticmethod
    def _dim_out_name(dim, band_width: int | None) -> str:
        """The dim's OUTPUT column name: banded continuous cuts emit
        `<alias>_band` (exactly like the SQL/Polars paths)."""
        if band_width and dim.kind in ("age", "days_since"):
            return f"{dim.alias}_band"
        return dim.column if dim.kind == "column" else dim.alias

    def _group_cols(self, intent: KPIIntent, *, band_width: int | None = None) -> list[str]:
        return [f'"{self._dim_out_name(d, band_width)}"' for d in intent.dims]

    def _dim_select_cols(self, intent: KPIIntent, *, band_width: int | None = None) -> list[str]:
        return [f'"{self._dim_out_name(d, band_width)}"' for d in intent.dims]

    def _metric_agg_expr(self, m) -> str:
        col = m.column
        if m.fn == "sum":
            return f'F.round(F.sum(F.col("{col}")), 2)'
        if m.fn == "avg":
            return f'F.round(F.avg(F.col("{col}")), 2)'
        if m.fn == "count" and m.distinct:
            return f'F.countDistinct(F.col("{col}"))'
        if m.fn == "count":
            return "F.count(F.lit(1))"
        if m.fn in {"min", "max"}:
            return f'F.{m.fn}(F.col("{col}"))'
        return f'F.sum(F.col("{col}"))'

    def _agg_expr(self, metric) -> tuple[str, str]:
        if not metric:
            return 'F.count(F.lit(1)).alias("row_count")', "row_count"
        return f'{self._metric_agg_expr(metric)}.alias("{metric.alias}")', metric.alias

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
    parser = argparse.ArgumentParser(description="Generate PySpark KPI script from feature mappings.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--kpi-id", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--dialect", default="local", choices=["local", "databricks"])
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--schema", default="autoresearch")
    args = parser.parse_args(argv)
    gen = PySparkKPIGenerator(
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
