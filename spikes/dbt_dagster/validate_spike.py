"""Offline validation harness for the dbt spike (no dbt / dagster needed).

dbt-core and dagster are NOT installed in this repo (adding them is part of the
adopt-or-not decision this spike informs). So instead of `dbt build`, this
harness does a *lightweight compile* of the dbt models -- stripping
``{{ config(...) }}``, resolving ``{{ source('raw', X) }}`` -> delta_scan of
bronze/X and ``{{ ref('Y') }}`` -> the view Y -- then materializes them in
DuckDB in dependency order and proves the three things that matter:

  1. PARITY     : fct_kpi_001 / fct_kpi_003 reproduce the platform's GOLD totals
                  and row counts (the dq.py gold-reconciliation rule).
  2. GOVERNANCE : no PII column reaches any mart (the pii_redaction rule).
  3. SHARE      : kpi_002 single-attribution shares sum to ~100%.

This IS the evidence behind FINDINGS.md. Run:
    .venv/Scripts/python.exe spikes/dbt_dagster/validate_spike.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MEDALLION = (REPO / "workspaces" / "Healthcare-RCM-Data-Platform"
             / "interns" / "state" / "medallion").resolve()
BRONZE = MEDALLION / "bronze"
GOLD = MEDALLION / "gold"

# Materialization order: staging -> intermediate -> marts.
MODELS = [
    ("stg_transactions",        "models/staging/stg_transactions.sql"),
    ("stg_patients",            "models/staging/stg_patients.sql"),
    ("stg_departments",         "models/staging/stg_departments.sql"),
    ("int_claims_enriched",     "models/intermediate/int_claims_enriched.sql"),
    ("fct_kpi_001_paid_trend",  "models/marts/fct_kpi_001_paid_trend.sql"),
    ("fct_kpi_002_lives_share", "models/marts/fct_kpi_002_lives_share.sql"),
    ("fct_kpi_003_top_payers",  "models/marts/fct_kpi_003_top_payers.sql"),
]

_CONFIG = re.compile(r"\{\{\s*config\([^}]*\)\s*\}\}")
_SOURCE = re.compile(r"\{\{\s*source\(\s*'raw'\s*,\s*'(\w+)'\s*\)\s*\}\}")
_REF = re.compile(r"\{\{\s*ref\(\s*'(\w+)'\s*\)\s*\}\}")


def compile_model(text: str) -> str:
    """Minimal dbt compile: resolve config/source/ref to runnable DuckDB SQL."""
    text = _CONFIG.sub("", text)
    text = _SOURCE.sub(
        lambda m: f"delta_scan('{(BRONZE / m.group(1)).as_posix()}')", text)
    text = _REF.sub(lambda m: f'"{m.group(1)}"', text)
    return text.strip()


def _gold_total(con, name: str):
    return con.execute(
        f"select round(sum(sum_paidamount), 2), count(*) "
        f"from delta_scan('{(GOLD / name).as_posix()}')"
    ).fetchone()


def main() -> int:
    con = duckdb.connect()
    con.execute("INSTALL delta; LOAD delta;")

    for name, rel in MODELS:
        sql = compile_model((HERE / rel).read_text(encoding="utf-8"))
        con.execute(f'CREATE OR REPLACE VIEW "{name}" AS {sql}')
    print(f"[ok] built {len(MODELS)} dbt models in DuckDB (staging->int->marts)")

    failures: list[str] = []

    # 1. Parity -- stable KPIs only (002 drifts with CURRENT_DATE by design).
    for gold_name, view in [("kpi_001_results", "fct_kpi_001_paid_trend"),
                            ("kpi_003_results", "fct_kpi_003_top_payers")]:
        gt, gc = _gold_total(con, gold_name)
        mt, mc = con.execute(
            f'select round(sum(sum_paidamount), 2), count(*) from "{view}"'
        ).fetchone()
        ok = abs((mt or 0) - (gt or 0)) < 0.01 and mc == gc
        mark = "ok" if ok else "x"
        print(f"[{mark}] parity {view}: mart=(total={mt}, rows={mc}) "
              f"gold=(total={gt}, rows={gc})")
        if not ok:
            failures.append(f"parity:{view}")

    # 2. Governance -- no PII column may reach a mart.
    pii = con.execute(
        "select table_name, column_name from information_schema.columns "
        "where lower(table_name) like 'fct\\_%' escape '\\' and "
        "lower(column_name) in ('ssn','firstname','lastname','middlename',"
        "'phonenumber','address','medicaidid','medicareid')"
    ).fetchall()
    print(f"[{'ok' if not pii else 'x'}] governance: PII columns in marts = "
          f"{pii or 'none'}")
    if pii:
        failures.append("pii_leak")

    # 3. Single-attribution shares sum to ~100%.
    share = con.execute(
        'select round(sum(percentage_share), 4) from "fct_kpi_002_lives_share"'
    ).fetchone()[0]
    ok = abs((share or 0) - 100.0) < 0.5
    print(f"[{'ok' if ok else 'x'}] share kpi_002 sums to ~100: {share}")
    if not ok:
        failures.append("share_sum")

    if failures:
        print(f"\n[x] SPIKE VALIDATION FAILED: {failures}")
        return 1
    print("\n[ok] SPIKE VALIDATION PASSED -- dbt models reconcile to gold, "
          "governance holds, shares sum to 100.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
