"""Read warehouse spend back out of Databricks and attribute it to one run.

Phase 3. The audited run tagged 92 warehouse queries and priced none of them.
Two things were missing, and this module is the second one:

  1. The tag carried `project_name` + `env` only -- a TENANT key, not a run key.
     Fixed in `dbt_project_generator`, which now also tags `run_id` from
     `cost_ledger.RUN_ID_ENV`, resolved per invocation.
  2. Nothing ever queried `system.query.history` back. This does.

**Why this is not written into `AnchorEntry.cost_usd`.** That column is
AGENT-TOKEN cost -- `cost_ledger` is titled "agent-token cost ledger" and its
`cost_source: unreconciled` means "USD needs real per-model LLM prices"
(`cost_ingest`). `system.billing` bills warehouse DBUs. They are different cost
bases and summing them, or letting one backfill the other's column, produces a
number that means nothing. Warehouse dollars therefore land in their OWN
artifact (`interns/reports/cost_ledger/warehouse_cost.json`), keyed by the same
`run_id`, so the two can be reported side by side and never conflated. That also
keeps the JSONL ledger append-only, which was a deliberate choice.

**The allocation is an estimate and says so.** `system.query.history` has no
dollar column; `system.billing.usage` bills a WAREHOUSE, not a query. A run's
share is its query duration over all query duration on that warehouse in the
same window. That is the standard attribution and it is honest about being one:
every row is stamped `cost_source: "list_price_duration_allocated"`. It is list
price -- account discounts are not in `list_prices`.

Executing this touches the warehouse, so it sits behind the same G5 gate as
every other remote path. `refused_no_remote_approval` is a distinct status: it
never reports a cost it did not read.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.contracts.versioning import register_contract
from core.observability.cost_ledger import CostLedger, current_run_id, ledger_dir_for
from core.onboarding.databricks.deploy_gates import check_remote_approval
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

REPORT_VERSION = 1
register_contract("cost_ledger/warehouse_cost.json", current_version=REPORT_VERSION)

# How the dollar figure was derived. Stamped on every row so a reader never has
# to guess whether it is metered, allocated, list price, or negotiated.
COST_SOURCE = "list_price_duration_allocated"

# run_id shapes this platform mints: `sess-<hex8>` (session-scoped) and
# `<YYYYMMDDTHHMMSSZ>-<hex8>` (degraded/seam mint). Validated rather than
# quoted because the value is interpolated into SQL -- there is no bind-parameter
# path through `execute_query`, so the safe move is to refuse anything that is
# not one of our own ids.
_RUN_ID_RE = re.compile(r"^(?:sess-[0-9a-f]{8}|\d{8}T\d{6}Z-[0-9a-f]{8})$")


@dataclass(frozen=True)
class WarehouseCostResult:
    current_json_path: str
    current_markdown_path: str
    status: str  # "reconciled" | "no_tagged_queries" | "refused_no_remote_approval"
    run_id: str
    warehouse_usd: float = 0.0
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


def warehouse_cost_sql(run_id: str) -> str:
    """The attribution query for one run.

    `run_q`   -- this run's queries, by warehouse, from the tag dbt now writes.
    `wh_q`    -- ALL query time on those warehouses in the same window: the
                 denominator. Without it a run on a busy warehouse would be
                 charged the warehouse's whole bill.
    `wh_cost` -- what those warehouses actually billed over the window, priced
                 at the list price in effect when the usage was recorded.
    """
    if not _RUN_ID_RE.match(str(run_id or "")):
        raise ValueError(
            f"refusing to build SQL for an unrecognised run_id {run_id!r}. "
            "Expected `sess-<hex8>` or `<YYYYMMDDTHHMMSSZ>-<hex8>` as minted by "
            "core.observability.cost_ledger."
        )
    return f"""
