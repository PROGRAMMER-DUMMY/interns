import tempfile
import unittest
from pathlib import Path

from core.onboarding.workspace.delegation import (
    DelegationVerdict,
    record_delegation,
    routing_for,
)
from core.storage.workspace_layout import WorkspaceLayout


class DelegationHandoffTests(unittest.TestCase):
    def test_record_delegation_writes_a_handoff_doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "workspaces" / "demo"
            ws.mkdir(parents=True)
            layout = WorkspaceLayout(project_root=ws)
            event = record_delegation(
                layout,
                "workspaces/demo",
                agent="kpi-analyst",
                stage="kpi_output_verification",
                reason="self-grill",
                verdict_fn=lambda: DelegationVerdict(status="ok", summary="all good"),
            )
            self.assertTrue(event.handoff_path, "delegation should produce a handoff path")
            doc = Path(tmp) / event.handoff_path
            self.assertTrue(doc.exists())
            text = doc.read_text(encoding="utf-8")
            # the side agent reads task + expected return + referenced artifacts (not pasted)
            self.assertIn("## Task", text)
            self.assertIn("## Expected return", text)
            self.assertIn("Read these (referenced, not pasted)", text)

    def test_handoff_skill_routed_to_flow_entry(self):
        self.assertIn("handoff", routing_for("flow_entry")["skills"])


if __name__ == "__main__":
    unittest.main()
