"""
core/medallion/mlflow_emit.py — MLflow run tagging for build-medallion (P2/P5).

Every build-medallion run is an MLflow run. MLflow unavailability never
fails a build — all calls are wrapped in try/except.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def start_run(
    workspace: str,
    manifest_hash: str,
    run_id: str,
    target_declared: str,
) -> Optional[str]:
    """
    Start an MLflow run and return its run_id string.
    Returns None if MLflow is unavailable.
    """
    try:
        import mlflow
        mlflow.set_experiment(f"/medallion/{workspace}")
        run = mlflow.start_run(run_name=run_id, nested=False)
        mlflow.set_tags({
            "workspace": workspace,
            "target_declared": target_declared,
            "manifest_hash": manifest_hash,
            "medallion_run_id": run_id,
        })
        return run.info.run_id
    except Exception as exc:
        print(f"[mlflow_emit] WARNING: MLflow start_run failed: {exc}")
        return None


def finalize_run(run_state: dict[str, Any], run_state_dir: Path) -> None:
    """
    Set final tags, log metrics, and attach artifacts. Safe to call even
    if start_run was never called (no-op if no active run).
    """
    try:
        import mlflow

        if not mlflow.active_run():
            return

        # P2 tags
        mlflow.set_tag("target_actual", run_state.get("target_actual", ""))
        mlflow.set_tag("degraded_run", str(run_state.get("degraded_run", False)))

        # P5 audit tags
        mlflow.set_tag("pii_columns_hashed_count",
                       str(run_state.get("pii_columns_hashed_count", 0)))
        mlflow.set_tag("assertions_passed_count",
                       str(run_state.get("assertions_passed_count", 0)))
        mlflow.set_tag("unconfirmed_decisions_at_design_time",
                       str(run_state.get("unconfirmed_at_design", 0)))

        # Per-table row count metrics + assertion pass rates + row-count delta
        for table, status in (run_state.get("per_table_status") or {}).items():
            key = table.replace(".", "_")
            if "row_count_after" in status:
                mlflow.log_metric(f"rows.{key}", status["row_count_after"])
            if "assertions" in status:
                passed = sum(1 for v in (status["assertions"] or {}).values() if v == "pass")
                total = len(status["assertions"] or {})
                mlflow.log_metric(f"assertions_pass_rate.{key}", passed / max(total, 1))
            delta = status.get(
                "row_count_delta",
                status.get("row_count_after", 0) - status.get("row_count_before", 0),
            )
            mlflow.log_metric(f"row_count_delta.{key}", delta)

        # Artifacts
        run_json = run_state_dir / "run.json"
        if run_json.exists():
            mlflow.log_artifact(str(run_json))

        medallion_dir = run_state_dir.parent.parent.parent / "generated" / "medallion"
        for artifact_name in ("manifest.yaml", "lineage.json"):
            p = medallion_dir / artifact_name
            if p.exists():
                mlflow.log_artifact(str(p))

        # P5 lineage-with-runtime artifact
        lineage_rt = run_state_dir / "lineage_with_runtime.json"
        if lineage_rt.exists():
            mlflow.log_artifact(str(lineage_rt))

        mlflow.end_run()
    except Exception as exc:
        print(f"[mlflow_emit] WARNING: MLflow finalize_run failed: {exc}")


def write_lineage_with_runtime(
    lineage_dict: dict[str, Any],
    run_state: dict[str, Any],
    out_path: Path,
) -> None:
    """Annotate lineage JSON with build-time row counts and write to out_path."""
    annotated = dict(lineage_dict)
    per_table = run_state.get("per_table_status", {})
    annotated["runtime_annotation"] = {
        table: {
            "row_count_after": status.get("row_count_after", 0),
            "elapsed_s": status.get("elapsed_s", 0),
        }
        for table, status in per_table.items()
    }
    out_path.write_text(json.dumps(annotated, indent=2), encoding="utf-8")
