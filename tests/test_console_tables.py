import unittest

from core.presentation.console_tables import render_markdown_table


class ConsoleTableTests(unittest.TestCase):
    def test_render_markdown_table(self):
        rendered = render_markdown_table(
            ["Year", "Count"],
            [(2021, 12), (2022, 3)],
        )
        self.assertIn("| Year | Count |", rendered)
        self.assertIn("| 2021 | 12    |", rendered)
        self.assertIn("| 2022 | 3     |", rendered)

    def test_escapes_newlines_and_none(self):
        rendered = render_markdown_table(["Name", "Value"], [("a\nb", None)])
        self.assertIn("| a b  |       |", rendered)


if __name__ == "__main__":
    unittest.main()
