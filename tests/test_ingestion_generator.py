"""Ingestion generator: method choice, emitted-code rules, additive-only, no secrets."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.delegation import routing_for
from core.provisioning.ingestion import (
    METHOD_AUTO_LOADER,
    METHOD_COPY_INTO,
    METHOD_JDBC,
    METHOD_KAFKA,
    METHOD_UNSUPPORTED,
    choose_method,
    generate_ingestion,
    safe_name,
)
from core.provisioning.plan import build_provision_plan

# Additive-only invariant: none of these may appear in ANY emitted artifact.
#
# MERGE is deliberately NOT in this list. A Delta upsert (merge / whenMatchedUpdateAll
# / whenNotMatchedInsertAll) is how the JDBC job stays idempotent on re-run (audit A2):
# it updates or inserts rows for the declared key and can remove nothing -- there is no
# WHEN NOT MATCHED BY SOURCE ... DELETE branch anywhere in the emitters. The tokens that
# DO remove or rewrite data (DROP / DELETE / TRUNCATE / OVERWRITE / CREATE OR REPLACE)
# stay forbidden; `test_destructive_grep_allows_merge_but_not_removal` pins that split.
DESTRUCTIVE = re.compile(
    r"\b(drop|replace|delete|truncate|overwrite|insert\s+overwrite)\b", re.IGNORECASE
)
# A JDBC job may only append when a marker guard makes the re-run a no-op.
APPEND = re.compile(r"\.mode\(\s*[\"']append[\"']\s*\)")
# Secret VALUES must never be inlined; only reference names (scope/key) may appear.
SECRETISH = re.compile(
    r"(password\s*=\s*[\"'][^\"']|token\s*=\s*[\"'][^\"']|AKIA[0-9A-Z]{8}|"
    r"secret_key|BEGIN [A-Z ]*PRIVATE KEY)"
)


def _workspace(
    tmp: str, *, connector: str, location: str, tables: list[dict], catalog="acme",
    declaration_extra: dict | None = None,
):
    root = Path(tmp)
    ws_rel = f"workspaces/{catalog}"
    ws = root / ws_rel
    (ws / "interns/generated/intake").mkdir(parents=True, exist_ok=True)
    declaration = {
        "type": connector,
        "location": location,
        "format_hint": "parquet",
        "credential_ref": "acme_secret_scope",
        "declared_by": "tester",
        "declared_at": "2026-08-05T00:00:00+00:00",
    }
    declaration.update(declaration_extra or {})
    (ws / "workspace_settings.json").write_text(
        json.dumps({"source_declaration": declaration}),
        encoding="utf-8",
    )
    (ws / "interns/generated/intake/discovery.json").write_text(
        json.dumps({"connector": connector, "tables": tables,
                    "working_set_estimate_bytes": 1}),
        encoding="utf-8",
    )
    build_provision_plan(root, ws_rel, catalog=catalog, env="dev")
    return root, ws_rel


def _emitted(root: Path, ws_rel: str) -> dict[str, str]:
    out = root / ws_rel / "ingestion"
    return {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()}


S3_TABLES = [
    {"name": "claims_raw", "path": "s3://bkt/pfx/claims", "format": "parquet",
     "size_bytes": 10, "row_estimate": 5, "is_streaming": True},
    {"name": "Payer Dim", "path": "s3://bkt/pfx/payer", "format": "csv",
     "size_bytes": 10, "row_estimate": 5, "is_streaming": False,
     "columns": [{"name": "payer_id", "type": "STRING"}]},
]


class MethodChoiceTests(unittest.TestCase):
    def test_streaming_object_store_gets_auto_loader_batch_gets_copy_into(self):
        self.assertEqual(choose_method("s3", {"is_streaming": True}), METHOD_AUTO_LOADER)
        self.assertEqual(choose_method("adls", {"is_streaming": False}), METHOD_COPY_INTO)
        self.assertEqual(choose_method("gcs", {}), METHOD_COPY_INTO)
        self.assertEqual(choose_method("jdbc", {}), METHOD_JDBC)
        self.assertEqual(choose_method("kafka", {}), METHOD_KAFKA)
        for connector in ("sftp", "local_files", "uc_existing"):
            self.assertEqual(choose_method(connector, {}), METHOD_UNSUPPORTED)

    def test_safe_name(self):
        self.assertEqual(safe_name("Payer Dim"), "payer_dim")
        self.assertEqual(safe_name("2024-claims"), "t_2024_claims")
        self.assertEqual(safe_name(""), "table")


class ObjectStoreEmissionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root, self.ws = _workspace(
            self._tmp.name, connector="s3", location="s3://bkt/pfx", tables=S3_TABLES
        )
        self.result = generate_ingestion(self.root, self.ws)
        self.files = _emitted(self.root, self.ws)

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_file_per_table_plus_manifest(self):
        self.assertEqual(
            set(self.files), {"ingest_claims_raw.py", "ingest_payer_dim.sql", "jobs_manifest.json"}
        )
        self.assertEqual(len(self.result.jobs), 2)

    def test_auto_loader_code_shape(self):
        code = self.files["ingest_claims_raw.py"]
        self.assertIn('format("cloudFiles")', code)
        self.assertIn('"cloudFiles.format", "parquet"', code)
        self.assertIn('"cloudFiles.schemaEvolutionMode", "addNewColumns"', code)
        # addNewColumns absorbs ADDED columns; it does nothing for a value
        # whose TYPE changed underneath, which nulls silently. The rescued
        # column is independent of evolution mode and covers exactly that.
        self.assertIn('"cloudFiles.rescuedDataColumn", "_rescued_data"', code)
        self.assertIn("cloudFiles.maxFilesPerTrigger", code)
        self.assertIn('outputMode("append")', code)
        self.assertIn("trigger(availableNow=True)", code)
        self.assertIn('toTable(TARGET_TABLE)', code)
        self.assertIn('TARGET_TABLE = "acme_dev.bronze.claims_raw"', code)

    def test_checkpoint_prefix_rule(self):
        code = self.files["ingest_claims_raw.py"]
        self.assertIn(
            'CHECKPOINT_PATH = "/Volumes/acme_dev/bronze/_checkpoints/claims_raw"', code
        )
        self.assertIn("SCHEMA_PATH = CHECKPOINT_PATH", code)
        self.assertIn("lifecycle", code)  # the exemption is documented in-file
        # checkpoints never live under the source prefix
        self.assertNotIn("s3://bkt/pfx/claims/_checkpoint", code)
        manifest = json.loads(self.files["jobs_manifest.json"])
        self.assertEqual(manifest["checkpoint_root"], "/Volumes/acme_dev/bronze/_checkpoints")
        self.assertIn("exempt", manifest["checkpoint_lifecycle_policy"])

    def test_copy_into_sql_shape(self):
        sql = self.files["ingest_payer_dim.sql"]
        self.assertIn("COPY INTO acme_dev.bronze.payer_dim", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS acme_dev.bronze.payer_dim", sql)
        self.assertIn("FILEFORMAT = CSV", sql)
        self.assertIn("'mergeSchema' = 'true'", sql)
        self.assertIn("payer_id STRING", sql)

    def test_csv_declares_a_header_row(self):
        """F23: COPY INTO defaults CSV `header` to FALSE. Without this the
        header line lands as a DATA row and the columns come back `_c0, _c1,
        ...` -- a silently wrong bronze table rather than a loud failure."""
        sql = self.files["ingest_payer_dim.sql"]
        self.assertIn("'header' = 'true'", sql)

    def test_csv_rescues_values_that_do_not_match_the_column_type(self):
        """Without a rescued-data column a value that will not parse into its
        column lands as NULL, which is indistinguishable from a real null and
        reaches a KPI as a quietly wrong number. Rescued, it is still visible
        and still queryable -- the handbook's Bronze inspection step
        (`SELECT _rescued_data ... WHERE _rescued_data IS NOT NULL`) has
        something to read."""
        sql = self.files["ingest_payer_dim.sql"]
        self.assertIn("'rescuedDataColumn' = '_rescued_data'", sql)

    def test_manifest_shape(self):
        manifest = json.loads(self.files["jobs_manifest.json"])
        self.assertTrue(manifest["additive_only"])
        self.assertEqual(manifest["catalog"], "acme_dev")
        methods = {job["method"] for job in manifest["jobs"]}
        self.assertEqual(methods, {METHOD_AUTO_LOADER, METHOD_COPY_INTO})
        streaming = next(j for j in manifest["jobs"] if j["method"] == METHOD_AUTO_LOADER)
        self.assertEqual(streaming["trigger"], "availableNow")
        self.assertTrue(streaming["checkpoint_path"].startswith("/Volumes/"))

    def test_no_destructive_statement_in_any_emitted_file(self):
        for name, body in self.files.items():
            match = DESTRUCTIVE.search(body)
            self.assertIsNone(match, f"{name} emitted destructive token {match and match.group()}")

    def test_no_secret_values_inlined(self):
        for name, body in self.files.items():
            self.assertIsNone(SECRETISH.search(body), f"{name} inlined something secret-shaped")
            self.assertIn("acme_secret_scope", json.dumps(json.loads(
                self.files["jobs_manifest.json"]
            )))  # reference name only


JDBC_URL = "jdbc:postgresql://host:5432/db"
# order_id is inferable from the table stem; updated_at is an inferable watermark.
JDBC_TABLE = {
    "name": "public.orders", "format": "jdbc",
    "columns": [
        {"name": "order_id", "type": "BIGINT"},
        {"name": "customer_id", "type": "BIGINT"},
        {"name": "updated_at", "type": "TIMESTAMP"},
        {"name": "amount", "type": "DECIMAL(10,2)"},
    ],
}
# No column resolves to a key: customer_id is a foreign key, not this table's identity.
JDBC_TABLE_NO_KEY = {
    "name": "public.order_events", "format": "jdbc",
    "columns": [
        {"name": "customer_id", "type": "BIGINT"},
        {"name": "amount", "type": "DECIMAL(10,2)"},
    ],
}


def _jdbc_workspace(tmp, tables=None, **declaration_extra):
    return _workspace(
        tmp, connector="jdbc", location=JDBC_URL,
        tables=tables if tables is not None else [JDBC_TABLE],
        declaration_extra=declaration_extra or None,
    )


class JdbcIdempotencyTests(unittest.TestCase):
    """Audit A2: an unbounded `.mode("append")` re-run duplicates the whole table."""

    def _job(self, root, ws, table_name="public.orders"):
        manifest = json.loads(_emitted(root, ws)["jobs_manifest.json"])
        return next(job for job in manifest["jobs"] if job["table"] == table_name)

    def test_declared_key_and_watermark_win_over_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _jdbc_workspace(
                tmp, jdbc_key_columns=["customer_id"], jdbc_watermark_column="amount",
            )
            generate_ingestion(root, ws)
            code = _emitted(root, ws)["ingest_public_orders.py"]
            self.assertIn('KEY_COLUMNS = ["customer_id"]', code)
            self.assertIn('WATERMARK_COLUMN = "amount"', code)
            job = self._job(root, ws)
            self.assertEqual(job["idempotency"], {
                "mode": "merge", "key": ["customer_id"], "watermark": "amount",
                "source": "declared", "evidence": job["idempotency"]["evidence"],
            })
            self.assertIn("source_declaration", job["idempotency"]["evidence"])

    def test_key_and_watermark_inferred_from_discovery_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _jdbc_workspace(tmp)
            generate_ingestion(root, ws)
            code = _emitted(root, ws)["ingest_public_orders.py"]
            self.assertIn('KEY_COLUMNS = ["order_id"]', code)
            self.assertIn('WATERMARK_COLUMN = "updated_at"', code)
            idempotency = self._job(root, ws)["idempotency"]
            self.assertEqual(idempotency["mode"], "merge")
            self.assertEqual(idempotency["source"], "inferred")
            self.assertIn("order_id", idempotency["evidence"])
            self.assertIn("updated_at", idempotency["evidence"])

    def test_merge_job_is_watermark_bounded_and_never_plain_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _jdbc_workspace(tmp)
            generate_ingestion(root, ws)
            code = _emitted(root, ws)["ingest_public_orders.py"]
            self.assertIn("whenMatchedUpdateAll", code)
            self.assertIn("whenNotMatchedInsertAll", code)
            self.assertIn("max({WATERMARK_COLUMN})", code)  # bounded incremental pull
            self.assertIsNone(APPEND.search(code))
            self.assertIsNone(DESTRUCTIVE.search(code))
            self.assertIsNone(SECRETISH.search(code))
            self.assertIn('dbutils.secrets.get(scope=CREDENTIAL_SCOPE, key="password")', code)

    def test_no_resolvable_key_refuses_to_emit_a_job_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _jdbc_workspace(tmp, tables=[JDBC_TABLE_NO_KEY])
            result = generate_ingestion(root, ws)
            self.assertEqual(set(_emitted(root, ws)), {"jobs_manifest.json"})
            self.assertFalse(result.ok)
            job = self._job(root, ws, "public.order_events")
            self.assertEqual(job["status"], "blocked_no_idempotency_key")
            self.assertIsNone(job["file"])
            self.assertEqual(job["idempotency"]["mode"], "refused")
            needs = " ".join(job["needs"])
            self.assertIn("jdbc_key_columns", needs)
            self.assertIn("one_shot", needs)

    def test_one_shot_appends_once_behind_a_marker_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _jdbc_workspace(tmp, tables=[JDBC_TABLE_NO_KEY], one_shot=True)
            result = generate_ingestion(root, ws)
            self.assertTrue(result.ok)
            code = _emitted(root, ws)["ingest_public_order_events.py"]
            self.assertIsNotNone(APPEND.search(code))
            self.assertIn("acme_dev.bronze._ingest_markers", code)
            self.assertIn("already ingested", code)  # re-run is a no-op
            job = self._job(root, ws, "public.order_events")
            self.assertEqual(job["idempotency"]["mode"], "append_once")
            self.assertEqual(job["idempotency"]["key"], [])

    def test_every_emitted_jdbc_append_carries_a_marker_guard(self):
        """The A2 regression grep: no JDBC job may append without the marker guard."""
        fixtures = [
            ({}, [JDBC_TABLE]),
            ({"one_shot": True}, [JDBC_TABLE, JDBC_TABLE_NO_KEY]),
            ({"jdbc_key_columns": ["order_id"]}, [JDBC_TABLE]),
        ]
        for extra, tables in fixtures:
            with tempfile.TemporaryDirectory() as tmp:
                root, ws = _jdbc_workspace(tmp, tables=tables, **extra)
                generate_ingestion(root, ws)
                for name, body in _emitted(root, ws).items():
                    if not name.endswith(".py") or not APPEND.search(body):
                        continue
                    self.assertIn("_ingest_markers", body, f"{name} appends unguarded")

    def test_destructive_grep_allows_merge_but_not_removal(self):
        self.assertIsNone(DESTRUCTIVE.search("MERGE INTO t USING s ON t.id = s.id"))
        for statement in ("DROP TABLE t", "DELETE FROM t", "TRUNCATE TABLE t",
                          "INSERT OVERWRITE t", "CREATE OR REPLACE TABLE t"):
            self.assertIsNotNone(DESTRUCTIVE.search(statement), statement)


class KafkaEmissionTests(unittest.TestCase):
    def _kafka(self, tmp, **declaration_extra):
        root, ws = _workspace(
            tmp, connector="kafka", location="broker-1:9092",
            tables=[{"name": "events", "format": "kafka", "is_streaming": True}],
            declaration_extra=declaration_extra or None,
        )
        generate_ingestion(root, ws)
        files = _emitted(root, ws)
        manifest = json.loads(files["jobs_manifest.json"])
        return files["ingest_events.py"], manifest["jobs"][0]

    def test_kafka_stub_streams_with_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, job = self._kafka(tmp)
            self.assertIn('format("kafka")', code)
            self.assertIn('"subscribe", TOPIC', code)
            self.assertIn('CHECKPOINT_PATH = "/Volumes/acme_dev/bronze/_checkpoints/events"', code)
            self.assertIn("dbutils.secrets.get(scope=CREDENTIAL_SCOPE", code)
            self.assertIsNone(DESTRUCTIVE.search(code))
            self.assertEqual(job["idempotency"]["mode"], "streaming_checkpoint")

    def test_kafka_throttles_the_backlog(self):
        """Audit A9: startingOffsets=earliest with no cap reads the whole backlog."""
        with tempfile.TemporaryDirectory() as tmp:
            code, job = self._kafka(tmp)
            self.assertIn('"maxOffsetsPerTrigger", 1000000', code)
            self.assertIn("maxOffsetsPerTrigger", code.split("stream = ")[0])  # documented
            self.assertEqual(job["max_offsets_per_trigger"], 1000000)

    def test_kafka_without_registry_flags_the_schema_as_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, job = self._kafka(tmp)
            self.assertIn('cast("string")', code)
            self.assertIn("[~]", code)
            self.assertEqual(job["schema"], "unverified")

    def test_kafka_with_registry_decodes_avro_by_secret_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, job = self._kafka(tmp, schema_registry_url="https://registry.example:8081")
            self.assertIn("from_avro", code)
            self.assertIn('SCHEMA_REGISTRY_URL = "https://registry.example:8081"', code)
            self.assertIn('key="schema_registry_auth"', code)
            self.assertNotIn('cast("string").alias("kafka_value")', code)
            self.assertEqual(job["schema"], "avro_schema_registry")
            self.assertEqual(job["schema_registry_url"], "https://registry.example:8081")
            self.assertIsNone(SECRETISH.search(code))


class OtherConnectorTests(unittest.TestCase):
    def test_every_job_entry_carries_the_idempotency_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(
                tmp, connector="s3", location="s3://bkt/pfx", tables=S3_TABLES
            )
            generate_ingestion(root, ws)
            manifest = json.loads(_emitted(root, ws)["jobs_manifest.json"])
            modes = {}
            for job in manifest["jobs"]:
                idempotency = job["idempotency"]
                self.assertEqual({"mode", "key", "watermark"} & set(idempotency),
                                 {"mode", "key", "watermark"})
                self.assertIn(idempotency["mode"],
                              {"merge", "append_once", "streaming_checkpoint", "refused"})
                modes[job["method"]] = idempotency["mode"]
            self.assertEqual(modes[METHOD_AUTO_LOADER], "streaming_checkpoint")
            self.assertEqual(modes[METHOD_COPY_INTO], "append_once")

    def test_unsupported_connector_emits_a_note_not_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(
                tmp, connector="sftp", location="sftp://host/drop",
                tables=[{"name": "daily_extract", "format": "csv"}],
            )
            result = generate_ingestion(root, ws)
            self.assertEqual(result.jobs, [])
            self.assertTrue(any("daily_extract" in note for note in result.notes))
            self.assertEqual(set(_emitted(root, ws)), {"jobs_manifest.json"})

    def test_regeneration_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(
                tmp, connector="s3", location="s3://bkt/pfx", tables=S3_TABLES
            )
            generate_ingestion(root, ws)
            first = _emitted(root, ws)["ingest_claims_raw.py"]
            generate_ingestion(root, ws)
            self.assertEqual(_emitted(root, ws)["ingest_claims_raw.py"], first)

    def test_refuses_without_a_provision_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspaces/none/interns").mkdir(parents=True)
            with self.assertRaises(FileNotFoundError):
                generate_ingestion(root, "workspaces/none")


class EmittedPythonIsValidTests(unittest.TestCase):
    def test_every_emitted_python_file_compiles(self):
        fixtures = [
            dict(connector="s3", location="s3://bkt/pfx", tables=S3_TABLES),
            dict(connector="jdbc", location=JDBC_URL, tables=[JDBC_TABLE]),
            dict(connector="jdbc", location=JDBC_URL, tables=[JDBC_TABLE_NO_KEY],
                 declaration_extra={"one_shot": True}),
            dict(connector="kafka", location="broker-1:9092",
                 tables=[{"name": "events", "format": "kafka", "is_streaming": True}],
                 declaration_extra={"schema_registry_url": "https://registry.example:8081"}),
        ]
        for fixture in fixtures:
            with tempfile.TemporaryDirectory() as tmp:
                root, ws = _workspace(tmp, **fixture)
                generate_ingestion(root, ws)
                for name, body in _emitted(root, ws).items():
                    if name.endswith(".py"):
                        compile(body, name, "exec")


class StageRoutingTests(unittest.TestCase):
    def test_generate_ingestion_summary_carries_the_ingestion_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, ws = _workspace(
                tmp, connector="s3", location="s3://bkt/pfx", tables=S3_TABLES
            )
            summary = generate_ingestion(root, ws).summary()
            roster = routing_for("ingestion_generation")
            self.assertTrue(roster["agents"], "ingestion_generation routes no agent")
            self.assertEqual(summary["stage"], "ingestion_generation")
            self.assertEqual(summary["required_specialists"], roster["agents"])
            self.assertEqual(summary["suggested_skills"], roster["skills"])


if __name__ == "__main__":
    unittest.main()
