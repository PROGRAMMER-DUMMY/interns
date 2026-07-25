"""Security S2: CostLedger.append() (core/observability/cost_ledger.py) had no
lock and was proven, via live reproduction, to lose ~31% of writes under real
concurrent subprocess load. That was one instance of a pattern repeated across
7 files that all fire on every CLI command or apply-* command. This suite
re-proves each fix the same way the original bug was proven: real OS
subprocesses (not just threads), not a unit-test mock. See
~/.claude/plans/dynamic-cooking-firefly.md S2.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

N_WORKERS = 8
N_PER_WORKER = 25


def _run_workers(script: str, args: list[str], n_workers: int = N_WORKERS) -> None:
    """Launch n_workers real subprocesses running `script`, wait for all."""
    procs = [
        subprocess.Popen([PYTHON, "-c", script, *args], cwd=str(REPO_ROOT))
        for _ in range(n_workers)
    ]
    for p in procs:
        rc = p.wait(timeout=60)
        assert rc == 0, f"worker exited {rc}"


class AuditChainConcurrencyTests(unittest.TestCase):
    def test_no_lost_or_falsely_tampered_entries_under_concurrent_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            chain_path = Path(tmp) / "workspaces" / "demo" / "interns" / "state" / "audit_chain.jsonl"
            script = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from pathlib import Path
                from core.governance.audit_chain import append_audit_record
                chain_path = Path({str(chain_path)!r})
                for i in range(sys.argv.count("x") or {N_PER_WORKER}):
                    append_audit_record(chain_path, {{"i": i}})
            """)
            _run_workers(script, ["x"] * N_PER_WORKER)

            from core.governance.audit_chain import verify_chain

            result = verify_chain(chain_path)
            lines = [l for l in chain_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), N_WORKERS * N_PER_WORKER)
            self.assertTrue(result["ok"], result["reason"])
            self.assertEqual(result["entry_count"], N_WORKERS * N_PER_WORKER)


class TrajectoryRecorderConcurrencyTests(unittest.TestCase):
    def test_no_lost_trajectory_entries_under_concurrent_record_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "workspaces" / "demo"
            ws.mkdir(parents=True)
            script = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from core.onboarding.harness.trajectory_recorder import WorkspaceTrajectoryRecorder
                rec = WorkspaceTrajectoryRecorder({str(root)!r}, "workspaces/demo")
                for i in range({N_PER_WORKER}):
                    rec.record(event_type="tool_start", summary=f"call {{i}}")
            """)
            _run_workers(script, [])

            trajectory_path = ws / "interns" / "state" / "trajectory.jsonl"
            lines = [l for l in trajectory_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), N_WORKERS * N_PER_WORKER)
            for line in lines:
                json.loads(line)  # must all be independently parseable, none interleaved/corrupted


class EventsConcurrencyTests(unittest.TestCase):
    def test_no_lost_events_under_concurrent_emit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "workspaces" / "demo"
            script = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from pathlib import Path
                from core.observability.events import emit_event
                ws = Path({str(ws)!r})
                for i in range({N_PER_WORKER}):
                    emit_event(ws, event_type="command", command="test", summary=str(i))
            """)
            _run_workers(script, [])

            events_path = ws / "interns" / "state" / "events.jsonl"
            lines = [l for l in events_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), N_WORKERS * N_PER_WORKER)
            for line in lines:
                json.loads(line)