WITH run_q AS (
  SELECT
    q.compute.warehouse_id   AS warehouse_id,
    COUNT(*)                 AS query_count,
    SUM(q.total_duration_ms) AS run_duration_ms,
    MIN(q.start_time)        AS window_start,
    MAX(q.end_time)          AS window_end
  FROM system.query.history q
  WHERE q.query_tags['run_id'] = '{run_id}'
    AND q.compute.warehouse_id IS NOT NULL
  GROUP BY q.compute.warehouse_id
),
wh_q AS (
  SELECT r.warehouse_id, SUM(q.total_duration_ms) AS warehouse_duration_ms
  FROM run_q r
  JOIN system.query.history q
    ON q.compute.warehouse_id = r.warehouse_id
   AND q.start_time >= r.window_start
   AND q.end_time   <= r.window_end
  GROUP BY r.warehouse_id
),
wh_cost AS (
  SELECT
    r.warehouse_id,
    SUM(u.usage_quantity)                                    AS warehouse_dbus,
    SUM(u.usage_quantity * p.pricing.effective_list.default) AS warehouse_usd
  FROM run_q r
  JOIN system.billing.usage u
    ON u.usage_metadata.warehouse_id = r.warehouse_id
   AND u.record_type = 'ORIGINAL'
   AND u.usage_end_time   > r.window_start
   AND u.usage_start_time < r.window_end
  JOIN system.billing.list_prices p
    ON p.sku_name  = u.sku_name
   AND p.usage_unit = u.usage_unit
   AND u.usage_start_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)
  GROUP BY r.warehouse_id
)
SELECT
  r.warehouse_id,
  r.query_count,
  r.run_duration_ms,
  w.warehouse_duration_ms,
  c.warehouse_dbus,
  c.warehouse_usd,
  c.warehouse_usd * (r.run_duration_ms / NULLIF(w.warehouse_duration_ms, 0)) AS run_usd
