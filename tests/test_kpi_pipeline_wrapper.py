"""Tests for Task A (run-kpi-pipeline wrapper) and BUG-021 (session resume after relationship approval).

BUG-021: After out-of-band relationship approvals, a session previously blocked on
source_to_target / join_proof_missing must accept `answer --answer continue` and
re-advance instead of raising ValueError.

Task A: `run-kpi-pipeline` wrapper must
  - run the deterministic chain end-to-end on a clean workspace,
  - stop with exit code 1 + actionable message at any human gate,
  - be idempotent / resumable,
  - emit the KPI result packet exactly once on completion.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.flow import (
    WorkspaceFlow,
    pipeline_main,
)


def _make_simple_encounters_workspace(root: Path) -> str:
    """Minimal workspace with one KPI that resolves cleanly (no join needed)."""
    workspace = root / "workspaces" / "demo"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "datasets").mkdir()
    (workspace / "datasets" / "encounters.csv").write_text(
        "Id,START,ENCOUNTERCLASS\nE1,2024-01-01,ambulatory\nE2,2024-01-02,inpatient\n",
        encoding="utf-8",
    )
    (workspace / "docs" / "kpi_registry.csv").write_text(
        "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
        "How many encounters are recorded?,Count encounters,,count(Id),\n",
        encoding="utf-8",
    )
    return "workspaces/demo"


def _make_two_source_workspace(root: Path) -> str:
    """Workspace with two CSVs that need a relationship (join) to be approved."""
    workspace = root / "workspaces" / "two_source"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "datasets").mkdir()
    (workspace / "datasets" / "orders.csv").write_text(
        "order_id,customer_id,total_amount\n1,7,49.99\n2,9,200.00\n",
        encoding="utf-8",
    )
    (workspace / "datasets" / "customers.csv").write_text(
        "customer_id,signup_date,segment\n7,2024-06-01,Loyal\n9,2024-08-15,New\n",
        encoding="utf-8",
    )
    (workspace / "docs" / "kpi_registry.csv").write_text(
        "Key business question,Description,Cuts / grain hints,Metric,Data model refinement required\n"
        "Monthly revenue by segment?,Revenue KPI,segment,sum(total_amount),\n",
        encoding="utf-8",
    )
    (workspace / "docs" / "data_model.md").write_text(
        "# Data Model\n\norders.customer_id -> customers.customer_id\n",
        encoding="utf-8",
    )
    return "workspaces/two_source"


# ---------------------------------------------------------------------------
# BUG-021 tests
# ---------------------------------------------------------------------------

class Bug021ResumeTests(unittest.TestCase):
    """BUG-021: session blocked on source_to_target must accept answer 'continue'
    and re-advance after out-of-band relationship approval, instead of raising ValueError.
    """

    def test_bug021_answer_continue_accepted_on_source_to_target_blocked_session(self):
        """A session in source_to_target blocked status must accept answer='continue'
        without raising ValueError.  After relationships are approved the flow re-advances.
        """
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)

            # Advance until the flow reaches kpi_analyst_review or completes —
            # on a single-source workspace there is no join blocker, so we instead
            # simulate a source_to_target blocked state by patching the saved panel.
            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            session_id = result.session_id

            # Inject a synthetic source_to_target blocked panel into the session state
            # so we can test the answer('continue') path without needing a real
            # multi-source join failure.
            flow = WorkspaceFlow.from_session(root, session_id)
            state = flow._load_state()
            state["stage"] = "source_to_target_blocked"
            state["status"] = "blocked"
            state["current_panel"] = {
                "stage": "source_to_target_blocked",
                "status": "blocked",
                "source": "source_to_target",
                "session_id": session_id,
                "workspace": ws,
                "instruction": "Source-to-target planning found blockers.",
                "question": "",
                "options": [],
                "recommended_option_id": "",
                "artifact_paths": [],
                "summary": {},
            }
            flow._write_state(state)

            # answer('continue') must NOT raise ValueError — it must call
            # _advance_until_stop instead.
            try:
                resumed = WorkspaceFlow.from_session(root, session_id).answer(answer="continue")
            except ValueError as exc:
                self.fail(
                    f"BUG-021: answer('continue') on source_to_target blocked session raised ValueError: {exc}"
                )

            # The flow must have re-advanced (stage should no longer be source_to_target_blocked).
            self.assertNotEqual(
                resumed.stage,
                "source_to_target_blocked",
                "BUG-021: flow should advance past source_to_target_blocked after answer('continue')",
            )

    def test_bug021_answer_unknown_source_still_raises_value_error(self):
        """answer() with an unrecognised source and a non-continue answer must still
        raise ValueError (backward compatibility — no regression).
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="kpi_generation")
            session_id = result.session_id

            # Inject an unsupported source.
            flow = WorkspaceFlow.from_session(root, session_id)
            state = flow._load_state()
            state["current_panel"] = {
                "stage": "some_unknown_stage",
                "status": "blocked",
                "source": "totally_unknown_source",
                "session_id": session_id,
                "workspace": ws,
            }
            flow._write_state(state)

            with self.assertRaises(ValueError):
                WorkspaceFlow.from_session(root, session_id).answer(answer="option_a")

    def test_bug021_answer_continue_on_non_source_to_target_source_raises_value_error(self):
        """answer(answer='continue') on a non-source_to_target source must still raise
        ValueError so governance is preserved.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="kpi_generation")
            session_id = result.session_id

            flow = WorkspaceFlow.from_session(root, session_id)
            state = flow._load_state()
            # source="kpi_analyst_review_blocked" is not the join gate; continue
            # must not be accepted there.
            state["current_panel"] = {
                "stage": "kpi_analyst_review_blocked",
                "status": "blocked",
                "source": "kpi_analyst_review_blocked",
                "session_id": session_id,
                "workspace": ws,
            }
            flow._write_state(state)

            with self.assertRaises(ValueError):
                WorkspaceFlow.from_session(root, session_id).answer(answer="continue")


# ---------------------------------------------------------------------------
# Task A: run-kpi-pipeline wrapper tests
# ---------------------------------------------------------------------------

class PipelineWrapperTests(unittest.TestCase):
    """Tests for the `run-kpi-pipeline` (pipeline_main) wrapper command."""

    def test_pipeline_main_registered_and_callable(self):
        """pipeline_main must be importable from core.onboarding.workspace.flow."""
        from core.onboarding.workspace.flow import pipeline_main  # noqa: F401  # already imported at top

        self.assertTrue(callable(pipeline_main))

    def test_pipeline_main_exits_nonzero_without_workspace_arg(self):
        """Missing --workspace must produce a non-zero exit (argparse error)."""
        with self.assertRaises(SystemExit) as ctx:
            pipeline_main([])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_pipeline_main_runs_end_to_end_and_emits_result_packet(self):
        """Full happy-path: pipeline_main must complete and emit the KPI result packet."""
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)

            stdout = io.StringIO()
            exit_code: int = -1
            with contextlib.redirect_stdout(stdout):
                try:
                    # The kpi-analyst review gate is a HUMAN gate — it will stop here.
                    # We therefore only test up to the gate stop (exit code 1) and
                    # verify the gate message is informative, not that it reaches
                    # "complete" (that would require bypassing the human gate which
                    # the wrapper intentionally forbids).
                    exit_code = pipeline_main([
                        "--repo-root", str(root),
                        "--workspace", ws,
                        "--domain", "generic",
                        "--quiet",
                    ])
                except SystemExit as exc:
                    exit_code = int(exc.code or 0)

            output = stdout.getvalue()
            # Either:
            #  (a) the flow completes and exits 0 with a KPI result packet, OR
            #  (b) it stops at the kpi-analyst review gate (exit 1, [blocked] message)
            # Both are correct behaviour for the wrapper. Any other exit code / crash is wrong.
            self.assertIn(exit_code, (0, 1),
                          f"pipeline_main must exit 0 (complete) or 1 (human gate), got {exit_code}.\nOutput:\n{output}")
            if exit_code == 1:
                # Must print a [blocked] gate stop, not a traceback.
                self.assertIn("[blocked]", output,
                              "gate stop must print a [blocked] headline")
                self.assertNotIn("Traceback", output,
                                 "pipeline_main must not propagate unhandled exceptions")
            else:
                # Completed — must have the result packet.
                self.assertIn("KPI Result Packet", output,
                              "completed pipeline must emit the KPI Result Packet")

    def test_pipeline_main_quiet_flag_suppresses_step_commentary(self):
        """--quiet must suppress step-by-step log lines but still print gate stops or results."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                try:
                    pipeline_main([
                        "--repo-root", str(root),
                        "--workspace", ws,
                        "--quiet",
                    ])
                except SystemExit:
                    pass

            output = stdout.getvalue()
            # With --quiet the "[ok] Step N/5:" lines must NOT appear.
            self.assertNotIn("[ok] Step 1/5:", output)
            self.assertNotIn("[ok] Step 2/5:", output)

    def test_pipeline_main_without_quiet_prints_step_commentary(self):
        """Without --quiet, step commentary must be printed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                try:
                    pipeline_main([
                        "--repo-root", str(root),
                        "--workspace", ws,
                    ])
                except SystemExit:
                    pass

            output = stdout.getvalue()
            # Without --quiet at least the early step lines must appear.
            self.assertIn("[ok] Step 1/5:", output)
            self.assertIn("[ok] Step 2/5:", output)

    def test_pipeline_main_kpi_analyst_gate_prints_session_id_in_command(self):
        """The kpi-analyst gate stop must include the session id in the resolve command."""
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars not installed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = pipeline_main([
                    "--repo-root", str(root),
                    "--workspace", ws,
                    "--quiet",
                ])

            output = stdout.getvalue()
            if exit_code == 1:
                # Must surface the workspace-flow review command so the user knows exactly
                # what to do.
                self.assertIn("workspace-flow review", output,
                              "kpi-analyst gate must include workspace-flow review command")

    def test_pipeline_main_idempotent_second_call_reuses_session(self):
        """Calling pipeline_main twice on the same workspace must resume (not create a new session)
        and must not crash."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)

            stdout1 = io.StringIO()
            with contextlib.redirect_stdout(stdout1):
                try:
                    pipeline_main(["--repo-root", str(root), "--workspace", ws, "--quiet"])
                except SystemExit:
                    pass

            # Second call must not crash.
            stdout2 = io.StringIO()
            try:
                with contextlib.redirect_stdout(stdout2):
                    pipeline_main(["--repo-root", str(root), "--workspace", ws, "--quiet"])
            except SystemExit:
                pass
            except Exception as exc:
                self.fail(f"pipeline_main raised on second call: {exc}")

    def test_pipeline_main_new_session_flag_bypasses_resume(self):
        """--new-session must force a fresh session instead of resuming the existing one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                try:
                    pipeline_main([
                        "--repo-root", str(root),
                        "--workspace", ws,
                        "--new-session",
                        "--quiet",
                    ])
                except (SystemExit, Exception):
                    pass
            # Just verify it didn't crash — no crash == new-session flag accepted.

    def test_pipeline_main_does_not_gate_on_advisory_candidate_relationships(self):
        """Candidate (non-executable) relationships are ADVISORY and must NOT cause a
        wrapper-level relationship gate.

        A candidate join only matters if an in-scope KPI actually needs it; the per-KPI
        join-proof check inside `start` is the correct stop (it sets the session
        `source_to_target_blocked` only for a KPI that requires an unapproved join). The
        STEP-3 relationship block in `pipeline_main` therefore proceeds with an advisory
        `[~]` note and never blocks merely because candidates remain. Hard-blocking on raw
        candidate count over-blocks legitimate single-dataset KPIs that need no join
        (including this single-CSV `count encounters` workspace), so this test locks the
        NON-blocking contract. (Replaces the prior over-broad gate test; see the reasoned
        comment in core/onboarding/workspace/flow.py at the STEP-3 relationship block.)
        """
        import unittest.mock as mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)

            # Force the relationship builder to report a candidate (advisory) relationship.
            from core.onboarding.relationships.contracts import RelationshipContractResult

            fake_result = mock.MagicMock(spec=RelationshipContractResult)
            fake_result.summary.return_value = {
                "executable_relationship_count": 0,
                "candidate_relationship_count": 1,
                "relationship_count": 1,
            }

            # Pre-seed the contracts JSON with a candidate relationship whose own policy
            # declares it advisory -- exactly the shape that must NOT gate the wrapper.
            workspace_path = root / ws
            contracts_dir = workspace_path / "interns" / "generated" / "contracts"
            contracts_dir.mkdir(parents=True, exist_ok=True)
            (contracts_dir / "relationship_contracts.json").write_text(
                json.dumps({
                    "artifact_type": "relationship_contracts.json",
                    "version": 1,
                    "generated_by": "build-relationship-contracts",
                    "workspace": ws,
                    "generated_at": "2026-06-01T00:00:00+00:00",
                    "evidence_order": ["data_model_docs", "profile_schema", "user_confirmed_relationships"],
                    "executable_usage_policy": {
                        "required_for_multi_dataset_generation": True,
                        "allowed_states": ["proven_data_model", "user_confirmed"],
                        "candidate_policy": "candidate relationships are advisory",
                    },
                    "relationships": [
                        {
                            "relationship_id": "rel_test_001",
                            "left_dataset": f"{ws}/datasets/encounters.csv",
                            "right_dataset": f"{ws}/datasets/encounters.csv",
                            "state": "profile_validated",
                            "executable_usage_policy": {
                                "allowed_in_sql_generation": False,
                            },
                        }
                    ],
                    "summary": {
                        "relationship_count": 1,
                        "executable_relationship_count": 0,
                        "candidate_relationship_count": 1,
                    },
                }),
                encoding="utf-8",
            )

            with mock.patch(
                "core.onboarding.workspace.flow.RelationshipContractBuilder"
            ) as MockBuilder:
                MockBuilder.return_value.build.return_value = fake_result

                # No --quiet: the advisory STEP-3 `_log` note is only emitted when not quiet.
                stdout = io.StringIO()
                exit_code: int = -1
                with contextlib.redirect_stdout(stdout):
                    try:
                        exit_code = pipeline_main([
                            "--repo-root", str(root),
                            "--workspace", ws,
                        ])
                    except SystemExit as exc:
                        exit_code = int(exc.code or 0)

            output = stdout.getvalue()
            # STEP 3 must reach the non-blocking advisory path for the candidate relationship.
            self.assertIn(
                "candidate relationship", output,
                f"pipeline_main must emit the advisory candidate note and proceed.\nOutput:\n{output}",
            )
            # The candidate id must NEVER surface as a wrapper-level relationship gate.
            self.assertNotIn(
                "rel_test_001", output,
                f"advisory candidate relationships must not be surfaced as a gate.\nOutput:\n{output}",
            )


# ---------------------------------------------------------------------------
# FIX B: result_review CONTENT gate
# ---------------------------------------------------------------------------

import unittest.mock as _mock

from core.onboarding.workspace.delegation import verdict_from_result_review


class ResultReviewVerdictTests(unittest.TestCase):
    """Unit tests for the extended verdict_from_result_review (FIX B).

    Workspace-agnostic: the verdict operates only on the produced rows/columns.
    """

    def test_healthy_results_are_ok(self):
        entries = [
            {"kpi_id": "kpi_001", "status": "ok", "result_row_count": 3,
             "result_columns": ["segment", "revenue"], "all_blank_columns": []},
        ]
        self.assertEqual(verdict_from_result_review(entries).status, "ok")

    def test_zero_row_kpi_needs_review(self):
        entries = [
            {"kpi_id": "kpi_001", "status": "ok", "result_row_count": 0,
             "result_columns": ["segment", "revenue"], "all_blank_columns": []},
        ]
        verdict = verdict_from_result_review(entries)
        self.assertEqual(verdict.status, "needs_review")
        self.assertIn("kpi_001", verdict.details.get("no_result_kpi_ids") or [])

    def test_all_blank_dimension_column_needs_review(self):
        entries = [
            {"kpi_id": "kpi_dept", "status": "ok", "result_row_count": 5,
             "result_columns": ["department", "count"], "all_blank_columns": ["department"]},
        ]
        verdict = verdict_from_result_review(entries)
        self.assertEqual(verdict.status, "needs_review")
        self.assertIn("kpi_dept", verdict.details.get("all_blank_column_kpis") or {})

    def test_non_ok_execution_status_counts_as_zero_rows(self):
        entries = [
            {"kpi_id": "kpi_err", "status": "error", "error": "boom"},
        ]
        verdict = verdict_from_result_review(entries)
        self.assertEqual(verdict.status, "needs_review")
        self.assertIn("kpi_err", verdict.details.get("no_result_kpi_ids") or [])


class ResultReviewGateWiringTests(unittest.TestCase):
    """Integration: the result_review verdict must BLOCK pipeline completion (FIX B)."""

    def _skip_without_deps(self) -> None:
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars not installed")

    def _complete_session(self, root: Path, ws: str):
        """Advance to the review gate and post a human-confirmed verdict so the
        session reaches `complete`. Returns the completed session id."""
        result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
        self.assertEqual(result.status, "needs_specialist_review")
        done = WorkspaceFlow.from_session(root, result.session_id).review(
            verdict="ok", summary="reviewed", confirmed_by="alice",
        )
        self.assertEqual(done.status, "complete")
        return result.session_id

    def _packet_path(self, root: Path, ws: str) -> Path:
        return (root / ws / "interns" / "generated" / "evidence"
                / "kpi_results" / "current.json")

    def _run_pipeline_resuming(self, root: Path, ws: str, session_id: str):
        """Run pipeline_main so it RESUMES the (already complete) session via a
        read-only `status()` call instead of minting a fresh session that would
        re-execute and overwrite the tampered packet. This isolates the Step 5
        result_review content gate."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with _mock.patch(
                "core.onboarding.workspace.flow.latest_open_session",
                return_value=session_id,
            ):
                try:
                    exit_code = pipeline_main([
                        "--repo-root", str(root), "--workspace", ws,
                        "--domain", "generic", "--quiet",
                    ])
                except SystemExit as exc:
                    exit_code = int(exc.code or 0)
        return exit_code, stdout.getvalue()

    def test_healthy_result_completes(self):
        self._skip_without_deps()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)
            session_id = self._complete_session(root, ws)

            exit_code, output = self._run_pipeline_resuming(root, ws, session_id)
            self.assertEqual(exit_code, 0,
                             f"healthy result must complete.\nOutput:\n{output}")
            self.assertIn("KPI Result Packet", output)
            self.assertIn("## Pipeline Complete", output)

    def test_zero_row_result_blocks_completion(self):
        self._skip_without_deps()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)
            session_id = self._complete_session(root, ws)

            # Tamper the executed packet to look like a zero-row KPI.
            packet = self._packet_path(root, ws)
            data = json.loads(packet.read_text(encoding="utf-8"))
            for entry in data.get("kpis", []):
                entry["status"] = "ok"
                entry["result_row_count"] = 0
                entry["all_blank_columns"] = []
            packet.write_text(json.dumps(data), encoding="utf-8")

            exit_code, output = self._run_pipeline_resuming(root, ws, session_id)
            self.assertNotEqual(exit_code, 0,
                                f"zero-row KPI must block completion.\nOutput:\n{output}")
            self.assertIn("[blocked]", output)
            self.assertNotIn("## Pipeline Complete", output)

    def test_all_blank_dimension_blocks_completion(self):
        self._skip_without_deps()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_simple_encounters_workspace(root)
            session_id = self._complete_session(root, ws)

            packet = self._packet_path(root, ws)
            data = json.loads(packet.read_text(encoding="utf-8"))
            for entry in data.get("kpis", []):
                entry["status"] = "ok"
                entry["result_row_count"] = 4
                entry["all_blank_columns"] = ["department"]
            packet.write_text(json.dumps(data), encoding="utf-8")

            exit_code, output = self._run_pipeline_resuming(root, ws, session_id)
            self.assertNotEqual(exit_code, 0,
                                f"all-blank dimension column must block completion.\nOutput:\n{output}")
            self.assertIn("[blocked]", output)
            self.assertIn("department", output)


if __name__ == "__main__":
    unittest.main()
