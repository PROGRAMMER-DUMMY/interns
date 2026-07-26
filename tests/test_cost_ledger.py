"""Phase 1a.1 -- cost-ledger anchor half.

These tests lock the two things that make the anchor honest and joinable:
  * the join key is the AGENT-NATIVE session id from the environment
    (CLAUDE_CODE_SESSION_ID), never the platform's invented session_snapshot id;
  * a completed run that produced zero anchors, or too many unattributable ones,
    FAILS the liveness gate loudly (the assert_installed() analogue).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.observability import cost_ledger as cl
from core.observability.cost_ledger import (
    UNATTRIBUTED_FAIL_FRACTION,
    AnchorEntry,
    CostLedger,
    anchored,
    assert_ledger_active,
    build_anchor,
    detect_agent,
    ledger_dir_for,
    liveness_check,
    new_run_id,
    resolve_agent,
    resolve_agent_session,
    run_id_for,
)


class AgentDetectionTests(unittest.TestCase):
    def test_claude_code_detected_from_env_marker(self):
        agent, source = detect_agent({"CLAUDECODE": "1"})
        self.assertEqual(agent, "claude-code")
        self.assertEqual(source, "env:CLAUDECODE")

    def test_codex_detected_from_sandbox_marker(self):
        agent, source = detect_agent({"CODEX_SANDBOX": "seatbelt"})
        self.assertEqual(agent, "codex")
        self.assertEqual(source, "env:CODEX_SANDBOX")

    def test_no_marker_returns_none_never_guesses(self):
        self.assertEqual(detect_agent({}), (None, "none"))
        # Blank marker is not a detection.
        self.assertEqual(detect_agent({"CLAUDECODE": "  "}), (None, "none"))

    def test_resolve_agent_detected_beats_configured(self):
        agent, source = resolve_agent({"CLAUDECODE": "1"}, "gemini-cli")
        self.assertEqual(agent, "claude-code")
        self.assertEqual(source, "env:CLAUDECODE")

    def test_resolve_agent_config_fallback_recorded_honestly(self):
        # Gemini has no reliable marker: it must resolve via config, recorded as
        # agent_detection_source == "config" (not a fabricated env marker).
        agent, source = resolve_agent({}, "gemini-cli")
        self.assertEqual(agent, "gemini-cli")
        self.assertEqual(source, "config")

    def test_resolve_agent_unknown_when_nothing_resolves(self):
        self.assertEqual(resolve_agent({}, None), ("unknown", "none"))


class SessionJoinKeyTests(unittest.TestCase):
    def test_agent_native_session_id_is_the_join_key(self):
        sid, source = resolve_agent_session(
            {"CLAUDE_CODE_SESSION_ID": "b8fb0956-c1b5-4ffe-9efc-c370f4bcc89a"}
        )
        self.assertEqual(sid, "b8fb0956-c1b5-4ffe-9efc-c370f4bcc89a")
        self.assertEqual(source, "env:CLAUDE_CODE_SESSION_ID")

    def test_absent_session_id_is_empty_none_not_fabricated(self):
        self.assertEqual(resolve_agent_session({}), ("", "none"))

    def test_explicit_override_wins_over_claude_native_marker(self):
        # Generic across any CLI: an explicit AUTORESEARCH_SESSION_ID (set by
        # a wrapper script, CI, or a human) is the base case, not a fallback
        # -- it must win even when a native marker is ALSO present.
        sid, source = resolve_agent_session(
            {
                "AUTORESEARCH_SESSION_ID": "codex-run-42",
                "CLAUDE_CODE_SESSION_ID": "b8fb0956-c1b5-4ffe-9efc-c370f4bcc89a",
            }
        )
        self.assertEqual(sid, "codex-run-42")
        self.assertEqual(source, "env:AUTORESEARCH_SESSION_ID")

    def test_explicit_override_works_with_no_native_marker_at_all(self):
        # The actual gap this closes: a CLI with no verified native marker
        # (Codex, Gemini, or anything not yet named) still gets correct
        # session-scoped run grouping via the override alone.
        sid, source = resolve_agent_session({"AUTORESEARCH_SESSION_ID": "gemini-run-7"})
        self.assertEqual(sid, "gemini-run-7")
        self.assertEqual(source, "env:AUTORESEARCH_SESSION_ID")

    def test_platform_session_id_is_never_the_join_key(self):
        # A platform session_snapshot id (session-<digest>) must NOT be mistaken
        # for the agent-native session id. build_anchor keeps them separate.
        entry = build_anchor(
            run_id="r1",
            workspace_id="workspaces/x",
            pipeline_stage="onboard",
            env={},  # no agent-native session id present
            platform_session_id="session-deadbeef01",
        )
        self.assertEqual(entry.agent_session_id, "")  # join key stays empty
        self.assertEqual(entry.platform_session_id, "session-deadbeef01")


class RunIdTests(unittest.TestCase):
    def test_format_and_determinism(self):
        rid = new_run_id(workspace_id="workspaces/x", agent_session_id="s", now=0.0)
        self.assertRegex(rid, r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
        self.assertEqual(
            rid, new_run_id(workspace_id="workspaces/x", agent_session_id="s", now=0.0)
        )

    def test_different_workspace_yields_different_suffix(self):
        a = new_run_id(workspace_id="workspaces/a", agent_session_id="s", now=0.0)
        b = new_run_id(workspace_id="workspaces/b", agent_session_id="s", now=0.0)
        self.assertNotEqual(a, b)


class SchemaTests(unittest.TestCase):
    def test_anchor_tokens_and_cost_are_null_at_anchor_time(self):
        entry = AnchorEntry(run_id="r", workspace_id="w", pipeline_stage="onboard")
        d = entry.to_dict()
        for col in (
            "input_tokens", "output_tokens", "cached_tokens",
            "thinking_tokens", "cost_usd", "provider", "model",
        ):
            self.assertIsNone(d[col], f"{col} must be null at anchor time")
        self.assertEqual(d["capture_method"], "anchor_only")
        self.assertEqual(d["cost_source"], "unreconciled")
        self.assertEqual(d["artifact_type"], "cost_ledger.entry")

    def test_build_anchor_from_claude_env(self):
        entry = build_anchor(
            run_id="r1",
            workspace_id="workspaces/x",
            pipeline_stage="medallion_build",
            env={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "uuid-1"},
            configured_agent="gemini-cli",
        )
        self.assertEqual(entry.agent, "claude-code")  # detection beats config
        self.assertEqual(entry.agent_detected, "claude-code")
        self.assertEqual(entry.agent_configured, "gemini-cli")
        self.assertEqual(entry.agent_session_id, "uuid-1")
        self.assertEqual(entry.agent_session_source, "env:CLAUDE_CODE_SESSION_ID")
        self.assertEqual(entry.agent_detection_source, "env:CLAUDECODE")


class LedgerStoreTests(unittest.TestCase):
    def test_append_read_roundtrip_and_run_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CostLedger(Path(tmp) / "cost_ledger")
            ledger.append(AnchorEntry(run_id="r1", workspace_id="w", pipeline_stage="onboard"))
            ledger.append(AnchorEntry(run_id="r1", workspace_id="w", pipeline_stage="kpi_results"))
            ledger.append(AnchorEntry(run_id="r2", workspace_id="w", pipeline_stage="onboard"))
            self.assertEqual(len(ledger.read_entries()), 3)
            self.assertEqual(len(ledger.entries_for_run("r1")), 2)
            self.assertEqual(len(ledger.entries_for_run("r2")), 1)

    def test_summary_is_written_and_never_renders_a_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CostLedger(Path(tmp) / "cost_ledger")
            ledger.append(
                build_anchor(
                    run_id="r1", workspace_id="w", pipeline_stage="onboard",
                    env={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "uuid-1"},
                )
            )
            path = ledger.write_summary("r1")
            text = path.read_text(encoding="utf-8")
            self.assertIn("NULL by design", text)
            self.assertIn("byte-match", text)  # the explicit 1a.2 gate is recorded
            self.assertNotIn("$", text)  # no cost number rendered


class LivenessTests(unittest.TestCase):
    def _attributable(self) -> dict:
        return {"agent_session_id": "uuid-1", "agent_detection_source": "env:CLAUDECODE"}

    def _unattributable(self) -> dict:
        return {"agent_session_id": "", "agent_detection_source": "none"}

    def test_zero_anchors_fails_capture_not_live(self):
        result = liveness_check([])
        self.assertFalse(result.ok)
        self.assertIn("not live", result.reason)

    def test_all_attributable_passes(self):
        result = liveness_check([self._attributable() for _ in range(6)])
        self.assertTrue(result.ok)
        self.assertEqual(result.unattributable, 0)

    def test_over_threshold_fails(self):
        # 2 unattributable of 6 == 33% > 10%.
        rows = [self._attributable() for _ in range(4)] + [self._unattributable() for _ in range(2)]
        result = liveness_check(rows)
        self.assertFalse(result.ok)
        self.assertEqual(result.unattributable, 2)

    def test_boundary_is_inclusive(self):
        # Exactly 10% must PASS (<= threshold).
        rows = [self._attributable() for _ in range(9)] + [self._unattributable()]
        result = liveness_check(rows, threshold=UNATTRIBUTED_FAIL_FRACTION)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(result.unattributable_fraction, 0.1)

    def test_config_detected_row_is_not_unattributable(self):
        # A Gemini row known only by config (no session id) is WEAKLY attributed,
        # not unattributable -- it must not trip the gate. It is still counted in
        # without_session_id as a join-coverage signal.
        rows = [{"agent_session_id": "", "agent_detection_source": "config"}]
        result = liveness_check(rows)
        self.assertTrue(result.ok)
        self.assertEqual(result.unattributable, 0)
        self.assertEqual(result.without_session_id, 1)

    def test_assert_ledger_active_raises_on_empty_returns_on_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CostLedger(Path(tmp) / "cost_ledger")
            with self.assertRaises(RuntimeError):
                assert_ledger_active(ledger, "r1")
            ledger.append(
                build_anchor(
                    run_id="r1", workspace_id="w", pipeline_stage="onboard",
                    env={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "uuid-1"},
                )
            )
            result = assert_ledger_active(ledger, "r1")
            self.assertTrue(result.ok)


class RunPipelineAnchorTests(unittest.TestCase):
    def test_run_pipeline_emits_one_anchor_per_stage(self):
        from core.orchestration import dagster_defs
        from core.orchestration.pipeline_stages import STAGES

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            ok = {"command": "x", "returncode": 0, "ok": True, "stdout_tail": "", "stderr_tail": ""}
            env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "uuid-live"}
            with mock.patch.object(dagster_defs, "_run_command", return_value=ok), \
                    mock.patch.dict("os.environ", env, clear=True):
                results = dagster_defs.run_pipeline("workspaces/ws", repo_root=repo_root)

            self.assertTrue(results["_ok"])
            run_id = results["_run_id"]
            ledger = CostLedger(ledger_dir_for("workspaces/ws", repo_root))
            entries = ledger.entries_for_run(run_id)
            self.assertEqual(len(entries), len(STAGES))
            self.assertEqual(
                {e["pipeline_stage"] for e in entries}, {s.key for s in STAGES}
            )
            # Anchored on the agent-native session id, attributable, liveness ok.
            self.assertTrue(all(e["agent_session_id"] == "uuid-live" for e in entries))
            self.assertTrue(all(e["finished_at"] >= e["started_at"] > "" for e in entries))
            self.assertTrue(results["_cost_ledger"]["ok"])
            self.assertTrue(ledger.summary_path.exists())
            self.assertNotIn("_anchor_errors", results)


class RunGroupingTests(unittest.TestCase):
    def test_session_present_is_stable_and_time_free(self):
        # All commands in one session+workspace collapse to ONE run id (no time
        # component), so a multi-process onboard->build sequence groups.
        a = run_id_for("uuid-live", "workspaces/x")
        b = run_id_for("uuid-live", "workspaces/x")
        self.assertEqual(a, b)
        self.assertEqual(a[1], "session_workspace")
        self.assertTrue(a[0].startswith("sess-"))
        # Different workspace in the same session is a different run.
        self.assertNotEqual(run_id_for("uuid-live", "workspaces/y")[0], a[0])

    def test_absent_session_degrades_to_unique_time_mint(self):
        rid, source = run_id_for("", "workspaces/x")
        self.assertEqual(source, "degraded_time")
        self.assertRegex(rid, r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


class DecoratorTests(unittest.TestCase):
    def test_marks_and_writes_one_anchor_keyed_by_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            # absolute path -> ledger stays in tmp, never a real workspace
            ws = str(Path(tmp) / "workspaces" / "demo")

            @anchored("my-command")
            def fake_main():
                return 42

            self.assertTrue(getattr(fake_main, "__anchored__", False))
            env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "uuid-x"}
            with mock.patch.object(sys, "argv", ["prog", "--workspace", ws]), \
                    mock.patch.dict("os.environ", env, clear=True):
                self.assertEqual(fake_main(), 42)  # command still runs
            rows = CostLedger(ledger_dir_for(ws)).read_entries()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["pipeline_stage"], "my-command")
            self.assertEqual(rows[0]["agent_session_id"], "uuid-x")
            self.assertEqual(rows[0]["run_id_source"], "session_workspace")
            # finished_at captured (can't be backfilled) and >= started_at.
            self.assertTrue(rows[0]["started_at"])
            self.assertTrue(rows[0]["finished_at"])
            self.assertGreaterEqual(rows[0]["finished_at"], rows[0]["started_at"])

    def test_workspace_less_invocation_writes_no_row_but_runs(self):
        @anchored("no-ws-command")
        def fake_main():
            return 7

        with mock.patch.object(sys, "argv", ["prog", "--flag"]):
            self.assertEqual(fake_main(), 7)  # no --workspace: nothing to key, no crash

    def test_anchor_write_failure_is_surfaced_not_swallowed_and_command_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            @anchored("boom")
            def fake_main():
                return 1

            with mock.patch.object(sys, "argv", ["prog", "--workspace", tmp]), \
                    mock.patch.object(cl.CostLedger, "append", side_effect=OSError("disk full")), \
                    mock.patch("sys.stderr") as err:
                self.assertEqual(fake_main(), 1)  # telemetry fault never breaks the command
            printed = "".join(str(c.args[0]) for c in err.write.call_args_list)
            self.assertIn("cost-ledger", printed)  # surfaced, not silent


class CoverageTests(unittest.TestCase):
    """The anti-attrition guarantee: every console-script either anchors or is
    explicitly exempted. Checked by reading __anchored__ at IMPORT time -- not an
    AST guess, not a live run -- so a new command added un-anchored fails here."""

    def test_every_script_anchors_or_is_exempt(self):
        import importlib

        from core.observability.cost_ledger import EXEMPTIONS, entry_point_scripts

        offenders = []
        for name, target in entry_point_scripts().items():
            if name in EXEMPTIONS:
                continue
            mod, func = target.split(":")
            fn = getattr(importlib.import_module(mod), func, None)
            if not getattr(fn, "__anchored__", False):
                offenders.append(name)
        self.assertEqual(
            offenders, [],
            f"commands neither @anchored nor exempted: {offenders} "
            f"(add @anchored(<name>) to the entry point, or exempt it with a reason)",
        )

    def test_exemptions_are_current_and_reasoned(self):
        from core.observability.cost_ledger import EXEMPTIONS, entry_point_scripts

        names = set(entry_point_scripts())
        stale = sorted(n for n in EXEMPTIONS if n not in names)
        self.assertEqual(stale, [], f"stale exemptions (no such script): {stale}")
        reasonless = sorted(n for n, r in EXEMPTIONS.items() if not str(r).strip())
        self.assertEqual(reasonless, [], f"exemptions missing a reason: {reasonless}")


if __name__ == "__main__":
    unittest.main()
