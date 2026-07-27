"""Regression: a generated artifact must not silently outlive its generator.

Origin (2026-07-27 verification): `workspaces/rcm_dashboard/dbt` still carried
the KPI-002 identifier leak (`PatientID` in SELECT and GROUP BY) and a
`CURRENT_DATE` that Phase 0 had pinned to a literal months earlier. Neither was
a live bug -- every fix was in `core/`. The artifact on disk predated all of
them, and nothing noticed: incremental onboarding fingerprints INPUTS, so an
unchanged dataset reads as "still current" however far the generator has moved.

Verified end-to-end against a real workspace before this test was written:
generate -> validate clean -> perturb result_view_builder.py -> validator exits
1 naming both artifact families -> revert -> clean again.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.dev.generator_stamp import (
    GENERATOR_MODULES,
    fingerprint,
    stale,
    stamp,
    unstamped,
)
from core.storage.workspace_layout import WorkspaceLayout


def _layout(tmp: str) -> WorkspaceLayout:
    layout = WorkspaceLayout(project_root=Path(tmp) / "workspaces" / "demo")
    layout.ensure_runtime_dirs()
    return layout


class FingerprintTests(unittest.TestCase):
    def test_it_hashes_content_not_mtime(self):
        # mtime would change on every clone/checkout and report all artifacts
        # stale everywhere. Two calls with no edit in between must agree.
        modules = GENERATOR_MODULES["kpi_sql"]
        self.assertEqual(fingerprint(modules), fingerprint(modules))

    def test_different_module_sets_differ(self):
        self.assertNotEqual(
            fingerprint(GENERATOR_MODULES["kpi_sql"]),
            fingerprint(GENERATOR_MODULES["dbt_project"]),
        )

    def test_an_unknown_family_is_refused_rather_than_silently_unstamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(KeyError):
                stamp(_layout(tmp), "not_a_family")


class StaleTests(unittest.TestCase):
    def test_a_freshly_stamped_artifact_is_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            stamp(layout, "dbt_project")
            self.assertEqual(stale(layout), [])

    def test_a_changed_generator_makes_the_artifact_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            stamp(layout, "dbt_project")
            # Simulate the generator moving on, without editing a real module.
            path = layout.state_dir / "generator_stamps.json"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    fingerprint(GENERATOR_MODULES["dbt_project"]), "0" * 16
                ),
                encoding="utf-8",
            )
            findings = stale(layout)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["artifact"], "dbt_project")
            self.assertEqual(findings[0]["generated_by"], "0" * 16)
            self.assertNotEqual(findings[0]["current"], "0" * 16)

    def test_stamping_one_family_does_not_disturb_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            stamp(layout, "dbt_project")
            stamp(layout, "kpi_sql")
            self.assertEqual(stale(layout), [])

    def test_a_corrupt_stamp_file_reports_nothing_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            (layout.state_dir / "generator_stamps.json").write_text(
                "{ truncated", encoding="utf-8"
            )
            self.assertEqual(stale(layout), [])


class UnstampedTests(unittest.TestCase):
    """The rcm_dashboard case: output exists, age unknown. Reported separately
    from `stale` -- same remedy, different confidence."""

    def test_an_existing_unstamped_artifact_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            self.assertEqual(
                unstamped(layout, {"dbt_project": True, "kpi_sql": False}),
                ["dbt_project"],
            )

    def test_an_artifact_that_does_not_exist_is_not_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            self.assertEqual(unstamped(layout, {"dbt_project": False}), [])

    def test_stamping_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            stamp(layout, "dbt_project")
            self.assertEqual(unstamped(layout, {"dbt_project": True}), [])

    def test_stale_and_unstamped_never_report_the_same_family(self):
        # A stamped-but-changed family is stale, NOT unstamped -- conflating
        # "known old" with "age unknown" would make the first claim untrustworthy.
        with tempfile.TemporaryDirectory() as tmp:
            layout = _layout(tmp)
            stamp(layout, "dbt_project")
            path = layout.state_dir / "generator_stamps.json"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    fingerprint(GENERATOR_MODULES["dbt_project"]), "0" * 16
                ),
                encoding="utf-8",
            )
            self.assertEqual([f["artifact"] for f in stale(layout)], ["dbt_project"])
            self.assertEqual(unstamped(layout, {"dbt_project": True}), [])


class GeneratorsStampTheirOwnOutputTests(unittest.TestCase):
    """Every family in the registry must actually be written by some generator,
    or the gate is decorative."""

    def test_each_family_is_stamped_somewhere_in_core(self):
        import re

        root = Path(__file__).resolve().parents[2] / "core"
        sources = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in root.rglob("*.py")
            if "generator_stamp" not in p.name
        )
        for family in GENERATOR_MODULES:
            with self.subTest(family=family):
                self.assertTrue(
                    # `\w*stamp\w*` so an aliased import counts too
                    # (sql_generator.py imports it as `_stamp_generator`).
                    re.search(rf"""\w*stamp\w*\(\s*[\w.]+,\s*["']{family}["']""", sources),
                    f"no generator stamps {family!r}; either wire it up or "
                    "remove it from GENERATOR_MODULES",
                )


if __name__ == "__main__":
    unittest.main()
