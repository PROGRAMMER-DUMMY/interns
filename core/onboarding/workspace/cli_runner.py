"""Shared runner for governed CLI commands.

Every ``apply-*`` / ``finalize-*`` / ``prepare-*`` CLI in this repo follows
the same envelope:

* serialise mutations on a single workspace with [[workspace_lock]];
* timestamp + duration via [[time_command]];
* deterministic op-id + replay-suppression via [[idempotency]] (for apply/
  finalize commands that mutate accepted decisions);
* trajectory event recording before/after the call.

This module factors that envelope out of every CLI module so each command
becomes a short adapter that calls the workflow function. New commands
should call :func:`run_workspace_command` rather than re-implementing the
boilerplate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from core.governance.op_signals import derive_op_signals, signals_to_skills
from core.observability.events import time_command
from core.onboarding.harness.trajectory_recorder import record_trajectory_event_safe
from core.onboarding.workspace.idempotency import (
    compute_op_id,
    get_applied_op,
    record_op,
)
from core.storage.workspace_lock import WorkspaceLockTimeout, workspace_lock

# Commands that mutate user-decided/accepted state. The reliability hook only
# snapshots+verifies user state around these (prepare/read commands cannot wipe
# a decision, so skipping them avoids needless work). Generic prefixes, not
# specific command names.
_MUTATING_PREFIXES = (
    "apply-",
    "finalize-",
    "onboard-",
    "resolve-",
    "generate-",
    "build-",
    "kickstart-",
    "run-kpi-pipeline",
    "workspace-flow",
)


def _is_mutating(command: str) -> bool:
    name = str(command or "").strip().lower()
    return any(name.startswith(prefix) for prefix in _MUTATING_PREFIXES)


def resolve_workspace_path(repo_root: str | Path, workspace: str | Path) -> Path:
    return (Path(repo_root) / workspace).resolve()


def _snapshot_state_safe(workspace_path: Path) -> Any:
    """Snapshot user-decided state for the reliability hook. Never raises."""
    try:
        from core.storage.workspace_layout import WorkspaceLayout
        from tools.workflow_state_tripwire import snapshot_user_state

        return snapshot_user_state(WorkspaceLayout(project_root=workspace_path))
    except Exception:  # pragma: no cover - reliability hook is best-effort
        return None


def _verify_state_safe(workspace_path: Path, prior: Any) -> list[str]:
    """Return user-state regressions since ``prior``. Never raises."""
    try:
        from core.storage.workspace_layout import WorkspaceLayout
        from tools.workflow_state_tripwire import verify_user_state_preserved

        diff = verify_user_state_preserved(
            WorkspaceLayout(project_root=workspace_path), prior
        )
        return list(diff.regressions) + [
            f"removed applied-op record: {op}" for op in diff.removed_ops
        ]
    except Exception:  # pragma: no cover - reliability hook is best-effort
        return []


def _payload_from_result(result: Any) -> dict[str, Any]:
    if result is None:
        return {}
    if hasattr(result, "summary"):
        try:
            payload = result.summary()
        except Exception:  # pragma: no cover - defensive
            payload = None
        if isinstance(payload, dict):
            return payload
    if isinstance(result, dict):
        return result
    return {"result": str(result)}


def run_workspace_command(
    *,
    command: str,
    workspace: str,
    repo_root: str | Path,
    fn: Callable[[], Any],
    op_args: dict[str, Any] | None = None,
    allow_replay: bool = True,
    decision: str | None = None,
    metadata: dict[str, Any] | None = None,
    artifact: str | None = None,
    validation: str | None = None,
    record_idempotent: bool = False,
) -> int:
    """Run a workspace-mutating CLI command with the standard envelope.

    Args:
        command: Stable CLI name (e.g. ``apply-relationship-answer``). Used
            for event emission, trajectory recording, and op-id derivation.
        workspace: Workspace path relative to ``repo_root``.
        repo_root: Project root. Defaults provided by each adapter.
        fn: Zero-arg callable that performs the workflow work. Its return
            value is JSON-printed (preferred shape: dataclass with
            ``.summary()`` or a plain ``dict``).
        op_args: Stable kwargs feeding ``compute_op_id`` when
            ``record_idempotent=True``. When omitted with idempotency
            enabled, all keys from ``metadata`` plus ``workspace`` are used.
        allow_replay: When False and a prior op with the same id exists,
            return the prior payload without re-running ``fn``.
        decision: Optional decision label for trajectory recording.
        metadata: Optional metadata merged into the trajectory + event.
        artifact / validation: Optional pointers stored on the success
            trajectory event.
        record_idempotent: When True, derive an op-id and record an
            ``AppliedOp`` row after a successful run. Apply/finalize
            commands should set this; prepare commands typically should not.

    Returns:
        Process exit code: 0 on success / idempotent replay, 2 if blocked
        by the workspace lock. Other exceptions propagate after logging.
    """

    workspace_path = resolve_workspace_path(repo_root, workspace)
    base_metadata = dict(metadata or {})
    base_metadata.setdefault("tool", command)

    op_id: str | None = None
    if record_idempotent:
        idempotent_args = op_args if op_args is not None else {
            **base_metadata,
            "workspace": workspace,
        }
        op_id = compute_op_id(command, **idempotent_args)
        base_metadata["op_id"] = op_id
        if not allow_replay:
            prior = get_applied_op(workspace_path, op_id)
            if prior is not None:
                print(
                    json.dumps(
                        {
                            "status": "idempotent_replay",
                            "op_id": op_id,
                            "previously_applied_at": prior.applied_at,
                            "result": prior.payload,
                            "note": (
                                "Skipped re-apply because this exact call was "
                                "already applied. Pass --allow-replay to force "
                                "re-execution."
                            ),
                        },
                        indent=2,
                    )
                )
                return 0

    record_trajectory_event_safe(
        repo_root,
        workspace,
        event_type="tool_start",
        status="running",
        summary=f"Running {command}.",
        decision=decision,
        metadata=base_metadata,
    )

    # Reliability hook (live tripwire): snapshot user-decided state before a
    # mutating op so we can detect a silent wipe afterwards. Best-effort; never
    # blocks the command.
    state_before = _snapshot_state_safe(workspace_path) if _is_mutating(command) else None

    try:
        with time_command(workspace_path, command) as event_details:
            for key, value in base_metadata.items():
                event_details.setdefault(key, value)
            with workspace_lock(workspace_path):
                result = fn()
    except WorkspaceLockTimeout as exc:
        record_trajectory_event_safe(
            repo_root,
            workspace,
            event_type="tool_result",
            status="failed",
            summary=f"{command} blocked by workspace lock",
            decision=decision,
            metadata={**base_metadata, "error": str(exc)},
        )
        print(
            json.dumps(
                {"error": "workspace_lock_timeout", "detail": str(exc)},
                indent=2,
            )
        )
        return 2
    except Exception as exc:
        record_trajectory_event_safe(
            repo_root,
            workspace,
            event_type="tool_result",
            status="failed",
            summary=f"{command} failed: {type(exc).__name__}",
            decision=decision,
            metadata={**base_metadata, "error": str(exc)},
        )
        raise

    payload = _payload_from_result(result)

    # Reliability hook (continued): verify user-decided state survived. A
    # regression (a user_confirmed/approved/rejected marker silently wiped) is
    # surfaced on the envelope, not hard-blocked -- the operator sees it and can
    # re-confirm. This is the guard the tripwire was built for, now on the LIVE
    # path instead of only in CI.
    user_state_changed = False
    if state_before is not None:
        regressions = _verify_state_safe(workspace_path, state_before)
        if regressions:
            user_state_changed = True
            payload.setdefault("reliability", {})
            if isinstance(payload.get("reliability"), dict):
                payload["reliability"]["user_state_regressions"] = regressions
                payload["reliability"]["note"] = (
                    "[x] reliability_regression: a user-decided marker changed during "
                    "this op; confirm it was intended before trusting the result."
                )
            record_trajectory_event_safe(
                repo_root,
                workspace,
                event_type="reliability_regression",
                status="warning",
                summary=f"{command} changed user-decided state",
                metadata={**base_metadata, "regressions": regressions},
            )

    # Activation hook: read normalized op-signals from the result and, when any
    # are actionable (low-confidence / blocked / empty-panel / stuck / state
    # change), surface the matching skill + specialist so the orchestrator can
    # act -- including at dead-ends, where stage routing would otherwise be
    # silent. Additive: never alters the command's own payload fields.
    try:
        if user_state_changed:
            payload["user_state_changed"] = True
        signals = derive_op_signals(command, payload)
        if signals.actionable:
            payload["op_signals"] = signals.to_dict()
            suggested = signals_to_skills(signals)
            if suggested:
                payload["suggested_skills"] = suggested
    except Exception:  # pragma: no cover - activation is advisory, never fatal
        pass

    if record_idempotent and op_id is not None:
        record_op(
            workspace_path,
            op_id=op_id,
            command=command,
            payload=payload,
        )

    record_trajectory_event_safe(
        repo_root,
        workspace,
        event_type="tool_result",
        status="ok",
        summary=f"Ran {command}.",
        decision=decision,
        artifact=artifact,
        validation=validation,
        metadata={**base_metadata, "result": payload},
    )
    print(json.dumps(payload, indent=2))
    return 0


__all__ = ["resolve_workspace_path", "run_workspace_command"]
