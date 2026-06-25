"""Regression tests for the production-hardening tranches on the medallion
generator: data-contract population/application, idempotent silver MERGE, the
5 DQ dimensions, the SLA contract, and the SCD2 dimension emitter.
"""
from __future__ import annotations

import unittest

import duckdb

from core.medallion.merge_emitter import emit_scd2_merge, emit_silver_merge
from core.medallion.silver_contract import TableContract
from core.medallion.design import (
    _seed_accuracy_assertions,
    _seed_type_casts,
    _build_sla_contract,
)


class ContractRoundTripTests(unittest.TestCase):
    def test_key_rename_survives_round_trip(self):
        tc = TableContract(key_rename={"Id": "encounter_id"},
                           dedup_keys=["source_system", "encounter_id"])
        back = TableContract.from_dict(tc.to_dict())
        self.assertEqual(back.key_rename, {"Id": "encounter_id"})

    def test_seed_temporal_casts_and_money_accuracy(self):
        schema = {"Id": "string", "START": "string", "BASE_COST": "double", "NAME": "string"}
        casts = _seed_type_casts(schema, exclude={"Id"})
        self.assertIn("START", casts)
        self.assertEqual(casts["START"]["to"], "TIMESTAMP")
        accuracy = _seed_accuracy_assertions(schema, exclude=set())
        ids = {a["id"] for a in accuracy}
        self.assertIn("nonneg_base_cost", ids)
        self.assertTrue(all(a["min_value"] == 0 for a in accuracy))


class IdempotentMergeTests(unittest.TestCase):
    def _con(self):
        con = duckdb.connect()
        con.execute("CREATE SCHEMA silver; CREATE SCHEMA gold;")
        return con

    def test_silver_merge_is_idempotent(self):
        con = self._con()
        con.execute("CREATE TABLE bronze_src AS SELECT * FROM (VALUES (1,'a'),(2,'b')) t(encounter_id, v);")
        p0 = ("CREATE OR REPLACE TABLE silver.encounter AS\n"
              "WITH unioned AS (SELECT *, 's' AS source_system FROM bronze_src)\nSELECT * FROM unioned;")
        merge = emit_silver_merge("encounter", ["source_system", "encounter_id"], p0)
        con.execute("DROP TABLE IF EXISTS silver.encounter;")
        run = lambda: [con.execute(s) for s in merge.split(";") if s.strip()]
        run()
        n1 = con.execute("SELECT COUNT(*) FROM silver.encounter").fetchone()[0]
        run()
        n2 = con.execute("SELECT COUNT(*) FROM silver.encounter").fetchone()[0]
        self.assertEqual(n1, n2)
        self.assertEqual(n1, 2)

    def test_scd2_idempotent_and_tracks_history(self):
        con = self._con()
        con.execute("CREATE TABLE src AS SELECT * FROM (VALUES (1,'free'),(2,'free')) t(user_id, plan);")
        scd2 = emit_scd2_merge("dim_user", ["user_id"], ["plan"], "SELECT user_id, plan FROM src")
        run = lambda: [con.execute(s) for s in scd2.split(";") if s.strip()]
        run()
        n1 = con.execute("SELECT COUNT(*) FROM gold.dim_user").fetchone()[0]
        run()
        n2 = con.execute("SELECT COUNT(*) FROM gold.dim_user").fetchone()[0]
        self.assertEqual(n1, n2)  # idempotent
        con.execute("DELETE FROM src; INSERT INTO src VALUES (1,'premium'),(2,'free');")
        run()
        u1 = con.execute(
            "SELECT COUNT(*) FROM gold.dim_user WHERE user_id=1").fetchone()[0]
        self.assertEqual(u1, 2)  # closed 'free' + current 'premium'
        cur = con.execute(
            "SELECT plan FROM gold.dim_user WHERE user_id=1 AND valid_to IS NULL").fetchone()[0]
        self.assertEqual(cur, "premium")


class SlaContractTests(unittest.TestCase):
    def test_sla_contract_picks_freshness_column(self):
        from types import SimpleNamespace
        manifest = SimpleNamespace(silver=[SimpleNamespace(name="encounter")])
        contract = SimpleNamespace(tables={
            "encounter": TableContract(type_casts={"START": __import__(
                "core.medallion.silver_contract", fromlist=["TypeCast"]).TypeCast("string", "TIMESTAMP")}),
        })
        sla = _build_sla_contract("demo", manifest, contract)
        entry = sla["tables"][0]
        self.assertEqual(entry["freshness_column"], "START")
        self.assertEqual(entry["max_lag_hours"], 24)
        self.assertIn("retention_policy", sla)


if __name__ == "__main__":
    unittest.main()
