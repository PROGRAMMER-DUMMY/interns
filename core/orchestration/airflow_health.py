"""Airflow operability: is_paused + scheduler-heartbeat health, over REST.

A repo-wide grep for `pool=|is_paused|check_airflow_health|setup_pools`
across `core/orchestration/` returned 0 matches before this module -- nothing
today notices a silently-paused generated DAG (the quietest way this
pipeline dies -- docs/reference/airflow_cli_reference.md section 8, item 4)
or gives a replay a bounded slot count of its own (section "Known gaps in
our wiring" #1).

REST, never the CLI: `docs/reference/airflow_cli_reference.md` section 7 --
the CLI needs to be co-located with (or shelled into) the Airflow
deployment; the REST API does not. A workflow-guard health poll against a
remote/managed Airflow (Astro, MWAA, Composer) has to go over HTTP, the same
way `GET /api/v2/monitor/health` is documented there for exactly this job.

Two calls per check:
  - `GET {base_url}/api/v2/monitor/health` -- scheduler heartbeat.
  - `GET {base_url}/api/v2/dags/{dag_id}` per `dag_id` -- `is_paused`.

`http` (default `urllib.request.urlopen`) is injectable so tests never open
a real socket, same shape as every other network-touching seam in this
package (`core.provisioning.sync_code`, `core.orchestration.dbt_state`).

Secret hygiene: the JWT is carried ONLY in the `Authorization: Bearer`
request header, never in the returned dict; any exception text surfaced in
the result is passed through `core.observability.log_redaction.redact`
first (a connection error can echo the host it failed to reach).
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from typing import Any, Callable, Sequence

from core.observability.cost_ledger import anchored
from core.observability.log_redaction import redact

_HEALTH_PATH = "/api/v2/monitor/health"
_DAG_PATH = "/api/v2/dags/{dag_id}"

STATUS_HEALTHY = "healthy"
STATUS_UNHEALTHY = "unhealthy"
STATUS_UNREACHABLE = "unreachable"

_DEFAULT_TIMEOUT_SECONDS = 10

HttpCallable = Callable[..., Any]


def _get_json(http: HttpCallable, url: str, token: str, *, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    resp = http(req, timeout=timeout)
    try:
        raw = resp.read()
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return json.loads(text)


def check_airflow_health(
    base_url: str,
    token: str,
    dag_ids: Sequence[str],
    *,
    http: HttpCallable = urllib.request.urlopen,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """`{"ok": bool, "paused_dags": [...], "scheduler": "healthy"|"unhealthy"|"unreachable"}`.

    A paused DAG fails silently and forever -- it looks identical to "no
    data changed" -- so it makes `ok: False` even when the scheduler itself
    is healthy. Never raises: this is a read-only probe, and a connection
    failure IS the finding (`scheduler: "unreachable"`), not a crash.
    """
    base = str(base_url).rstrip("/")
    try:
        health = _get_json(http, f"{base}{_HEALTH_PATH}", token, timeout=timeout)
    except Exception as exc:
        return {
            "ok": False,
            "paused_dags": [],
            "scheduler": STATUS_UNREACHABLE,
            "detail": redact(f"{type(exc).__name__}: {exc}"),
        }

    scheduler_status = str(
        ((health.get("scheduler") or {}).get("status")) or ""
    ).strip().lower()
    scheduler_status = scheduler_status if scheduler_status == STATUS_HEALTHY else STATUS_UNHEALTHY

    paused: list[str] = []
    for dag_id in dag_ids:
        url = f"{base}{_DAG_PATH.format(dag_id=dag_id)}"
        try:
            data = _get_json(http, url, token, timeout=timeout)
        except Exception:
            # The scheduler already answered the health endpoint above, so
            # this is a dag-scoped lookup failure, not a broader outage --
            # still, an unknown state must not silently read as healthy.
            paused.append(dag_id)
            continue
        if bool(data.get("is_paused")):
            paused.append(dag_id)

    ok = scheduler_status == STATUS_HEALTHY and not paused
    return {"ok": ok, "paused_dags": paused, "scheduler": scheduler_status}


@anchored("check-airflow-health")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check an Airflow deployment's scheduler health and DAG pause state."
    )
    parser.add_argument("--base-url", default=os.environ.get("AIRFLOW_API_BASE_URL", ""))
    parser.add_argument("--token", default=os.environ.get("AIRFLOW_API_TOKEN", ""))
    parser.add_argument("--dag-id", action="append", default=[], dest="dag_ids")
    args = parser.parse_args(argv)

    if not args.base_url or not args.token:
        print(json.dumps({
            "ok": False,
            "reason": (
                "--base-url/AIRFLOW_API_BASE_URL and --token/AIRFLOW_API_TOKEN "
                "are both required"
            ),
        }))
        return 1

    result = check_airflow_health(args.base_url, args.token, args.dag_ids)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "STATUS_HEALTHY",
    "STATUS_UNHEALTHY",
    "STATUS_UNREACHABLE",
    "check_airflow_health",
    "main",
]
