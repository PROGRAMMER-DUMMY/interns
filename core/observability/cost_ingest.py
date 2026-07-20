"""core/observability/cost_ingest.py -- Phase 1a.2b: fill agent token spend from
the Claude Code session transcript (the INGEST half; the anchor half is
cost_ledger.py).

Source (decided in docs/core_audit/cost-ledger.md): Claude Code writes a per-session
transcript to ``~/.claude/projects/<cwd>/<session-uuid>.jsonl`` as a byproduct of
running, keyed by the SAME id we anchor on (``CLAUDE_CODE_SESSION_ID`` ==
``sessionId`` field == filename), carrying per-turn ``message.usage`` with
mutually-exclusive token buckets. Chosen over an OTLP collector: no new component to
keep alive, and no inclusive/exclusive normalization needed.

PRIVACY BOUNDARY (hard): this reads a file full of conversation content but emits
ONLY numbers, timestamps, and the session id. Message text / prompts / responses /
tool results are NEVER extracted, stored, or surfaced -- see ``_usage_from_record``,
which pulls only the four integer usage keys.

LIVENESS: the transcript is a byproduct of the agent running, so its ABSENCE is a
loud signal (that session's spend is unrecoverable), never a quiet zero-fill. A
session that has anchors but no transcript, or a transcript with zero usage, makes
``IngestResult.ok`` False and is listed explicitly.

ATTRIBUTION: run-level. A session's totals attribute to its run when the session
holds exactly one run_id; a session spanning multiple runs is recorded as
``session_spans_multiple_runs`` (unsplit -- stage/temporal split is deferred).

Trade-off: Claude Code-SPECIFIC. Codex/Gemini need their own readers (Gemini stays
honestly-degraded). The OTel span ``session.id`` was never verified -- the transcript
made it unnecessary.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from core.observability.cost_ledger import CostLedger, anchored, ledger_dir_for

USAGE_SCHEMA_VERSION = 1

# The ONLY fields extracted from a transcript record. Everything else in the record
# (content, prompts, tool results, cwd, ...) is ignored -- this list is the privacy
# boundary in code form.
_USAGE_INT_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def transcript_root(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / ".claude" / "projects"


def find_transcript(session_id: str, home: Optional[Path] = None) -> Optional[Path]:
    """Locate ``<session_id>.jsonl`` under any project dir. None when absent."""
    root = transcript_root(home)
    if not session_id or not root.exists():
        return None
    matches = sorted(root.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def _usage_from_record(obj: Mapping[str, Any]) -> Optional[dict[str, int]]:
    """Return only the integer usage buckets from one transcript record, or None.
    NOTHING else is read -- this is the privacy boundary."""
    msg = obj.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else obj.get("usage")
    if not isinstance(usage, dict):
        return None
    return {k: int(usage.get(k) or 0) for k in _USAGE_INT_KEYS}


@dataclass
class SessionUsage:
    session_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    turns: int = 0
    first_ts: str = ""
    last_ts: str = ""

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


def _parse_ts(s: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (``Z`` or ``+00:00``) to an aware UTC datetime.
    None on failure. A naive result is assumed UTC so window comparisons never mix
    aware/naive datetimes (which would raise)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def extract_session_usage(
    path: Path,
    session_id: str,
    window: Optional[tuple[datetime, datetime]] = None,
) -> SessionUsage:
    """Sum the exclusive token buckets over a transcript. Reads numbers +
    timestamps only; never message content.

    When ``window`` is given, only turns whose timestamp falls inside ``[lo, hi]``
    are summed -- the temporal-attribution path. A turn with a missing/unparseable
    timestamp is EXCLUDED under a window (it cannot be placed in time) but included
    when ``window is None`` -- the unchanged default (session total)."""
    su = SessionUsage(session_id=session_id)
    lo = hi = None
    if window is not None:
        lo, hi = window
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = _usage_from_record(obj)
        if usage is None:
            continue
        ts = str(obj.get("timestamp") or "")
        if window is not None:
            dt = _parse_ts(ts)
            if dt is None or dt < lo or dt > hi:
                continue
        su.input_tokens += usage["input_tokens"]
        su.output_tokens += usage["output_tokens"]
        su.cache_creation_input_tokens += usage["cache_creation_input_tokens"]
        su.cache_read_input_tokens += usage["cache_read_input_tokens"]
        su.turns += 1
        if ts:
            su.first_ts = ts if not su.first_ts else min(su.first_ts, ts)
            su.last_ts = ts if not su.last_ts else max(su.last_ts, ts)
    return su


def run_window(
    anchors: Iterable[Mapping[str, Any]]
) -> Optional[tuple[datetime, datetime]]:
    """A run's temporal window ``[min(started_at), max(finished_at)]`` over its
    anchors. ``None`` when no anchor carries usable timestamps -- temporal
    attribution is then unavailable for that run. This should not happen since
    1a.2b writes ``finished_at`` unconditionally (that data-collection decision is
    exactly what makes temporal attribution possible now), but it is handled
    honestly rather than assumed away."""
    starts = [t for t in (_parse_ts(str(a.get("started_at") or "")) for a in anchors) if t]
    finishes = [t for t in (_parse_ts(str(a.get("finished_at") or "")) for a in anchors) if t]
    if not starts or not finishes:
        return None
    return (min(starts), max(finishes))


def detect_anchor_dupes(anchors: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Find anchor rows sharing ``(run_id, pipeline_stage)`` -- the known in-process
    double-fire. Same session+agent => info; a differing session/agent on the same
    run+stage is unexpected => warning. Logged, never silently collapsed (§4)."""
    seen: dict[tuple[str, str], Mapping[str, Any]] = {}
    events: list[dict[str, Any]] = []
    for a in anchors:
        key = (str(a.get("run_id") or ""), str(a.get("pipeline_stage") or ""))
        prev = seen.get(key)
        if prev is None:
            seen[key] = a
            continue
        if str(a.get("agent_session_id") or "") != str(prev.get("agent_session_id") or ""):
            kind, level = "cross_session_same_run_stage", "warning"
        elif str(a.get("agent") or "") != str(prev.get("agent") or ""):
            kind, level = "cross_agent_same_run_stage", "warning"
        else:
            kind, level = "same_stage_double_fire", "info"
        events.append(
            {"run_id": key[0], "pipeline_stage": key[1], "kind": kind, "level": level}
        )
    return events


