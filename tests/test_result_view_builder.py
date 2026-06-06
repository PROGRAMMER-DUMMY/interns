"""Tests for the generic KPI result-view SQL builder.

Covers metric/cuts parsing, time bucket detection, top-N, filter
extraction, ratio metrics, column resolution, and graceful fallback for
complex shapes. Uses synthetic KPI shapes across multiple domains to
prove the builder is workspace-agnostic.
"""
from __future__ import annotations

import unittest

from core.onboarding.kpi.result_view_builder import (
    Aggregation,
    Dimension,
    FilterClause,
    build_result_view_sql,
    parse_kpi,
)


def _kpi(**kwargs):
    base = {"name": "", "business_question": "", "metric": "", "cuts": "", "features": []}
    base.update(kwargs)
    return base


def test_parse_simple_sum_with_one_dimension():
    parsed = parse_kpi(_kpi(metric="sum(total_amount)", cuts="channel"))
    assert parsed.can_compose
    assert len(parsed.aggregations) == 1
    assert parsed.aggregations[0].fn == "sum"
    assert parsed.aggregations[0].column == "total_amount"
    assert len(parsed.dimensions) == 1
    assert parsed.dimensions[0].alias == "channel"


def test_parse_time_bucket_with_explicit_source_column():
    parsed = parse_kpi(_kpi(metric="sum(total_amount)", cuts="Month (order_date), channel"))
    assert parsed.can_compose
    assert len(parsed.dimensions) == 2
    month_dim = parsed.dimensions[0]
    assert "date_trunc('month'" in month_dim.expression
    assert "order_date" in month_dim.expression
    assert month_dim.alias == "month"
    assert parsed.dimensions[1].alias == "channel"


def test_parse_count_star_with_no_dimensions():
    parsed = parse_kpi(_kpi(metric="count(*)"))
    assert parsed.can_compose
    assert parsed.aggregations[0].column == "*"
    assert parsed.aggregations[0].alias == "row_count"
    assert not parsed.dimensions


def test_parse_count_distinct():
    parsed = parse_kpi(_kpi(metric="count(distinct customer_id)", cuts="region"))
    assert parsed.aggregations[0].distinct
    assert parsed.aggregations[0].fn == "count"
    assert parsed.aggregations[0].column == "customer_id"


def test_parse_top_n_from_name_sets_limit():
    parsed = parse_kpi(_kpi(name="Top 10 payers by amount", metric="sum(total_amount)", cuts="payer_id"))
    assert parsed.limit == 10


def test_parse_comparison_filter_from_cuts():
    parsed = parse_kpi(_kpi(metric="sum(total_amount)", cuts="channel, age > 50"))
    assert len(parsed.filters) == 1
    assert parsed.filters[0].column == "age"
    assert parsed.filters[0].op == ">"
    assert parsed.filters[0].value == "50"


def test_parse_quoted_literal_filter_aligned_with_cut_dimension():
    parsed = parse_kpi(_kpi(metric="sum(total_amount)", cuts="status = 'Refunded', channel"))
    assert any(f.column == "status" and f.value == "'Refunded'" for f in parsed.filters)


def test_parse_ratio_metric_emits_both_legs_and_ratio_column():
    parsed = parse_kpi(_kpi(metric="count(status = 'Refunded') / count(*)", cuts="channel"))
    assert parsed.can_compose
    assert parsed.ratio is not None
    numerator, denominator = parsed.ratio
    assert numerator.predicate and "Refunded" in numerator.predicate
    assert denominator.column == "*"


def test_complex_grain_mismatch_percentage_now_composes_via_window_function():
    """Mismatched-grain percentage used to fall back; it now composes via window functions."""
    parsed = parse_kpi(
        _kpi(
            metric="percentage of sum(distinct PatientID) / sum(distinct PatientID) for departement",
            cuts="Gender",
        )
    )
    assert parsed.can_compose
    # Should have at least one windowed aggregation.
    assert any(a.window is not None for a in parsed.aggregations)


def test_build_sql_for_simple_aggregation():
    kpi = _kpi(metric="sum(total_amount)", cuts="month (order_date), channel")
    sql = build_result_view_sql(
        kpi, kpi_id="kpi_001",
        feature_view='"kpi_001_features"',
        result_view='"kpi_001_results"',
    )
    assert "CREATE OR REPLACE VIEW \"kpi_001_results\" AS" in sql
    assert "SUM(\"total_amount\")" in sql
    assert "date_trunc('month'" in sql
    assert "GROUP BY" in sql
    assert "ORDER BY" in sql


def test_build_sql_for_top_n_with_dimensions():
    kpi = _kpi(name="Top 5 customers by revenue", metric="sum(total_amount)", cuts="customer_id")
    sql = build_result_view_sql(
        kpi, kpi_id="kpi_003",
        feature_view='"kpi_003_features"',
        result_view='"kpi_003_results"',
    )
    assert "ORDER BY sum_total_amount DESC" in sql
    assert "LIMIT 5" in sql


def test_build_sql_for_ratio_metric():
    kpi = _kpi(metric="count(status = 'Refunded') / count(*)", cuts="channel")
    sql = build_result_view_sql(
        kpi, kpi_id="kpi_002",
        feature_view='"kpi_002_features"',
        result_view='"kpi_002_results"',
    )
    assert "SUM(CASE WHEN" in sql
    assert "Refunded" in sql
    assert "COUNT(*)" in sql
    assert "ratio" in sql
    assert "/" in sql


