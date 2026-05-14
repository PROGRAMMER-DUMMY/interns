# Test Suite Overview

The active test surface covers the enterprise optimization foundation and governed workspace onboarding.

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
- workspace kickstart
- KPI feature resolution
- gated KPI SQL generation

Workspace-specific benchmarks require local or catalog-backed data to be provisioned first. Generated state, profiles, DuckDB databases, CSV/PDF dumps, parquet files, and logs are intentionally ignored by git.
