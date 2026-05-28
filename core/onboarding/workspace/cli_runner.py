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

from core.observability.events import time_command
from core.onboarding.harness.trajectory_recorder import record_trajectory_event_safe
from core.onboarding.workspace.idempotency import (
    compute_op_id,
    get_applied_op,
    record_op,
)
from core.storage.workspace_lock import WorkspaceLockTimeout, workspace_lock


def resolve_workspace_path(repo_root: str | Path, workspace: str | Path) -> Path:
    return (Path(repo_root) / workspace).resolve()


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
