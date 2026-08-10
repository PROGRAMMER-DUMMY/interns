"""Genericity guard: no domain vocabulary in the feature-resolution Python.

WHY THIS RULE EXISTS
--------------------
`core/` is the workspace-agnostic engine. The moment a healthcare/RCM noun is
baked into a scoring branch, that branch is a silent accuracy tax on every
other workspace: a retail or logistics tenant gets scored by a rule written for
claims data and never learns why its mapping ranked oddly. This has already
happened once -- the `procedure`->`description` scoring branch removed from
`derivation_patterns.py` (referenced in-repo as T12) was exactly that shape,
and nothing structural stopped it from being written. Meaning belongs in
workspace evidence (profiles, dictionary, accepted decisions) and in the routed
domain agents; it does not belong in a deterministic core module.

This guard is the structural stop. It parses every `*.py` under
`core/onboarding/features/` and fails on a domain token appearing in EXECUTABLE
code -- identifiers, attributes, parameter names, and string literals.

WHAT IS DELIBERATELY NOT SCANNED
--------------------------------
1. `derivation_patterns.json` -- KNOWN PENDING, out of scope on purpose. That
   catalogue is still full of domain nouns (cpt, provider, encounter, denial,
   specialty, birth); a later work item replaces it with statistical detectors.
   Its exclusion here is a recorded decision, not an oversight. This guard is
   `**/*.py` only, so it passes today and still catches new Python leakage.
2. Comments and docstrings. Every current hit in them is prose explaining THIS
   rule ("Domain-specific terms (e.g., 'encounter', 'claim') must come in via
   structural_hints"). Banning the words used to document the ban makes the
   documentation worse and changes no behavior. Code is where a domain word
   actually decides something, so code is what is scanned.
3. `KNOWN_PENDING_LITERALS` below -- the Python mirror of the JSON catalogue in
   (1). Narrowly scoped to exact literals so a NEW domain word in the same file
   still fails.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

FEATURES_DIR = Path(__file__).resolve().parents[1] / "core" / "onboarding" / "features"

DOMAIN_TOKENS = frozenset(
    {
        "cpt",
        "icd",
        "payor",
        "payer",
        "encounter",
        "claim",
        "denial",
        "specialty",
        "diagnosis",
        "patient",
        "provider",
        "hospital",
        "medicare",
        "medicaid",
    }
)

# Whole-word match over snake_case AND camelCase, so `claim_id` / `ClaimId` /
# `CPT` are caught while `encountered`, `unclaimed`, and `playerdata` are not.
# Equality on the split word -- never a substring test, which is what turns a
# guard like this into a source of false alarms nobody trusts.
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+")

# See docstring note (3). These exact string literals in `derived_evidence.py`
# are pattern IDs and their example payloads, keyed 1:1 to entries in
# `derivation_patterns.json`. They are NOT independent leakage -- they are the
# Python half of the same known-pending catalogue and die with it when the JSON
# is replaced by statistical detectors. Excluded by exact value (not by file, not
# by line) so any other domain word appearing in this module still fails.
KNOWN_PENDING_LITERALS: dict[str, frozenset[str]] = {
    "derived_evidence.py": frozenset(
        {"cpt_family", "cpt_reference", "provider_specialty", "provider_id"}
    ),
}


def _offending_tokens(text: str) -> list[str]:
    return sorted({w for w in (m.group().lower() for m in _WORD_RE.finditer(text)) if w in DOMAIN_TOKENS})


def _docstring_node_ids(tree: ast.Module) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        first = body[0] if body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def _scan(path: Path) -> list[str]:
    """Return one 'file:line: token' violation string per offending code node."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_node_ids(tree)
    exempt = KNOWN_PENDING_LITERALS.get(path.name, frozenset())
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings or node.value in exempt:
                continue
            text = node.value
        else:
            # Identifiers, attribute names, def/class names, parameter names,
            # and keyword-argument names -- everything a domain rule can hide in.
            text = " ".join(
                str(getattr(node, attr))
                for attr in ("id", "attr", "name", "arg")
                if isinstance(getattr(node, attr, None), str)
            )
            if not text:
                continue
        for token in _offending_tokens(text):
            violations.append(f"{path}:{getattr(node, 'lineno', 0)}: domain token {token!r}")
    return violations


class FeaturesGenericityGuardTest(unittest.TestCase):
    """Fails when a domain noun is hardcoded into feature-resolution Python.

    The engine must score a logistics workspace and a claims workspace by the
    same rules; a domain branch in `core/` breaks that silently rather than
    loudly, which is why this is a gated test and not a code-review convention.
    """

    def test_no_domain_vocabulary_in_features_python(self):
        modules = sorted(FEATURES_DIR.rglob("*.py"))
        self.assertTrue(modules, f"no Python modules found under {FEATURES_DIR}")
        violations = [v for path in modules for v in _scan(path)]
        self.assertEqual(
            [],
            violations,
            "[x] Domain vocabulary leaked into workspace-agnostic core code.\n"
            + "\n".join(violations)
            + "\n\nFix the module -- derive the behavior from workspace evidence "
            "(profiles, data dictionary, accepted decisions) or move the judgment "
            "into a routed domain agent. Do NOT widen DOMAIN_TOKENS or add an "
            "allowlist entry without an inline comment justifying it.",
        )

    def test_guard_detects_a_planted_violation(self):
        """The guard is worthless if it can't fail; prove the matcher fires."""
        self.assertEqual(["claim"], _offending_tokens("claim_id"))
        self.assertEqual(["patient"], _offending_tokens("PatientRecord"))
        self.assertEqual(["cpt"], _offending_tokens("CPT"))
        # Tightened to whole words: these are NOT domain leakage.
        self.assertEqual([], _offending_tokens("encountered"))
        self.assertEqual([], _offending_tokens("unclaimed"))
        self.assertEqual([], _offending_tokens("providence"))


if __name__ == "__main__":
    unittest.main()