@dataclass
class IngestResult:
    ok: bool
    sessions_total: int = 0
    sessions_filled: int = 0
    temporal_runs: int = 0  # per-run temporal_approximate records written
    zero_window_runs: list[str] = field(default_factory=list)  # runs whose window caught 0 turns (undercount extreme)
    missing_transcript: list[dict[str, Any]] = field(default_factory=list)
    empty_transcript: list[dict[str, Any]] = field(default_factory=list)
    unattributable_anchors: int = 0  # anchors with no agent_session_id (anchor gate owns these)
    dedupe_events: list[dict[str, Any]] = field(default_factory=list)
    usage_path: str = ""
    summary_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# The direction-of-error, in the record itself (Requirement 1: the approximation is
# self-describing in the schema, not just the docs). Mislabeling was half of why the
# 474x over-attribution read as plausible; a reader must not mistake this for exact.
_TEMPORAL_UNDERCOUNT_NOTE = (
    "transcript turns intersected with the run's anchor window "
    "[min started_at, max finished_at]. SYSTEMATICALLY UNDERCOUNTS: the agent turn "
    "that launches a stage is timestamped just before the subprocess's started_at "
    "and the turn that reacts to it just after finished_at, so boundary driving-turns "
    "fall outside the window; the undercount grows for runs with few stages (a "
    "multi-stage run catches its inter-stage turns; a single-stage run can catch "
    "near-zero). Approximate -- never an exact per-pipeline cost."
)


