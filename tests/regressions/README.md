# `tests/regressions/` — per-phase regression tests for the core/ remediation

Every BUG fixed in the `core/` remediation (`docs/core_audit/REMEDIATION_PLAN.md`) ships a
regression test here that **fails before the fix and passes after**.

## Naming convention
```
test_core_p<N>_<slug>.py
```
- `<N>` = the remediation phase (1-8) the fix belongs to.
- `<slug>` = short kebab/snake describing the defect class.

Examples:
- `test_core_p1_pii.py` — PII/PHI masking parity, packet redaction, sensitivity-field unification.
- `test_core_p2_gates.py` — Genie-lane gate, remote-target gate, external-root allowlist, SSRF egress.
- `test_core_p3_injection.py` — escaping/parameterization of emitted SQL/PySpark/Delta.
- `test_core_p4_concurrency.py` — atomic writes, locks, no `os.chdir`, SQLite WAL.
- `test_core_p5_correctness.py` — parity coverage, substring->token matching, success-enum, etc.

## Rules
- One assertion cluster per BUG; name the test after the symptom it locks down.
- No domain words (workspace-agnostic). Use synthetic fixtures, not Healthcare-RCM specifics.
- Local-safe: no remote calls without `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`.
- ASCII status markers only in any generated text the test inspects (`[ok] [~] [x] [blocked]`).

Discovery: `tests/` has no `__init__.py`; pytest finds `tests/regressions/test_*.py` automatically.
