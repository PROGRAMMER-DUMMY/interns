import unittest

from core.blueprint.playbook import PlaybookError, consult, load_playbook


class LoadPlaybookTests(unittest.TestCase):
    def test_loads_and_validates_the_real_yaml(self):
        rules = load_playbook()
        self.assertGreater(len(rules), 20)

    def test_every_rule_has_threshold_and_source_url(self):
        for rule in load_playbook():
            detect = rule["detect"]
            self.assertIn("threshold", detect, rule["id"])
            self.assertNotEqual(detect["threshold"], "", rule["id"])
            self.assertIsNotNone(detect["threshold"], rule["id"])
            self.assertTrue(rule.get("source_url"), rule["id"])

    def test_every_rule_has_confidence_high_or_medium(self):
        for rule in load_playbook():
            self.assertIn(rule["confidence"], ("high", "medium"), rule["id"])

    def test_every_rule_has_ordered_nonempty_remedies(self):
        for rule in load_playbook():
            self.assertTrue(rule["remedies"], rule["id"])
            for remedy in rule["remedies"]:
                self.assertIn("action", remedy, rule["id"])

    def test_rule_ids_are_unique(self):
        rules = load_playbook()
        ids = [r["id"] for r in rules]
        self.assertEqual(len(ids), len(set(ids)))

    def test_missing_threshold_fails_loud(self, tmp_path=None):
        import tempfile
        from pathlib import Path

        bad_yaml = """
rules:
  - id: broken_rule
    engine: spark
    symptom: "missing threshold"
    detect:
      metric: some.metric
      source: somewhere
      comparator: ">"
    remedies:
      - action: do_something
    source_url: https://example.com
    confidence: high
"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.yaml"
            p.write_text(bad_yaml, encoding="utf-8")
            with self.assertRaises(PlaybookError):
                load_playbook(p)

    def test_missing_source_url_fails_loud(self):
        import tempfile
        from pathlib import Path

        bad_yaml = """
rules:
  - id: broken_rule
    engine: spark
    symptom: "missing source_url"
    detect:
      metric: some.metric
      source: somewhere
      comparator: ">"
      threshold: 0
    remedies:
      - action: do_something
    confidence: high
"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.yaml"
            p.write_text(bad_yaml, encoding="utf-8")
            with self.assertRaises(PlaybookError):
                load_playbook(p)


class ConsultTests(unittest.TestCase):
    def test_known_symptom_by_id_returns_cheapest_first_remedies(self):
        matched = consult(symptoms=["spill_to_disk"])
        self.assertEqual(len(matched), 1)
        remedies = matched[0]["remedies"]
        self.assertEqual(remedies[0]["action"], "size_up_compute")
        self.assertEqual(remedies[1]["action"], "reduce_projection")

    def test_known_symptom_by_metric_threshold(self):
        matched = consult(metrics={"query_profile.bytes_spilled_to_disk": 12345})
        ids = {r["id"] for r in matched}
        self.assertIn("spill_to_disk", ids)
        self.assertIn("revisit_spill", ids)

    def test_metric_below_threshold_does_not_match(self):
        matched = consult(metrics={"query_profile.bytes_spilled_to_disk": 0})
        ids = {r["id"] for r in matched}
        self.assertNotIn("spill_to_disk", ids)

    def test_unknown_symptom_returns_empty_list_with_clear_structure(self):
        matched = consult(symptoms=["this symptom does not exist anywhere"])
        self.assertEqual(matched, [])
        self.assertIsInstance(matched, list)

    def test_no_input_returns_empty_list(self):
        self.assertEqual(consult(), [])

    def test_revisit_triggers_carry_the_canonical_last_resort_remedy(self):
        matched = consult(symptoms=["revisit_tier_boundary"])
        self.assertEqual(len(matched), 1)
        actions = [r["action"] for r in matched[0]["remedies"]]
        self.assertIn("consult_playbook_first", actions)


if __name__ == "__main__":
    unittest.main()
