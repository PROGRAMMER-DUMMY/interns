"""
core/intern_bus.py — intern invocation, routing, and activity logging.

Every intern call goes through this bus. Calls are logged to workspace.db
and optionally traced via TelemetryBackend (MLflow 3 LLM Tracing).
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.config import Config
from core.registry import InternRegistry
from core.workspace import Workspace

if TYPE_CHECKING:
    from core.telemetry_backend import TelemetryBackend

ROOT = Path(__file__).parent.parent


class InternBus:
    """Dispatch intern invocations, log all activity, and emit LLM traces."""

    def __init__(self, cfg: Config, telemetry: Optional["TelemetryBackend"] = None):
        self.cfg = cfg
        self.registry = InternRegistry(cfg)
        self.workspace = Workspace(cfg.workspace_db)
        self.telemetry = telemetry
        cfg.state_dir.mkdir(parents=True, exist_ok=True)

    # ── Public ────────────────────────────────────────────────────────────────

    def invoke(self, intern_name: str, request: str, context: Optional[dict] = None) -> str:
        """Invoke a named intern. Returns its markdown report. Always logs the call."""
        context = context or {}
        start = time.time()
        print(f"\n[intern_bus] >> {intern_name}", flush=True)

        try:
            response = self._dispatch(intern_name, request, context)
        except Exception as exc:
            response = f"[ERROR] {intern_name} raised: {exc}"
            print(f"[intern_bus] {intern_name} FAILED: {exc}", flush=True)

        elapsed = round(time.time() - start, 2)
        self._log(intern_name, request, response, elapsed, context)

        if self.telemetry:
            try:
                self.telemetry.log_intern_trace(intern_name, request, response, elapsed)
            except Exception as exc:
                print(f"[intern_bus] telemetry trace failed (non-fatal): {exc}", flush=True)

        print(f"[intern_bus] << {intern_name} ({elapsed}s)", flush=True)
        return response

    def list_active(self, domain: str = "prompt_optimisation") -> list[str]:
        """Return active intern names for the given domain (from agents.toml)."""
        try:
            import toml
            agents = toml.load(ROOT / "config" / "agents.toml")
            return agents.get("interns", {}).get(domain, {}).get("active", [])
        except Exception:
            return ["prompt_engineer", "insights", "eval"]

    # ── Private ───────────────────────────────────────────────────────────────

    def _dispatch(self, name: str, request: str, context: dict) -> str:
        intern = self.registry.get_intern(name)
        return intern.run(request, context)

    def _log(self, name: str, request: str, response: str,
             elapsed: float, context: dict) -> None:
        entry = {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "intern":      name,
            "request":     request[:600],
            "response":    response[:3000],
            "elapsed_s":   elapsed,
            "exp_count":   context.get("experiment_count", 0),
            "best_metric": context.get("best_metric"),
        }
        self.workspace.log_intern_activity(name, request, entry)
