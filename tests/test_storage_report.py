"""Storage-efficiency measurement: per-table real bytes (block accounting, not
the misleading duckdb_tables.estimated_size which is cardinality), total
warehouse size, and the raw->stored compression ratio."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import duckdb

from core.medallion.storage_report import build_storage_report, write_storage_report


class StorageReportTests(unittest.TestCase):
    def _warehouse(self, root: Path):
        med = root / "interns" / "state" / "medallion"
        med.mkdir(parents=True)
        con = duckdb.connect(str(med / "workspace.duckdb"))
        con.execute("CREATE SCHEMA bronze; CREATE SCHEMA silver; CREATE SCHEMA gold")
        con.execute("CREATE TABLE bronze.t AS SELECT range AS id, range::VARCHAR AS v FROM range(5000)")
        con.execute("CREATE TABLE silver.t AS SELECT * FROM bronze.t")
        con.execute("CREATE TABLE gold.fact_t AS SELECT * FROM silver.t")
        return con

    def test_measures_rows_and_real_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = self._warehouse(root)
            report = build_storage_report(con, workspace=root, raw_source_bytes=100000)
            con.close()
            self.assertEqual(report["summary"]["table_count"], 3)
            by = {t["table"]: t for t in report["tables"]}
            self.assertEqual(by["silver.t"]["rows"], 5000)
            # real block-accounted bytes, NOT cardinality (would be 5000)
            self.assertGreater(by["silver.t"]["stored_bytes"], 5000)
            # total comes from the real warehouse file
            self.assertGreater(report["summary"]["warehouse_file_bytes"], 0)
            self.assertIsNotNone(report["summary"]["compression_ratio_raw_to_stored"])

    def test_writes_markdown_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            con = self._warehouse(root)
            report = build_storage_report(con, workspace=root, raw_source_bytes=50000)
            con.close()
            out = root / "reports"
            path = write_storage_report(report, out)
            self.assertTrue(path.exists())
            self.assertTrue((out / "storage_report.md").exists())
            md = (out / "storage_report.md").read_text(encoding="utf-8")
            self.assertIn("Compression ratio", md)
            self.assertIn("silver.t", md)


if __name__ == "__main__":
    unittest.main()
