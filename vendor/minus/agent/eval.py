"""Eval harness for agent edits and configs.

Small, CI-friendly utilities to assert that a MinusAnalyst config is correct
after an agent (or human) has edited the YAML. Two layers:

* :func:`validate_config` — does the project + every dashboard load cleanly?
  Returns a list of error strings (empty == healthy). Cheap pass/fail for CI.
* :class:`GoldenCase` + :func:`run_golden` — declarative "golden" expectations
  about a config (e.g. "overview has 4 KPI widgets", "every measure resolves").
  Loads the config once, runs each check, reports pass/fail counts + failures.

The built-in checks (:func:`check_all_measures_exist`,
:func:`check_all_fields_resolve`, :func:`check_no_duplicate_ids`) re-express the
loader's semantic checks independently so they are usable as golden cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from minus.config.loader import ConfigError, load_dashboards, load_project
from minus.config.models import Dashboard, Project

# A check receives the loaded project + pages and returns True (pass) or a
# string describing the failure.
CheckFn = Callable[[Project, list[Dashboard]], "bool | str"]


# ---------------------------------------------------------------------------
# Cheap validation
# ---------------------------------------------------------------------------


def validate_config(root: Path | str) -> list[str]:
    """Load the project and all dashboards; return error messages.

    Returns ``[]`` when everything loads and cross-checks cleanly. Any
    :class:`ConfigError` (bad YAML, unknown measure/field/table, duplicate id)
    is caught and returned as a one-element list, so callers can branch on
    truthiness without try/except.
    """
    try:
        project = load_project(root)
        load_dashboards(root, project)
    except ConfigError as exc:
        return [str(exc)]
    return []


# ---------------------------------------------------------------------------
# Golden cases
# ---------------------------------------------------------------------------


@dataclass
class GoldenCase:
    """A named expectation about a config."""

    name: str
    check: CheckFn


@dataclass
class Report:
    """Result of running a set of golden cases."""

    passed: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        lines = [f"{self.passed}/{self.total} golden cases passed."]
        for name, why in self.failures:
            lines.append(f"  FAIL {name}: {why}")
        return "\n".join(lines)


def run_golden(cases: list[GoldenCase], root: Path | str) -> Report:
    """Load the config once, run each golden case, collect pass/fail.

    If the config itself fails to load, every case is reported as failed with
    the load error (there is nothing to check against).
    """
    report = Report()
    try:
        project = load_project(root)
        pages = load_dashboards(root, project)
    except ConfigError as exc:
        for case in cases:
            report.failed += 1
            report.failures.append((case.name, f"config did not load: {exc}"))
        return report

    for case in cases:
        try:
            result = case.check(project, pages)
        except Exception as exc:  # a buggy check shouldn't crash the run
            result = f"check raised {type(exc).__name__}: {exc}"
        if result is True:
            report.passed += 1
        else:
            why = result if isinstance(result, str) else "check returned False"
            report.failed += 1
            report.failures.append((case.name, why))
    return report


# ---------------------------------------------------------------------------
# Built-in checks (usable directly as GoldenCase.check)
# ---------------------------------------------------------------------------


def _iter_widget_measures(pages: list[Dashboard]):
    for page in pages:
        for w in page.widgets:
            refs = ([w.measure] if w.measure else []) + list(w.measures)
            for mref in refs:
                yield page, w, mref


def _iter_field_refs(project: Project, pages: list[Dashboard]):
    """Yield (where, ref) for every dotted 'table.column' reference."""
    for m in project.measures:
        if m.field:
            yield f"measure {m.name!r}.field", m.field
        if m.date_field:
            yield f"measure {m.name!r}.date_field", m.date_field
    for page in pages:
        for f in page.filters:
            yield f"page {page.id!r} filter {f.id!r}.field", f.field
        for w in page.widgets:
            for attr in ("dimension", "breakdown", "compare"):
                ref = getattr(w, attr)
                if ref:
                    yield f"page {page.id!r} widget {w.id!r}.{attr}", ref


def check_all_measures_exist(project: Project, pages: list[Dashboard]) -> bool | str:
    """Every measure referenced by a widget must be defined in the project."""
    known = {m.name for m in project.measures}
    bad: list[str] = []
    for page, w, mref in _iter_widget_measures(pages):
        if mref not in known:
            bad.append(f"page {page.id!r} widget {w.id!r} -> unknown measure {mref!r}")
    return True if not bad else "; ".join(bad)


def check_all_fields_resolve(project: Project, pages: list[Dashboard]) -> bool | str:
    """Every dotted field ref must be 'table.column' with a known table."""
    tables = {t.name for t in project.tables}
    bad: list[str] = []
    for where, ref in _iter_field_refs(project, pages):
        if "." not in ref:
            bad.append(f"{where}: {ref!r} is not 'table.column'")
        elif ref.split(".", 1)[0] not in tables:
            bad.append(f"{where}: unknown table in {ref!r}")
    return True if not bad else "; ".join(bad)


def check_no_duplicate_ids(project: Project, pages: list[Dashboard]) -> bool | str:
    """Page ids unique across the project; widget/filter ids unique per page."""
    bad: list[str] = []

    seen_pages: set[str] = set()
    for page in pages:
        if page.id in seen_pages:
            bad.append(f"duplicate page id {page.id!r}")
        seen_pages.add(page.id)

        seen_w: set[str] = set()
        for w in page.widgets:
            if w.id in seen_w:
                bad.append(f"page {page.id!r} duplicate widget id {w.id!r}")
            seen_w.add(w.id)

        seen_f: set[str] = set()
        for f in page.filters:
            if f.id in seen_f:
                bad.append(f"page {page.id!r} duplicate filter id {f.id!r}")
            seen_f.add(f.id)

    return True if not bad else "; ".join(bad)


def builtin_cases() -> list[GoldenCase]:
    """The three built-in checks wrapped as ready-to-run golden cases."""
    return [
        GoldenCase("all_measures_exist", check_all_measures_exist),
        GoldenCase("all_fields_resolve", check_all_fields_resolve),
        GoldenCase("no_duplicate_ids", check_no_duplicate_ids),
    ]
