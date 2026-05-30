"""Genericity audit: greps core/ and tools/ for known workspace-specific
violations and fails the build when new ones creep in.

Locks in the workspace-agnostic rule from [[feedback-workspace-agnostic]].
This test does NOT enforce semantic genericity (that requires judgment); it
enforces the SHAPE rule: no hardcoded workspace names, dataset filenames,
domain-specific table names, or curated business vocabulary in non-test
production paths.

Test files are explicitly allowed to mention any name — fixtures must be
concrete. Help-text / docstring examples in CLI files are also allowed
(documented as such with `e.g.` proximity).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]

PROD_DIRS = ("core", "tools")
EXCLUDED_PATH_PARTS = {
    "__pycache__",
    ".venv",
    "node_modules",
    "build",
    "dist",
    ".pytest_cache",
}


VIOLATION_PATTERNS = {
    "hardcoded_workspace_name": re.compile(
        r"\bworkspaces/(Healthcare-RCM-Data-Platform|Hospital_Patient_Records)\b"
    ),
    "hardcoded_dataset_subpath": re.compile(r"\btrendytech-hospital-[ab]\b"),
    "hardcoded_transactions_pk_sql": re.compile(
        r"\bFROM\s+transactions\s+GROUP\s+BY\s+TransactionID\b", re.IGNORECASE
    ),
    "hardcoded_domain_filter_check": re.compile(
        r"if\s+[\"']medicare[\"']\s+in\s+cuts(?:\.lower\(\))?\s+and\s+[\"']medicare[\"']\s+not\s+in"
    ),
    "hardcoded_kpi_name_template": re.compile(
        r"if\s+[\"'](zero payer coverage|refund rate by product|landing pages convert|"
        r"share of sessions reach product|highest revenue per session|"
        r"average order value|most readmissions|gross revenue)[\"']\s+in\s+name(?:\.lower\(\))?",
        re.IGNORECASE,
    ),
    "hospital_regex_in_naming": re.compile(
        r"re\.(?:search|sub|match|compile)\([^)]*[\"'][^\"']*hospital[^\"']*[\"']",
        re.IGNORECASE,
    ),
    "default_domain_healthcare": re.compile(
        r"default\s*=\s*[\"']healthcare[\"']|:\s*str\s*=\s*[\"']healthcare[\"']"
    ),
    "hardcoded_healthcare_word_literal": re.compile(
        r"[\"'](medicare|medicaid|payor|payer|encounter|claim|patient|diagnosis|icd)[\"']\s*(?:in|==)\s+\w",
        re.IGNORECASE,
    ),
    # A tuple/list of domain-entity id names baked into prod code (e.g. a
    # PREFERRED_JOIN_KEYS = ("patientid", "encounterid", ...)). Derive keys from
    # workspace evidence (identifier roles, profiles) instead.
    "hardcoded_domain_join_keys": re.compile(
        r"(?:[\"'](?:patient|encounter|claim|provider|member|payor|payer|diagnosis|admission|visit|dept|department|transaction)\w*(?:id|code)[\"']\s*,\s*){2,}",
        re.IGNORECASE,
    ),
}


_INLINE_ALLOW_TOKEN = "noqa: genericity"


def _iter_prod_files() -> Iterator[Path]:
    for top in PROD_DIRS:
        base = REPO_ROOT / top
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if any(part in EXCLUDED_PATH_PARTS for part in path.parts):
                continue
            yield path


def _file_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _scan(pattern: re.Pattern[str]) -> list[tuple[Path, int, str]]:
    hits: list[tuple[Path, int, str]] = []
    for path in _iter_prod_files():
        text = _file_text(path)
        if not text:
            continue
        for match in pattern.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            if line_end == -1:
                line_end = len(text)
            line = text[line_start:line_end]
            if _INLINE_ALLOW_TOKEN in line:
                continue
            line_number = text.count("\n", 0, match.start()) + 1
            hits.append((path.relative_to(REPO_ROOT), line_number, line.strip()))
    return hits


def test_no_hardcoded_workspace_paths():
    """Prod code must not name a specific workspace path (test fixtures excepted)."""
    hits = _scan(VIOLATION_PATTERNS["hardcoded_workspace_name"])
    allowed_help_text_files = {
        Path("core/medallion/build_cli.py"),
        Path("core/medallion/design_cli.py"),
        Path("core/medallion/lineage_cli.py"),
    }
    real = [
        h for h in hits
        if h[0] not in allowed_help_text_files or "help=" not in h[2]
    ]
    assert not real, (
        "Workspace-specific path found in production code:\n"
        + "\n".join(f"  {p}:{ln}  {snippet}" for p, ln, snippet in real)
        + "\n\nFix by parameterizing via WorkspaceLayout/--workspace or moving to a test fixture. "
        f"Suppress per-line with `# {_INLINE_ALLOW_TOKEN}` only when the reference is genuinely "
        "in help text or a code comment."
    )


def test_no_hardcoded_dataset_subpaths():
    hits = _scan(VIOLATION_PATTERNS["hardcoded_dataset_subpath"])
    assert not hits, (
        "Dataset-specific subpath (trendytech-hospital-*) found in production code:\n"
        + "\n".join(f"  {p}:{ln}  {snippet}" for p, ln, snippet in hits)
    )


def test_no_hardcoded_transactions_pk_duplicate_sql():
    """data_quality.py used to hardcode SELECT ... FROM transactions GROUP BY TransactionID. Never again."""
    hits = _scan(VIOLATION_PATTERNS["hardcoded_transactions_pk_sql"])
    assert not hits, (
        "Hardcoded `FROM transactions GROUP BY TransactionID` SQL found:\n"
        + "\n".join(f"  {p}:{ln}  {snippet}" for p, ln, snippet in hits)
        + "\n\nDuplicate detection must iterate profiled datasets generically."
    )


def test_no_hardcoded_domain_filter_check():
    """execution_harness.py used to hardcode `if 'medicare' in cuts ...`. Never again."""
    hits = _scan(VIOLATION_PATTERNS["hardcoded_domain_filter_check"])
    assert not hits, (
        "Hardcoded domain-specific filter check found:\n"
        + "\n".join(f"  {p}:{ln}  {snippet}" for p, ln, snippet in hits)
        + "\n\nCut-filter validation must iterate quoted tokens from the cuts string generically."
    )


def test_no_default_domain_healthcare():
    """CLI / class defaults must not assume `healthcare`."""
    hits = _scan(VIOLATION_PATTERNS["default_domain_healthcare"])
    assert not hits, (
        "default=\"healthcare\" / domain: str = \"healthcare\" found in production code:\n"
        + "\n".join(f"  {p}:{ln}  {snippet}" for p, ln, snippet in hits)
        + "\n\nChange the default to \"generic\" or remove the default and require explicit."
    )


def test_no_hardcoded_healthcare_word_literal():
    """Patterns like `if 'medicare' in cuts:` or `column == 'patient'` are hardcoded
    domain vocabulary. Use `terms_for(layout, category)` or remove the heuristic."""
    hits = _scan(VIOLATION_PATTERNS["hardcoded_healthcare_word_literal"])
    assert not hits, (
        "Hardcoded healthcare-word literal comparison found:\n"
        + "\n".join(f"  {p}:{ln}  {snippet}" for p, ln, snippet in hits)
        + "\n\nReplace with workspace-vocabulary lookup."
    )


def test_no_hardcoded_domain_join_keys():
    """Join-key preference must derive from workspace evidence (identifier roles,
    profiles), never a hardcoded tuple of domain entity ids."""
    hits = _scan(VIOLATION_PATTERNS["hardcoded_domain_join_keys"])
    assert not hits, (
        "Hardcoded domain-entity join-key list found in production code:\n"
        + "\n".join(f"  {p}:{ln}  {snippet}" for p, ln, snippet in hits)
        + "\n\nDerive preferred join keys from the model's `identifier` roles or profile evidence."
    )


def test_no_hospital_regex_in_naming_helpers():
    hits = _scan(VIOLATION_PATTERNS["hospital_regex_in_naming"])
    assert not hits, (
        "Hospital-specific regex pattern found in production code:\n"
        + "\n".join(f"  {p}:{ln}  {snippet}" for p, ln, snippet in hits)
        + "\n\nReplace with generic stem/path cleanup or move to a per-workspace config."
    )


def test_noqa_suppressions_are_visible():
    """Every `# noqa: genericity` suppression must be discoverable so they
    don't quietly accumulate. Test fails if the count exceeds a documented
    threshold — forcing review when new ones land.
    """
    suppressions: list[tuple[Path, int, str]] = []
    for path in _iter_prod_files():
        text = _file_text(path)
        if _INLINE_ALLOW_TOKEN not in text:
            continue
        for line_idx, line in enumerate(text.split("\n"), start=1):
            if _INLINE_ALLOW_TOKEN in line:
                suppressions.append((path.relative_to(REPO_ROOT), line_idx, line.strip()))
    documented_max = 0
    assert len(suppressions) <= documented_max, (
        f"{len(suppressions)} `# {_INLINE_ALLOW_TOKEN}` suppressions found; "
        f"documented max is {documented_max}. Each suppression hides a hardcoded "
        "workspace/domain reference. Fix the underlying issue or raise the threshold "
        "with justification:\n"
        + "\n".join(f"  {p}:{ln}  {snippet}" for p, ln, snippet in suppressions)
    )


def test_legacy_kpi_name_templates_gated():
    """The 13 hardcoded SQL templates in sql_generator.py must be reachable
    ONLY through `_legacy_templates_enabled()` gate."""
    path = REPO_ROOT / "core" / "onboarding" / "kpi" / "sql_generator.py"
    text = _file_text(path)
    template_match = VIOLATION_PATTERNS["hardcoded_kpi_name_template"].search(text)
    if not template_match:
        return
    method_match = re.search(
        r"def _workspace_specific_result_sql\([^)]*\)[^:]*:\n",
        text,
    )
    assert method_match, (
        "Hardcoded KPI-name templates exist but `_workspace_specific_result_sql` "
        "method definition could not be located to verify gating."
    )
    body_start = method_match.end()
    next_def = re.search(r"\n(?:    def |def |class )", text[body_start:])
    body_end = body_start + (next_def.start() if next_def else len(text) - body_start)
    body = text[body_start:body_end]
    assert "_legacy_templates_enabled" in body, (
        "Hardcoded KPI-name SQL templates are NOT gated by "
        "`_legacy_templates_enabled()`. They must default OFF and only "
        "activate via workspace_settings.legacy_kpi_name_templates=true or "
        "the AUTORESEARCH_LEGACY_KPI_TEMPLATES env var."
    )
