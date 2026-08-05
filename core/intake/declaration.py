"""The source declaration: what the workspace's data is and where it lives.

Phase 0 of the cloud-first spine. The user's most important input -- "our data
is at <location>" -- previously had nowhere to go, so the agent improvised
un-governed SDK calls. This records it once, as a workspace fact, under
``workspace_settings.json``'s ``source_declaration`` key:

    {"type", "location", "format_hint", "credential_ref",
     "declared_by", "declared_at"}

``credential_ref`` is a NAME: a Databricks secret scope/key, an AWS profile, an
env-var name, a UC storage-credential name. Never a secret VALUE. That is
enforced here (:func:`validate`), not left to caller discipline, because this
file is read back into panels, reports and blueprints that a human reads.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.panel_contract import attach_stage_routing
from core.storage.atomic_io import write_text_atomic
from core.storage.workspace_layout import WorkspaceLayout
from core.storage.workspace_lock import workspace_lock

SETTINGS_KEY = "source_declaration"

# Connector types the platform can be pointed at. Object stores, a relational
# pull, a file drop, a queue, or catalog objects that already exist. `local_files`
# stays a first-class type (it is the `--local` dev mode), it just stops being
# the implicit default.
SOURCE_TYPES = (
    "s3",
    "adls",
    "gcs",
    "jdbc",
    "sftp",
    "kafka",
    "uc_existing",
    "local_files",
)

# A credential *reference* is an identifier. Anything that looks like credential
# MATERIAL is refused -- the value is never echoed back in the error, only the
# reason, so a rejected secret does not leak into the transcript that reported
# the rejection.
_CREDENTIAL_REF_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/\\-]{0,199}$")
_INLINE_CREDENTIAL = re.compile(r"://[^/\s]+:[^/\s]+@")
_SECRET_MATERIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^-----BEGIN"), "looks like a PEM private key"),
    (re.compile(r"^ey[A-Za-z0-9_-]{10,}\."), "looks like a JWT"),
    (re.compile(r"^(dapi|sk-|ghp_|gho_|github_pat_|xox[baprs]-)", re.I), "looks like an API token"),
    (re.compile(r"^(AKIA|ASIA)[A-Z0-9]{12,}$"), "looks like an AWS access key id"),
    (re.compile(r"^[A-Za-z0-9+/]{32,}={1,2}$"), "looks like a base64-encoded secret"),
    (_INLINE_CREDENTIAL, "looks like a connection string with an inline password"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|apikey|api_key)\s*[=:]"), "carries an inline credential value"),
)

# A schema registry endpoint is a URL, not a credential -- but a URL is also the
# easiest place to smuggle one, so the inline-credential form is refused here too.
_REGISTRY_URL_SHAPE = re.compile(r"^https?://[^\s]+$")


@dataclass(frozen=True)
class SourceDeclaration:
    type: str
    location: str
    format_hint: str = ""
    credential_ref: str = ""
    declared_by: str = ""
    declared_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Optional. `schema_registry_url` lets the ingestion generator decode Avro/
    # Protobuf values instead of casting them to raw strings; `one_shot` declares
    # a single historical load, which is the only case where a full-table pull
    # without an idempotency key is safe.
    schema_registry_url: str = ""
    one_shot: bool = False

    def __post_init__(self) -> None:
        # Normalize once, at construction, so validation and persistence see the
        # same value whichever path built it (CLI flags or a settings payload).
        for field_name, value in (
            ("type", str(self.type or "").strip().lower()),
            ("location", str(self.location or "").strip()),
            ("format_hint", str(self.format_hint or "").strip().lower()),
            ("credential_ref", str(self.credential_ref or "").strip()),
            ("declared_by", str(self.declared_by or "").strip()),
            ("schema_registry_url", str(self.schema_registry_url or "").strip()),
        ):
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "one_shot", _as_bool(self.one_shot))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SourceDeclaration":
        return cls(
            type=str(payload.get("type") or ""),
            location=str(payload.get("location") or ""),
            format_hint=str(payload.get("format_hint") or ""),
            credential_ref=str(payload.get("credential_ref") or ""),
            declared_by=str(payload.get("declared_by") or ""),
            declared_at=str(payload.get("declared_at") or "").strip()
            or datetime.now(timezone.utc).isoformat(),
            schema_registry_url=str(payload.get("schema_registry_url") or ""),
            one_shot=_as_bool(payload.get("one_shot")),
        )

    def validate(self) -> "SourceDeclaration":
        """Return self when the declaration is usable; raise ValueError otherwise."""
        if self.type not in SOURCE_TYPES:
            raise ValueError(
                f"unknown source type {self.type!r}; expected one of {', '.join(SOURCE_TYPES)}"
            )
        if not self.location:
            raise ValueError("source declaration needs a location (bucket/URI/JDBC url/catalog.schema/path)")
        if self.credential_ref:
            _assert_reference_not_secret(self.credential_ref)
        if self.schema_registry_url:
            _assert_registry_url(self.schema_registry_url)
        return self


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1"}
    return bool(value)


def _assert_registry_url(value: str) -> None:
    if _INLINE_CREDENTIAL.search(value):
        raise ValueError(
            "schema_registry_url carries an inline credential. Pass the endpoint "
            "only, and the NAME of the credential in credential_ref."
        )
    if not _REGISTRY_URL_SHAPE.match(value):
        raise ValueError(
            "schema_registry_url must be an http(s) URL, e.g. "
            "https://schema-registry.internal:8081"
        )


def _assert_reference_not_secret(value: str) -> None:
    for pattern, reason in _SECRET_MATERIAL_PATTERNS:
        if pattern.search(value):
            raise ValueError(
                f"credential_ref {reason}. Pass the NAME of the credential "
                "(secret scope/key, cloud profile, env-var name, UC storage "
                "credential), never the value itself."
            )
    if not _CREDENTIAL_REF_SHAPE.match(value):
        raise ValueError(
            "credential_ref must be a plain reference name (letters, digits, "
            "and _ . : @ / - ), not a value with spaces or quoting."
        )


def load_source_declaration(layout: WorkspaceLayout) -> SourceDeclaration | None:
    """The workspace's declared source, or None when it has not declared one."""
    payload = layout.load_settings().get(SETTINGS_KEY)
    if not isinstance(payload, dict):
        return None
    try:
        return SourceDeclaration.from_dict(payload)
    except Exception:  # pragma: no cover - defensive: malformed settings block
        return None


def save_source_declaration(
    layout: WorkspaceLayout, declaration: SourceDeclaration
) -> dict[str, Any]:
    """Validate and persist ``declaration``; returns a summary (no secrets)."""
    declaration.validate()
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    # Read-modify-write under the workspace lock (re-entrant, so nesting inside
    # run_workspace_command's lock is free) so a concurrent settings write
    # cannot lose the declaration.
    with workspace_lock(layout.project_root):
        settings = layout.load_settings()
        settings[SETTINGS_KEY] = declaration.to_dict()
        write_text_atomic(layout.workspace_settings, json.dumps(settings, indent=2) + "\n")
    return attach_stage_routing(
        {
            "settings_path": _rel(layout.workspace_settings, layout.project_root),
            "source_declaration": declaration.to_dict(),
        },
        "source_declaration",
    )


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "SETTINGS_KEY",
    "SOURCE_TYPES",
    "SourceDeclaration",
    "load_source_declaration",
    "save_source_declaration",
]
