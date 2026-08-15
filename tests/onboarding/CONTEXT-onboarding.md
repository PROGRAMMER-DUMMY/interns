# CONTEXT-onboarding.md — `tests/onboarding/`

## Executive Overview & Architectural Model

Tests for `core/onboarding/` subsystems that need their own directory because they carry
fixtures or a boundary contract, rather than living flat in `tests/`.

The organising rule here is **no external dependency at test time**. Onboarding reaches
out to heavy or absent things — document parsers, model downloads, Databricks — so each
test injects the boundary (a `runner=` callable, a fake client) instead of touching the
real one. A test that needs a 2GB model download is a test that gets skipped, and a
skipped test guards nothing.

## File Details

### 1. [`test_docling_loader.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/onboarding/test_docling_loader.py)

- **Exact Purpose**: Contract tests for the isolated Docling extraction boundary — [`core/onboarding/documents/docling_loader.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/docling_loader.py) (host side) and [`docling_runner.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/docling_runner.py) (isolated-env side).
- **Key Cases**:
  - Preflight: missing env reports the platform-correct install command; interpreter present but `import docling` failing; version reporting; interpreter that cannot execute.
  - Isolation: every test pins `root=` to a temp dir, so a developer who really has `.venv_docling` on disk cannot flip the results.
  - Fallback: unavailable engine, conversion failure, no payload written, and timeout all return `fallback_recommended=True` rather than raising. A missing **input file** deliberately does not (caller error, not engine absence).
  - API drift: `export_to_dataframe(doc=...)` with a fallback to the legacy no-kwarg signature.
- **Inputs & Outputs**:
  - *Inputs*: An injected `runner` standing in for `subprocess.run`; temp-dir fixtures.
  - *Outputs*: Assertions only. No files outside the temp dir, no network, no model download.
- **Failure Modes & Edge Cases**:
  - The fake runner must fulfil the real contract — write JSON to the path following `--out` — or the happy-path test passes against a fiction.
  - `AUTORESEARCH_DOCLING_PYTHON` is cleared per test; leaking it across tests would make results depend on the developer's shell.

## Invariants

- No test in this directory may require `docling`, `torch`, network access, or Databricks credentials.
- The primary `.venv` stays free of docling/torch; see [`CONTEXT-documents.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/CONTEXT-documents.md) §7 for why.
