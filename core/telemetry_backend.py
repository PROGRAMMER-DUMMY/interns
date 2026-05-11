"""
core/telemetry_backend.py — TelemetryBackend strategy.

Two implementations:
  LocalTelemetry      → wraps existing Workspace (SQLite). Always active.
  DatabricksTelemetry → MLflow 3: experiment tracking, LLM tracing, GenAI evaluation.

Usage in loop.py:
    telemetry.begin_run("exp_5")
    ...intern calls... (traces captured automatically when Databricks is active)
    telemetry.end_run(metric=9.2, params={...}, status="keep")

All MLflow imports are lazy — LocalTelemetry loads with zero extra deps.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from core.config import DatabricksConfig
    from core.workspace import Workspace

ROOT = Path(__file__).parent.parent


# ── Abstract interface ─────────────────────────────────────────────────────────

class TelemetryBackend(ABC):

    @abstractmethod
    def begin_run(self, run_name: str, params: Optional[dict] = None) -> None:
        """Start a telemetry context for one experiment iteration."""

    @abstractmethod
    def end_run(self, metric: Optional[float], params: dict, status: str) -> None:
        """Finalise the telemetry context and flush all metrics."""

    @abstractmethod
    def log_intern_trace(
        self,
        intern_name: str,
        request: str,
        response: str,
        latency_s: float,
    ) -> None:
        """Record one intern LLM call (prompt → response + latency)."""

    @abstractmethod
    def log_evaluation(self, run_id: str, eval_data: dict) -> None:
        """Record evaluator output (GenAI eval use case)."""


# ── Local (SQLite / Workspace) ─────────────────────────────────────────────────

class LocalTelemetry(TelemetryBackend):
    """
    Wraps the existing Workspace for telemetry.
    No extra dependencies. Always used as the baseline; Databricks is additive.
    """

    def __init__(self, workspace: "Workspace"):
        self.workspace = workspace
        self._current_run_name: str = ""

    def begin_run(self, run_name: str, params: Optional[dict] = None) -> None:
        self._current_run_name = run_name

    def end_run(self, metric: Optional[float], params: dict, status: str) -> None:
        self.workspace.append_run_log(
            json.dumps(
                {
                    "run_id": self._current_run_name,
                    "metric": metric,
                    "status": status,
                    "params": params,
                }
            )
        )

    def log_intern_trace(self, intern_name: str, request: str, response: str, latency_s: float) -> None:
        entry = {
            "intern": intern_name,
            "latency_s": latency_s,
            "request_len": len(request),
            "response_len": len(response),
        }
        self.workspace.log_intern_activity(intern_name, request[:200], entry)

    def log_evaluation(self, run_id: str, eval_data: dict) -> None:
        self.workspace.append_run_log(
            f"\n[evaluation] run={run_id}\n{json.dumps(eval_data, indent=2)}\n"
        )


# ── Databricks / MLflow 3 ──────────────────────────────────────────────────────

class DatabricksTelemetry(TelemetryBackend):
    """
    MLflow 3 telemetry backend with three use cases:
      1. Experiment Tracking  — log_metric per run
      2. LLM Tracing          — span per intern call (MLflow 3 Tracing API)
      3. GenAI Evaluation     — mlflow.evaluate() per run
    """

    def __init__(self, cfg: "DatabricksConfig", experiment_path: str = "/autoresearch/experiments"):
        self.cfg = cfg
        self.experiment_path = experiment_path
        self._active_run = None
        self._trace_redact = cfg.trace_redact
        self._setup_mlflow()

    def _setup_mlflow(self) -> None:
        import mlflow
        from core.databricks_client import DatabricksClient
        mlflow.set_tracking_uri("databricks")
        client = DatabricksClient(self.cfg)
        client.create_mlflow_experiment(self.experiment_path)

    # ── MLflow run lifecycle ───────────────────────────────────────────────────

    def begin_run(self, run_name: str, params: Optional[dict] = None) -> None:
        import mlflow
        self._active_run = mlflow.start_run(run_name=run_name)
        if params:
            for k, v in params.items():
                mlflow.log_param(k, str(v)[:250])

    def end_run(self, metric: Optional[float], params: dict, status: str) -> None:
        import mlflow
        try:
            if metric is not None:
                mlflow.log_metric("primary_metric", metric)
            mlflow.log_param("status", status)
            for k, v in params.items():
                try:
                    mlflow.log_metric(k, float(v))
                except (ValueError, TypeError):
                    mlflow.log_param(k, str(v)[:250])
        finally:
            mlflow.end_run()
            self._active_run = None

    # ── MLflow 3 LLM Tracing (use case 2) ─────────────────────────────────────

    def log_intern_trace(self, intern_name: str, request: str, response: str, latency_s: float) -> None:
        import mlflow
        with mlflow.start_span(name=f"intern.{intern_name}", span_type="LLM") as span:
            inputs: dict[str, Any] = {"intern": intern_name}
            outputs: dict[str, Any] = {"response_len": len(response)}

            if not self._trace_redact:
                inputs["request"] = request[:2000]
                outputs["response"] = response[:2000]

            span.set_inputs(inputs)
            span.set_outputs(outputs)
            span.set_attribute("latency_seconds", latency_s)
            span.set_attribute("request_chars", len(request))
            span.set_attribute("response_chars", len(response))

    # ── MLflow GenAI Evaluation (use case 3) ──────────────────────────────────

    def log_evaluation(self, run_id: str, eval_data: dict) -> None:
        """
        Run mlflow.evaluate() on evaluator output.
        eval_data expected keys: primary_metric, matching_score, execution_time_seconds, status
        """
        try:
            import mlflow
            import pandas as pd

            df = pd.DataFrame([{
                "run_id": run_id,
                **{k: str(v) for k, v in eval_data.items()},
            }])

            def primary_metric_fn(predictions, targets=None, metrics=None):
                try:
                    return float(eval_data.get("primary_metric", 0))
                except (TypeError, ValueError):
                    return 0.0

            with mlflow.start_run(run_id=self._active_run.info.run_id if self._active_run else None,
                                   nested=True):
                mlflow.evaluate(
                    data=df,
                    model_type="text",
                    evaluators=[],
                    extra_metrics=[
                        mlflow.metrics.make_metric(
                            eval_fn=primary_metric_fn,
                            name="primary_metric",
                            greater_is_better=True,
                        )
                    ],
                )
        except Exception as exc:
            print(f"[telemetry] mlflow.evaluate() failed (non-fatal): {exc}", flush=True)

    # ── Delta table writes (use case: profiler + intern logs) ─────────────────

    def write_to_delta(
        self,
        db_client: Any,
        table: str,
        records: list[dict],
    ) -> bool:
        """
        Write records to a Delta table. Returns False on failure (telemetry_partial).
        """
        try:
            catalog = self.cfg.catalog
            schema = self.cfg.schema
            db_client.write_delta(catalog, schema, table, records)
            return True
        except Exception as exc:
            print(f"[telemetry] Delta write failed for {table} (non-fatal): {exc}", flush=True)
            return False


# ── Factory ───────────────────────────────────────────────────────────────────

def build_telemetry_backend(
    cfg: Any,
    workspace: "Workspace",
) -> tuple[TelemetryBackend, Optional[TelemetryBackend]]:
    """
    Returns (local_telemetry, databricks_telemetry_or_None).
    Local is always returned. Databricks is None when not configured.
    """
    local = LocalTelemetry(workspace)
    if not cfg.databricks.is_active():
        return local, None

    try:
        db_tel = DatabricksTelemetry(cfg.databricks)
        return local, db_tel
    except Exception as exc:
        print(f"[telemetry] DatabricksTelemetry init failed: {exc} — Databricks telemetry disabled", flush=True)
        return local, None
