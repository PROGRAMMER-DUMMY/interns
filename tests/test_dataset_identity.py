"""core.profiling.dataset_identity: distinguishing a local filesystem path
from a Unity Catalog fqn, and deriving a correct display stem/name for
either -- see the module docstring for the bug this fixes.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from core.profiling.dataset_identity import (
    dataset_display_name,
    dataset_display_stem,
    is_uc_fqn,
    uc_fqn_parts,
)


class UcFqnDetectionTests(unittest.TestCase):
    def test_recognizes_a_real_uc_fqn(self):
        self.assertTrue(is_uc_fqn("`healthcare_rcm`.`bronze`.`cptcodes`"))
        self.assertEqual(
            uc_fqn_parts("`healthcare_rcm`.`bronze`.`cptcodes`"),
            ("healthcare_rcm", "bronze", "cptcodes"),
        )

    def test_rejects_local_paths(self):
        for raw in (
            "workspaces/demo/datasets/transactions.csv",
            "workspaces/demo/datasets/sub/foo.parquet",
            "C:\\Users\\shubh\\workspaces\\demo\\datasets\\bar.csv",
            "not_a_valid_fqn",
            "a.b.c",  # unquoted -- never what either profiler actually emits
            "",
        ):
            self.assertFalse(is_uc_fqn(raw), raw)
            self.assertIsNone(uc_fqn_parts(raw), raw)


class DisplayStemNameTests(unittest.TestCase):
    def test_uc_fqn_stem_is_the_real_table_name(self):
        # The exact bug: Path(fqn).stem strips everything after the LAST
        # dot, losing the table name entirely.
        fqn = "`healthcare_rcm`.`bronze`.`cptcodes`"
        self.assertEqual(dataset_display_stem(fqn), "cptcodes")
        self.assertEqual(dataset_display_name(fqn), "cptcodes")
        # Prove the bug this replaces actually exists, so this test can't
        # silently pass for the wrong reason.
        self.assertNotEqual(Path(fqn).stem, "cptcodes")

    def test_local_paths_are_byte_identical_to_pathlib(self):
        cases = [
            "workspaces/demo/datasets/transactions.csv",
            "workspaces/demo/datasets/sub/foo.parquet",
            "workspaces/demo/datasets/no_extension",
            "single_component.csv",
        ]
        for raw in cases:
            self.assertEqual(dataset_display_stem(raw), Path(raw).stem, raw)
            self.assertEqual(dataset_display_name(raw), Path(raw).name, raw)

    def test_empty_string_does_not_raise(self):
        self.assertEqual(dataset_display_stem(""), "")
        self.assertEqual(dataset_display_name(""), "")


class GenericityGuardTest(unittest.TestCase):
    def test_no_hardcoded_workspace_vocabulary(self):
        text = Path("core/profiling/dataset_identity.py").read_text(encoding="utf-8")
        # Docstring/comments legitimately reference real examples for
        # explanation -- scope the guard to the executable regex/logic only.
        code_start = text.index("_UC_FQN_RE = re.compile")
        region = text[code_start:]
        banned = re.compile(r"healthcare|\brcm\b|hospital|cptcodes", re.IGNORECASE)
        match = banned.search(region)
        self.assertIsNone(match, f"found workspace-specific vocabulary in generic code: {match}")


if __name__ == "__main__":
    unittest.main()
