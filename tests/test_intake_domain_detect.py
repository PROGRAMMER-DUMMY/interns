"""Domain detection: derived from workspace evidence, never invented.

Two things are locked here. First the contract: what matched, how confident the
match is, and the exact tokens it rested on -- plus `unknown` whenever nothing
matched. Second the genericity rule: the domain words live in
`core/intake/domain_vocabulary.json` (data) and must never appear as string
literals in `core/intake/*.py` (logic). Same shape as `test_genericity_audit`.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from core.intake.domain_detect import (
    UNKNOWN_DOMAIN,
    VOCABULARY_PATH,
    detect_domain,
    domain_labels,
    load_vocabulary,
    overlay_for,
)
from core.intake.interview import QUESTIONS_BY_ID

REPO_ROOT = Path(__file__).resolve().parents[1]
INTAKE_DIR = REPO_ROOT / "core" / "intake"


def _first_domain() -> tuple[str, dict]:
    """A domain from the vocabulary -- picked by position, never by name, so this
    file stays as workspace-agnostic as the code it tests."""
    return next(iter(load_vocabulary().items()))


def _tables(*names: str) -> dict:
    return {"status": "ok", "tables": [{"name": name, "size_bytes": 1} for name in names]}


class TestDetection(unittest.TestCase):
    def test_table_names_carry_the_signal_and_the_evidence(self):
        slug, entry = _first_domain()
        token = entry["tokens"][0]
        signal = detect_domain(_tables(f"{token}s", "dim_date"))
        self.assertEqual(slug, signal.domain)
        self.assertGreater(signal.confidence, 0.0)
        self.assertTrue(any(token in line and "table" in line for line in signal.evidence))

    def test_column_names_carry_the_signal(self):
        slug, entry = _first_domain()
        token = entry["tokens"][1]
        discovery = {
            "status": "ok",
            "tables": [{"name": "t1", "columns": [{"name": f"{token}_id"}, {"name": "amount"}]}],
        }
        signal = detect_domain(discovery)
        self.assertEqual(slug, signal.domain)
        self.assertTrue(any("column" in line for line in signal.evidence))

    def test_document_filenames_carry_the_signal(self):
        slug, entry = _first_domain()
        token = entry["tokens"][2]
        signal = detect_domain({}, [f"docs/{token} definitions.xlsx"])
        self.assertEqual(slug, signal.domain)
        self.assertTrue(any("document" in line for line in signal.evidence))

    def test_kpi_terms_carry_the_signal(self):
        slug, entry = _first_domain()
        signal = detect_domain({}, None, [f"Total {entry['tokens'][0]} count per month"])
        self.assertEqual(slug, signal.domain)
        self.assertTrue(any("kpi term" in line for line in signal.evidence))

    def test_camel_case_and_plurals_still_match(self):
        slug, entry = _first_domain()
        token = entry["tokens"][0]
        camel = f"Fact{token.capitalize()}Lines"
        self.assertEqual(slug, detect_domain(_tables(camel)).domain)
        self.assertEqual(slug, detect_domain(_tables(f"{token}es")).domain)

    def test_no_match_is_unknown_and_invents_nothing(self):
        signal = detect_domain(_tables("t1", "t2", "dim_date"), ["readme.md"], ["Total rows"])
        self.assertEqual(UNKNOWN_DOMAIN, signal.domain)
        self.assertEqual(0.0, signal.confidence)
        self.assertEqual([], signal.evidence)
        self.assertEqual([], signal.alternatives)

    def test_no_evidence_at_all_is_unknown_not_a_crash(self):
        for payload in ({}, None, {"status": "not_declared"}, {"tables": None}):
            with self.subTest(payload=payload):
                self.assertEqual(UNKNOWN_DOMAIN, detect_domain(payload).domain)

    def test_malformed_discovery_entries_are_skipped(self):
        signal = detect_domain({"tables": ["not a dict", {"name": None}, {}]})
        self.assertEqual(UNKNOWN_DOMAIN, signal.domain)

    def test_one_lonely_token_is_reported_as_weak(self):
        _, entry = _first_domain()
        signal = detect_domain(_tables(entry["tokens"][0]))
        self.assertLessEqual(signal.confidence, 0.4)

    def test_more_agreeing_tokens_raise_confidence(self):
        _, entry = _first_domain()
        weak = detect_domain(_tables(entry["tokens"][0])).confidence
        strong = detect_domain(_tables(*entry["tokens"][:4])).confidence
        self.assertGreater(strong, weak)
        self.assertLessEqual(strong, 1.0)

    def test_a_rival_domain_becomes_an_alternative_and_lowers_confidence(self):
        domains = list(load_vocabulary().items())
        (first, one), (second, two) = domains[0], domains[1]
        signal = detect_domain(_tables(*one["tokens"][:3], *two["tokens"][:2]))
        self.assertEqual(first, signal.domain)
        self.assertIn(second, signal.alternatives)
        self.assertLess(signal.confidence, 1.0)

    def test_repeated_tables_do_not_inflate_the_score(self):
        _, entry = _first_domain()
        token = entry["tokens"][0]
        once = detect_domain(_tables(token)).confidence
        many = detect_domain(_tables(*[f"{token}_{i}" for i in range(20)])).confidence
        self.assertEqual(once, many)


class TestVocabularyFile(unittest.TestCase):
    def test_every_domain_has_a_label_and_tokens(self):
        vocabulary = load_vocabulary()
        self.assertGreaterEqual(len(vocabulary), 10)
        for slug, entry in vocabulary.items():
            with self.subTest(domain=slug):
                self.assertTrue(entry.get("label"))
                self.assertGreaterEqual(len(entry.get("tokens") or []), 5)
                self.assertEqual(slug, slug.lower())

    def test_labels_cover_the_vocabulary(self):
        self.assertEqual(set(load_vocabulary()), set(domain_labels()))

    def test_overlays_only_ever_reference_existing_questions_and_option_ids(self):
        for slug in load_vocabulary():
            for question_id, tune in overlay_for(slug).items():
                with self.subTest(domain=slug, question=question_id):
                    self.assertIn(question_id, QUESTIONS_BY_ID)
                    question = QUESTIONS_BY_ID[question_id]
                    ids = {o["option_id"] for o in question.get("options") or []}
                    recommended = str(tune.get("recommended_option_id") or "")
                    if recommended:
                        self.assertIn(recommended, ids)
                    for option_id in (tune.get("option_labels") or {}):
                        self.assertIn(option_id, ids)
                    if tune.get("suggested_answer"):
                        self.assertEqual("text", question["answer_type"])

    def test_every_overlay_explains_itself(self):
        for slug in load_vocabulary():
            for question_id, tune in overlay_for(slug).items():
                with self.subTest(domain=slug, question=question_id):
                    self.assertGreater(len(str(tune.get("why") or "")), 40)


# ---------------------------------------------------------------- genericity

# Words the PLATFORM legitimately uses whatever the domain is, so a hit on one
# proves nothing. Each is here because the intake spine already owns the word:
# a data "product"/"people"/"analytics"/"chain" reader class, a "supply" of
# files, an "interest" in a decision, and so on. Everything else stays guarded --
# the guard exists to stop `if domain == "<a domain name>"` creeping into logic.
PLATFORM_WORDS = {
    # reader classes and blueprint fact names the intake spine already owns
    "analytics",
    "chain",
    "people",
    "product",
    "supply",
    "retention",       # storage.retention
    "reconciliation",  # dq.reconciliation
    # ordinary English the questions are written in
    "interest",
    "coverage",
    "loss",
    "route",
    "plant",
    "credit",
    "trade",
    "machine",         # "machine contract", "machine-checked"
    "consumption",     # "feature consumption"
    "subscription",    # a cloud subscription, not a billing plan
    "broker",          # a Kafka broker
}


def _guarded_terms() -> set[str]:
    terms: set[str] = set()
    for slug, entry in load_vocabulary().items():
        terms.update(re.findall(r"[a-z]+", slug))
        terms.update(str(token).lower() for token in entry.get("tokens") or [])
    return {term for term in terms if len(term) > 2} - PLATFORM_WORDS


def _string_literals(path: Path) -> list[tuple[int, str]]:
    """Every string constant in `path` that is not a docstring.

    Docstrings and comments explain; only literals can BRANCH, and branching on
    a domain name is what this guard forbids.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