def _session_total_record(
    su: SessionUsage, run_ids: list[str], workspaces: list[str]
) -> dict[str, Any]:
    """The whole-session token total. Deliberately labeled ``session_total``, NOT
    ``run`` -- attributing a session total to a single ``run_id`` (the old label) is
    exactly the 474x conflation. This row is context/liveness, never a pipeline cost."""
    return {
        "artifact_type": "cost_ledger.usage",
        "schema_version": USAGE_SCHEMA_VERSION,
        "agent_session_id": su.session_id,
        "attribution": "session_total",
        "run_ids": run_ids,
        "workspaces": workspaces,
        "input_tokens": su.input_tokens,
        "output_tokens": su.output_tokens,
        "cached_tokens": su.cache_read_input_tokens + su.cache_creation_input_tokens,
        "thinking_tokens": None,  # not separately reported by the transcript
        # ... plus the exact Anthropic split, so 1a.2c can price cache-write vs -read
        "cache_creation_input_tokens": su.cache_creation_input_tokens,
        "cache_read_input_tokens": su.cache_read_input_tokens,
        "turns": su.turns,
        "first_ts": su.first_ts,
        "last_ts": su.last_ts,
        "cost_usd": None,
        "cost_source": "unreconciled",  # local $ estimate needs real prices (1a.2c)
        "capture_method": "transcript_ingest",
        "note": (
            "whole-session token total across all runs in this agent session; NOT a "
            "single pipeline's cost -- see the temporal_approximate rows for per-run "
            "approximate shares"
        ),
    }


def _temporal_record(
    su: SessionUsage,
    run_id: str,
    workspaces: list[str],
    window: tuple[datetime, datetime],
) -> dict[str, Any]:
    """A run's temporally-scoped token spend: the transcript turns inside the run's
    anchor window. Labeled ``temporal_approximate`` with its window bounds and a
    direction-of-error note, so it can never be mistaken for an exact figure."""
    lo, hi = window
    return {
        "artifact_type": "cost_ledger.usage",
        "schema_version": USAGE_SCHEMA_VERSION,
        "agent_session_id": su.session_id,
        "attribution": "temporal_approximate",
        "run_id": run_id,
        "workspaces": workspaces,
        "window_lo": lo.isoformat(),
        "window_hi": hi.isoformat(),
        "window_seconds": round((hi - lo).total_seconds(), 1),
        "input_tokens": su.input_tokens,
        "output_tokens": su.output_tokens,
        "cached_tokens": su.cache_read_input_tokens + su.cache_creation_input_tokens,
        "thinking_tokens": None,
        "cache_creation_input_tokens": su.cache_creation_input_tokens,
        "cache_read_input_tokens": su.cache_read_input_tokens,
        "turns_in_window": su.turns,
        "cost_usd": None,
        "cost_source": "unreconciled",
        "capture_method": "transcript_ingest_temporal",
        "attribution_note": _TEMPORAL_UNDERCOUNT_NOTE,
    }


def _temporal_unavailable_record(
    session_id: str, run_id: str, workspaces: list[str]
) -> dict[str, Any]:
    """Emitted when a run's anchors carry no usable timestamps, so no temporal
    window can be built. Honest gap, not a silent zero -- token columns are null."""
    return {
        "artifact_type": "cost_ledger.usage",
        "schema_version": USAGE_SCHEMA_VERSION,
        "agent_session_id": session_id,
        "attribution": "temporal_unavailable",
        "run_id": run_id,
        "workspaces": workspaces,
        "reason": "no usable started_at/finished_at on the run's anchors",
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "turns_in_window": 0,
        "cost_usd": None,
        "cost_source": "unreconciled",
        "capture_method": "transcript_ingest_temporal",
    }


