"""Regression: KPI result views must not be created in the source schema.

Origin (2026-07-26 audit): the harness created
`healthcare_rcm.bronze.kpi_002_results` -- a gold-grain view in the RAW SOURCE
schema -- because `flow.py` passed `databricks_source["schema"]` straight
through. Onboarding's `SHOW TABLES` then re-discovered it as a bronze SOURCE,
landing it in `manifest.yaml` with the computed `percentage_share` column inside
its natural key. The pipeline's output became its own input.
"""
from __future__ import annotations

import inspect
import unittest

from core.onboarding.kpi.execution_harness import (
    DEFAULT_RESULT_SCHEMA,
    KPIExecutionHarness,
    result_schema_for,
)


class ResultSchemaTests(unittest.TestCase):
    def test_source_schema_is_never_the_result_schema(self):
        settings = {"databricks_source": {"catalog": "retail", "schema": "bronze"}}
        self.assertNotEqual(result_schema_for(settings), "bronze")

    def test_default_result_schema_is_gold(self):
        settings = {"databricks_source": {"catalog": "retail", "schema": "bronze"}}
        self.assertEqual(result_schema_for(settings), "gold")
        self.assertEqual(DEFAULT_RESULT_SCHEMA, "gold")

    def test_an_explicit_result_schema_is_honoured(self):
        settings = {"databricks_source": {
            "catalog": "retail", "schema": "bronze", "result_schema": "kpi_marts",
        }}
        self.assertEqual(result_schema_for(settings), "kpi_marts")

    def test_missing_or_empty_config_yields_gold(self):
        self.assertEqual(result_schema_for({}), "gold")
        self.assertEqual(result_schema_for({"databricks_source": {}}), "gold")
        self.assertEqual(
            result_schema_for({"databricks_source": {"result_schema": "   "}}), "gold")

    def test_harness_default_schema_is_the_result_schema(self):
        default = inspect.signature(KPIExecutionHarness.__init__).parameters["schema"].default
        self.assertEqual(default, DEFAULT_RESULT_SCHEMA)


class FlowCallSiteTests(unittest.TestCase):
    def test_flow_passes_the_result_schema_to_the_harness(self):
        from core.onboarding.workspace import flow

        src = inspect.getsource(flow)
        self.assertIn("schema=result_schema_for(settings)", src)

    def test_the_sql_generator_still_gets_the_source_schema(self):
        # Deliberate asymmetry: the generator READS source tables and needs the
        # declared source schema; only the harness's result-view TARGET moves to
        # gold. Asserting the source schema is absent everywhere would be wrong.
        from core.onboarding.workspace import flow

        src = inspect.getsource(flow)
        self.assertIn('schema=db_source["schema"]', src,
                      "the SQL generator must still read from the source schema")


if __name__ == "__main__":
    unittest.main()