def test_build_sql_fallback_includes_reason_comment_for_unparseable_metric():
    """When the metric is genuinely unparseable, the builder falls back with
    a clearly-commented SELECT *. Tested with a malformed metric expression."""
    kpi = _kpi(
        metric="some unparseable narrative metric description",
        cuts="Gender",
    )
    sql = build_result_view_sql(
        kpi, kpi_id="kpi_complex",
        feature_view='"kpi_complex_features"',
        result_view='"kpi_complex_results"',
    )
    assert "-- Generic builder fallback:" in sql
    assert "SELECT * FROM \"kpi_complex_features\"" in sql


def test_feature_mapping_resolves_alias_to_underlying_column():
    kpi = _kpi(
        metric="sum(Revenue)",
        cuts="month (order_date)",
        features=[
            {"feature": "Revenue", "source_columns": [{"column": "total_amount"}]},
            {"feature": "order_date", "source_columns": [{"column": "order_date"}]},
        ],
    )
    parsed = parse_kpi(kpi)
    assert parsed.aggregations[0].column == "total_amount"


def test_workspace_agnostic_no_healthcare_words_in_output_for_retail_kpi():
    kpi = _kpi(metric="sum(total_amount)", cuts="month (order_date), channel")
    sql = build_result_view_sql(
        kpi, kpi_id="kpi_001",
        feature_view='"kpi_001_features"',
        result_view='"kpi_001_results"',
    )
    for healthcare_word in ("medicare", "patient", "encounter", "payor", "claim"):
        assert healthcare_word not in sql.lower()


