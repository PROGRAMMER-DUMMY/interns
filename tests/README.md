# Test Suite Overview

The active test surface covers the enterprise optimization foundation and the SQL optimization benchmark harness.

## Unit Tests

```bash
uv run python -m unittest tests.test_enterprise_optimization
```

These tests cover:

- command normalization
- SQL diff classification
- semantic contract ingestion
- optimization memory stats
- hotspot-driven planning

## SQL Benchmark Harness

The SQL benchmark files live in `tests/06_sql_optimization`.

```bash
uv run python tests/06_sql_optimization/experiment.py
uv run python tests/06_sql_optimization/evaluator.py
```

The benchmark requires local or catalog-backed data to be provisioned first. Generated state, profiles, DuckDB databases, CSV/PDF dumps, parquet files, and logs are intentionally ignored by git.
