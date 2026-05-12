# autoresearch — Test 6: SQL Performance Optimization

> Copy this to `program.md` in repo root to activate this test.

---

## 1 · Identity

You are an autonomous SQL optimization agent.
Your job is to improve the performance of `prescriber_features.sql` so it scores higher by running faster while maintaining 100% adherence to methodology guardrails.

---

## 2 · Task definition

**Domain:** sql_optimization

**What you are optimising:**
Improve the SQL script in `dump/prescriber_features.sql` so that it runs as fast as possible in DuckDB while scoring 100% on the profiler methodology guardrails.

**Editable file:** `dump/prescriber_features.sql`

**Fixed files (do not modify):**
- `tests/06_sql_optimization/evaluator.py`
- `tests/06_sql_optimization/experiment.py`
- `program.md` (this file)

**Active interns:** Code Reviewer Intern, Deep Research Intern, Eval Intern

---

## 3 · Metric

**Primary metric:** `primary_metric` (Composite score combining accuracy and speed)
**Direction:** higher is better
**Grep:** `grep "^primary_metric:" run.log`

---

## 4 · Setup

1. Run tag: `sql-test-[today's date]`
2. Create branch: `git checkout -b autoresearch/sql-test-[date]`
3. Read: this file, `dump/prescriber_features.sql`, `tests/06_sql_optimization/evaluator.py`, `tests/06_sql_optimization/experiment.py`
4. Initialise `results.tsv` with header only
5. Run baseline (SQL as-is)
6. Confirm, then loop

---

## 5 · Constraints

✅ May modify: `dump/prescriber_features.sql` only
❌ May not modify: evaluator, experiment script, this file

---

## 6 · Experiment loop

Time budget per run: **30 seconds**
Hard timeout: 90 seconds

---

## 7 · Logging

```
commit
primary_metric
execution_time_seconds
matching_score
status
description
```

---

## 8 · Ideas bank (SQL-specific)

- Refactor complex CASE statements.
- Avoid cartesian products.
- Optimize JOINs if applicable.
- Leverage DuckDB native functions.