def test_min_max_aggregations():
    sql_min = build_result_view_sql(
        _kpi(metric="min(latency_ms)", cuts="service"),
        kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    sql_max = build_result_view_sql(
        _kpi(metric="max(latency_ms)", cuts="service"),
        kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    assert "MIN(\"latency_ms\")" in sql_min
    assert "MAX(\"latency_ms\")" in sql_max


def test_no_dimensions_no_filters_just_big_number():
    sql = build_result_view_sql(
        _kpi(metric="count(*)"),
        kpi_id="kpi_total", feature_view='"f"', result_view='"r"',
    )
    assert "COUNT(*)" in sql
    assert "GROUP BY" not in sql


# Tier A: window functions, HAVING, date arithmetic


def test_window_percent_of_total_emits_over_clause_and_ratio():
    kpi = _kpi(
        name="Percentage of total revenue by channel",
        metric="sum(total_amount)",
        cuts="channel",
    )
    sql = build_result_view_sql(
        kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    assert "SUM(\"total_amount\")" in sql
    assert "OVER ()" in sql, "percent-of-total needs OVER() with no partition"
    assert "percent_of_total" in sql
    assert "NULLIF" in sql


def test_window_share_of_group_partitions_by_group_column():
    kpi = _kpi(
        name="Share of region revenue",
        metric="sum(total_amount)",
        cuts="region, segment",
    )
    sql = build_result_view_sql(
        kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    assert "PARTITION BY \"region\"" in sql
    assert "percent_of_region" in sql


def test_window_running_total_emits_order_by_in_over():
    kpi = _kpi(
        name="Running total of orders by month",
        metric="sum(order_id)",
        cuts="month (order_date)",
    )
    sql = build_result_view_sql(
        kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    assert "running_" in sql
    assert "ORDER BY" in sql


def test_window_moving_average_emits_frame_clause():
    kpi = _kpi(
        name="Moving average 7 of daily orders",
        metric="sum(total_amount)",
        cuts="day (order_date)",
    )
    sql = build_result_view_sql(
        kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    assert "moving_avg_7" in sql
    assert "ROWS BETWEEN 6 PRECEDING AND CURRENT ROW" in sql


def test_window_rank_within_emits_row_number_over_partition():
    kpi = _kpi(
        name="Rank products within category by revenue",
        metric="sum(total_amount)",
        cuts="category, product_id",
    )
    sql = build_result_view_sql(
        kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    assert "ROW_NUMBER()" in sql
    assert "PARTITION BY \"category\"" in sql
    assert "ORDER BY sum_total_amount DESC" in sql


def test_having_clause_for_aggregate_filter():
    kpi = _kpi(
        name="Departments with at least 100 visits",
        metric="count(*)",
        cuts="department",
    )
    sql = build_result_view_sql(
        kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    assert "HAVING row_count > 100" in sql


def test_date_arithmetic_age_from_dob():
    kpi = _kpi(
        metric="count(distinct customer_id)",
        cuts="age (date_of_birth)",
    )
    sql = build_result_view_sql(
        kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    assert "date_diff('year'" in sql
    assert "\"date_of_birth\"" in sql
    assert "AS age" in sql


def test_date_arithmetic_days_since():
    kpi = _kpi(
        metric="count(*)",
        cuts="days since order_date",
    )
    sql = build_result_view_sql(
        kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    assert "date_diff('day'" in sql
    assert "days_since_order_date" in sql


def test_mismatched_grain_percentage_now_emits_window_function_instead_of_fallback():
    """KPI_002 shape: previously a fallback comment. Now it composes."""
    kpi = _kpi(
        name="Percentage share of lives by department",
        metric="percentage of sum(distinct PatientID) / sum(distinct PatientID) for departement",
        cuts="departement, Gender",
        features=[
            {"feature": "PatientID", "source_columns": [{"column": "PatientID"}]},
            {"feature": "departement", "source_columns": [{"column": "departement"}]},
            {"feature": "Gender", "source_columns": [{"column": "Gender"}]},
        ],
    )
    sql = build_result_view_sql(
        kpi, kpi_id="kpi_002", feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
    )
    # No fallback marker — it composes.
    assert "-- Generic builder fallback" not in sql
    # Uses window functions over the partition column.
    assert "PARTITION BY \"departement\"" in sql
    assert "percentage_share" in sql
    assert "NULLIF" in sql


def test_workspace_agnostic_no_healthcare_words_in_window_kpi_output():
    """Window-function path must stay workspace-agnostic too."""
    kpi = _kpi(
        name="Percentage of total revenue by channel",
        metric="sum(total_amount)",
        cuts="channel",
    )
    sql = build_result_view_sql(
        kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
    )
    for healthcare_word in ("medicare", "patient", "encounter", "payor", "claim"):
        assert healthcare_word not in sql.lower()


def _kpi_uc(**kwargs):
    base = {"name": "", "business_question": "", "metric": "", "cuts": "", "features": []}
    base.update(kwargs)
    return base


class ShareOfTotalByGroupTests(unittest.TestCase):
    """BUG-024: a metric phrased '<agg> / <same agg> for <group>' whose KPI also
    declares descriptive cuts is a share-of-total whose grain is the FULL cut set
    (group + every descriptive cut). The numerator counts WITHIN each full-grain
    cell (PARTITION BY all grain dimensions) and the denominator is the grand
    total of the same aggregate (OVER ()), so each row is one cell and its
    percentage of the whole population — shares sum to ~100% across all cells.

    (This reverses the prior group-only-grain reading: the KPI's stated cuts
    must subdivide the share, e.g. "percentage share of lives by gender, age,
    visit type, department" grains by all four, not by department alone. The
    denominator scope is intentionally left as the grand total.)
    """

    def test_descriptive_cuts_subdivide_the_grain(self):
        kpi = _kpi_uc(
            name="percentage share of lives by department",
            metric=(
                "percentage of sum(distinct PatientID) / "
                "sum(distinct PatientID) for departement"
            ),
            cuts="departement, Gender",
            features=[
                {"feature": "PatientID", "source_columns": [{"column": "PatientID"}]},
                {"feature": "departement", "source_columns": [{"column": "departement"}]},
                {"feature": "Gender", "source_columns": [{"column": "Gender"}]},
            ],
        )
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_002",
            feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
        )
        # Numerator partitions by the FULL grain (group + descriptive cut).
        self.assertIn('PARTITION BY "departement", "Gender"', sql)
        # Denominator is the grand total: a bare OVER () window.
        self.assertIn("OVER ()", sql)
        self.assertIn("percentage_share", sql)
        self.assertIn("NULLIF", sql)

        # Structurally: the numerator window partitions by every grain dimension,
        # and exactly one agg is the global grand total (empty window).
        parsed = parse_kpi(kpi)
        windowed = [a for a in parsed.aggregations if a.window is not None]
        self.assertTrue(windowed)
        self.assertTrue(
            any(a.window.partition_by == ('"departement"', '"Gender"') for a in windowed),
            "numerator must PARTITION BY every grain dimension (group + cuts)",
        )
        self.assertTrue(
            any(a.window.partition_by == () for a in windowed),
            "denominator must be the grand total (no partition)",
        )

        # Grain is the group AND every descriptive cut — the cuts subdivide.
        self.assertEqual(
            [d.alias for d in parsed.dimensions], ["departement", "gender"],
            "result grain must be group + every declared descriptive cut",
        )

    def test_share_emits_only_cuts_plus_single_metric_column(self):
        # The metric is a single logical column (percentage_share). The numerator
        # and denominator window aggregates are internal scaffolding and must NOT
        # leak as their own output columns — the cuts never named them.
        kpi = _kpi_uc(
            name="percentage share of lives by department",
            metric=(
                "percentage of sum(distinct PatientID) / "
                "sum(distinct PatientID) for departement"
            ),
            cuts="departement, Gender",
            features=[
                {"feature": "PatientID", "source_columns": [{"column": "PatientID"}]},
                {"feature": "departement", "source_columns": [{"column": "departement"}]},
                {"feature": "Gender", "source_columns": [{"column": "Gender"}]},
            ],
        )
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_002",
            feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
        )
        # The scaffolding aliases must NOT be projected anywhere.
        self.assertNotIn("per_group", sql)
        self.assertNotIn("_total AS", sql)
        self.assertNotIn("patientid_total", sql)
        # Exactly the declared cuts + the single percentage_share column are output
        # (DOUBLE is the inline CAST inside percentage_share, not an output column).
        import re as _re
        body = sql.split("SELECT DISTINCT", 1)[1].split("FROM", 1)[0]
        output_aliases = [
            a for a in _re.findall(r"\bAS\s+(\w+)", body) if a != "DOUBLE"
        ]
        self.assertEqual(output_aliases, ["departement", "gender", "percentage_share"])
        # The numerator/denominator windows are inlined into percentage_share.
        self.assertIn('OVER (PARTITION BY "departement", "Gender")', sql)
        self.assertIn("OVER ()", sql)

    def test_no_for_group_keeps_global_percent_of_total(self):
        # Without a "for <group>", the existing global percent-of-total path
        # (OVER ()) must be preserved.
        kpi = _kpi_uc(
            name="Percentage of total revenue by channel",
            metric="sum(total_amount)",
            cuts="channel",
        )
        sql = build_result_view_sql(
            kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
        )
        self.assertIn("OVER ()", sql)
        self.assertIn("percent_of_total", sql)


class PercentageSingleMetricColumnTests(unittest.TestCase):
    """A percentage metric is ONE logical output column across ALL percentage
    phrasings. The base aggregate and the denominator are internal scaffolding
    and must be inlined, never projected as their own columns."""

    def _output_aliases(self, sql: str) -> list[str]:
        import re as _re
        head = sql.split("SELECT", 1)[1]
        body = head[head.index("\n"):].split("FROM", 1)[0]
        return [a for a in _re.findall(r"\bAS\s+(\w+)", body) if a != "DOUBLE"]

    def test_percent_of_total_emits_single_metric_column(self):
        kpi = _kpi_uc(
            name="Percent of total revenue by channel",
            metric="percent of total sum(revenue)",
            cuts="channel",
            features=[
                {"feature": "revenue", "source_columns": [{"column": "revenue"}]},
                {"feature": "channel", "source_columns": [{"column": "channel"}]},
            ],
        )
        sql = build_result_view_sql(kpi, kpi_id="k", feature_view='"f"', result_view='"r"')
        self.assertEqual(self._output_aliases(sql), ["channel", "percent_of_total"])
        self.assertNotIn("total_sum", sql)  # denominator scaffolding not projected
        self.assertIn("OVER ()", sql)       # grand-total inlined

    def test_percent_of_group_emits_single_metric_column(self):
        kpi = _kpi_uc(
            name="share of region trend",
            metric="sum(amount)",
            cuts="region",
            features=[
                {"feature": "region", "source_columns": [{"column": "region"}]},
                {"feature": "amount", "source_columns": [{"column": "amount"}]},
            ],
        )
        sql = build_result_view_sql(kpi, kpi_id="k", feature_view='"f"', result_view='"r"')
        self.assertEqual(self._output_aliases(sql), ["region", "percent_of_region"])
        self.assertNotIn("_per_region AS", sql)  # per-group scaffolding not projected
        self.assertIn('OVER (PARTITION BY "region")', sql)


class WindowedOnlyDedupRegressionTests(unittest.TestCase):
    """BUG-012: a mismatched-grain percentage KPI parses into a window-only plan
    (every aggregation carries an OVER clause, no plain aggregations), so the
    builder emits no GROUP BY. Without deduping, the view returns one row per
    source record (~10k duplicate grain rows). Windowed-only mode must emit
    SELECT DISTINCT to collapse to one row per grain. The plain GROUP BY path
    must NOT gain DISTINCT.
    """

    def _windowed_only_kpi(self):
        return _kpi_uc(
            name="percentage share of lives by department",
            metric=(
                "percentage of sum(distinct PatientID) / "
                "sum(distinct PatientID) for departement"
            ),
            cuts="departement, Gender",
            features=[
                {"feature": "PatientID", "source_columns": [{"column": "PatientID"}]},
                {"feature": "departement", "source_columns": [{"column": "departement"}]},
                {"feature": "Gender", "source_columns": [{"column": "Gender"}]},
            ],
        )

    def test_windowed_only_kpi_emits_select_distinct_for_duckdb(self):
        kpi = self._windowed_only_kpi()
        # Confirm this really is a windowed-only plan (precondition for the fix).
        parsed = parse_kpi(kpi)
        self.assertTrue(any(a.window is not None for a in parsed.aggregations))
        self.assertFalse([a for a in parsed.aggregations if a.window is None])

        sql = build_result_view_sql(
            kpi, kpi_id="kpi_002",
            feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
            dialect="duckdb",
        )
        self.assertIn("SELECT DISTINCT", sql)
        # Window math preserved.
        self.assertIn("percentage_share", sql)
        self.assertIn('PARTITION BY "departement"', sql)
        # Windowed-only mode still emits no GROUP BY.
        self.assertNotIn("GROUP BY", sql)

    def test_windowed_only_kpi_emits_select_distinct_for_databricks(self):
        sql = build_result_view_sql(
            self._windowed_only_kpi(), kpi_id="kpi_002",
            feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
            dialect="databricks",
        )
        self.assertIn("SELECT DISTINCT", sql)
        self.assertIn("percentage_share", sql)

    def test_plain_group_by_kpi_does_not_get_distinct(self):
        # Guard against regressing the normal aggregating path: a plain GROUP BY
        # KPI already collapses to one row per grain and must NOT gain DISTINCT.
        kpi = _kpi_uc(metric="sum(total_amount)", cuts="month (order_date), channel")
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_001",
            feature_view='"kpi_001_features"', result_view='"kpi_001_results"',
        )
        self.assertNotIn("SELECT DISTINCT", sql)
        self.assertIn("GROUP BY", sql)


class AgeAsOfEventDateRegressionTests(unittest.TestCase):
    """BUG-005: age date arithmetic must be measured as-of the event/service
    date when the KPI exposes one, falling back to CURRENT_DATE when absent."""

    def test_age_uses_event_date_when_time_grain_present(self):
        kpi = _kpi_uc(
            name="amount paid trend for patients above 50",
            metric="sum(PaidAmount)",
            cuts="Month (ServiceDate), LineOfBusiness, Age(DOB)",
            features=[
                {"feature": "PaidAmount", "source_columns": [{"column": "PaidAmount"}]},
                {"feature": "ServiceDate", "source_columns": [{"column": "ServiceDate"}]},
                {"feature": "DOB", "source_columns": [{"column": "DOB"}]},
                {"feature": "LineOfBusiness",
                 "source_columns": [{"column": "LineOfBusiness"}]},
            ],
        )
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_001",
            feature_view='"kpi_001_features"', result_view='"kpi_001_results"',
        )
        # Age is computed relative to the event date, not today.
        self.assertIn(
            "date_diff('year', CAST(\"DOB\" AS DATE), CAST(\"ServiceDate\" AS DATE))",
            sql,
        )
        self.assertNotIn(
            "date_diff('year', CAST(\"DOB\" AS DATE), CURRENT_DATE)", sql,
        )

    def test_percentage_of_total_name_does_not_emit_phantom_age_dimension(self):
        # Regression: "percent`age of` total" (and "aver`age of` X") must NOT be
        # read as "age of <noun>". Without a leading word boundary on the age
        # pattern this produced a phantom `date_diff('year', CAST("total" AS
        # DATE)) AS age` dimension on a percentage KPI -> broken, ungrouped SQL.
        kpi = _kpi_uc(
            name=(
                "How many encounters had zero payer coverage, and what percentage "
                "of total encounters does this represent?"
            ),
            metric="count(*)",
            cuts="PAYER_COVERAGE = 0",
            features=[
                {"feature": "PAYER_COVERAGE",
                 "source_columns": [{"column": "PAYER_COVERAGE"}]},
            ],
        )
        parsed = parse_kpi(kpi)
        for dim in parsed.dimensions:
            self.assertNotIn("date_diff", dim.expression)
            self.assertNotEqual(dim.alias, "age")
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_x",
            feature_view='"kpi_x_features"', result_view='"kpi_x_results"',
        )
        self.assertNotIn('CAST("total" AS DATE)', sql)
        self.assertNotIn("AS age", sql)

    def test_age_falls_back_to_current_date_without_event_date(self):
        # No time-grain source column => no event date => CURRENT_DATE (legacy).
        kpi = _kpi_uc(
            metric="count(distinct customer_id)",
            cuts="age (date_of_birth)",
        )
        sql = build_result_view_sql(
            kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
        )
        self.assertIn(
            "date_diff('year', CAST(\"date_of_birth\" AS DATE), CURRENT_DATE)", sql,
        )

    def test_days_since_also_uses_event_date_when_present(self):
        kpi = _kpi_uc(
            metric="count(*)",
            cuts="Month (event_date), days since signup_date",
            features=[
                {"feature": "event_date", "source_columns": [{"column": "event_date"}]},
                {"feature": "signup_date", "source_columns": [{"column": "signup_date"}]},
            ],
        )
        sql = build_result_view_sql(
            kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
        )
        self.assertIn(
            "date_diff('day', CAST(\"signup_date\" AS DATE), "
            "CAST(\"event_date\" AS DATE))",
            sql,
        )


class ForGroupTokenResolvesToRealColumnTests(unittest.TestCase):
    """BUG-011: the "for <group>" partition token must resolve to a column the
    features view actually emits, even when the group word does NOT match any
    feature label exactly (e.g. a misspelled/aliased dimension whose redundant
    feature was dropped by the BUG-001 feature-dedup). A PARTITION BY on a
    phantom token makes the generated SQL non-executable.
    """

    def _emitted_columns(self, kpi):
        # The features view emits one column per feature (its resolved column).
        cols = set()
        for f in kpi["features"]:
            srcs = f.get("source_columns") or []
            cols.add(srcs[0]["column"] if srcs else (f.get("feature") or f.get("name")))
        return cols

    def _partition_cols(self, parsed):
        out = set()
        for agg in parsed.aggregations:
            if agg.window is not None:
                for term in agg.window.partition_by:
                    out.add(term)
        return out

    def test_group_word_aliased_dimension_resolves_to_emitted_physical_column(self):
        # Real KPI_002 shape AFTER BUG-001 dropped the redundant `departement`
        # feature: the surviving department dimension is `departments.Name`,
        # emitted as the column "Name". The cut "Department Name" resolves to
        # "Name"; the group token "departement" (a misspelling of the dataset
        # `departments`) must ALSO resolve to "Name", never to a phantom
        # "departement" column.
        ds = "/data/warehouse/departments.csv"
        kpi = _kpi_uc(
            name="percentage share of lives by department",
            metric=(
                "percentage of sum(distinct PatientID) / "
                "sum(disitnct PatientID) for departement"
            ),
            cuts="Department Name, VisitType, Gender",
            features=[
                {"feature": "PatientID",
                 "source_columns": [{"dataset": "/data/warehouse/transactions.csv",
                                     "column": "PatientID"}]},
                {"feature": "Name",
                 "source_columns": [{"dataset": ds, "column": "Name"}]},
                {"feature": "VisitType",
                 "source_columns": [{"dataset": "/data/warehouse/transactions.csv",
                                     "column": "VisitType"}]},
                {"feature": "Gender",
                 "source_columns": [{"dataset": "/data/warehouse/patients.csv",
                                     "column": "Gender"}]},
            ],
        )
        parsed = parse_kpi(kpi)
        emitted = self._emitted_columns(kpi)

        # The phantom token must NEVER appear in any partition.
        partitions = self._partition_cols(parsed)
        self.assertNotIn('"departement"', partitions)

        # Every partition term that is a bare quoted column must reference a
        # column the feature set actually emits.
        for term in partitions:
            bare = term.strip().strip('"')
            if "(" in term:  # an expression (e.g. date_trunc) — not a bare column
                continue
            self.assertIn(
                bare, emitted,
                f"partition references {bare!r} which is absent from the feature set",
            )

        # The per-group numerator must partition by the REAL department column
        # ("Name"), never the phantom "departement" token. Under BUG-024 the
        # descriptive cuts subdivide the grain, so the numerator window is the
        # full cut set (Name + VisitType + Gender) — "Name" must be present and
        # resolved, and the phantom token must be absent.
        windowed = [a for a in parsed.aggregations if a.window is not None]
        numerator = next(
            (a for a in windowed if a.window.partition_by), None
        )
        self.assertIsNotNone(numerator, "a per-cell numerator window must exist")
        self.assertEqual(
            numerator.window.partition_by, ('"Name"', '"VisitType"', '"Gender"'),
            "numerator must PARTITION BY the full resolved grain (group + cuts)",
        )

    def test_unresolvable_group_token_does_not_emit_phantom_partition(self):
        # A group word that matches NOTHING (no feature label, no dataset stem)
        # must not produce a PARTITION BY on a non-existent column; the builder
        # degrades to an executable global-total denominator instead.
        kpi = _kpi_uc(
            name="percentage share",
            metric=(
                "percentage of sum(distinct user_id) / "
                "sum(distinct user_id) for zzqqxx"
            ),
            cuts="channel",
            features=[
                {"feature": "user_id",
                 "source_columns": [{"dataset": "/d/events.csv", "column": "user_id"}]},
                {"feature": "channel",
                 "source_columns": [{"dataset": "/d/events.csv", "column": "channel"}]},
            ],
        )
        parsed = parse_kpi(kpi)
        emitted = self._emitted_columns(kpi)
        for term in self._partition_cols(parsed):
            bare = term.strip().strip('"')
            if "(" in term:
                continue
            self.assertIn(bare, emitted)
        self.assertNotIn('"zzqqxx"', self._partition_cols(parsed))

        # Still executable + still a percentage share.
        sql = build_result_view_sql(
            kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
        )
        self.assertNotIn('"zzqqxx"', sql)
        self.assertIn("percentage_share", sql)


class DenominatorScopeTests(unittest.TestCase):
    """Phase 1: denominator-scope facet wired end-to-end through build_result_view_sql.

    The live bug: a recorded ``within_department`` scope for kpi_002 was ignored
    and the generator emitted ``OVER ()`` instead of ``OVER (PARTITION BY <group>)``.
    These tests close that gap.
    """

    def _pct_kpi(self, *, extra_cuts: str = "") -> dict:
        cuts = "departement, Gender"
        if extra_cuts:
            cuts += f", {extra_cuts}"
        return _kpi(
            name="Percentage share of lives by department",
            metric=(
                "percentage of sum(distinct PatientID) / "
                "sum(distinct PatientID) for departement"
            ),
            cuts=cuts,
            features=[
                {"feature": "PatientID", "source_columns": [{"column": "PatientID"}]},
                {"feature": "departement", "source_columns": [{"column": "departement"}]},
                {"feature": "Gender", "source_columns": [{"column": "Gender"}]},
            ],
        )

    def test_within_group_scope_flips_denominator_to_partition_by(self):
        """A ``within_department`` scope must emit OVER (PARTITION BY <group>)
        for the denominator, not OVER ()."""
        kpi = self._pct_kpi()
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_002",
            feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
            denominator_scope="within_department",
        )
        # Denominator window must be scoped to the partition column.
        self.assertIn('PARTITION BY "departement"', sql)
        # Grand-total OVER () must NOT be present (it has been replaced).
        self.assertNotIn("OVER ()", sql)
        # percentage_share expression still present.
        self.assertIn("percentage_share", sql)
        self.assertIn("NULLIF", sql)

    def test_default_scope_none_still_emits_grand_total_over(self):
        """No denominator_scope (default) must keep OVER () — no regression."""
        kpi = self._pct_kpi()
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_002",
            feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
            denominator_scope=None,
        )
        self.assertIn("OVER ()", sql)
        self.assertIn("percentage_share", sql)

    def test_explicit_grand_total_scope_still_emits_over(self):
        """Explicit ``grand_total`` must behave identically to None."""
        kpi = self._pct_kpi()
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_002",
            feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
            denominator_scope="grand_total",
        )
        self.assertIn("OVER ()", sql)

    def test_within_group_scope_emits_auditable_comment(self):
        """The design requires a SQL comment recording the denominator scope for
        auditability (design/kpi_intent_contract.md §2 'Reported')."""
        kpi = self._pct_kpi()
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_002",
            feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
            denominator_scope="within_department",
        )
        self.assertIn("-- denominator_scope:", sql)
        self.assertIn("within_department", sql)

    def test_grand_total_scope_emits_auditable_comment(self):
        """Grand-total scope also emits an auditable comment (None defaults
        to ``grand_total`` label)."""
        kpi = self._pct_kpi()
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_002",
            feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
            denominator_scope=None,
        )
        self.assertIn("-- denominator_scope:", sql)

    def test_within_group_scope_grain_still_includes_all_cuts(self):
        """BUG-024 regression: the within-group scope must not drop descriptive
        cuts from the grain.  Both departement and Gender must remain."""
        kpi = self._pct_kpi()
        sql = build_result_view_sql(
            kpi, kpi_id="kpi_002",
            feature_view='"kpi_002_features"', result_view='"kpi_002_results"',
            denominator_scope="within_department",
        )
        # Numerator still partitions by the full grain.
        self.assertIn('"departement"', sql)
        self.assertIn('"Gender"', sql)

    def test_within_scope_parse_kpi_records_denominator_scope_field(self):
        """parse_kpi records the resolved denominator_scope in ParsedKPI for
        downstream audit."""
        kpi = self._pct_kpi()
        parsed = parse_kpi(kpi, denominator_scope="within_department")
        self.assertIsNotNone(parsed.denominator_scope)
        self.assertIn("within_department", parsed.denominator_scope)

    def test_none_scope_parse_kpi_records_grand_total_default(self):
        """With no scope, denominator_scope is recorded as 'grand_total' label."""
        kpi = self._pct_kpi()
        parsed = parse_kpi(kpi, denominator_scope=None)
        # For a percentage-share KPI, denominator_scope is set.
        self.assertIsNotNone(parsed.denominator_scope)
        self.assertIn("grand_total", parsed.denominator_scope)

    def test_non_percentage_kpi_denominator_scope_stays_none(self):
        """parse_kpi on a non-percentage-share KPI must not set denominator_scope."""
        kpi = _kpi(metric="sum(total_amount)", cuts="channel")
        parsed = parse_kpi(kpi, denominator_scope="within_department")
        self.assertIsNone(parsed.denominator_scope)


