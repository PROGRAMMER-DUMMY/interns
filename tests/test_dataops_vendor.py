"""Tests for the MinusDataOps vendor: the 6-step knowledge engine, decision rules,
volume math, workspace advisor, and interview coach.

Pure-knowledge tests run everywhere. The workspace-advisor test that loads a real
generated project is skipped when that project isn't present.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_VENDOR = str((Path(__file__).resolve().parents[1] / "vendor"))
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

_ROOT = Path("workspaces/Healthcare-RCM-Data-Platform/interns/state/minus")


class TestFramework(unittest.TestCase):
    def test_six_steps_in_order(self):
        from minus_dataops.framework import FRAMEWORK
        self.assertEqual([s.num for s in FRAMEWORK], [1, 2, 3, 4, 5, 6])
        keys = {s.key for s in FRAMEWORK}
        self.assertEqual(keys, {"requirements", "pipeline", "modeling",
                                "storage", "quality", "scalability"})

    def test_lookup_by_num_and_key_and_fuzzy(self):
        from minus_dataops.framework import get_step
        self.assertEqual(get_step(3).key, "modeling")
        self.assertEqual(get_step("storage").num, 4)
        self.assertEqual(get_step("data modeling").key, "modeling")
        self.assertEqual(get_step("step5").key, "quality")
        with self.assertRaises(KeyError):
            get_step("nonsense")


class TestDecisions(unittest.TestCase):
    def test_batch_vs_stream_by_latency(self):
        from minus_dataops.decisions import batch_vs_stream
        self.assertIn("Stream", batch_vs_stream(latency="5s").recommendation)
        self.assertIn("Batch", batch_vs_stream(latency="daily").recommendation)
        self.assertIn("Lambda", batch_vs_stream(latency="5s",
                                                needs_history=True).recommendation)
        self.assertIn("Kappa", batch_vs_stream(latency="2s",
                                               replayable=True).recommendation)
        self.assertEqual(batch_vs_stream(latency=None).confidence, "low")

    def test_compute_engine_by_volume(self):
        from minus_dataops.decisions import compute_engine
        self.assertIn("Single-node", compute_engine("10GB").recommendation)
        self.assertIn("Distributed", compute_engine("4TB").recommendation)
        self.assertEqual(compute_engine(None).confidence, "low")

    def test_storage_and_table_format(self):
        from minus_dataops.decisions import storage_format, table_format
        self.assertIn("Parquet", storage_format("analytics").recommendation)
        self.assertIn("Avro", storage_format("streaming ingest").recommendation)
        self.assertIn("Open table", table_format(streaming_small_files=True).recommendation)
        self.assertIn("Raw Parquet", table_format(streaming_small_files=False,
                                                  needs_acid=False,
                                                  concurrent_writes=False,
                                                  time_travel=False).recommendation)

    def test_scd_and_modeling(self):
        from minus_dataops.decisions import scd_type, modeling_shape
        self.assertIn("Type 2", scd_type(track_history=True).recommendation)
        self.assertIn("Type 1", scd_type(track_history=False).recommendation)
        self.assertIn("OBT", modeling_shape(predictable_dashboard=True,
                                            frequent_updates=False).recommendation)

    def test_registry_decide(self):
        from minus_dataops.decisions import decide, rule_keys
        self.assertIn("batch_vs_stream", rule_keys())
        d = decide("compute_engine", volume_per_day="2TB")
        self.assertIn("Distributed", d.recommendation)
        with self.assertRaises(KeyError):
            decide("does_not_exist")


class TestVolume(unittest.TestCase):
    def test_back_of_envelope(self):
        from minus_dataops.volume import back_of_envelope, implications
        est = back_of_envelope(daily_active_users=1_000_000, sessions_per_user=5,
                               events_per_session=20, payload_bytes=1_000)
        # 1e6 * 5 * 20 * 1e3 = 1e11 bytes/day = 100 GB/day
        self.assertAlmostEqual(est.bytes_per_day, 1e11, delta=1e6)
        self.assertEqual(implications(est)["scale"], "GB-scale")

    def test_scan_path_formats(self):
        from minus_dataops.volume import estimate_from_path
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "a.parquet").write_bytes(b"x" * 1000)
            (base / "b.csv").write_bytes(b"y" * 500)
            (base / "ignore.log").write_bytes(b"z" * 9999)
            est = estimate_from_path(base)
            self.assertEqual(est.method, "file-scan")
            self.assertEqual(est.bytes_retained, 1500)  # log ignored
            self.assertIn("Parquet (columnar)", est.detail["formats"])


class TestReportAndCoach(unittest.TestCase):
    def test_review_without_project_is_checklist(self):
        from minus_dataops.assess import review
        from minus_dataops.report import to_markdown, to_json
        rv = review()  # no root, no scan
        self.assertEqual(len(rv.findings), 6)
        md = to_markdown(rv)
        self.assertIn("DataOps system-design review", md)
        self.assertIn("Open questions", md)
        import json
        json.loads(to_json(rv))  # must be valid JSON

    def test_coach_drill_and_score(self):
        from minus_dataops.coach import drill, score
        self.assertEqual(len(drill()), 6)
        weak = score("we will use a database")
        strong = score("Bronze Silver Gold medallion, star schema, SCD2 partition, "
                       "Parquet Delta ACID, Kafka batch stream airflow, idempotent merge "
                       "backfill, completeness accuracy consistency freshness uniqueness "
                       "data contract observability schema evolution, latency volume "
                       "retention back-of-envelope")
        self.assertGreater(strong.overall, weak.overall)
        self.assertGreater(strong.overall, 0.5)


class TestDepth(unittest.TestCase):
    def test_all_six_dossiers_present_and_keyed_correctly(self):
        from minus_dataops.depth import deep_steps
        d = deep_steps()
        self.assertEqual(set(d), {"requirements", "pipeline", "modeling",
                                  "storage", "quality", "scalability"})
        for key, deep in d.items():
            self.assertEqual(deep.step_key, key)

    def test_dossiers_have_real_depth(self):
        from minus_dataops.depth import deep_steps
        for key, deep in deep_steps().items():
            with self.subTest(step=key):
                self.assertGreaterEqual(len(deep.topics), 6, f"{key}: too few topics")
                self.assertGreaterEqual(len(deep.comparisons), 4)
                self.assertGreaterEqual(len(deep.heuristics), 5)
                self.assertGreaterEqual(len(deep.pitfalls), 5)
                self.assertTrue(deep.at_scale, f"{key}: missing at-scale notes")
                self.assertTrue(deep.interview_signals)
                # every comparison has options with pros/cons
                for c in deep.comparisons:
                    self.assertGreaterEqual(len(c.options), 2)
                # topics carry explanatory points
                self.assertTrue(all(t.points for t in deep.topics),
                                f"{key}: a topic has no points")

    def test_deep_keys_match_framework_keys(self):
        from minus_dataops.depth import deep_steps
        from minus_dataops.framework import step_keys
        self.assertEqual(set(deep_steps()), set(step_keys()))

    def test_no_emojis_in_deep_text(self):
        # house rule: ASCII markers, never emojis
        from minus_dataops.depth import deep_steps
        for key, deep in deep_steps().items():
            blob = " ".join([deep.overview, *deep.pitfalls, *deep.at_scale,
                             *deep.interview_signals,
                             *(p for t in deep.topics for p in t.points)])
            self.assertTrue(blob.isascii(), f"{key}: non-ASCII char in deep text")

    def test_concept_tokens_nonempty(self):
        from minus_dataops.depth import get_deep
        self.assertTrue(get_deep("storage").concept_tokens())


class TestDepthIntegration(unittest.TestCase):
    def test_review_deep_enriches_findings(self):
        from minus_dataops.assess import review
        from minus_dataops.report import to_markdown
        base = review()
        deep = review(deep=True)
        # base has no at-scale/pitfalls; deep does
        self.assertFalse(any(f.at_scale or f.pitfalls for f in base.findings))
        self.assertTrue(all(f.at_scale and f.pitfalls for f in deep.findings))
        md = to_markdown(deep)
        self.assertIn("What changes at TB/PB scale", md)
        self.assertIn("Pitfalls to avoid", md)

    def test_score_deep_widens_concept_set(self):
        from minus_dataops.coach import score
        ans = "parquet delta avro star schema kafka batch stream"
        base = score(ans, step="storage")
        deep = score(ans, step="storage", deep=True)
        base_total = len(base.steps[0].hit) + len(base.steps[0].missed)
        deep_total = len(deep.steps[0].hit) + len(deep.steps[0].missed)
        self.assertGreater(deep_total, base_total)

    def test_decide_explain_maps_to_comparison(self):
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor"))
        from minus_dataops.cli import _deep_comparison_for
        matches = _deep_comparison_for("batch_vs_stream")
        self.assertTrue(matches)
        step_key, comp = matches[0]
        self.assertGreaterEqual(len(comp.options), 2)


@unittest.skipUnless((_ROOT / "project.yaml").exists(),
                     "generated MinusAnalyst root not present")
class TestWorkspaceAdvisor(unittest.TestCase):
    def test_review_reads_generated_project(self):
        from minus_dataops.assess import review
        rv = review(root=_ROOT, latency="daily")
        self.assertEqual(len(rv.findings), 6)
        # the requirements finding should observe rows or measures from the project
        req = next(f for f in rv.findings if f.step == "requirements")
        self.assertTrue(req.observed, "expected observed evidence from the project")


if __name__ == "__main__":
    unittest.main()
