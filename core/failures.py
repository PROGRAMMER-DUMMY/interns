from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class FailureKind(str, Enum):
    USER_INPUT_BLOCKER = "user_input_blocker"
    VALIDATION_BLOCKER = "validation_blocker"
    MISSING_DEPENDENCY = "missing_dependency"
    REMOTE_EXECUTION_DENIED = "remote_execution_denied"
    REMOTE_EXECUTION_UNAVAILABLE = "remote_execution_unavailable"
    INTERNAL_BUG = "internal_bug"
    NON_FATAL_TELEMETRY = "non_fatal_telemetry"
    UI_RENDERING = "ui_rendering"


@dataclass(frozen=True)
class StructuredFailure:
    kind: FailureKind
    message: str
    stage: str
    next_command: str = ""
    retryable: bool = False
    details: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind.value,
            "stage": self.stage,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.next_command:
            data["next_command"] = self.next_command
        if self.details:
            data["details"] = self.details
        return data


class WorkflowBlockedError(RuntimeError):
    def __init__(self, failure: StructuredFailure):
        super().__init__(failure.message)
        self.failure = failure


def validation_blocker(stage: str, message: str, *, next_command: str = "") -> StructuredFailure:
    return StructuredFailure(
        kind=FailureKind.VALIDATION_BLOCKER,
        stage=stage,
        message=message,
        next_command=next_command,
        retryable=False,
    )


def remote_denied(stage: str, message: str, *, next_command: str = "") -> StructuredFailure:
    return StructuredFailure(
        kind=FailureKind.REMOTE_EXECUTION_DENIED,
        stage=stage,
        message=message,
        next_command=next_command,
        retryable=False,
    )


def remote_unavailable(stage: str, message: str) -> StructuredFailure:
    return StructuredFailure(
        kind=FailureKind.REMOTE_EXECUTION_UNAVAILABLE,
        stage=stage,
        message=message,
        retryable=True,
    )


def missing_dependency(stage: str, message: str, *, next_command: str = "") -> StructuredFailure:
    return StructuredFailure(
        kind=FailureKind.MISSING_DEPENDENCY,
        stage=stage,
        message=message,
        next_command=next_command,
        retryable=False,
    )


def internal_bug(stage: str, message: str) -> StructuredFailure:
    return StructuredFailure(
        kind=FailureKind.INTERNAL_BUG,
        stage=stage,
        message=message,
        retryable=False,
    )
