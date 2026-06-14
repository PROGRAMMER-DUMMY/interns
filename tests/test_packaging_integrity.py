"""Packaging integrity regression tests.

Guards against the class of bug where pyproject.toml [project.scripts] declares
a console entry point that imports a package not listed in
[tool.hatch.build.targets.wheel].packages, meaning the script would be broken
in a built wheel.  Also asserts every entry-point module is importable and
carries the named callable, and every packaged top-level directory is importable.
"""
from __future__ import annotations

import importlib
import sys
import tomllib
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate pyproject.toml from this test file's location (tests/ -> repo root).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    with open(_PYPROJECT, "rb") as fh:
        return tomllib.load(fh)


def _parse_scripts(data: dict) -> dict[str, tuple[str, str]]:
    """Return {script_name: (dotted_module, callable_name)} for every entry point."""
    scripts = data.get("project", {}).get("scripts", {})
    result: dict[str, tuple[str, str]] = {}
    for name, target in scripts.items():
        if ":" not in target:
            raise ValueError(f"Entry point {name!r} has no ':' separator: {target!r}")
        module_path, func = target.split(":", 1)
        result[name] = (module_path.strip(), func.strip())
    return result


def _wheel_packages(data: dict) -> list[str]:
    return (
        data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages", [])
    )


class TestPackagingIntegrity(unittest.TestCase):
    """Three packaging-integrity checks derived from pyproject.toml."""

    @classmethod
    def setUpClass(cls) -> None:
        data = _load_pyproject()
        cls.scripts = _parse_scripts(data)           # {name: (module, func)}
        cls.wheel_pkgs = _wheel_packages(data)       # ["core", "interns", "tools"]

    # ------------------------------------------------------------------
    # Test 1: every entry-point's top-level package is in wheel packages
    # ------------------------------------------------------------------
    def test_entry_point_packages_shipped_in_wheel(self) -> None:
        """Every script target's top-level package must appear in wheel packages.

        This is the test that would have caught the original bug: tools.* scripts
        were declared but 'tools' was absent from [tool.hatch.build.targets.wheel].packages.
        """
        missing: list[str] = []
        for script_name, (module_path, _func) in self.scripts.items():
            top_level_pkg = module_path.split(".")[0]
            if top_level_pkg not in self.wheel_pkgs:
                missing.append(
                    f"  script {script_name!r} -> module {module_path!r} "
                    f"(top-level package {top_level_pkg!r} not in wheel packages)"
                )

        if missing:
            self.fail(
                "The following entry points reference a package that is NOT listed in "
                "[tool.hatch.build.targets.wheel].packages — these scripts would be "
                "broken in a built wheel:\n" + "\n".join(missing)
            )

    # ------------------------------------------------------------------
    # Test 2: every entry-point module is importable and callable exists
    # ------------------------------------------------------------------
    def test_entry_point_modules_importable_and_callable(self) -> None:
        """Every script module must import cleanly and expose the named callable.

        Collects ALL failures so one test run reveals every broken entry point.
        Only narrow, well-justified ImportError skips are permitted — see inline
        comments.
        """
        # Ensure the repo root is on sys.path so local packages resolve without
        # a full editable install (pytest typically adds it; this is belt-and-suspenders).
        repo_root_str = str(_REPO_ROOT)
        if repo_root_str not in sys.path:
            sys.path.insert(0, repo_root_str)

        failures: list[str] = []

        for script_name, (module_path, func_name) in self.scripts.items():
            try:
                mod = importlib.import_module(module_path)
            except ImportError as exc:
                # PySpark has a mandatory JVM dependency.  If JAVA_HOME is absent
                # or an incompatible JDK is present the import raises.  This is a
                # known-optional runtime dep (the project documents it in MEMORY.md
                # under "PySpark local-run prereqs") and the test harness env-skips
                # Spark. Only skip when the ImportError chain traces through pyspark;
                # any other ImportError is a real packaging failure.
                exc_str = str(exc).lower()
                if "pyspark" in exc_str or "java" in exc_str or "py4j" in exc_str:
                    # Re-attempt: check whether the module itself is pyspark or a
                    # module that directly imports pyspark at the top level.
                    # We only skip when PySpark's absence is the root cause.
                    pass  # fall through to chain inspection below
                else:
                    failures.append(
                        f"  [{script_name}] import {module_path!r} failed: {exc}"
                    )
                    continue

                # Walk the exception chain to find the originating import.
                root_exc: BaseException | None = exc
                while root_exc.__cause__ is not None:
                    root_exc = root_exc.__cause__
                root_msg = str(root_exc).lower()
                if "pyspark" in root_msg or "java" in root_msg or "py4j" in root_msg:
                    # Legitimately optional — skip with an explanatory note so the
                    # output remains auditable.
                    print(
                        f"  [SKIP] {script_name!r} ({module_path}): "
                        f"PySpark/JVM not available in this env — {exc}"
                    )
                    continue
                else:
                    failures.append(
                        f"  [{script_name}] import {module_path!r} failed: {exc}"
                    )
                    continue

            # Module imported — now verify the callable.
            if not hasattr(mod, func_name):
                failures.append(
                    f"  [{script_name}] module {module_path!r} has no attribute "
                    f"{func_name!r}"
                )
            elif not callable(getattr(mod, func_name)):
                failures.append(
                    f"  [{script_name}] {module_path}:{func_name} exists but is "
                    f"not callable"
                )

        if failures:
            self.fail(
                "The following entry points have import or callable failures:\n"
                + "\n".join(failures)
            )

    # ------------------------------------------------------------------
    # Test 3: every top-level package in wheel packages exists on disk
    # ------------------------------------------------------------------
    def test_wheel_packages_exist_on_disk(self) -> None:
        """Every package listed in wheel packages must exist as an importable package.

        Guards the inverse: a packaged directory that lost its __init__.py would
        cause the wheel to ship a broken namespace package.
        """
        missing_init: list[str] = []
        missing_dir: list[str] = []

        repo_root_str = str(_REPO_ROOT)
        if repo_root_str not in sys.path:
            sys.path.insert(0, repo_root_str)

        for pkg in self.wheel_pkgs:
            pkg_dir = _REPO_ROOT / pkg
            if not pkg_dir.is_dir():
                missing_dir.append(
                    f"  {pkg!r}: directory {pkg_dir} does not exist"
                )
                continue
            init_file = pkg_dir / "__init__.py"
            if not init_file.is_file():
                # Also accept importability as a namespace package (rare but valid).
                try:
                    importlib.import_module(pkg)
                except ImportError:
                    missing_init.append(
                        f"  {pkg!r}: no __init__.py at {init_file} and not "
                        f"importable as a namespace package"
                    )

        problems = missing_dir + missing_init
        if problems:
            self.fail(
                "The following wheel packages are broken on disk:\n"
                + "\n".join(problems)
            )


if __name__ == "__main__":
    unittest.main()
