import unittest

from core.onboarding.data_model.generation_workflow import (
    _candidate_relationships,
    _shared_key,
    _understanding_score,
)


def _table(name, columns):
    return {"name": name, "source_dataset": f"datasets/{name}.csv", "columns": columns}


class SharedKeyMatchBasisTests(unittest.TestCase):
    def test_identifier_role_is_evidence_backed(self):
        left = _table("sales", [{"name": "region_id", "role": "identifier"}, {"name": "amount", "role": "measure"}])
        right = _table("regions", [{"name": "region_id", "role": "identifier"}, {"name": "label", "role": "descriptor"}])
        match = _shared_key(left, right)
        self.assertIsNotNone(match)
        self.assertEqual(match[2], "identifier_role")

    def test_name_only_match_is_flagged(self):
        # shared column ends in `id` but is NOT an identifier role on either side
        left = _table("sales", [{"name": "region_id", "role": "descriptor"}, {"name": "amount", "role": "measure"}])
        right = _table("regions", [{"name": "region_id", "role": "descriptor"}, {"name": "label", "role": "descriptor"}])
        match = _shared_key(left, right)
        self.assertIsNotNone(match)
        self.assertEqual(match[2], "name_only")


class CandidateRelationshipConfirmationTests(unittest.TestCase):
    def test_name_only_join_requires_user_confirmation(self):
        tables = [
            _table("sales", [{"name": "region_id", "role": "descriptor"}, {"name": "amount", "role": "measure"}]),
            _table("regions", [{"name": "region_id", "role": "descriptor"}, {"name": "label", "role": "descriptor"}]),
        ]
        rels = _candidate_relationships(tables)
        self.assertEqual(len(rels), 1)
        self.assertTrue(rels[0]["needs_confirmation"])
        self.assertEqual(rels[0]["state"], "candidate_name_only")
        self.assertLessEqual(rels[0]["confidence"], 0.5)

    def test_identifier_join_does_not_require_confirmation(self):
        tables = [
            _table("sales", [{"name": "region_id", "role": "identifier"}, {"name": "amount", "role": "measure"}]),
            _table("regions", [{"name": "region_id", "role": "identifier"}, {"name": "label", "role": "descriptor"}]),
        ]
        rels = _candidate_relationships(tables)
        self.assertEqual(len(rels), 1)
        self.assertFalse(rels[0]["needs_confirmation"])


class UnderstandingScoreTests(unittest.TestCase):
    def test_score_is_bounded(self):
        tables = [
            _table("sales", [{"name": "region_id", "role": "identifier"}, {"name": "amount", "role": "measure"}]),
            _table("regions", [{"name": "region_id", "role": "identifier"}, {"name": "label", "role": "descriptor"}]),
        ]
        draft = {"tables": tables, "relationships": _candidate_relationships(tables)}
        score = _understanding_score(draft)
        value = score["score"] if isinstance(score, dict) else score
        self.assertTrue(0 <= value <= 100)


class SkillRoutingTests(unittest.TestCase):
    def test_routes_to_data_model_creation_and_grill_when_low(self):
        from core.onboarding.data_model.generation_workflow import _dm_conversation_skills

        active = {s["name"]: s["active"] for s in _dm_conversation_skills({"understanding": {"score": 40}})}
        self.assertTrue(active["data-model-creation"])
        self.assertTrue(active["grill-requirements"])
        self.assertTrue(active["domain-model"])


if __name__ == "__main__":
    unittest.main()
