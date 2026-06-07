import unittest

from core.onboarding.panel_contract import (
    REQUIRED_KEYS,
    normalize_decision_panel,
    validate_decision_panel,
)


class PanelContractTests(unittest.TestCase):
    def test_normalize_fills_contract_and_passes_validation(self):
        raw = {
            "stage": "route_selection",
            "question": "Which path?",
            "options": [{"option_id": "option_a", "label": "A", "value": "x", "columns": ["c1"]}],
            "recommended_option_id": "option_a",
        }
        panel = normalize_decision_panel(raw, workspace="workspaces/demo", routing_stage="kpi_definition")
        self.assertEqual(validate_decision_panel(panel), [])
        for key in REQUIRED_KEYS:
            self.assertIn(key, panel)
        # routing footer attached from the stage
        self.assertIn("business-analyst", panel["required_specialists"])
        # option payload preserved, contract keys ensured
        opt = panel["options"][0]
        self.assertEqual(opt["value"], "x")
        self.assertEqual(opt["columns"], ["c1"])
        self.assertEqual(opt["description"], "")  # ensured even if builder omitted it

    def test_existing_values_win(self):
        panel = normalize_decision_panel(
            {"stage": "s", "status": "blocked_placeholder_seed", "suggested_skills": [{"name": "grill-requirements"}]},
            workspace="workspaces/demo", routing_stage="kpi_definition",
        )
        self.assertEqual(panel["status"], "blocked_placeholder_seed")          # not overwritten
        self.assertEqual(panel["suggested_skills"], [{"name": "grill-requirements"}])  # not overwritten

    def test_validate_flags_missing_keys_and_bad_options(self):
        errors = validate_decision_panel({"options": [{"label": "no id"}]})
        self.assertTrue(any("missing required key" in e for e in errors))
        self.assertTrue(any("missing option_id" in e for e in errors))

    def test_no_options_is_informational(self):
        panel = normalize_decision_panel({"stage": "terminal"}, workspace="workspaces/demo")
        self.assertEqual(panel["status"], "info")
        self.assertEqual(validate_decision_panel(panel), [])


if __name__ == "__main__":
    unittest.main()
