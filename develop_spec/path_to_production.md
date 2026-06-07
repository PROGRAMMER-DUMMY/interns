# develop_spec/path_to_production.md — dev -> production gates

"Production" here = **the platform/product code being release-ready** (ready to
commit/merge), not a workspace going live. Dev mode = us iterating on `core/`,
`tools/`, `tests/`. A change graduates only when every gate below is green.

## Promotion gates (all must pass)

1. **Generic.** No workspace/domain specifics baked into logic; the genericity
   guard test passes and a grep of the diff is clean.
2. **Gate clean.** `green-gate` passes on the venv interpreter.
3. **Tests green.** All directly-affected unittest suites (and adjacent ones)
   pass via `.venv\Scripts\python.exe -m unittest`. New behavior has a test; the
   fixed failure has a guard test.
4. **No hand-edited generated contracts.** Any change to `interns/generated/**`
   came from re-running a generator, not manual edits.
5. **Provenance + gates intact.** Human gates recorded as `source: human` with
   `--confirmed-by`; no fabricated results.
6. **Docs/specs in sync.** `develop_spec/changelog.md` has a dated entry and
   `develop_spec/follow_ups.md` is updated. If behavior or commands changed,
   the relevant operating doc (AGENTS.md / README.md / TOOLS.md) is updated too.

## Pre-merge checklist (copy into the commit/PR notes)

```text
[ ] generic (guard + diff grep)
[ ] green-gate clean
[ ] affected + adjacent unittest suites green (.venv)
[ ] no generated contract hand-edited
[ ] human gates have source/confirmed-by; no fabricated output
[ ] changelog.md entry added; follow_ups.md updated
[ ] operating docs updated if commands/behavior changed
```

## Staging

Explicit paths only (never `git add -A`). Exclusions per
`docs/repo_hygiene.md` and `guidelines.md` (secrets, state, workspace interns,
raw datasets, scratch probes). Commit/push only when the maintainer asks.
