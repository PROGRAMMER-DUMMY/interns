"""CI drift linter for CONTEXT-MAP.md and the CLI registry.

`CONTEXT-MAP.md` rule 4 promises "zero spec drift" between the context tree and disk.
Nothing enforced it, and the repo already demonstrated the failure mode: GO_NO_GO_2026-07
read "three P1s open" for a month after they had closed. A promise with no check is a
promise that rots.

Two directions of drift are checked, because they fail differently:

* A directory the map CLAIMS has a context file, but disk does not -> a stale map that
  sends a reader to a file that isn't there.
* A source directory on disk that the map never mentions -> new code landing outside the
  documented tree.

The CLI half is BASELINE-LOCKED on purpose. 39 of 136 `[project.scripts]` entry points
have no `### <command>` section in TOOLS.md today. A test that fails on all 39 would be
deleted rather than fixed, so this locks the current set and fails only on a NEW
undocumented command. Shrinking KNOWN_UNDOCUMENTED_CLIS is the follow-up work; the test
enforces that the number never grows.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTEXT_MAP = REPO_ROOT / "CONTEXT-MAP.md"

# CONTEXT-MAP.md states workspaces/ is excluded by workspace-isolation policy. The rest
# are third-party or tool-owned trees the map deliberately documents at their root only
# (vendor/ has one CONTEXT-vendor.md covering the vendored packages).
EXCLUDED_TOP_LEVEL = {
    "workspaces", "vendor", ".git", ".venv", ".venv_docling", "node_modules",
    ".claude", ".agents", ".gemini", ".codex", ".github", ".serena", ".pytest_cache",
    "develop_spec", "interns",
}
EXCLUDED_DIR_NAMES = {"__pycache__", ".ipynb_checkpoints"}

# Directories with .py files that are tooling/scratch rather than documented platform
# surface. Keep this list SHORT and justified -- every entry is a hole in the contract.
UNMAPPED_ALLOWED = {
    Path("."),                      # repo root: documented by CONTEXT.md, not CONTEXT-..md
    Path("docker/airflow/dags"),    # generated Airflow DAG drop dir, documented by docker/
    Path("spikes/dbt_dagster"),     # throwaway spike, documented by spikes/
}

# Commands with no `### <command>` section in TOOLS.md as of this baseline. Do not add to
# this set -- document the command instead.
KNOWN_UNDOCUMENTED_CLIS = frozenset({
    "assess-workspace-phi", "build-intent-contract", "build-tool-index",
    "check-airflow-health", "check-platform-readiness",
    "check-remote-execution-gate", "cost-ledger-ingest", "dataops", "dbt-index",
    "deploy-databricks-workspace", "fetch-source-documents",
    "generate-dbt-project", "green-gate", "kpi-local-warehouse", "pipeline-run",
    "plan-kpi-completion", "publish-dbt-state", "query-workspace-evidence-graph",
    "recommend-kpi-engine", "reconcile-warehouse-cost", "resolver-accuracy",
    "retrieve-docs", "run-dbt-backfill", "run-ingestion",
    "run-kpi-execution-harness", "scan-document", "source-catalog",
    "suggest-kpi-improvements", "sync-workspace-code", "token-report",
    "validate-engine-generation", "validate-kpi-intent-coverage",
    "verify-audit-chain", "verify-dbt-project", "verify-kpi-output",
    "verify-orchestration", "workspace-dashboard-deck", "workspace-dashboard-pdf",
    "workspace-state-health",
})


def source_directories() -> list[Path]:
    """Repo-relative directories that contain at least one .py file and are in scope."""
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        directory = path.parent
        relative = directory.relative_to(REPO_ROOT)
        parts = relative.parts
        if parts and parts[0] in EXCLUDED_TOP_LEVEL:
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in parts):
            continue
        if relative not in out:
            out.append(relative)
    return sorted(set(out))


def context_file_for(relative: Path) -> Path:
    name = relative.name if relative != Path(".") else REPO_ROOT.name
    return REPO_ROOT / relative / f"CONTEXT-{name}.md"


def claimed_context_files() -> set[Path]:
    """Every CONTEXT-*.md path the map links to, as a repo-relative path."""
    text = CONTEXT_MAP.read_text(encoding="utf-8")
    claimed: set[Path] = set()
    # Path characters only: an unbounded `.` crosses the closing paren of one link
    # and swallows prose up to the next CONTEXT-*.md mention.
    for match in re.finditer(r"file:///([^)\s\"'`]*?CONTEXT-[A-Za-z0-9_]+\.md)", text):
        raw = match.group(1).replace("\\", "/")
        marker = "/interns/"
        if marker in raw:
            claimed.add(Path(raw.split(marker, 1)[1]))
    return claimed


def declared_clis() -> list[str]:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"\[project\.scripts\](.*?)(?:\n\[|\Z)", text, re.S)
    return re.findall(r"^([A-Za-z0-9_-]+)\s*=", block.group(1), re.M) if block else []


def documented_clis() -> set[str]:
    text = (REPO_ROOT / "TOOLS.md").read_text(encoding="utf-8")
    return set(re.findall(r"^###\s+`?([A-Za-z0-9_-]+)`?", text, re.M))


class ContextMapDriftTest(unittest.TestCase):
    def test_every_source_directory_has_a_context_file(self) -> None:
        missing = [
            str(d)
            for d in source_directories()
            if d not in UNMAPPED_ALLOWED and not context_file_for(d).exists()
        ]
        self.assertEqual(
            missing,
            [],
            "CI DRIFT: source directories with no CONTEXT-<dir>.md "
            f"(add the file, or justify it in UNMAPPED_ALLOWED): {missing}",
        )

    def test_every_context_file_the_map_claims_exists_on_disk(self) -> None:
        stale = sorted(
            str(rel) for rel in claimed_context_files() if not (REPO_ROOT / rel).exists()
        )
        self.assertEqual(
            stale,
            [],
            f"CI DRIFT: CONTEXT-MAP.md links to context files that do not exist: {stale}",
        )

    def test_context_map_is_not_empty(self) -> None:
        # Guards the two tests above: a parser that silently matched nothing would
        # make them vacuously pass.
        self.assertGreater(len(claimed_context_files()), 20)
        self.assertGreater(len(source_directories()), 20)


class CliDocumentationDriftTest(unittest.TestCase):
    def test_no_new_undocumented_cli_entry_points(self) -> None:
        undocumented = set(declared_clis()) - documented_clis()
        new = sorted(undocumented - KNOWN_UNDOCUMENTED_CLIS)
        self.assertEqual(
            new,
            [],
            "CI DRIFT: new CLI entry point(s) with no `### <command>` section in "
            f"TOOLS.md: {new}",
        )

    def test_baseline_does_not_list_commands_that_are_now_documented(self) -> None:
        # Keeps the baseline honest: once a command is documented it must leave the
        # list, so the debt count can only shrink.
        stale = sorted(KNOWN_UNDOCUMENTED_CLIS & documented_clis())
        self.assertEqual(
            stale,
            [],
            f"KNOWN_UNDOCUMENTED_CLIS lists commands that are now documented: {stale}",
        )

    def test_baseline_only_lists_real_commands(self) -> None:
        stale = sorted(KNOWN_UNDOCUMENTED_CLIS - set(declared_clis()))
        self.assertEqual(
            stale, [], f"KNOWN_UNDOCUMENTED_CLIS lists commands that no longer exist: {stale}"
        )


if __name__ == "__main__":
    unittest.main()