FROM run_q r
LEFT JOIN wh_q    w ON w.warehouse_id = r.warehouse_id
LEFT JOIN wh_cost c ON c.warehouse_id = r.warehouse_id
ORDER BY r.warehouse_id
""".strip()


def _f(value: Any) -> float:
    """A numeric cell from the JSON_ARRAY result format, which returns strings.

    None/'' means the LEFT JOIN found no billing row (usage not landed yet --
    system.billing lags by hours). 0.0, and the row's own null columns still
    show it, rather than a crash on an expected state.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def reconcile_warehouse_cost(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    run_id: str = "",
    client: Optional[Any] = None,
) -> WarehouseCostResult:
    repo_root = Path(repo_root).resolve()
    workspace_rel = str(workspace).replace("\\", "/")
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())
    run_id = str(run_id or "").strip() or current_run_id(workspace_rel)[0]

    remote_gate = check_remote_approval()
    rows: list[dict[str, Any]] = []
    sql = warehouse_cost_sql(run_id)
    if remote_gate.ok:
        if client is None:
            from core.config import resolve_databricks_config
            from core.execution.databricks_client import DatabricksClient

            client = DatabricksClient(resolve_databricks_config(layout.enterprise_id()))
        columns, raw_rows = client.execute_query(sql)
        for raw in raw_rows:
            record = dict(zip(columns, raw))
            rows.append({
                "warehouse_id": str(record.get("warehouse_id") or ""),
                "query_count": int(_f(record.get("query_count"))),
                "run_duration_ms": _f(record.get("run_duration_ms")),
                "warehouse_duration_ms": _f(record.get("warehouse_duration_ms")),
                "warehouse_dbus": _f(record.get("warehouse_dbus")),
                "warehouse_usd": _f(record.get("warehouse_usd")),
                "run_usd": round(_f(record.get("run_usd")), 4),
            })
        status = "reconciled" if rows else "no_tagged_queries"
    else:
        status = "refused_no_remote_approval"

    anchors = CostLedger(ledger_dir_for(workspace_rel, repo_root)).entries_for_run(run_id)
    report = {
        "artifact_type": "cost_ledger/warehouse_cost.json",
        "version": REPORT_VERSION,
        "generated_by": "reconcile-warehouse-cost",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "workspace_id": workspace_rel,
        "status": status,
        "cost_source": COST_SOURCE,
        "remote_approval_ok": remote_gate.ok,
        "remote_approval_blocking_reason": remote_gate.blocking_reason,
        # Deliberately NOT agent token cost. Kept as a separate total so the two
        # bases are reported side by side and never summed into one number.
        "warehouse_usd": round(sum(r["run_usd"] for r in rows), 4),
        "warehouse_dbus": round(sum(r["warehouse_dbus"] for r in rows), 4),
        "anchor_rows_for_run": len(anchors),
        "warehouses": rows,
        "sql": sql,
    }
    panel_dir = layout.reports_dir / "cost_ledger"
    panel_dir.mkdir(parents=True, exist_ok=True)
    current_json = panel_dir / "warehouse_cost.json"
    current_md = panel_dir / "warehouse_cost.md"
    current_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    current_md.write_text(_render_markdown(report), encoding="utf-8")

    if status == "refused_no_remote_approval":
        raise PermissionError(
            f"{remote_gate.blocking_reason} No warehouse cost was read; the query "
            f"that WOULD run is in {_rel(current_md, repo_root)}."
        )
    return WarehouseCostResult(
        _rel(current_json, repo_root),
        _rel(current_md, repo_root),
        status,
        run_id,
        warehouse_usd=report["warehouse_usd"],
        # Zero tagged queries is a real finding, not a pass: either nothing ran
        # on the warehouse for this run, or AUTORESEARCH_RUN_ID was not exported
        # into the dbt subprocess and the tag went out empty.
        ok=status == "reconciled",
    )


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Warehouse cost -- one run",
        "",
        f"- run_id: `{report['run_id']}`  workspace: `{report['workspace_id']}`",
        f"- status: `{report['status']}`",
        f"- anchor rows for this run: `{report['anchor_rows_for_run']}`",
        "",
    ]
    if report["status"] == "refused_no_remote_approval":
        lines += [
            f"Nothing was read: {report['remote_approval_blocking_reason']}.",
            "",
            "The query that would run:",
            "",
            "```sql",
            report["sql"],
            "```",
            "",
        ]
        return "\n".join(lines)
    if report["status"] == "no_tagged_queries":
        lines += [
            "No query in `system.query.history` carries this run's tag. Either no",
            "warehouse work ran for it, or `AUTORESEARCH_RUN_ID` was not exported",
            "into the dbt subprocess and the `run_id` tag went out empty.",
            "",
        ]
        return "\n".join(lines)
    lines += [
        f"## Warehouse cost: **${report['warehouse_usd']:.2f}** "
        f"({report['warehouse_dbus']:.2f} DBU)",
        "",
        f"`cost_source: {report['cost_source']}` -- LIST price, allocated by this",
        "run's share of query duration on each warehouse. `system.billing` bills a",
        "warehouse, not a query, so a per-run figure is necessarily an allocation.",
        "This is NOT agent-token cost; that lives in `current.md` and the two are",
        "different cost bases.",
        "",
        "| Warehouse | Queries | Run ms | Warehouse ms | DBU | Warehouse $ | Run $ |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report["warehouses"]:
        lines.append(
            f"| `{row['warehouse_id']}` | {row['query_count']} | "
            f"{row['run_duration_ms']:.0f} | {row['warehouse_duration_ms']:.0f} | "
            f"{row['warehouse_dbus']:.2f} | ${row['warehouse_usd']:.2f} | "
            f"${row['run_usd']:.2f} |"
        )
    lines.append("")
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@anchored("reconcile-warehouse-cost")
def main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--run-id", default="", help="defaults to this session's run id")
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="reconcile-warehouse-cost",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: reconcile_warehouse_cost(
            args.repo_root, args.workspace, run_id=args.run_id,
        ),
        metadata={"run_id": args.run_id},
    )


if __name__ == "__main__":
    raise SystemExit(main())