class TestGenericityOfIntakeCode(unittest.TestCase):
    def test_no_domain_word_appears_in_intake_logic(self):
        guarded = _guarded_terms()
        hits: list[str] = []
        for path in sorted(INTAKE_DIR.glob("*.py")):
            for lineno, literal in _string_literals(path):
                for term in guarded:
                    if re.search(rf"\b{re.escape(term)}\b", literal, re.IGNORECASE):
                        hits.append(
                            f"  {path.relative_to(REPO_ROOT)}:{lineno}  `{term}` in {literal[:80]!r}"
                        )
        self.assertFalse(
            hits,
            "Domain vocabulary found in intake LOGIC (it belongs in "
            f"{VOCABULARY_PATH.relative_to(REPO_ROOT)}, matched against workspace "
            "evidence -- never branched on in code):\n" + "\n".join(sorted(set(hits))),
        )

    def test_the_guard_would_catch_a_domain_branch(self):
        """Positive control: the guard is worthless if it cannot fail."""
        guarded = _guarded_terms()
        self.assertTrue(guarded)
        for slug in load_vocabulary():
            with self.subTest(domain=slug):
                words = [w for w in re.findall(r"[a-z]+", slug) if len(w) > 2]
                self.assertTrue(
                    guarded.intersection(words + list(load_vocabulary()[slug]["tokens"])),
                    f"nothing about `{slug}` is guarded",
                )
        branch = f'if domain == "{next(iter(load_vocabulary()))}":'
        self.assertTrue(
            any(re.search(rf"\b{term}\b", branch) for term in guarded),
            "a hardcoded domain branch would slip past the guard",
        )

    def test_the_vocabulary_file_is_the_only_place_the_words_live(self):
        self.assertTrue(VOCABULARY_PATH.exists())
        self.assertEqual(".json", VOCABULARY_PATH.suffix)


if __name__ == "__main__":
    unittest.main()
