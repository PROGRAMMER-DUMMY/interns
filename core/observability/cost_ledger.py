"""core/observability/cost_ledger.py -- Phase 1a.1: the ANCHOR half of the
agent-token cost ledger.

Two-point capture model (see docs/core_audit/cost-ledger.md). A child ``uv run``
process CANNOT read its parent agent's token counters, so capture splits into two
mechanically different points:

  * ANCHOR (this module, synchronous, in-process): write one correlation row per
    pipeline stage, keyed by ``run_id`` / ``workspace_id`` / ``pipeline_stage``,
    carrying the agent-native session id read from the ENVIRONMENT
    (``CLAUDE_CODE_SESSION_ID`` -- the OTel ``session.id`` join key) plus the
    resolved driving agent. Token/cost columns are deliberately NULL. The anchor
    makes agent tokens JOINABLE later; it does not, and cannot, contain them.
  * INGEST (Phase 1a.2, out-of-band): collect the agent CLIs' native OTel export
    and fill the null token columns by joining on ``agent_session_id``.

Why the anchor is honestly empty: ``capture_method="anchor_only"`` and
``cost_source="unreconciled"`` with null tokens. Nothing downstream should render
a cost number until 1a.2 fills it -- a placeholder must never read as a zero.

Known alternative (recorded so it is not rediscovered): the ONLY way to get a
synchronous, in-process token count is to route the agent through a gateway
(LiteLLM / Helicone) in front of the model. That is a larger architectural
commitment than Phase 1 makes; the two-point model is the deliberate choice.

Join-key provenance (the finding that shaped this schema): the platform's own
``session_snapshot`` id is ``sha256(workspace|tool|now)[:10]`` -- an invented
digest that never joins against anything the agent emits. The real join key is
the agent-native session id exported to every subprocess. Empirically verified
2026-07-18 that ``CLAUDE_CODE_SESSION_ID`` is exposed, stable, and matches the
session's scratchpad path segment. NOT yet byte-matched against the OTel
``session.id`` span attribute -- that is an explicit Phase 1a.2 gate (see the
ledger doc): the first live OTLP capture MUST confirm the byte-match before any
ingest is trusted, or the join key is revisited.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

SCHEMA_VERSION = 1

# Report Stage-1 advance gate: "every agent run and every pipeline has
# attributable cost with <10% UNATTRIBUTED" (modern-data-engineering-report.md,
# Recommendations, Stage 1.1). Reusing that exact bar keeps the ledger's liveness
# threshold identical to the platform's stated FinOps bar rather than inventing a
# number. A bare count gets ignored; a failing gate does not.
UNATTRIBUTED_FAIL_FRACTION = 0.10


# --------------------------------------------------------------------------- #
# Agent detection (env-first, config-fallback). Records WHICH source resolved   #
# it, so weak attribution is visible, not hidden behind a default value.        #
# --------------------------------------------------------------------------- #
# (env_var, agent, detection_source). Order = precedence.
# CLAUDECODE is empirically verified exposed to subprocesses (live check
# 2026-07-18: CLAUDECODE=1, CLAUDE_CODE_ENTRYPOINT=cli). CODEX_SANDBOX is Codex's
# documented sandbox marker (report-backed, not yet locally verified -- there is
# no Codex-driven session to check from here). Gemini CLI has NO reliable marker
# verified, so it is deliberately absent: a Gemini run resolves via config, which
# an honest ``agent_detection_source: config`` records -- that beats a fabricated
# marker and is testable in 1a.2 when someone actually drives with it.
_AGENT_ENV_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("CLAUDECODE", "claude-code", "env:CLAUDECODE"),
    ("CLAUDE_CODE_ENTRYPOINT", "claude-code", "env:CLAUDE_CODE_ENTRYPOINT"),
    ("CODEX_SANDBOX", "codex", "env:CODEX_SANDBOX"),
)

# (env_var, agent_hint). Order = precedence. ONLY CLAUDE_CODE_SESSION_ID is
# empirically verified exposed to subprocesses (live check 2026-07-18). Codex and
# Gemini session-id env vars are UNVERIFIED and gated on Phase 1a.2's per-agent
# capture; adding one here is a one-line data change once verified.
_SESSION_ID_ENV: tuple[tuple[str, str], ...] = (
    ("CLAUDE_CODE_SESSION_ID", "claude-code"),
)


def detect_agent(env: Mapping[str, str]) -> tuple[Optional[str], str]:
    """Return ``(agent, detection_source)`` from environment markers.

    ``(None, "none")`` when no marker matches -- the caller then falls back to the
    configured agent. Never guesses.
    """
    for var, agent, source in _AGENT_ENV_MARKERS:
        if str(env.get(var) or "").strip():
            return agent, source
    return None, "none"


def resolve_agent_session(env: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(agent_session_id, agent_session_source)`` -- the OTel join key.

    ``("", "none")`` when no known agent-native session id is present in the
    environment. This is the load-bearing value: 1a.2 joins OTel token metrics to
    anchors on ``agent_session_id``.
    """
    for var, _agent in _SESSION_ID_ENV:
        value = str(env.get(var) or "").strip()
        if value:
            return value, f"env:{var}"
    return "", "none"


