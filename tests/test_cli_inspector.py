"""Unit tests for core/agents/cli_inspector.py."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core.agents.cli_inspector import (
    _parse_gemini_model,
    _parse_claude_model,
    _parse_codex_model,
    inspect_cli,
    resolve_active_model,
    render_startup_banner,
)


class GeminiConfigParseTests(unittest.TestCase):
    def test_nested_model_block(self):
        text = '{"model": {"name": "gemini-3.1-pro-preview"}}'
        self.assertEqual(_parse_gemini_model(text), "gemini-3.1-pro-preview")

    def test_top_level_string(self):
        text = '{"model": "gemini-2.5-flash"}'
        self.assertEqual(_parse_gemini_model(text), "gemini-2.5-flash")

    def test_missing_returns_none(self):
        text = '{"other": "thing"}'
        self.assertIsNone(_parse_gemini_model(text))

    def test_invalid_json_returns_none(self):
        self.assertIsNone(_parse_gemini_model("not json"))


class ClaudeConfigParseTests(unittest.TestCase):
    def test_string_alias(self):
        self.assertEqual(_parse_claude_model('{"model": "opus"}'), "opus")

    def test_full_id(self):
        self.assertEqual(
            _parse_claude_model('{"model": "claude-sonnet-4-6"}'),
            "claude-sonnet-4-6",
        )

    def test_invalid_json(self):
        self.assertIsNone(_parse_claude_model("garbage"))


class CodexConfigParseTests(unittest.TestCase):
    def test_basic_toml(self):
        text = 'model = "gpt-5.5"\nother = "x"'
        self.assertEqual(_parse_codex_model(text), "gpt-5.5")

    def test_regex_fallback_on_malformed(self):
        # Malformed TOML but the regex still finds the model line
        text = "[broken section\nmodel = \"gpt-5\"\n"
        self.assertEqual(_parse_codex_model(text), "gpt-5")

    def test_missing(self):
        self.assertIsNone(_parse_codex_model("# no model here\n"))


class InspectCLITests(unittest.TestCase):
    @patch("core.agents.cli_inspector.shutil.which", return_value=None)
    def test_missing_binary(self, _):
        st = inspect_cli("gemini")
        self.assertFalse(st.installed)
        self.assertIn("not in PATH", st.note)

    @patch("core.agents.cli_inspector.shutil.which", return_value="C:\\fake\\gemini")
    def test_installed_no_config(self, _):
        # Config path won't exist for our fake binary
        st = inspect_cli("gemini")
        self.assertTrue(st.installed)
        # model may or may not be set depending on real local config; we just
        # assert the dataclass shape is sane
        self.assertEqual(st.name, "gemini")


class ResolveActiveModelTests(unittest.TestCase):
    def test_unknown_main_agent_returns_none(self):
        self.assertIsNone(resolve_active_model("not-a-real-cli"))


class StartupBannerTests(unittest.TestCase):
    def test_banner_includes_main_agent(self):
        out = render_startup_banner("gemini-cli", has_api_key=False)
        self.assertIn("gemini-cli", out)
        self.assertIn("CLIEngine", out)

    def test_banner_includes_api_mode_when_key_set(self):
        out = render_startup_banner("gemini-cli", has_api_key=True)
        self.assertIn("APIEngine", out)
        self.assertIn("GOOGLE_API_KEY", out)

    def test_banner_warns_on_unknown_main_agent(self):
        out = render_startup_banner("bogus-agent", has_api_key=False)
        self.assertIn("bogus-agent", out)


if __name__ == "__main__":
    unittest.main()
