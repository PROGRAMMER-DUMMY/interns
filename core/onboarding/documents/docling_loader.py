"""Docling document extraction -- host side, zero-pollution by construction.

Docling drags in ``torch`` and a large model stack. This repo's primary ``.venv`` pins
``pyspark<4``, ``deltalake`` and ``numpy<2.0``; installing docling into it risks
resolving those pins out from under the engine-parity and Delta paths. So docling is
NEVER imported in-process. It runs in a separate interpreter, and only JSON crosses the
boundary.

Layout::

    docling_loader.py   <- this module (primary .venv). Never imports docling.
    docling_runner.py   <- executed BY PATH inside the isolated env. Imports docling.

Interpreter resolution, first match wins:

1. ``$AUTORESEARCH_DOCLING_PYTHON`` -- explicit override (CI, or a shared install).
2. ``<repo>/.venv_docling`` -- the conventional local isolated env.
3. Unavailable -> :func:`can_parse_with_docling` reports the exact setup command and
   :func:`parse_document` returns ``fallback_recommended=True`` so the caller keeps
   using the existing opendataloader-pdf path rather than failing the run.

Preflight (also runnable directly)::

    .venv/Scripts/python.exe core/onboarding/documents/docling_loader.py
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_OVERRIDE = "AUTORESEARCH_DOCLING_PYTHON"
VENV_DIR = ".venv_docling"

# Docling downloads layout/OCR models on first real conversion; a short timeout here
# reads as a spurious failure. Preflight is a bare import, so it stays tight.
DEFAULT_TIMEOUT_S = 900
PREFLIGHT_TIMEOUT_S = 60

_RUNNER = Path(__file__).with_name("docling_runner.py")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def install_command(root: Path | None = None) -> str:
    """Platform-correct setup for the isolated env."""
    root = root or repo_root()
    interpreter = _venv_python(Path(VENV_DIR))
    return f"uv venv {VENV_DIR} && uv pip install --python {interpreter.as_posix()} docling"


@dataclass
class DoclingPreflight:
    available: bool
    python_path: str | None = None
    version: str | None = None
    reason: str | None = None
    next_step: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "python_path": self.python_path,
            "version": self.version,
            "reason": self.reason,
            "next_step": self.next_step,
        }


@dataclass
class DoclingResult:
    ok: bool
    engine: str = "docling"
    markdown: str = ""
    tables: list[dict[str, Any]] = field(default_factory=list)
    source_file: str | None = None
    engine_version: str | None = None
    reason: str | None = None
    next_step: str | None = None
    fallback_recommended: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "source_file": self.source_file,
            "table_count": len(self.tables),
            "markdown_chars": len(self.markdown),
            "reason": self.reason,
            "next_step": self.next_step,
            "fallback_recommended": self.fallback_recommended,
        }


def resolve_docling_python(root: Path | None = None) -> Path | None:
    """Locate the isolated interpreter without executing it."""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        candidate = Path(override)
        return candidate if candidate.exists() else None
    candidate = _venv_python((root or repo_root()) / VENV_DIR)
    return candidate if candidate.exists() else None


def can_parse_with_docling(
    root: Path | None = None, *, runner: Any = subprocess.run
) -> DoclingPreflight:
    """Report whether docling is usable, and exactly how to fix it if not."""
    root = root or repo_root()
    python_path = resolve_docling_python(root)
    if python_path is None:
        return DoclingPreflight(
            available=False,
            reason=(
                f"No isolated docling environment found. Looked at ${ENV_OVERRIDE} and "
                f"{VENV_DIR}/. Docling is intentionally NOT installed in the primary "
                ".venv (it would pull torch alongside pinned pyspark<4 / numpy<2.0)."
            ),
            next_step=install_command(root),
        )

    probe = "import docling, importlib.metadata as m; print(m.version('docling'))"
    try:
        completed = runner(
            [str(python_path), "-c", probe],
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_S,
        )
    except Exception as exc:
        return DoclingPreflight(
            available=False,
            python_path=str(python_path),
            reason=f"Could not run the isolated interpreter: {type(exc).__name__}: {exc}",
            next_step=install_command(root),
        )

    if completed.returncode != 0:
        return DoclingPreflight(
            available=False,
            python_path=str(python_path),
            reason=(
                f"Interpreter at {python_path} exists but `import docling` failed "
                f"(exit {completed.returncode})."
            ),
            next_step=install_command(root),
        )
    return DoclingPreflight(
        available=True,
        python_path=str(python_path),
        version=(completed.stdout or "").strip() or None,
    )


def parse_document(
    input_path: str | Path,
    *,
    root: Path | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    runner: Any = subprocess.run,
) -> DoclingResult:
    """Convert a document via docling in its isolated env.

    Never raises for an unavailable/failing engine -- returns ``ok=False`` with
    ``fallback_recommended=True`` so the caller can keep using the existing
    opendataloader-pdf text/table path instead of failing the run.
    """
    root = root or repo_root()
    input_path = Path(input_path)
    if not input_path.exists():
        return DoclingResult(
            ok=False,
            source_file=str(input_path),
            reason=f"Input file not found: {input_path}",
            next_step="Check the path and re-run.",
        )

    preflight = can_parse_with_docling(root, runner=runner)
    if not preflight.available:
        return DoclingResult(
            ok=False,
            source_file=str(input_path),
            reason=preflight.reason,
            next_step=preflight.next_step,
            fallback_recommended=True,
        )

    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "docling.json"
        try:
            completed = runner(
                [
                    str(preflight.python_path),
                    str(_RUNNER),
                    str(input_path),
                    "--out",
                    str(out_path),
                ],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except Exception as exc:
            return DoclingResult(
                ok=False,
                source_file=str(input_path),
                reason=f"docling runner failed to start: {type(exc).__name__}: {exc}",
                next_step=install_command(root),
                fallback_recommended=True,
            )

        try:
            payload = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            return DoclingResult(
                ok=False,
                source_file=str(input_path),
                reason=(
                    f"docling runner wrote no readable payload "
                    f"(exit {getattr(completed, 'returncode', '?')})."
                ),
                next_step="Re-run with the isolated interpreter directly to see its output.",
                fallback_recommended=True,
            )

    if not payload.get("ok"):
        return DoclingResult(
            ok=False,
            source_file=str(input_path),
            reason=payload.get("reason") or "docling conversion failed.",
            next_step="Inspect the document; docling could not parse it.",
            fallback_recommended=True,
        )

    return DoclingResult(
        ok=True,
        markdown=payload.get("markdown") or "",
        tables=payload.get("tables") or [],
        source_file=payload.get("source_file") or str(input_path),
        engine_version=payload.get("engine_version"),
    )


def main() -> int:
    """Preflight entry point: report docling availability as JSON."""
    preflight = can_parse_with_docling()
    print(json.dumps(preflight.summary(), indent=2))
    return 0 if preflight.available else 1


if __name__ == "__main__":
    raise SystemExit(main())
