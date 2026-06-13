# core/ Full-Read Audit

> **New session? Read `HANDOFF.md` first**, then `SUMMARY.md` (themes), then
> `REMEDIATION_PLAN.md` (phased fix plan with `file:line` + progress ledger).


Section-by-section deep read of `core/` (227 files, ~86.9k lines). Each package is read
**in full** by a dedicated agent in its own context; findings land in one file per unit here.

## Finding tags
- `[BUG]` — incorrect behavior, crash, logic error, wrong result
- `[INTEGRATION]` — feature exists but isn't wired up / two parts don't connect
- `[NOT-PROD]` — works but not production-ready (no error handling, hardcoded, race, perf)
- `[MISSING]` — stub / incomplete / referenced-but-absent / feature gap
- `[DEAD]` — unused / unreachable / no longer needed
- `[DUP]` — duplicated logic that should be shared

## Units & status

| Unit | Package(s) | Files | ~Lines | Status | Handoff |
| --- | --- | --- | --- | --- | --- |
| governance | core/governance | 10 | 2018 | **done** — BUG:2 INT:2 NP:2 MISS:2 DUP:1 | `governance.md` |
| storage | core/storage | 6 | 1467 | **done** — BUG:4 NP:4 DEAD:2 INT:2 MISS:2 DUP:1 | `storage.md` |
| agents | core/agents | 6 | 859 | **done** — BUG:3 INT:2 NP:5 DEAD:1 DUP:2 MISS:1 | `agents.md` |
| optimization | core/optimization | 6 | 730 | **done** — BUG:2 NP:4 INT:1 MISS:1 DUP:1 | `optimization.md` |
| orchestration | core/orchestration | 4 | 1039 | **done** — BUG:2 INT:2 NP:5 MISS:1 DEAD:2 DUP:1 | `orchestration.md` |
| execution | core/execution | 3 | 909 | pending | `execution.md` |
| context | core/context | 5 | 1060 | **done** — BUG:2 INT:1 NP:2 DEAD:1 DUP:1 MISS:1 | `context.md` |
| wiki | core/wiki | 6 | 928 | **done** — BUG:5 INT:1 NP:2 MISS:2 DEAD:1 DUP:2 | `wiki.md` |
| dashboard | core/dashboard | 9 | 3773 | **done** — BUG:6 NP:8 DEAD:2 MISS:1 | `dashboard.md` |
| medallion-A | core/medallion (design/build/schema) | ~16 | ~3600 | **done** — BUG:9 NP:6 DEAD:2 INT:1 DUP:1 | `medallion-a.md` |
| medallion-B | core/medallion (lineage/emit/deploy) | ~15 | ~3000 | **done** — BUG:7 NP:4 DEAD:1 DUP:1 INT:1 MISS:1 | `medallion-b.md` |
| execution | core/execution | 3 | 909 | **done** — BUG:4 NP:7 MISS:1 DEAD:1 DUP:2 | `execution.md` |
| small-combo | contracts+dev+resource+presentation+profiling+skills+observability | 23 | ~3500 | **done** — BUG:2 INT:2 NP:6 MISS:1 DEAD:3 DUP:1 | `small-combo.md` |
| onboarding-root | core/onboarding/*.py | 12 | ~3800 | **done** — BUG:5 NP:3 INT:2 MISS:1 DEAD:1 DUP:1 | `onboarding-root.md` |
| ob-kpi-A | onboarding/kpi (intent/resolve/derive) | ~10 | ~7000 | **done** — BUG:10 INT:1 NP:1 DUP:1 DEAD:1 MISS:1 | `ob-kpi-a.md` |
| ob-kpi-B | onboarding/kpi (engines/parity/exec) | ~8 | ~4000 | **done** — BUG:9 NP:2 INT:1 MISS:1 DUP:1 | `ob-kpi-b.md` |
| ob-kpi-C | onboarding/kpi (panels/blockers) | ~7 | ~5000 | **done** — BUG:4 NP:3 MISS:2 INT:2 DUP:1 DEAD:1 | `ob-kpi-c.md` |
| ob-kpi-D | onboarding/kpi (results/proof/generation) | ~9 | ~5000 | **done** — BUG:7 NP:3 INT:2 DUP:2 | `ob-kpi-d.md` |
| ob-workspace-A | onboarding/workspace/flow.py | 1 | 4068 | **done** — BUG:3 INT:3 NP:4 MISS:1 DEAD:2 DUP:2 | `ob-workspace-a.md` |
| ob-workspace-B | onboarding/workspace (onboarding/validation/bootstrap) | ~4 | ~4500 | **done** — BUG:4 NP:7 MISS:2 DUP:1 | `ob-workspace-b.md` |
| ob-workspace-C | onboarding/workspace (runner/idempotency/misc) | ~10 | ~3000 | **done** — BUG:3 INT:3 NP:2 DUP:2 DEAD:2 MISS:1 | `ob-workspace-c.md` |
| ob-harness | onboarding/harness | 11 | ~5000 | **done** — BUG:6 NP:5 INT:2 DUP:1 DEAD:1 | `ob-harness.md` |
| ob-data_model | onboarding/data_model | 5 | ~5000 | **done** — INT:2 NP:4 BUG:2 DUP:1 DEAD:1 | `ob-data_model.md` |
| ob-relationships | onboarding/relationships | 4 | ~3500 | **done** — BUG:7 INT:4 MISS:2 DUP:1 NP:1 | `ob-relationships.md` |
| ob-documents | onboarding/documents | 5 | ~2500 | **done** — BUG:4 NP:5 INT:2 MISS:1 DUP:1 | `ob-documents.md` |
| ob-databricks | onboarding/databricks | 4 | ~2500 | **done** — BUG:2 NP:3 DEAD:1 MISS:1 INT:1 | `ob-databricks.md` |
| ob-sources | onboarding/sources | 4 | ~3000 | **done** — BUG:11 NP:3 INT:1 DUP:1 DEAD:1 MISS:1 | `ob-sources.md` |
| ob-features | onboarding/features | 6 | ~2000 | **done** — BUG:3 NP:5 MISS:1 INT:1 DEAD:2 DUP:2 (eval-safe) | `ob-features.md` |
| ob-memory | onboarding/memory | 5 | ~1500 | **done** — BUG:4 INT:1 NP:1 DUP:1 DEAD:1 MISS:1 | `ob-memory.md` |
| ob-lexicon-benchmark | onboarding/lexicon + benchmark | 5 | ~1500 | **done** — BUG:1 NP:6 INT:1 MISS:1 | `ob-lexicon-benchmark.md` |

Update the Status column as units complete. Aggregate findings roll up into `SUMMARY.md`.