def resolve_agent(
    env: Mapping[str, str], configured_agent: Optional[str]
) -> tuple[str, str]:
    """Resolve the driving agent as detected -> configured -> ``"unknown"``.

    Returns ``(agent, agent_detection_source)``. ``agent_detection_source`` is the
    env marker when detected, ``"config"`` when it falls back to a configured
    value, and ``"none"`` when neither resolves.
    """
    detected, source = detect_agent(env)
    if detected:
        return detected, source
    configured = str(configured_agent or "").strip()
    if configured:
        return configured, "config"
    return "unknown", "none"


# --------------------------------------------------------------------------- #
# Run id -- minted at the pipeline seam (Q3: a distinct id with a distinct       #
# owner, NOT a promotion of medallion's run_state.new_run_id which is             #
# medallion-lifecycle-scoped). Same familiar format.                            #
# --------------------------------------------------------------------------- #
def new_run_id(
    *, workspace_id: str, agent_session_id: str = "", now: Optional[float] = None
) -> str:
    """Mint a unified per-run id: ``%Y%m%dT%H%M%SZ-<hash8>``."""
    moment = time.time() if now is None else now
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(moment))
    seed = f"{workspace_id}|{agent_session_id}|{moment}"
    # Non-security fingerprint: shortens the seed into a human-readable suffix.
    suffix = hashlib.sha1(seed.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{ts}-{suffix}"


def run_id_for(agent_session_id: str, workspace_id: str) -> tuple[str, str]:
    """Derive ``(run_id, run_id_source)`` for an INDIVIDUALLY-invoked command.

    Individually-invoked commands are separate processes with no shared parent to
    hand down a run id. So:

    * When an agent-native session id is present, derive a STABLE id from
      ``(session, workspace)`` with NO time component -- every command in one agent
      session against one workspace (onboard -> resolve -> build -> dashboard)
      collapses into a single run rather than fragmenting into one run per process.
      ``run_id_source = "session_workspace"``.
    * When the session id is ABSENT (Finding A: not universally available -- weak
      for Gemini), fall back to a unique time-based mint so unattributed
      invocations do NOT collapse into one bogus shared run id or collide. The
      degradation is recorded, never silent. ``run_id_source = "degraded_time"``.

    The seam path (``run_pipeline``) keeps its own time-based mint tagged
    ``"pipeline_seam"`` -- the derivations are deliberately NOT unified, and the
    source marker is what lets them be told apart later instead of inferred from id
    format.
    """
    session = str(agent_session_id or "").strip()
    if session:
        seed = f"{session}|{workspace_id}"
        suffix = hashlib.sha1(seed.encode(), usedforsecurity=False).hexdigest()[:8]
        return f"sess-{suffix}", "session_workspace"
    return new_run_id(workspace_id=workspace_id), "degraded_time"


# --------------------------------------------------------------------------- #
# Schema -- every token/cost column is nullable and empty at anchor time. That   #
# nullability is what makes surfacing the ledger later PURELY ADDITIVE.          #
# --------------------------------------------------------------------------- #
@dataclass
class AnchorEntry:
    # ---- keys (always present at anchor time) ----
    run_id: str
    workspace_id: str
    pipeline_stage: str
    run_id_source: str = ""  # how run_id was derived: pipeline_seam | session_workspace | degraded_time
    # ---- agent-native join key (the finding that reshaped the schema) ----
    agent_session_id: str = ""
    agent_session_source: str = "none"
    platform_session_id: str = ""  # session_snapshot id -- secondary correlation only
    # ---- agent detection ----
    agent: str = "unknown"
    agent_detected: Optional[str] = None
    agent_configured: Optional[str] = None
    agent_detection_source: str = "none"
    # ---- timing ----
    started_at: str = ""
    finished_at: str = ""
    # ---- tokens (NULL until 1a.2 ingest; normalized to mutually-exclusive buckets) ----
    provider: Optional[str] = None
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    thinking_tokens: Optional[int] = None
    # ---- cost + provenance ----
    cost_usd: Optional[float] = None
    cost_source: str = "unreconciled"
    capture_method: str = "anchor_only"
    endpoint_note: str = ""
    # ---- provenance envelope ----
    artifact_type: str = "cost_ledger.entry"
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_anchor(
    *,
    run_id: str,
    workspace_id: str,
    pipeline_stage: str,
    env: Mapping[str, str],
    configured_agent: Optional[str] = None,
    platform_session_id: str = "",
    started_at: str = "",
    finished_at: str = "",
    run_id_source: str = "",
) -> AnchorEntry:
    """Build one honestly-empty anchor row from the environment.

    The driving agent and its session id are read from the environment (with a
    configured fallback for the agent only). Token/cost columns stay null.

    ``finished_at`` is captured now (not conditionally) even though stage-level
    temporal attribution is deferred: it is a data-collection decision, not a
    feature -- an anchor written without it forecloses temporal attribution for
    that row permanently, and it cannot be backfilled.
    """
    agent_session_id, agent_session_source = resolve_agent_session(env)
    detected, _detected_source = detect_agent(env)
    agent, detection_source = resolve_agent(env, configured_agent)
    return AnchorEntry(
        run_id=run_id,
        workspace_id=workspace_id,
        pipeline_stage=pipeline_stage,
        run_id_source=run_id_source,
        agent_session_id=agent_session_id,
        agent_session_source=agent_session_source,
        platform_session_id=platform_session_id,
        agent=agent,
        agent_detected=detected,
        agent_configured=(str(configured_agent).strip() or None) if configured_agent else None,
        agent_detection_source=detection_source,
        started_at=started_at,
        finished_at=finished_at,
    )


# --------------------------------------------------------------------------- #
# Liveness -- the capture analogue of P0.2's assert_installed(). A completed run  #
# that produced zero anchors, or too many unattributable anchors, FAILS loudly.  #
# --------------------------------------------------------------------------- #
def _is_unattributable(entry: Mapping[str, Any]) -> bool:
    """A row is unattributable iff it has NO agent-native session id AND NO agent
    was detected (session empty AND detection_source == "none"). Such a row can
    never be joined to a token count and its agent is a pure guess."""
    no_session = not str(entry.get("agent_session_id") or "").strip()
    no_agent = str(entry.get("agent_detection_source") or "none") == "none"
    return no_session and no_agent


@dataclass
class LivenessResult:
    ok: bool
    reason: str
    total_anchors: int
    unattributable: int
    unattributable_fraction: float
    without_session_id: int  # join-coverage signal (reported, NOT gated in 1a.1)
    threshold: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def liveness_check(
    entries: list[Mapping[str, Any]], *, threshold: float = UNATTRIBUTED_FAIL_FRACTION
) -> LivenessResult:
    """Gate a run's anchors. Fails on (a) zero anchors -- capture is not live -- or
    (b) unattributable fraction over ``threshold``."""
    total = len(entries)
    without_session = sum(
        1 for e in entries if not str(e.get("agent_session_id") or "").strip()
    )
    if total == 0:
        return LivenessResult(
            ok=False,
            reason="no anchor rows written -- capture is not live",
            total_anchors=0,
            unattributable=0,
            unattributable_fraction=0.0,
            without_session_id=0,
            threshold=threshold,
        )
    unattributable = sum(1 for e in entries if _is_unattributable(e))
    fraction = unattributable / total
    ok = fraction <= threshold
    if ok:
        reason = (
            f"{total} anchor(s); {unattributable} unattributable "
            f"({fraction:.0%} <= {threshold:.0%} threshold)"
        )
    else:
        reason = (
            f"unattributable anchors {unattributable}/{total} ({fraction:.0%}) "
            f"exceed {threshold:.0%} threshold -- attribution is degrading"
        )
    return LivenessResult(
        ok=ok,
        reason=reason,
        total_anchors=total,
        unattributable=unattributable,
        unattributable_fraction=round(fraction, 4),
        without_session_id=without_session,
        threshold=threshold,
    )


# --------------------------------------------------------------------------- #
# Ledger store -- append-only JSONL per workspace + a paired .md summary so the   #
# JSON is never read whole (token discipline). JSONL is the deliberate choice     #
# (Q4): zero-dep, additive, append-friendly for out-of-band backfill. Migration   #
# path if it outgrows JSONL: LocalTelemetry (SQLite).                             #
# --------------------------------------------------------------------------- #
def ledger_dir_for(workspace: str, repo_root: Optional[Path] = None) -> Path:
    root = (repo_root or PROJECT_ROOT).resolve()
    project_root = Path(workspace)
    if not project_root.is_absolute():
        project_root = (root / workspace).resolve()
    return WorkspaceLayout(project_root=project_root).reports_dir / "cost_ledger"


@dataclass
class CostLedger:
    ledger_dir: Path

    @property
    def entries_path(self) -> Path:
        return self.ledger_dir / "entries.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.ledger_dir / "current.md"

    def append(self, entry: AnchorEntry) -> None:
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        with self.entries_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    def read_entries(self) -> list[dict[str, Any]]:
        if not self.entries_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.entries_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def entries_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [e for e in self.read_entries() if e.get("run_id") == run_id]

    def write_summary(self, run_id: str) -> Path:
        """Write the paired human summary for one run. Read THIS, not the JSONL."""
        rows = self.entries_for_run(run_id)
        live = liveness_check(rows)
        stages = [str(r.get("pipeline_stage") or "?") for r in rows]
        agents = sorted({str(r.get("agent") or "unknown") for r in rows})
        session_ids = sorted(
            {str(r.get("agent_session_id") or "") for r in rows if r.get("agent_session_id")}
        )
        lines = [
            "# Cost Ledger -- anchors (Phase 1a.1)",
            "",
            f"- run_id: `{run_id}`",
            f"- anchors: `{live.total_anchors}`  stages: `{', '.join(stages) or 'none'}`",
            f"- agent(s): `{', '.join(agents) or 'none'}`",
            f"- agent_session_id(s): `{', '.join(session_ids) or 'none'}`",
            f"- liveness: `{'ok' if live.ok else 'FAIL'}` -- {live.reason}",
            f"- rows without a session id (join-coverage): `{live.without_session_id}`",
            "",
            "All token/cost columns are NULL by design at anchor time "
            "(`capture_method: anchor_only`, `cost_source: unreconciled`). Phase "
            "1a.2 fills them by joining agent OTel token metrics on "
            "`agent_session_id`. Do not render a cost number from this file.",
            "",
            "> Phase 1a.2 gate: the first live OTLP capture MUST byte-match "
            "`CLAUDE_CODE_SESSION_ID` against the OTel `session.id` span attribute "
            "before any ingest is trusted. If it does not match, 1a.2 stops and the "
            "join key is revisited.",
            "",
        ]
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text("\n".join(lines), encoding="utf-8")
        return self.summary_path


def assert_ledger_active(
    ledger: CostLedger, run_id: str, *, threshold: float = UNATTRIBUTED_FAIL_FRACTION
) -> LivenessResult:
    """Self-check that capture is live for ``run_id``. Raises ``RuntimeError`` when
    the liveness gate fails -- the capture analogue of ``assert_installed()``.
    A new safety/enforcement mechanism is not-done until it asserts it is live.
    """
    result = liveness_check(ledger.entries_for_run(run_id), threshold=threshold)
    if not result.ok:
        raise RuntimeError(f"cost-ledger capture not live for run {run_id}: {result.reason}")
    return result


# --------------------------------------------------------------------------- #
# A2 -- shared decorator for individually-invoked console-scripts (Phase 1a.2a). #
# Marks the entry point with __anchored__ (a fact the coverage test reads at      #
# IMPORT time, never an AST guess or a live run) and writes one anchor per call.  #
# --------------------------------------------------------------------------- #
def _workspace_from_argv(argv: list[str]) -> str:
    """Extract the ``--workspace <value>`` (or ``--workspace=<value>``) from argv.
    Empty string when absent -- a workspace-less invocation has nothing to key."""
    for i, tok in enumerate(argv):
        if tok == "--workspace" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--workspace="):
            return tok.split("=", 1)[1]
    return ""


def _try_anchor(
    command_name: str,
    argv: Optional[list[str]] = None,
    *,
    started_at: str = "",
    finished_at: str = "",
) -> None:
    """Write one anchor row for an individually-invoked command. Resolves the
    workspace from argv (``sys.argv`` when not given). Errors are SURFACED to
    stderr and never swallowed -- the CLI analogue of the seam's ``_anchor_errors``
    -- so a decorated-but-silently-failing command cannot both pass coverage and
    write nothing (that gap is then caught by the liveness gate: zero rows)."""
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        workspace = _workspace_from_argv(args)
        if not workspace:
            return  # workspace-less invocation: nothing to key a row to
        env = os.environ
        agent_session_id, _ = resolve_agent_session(env)
        run_id, run_id_source = run_id_for(agent_session_id, workspace)
        CostLedger(ledger_dir_for(workspace)).append(
            build_anchor(
                run_id=run_id,
                workspace_id=workspace,
                pipeline_stage=command_name,
                env=env,
                configured_agent=env.get("AUTORESEARCH_MAIN_AGENT"),
                run_id_source=run_id_source,
                started_at=started_at or datetime.now(timezone.utc).isoformat(),
                finished_at=finished_at,
            )
        )
    except Exception as exc:  # surfaced, never swallowed
        print(
            f"[cost-ledger] anchor write failed for {command_name}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def anchored(command_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a console-script entry point as cost-anchored and write an anchor on
    each call. ``__anchored__`` is the marker the coverage test reads at import.

    The anchor is written in a ``finally`` so it captures ``started_at`` +
    ``finished_at`` around the command AND still records a row when the command
    raises/exits (the "failing command still anchors" property, preserved from the
    seam). A hard-killed/hung process leaves no row -- caught by the liveness gate,
    not silently."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = datetime.now(timezone.utc).isoformat()
            try:
                return fn(*args, **kwargs)
            finally:
                _try_anchor(
                    command_name,
                    started_at=started,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )

        wrapper.__anchored__ = True  # type: ignore[attr-defined]
        wrapper.__anchored_command__ = command_name  # type: ignore[attr-defined]
        return wrapper

    return deco


# Console-scripts that legitimately emit no ledger row: no ``--workspace`` to key a
# row to, or out-of-launch-scope / dev-meta tooling. Each carries a REQUIRED reason;
# the coverage test forbids stale or reasonless entries, so exempting stays a
# visible, reviewed decision -- never where an inconvenient command disappears.
# NOTE: `medallion` and `harness` are deliberately NOT here -- they dispatch to
# --workspace subcommands (`medallion build --workspace`, `harness ... --workspace`)
# and ARE anchored; a naive "no --workspace literal in the module" grep would have
# mis-exempted them (medallion is the #2 most-invoked command).
EXEMPTIONS: dict[str, str] = {
    "loop": "out-of-launch-scope loop/interns subsystem (Phase 1a retarget)",
    "green-gate": "dev/CI test gate; not workspace pipeline work",
    "retrieve-docs": "repo-level doc retrieval; no --workspace to key",
    "generate-skill-adapters": "repo-level skill-adapter generation; no --workspace",
    "validate-git-hygiene": "repo-level git-hygiene check; no --workspace",
    "check-remote-execution-gate": "repo-level env-var gate check, no --workspace to key; "
    "called synchronously inside shell `&&` chains (pipeline_stages.py's DBT_BUILD_STAGE), "
    "anchoring overhead there would be pure waste on a check that must fail fast",
}


def entry_point_scripts() -> dict[str, str]:
    """Parse ``[project.scripts]`` -> ``{name: 'module:function'}`` from pyproject."""
    import tomllib

    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return dict((data.get("project") or {}).get("scripts") or {})


__all__ = [
    "EXEMPTIONS",
    "SCHEMA_VERSION",
    "UNATTRIBUTED_FAIL_FRACTION",
    "AnchorEntry",
    "CostLedger",
    "LivenessResult",
    "anchored",
    "assert_ledger_active",
    "build_anchor",
    "detect_agent",
    "entry_point_scripts",
    "ledger_dir_for",
    "liveness_check",
    "new_run_id",
    "resolve_agent",
    "resolve_agent_session",
    "run_id_for",
]
