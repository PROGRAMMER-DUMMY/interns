"""Contract schema versioning.

Each on-disk artifact under ``workspaces/<project>/interns/generated/contracts``
declares ``artifact_type`` and ``version``. This module is the central registry
for those declarations and for forward-migration functions between versions.

The registry is populated as a side effect of importing the writer modules:
each writer calls :func:`register_contract` (and optionally
:func:`register_migration`) at module load time. Readers call :func:`migrate`
to upgrade a payload to the current schema version before consuming it.

There is no built-in vocabulary or fallback: an artifact type that has not
been registered passes through :func:`migrate` unchanged. This matches the
repo-wide ``derive don't curate`` policy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

LOGGER = logging.getLogger(__name__)

Migration = Callable[[dict], dict]


@dataclass(frozen=True)
class ContractVersion:
    """Registered version metadata for a single artifact type."""

    artifact_type: str
    current_version: int
    minimum_readable_version: int = 1


_REGISTRY: dict[str, ContractVersion] = {}
_MIGRATIONS: dict[tuple[str, int, int], Migration] = {}


def register_contract(
    artifact_type: str,
    current_version: int,
    minimum_readable_version: int = 1,
) -> None:
    """Register (or overwrite) the version metadata for an artifact type."""
    _REGISTRY[artifact_type] = ContractVersion(
        artifact_type=artifact_type,
        current_version=current_version,
        minimum_readable_version=minimum_readable_version,
    )


def register_migration(
    artifact_type: str,
    from_version: int,
    to_version: int,
    migration: Migration,
) -> None:
    """Register a forward migration from ``from_version`` to ``to_version``."""
    _MIGRATIONS[(artifact_type, from_version, to_version)] = migration


def contract_version(artifact_type: str) -> ContractVersion | None:
    """Return the registered :class:`ContractVersion` or ``None``."""
    return _REGISTRY.get(artifact_type)


def known_artifacts() -> list[str]:
    """Return a sorted list of all registered artifact types."""
    return sorted(_REGISTRY.keys())


def migrate(payload: dict) -> dict:
    """Migrate ``payload`` forward to the current registered schema version.

    The payload must declare both ``artifact_type`` and ``version``. If the
    artifact type is not registered the payload is returned unchanged. If the
    payload's version exceeds the current registered version a warning is
    logged and the payload is returned unchanged (forward compatibility is the
    caller's problem). If the payload's version is below the registered
    minimum readable version a :class:`ValueError` is raised. Otherwise each
    intermediate ``(v, v+1)`` migration is applied in order; a missing step
    raises :class:`ValueError`.
    """
    artifact_type = payload.get("artifact_type")
    version = payload.get("version")
    if not isinstance(artifact_type, str) or not isinstance(version, int):
        return payload

    contract = _REGISTRY.get(artifact_type)
    if contract is None:
        return payload

    if version == contract.current_version:
        return payload
    if version > contract.current_version:
        LOGGER.warning(
            "artifact %s declares version %d but current is %d; "
            "returning payload unchanged",
            artifact_type,
            version,
            contract.current_version,
        )
        return payload
    if version < contract.minimum_readable_version:
        raise ValueError(
            f"artifact {artifact_type} version {version} is below "
            f"minimum_readable_version {contract.minimum_readable_version}"
        )

    current = payload
    for step in range(version, contract.current_version):
        next_version = step + 1
        key = (artifact_type, step, next_version)
        migration = _MIGRATIONS.get(key)
        if migration is None:
            raise ValueError(
                f"no migration registered for {artifact_type} "
                f"{step} -> {next_version}"
            )
        current = migration(current)
        current["version"] = next_version
    return current
