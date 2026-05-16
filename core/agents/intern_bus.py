"""
core/intern_bus.py — intern invocation, routing, retry, and activity logging.

Every intern call goes through this bus. Calls are logged to workspace.db
and optionally traced via TelemetryBackend (MLflow 3 LLM Tracing).

Retry policy: on failure (exception or empty/[ERROR]/None response), wait
`cfg.intern_retry_backoff_s` seconds and retry once. If retry also fails,
inject an `INTERN_FAILED: <reason>` placeholder that downstream interns can
see in their chained context.
"""
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.config import Config
from core.agents.registry import InternRegistry
from core.storage.workspace import Workspace

if TYPE_CHECKING:
    from core.observability.telemetry_backend import TelemetryBackend

ROOT = Path(__file__).resolve().parents[2]

_FAILURE_PREFIX = "INTERN_FAILED:"


def _is_failure(response: Optional[str]) -> bool:
    """Treat None / empty / explicit [ERROR] prefix as a failure that should be retried."""
    if not response:
        return True
    stripped = response.strip()
    if not stripped:
        return True
    if stripped.startswith("[ERROR]"):
        return True
    return False


def truncate_for_chain(text: str, max_chars: int) -> str:
    """Truncate a prior intern report for injection into the next intern's context.

    Strategy: keep the head (which usually contains the analysis summary +
    recommendation) and drop the tail. Mark the cut explicitly so downstream
    interns know content was removed.
    """
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    return head + f"\n... [TRUNCATED {len(text) - max_chars} chars]"


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
        """Invoke a named intern. Returns its markdown report. Retries once on failure.
        Always logs the call (both the first and the retry attempt)."""
        context = context or {}
        backoff_s = max(0, int(getattr(self.cfg, "intern_retry_backoff_s", 5)))

        response = self._invoke_once(intern_name, request, context, attempt=1)
        if _is_failure(response):
            if backoff_s:
                print(
                    f"[intern_bus] {intern_name} attempt 1 failed; "
                    f"retrying in {backoff_s}s",
                    flush=True,
                )
                time.sleep(backoff_s)
            retry_response = self._invoke_once(intern_name, request, context, attempt=2)
            if _is_failure(retry_response):
                reason = (retry_response or response or "no response").strip().splitlines()[0]
                return f"{_FAILURE_PREFIX} {intern_name} failed twice: {reason[:200]}"
            return retry_response
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

    def _invoke_once(self, intern_name: str, request: str, context: dict,
                     attempt: int) -> str:
        start = time.time()
        print(f"\n[intern_bus] >> {intern_name} (attempt {attempt})", flush=True)
        try:
            response = self._dispatch(intern_name, request, context)
        except Exception as exc:
            response = f"[ERROR] {intern_name} raised: {exc}"
            print(f"[intern_bus] {intern_name} raised: {exc}", flush=True)

        elapsed = round(time.time() - start, 2)
        self._log(intern_name, request, response, elapsed, context, attempt)

        if self.telemetry:
            try:
                self.telemetry.log_intern_trace(intern_name, request, response, elapsed)
            except Exception as exc:
                print(f"[intern_bus] telemetry trace failed (non-fatal): {exc}", flush=True)

        print(f"[intern_bus] << {intern_name} ({elapsed}s, attempt {attempt})", flush=True)
        return response or ""

    def _dispatch(self, name: str, request: str, context: dict) -> str:
        intern = self.registry.get_intern(name)
        return intern.run(request, context)

    def _log(self, name: str, request: str, response: str,
             elapsed: float, context: dict, attempt: int) -> None:
        entry = {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "intern":      name,
            "attempt":     attempt,
            "request":     request[:600],
            "response":    response[:3000],
            "elapsed_s":   elapsed,
            "exp_count":   context.get("experiment_count", 0),
            "best_metric": context.get("best_metric"),
        }
        self.workspace.log_intern_activity(name, request, entry)
