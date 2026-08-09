"""Deprecated stage CLIs delegate to prepare-kpi-blocker-panel.

The three retired entry points (`resolve-kpi-features`, `derived-feature-markdown`,
`blocker-question-panel`) must redirect their default invocation to the canonical
wrapper, while stage-only flags, apply modes, and internal wrapper calls keep the
legacy behavior so debugging workflows are unaffected.
"""
from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from core.onboarding.cli_deprecation import (
    announce_deprecated_cli_redirect,
    is_internal_cli_call,
)
from core.onboarding.features import derived_markdown
from core.onboarding.kpi import blocker_question_panel, feature_resolver


def _clean_env() -> mock._patch_dict:
    return mock.patch.dict(
        os.environ,
        {"AUTORESEARCH_INTERNAL_CALL": "", "AUTORESEARCH_SUPPRESS_CLI_DEPRECATION": ""},
        clear=False,
    )


class ResolveKpiFeaturesRedirectTest(unittest.TestCase):
    def test_default_resolve_mode_redirects_to_prepare(self) -> None:
        with _clean_env(), mock.patch(
            "core.onboarding.kpi.blocker_cli.prepare_main", return_value=0
        ) as prepare:
            with redirect_stderr(io.StringIO()):
                code = feature_resolver.main(
                    [
                        "--workspace",
                        "workspaces/demo",
                        "--repo-root",
                        ".",
                        "--domain",
                        "logistics",
                        "--include-candidates",
                    ]
                )
        self.assertEqual(code, 0)
        prepare.assert_called_once_with(
            [
                "--workspace",
                "workspaces/demo",
                "--repo-root",
                ".",
                "--domain",
                "logistics",
            ]
        )

    def test_apply_decision_keeps_legacy_path(self) -> None:
        with _clean_env(), mock.patch(
            "core.onboarding.kpi.blocker_cli.prepare_main"
        ) as prepare:
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    feature_resolver.main(
                        ["--workspace", "workspaces/demo", "--apply-decision"]
                    )
        prepare.assert_not_called()

    def test_internal_call_keeps_legacy_resolve(self) -> None:
        with mock.patch.dict(os.environ, {"AUTORESEARCH_INTERNAL_CALL": "1"}), mock.patch(
            "core.onboarding.kpi.blocker_cli.prepare_main"
        ) as prepare, mock.patch.object(
            feature_resolver, "KPIFeatureResolver"
        ) as resolver:
            resolver.return_value.run.return_value.summary.return_value = {}
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = feature_resolver.main(["--workspace", "workspaces/demo"])
        self.assertEqual(code, 0)
        prepare.assert_not_called()
        resolver.assert_called_once()


class DerivedFeatureMarkdownRedirectTest(unittest.TestCase):
    def test_default_invocation_redirects(self) -> None:
        with _clean_env(), mock.patch(
            "core.onboarding.kpi.blocker_cli.prepare_main", return_value=0
        ) as prepare:
            with redirect_stderr(io.StringIO()):
                code = derived_markdown.main(
                    ["--workspace", "workspaces/demo", "--repo-root", "."]
                )
        self.assertEqual(code, 0)
        prepare.assert_called_once_with(
            ["--workspace", "workspaces/demo", "--repo-root", "."]
        )

    def test_stage_only_flags_keep_legacy(self) -> None:
        with _clean_env(), mock.patch(
            "core.onboarding.kpi.blocker_cli.prepare_main"
        ) as prepare, mock.patch.object(
            derived_markdown, "DerivedFeatureMarkdownConverter"
        ) as converter:
            run_result = converter.return_value.run.return_value
            run_result.option_count = 0
            run_result.output_dir = "out"
            run_result.files = []
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                derived_markdown.main(
                    ["--workspace", "workspaces/demo", "--out", "tmp_out"]
                )
        prepare.assert_not_called()
        converter.assert_called_once()


