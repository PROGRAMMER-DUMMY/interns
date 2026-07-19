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
from pathlib import Path

from core.observability.cost_ingest import (
    CostIngest,
    detect_anchor_dupes,
    extract_session_usage,
    find_transcript,
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

    def test_fills_run_level_usage_from_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "proj", "sess-abc",
                              [_usage_msg(100, 40, 500, 300, "2026-07-19T10:00:00Z")])
            ledger = self._ledger(tmp)
            ledger.append(AnchorEntry(run_id="r1", workspace_id="ws", pipeline_stage="onboard",
                                      agent_session_id="sess-abc", agent="claude-code"))
            result = CostIngest(ledger, home=home).run()
            self.assertTrue(result.ok)
            self.assertEqual(result.sessions_filled, 1)
            usage = [json.loads(l) for l in ledger.ledger_dir.joinpath("usage.jsonl").read_text().splitlines()]
            self.assertEqual(usage[0]["input_tokens"], 100)
            self.assertEqual(usage[0]["output_tokens"], 40)
            self.assertEqual(usage[0]["cached_tokens"], 800)  # cache_creation + cache_read
            self.assertEqual(usage[0]["attribution"], "run")
            self.assertEqual(usage[0]["cost_source"], "unreconciled")
            self.assertEqual(usage[0]["capture_method"], "transcript_ingest")

    def test_missing_transcript_is_loud_not_quiet_zero_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger(tmp)
            ledger.append(AnchorEntry(run_id="r1", workspace_id="ws", pipeline_stage="onboard",
                                      agent_session_id="no-such-session", agent="claude-code"))
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
            ledger.append(AnchorEntry(run_id="r1", workspace_id="ws", pipeline_stage="onboard",
                                      agent_session_id="sess-empty", agent="claude-code"))
            result = CostIngest(ledger, home=home).run()
            self.assertFalse(result.ok)
            self.assertEqual(len(result.empty_transcript), 1)

    def test_multi_run_session_marked_not_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_transcript(home, "proj", "sess-multi", [_usage_msg(10, 10, 0, 0, "t")])
            ledger = self._ledger(tmp)
            for rid in ("r1", "r2"):
                ledger.append(AnchorEntry(run_id=rid, workspace_id="ws", pipeline_stage="onboard",
                                          agent_session_id="sess-multi", agent="claude-code"))
            result = CostIngest(ledger, home=home).run()
            self.assertTrue(result.ok)
            usage = [json.loads(l) for l in ledger.ledger_dir.joinpath("usage.jsonl").read_text().splitlines()]
            self.assertEqual(usage[0]["attribution"], "session_spans_multiple_runs")


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