class IdempotencyConcurrencyTests(unittest.TestCase):
    def test_distinct_op_ids_all_recorded_under_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspaces" / "demo"
            script = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from pathlib import Path
                from core.onboarding.workspace.idempotency import record_op
                ws = Path({str(ws)!r})
                worker_id = sys.argv[1]
                for i in range({N_PER_WORKER}):
                    record_op(ws, op_id=f"op-{{worker_id}}-{{i}}", command="test")
            """)
            procs = [
                subprocess.Popen([PYTHON, "-c", script, str(w)], cwd=str(REPO_ROOT))
                for w in range(N_WORKERS)
            ]
            for p in procs:
                self.assertEqual(p.wait(timeout=60), 0)

            from core.onboarding.workspace.idempotency import list_applied_ops

            ops = list_applied_ops(ws)
            self.assertEqual(len(ops), N_WORKERS * N_PER_WORKER)
            self.assertEqual(len({o.op_id for o in ops}), N_WORKERS * N_PER_WORKER)

    def test_same_op_id_concurrent_calls_record_exactly_once(self):
        """The check-then-act race: before the fix, two concurrent calls with
        the SAME op_id could both see 'not a duplicate yet' and both record,
        defeating the idempotency guarantee apply-*/finalize-* commands rely
        on. This proves that's closed: exactly one True, one row."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspaces" / "demo"
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            script = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from pathlib import Path
                from core.onboarding.workspace.idempotency import record_op
                ws = Path({str(ws)!r})
                worker_id = sys.argv[1]
                result = record_op(ws, op_id="shared-op-id", command="test")
                (Path({str(out_dir)!r}) / f"result-{{worker_id}}.txt").write_text(str(result))
            """)
            n = 16
            procs = [
                subprocess.Popen([PYTHON, "-c", script, str(w)], cwd=str(REPO_ROOT))
                for w in range(n)
            ]
            for p in procs:
                self.assertEqual(p.wait(timeout=60), 0)

            results = [
                (out_dir / f"result-{w}.txt").read_text().strip() == "True" for w in range(n)
            ]
            self.assertEqual(sum(results), 1, "exactly one caller must win the race")

            from core.onboarding.workspace.idempotency import list_applied_ops

            ops = list_applied_ops(ws)
            self.assertEqual(len(ops), 1, "exactly one row must ever be recorded for the shared op_id")


class UserDecisionsConcurrencyTests(unittest.TestCase):
    def _seed_workspace(self, root: Path, n_features: int) -> None:
        from core.storage.workspace_layout import WorkspaceLayout

        ws = root / "workspaces" / "demo"
        layout = WorkspaceLayout(project_root=ws)
        layout.ensure_runtime_dirs()
        mapping = {
            "summary": {},
            "kpis": [
                {
                    "kpi_id": "kpi_001",
                    "features": [
                        {"feature": f"feature_{i}", "state": "blocked_ambiguous"}
                        for i in range(n_features)
                    ],
                }
            ],
        }
        (layout.contracts_dir / "kpi_feature_mapping.json").write_text(
            json.dumps(mapping), encoding="utf-8"
        )

    def test_concurrent_decisions_on_different_features_all_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._seed_workspace(root, N_WORKERS)
            script = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from core.onboarding.memory.user_decisions import apply_user_decision
                worker_id = sys.argv[1]
                apply_user_decision(
                    {str(root)!r}, "workspaces/demo",
                    kpi_id="kpi_001", feature=f"feature_{{worker_id}}",
                    state="user_confirmed", resolution_type="manual",
                    evidence_note=f"decided by worker {{worker_id}}",
                )
            """)
            procs = [
                subprocess.Popen([PYTHON, "-c", script, str(w)], cwd=str(REPO_ROOT))
                for w in range(N_WORKERS)
            ]
            for p in procs:
                self.assertEqual(p.wait(timeout=60), 0)

            from core.storage.workspace_layout import WorkspaceLayout

            layout = WorkspaceLayout(project_root=root / "workspaces" / "demo")
            mapping = json.loads(
                (layout.contracts_dir / "kpi_feature_mapping.json").read_text(encoding="utf-8")
            )
            features = mapping["kpis"][0]["features"]
            confirmed = [f for f in features if f["state"] == "user_confirmed"]
            # Every worker's decision must have survived -- a lost read-modify-write
            # would show fewer than N_WORKERS confirmed (one worker's write clobbered
            # by another's stale in-memory copy of the mapping).
            self.assertEqual(len(confirmed), N_WORKERS)


class WikiMemoryNamedLockTests(unittest.TestCase):
    def test_named_lock_serializes_across_processes(self):
        """wiki_memory_index.json is shared across ALL workspaces, not scoped
        to one -- named_lock is a distinct code path from workspace_lock and
        needs its own proof of real cross-process exclusion."""
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / "team_memory" / ".wiki_memory.lock"
            counter_path = Path(tmp) / "counter.txt"
            counter_path.write_text("0", encoding="utf-8")
            script = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from pathlib import Path
                from core.storage.workspace_lock import named_lock
                lock_path = Path({str(lock_path)!r})
                counter_path = Path({str(counter_path)!r})
                for _ in range({N_PER_WORKER}):
                    with named_lock(lock_path):
                        n = int(counter_path.read_text())
                        # A window where a non-atomic increment would lose
                        # updates if two processes interleaved read/write.
                        counter_path.write_text(str(n + 1))
            """)
            _run_workers(script, [])
            self.assertEqual(int(counter_path.read_text()), N_WORKERS * N_PER_WORKER)


class FlowStateConcurrencyTests(unittest.TestCase):
    def test_write_state_never_produces_a_torn_or_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "workspaces" / "demo"
            ws.mkdir(parents=True)
            script = textwrap.dedent(f"""
                import sys, json
                sys.path.insert(0, {str(REPO_ROOT)!r})
                from core.onboarding.workspace.flow import WorkspaceFlow
                flow = WorkspaceFlow.__new__(WorkspaceFlow)
                from pathlib import Path
                flow.repo_root = Path({str(root)!r})
                flow.workspace = Path({str(ws)!r})
                flow.session_id = "shared-session"
                from core.storage.workspace_layout import WorkspaceLayout
                flow.layout = WorkspaceLayout(project_root=flow.workspace)
                worker_id = sys.argv[1]
                for i in range({N_PER_WORKER}):
                    # Large-ish payload increases the odds an unlocked
                    # write_text would be caught mid-write by a concurrent one.
                    state = {{"worker": worker_id, "i": i, "pad": "x" * 5000}}
                    flow._write_state(state)
            """)
            procs = [
                subprocess.Popen([PYTHON, "-c", script, str(w)], cwd=str(REPO_ROOT))
                for w in range(N_WORKERS)
            ]
            for p in procs:
                self.assertEqual(p.wait(timeout=60), 0)

            state_path = ws / "interns" / "state" / "workflow_sessions" / "shared-session" / "session.json"
            # Must always be valid, complete JSON -- never truncated/interleaved
            # by a concurrent writer (the property this fix actually closes).
            parsed = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("worker", parsed)


if __name__ == "__main__":
    unittest.main()
