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
    """A metric phrased '<agg> / <same agg> for <group>' is a share-of-total BY
    <group>: the numerator is the aggregate WITHIN each group (PARTITION BY
    <group>) and the denominator is the grand total of the same aggregate
    (OVER ()), so each row is one group and its percentage of the whole — shares
    sum to ~100% across groups. The result grain is the group only; the
    descriptive cuts (e.g. Gender) do NOT subdivide the share.

    (This supersedes the earlier BUG-002 reading, which scoped the denominator
    to the group and grained by group+cuts, producing within-group composition
    instead of each group's share of the total.)
    """

    def test_numerator_per_group_denominator_is_grand_total(self):
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
        # Numerator is per-group, partitioned by the "for X" column.
        self.assertIn('PARTITION BY "departement"', sql)
        # Denominator is the grand total: a bare OVER () window.
        self.assertIn("OVER ()", sql)
        self.assertIn("percentage_share", sql)
        self.assertIn("NULLIF", sql)

        # Structurally: exactly one windowed agg partitions by the group, and
        # exactly one is the global grand total (empty window).
        parsed = parse_kpi(kpi)
        windowed = [a for a in parsed.aggregations if a.window is not None]
        self.assertTrue(windowed)
        self.assertTrue(
            any(a.window.partition_by == ('"departement"',) for a in windowed),
            "numerator must PARTITION BY the group column",
        )
        self.assertTrue(
            any(a.window.partition_by == () for a in windowed),
            "denominator must be the grand total (no partition)",
        )

        # Grain is the group only — the Gender cut does not subdivide the share.
        self.assertEqual(
            [d.alias for d in parsed.dimensions], ["departement"],
            "result grain must be the group column only, not group + cuts",
        )

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
        # ("Name"), never the phantom "departement" token.
        windowed = [a for a in parsed.aggregations if a.window is not None]
        self.assertTrue(
            any(a.window.partition_by == ('"Name"',) for a in windowed),
            "numerator must PARTITION BY the resolved physical department column",
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


if __name__ == "__main__":
    unittest.main()
