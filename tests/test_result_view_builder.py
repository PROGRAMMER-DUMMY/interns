"""Tests for the generic KPI result-view SQL builder.

Covers metric/cuts parsing, time bucket detection, top-N, filter
extraction, ratio metrics, column resolution, and graceful fallback for
complex shapes. Uses synthetic KPI shapes across multiple domains to
prove the builder is workspace-agnostic.
"""
from __future__ import annotations

import pytest

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