class CostIngest:
    """Reads a workspace's anchor ledger, joins Claude Code transcript token usage
    by ``agent_session_id``, and writes a run/session-level usage artifact."""

    def __init__(self, ledger: CostLedger, *, home: Optional[Path] = None) -> None:
        self.ledger = ledger
        self.home = home

    @property
    def usage_path(self) -> Path:
        return self.ledger.ledger_dir / "usage.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.ledger.ledger_dir / "usage.md"

    def run(self) -> IngestResult:
        anchors = self.ledger.read_entries()
        dedupe_events = detect_anchor_dupes(anchors)

        by_session: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        unattributable = 0
        for a in anchors:
            sid = str(a.get("agent_session_id") or "")
            if not sid:
                unattributable += 1
                continue
            by_session[sid].append(a)

        session_records: list[dict[str, Any]] = []
        temporal_records: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        empty: list[dict[str, Any]] = []
        zero_window_runs: list[str] = []
        for sid, rows in sorted(by_session.items()):
            run_ids = sorted({str(r.get("run_id") or "") for r in rows})
            workspaces = sorted({str(r.get("workspace_id") or "") for r in rows})
            tpath = find_transcript(sid, self.home)
            if tpath is None:
                missing.append({"agent_session_id": sid, "run_ids": run_ids, "reason": "transcript_not_found"})
                continue
            su_full = extract_session_usage(tpath, sid)
            if su_full.turns == 0 or su_full.total_tokens == 0:
                empty.append({"agent_session_id": sid, "reason": "transcript_has_no_usage"})
                continue
            # (1) whole-session total -- context, honestly labeled (NOT a run cost).
            session_records.append(_session_total_record(su_full, run_ids, workspaces))
            # (2) per-run temporal_approximate -- the scoped, approximate number.
            for rid in run_ids:
                run_rows = [r for r in rows if str(r.get("run_id") or "") == rid]
                run_ws = sorted({str(r.get("workspace_id") or "") for r in run_rows})
                win = run_window(run_rows)
                if win is None:
                    temporal_records.append(_temporal_unavailable_record(sid, rid, run_ws))
                    continue
                su_win = extract_session_usage(tpath, sid, window=win)
                temporal_records.append(_temporal_record(su_win, rid, run_ws, win))
                if su_win.turns == 0:
                    zero_window_runs.append(rid)

        records = session_records + temporal_records
        # LIVENESS: a session that has anchors but no usable transcript is loud.
        # (A zero-turn temporal window is the undercount extreme, flagged but not a
        # liveness failure -- the transcript IS present; it is a magnitude signal for
        # the human acceptance read, not a capture failure.)
        ok = not missing and not empty
        self._write(session_records, temporal_records, missing, empty,
                    dedupe_events, ok, zero_window_runs)
        return IngestResult(
            ok=ok,
            sessions_total=len(by_session),
            sessions_filled=len(session_records),
            temporal_runs=len(temporal_records),
            zero_window_runs=zero_window_runs,
            missing_transcript=missing,
            empty_transcript=empty,
            unattributable_anchors=unattributable,
            dedupe_events=dedupe_events,
            usage_path=str(self.usage_path.name),
            summary_path=str(self.summary_path.name),
        )

    def _write(
        self,
        session_records: list[dict[str, Any]],
        temporal_records: list[dict[str, Any]],
        missing: list[dict[str, Any]],
        empty: list[dict[str, Any]],
        dedupe_events: list[dict[str, Any]],
        ok: bool,
        zero_window_runs: list[str],
    ) -> None:
        self.ledger.ledger_dir.mkdir(parents=True, exist_ok=True)
        with self.usage_path.open("w", encoding="utf-8") as fh:
            for rec in session_records + temporal_records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        lines = [
            "# Cost Ledger -- token usage (Phase 1a.2b ingest + temporal attribution)",
            "",
            f"- liveness: `{'ok' if ok else 'FAIL'}`",
            f"- sessions filled: `{len(session_records)}`  missing transcript: "
            f"`{len(missing)}`  empty transcript: `{len(empty)}`",
            "",
        ]
        if missing or empty:
            lines.append("## Unrecoverable spend (LOUD -- not a quiet zero-fill)")
            lines.append("")
            for m in missing:
                lines.append(f"- [x] no transcript for session `{m['agent_session_id']}` "
                             f"(runs: {', '.join(m['run_ids']) or 'none'})")
            for e in empty:
                lines.append(f"- [x] transcript has no usage for session `{e['agent_session_id']}`")
            lines.append("")
        lines.append("## Session totals (whole agent session -- NOT a pipeline cost)")
        lines.append("")
        for rec in session_records:
            lines.append(
                f"- `{rec['agent_session_id']}` (session_total, {len(rec['run_ids'])} run(s)): "
                f"in={rec['input_tokens']} out={rec['output_tokens']} "
                f"cached={rec['cached_tokens']} turns={rec['turns']}  cost=`{rec['cost_source']}`"
            )
        lines.append("")
        lines.append("## Per-run temporal attribution (approximate -- undercounts; read the magnitude)")
        lines.append("")
        for rec in temporal_records:
            if rec["attribution"] == "temporal_unavailable":
                lines.append(f"- [x] run `{rec['run_id']}`: temporal_unavailable -- {rec['reason']}")
                continue
            flag = "  [~] 0 turns in window (undercount extreme)" if rec["turns_in_window"] == 0 else ""
            lines.append(
                f"- run `{rec['run_id']}` (temporal_approximate): window={rec['window_seconds']}s "
                f"in={rec['input_tokens']} out={rec['output_tokens']} cached={rec['cached_tokens']} "
                f"turns_in_window={rec['turns_in_window']}{flag}"
            )
        if dedupe_events:
            lines.extend(["", "## Dedupe events (logged, not silent)"])
            for d in dedupe_events:
                lines.append(f"- `{d['level']}` {d['kind']}: run `{d['run_id']}` stage `{d['pipeline_stage']}`")
        lines.append("")
        lines.append(
            "`temporal_approximate` intersects transcript turns with each run's anchor "
            "window and SYSTEMATICALLY UNDERCOUNTS (see `attribution_note`); it is never "
            "exact. `session_total` is the whole agent session, not one pipeline. "
            "**Acceptance for the temporal slice is a HUMAN magnitude read of these "
            "numbers, not a passing test** -- no automated assertion catches magnitude, "
            "and the check is on TOKEN COUNTS, never dollars (the number is ~99.9% "
            "cache-read, so a USD figure is uninformative about scope at any granularity). "
            "**Driving-pattern sensitive:** the SAME pipeline yields different numbers by "
            "how the agent drove it -- run entirely within one agent turn, the window "
            "catches ZERO turns (`turns_in_window: 0`, flagged above, an undercount and "
            "NOT a cheap run); driven across several turns it catches the inter-stage "
            "turns. Before comparing two runs' costs, read `turns_in_window` and "
            "`window_seconds` first -- a low or zero count is an undercount, not a bargain. "
            "All values are token counts + timestamps only; no conversation content is "
            "read or stored. `cost_source: unreconciled` -- USD requires real prices "
            "(1a.2c), never rendered as reconciled here."
        )
        lines.append("")
        self.summary_path.write_text("\n".join(lines), encoding="utf-8")


@anchored("cost-ledger-ingest")
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cost-ledger-ingest",
        description="Fill agent token spend into the cost ledger from Claude Code transcripts.",
    )
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--home", default="", help="Override home dir (for testing).")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else None
    ledger = CostLedger(ledger_dir_for(args.workspace))
    result = CostIngest(ledger, home=home).run()
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


__all__ = [
    "CostIngest",
    "IngestResult",
    "SessionUsage",
    "detect_anchor_dupes",
    "extract_session_usage",
    "find_transcript",
    "main",
    "run_window",
    "transcript_root",
]
