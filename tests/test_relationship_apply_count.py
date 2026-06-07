"""Regression: relationship-approval count must stay correct + monotonic.

Reproduces the reported symptom where, approving several relationship
contracts in one session, the returned ``executable_relationship_count``
came back non-monotonic and impossible (e.g. 3 -> 7 -> 4 -> 5 -> 6 with 7
total relationships).

Two layers:

* ``test_sequential_apply_count_is_correct_and_monotonic`` -- single-process,
  no contention. Proves the count recompute + return value is correct after
  every apply. If this fails, the bug is a faulty recompute/return (b).

* ``test_concurrent_apply_no_lost_update`` -- overlapping applies wrapped in
  the same ``workspace_lock`` the governed CLI envelope uses. Proves the
  per-workspace mutex actually serialises read-modify-write so no approval is
  clobbered by a last-writer-wins race (a). If this fails, the lock is not
  providing mutual exclusion.

Generic synthetic workspace only (no domain vocabulary).
"""
from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.onboarding.relationships.contracts import (
    apply_relationship_answer,
)
from core.onboarding.workspace.cli_runner import run_workspace_command
from core.storage.workspace_layout import WorkspaceLayout
from core.storage.workspace_lock import workspace_lock


def _seed_contract(workspace_path: Path, count: int) -> list[str]:
    """Write a relationship_contracts.json with ``count`` candidate edges.

    Each starts in ``profile_validated`` (a non-executable candidate state),
    so the executable count starts at 0 and must climb by exactly 1 per
    approved edge.
    """
    layout = WorkspaceLayout(project_root=workspace_path)
    layout.ensure_runtime_dirs()
    relationships = []
    for i in range(count):
        relationships.append(
            {
                "relationship_id": f"rel-{i:02d}",
                "left_dataset": f"workspaces/syn/d{i}a.csv",
                "left_column": "k",
                "right_dataset": f"workspaces/syn/d{i}b.csv",
                "right_column": "k",
                "state": "profile_validated",
                "approval": {"state": "needs_review"},
                "executable_usage_policy": {
                    "allowed_in_sql_generation": False,
                    "allowed_in_polars_generation": False,
                    "allowed_in_pyspark_generation": False,
                    "allowed_in_medallion_generation": False,
                    "block_reason": "candidate",
                },
                "decision_history": [],
            }
        )
    data = {
        "workspace": "workspaces/syn",
        "generated_by": "test-seed",
        "relationships": relationships,
        "summary": {
            "relationship_count": count,
            "executable_relationship_count": 0,
            "candidate_relationship_count": count,
        },
    }
    path = layout.contracts_dir / "relationship_contracts.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return [r["relationship_id"] for r in relationships]


class RelationshipApplyCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.workspace_rel = "workspaces/syn"
        self.workspace_path = (self.repo_root / self.workspace_rel).resolve()
        self.workspace_path.mkdir(parents=True)

    def _disk_executable_count(self) -> int:
        path = (
            WorkspaceLayout(project_root=self.workspace_path).contracts_dir
            / "relationship_contracts.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data["summary"]["executable_relationship_count"])

    def test_sequential_apply_count_is_correct_and_monotonic(self) -> None:
        total = 7
        rids = _seed_contract(self.workspace_path, total)

        seen = []
        for n, rid in enumerate(rids, start=1):
            result = apply_relationship_answer(
                self.repo_root,
                self.workspace_rel,
                relationship_id=rid,
                answer="approve",
            )
            seen.append(result.executable_relationship_count)
            self.assertEqual(
                result.executable_relationship_count,
                n,
                f"after {n} approvals the returned executable count must be {n}",
            )
            self.assertEqual(
                self._disk_executable_count(),
                n,
                "returned count must match the count persisted on disk",
            )

        # Monotonic non-decreasing, ending at total.
        self.assertEqual(seen, list(range(1, total + 1)))

    def test_replay_reports_current_disk_count_not_stale_payload(self) -> None:
        """A governed idempotent replay must report the CURRENT executable
        count, not the count frozen in the cached payload from the first apply.

        This is the reported display artifact: with distinct approvals advancing
        the disk count, an echoed stale replay payload makes the reported count
        jump backwards (non-monotonic) even though disk is correct.
        """
        total = 7
        rids = _seed_contract(self.workspace_path, total)

        def _run(rid: str) -> dict:
            buf = io.StringIO()
            with redirect_stdout(buf):
                run_workspace_command(
                    command="apply-relationship-answer",
                    workspace=self.workspace_rel,
                    repo_root=str(self.repo_root),
                    fn=lambda: apply_relationship_answer(
                        self.repo_root,
                        self.workspace_rel,
                        relationship_id=rid,
                        answer="approve",
                    ),
                    op_args={"workspace": self.workspace_rel, "relationship_id": rid, "answer": "approve"},
                    allow_replay=False,
                    record_idempotent=True,
                )
            return json.loads(buf.getvalue())

        # First apply of rel-00 -> disk count 1, payload count 1.
        first = _run(rids[0])
        self.assertEqual(first.get("executable_relationship_count"), 1)

        # Advance disk by approving the other six directly.
        for rid in rids[1:]:
            apply_relationship_answer(
                self.repo_root, self.workspace_rel, relationship_id=rid, answer="approve"
            )
        self.assertEqual(self._disk_executable_count(), total)

        # Re-run the SAME op for rel-00 -> idempotent replay. The reported count
        # must reflect current disk (7), not the stale cached 1.
        replay = _run(rids[0])
        self.assertEqual(replay.get("status"), "idempotent_replay")
        reported = replay.get("result", {}).get("executable_relationship_count")
        self.assertEqual(
            reported,
            total,
            "idempotent replay must report the current on-disk executable "
            f"count ({total}), not the stale cached payload value",
        )

    def test_concurrent_apply_no_lost_update(self) -> None:
        """Overlapping applies wrapped in the same workspace_lock the governed
        envelope uses must not lose an approval (last-writer-wins clobber)."""
        total = 7
        rids = _seed_contract(self.workspace_path, total)

        start = threading.Barrier(total)
        errors: list[BaseException] = []

        def worker(rid: str) -> None:
            try:
                start.wait()
                # Mirror cli_runner.run_workspace_command: the entire
                # read-modify-write runs inside the per-workspace mutex.
                with workspace_lock(self.workspace_path, timeout_seconds=30.0):
                    apply_relationship_answer(
                        self.repo_root,
                        self.workspace_rel,
                        relationship_id=rid,
                        answer="approve",
                    )
            except BaseException as exc:  # noqa: BLE001 - surfaced to test
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(rid,)) for rid in rids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"workers raised: {errors}")
        # All 7 approvals must survive: no lost update under contention.
        self.assertEqual(
            self._disk_executable_count(),
            total,
            "every concurrent approval must persist; a lower count means a "
            "lost update (lock failed to serialise read-modify-write)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
