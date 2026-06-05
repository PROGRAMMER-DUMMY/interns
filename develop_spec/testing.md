# develop_spec/testing.md — tests + the gate

## Hard rule: tests run on the venv interpreter, NOT `uv run`

A pre-tool hook (`.claude/hooks/guard_uv_run.py`) blocks `uv run` for tests,
pyspark, and engine generation, because `uv run` reinstalls a pre-release
pyspark (no Delta) and breaks them. Use the project venv interpreter:

```powershell
.venv\Scripts\python.exe -m unittest tests.<module> [tests.<module> ...]
```

Tests are **unittest** modules (there is no `pytest` in the venv). Examples:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_metric_derivation
.venv\Scripts\python.exe -m unittest tests.test_workspace_flow tests.test_kpi_nl_chain_e2e
```

(From the Bash tool on this box, the working invocation is
`.venv/Scripts/python.exe -m unittest tests.<module>` with forward slashes.)

## The gate

`green-gate` is the portable gate, installed against the venv interpreter. Run it
before considering a change done:

```powershell
green-gate
```

Engine generation / pyspark also use the venv interpreter directly, e.g.:

```powershell
.venv\Scripts\python.exe -m core.onboarding.kpi.generate_kpi_engines ...
```

## What to test when you change platform logic

- Add or extend a unittest in `tests/` covering the new behavior AND a guard for
  the failure it fixes.
- Keep fixtures generic / non-domain (orders/shipments/customers) — see the
  genericity guard in `guidelines.md`.
- Run the directly-affected suites plus adjacent ones (resolver, panel, flow,
  derivation) before claiming green.

## Pre-existing failures (do not attribute to your change)

Confirm a failure is yours by stashing and re-running. Known pre-existing
failures as of the latest changelog entry:

- `tests.test_kpi_pipeline_wrapper` relationship-gate assertion.
- `tests.test_kpi_proof_packet` `data_engineering_evidence` KeyError.

If a suite fails, `git stash` your changes and re-run that one test; if it still
fails, it is pre-existing — note it, don't "fix" it by accident. Update this list
when the set changes.
