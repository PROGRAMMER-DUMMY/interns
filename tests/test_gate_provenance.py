"""Tests for BUG-014 gate-provenance enforcement in workspace-flow.

FIX C flipped the default: human-gate provenance is now ENFORCED by default.

Required cases:
  (a) Completion with an agent-asserted gate (no --confirmed-by) is BLOCKED by
      default (exit non-zero) — provenance is enforced.
  (a2) --allow-agent-gates is the escape hatch: it lets that same case complete.
  (b) --require-human-gates also blocks (now redundant with the default; kept
      for backward compatibility).
  (c) A human-confirmed gate (source: human via --confirmed-by) completes
      cleanly with no [~] agent-asserted warning.
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
    _collect_gate_provenance,
    _gate_provenance_headline,
    _render_gate_provenance_lines,
    main as workspace_flow_main,
    pipeline_main,
)
from core.storage.workspace_layout import WorkspaceLayout


def _make_encounters_workspace(root: Path) -> str:
    """Minimal one-KPI workspace that resolves cleanly without joins."""
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


class GateProvenanceHelpersTests(unittest.TestCase):
    """Unit tests for the provenance helper functions."""

    def test_headline_with_no_gates_reports_clean(self):
        """When no gates exist the headline must say all gates confirmed."""
        headline = _gate_provenance_headline([])
        self.assertIn("[ok]", headline)
        self.assertNotIn("[~]", headline)

    def test_headline_with_agent_asserted_gate_warns(self):
        """When a gate is agent-asserted the headline must warn with a count."""
        gates = [{"source": "agent", "gate": "kpi_analyst_review", "label": "KPI-analyst review"}]
        headline = _gate_provenance_headline(gates)
        self.assertIn("[~]", headline)
        self.assertIn("1 gate", headline)
        self.assertNotIn("[ok]", headline)

    def test_headline_with_human_gate_reports_clean(self):
        """When all gates are human-confirmed the headline must say clean."""
        gates = [{"source": "human", "confirmed_by": "alice", "gate": "kpi_analyst_review", "label": "KPI-analyst review"}]
        headline = _gate_provenance_headline(gates)
        self.assertIn("[ok]", headline)
        self.assertNotIn("[~]", headline)

    def test_render_lines_marks_agent_gate_with_tilde(self):
        """Agent-asserted gates must use the [~] marker in rendered lines."""
        gates = [{"source": "agent", "gate": "kpi_analyst_review", "label": "KPI-analyst review (verdict: ok)"}]
        lines = _render_gate_provenance_lines(gates)
        joined = "\n".join(lines)
        self.assertIn("[~]", joined)
        self.assertIn("agent-asserted", joined)

    def test_render_lines_marks_human_gate_with_ok(self):
        """Human-confirmed gates must use the [ok] marker in rendered lines."""
        gates = [{"source": "human", "confirmed_by": "bob", "gate": "kpi_analyst_review", "label": "KPI-analyst review (verdict: ok)"}]
        lines = _render_gate_provenance_lines(gates)
        joined = "\n".join(lines)
        self.assertIn("[ok]", joined)
        self.assertIn("human:bob", joined)

    def test_collect_gate_provenance_reads_kpi_review_from_state(self):
        """_collect_gate_provenance must read kpi_analyst_review from session state."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_path = root / "workspaces" / "demo"
            ws_path.mkdir(parents=True)
            layout = WorkspaceLayout(project_root=ws_path)
            state = {
                "kpi_analyst_review": {
                    "verdict": "ok",
                    "source": "agent",
                    "confirmed_by": "",
                }
            }
            gates = _collect_gate_provenance(state, layout)
            review_gates = [g for g in gates if g["gate"] == "kpi_analyst_review"]
            self.assertEqual(len(review_gates), 1)
            self.assertEqual(review_gates[0]["source"], "agent")

    def test_collect_gate_provenance_reads_relationship_approvals(self):
        """_collect_gate_provenance must read approved relationships from the contracts file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_path = root / "workspaces" / "demo"
            ws_path.mkdir(parents=True)
            layout = WorkspaceLayout(project_root=ws_path)
            layout.ensure_runtime_dirs()
            # Write a contracts file with one user_confirmed relationship.
            contracts = {
                "relationships": [
                    {
                        "relationship_id": "rel_001",
                        "state": "user_confirmed",
                        "approval": {"source": "human", "confirmed_by": "carol"},
                    }
                ]
            }
            layout.relationship_contracts_path.write_text(
                json.dumps(contracts), encoding="utf-8"
            )
            state: dict = {}
            gates = _collect_gate_provenance(state, layout)
            rel_gates = [g for g in gates if "rel_001" in g["gate"]]
            self.assertEqual(len(rel_gates), 1)
            self.assertEqual(rel_gates[0]["source"], "human")
            self.assertEqual(rel_gates[0]["confirmed_by"], "carol")


class GateProvenanceCompletionTests(unittest.TestCase):
    """Integration tests: gate provenance at completion of a full KPI flow."""

    def _skip_without_deps(self) -> None:
        try:
            import duckdb  # noqa: F401
            import polars  # noqa: F401
        except ImportError:
            self.skipTest("duckdb or polars not installed")

    # ------------------------------------------------------------------
    # Case (a): agent-asserted gate is BLOCKED by default (FIX C)
    # ------------------------------------------------------------------
    def test_agent_asserted_gate_blocks_by_default(self):
        """Completion with an agent-asserted kpi-analyst review (no --confirmed-by)
        must BLOCK by default (exit non-zero) — provenance is enforced (FIX C).
        """
        self._skip_without_deps()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_encounters_workspace(root)

            # Advance to the review gate.
            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            self.assertEqual(result.status, "needs_specialist_review")

            # Post review WITHOUT --confirmed-by (agent-asserted) and WITHOUT the
            # escape hatch.
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = workspace_flow_main([
                    "--repo-root", str(root),
                    "review",
                    "--session", result.session_id,
                    "--verdict", "ok",
                    "--summary", "agent check",
                    # NO --confirmed-by => source: agent
                ])

            output = stdout.getvalue()
            # Must block (exit non-zero) by default.
            self.assertNotEqual(exit_code, 0,
                                f"Default mode must BLOCK an agent-asserted gate (FIX C).\nOutput:\n{output}")
            self.assertIn("[blocked]", output,
                          "Default agent-asserted gate must print [blocked] headline")
            self.assertIn("agent-asserted", output,
                          "[blocked] message must name agent-asserted gates")

    # ------------------------------------------------------------------
    # Case (a2): --allow-agent-gates escape hatch lets it complete
    # ------------------------------------------------------------------
    def test_allow_agent_gates_escape_hatch_completes(self):
        """--allow-agent-gates must let an agent-asserted gate complete (exit 0)."""
        self._skip_without_deps()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_encounters_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            self.assertEqual(result.status, "needs_specialist_review")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = workspace_flow_main([
                    "--repo-root", str(root),
                    "review",
                    "--session", result.session_id,
                    "--verdict", "ok",
                    "--summary", "agent check",
                    "--allow-agent-gates",  # explicit opt-out
                    # NO --confirmed-by => source: agent
                ])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0,
                             f"--allow-agent-gates must complete even with agent-asserted gate.\nOutput:\n{output}")
            # The escape hatch lets completion PROCEED, but it does not make the
            # result human-verified: the provenance BLOCK message must be absent
            # (the FIX D packet banner may still honestly say UNVERIFIED).
            self.assertNotIn("human-gate provenance: completion blocked", output,
                             "--allow-agent-gates must NOT print the provenance block message")

    # ------------------------------------------------------------------
    # Case (b): --require-human-gates blocks agent-asserted gate
    # ------------------------------------------------------------------
    def test_require_human_gates_blocks_agent_asserted_gate(self):
        """--require-human-gates must block completion (exit non-zero) when the
        kpi-analyst review is agent-asserted (no --confirmed-by).
        """
        self._skip_without_deps()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_encounters_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            self.assertEqual(result.status, "needs_specialist_review")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = workspace_flow_main([
                    "--repo-root", str(root),
                    "review",
                    "--session", result.session_id,
                    "--verdict", "ok",
                    "--summary", "agent check",
                    "--require-human-gates",  # strict opt-in
                    # NO --confirmed-by => source: agent
                ])

            output = stdout.getvalue()
            # Must NOT complete — must exit non-zero (2).
            self.assertNotEqual(exit_code, 0,
                                f"--require-human-gates must block when gate is agent-asserted.\nOutput:\n{output}")
            self.assertIn("[blocked]", output,
                          "--require-human-gates must print [blocked] headline")
            self.assertIn("agent-asserted", output,
                          "[blocked] message must name agent-asserted gates")

    # ------------------------------------------------------------------
    # Case (c): human-confirmed gate completes cleanly (no [~] warning)
    # ------------------------------------------------------------------
    def test_human_confirmed_gate_completes_cleanly(self):
        """When --confirmed-by is set, the gate is human-confirmed and completion
        must proceed cleanly with [ok] provenance and no [~] agent-asserted warning
        in the Gate Provenance section.
        """
        self._skip_without_deps()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_encounters_workspace(root)

            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            self.assertEqual(result.status, "needs_specialist_review")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = workspace_flow_main([
                    "--repo-root", str(root),
                    "review",
                    "--session", result.session_id,
                    "--verdict", "ok",
                    "--summary", "human reviewed",
                    "--confirmed-by", "alice",   # => source: human
                ])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0,
                             f"Human-confirmed gate must complete cleanly.\nOutput:\n{output}")
            # The Gate Provenance section must show [ok] for this gate.
            self.assertIn("human:alice", output,
                          "CLI output must show human:alice in Gate Provenance")
            # With only human-confirmed gates the [~] agent-asserted warning must
            # NOT appear in the Gate Provenance section (it may appear elsewhere
            # for other things, but not for gate provenance lines).
            # We check the panel JSON for the authoritative value.
            panel_path = root / result.current_panel_path if result.current_panel_path else None
            # After the review the result obj has the old path; reload from disk.
            layout = WorkspaceLayout(project_root=(root / ws).resolve())
            sessions_dir = layout.state_dir / "workflow_sessions"
            # Find the session panel.
            session_panel: dict = {}
            for session_dir in sessions_dir.iterdir():
                panel_file = session_dir / "current.json"
                if panel_file.exists():
                    try:
                        data = json.loads(panel_file.read_text(encoding="utf-8"))
                        if data.get("stage") == "complete":
                            session_panel = data
                            break
                    except (json.JSONDecodeError, OSError):
                        pass
            if session_panel:
                provenance = (session_panel.get("summary") or {}).get("gate_provenance") or []
                agent_gates = [g for g in provenance if g.get("source") != "human"]
                # The kpi_analyst_review gate must be human (source: human).
                review_gates = [g for g in provenance if g.get("gate") == "kpi_analyst_review"]
                if review_gates:
                    self.assertEqual(
                        review_gates[0]["source"], "human",
                        "Gate provenance for kpi_analyst_review must be 'human' when --confirmed-by is set"
                    )
                    self.assertEqual(review_gates[0]["confirmed_by"], "alice")
                # No agent-asserted gates for the kpi_analyst_review.
                kpi_agent_gates = [g for g in agent_gates if g.get("gate") == "kpi_analyst_review"]
                self.assertEqual(
                    kpi_agent_gates, [],
                    "kpi_analyst_review gate must NOT be agent-asserted when --confirmed-by alice was passed"
                )

    # ------------------------------------------------------------------
    # Case (b) via pipeline_main: --require-human-gates also works there
    # ------------------------------------------------------------------
    def test_pipeline_main_require_human_gates_blocks_agent_asserted(self):
        """pipeline_main --require-human-gates must exit 2 when the kpi-analyst
        review is agent-asserted (no --confirmed-by).

        The test simulates the post-completion state by seeding a session where the
        review is recorded without confirmed_by, then calling pipeline_main with
        --require-human-gates on the already-completed session.
        """
        self._skip_without_deps()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = _make_encounters_workspace(root)

            # Step 1: advance to needs_specialist_review.
            result = WorkspaceFlow(root, ws).start(intent="full_kpi_sql")
            self.assertEqual(result.status, "needs_specialist_review")

            # Step 2: post review WITHOUT confirmed-by (agent-asserted).
            done = WorkspaceFlow.from_session(root, result.session_id).review(
                verdict="ok",
                summary="agent check — no human",
                # confirmed_by intentionally omitted
            )
            self.assertEqual(done.status, "complete",
                             "Default mode must complete before we test --require-human-gates")

            # Step 3: re-run pipeline_main with --require-human-gates.
            # It will resume the existing completed session and check provenance.
            stdout = io.StringIO()
            exit_code: int = -1
            with contextlib.redirect_stdout(stdout):
                try:
                    exit_code = pipeline_main([
                        "--repo-root", str(root),
                        "--workspace", ws,
                        "--domain", "generic",
                        "--require-human-gates",
                        "--quiet",
                    ])
                except SystemExit as exc:
                    exit_code = int(exc.code or 0)

            output = stdout.getvalue()
            # Must block (exit 2) because the kpi-analyst gate is agent-asserted.
            self.assertIn(exit_code, (1, 2),
                          f"--require-human-gates must block (exit 1 or 2) when gate is agent-asserted.\nOutput:\n{output}")
            self.assertIn("[blocked]", output,
                          "pipeline_main --require-human-gates must print [blocked] headline")


if __name__ == "__main__":
    unittest.main()
