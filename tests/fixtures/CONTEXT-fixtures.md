# Tests Fixtures Context: `tests/fixtures`

This document provides an exhaustive reference for all components in `tests/fixtures`.

---

## Executive Overview & Architectural Model

`tests/fixtures` stores reference JSON artifacts and baseline performance snapshots used across unit tests and accuracy benchmark suites.

---

## File Details

### 1. [`resolver_accuracy_baseline.json`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/fixtures/resolver_accuracy_baseline.json)

- **Exact Purpose**: Baseline evaluation snapshot for feature resolver accuracy tests (`resolver-accuracy --write-baseline`).
- **Key Sections**:
  - `total`, `correct`, `wrong`, `abstained`, `precision`, `answer_rate` ([lines 5-10](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/fixtures/resolver_accuracy_baseline.json#L5-L10)): Stores baseline metrics (5 total, 5 correct, 0 wrong, 1.0 precision, 1.0 answer rate).
- **Inputs & Outputs**:
  - *Inputs*: Read by `resolver-accuracy` evaluation runner.
  - *Outputs*: Regression threshold verification.
- **Failure Modes & Edge Cases**:
  - Evaluators fail if `wrong` increases above 0 or `correct` decreases below 5.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None.
- 🔌 **Unwired Components**: None.
- 👯 **Logic & Code Duplication**: None.
- ⚠️ **Broken References & Mismatches**: None.
