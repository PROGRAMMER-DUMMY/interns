---
name: green-gate
description: >
  Run the project's portable green gate -- the curated CI suite plus the
  enterprise suite, the same way ci.yml does -- and report pass/fail with any
  failures. Use before claiming work is done, before commit, or when the user
  asks to "run the tests", "check it's green", or "run the green gate". With a
  sweep, also classify broader blast-radius failures as new vs. known-baseline.
---

# Green Gate

The single, CLI-agnostic command that decides whether the tree is green.

## How to run

The logic lives in a repo console script, so any CLI just invokes it:

```powershell
green-gate            # curated + enterprise suites (the strict gate)
green-gate --sweep    # also sweep resolver/pipeline modules; flag NEW vs known failures
green-gate --json     # machine-readable summary
```

If the `green-gate` entry point is not on PATH, call it via the venv interpreter:

```powershell
.venv\Scripts\python.exe -m core.dev.green_gate --sweep
```

## Hard rule: never use `uv run` for tests

`uv run` resyncs and reinstalls pre-release pyspark 4.1.1 (no Delta) and breaks the
pyspark-backed tests. Always use the venv interpreter / the installed `green-gate`
script. The `green-gate` console script is wired to the venv interpreter by install.

## How to read the result

- `[ok] all green` and exit 0 -> the gate passed; safe to claim done / commit.
- `[x]` against the gate -> a curated/enterprise test failed. This is a hard stop;
  do not commit. Fix or quarantine before proceeding.
- `--sweep` separates `REGRESSION` (a failure your change introduced) from `[~] known`
  (a pre-existing baseline failure recorded in `core/dev/green_gate.py::KNOWN_BASELINE`).
  Only regressions block; report knowns but do not treat them as your fault.

## When a sweep finds a new known-good baseline

If a previously-failing baseline test is fixed, remove its id from `KNOWN_BASELINE`
so the gate starts protecting it. Never add a green test to the baseline to silence it.

Output uses ASCII markers ([ok]/[x]/[~]) only -- no emojis (repo rule).
