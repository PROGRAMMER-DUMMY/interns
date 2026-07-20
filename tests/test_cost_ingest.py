"""Phase 1a.2b -- transcript ingest.

Locks the two boundaries: (1) PRIVACY -- only token numbers/timestamps/session id
are read, never conversation content; (2) LIVENESS -- a session with anchors but no
usable transcript is loud (ok=False), never a quiet zero-fill. Plus run-level
attribution, exclusive-bucket summing, and the logged dedupe of the in-process
double-fire.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.observability.cost_ingest import (
    CostIngest,
    detect_anchor_dupes,
    extract_session_usage,
    find_transcript,
    run_window,
)
from core.observability.cost_ledger import AnchorEntry, CostLedger


def _write_transcript(home: Path, cwd_dir: str, session_id: str, records: list[dict]) -> Path:
    d = home / ".claude" / "projects" / cwd_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _usage_msg(inp, out, cc, cr, ts, secret_text="TOP_SECRET_PROMPT"):
    # A realistic record: usage numbers alongside conversation CONTENT that must
    # never be extracted.
    return {
        "sessionId": "sid",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": secret_text}],
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": cr,
            },
        },
    }


class ExtractionTests(unittest.TestCase):
    def test_sums_exclusive_buckets_and_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_transcript(
                Path(tmp), "proj", "sid",
                [
                    _usage_msg(10, 5, 100, 50, "2026-07-19T10:00:00Z"),
                    _usage_msg(2, 3, 0, 200, "2026-07-19T10:05:00Z"),
                    {"type": "user", "message": {"content": "no usage here"}},  # skipped
                ],
            )
            su = extract_session_usage(p, "sid")
            self.assertEqual(su.input_tokens, 12)
            self.assertEqual(su.output_tokens, 8)
            self.assertEqual(su.cache_creation_input_tokens, 100)
            self.assertEqual(su.cache_read_input_tokens, 250)
            self.assertEqual(su.turns, 2)
            self.assertEqual(su.first_ts, "2026-07-19T10:00:00Z")
            self.assertEqual(su.last_ts, "2026-07-19T10:05:00Z")

    def test_privacy_no_conversation_content_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_transcript(
                Path(tmp), "proj", "sid",
                [_usage_msg(10, 5, 0, 0, "2026-07-19T10:00:00Z", secret_text="LEAKED_SECRET_XYZ")],
            )
            su = extract_session_usage(p, "sid")
            blob = json.dumps(su.__dict__)
            self.assertNotIn("LEAKED_SECRET_XYZ", blob)
            self.assertNotIn("content", blob)

    def test_window_filters_turns_by_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_transcript(
                Path(tmp), "proj", "sid",
                [
                    _usage_msg(10, 5, 0, 0, "2026-07-19T10:00:00Z"),   # inside
                    _usage_msg(99, 99, 0, 0, "2026-07-19T23:00:00Z"),  # outside
                ],
            )
            lo = datetime(2026, 7, 19, 9, 59, tzinfo=timezone.utc)
            hi = datetime(2026, 7, 19, 10, 1, tzinfo=timezone.utc)
            su = extract_session_usage(p, "sid", window=(lo, hi))
            self.assertEqual(su.input_tokens, 10)
            self.assertEqual(su.turns, 1)
            # window=None is unchanged: both turns summed
            self.assertEqual(extract_session_usage(p, "sid").turns, 2)


class RunWindowTests(unittest.TestCase):
    def test_spans_min_start_to_max_finish(self):
        win = run_window([
            {"started_at": "2026-07-19T10:00:00Z", "finished_at": "2026-07-19T10:00:30Z"},
            {"started_at": "2026-07-19T10:05:00Z", "finished_at": "2026-07-19T10:05:10+00:00"},
        ])
        self.assertIsNotNone(win)
        lo, hi = win
        self.assertEqual(lo.isoformat(), "2026-07-19T10:00:00+00:00")
        self.assertEqual(hi.isoformat(), "2026-07-19T10:05:10+00:00")

    def test_none_when_no_usable_timestamps(self):
        self.assertIsNone(run_window([{"started_at": "", "finished_at": ""}]))


class FindTranscriptTests(unittest.TestCase):
    def test_locates_by_session_id_across_project_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "some-cwd-hash", "abc-123", [_usage_msg(1, 1, 0, 0, "t")])
            found = find_transcript("abc-123", home=home)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "abc-123.jsonl")
            self.assertIsNone(find_transcript("nope", home=home))


class IngestTests(unittest.TestCase):
    def _ledger(self, tmp: str) -> CostLedger:
        return CostLedger(Path(tmp) / "cost_ledger")

    def _usage(self, ledger: CostLedger) -> list[dict]:
        return [json.loads(l) for l in ledger.ledger_dir.joinpath("usage.jsonl").read_text().splitlines()]

    def _by_attr(self, recs: list[dict], attr: str) -> list[dict]:
        return [r for r in recs if r["attribution"] == attr]

    def _anchor(self, **kw) -> AnchorEntry:
        base = dict(workspace_id="ws", pipeline_stage="onboard", agent="claude-code")
        base.update(kw)
        return AnchorEntry(**base)

    def test_emits_session_total_and_temporal_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "proj", "sess-abc",
                              [_usage_msg(100, 40, 500, 300, "2026-07-19T10:00:00Z")])
            ledger = self._ledger(tmp)
            ledger.append(self._anchor(run_id="r1", agent_session_id="sess-abc",
                                       started_at="2026-07-19T09:59:00Z",
                                       finished_at="2026-07-19T10:01:00Z"))
            result = CostIngest(ledger, home=home).run()
            self.assertTrue(result.ok)
            self.assertEqual(result.sessions_filled, 1)
            self.assertEqual(result.temporal_runs, 1)
            recs = self._usage(ledger)
            st = self._by_attr(recs, "session_total")
            tp = self._by_attr(recs, "temporal_approximate")
            self.assertEqual(len(st), 1)
            self.assertEqual(len(tp), 1)
            # session_total = full transcript sum, honestly labeled (not "run")
            self.assertEqual(st[0]["input_tokens"], 100)
            self.assertEqual(st[0]["cached_tokens"], 800)  # cache_creation + cache_read
            self.assertEqual(st[0]["run_ids"], ["r1"])
            self.assertEqual(st[0]["capture_method"], "transcript_ingest")
            # temporal = windowed (the 10:00 turn is inside 09:59-10:01)
            self.assertEqual(tp[0]["input_tokens"], 100)
            self.assertEqual(tp[0]["turns_in_window"], 1)
            self.assertEqual(tp[0]["run_id"], "r1")
            self.assertEqual(tp[0]["cost_source"], "unreconciled")
            self.assertEqual(tp[0]["capture_method"], "transcript_ingest_temporal")
            self.assertIn("window_seconds", tp[0])

    def test_no_record_is_labeled_plain_run(self):
        # Regression guard: the mislabel that made 474x read as plausible must not
        # recur -- nothing is a bare "run"; the scoped figure is temporal_approximate.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "proj", "s", [_usage_msg(10, 5, 0, 0, "2026-07-19T10:00:00Z")])
            ledger = self._ledger(tmp)
            ledger.append(self._anchor(run_id="r1", agent_session_id="s",
                                       started_at="2026-07-19T09:59:00Z",
                                       finished_at="2026-07-19T10:01:00Z"))
            CostIngest(ledger, home=home).run()
            recs = self._usage(ledger)
            self.assertTrue(all(r["attribution"] != "run" for r in recs))
            self.assertIn("temporal_approximate", {r["attribution"] for r in recs})

    def test_temporal_record_self_describes_undercount(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "proj", "s", [_usage_msg(10, 5, 0, 0, "2026-07-19T10:00:00Z")])
            ledger = self._ledger(tmp)
            ledger.append(self._anchor(run_id="r1", agent_session_id="s",
                                       started_at="2026-07-19T09:59:00Z",
                                       finished_at="2026-07-19T10:01:00Z"))
            CostIngest(ledger, home=home).run()
            tp = self._by_attr(self._usage(ledger), "temporal_approximate")[0]
            self.assertIn("UNDERCOUNT", tp["attribution_note"].upper())

    def test_missing_transcript_is_loud_not_quiet_zero_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            ledger.append(self._anchor(run_id="r1", agent_session_id="no-such-session"))
            result = CostIngest(ledger, home=Path(tmp)).run()
            self.assertFalse(result.ok)  # LOUD
            self.assertEqual(len(result.missing_transcript), 1)
            self.assertIn("FAIL", ledger.ledger_dir.joinpath("usage.md").read_text())

    def test_empty_transcript_is_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "proj", "sess-empty",
                              [{"type": "user", "message": {"content": "hi"}}])  # no usage
            ledger = self._ledger(tmp)
            ledger.append(self._anchor(run_id="r1", agent_session_id="sess-empty"))
            result = CostIngest(ledger, home=home).run()
            self.assertFalse(result.ok)
            self.assertEqual(len(result.empty_transcript), 1)

    def test_multi_run_session_splits_into_per_run_temporal(self):
        # A session spanning two runs is now SPLIT (approximately) by anchor window,
        # not left as one un-split blob. Each run gets only its window's turns.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "proj", "sess-multi", [
                _usage_msg(10, 10, 0, 0, "2026-07-19T10:00:00Z"),
                _usage_msg(20, 20, 0, 0, "2026-07-19T11:00:00Z"),
            ])
            ledger = self._ledger(tmp)
            ledger.append(self._anchor(run_id="r1", agent_session_id="sess-multi",
                                       started_at="2026-07-19T09:59:00Z",
                                       finished_at="2026-07-19T10:01:00Z"))
            ledger.append(self._anchor(run_id="r2", agent_session_id="sess-multi",
                                       started_at="2026-07-19T10:59:00Z",
                                       finished_at="2026-07-19T11:01:00Z"))
            result = CostIngest(ledger, home=home).run()
            self.assertTrue(result.ok)
            recs = self._usage(ledger)
            st = self._by_attr(recs, "session_total")
            self.assertEqual(len(st), 1)
            self.assertEqual(st[0]["run_ids"], ["r1", "r2"])
            self.assertEqual(st[0]["input_tokens"], 30)  # full session
            tp = {r["run_id"]: r for r in self._by_attr(recs, "temporal_approximate")}
            self.assertEqual(set(tp), {"r1", "r2"})
            self.assertEqual(tp["r1"]["input_tokens"], 10)  # only the 10:00 turn
            self.assertEqual(tp["r2"]["input_tokens"], 20)  # only the 11:00 turn

    def test_turn_outside_window_is_flagged_zero_not_liveness_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "proj", "s",
                              [_usage_msg(10, 5, 0, 0, "2026-07-19T20:00:00Z")])  # far outside
            ledger = self._ledger(tmp)
            ledger.append(self._anchor(run_id="r1", agent_session_id="s",
                                       started_at="2026-07-19T09:59:00Z",
                                       finished_at="2026-07-19T10:01:00Z"))
            result = CostIngest(ledger, home=home).run()
            self.assertTrue(result.ok)  # transcript present -> not a capture failure
            self.assertEqual(result.zero_window_runs, ["r1"])
            recs = self._usage(ledger)
            tp = self._by_attr(recs, "temporal_approximate")[0]
            self.assertEqual(tp["turns_in_window"], 0)
            self.assertEqual(tp["input_tokens"], 0)
            # ... but the session total still counted the turn
            self.assertEqual(self._by_attr(recs, "session_total")[0]["input_tokens"], 10)

    def test_usage_md_carries_driving_pattern_and_human_read_notes(self):
        # The output artifact -- not just the design doc -- must warn a reader that
        # the number is driving-pattern sensitive and the check is a human token read.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "proj", "s", [_usage_msg(10, 5, 0, 0, "2026-07-19T10:00:00Z")])
            ledger = self._ledger(tmp)
            ledger.append(self._anchor(run_id="r1", agent_session_id="s",
                                       started_at="2026-07-19T09:59:00Z",
                                       finished_at="2026-07-19T10:01:00Z"))
            CostIngest(ledger, home=home).run()
            md = ledger.ledger_dir.joinpath("usage.md").read_text()
            self.assertIn("HUMAN magnitude read", md)
            self.assertIn("Driving-pattern sensitive", md)
            self.assertIn("turns_in_window", md)

    def test_temporal_unavailable_when_anchor_lacks_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "proj", "s", [_usage_msg(10, 5, 0, 0, "2026-07-19T10:00:00Z")])
            ledger = self._ledger(tmp)
            ledger.append(self._anchor(run_id="r1", agent_session_id="s"))  # no started/finished
            CostIngest(ledger, home=home).run()
            tu = self._by_attr(self._usage(ledger), "temporal_unavailable")
            self.assertEqual(len(tu), 1)
            self.assertEqual(tu[0]["run_id"], "r1")


class DedupeTests(unittest.TestCase):
    def test_same_stage_double_fire_is_info_cross_session_is_warning(self):
        anchors = [
            {"run_id": "r1", "pipeline_stage": "onboard", "agent_session_id": "s1", "agent": "claude-code"},
            {"run_id": "r1", "pipeline_stage": "onboard", "agent_session_id": "s1", "agent": "claude-code"},
            {"run_id": "r1", "pipeline_stage": "kpi", "agent_session_id": "s1", "agent": "claude-code"},
            {"run_id": "r1", "pipeline_stage": "kpi", "agent_session_id": "s2", "agent": "claude-code"},
        ]
        events = detect_anchor_dupes(anchors)
        self.assertEqual(len(events), 2)
        by_stage = {e["pipeline_stage"]: e for e in events}
        self.assertEqual(by_stage["onboard"]["level"], "info")
        self.assertEqual(by_stage["onboard"]["kind"], "same_stage_double_fire")
        self.assertEqual(by_stage["kpi"]["level"], "warning")
        self.assertEqual(by_stage["kpi"]["kind"], "cross_session_same_run_stage")


if __name__ == "__main__":
    unittest.main()