class BlockerQuestionPanelRedirectTest(unittest.TestCase):
    def test_default_invocation_redirects(self) -> None:
        with _clean_env(), mock.patch(
            "core.onboarding.kpi.blocker_cli.prepare_main", return_value=0
        ) as prepare:
            with redirect_stderr(io.StringIO()):
                code = blocker_question_panel.main(
                    ["--workspace", "workspaces/demo", "--repo-root", "."]
                )
        self.assertEqual(code, 0)
        prepare.assert_called_once_with(
            ["--workspace", "workspaces/demo", "--repo-root", "."]
        )

    def test_stage_only_flags_keep_legacy(self) -> None:
        with _clean_env(), mock.patch(
            "core.onboarding.kpi.blocker_cli.prepare_main"
        ) as prepare, mock.patch.object(
            blocker_question_panel, "BlockerQuestionPanelBuilder"
        ) as builder:
            run_result = builder.return_value.run.return_value
            run_result.question_count = 0
            run_result.output_dir = "out"
            run_result.current_json = "a"
            run_result.current_markdown = "b"
            run_result.index_json = "c"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                blocker_question_panel.main(
                    ["--workspace", "workspaces/demo", "--out", "tmp_out"]
                )
        prepare.assert_not_called()
        builder.assert_called_once()


class PrepareSolutionBlueprintRedirectTest(unittest.TestCase):
    """D1: `prepare-solution-blueprint` was the legacy blueprint producer,
    superseded by the cloud-first spine's `prepare-blueprint`. Two producers
    for one artifact is how they drift."""

    def test_default_invocation_redirects_to_prepare_blueprint(self) -> None:
        from core.onboarding import blueprint as legacy

        with _clean_env(), mock.patch(
            "core.blueprint.cli.prepare_blueprint_main", return_value=0
        ) as prepare:
            with redirect_stderr(io.StringIO()):
                code = legacy.prepare_main(
                    [
                        "--workspace", "workspaces/demo",
                        "--repo-root", ".",
                        "--catalog", "acme",
                        "--gold-schema", "marts",
                    ]
                )
        self.assertEqual(code, 0)
        prepare.assert_called_once_with(
            [
                "--workspace", "workspaces/demo",
                "--repo-root", ".",
                "--catalog", "acme",
                "--gold-schema", "marts",
            ]
        )

    def test_source_root_is_dropped_because_the_source_is_declared(self) -> None:
        # The new spine reads the source from `declare-source`
        # (workspace_settings.source_declaration), so --source-root has no
        # equivalent and must not be forwarded as an unknown flag.
        from core.onboarding import blueprint as legacy

        with _clean_env(), mock.patch(
            "core.blueprint.cli.prepare_blueprint_main", return_value=0
        ) as prepare:
            buf = io.StringIO()
            with redirect_stderr(buf):
                legacy.prepare_main(
                    [
                        "--workspace", "workspaces/demo",
                        "--source-root", "s3://bkt/pfx",
                        "--catalog", "acme",
                    ]
                )
        forwarded = prepare.call_args[0][0]
        self.assertNotIn("--source-root", forwarded)
        self.assertNotIn("s3://bkt/pfx", forwarded)
        self.assertIn("declare-source", buf.getvalue())

    def test_exit_code_is_the_delegates(self) -> None:
        from core.onboarding import blueprint as legacy

        with _clean_env(), mock.patch(
            "core.blueprint.cli.prepare_blueprint_main", return_value=3
        ):
            with redirect_stderr(io.StringIO()):
                code = legacy.prepare_main(["--workspace", "workspaces/demo"])
        self.assertEqual(code, 3)


class DeprecationHelperTest(unittest.TestCase):
    def test_redirect_notice_suppressed_by_env(self) -> None:
        with mock.patch.dict(
            os.environ, {"AUTORESEARCH_SUPPRESS_CLI_DEPRECATION": "1"}
        ):
            buf = io.StringIO()
            with redirect_stderr(buf):
                announce_deprecated_cli_redirect("x", prefer="y")
        self.assertEqual(buf.getvalue(), "")

    def test_redirect_notice_names_both_commands(self) -> None:
        with _clean_env():
            buf = io.StringIO()
            with redirect_stderr(buf):
                announce_deprecated_cli_redirect(
                    "resolve-kpi-features", prefer="prepare-kpi-blocker-panel"
                )
        notice = buf.getvalue()
        self.assertIn("resolve-kpi-features", notice)
        self.assertIn("prepare-kpi-blocker-panel", notice)

    def test_is_internal_cli_call_reads_env(self) -> None:
        with mock.patch.dict(os.environ, {"AUTORESEARCH_INTERNAL_CALL": "1"}):
            self.assertTrue(is_internal_cli_call())
        with _clean_env():
            self.assertFalse(is_internal_cli_call())


if __name__ == "__main__":
    unittest.main()