class GrainBucketingBlockTests(unittest.TestCase):
    """A share/percentage metric cut by a RAW exact continuous dimension (exact
    integer age / days-since) must HARD-BLOCK and propose bands instead of
    emitting the exploded GROUP BY that fragments the share into one tiny row per
    value. A recorded bucketing decision lets generation proceed. A non-share KPI
    with the same age cut is unaffected. Generic fixtures only (orders/customers).
    """

    def _share_age_kpi(self):
        # percent_of_group share metric cut by a raw exact-age dimension.
        return _kpi_uc(
            name="share of customers by age",
            metric="share of count(*) for region",
            cuts="region, age(date_of_birth)",
            features=[
                {"feature": "region", "source_columns": [{"column": "region"}]},
                {"feature": "date_of_birth",
                 "source_columns": [{"column": "date_of_birth"}]},
            ],
        )

    def test_share_with_raw_age_cut_blocks_parse_kpi(self):
        parsed = parse_kpi(self._share_age_kpi())
        self.assertFalse(parsed.can_compose)
        self.assertIsNotNone(parsed.grain_bucketing_block)
        # No exploded GROUP BY dimension on the exact age was emitted.
        self.assertEqual(parsed.dimensions, [])
        self.assertFalse(
            any("date_diff" in (d.expression or "") for d in parsed.dimensions)
        )

    def test_block_proposes_bands_for_the_raw_continuous_cut(self):
        parsed = parse_kpi(self._share_age_kpi())
        block = parsed.grain_bucketing_block
        self.assertEqual(block["recommended"], "band_continuous_cuts")
        self.assertIn("exact_value_grain", block["alternatives"])
        self.assertTrue(block["proposals"])
        proposal = block["proposals"][0]
        self.assertEqual(proposal["source_column"], "date_of_birth")
        self.assertEqual(proposal["unit"], "year")
        self.assertIn("band", proposal["proposed_bucket"].lower())

    def test_build_sql_emits_blocked_marker_not_exploded_group_by(self):
        sql = build_result_view_sql(
            self._share_age_kpi(),
            kpi_id="kpi_share", feature_view='"f"', result_view='"r"',
        )
        self.assertIn("-- BLOCKED: grain-bucketing decision required", sql)
        self.assertIn("propose:", sql)
        # The exploded per-exact-value GROUP BY must NOT be emitted: the body is
        # the blocked SELECT * fallback, with no executable GROUP BY clause.
        self.assertNotIn("date_diff('year'", sql)
        self.assertIn('SELECT * FROM "f"', sql)
        # No GROUP BY clause in the SQL body (the phrase may appear only inside
        # the leading -- comment lines explaining what was suppressed).
        body = "\n".join(
            ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")
        )
        self.assertNotIn("GROUP BY", body)

    def test_recorded_bucketing_decision_lets_view_generate(self):
        # With a recorded decision, generation proceeds (no block).
        parsed = parse_kpi(
            self._share_age_kpi(), grain_bucketing="band_continuous_cuts"
        )
        self.assertIsNone(parsed.grain_bucketing_block)
        sql = build_result_view_sql(
            self._share_age_kpi(),
            kpi_id="kpi_share", feature_view='"f"', result_view='"r"',
            grain_bucketing="band_continuous_cuts",
        )
        self.assertNotIn("-- BLOCKED: grain-bucketing", sql)

    def test_age_threshold_filter_does_not_trigger_block(self):
        # `age > 50` is a FILTER, not a grouping dimension — must not block.
        kpi = _kpi_uc(
            name="share of customers above 50 by region",
            metric="share of count(*) for region",
            cuts="region, age(date_of_birth) > 50",
            features=[
                {"feature": "region", "source_columns": [{"column": "region"}]},
                {"feature": "date_of_birth",
                 "source_columns": [{"column": "date_of_birth"}]},
            ],
        )
        parsed = parse_kpi(kpi)
        self.assertIsNone(parsed.grain_bucketing_block)

    def test_non_share_kpi_with_age_cut_still_generates(self):
        # A plain (non-share) aggregation cut by exact age generates as before.
        kpi = _kpi_uc(
            metric="count(distinct customer_id)",
            cuts="region, age(date_of_birth)",
            features=[
                {"feature": "customer_id",
                 "source_columns": [{"column": "customer_id"}]},
                {"feature": "region", "source_columns": [{"column": "region"}]},
                {"feature": "date_of_birth",
                 "source_columns": [{"column": "date_of_birth"}]},
            ],
        )
        parsed = parse_kpi(kpi)
        self.assertIsNone(parsed.grain_bucketing_block)
        self.assertTrue(parsed.can_compose)
        sql = build_result_view_sql(
            kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
        )
        # Exact-age dimension still emitted for the non-share metric.
        self.assertIn("date_diff('year'", sql)
        self.assertIn("GROUP BY", sql)

    def test_bug005_event_date_anchoring_unchanged_for_non_share_age_kpi(self):
        # BUG-005 anchoring is untouched: a non-share age KPI with a time-grain
        # cut still anchors age to the event date, not CURRENT_DATE.
        kpi = _kpi_uc(
            metric="sum(total_amount)",
            cuts="Month (order_date), age(date_of_birth)",
            features=[
                {"feature": "total_amount",
                 "source_columns": [{"column": "total_amount"}]},
                {"feature": "order_date",
                 "source_columns": [{"column": "order_date"}]},
                {"feature": "date_of_birth",
                 "source_columns": [{"column": "date_of_birth"}]},
            ],
        )
        sql = build_result_view_sql(
            kpi, kpi_id="k", feature_view='"f"', result_view='"r"',
        )
        self.assertIn(
            "date_diff('year', CAST(\"date_of_birth\" AS DATE), "
            "CAST(\"order_date\" AS DATE))",
            sql,
        )

    def test_share_kpi_without_continuous_cut_is_not_blocked(self):
        # A share metric cut only by categorical dimensions must still compose.
        kpi = _kpi_uc(
            name="share of customers by region",
            metric="share of count(*) for region",
            cuts="region, channel",
            features=[
                {"feature": "region", "source_columns": [{"column": "region"}]},
                {"feature": "channel", "source_columns": [{"column": "channel"}]},
            ],
        )
        parsed = parse_kpi(kpi)
        self.assertIsNone(parsed.grain_bucketing_block)
        self.assertTrue(parsed.can_compose)


