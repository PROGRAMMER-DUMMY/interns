import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.session_snapshot import (
    append_command,
    append_decision,
    append_file_change,
    append_turn,
    finish_session,
    start_session,
    verify_session_intent,
)
from tools.session_snapshot import main as session_snapshot_main


class SessionSnapshotTests(unittest.TestCase):
    def test_records_session_snapshot_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / ".agents" / "sessions" / "current"

            start_session(
                root,
                session_dir,
                workspace="workspaces/Hospital_Patient_Records",
                tool="gemini",
                session_id="demo-session",
            )
            append_turn(root, session_dir, role="user", content="set working directory as patients record")
            append_turn(root, session_dir, role="assistant", content="Workspace selected.")
            append_command(
                root,
                session_dir,
                command="uv run list-workspace-files --workspace workspaces/Hospital_Patient_Records",
                status="ok",
                summary="Listed workspace files.",
                exit_code="0",
            )
            append_file_change(
                root,
                session_dir,
                path="workspaces/Hospital_Patient_Records/interns/reports/current.md",
                action="create",
                summary="Generated report.",
            )
            append_decision(root, session_dir, decision="User approved cleanup dry run.", status="accepted")
            result = finish_session(root, session_dir)
            verification = verify_session_intent(root, session_dir)

            self.assertEqual(result.status, "finished")
            self.assertEqual(result.event_count, 7)
            self.assertTrue((session_dir / "events.jsonl").exists())
            self.assertTrue((session_dir / "transcript.md").exists())
            self.assertTrue((session_dir / "commands.md").exists())
            self.assertTrue((session_dir / "file_changes.md").exists())
            self.assertTrue((session_dir / "decisions.md").exists())
            self.assertTrue((session_dir / "compact.md").exists())
            self.assertTrue((session_dir / "intent_verification.md").exists())
            self.assertTrue((session_dir / "intent_verification.json").exists())
            self.assertEqual(verification["overall_status"], "model_pass")

            snapshot = json.loads((session_dir / "snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["session_id"], "demo-session")
            self.assertEqual(snapshot["workspace"], "workspaces/Hospital_Patient_Records")
            self.assertEqual(snapshot["paths"]["transcript"], ".agents/sessions/current/transcript.md")
            self.assertEqual(snapshot["paths"]["compact"], ".agents/sessions/current/compact.md")
            self.assertEqual(
                snapshot["paths"]["intent_verification"],
                ".agents/sessions/current/intent_verification.md",
            )

            transcript = (session_dir / "transcript.md").read_text(encoding="utf-8")
            self.assertIn("set working directory as patients record", transcript)
            self.assertIn("Workspace selected.", transcript)
            compact = (session_dir / "compact.md").read_text(encoding="utf-8")
            self.assertIn("Compact Session Context", compact)
            self.assertIn("User approved cleanup dry run.", compact)
            self.assertIn("Overall status: `model_pass`", compact)
            verification_text = (session_dir / "intent_verification.md").read_text(encoding="utf-8")
            self.assertIn("Intent Verification", verification_text)
            self.assertIn("Task Verification", verification_text)

    def test_redacts_common_secret_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / ".agents" / "sessions" / "current"

            start_session(root, session_dir, workspace="workspaces/demo", tool="codex")
            append_turn(root, session_dir, role="user", content="api_key=abc123 password=hunter2")

            transcript = (session_dir / "transcript.md").read_text(encoding="utf-8")
            self.assertIn("api_key=<redacted>", transcript)
            self.assertIn("password=<redacted>", transcript)
            self.assertNotIn("abc123", transcript)
            self.assertNotIn("hunter2", transcript)

            verification = verify_session_intent(root, session_dir)
            self.assertEqual(verification["overall_status"], "failed")
            secret_check = _check_by_id(verification, "no_secret_like_content")
            self.assertEqual(secret_check["status"], "failed")

    def test_cli_named_session_uses_alias_and_timestamped_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = Path.cwd()
            try:
                os.chdir(root)
                session_snapshot_main(
                    [
                        "start",
                        "--name",
                        "Gemini Hospital",
                        "--workspace",
                        "workspaces/Hospital_Patient_Records",
                        "--tool",
                        "gemini",
                        "--session-id",
                        "named-session",
                    ]
                )
                pointer = root / ".agents" / "sessions" / "_aliases" / "gemini-hospital.txt"
                self.assertTrue(pointer.exists())
                session_rel = pointer.read_text(encoding="utf-8").strip()
                self.assertRegex(session_rel, r"^\.agents/sessions/\d{4}-\d{2}-\d{2}-\d{6}-gemini-hospital$")

                session_snapshot_main(
                    [
                        "append",
                        "--name",
                        "gemini-hospital",
                        "--role",
                        "user",
                        "--content",
                        "show KPI results",
                    ]
                )
                session_snapshot_main(
                    [
                        "file-change",
                        "--name",
                        "gemini-hospital",
                        "--path",
                        "tools/session_snapshot.py",
                        "--action",
                        "update",
                        "--summary",
                        "Added verifier.",
                    ]
                )
                session_snapshot_main(["verify", "--name", "gemini-hospital"])

                session_dir = root / session_rel
                transcript = (session_dir / "transcript.md").read_text(encoding="utf-8")
                compact = (session_dir / "compact.md").read_text(encoding="utf-8")
                verification = json.loads(
                    (session_dir / "intent_verification.json").read_text(encoding="utf-8")
                )
                self.assertIn("show KPI results", transcript)
                self.assertIn("show KPI results", compact)
                self.assertEqual(verification["overall_status"], "not_checked")
                self.assertEqual(
                    (root / ".agents" / "sessions" / "current_session.txt")
                    .read_text(encoding="utf-8")
                    .strip(),
                    session_rel,
                )
            finally:
                os.chdir(cwd)

    def test_verify_fails_on_non_user_absolute_local_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / ".agents" / "sessions" / "current"

            start_session(root, session_dir, workspace="workspaces/demo", tool="codex")
            append_turn(root, session_dir, role="user", content="do not show local absolute paths")
            append_turn(
                root,
                session_dir,
                role="assistant",
                content="Output written to C:/Users/example/project/out.md",
            )

            verification = verify_session_intent(root, session_dir)
            self.assertEqual(verification["overall_status"], "failed")
            path_check = _check_by_id(verification, "no_local_absolute_paths")
            self.assertEqual(path_check["status"], "failed")

    def test_verify_fails_delete_without_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / ".agents" / "sessions" / "current"

            start_session(root, session_dir, workspace="workspaces/demo", tool="codex")
            append_turn(root, session_dir, role="user", content="delete generated interns folder")
            append_file_change(
                root,
                session_dir,
                path="workspaces/demo/interns",
                action="delete",
                summary="Deleted generated folder.",
            )

            verification = verify_session_intent(root, session_dir)
            self.assertEqual(verification["overall_status"], "failed")
            delete_check = _check_by_id(verification, "delete_requires_confirmation")
            self.assertEqual(delete_check["status"], "failed")


def _check_by_id(verification: dict, check_id: str) -> dict:
    for check in verification["guardrail_checks"]:
        if check["id"] == check_id:
            return check
    raise AssertionError(f"check not found: {check_id}")


if __name__ == "__main__":
    unittest.main()
