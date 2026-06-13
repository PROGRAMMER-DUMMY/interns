"""Regression tests for core remediation P6 — silent-except hardening.

A swallowed read/parse error must never let a governance/DQ gate report green or
discard human decisions. Workspace-agnostic.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class DataQualityUnreadableTests(unittest.TestCase):
    def test_unreadable_dataset_is_a_finding_not_clean(self) -> None:
        from core.onboarding.data_quality import (
            DATASET_UNREADABLE,
            _count_duplicate_values,
        )

        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.csv"
            # A read failure returns the sentinel, NOT 0 ("clean").
            self.assertEqual(_count_duplicate_values(missing, "id"), DATASET_UNREADABLE)

    def test_count_distinguishes_clean_from_unreadable(self) -> None:
        from core.onboarding.data_quality import (
            DATASET_UNREADABLE,
            _count_duplicate_values,
        )

        with TemporaryDirectory() as tmp:
            good = Path(tmp) / "ok.csv"
            good.write_text("id\n1\n2\n", encoding="utf-8")
            self.assertEqual(_count_duplicate_values(good, "id"), 0)  # clean, readable
            self.assertNotEqual(0, DATASET_UNREADABLE)


class DocumentsFailLoudTests(unittest.TestCase):
    def test_corrupt_accepted_candidates_not_silently_skeletoned(self) -> None:
        from core.onboarding.documents.candidate_apply import _load_durable
        from core.storage.atomic_io import CorruptStoreError

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "accepted_candidates.json"
            path.write_text("{ corrupt", encoding="utf-8")
            # Must NOT return a fresh skeleton (which would be written back and
            # erase accepted human decisions) — must fail loud.
            with self.assertRaises(CorruptStoreError):
                _load_durable(path, "workspaces/demo")

    def test_missing_store_returns_skeleton(self) -> None:
        from core.onboarding.documents.candidate_apply import _load_durable

        with TemporaryDirectory() as tmp:
            data = _load_durable(Path(tmp) / "absent.json", "workspaces/demo")
            self.assertIn("decisions", data)


if __name__ == "__main__":
    unittest.main()