class PreviewRowCapTests(unittest.TestCase):
    """Task B: preview row cap — PREVIEW_ROW_CAP constant, truncation note, no magic numbers."""

    def test_preview_row_cap_constant_is_defined_and_small(self):
        """PREVIEW_ROW_CAP must be a named constant, not scattered magic numbers."""
        from core.onboarding.workspace.flow import PREVIEW_ROW_CAP
        self.assertIsInstance(PREVIEW_ROW_CAP, int)
        self.assertGreater(PREVIEW_ROW_CAP, 0)
        # The cap is expected to be <= 10 (default is 10 per task spec)
        self.assertLessEqual(PREVIEW_ROW_CAP, 10)

    def test_render_query_result_table_emits_truncation_note_when_capped(self):
        """render_query_result_table with limit=N and exactly N rows emits a suffix note."""
        from core.presentation.console_tables import render_query_result_table

        class _FakeCursor:
            description = [("col",)]
            def fetchmany(self, n):
                return [(i,) for i in range(n)]
            def fetchall(self):
                return [(i,) for i in range(20)]

        md = render_query_result_table(_FakeCursor(), limit=5)
        self.assertIn("showing first 5 rows", md)

    def test_render_query_result_table_no_note_when_under_cap(self):
        """No truncation note when result fits within the limit."""
        from core.presentation.console_tables import render_query_result_table

        class _FakeCursor:
            description = [("col",)]
            def fetchmany(self, n):
                return [(1,), (2,)]   # fewer than limit
            def fetchall(self):
                return [(1,), (2,)]

        md = render_query_result_table(_FakeCursor(), limit=10)
        self.assertNotIn("showing first", md)

    def test_flow_preview_uses_preview_row_cap_constant(self):
        """The flow's _write_result_preview must honour PREVIEW_ROW_CAP, not hard-code 20.

        We verify this by checking the DuckDB LIMIT in the live execution path through
        the constant — the constant is used in both the `results()` default and the
        inline call inside `_advance_until_stop()`.
        """
        import inspect
        from core.onboarding.workspace.flow import PREVIEW_ROW_CAP, WorkspaceFlow

        src = inspect.getsource(WorkspaceFlow._write_result_preview)
        # The method must reference preview_rows (the parameter), not the literal 20.
        self.assertIn("preview_rows", src)
        # The literal 20 must not appear hard-coded inside the method body.
        self.assertNotIn("= 20", src)
        self.assertNotIn("(20)", src)
        # The constant value must equal 10 per task spec.
        self.assertEqual(PREVIEW_ROW_CAP, 10)

    def test_flow_advance_uses_preview_row_cap_constant_not_literal(self):
        """The _advance_until_stop() path must call _write_result_preview(preview_rows=PREVIEW_ROW_CAP)."""
        import inspect
        from core.onboarding.workspace import flow as flow_module

        # Locate the _advance_until_stop method source
        src = inspect.getsource(flow_module.WorkspaceFlow._advance_until_stop)
        # It must reference PREVIEW_ROW_CAP, not the literal 20
        self.assertIn("PREVIEW_ROW_CAP", src)
        self.assertNotIn("preview_rows=20", src)


if __name__ == "__main__":
    unittest.main()
