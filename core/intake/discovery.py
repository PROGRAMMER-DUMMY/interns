"""Read-only discovery: measure the declared source before asking anything.

Phase 0's second half. One scanner per connector, one output contract:
``workspaces/<ws>/interns/generated/intake/discovery.json``.

Two rules the rest of the spine depends on:

1. **No fabricated sizes.** The `rows * cols * 16` habit silently decided the
   engine tier. Here an unmeasured size is ``null`` plus an open question --
   never an estimate formula. ``working_set_estimate_bytes`` is emitted only
   when every discovered table carried a real measured size.
2. **A scanner never crashes the command.** A missing SDK, missing credentials
   or an unimplemented connector is a structured result naming what is needed,
   because "discovery blew up" is not information a user can act on.

Adding a connector = writing one function with the :data:`Scanner` signature
and registering it in :data:`SCANNERS`.
"""
from __future__ import annotations

import importlib.util
import json
import re
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core.intake.declaration import SourceDeclaration, load_source_declaration
from core.observability.log_redaction import redact
from core.onboarding.panel_contract import attach_stage_routing
from core.onboarding.sources.external_discovery import DATA_SUFFIXES
from core.storage.external_data import (
    is_within_allowed_roots,
    load_external_data_policy,
)
from core.storage.workspace_layout import WorkspaceLayout

DISCOVERY_VERSION = 1

STATUS_OK = "ok"
STATUS_UNSUPPORTED = "unsupported_yet"
STATUS_MISSING_TOOLING = "credential_or_tool_missing"
STATUS_BLOCKED = "blocked"
STATUS_NOT_DECLARED = "not_declared"

# How data shows up. MEASURED from file modification times where the source can
# be listed; `unknown` everywhere else. A guessed cadence would silently decide
# the velocity lane, which is the same failure mode as a fabricated byte size.
ARRIVAL_CONTINUOUS = "continuous"
ARRIVAL_PERIODIC = "periodic"
ARRIVAL_ONE_SHOT = "one_shot"
ARRIVAL_UNKNOWN = "unknown"

# ponytail: one documented threshold, not a model. Files whose median gap is
# under this arrive as a feed; above it, on a schedule. The evidence note always
# carries the real numbers, so a reviewer can disagree with the threshold rather
# than with an unexplained label.
CONTINUOUS_MEDIAN_GAP_SECONDS = 300.0
# Below these, the timestamps describe one upload rather than a repeating
# arrival. Two files written 3s apart are a bulk drop; calling that "continuous"
# picks a streaming ingestion mode off two data points.
MIN_ARRIVALS_FOR_CADENCE = 3
# Count carries the signal; the span floor only rejects a tight burst (three
# files written within a minute of each other is still one upload). Keep it
# small, or a real stream -- ten files a minute apart -- gets mistaken for a drop.
BULK_UPLOAD_SPAN_SECONDS = 60.0

# Which freshness answers are coherent with a measured arrival pattern, and the
# lane the pair implies. A pair that is not listed implies NOTHING -- the intake
# question `target_latency` gets asked instead of a lane being invented.
ARRIVAL_SLA_LANE: dict[tuple[str, str], str] = {
    (ARRIVAL_CONTINUOUS, "sub_minute"): "streaming",
    (ARRIVAL_CONTINUOUS, "minutes"): "streaming",
    (ARRIVAL_CONTINUOUS, "hourly"): "micro_batch",
    (ARRIVAL_PERIODIC, "hourly"): "micro_batch",
    (ARRIVAL_PERIODIC, "daily"): "batch",
    (ARRIVAL_PERIODIC, "weekly_or_monthly"): "batch",
    (ARRIVAL_ONE_SHOT, "daily"): "batch",
    (ARRIVAL_ONE_SHOT, "weekly_or_monthly"): "batch",
}


def implied_lane(arrival_pattern: str, latency_sla: str) -> str | None:
    """The velocity lane a measured arrival pattern plus a stated SLA imply, or
    ``None`` when they do not agree (or the pattern was never measured)."""
    return ARRIVAL_SLA_LANE.get((str(arrival_pattern or ""), str(latency_sla or "")))


def classify_arrival(mtimes: list[float]) -> tuple[str, str]:
    """``(arrival_pattern, evidence)`` from modification timestamps."""
    stamps = sorted(float(t) for t in mtimes if isinstance(t, (int, float)))
    if not stamps:
        return ARRIVAL_UNKNOWN, "no modification times available; arrival pattern not measured"
    distinct = sorted(set(stamps))
    if len(distinct) == 1:
        return (
            ARRIVAL_ONE_SHOT,
            f"{len(stamps)} file(s) share one modification time: a single drop, no cadence",
        )
    gaps = [later - earlier for earlier, later in zip(distinct, distinct[1:])]
    median = statistics.median(gaps)
    span = distinct[-1] - distinct[0]
    evidence = (
        f"{len(stamps)} file(s), {len(distinct)} distinct modification times spanning "
        f"{span:.0f}s, median gap {median:.0f}s "
        f"(threshold {CONTINUOUS_MEDIAN_GAP_SECONDS:.0f}s)"
    )
    # A handful of files written seconds apart is ONE bulk upload, not a cadence.
    # Found live: two files 3s apart were called `continuous`, which routed the
    # table to Auto Loader streaming instead of a batch COPY INTO -- the wrong
    # ingestion mode for a one-time load, chosen from two data points.
    # A stream has to look like a stream: several arrivals, spread over a window
    # longer than a single upload could plausibly take.
    if len(distinct) < MIN_ARRIVALS_FOR_CADENCE or span < BULK_UPLOAD_SPAN_SECONDS:
        return (
            ARRIVAL_ONE_SHOT,
            f"{evidence}; too few arrivals over too short a span to be a cadence "
            f"(needs >= {MIN_ARRIVALS_FOR_CADENCE} arrivals spanning "
            f">= {BULK_UPLOAD_SPAN_SECONDS:.0f}s) -- treated as a single bulk drop",
        )
    pattern = ARRIVAL_CONTINUOUS if median <= CONTINUOUS_MEDIAN_GAP_SECONDS else ARRIVAL_PERIODIC
    return pattern, evidence


@dataclass(frozen=True)
class DiscoveredTable:
    name: str
    path: str
    format: str
    size_bytes: int | None
    row_estimate: int | None
    arrival_pattern: str = ARRIVAL_UNKNOWN
    arrival_evidence: str = ""
    columns: list[dict[str, str]] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Kept for the readers that predate arrival measurement (ingestion
        # generator, blueprint renderer); DERIVED, never declared.
        data["is_streaming"] = self.arrival_pattern == ARRIVAL_CONTINUOUS
        return data


@dataclass(frozen=True)
class ScanOutcome:
    """What one connector scanner returns. `needs` names the missing piece
    (an SDK, a credential reference, an unimplemented connector) so the caller
    can tell the user what to fix instead of printing a traceback."""

    status: str
    tables: list[DiscoveredTable] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    truncated: bool = False


# A scanner reads metadata only: names, paths, formats, byte sizes. It never
# reads row content and never writes anything.
Scanner = Callable[..., ScanOutcome]


# ---------------------------------------------------------------- local files

def scan_local(
    declaration: SourceDeclaration, *, workspace: Path, repo_root: Path,
    max_items: int, max_seconds: float,
) -> ScanOutcome:
    from core.storage.external_data import bounded_external_files

    root = Path(declaration.location).expanduser()
    if not root.is_absolute():
        root = (workspace / root).resolve()
    policy = load_external_data_policy(repo_root)
    if not is_within_allowed_roots(root, repo_root, policy):
        return ScanOutcome(
            status=STATUS_BLOCKED,
            notes=[f"local root is not allow-listed: {root}"],
            needs=["add the root to config/external_data_roots.local.json"],
        )
    if not root.exists() or not root.is_dir():
        return ScanOutcome(status=STATUS_BLOCKED, notes=[f"local root not found: {root}"])

    files, truncated = bounded_external_files(root, max_paths=max_items, max_seconds=max_seconds)
    entries = [
        (path, *_stat_of(path))
        for path in files
        if path.suffix.lower() in DATA_SUFFIXES or "_delta_log" in path.parts
    ]
    tables = _group_paths(root, entries)
    notes = [f"scanned {len(files)} path(s) under {root}"]
    if truncated:
        notes.append(f"listing truncated at max_items={max_items} / max_seconds={max_seconds}")
    return ScanOutcome(status=STATUS_OK, tables=tables, notes=notes, truncated=truncated)


# ------------------------------------------------------------------------- s3

def scan_s3(
    declaration: SourceDeclaration, *, workspace: Path, repo_root: Path,
    max_items: int, max_seconds: float,
) -> ScanOutcome:
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError:
        return ScanOutcome(
            status=STATUS_MISSING_TOOLING,
            notes=["boto3 is not installed, so the bucket cannot be listed"],
            needs=["install boto3 (`uv add boto3`) to scan an s3 source"],
        )

    bucket, prefix = _split_object_uri(declaration.location, "s3")
    if not bucket:
        return ScanOutcome(
            status=STATUS_BLOCKED,
            notes=[f"could not read a bucket from location {declaration.location!r}"],
            needs=["declare the location as s3://<bucket>/<prefix>"],
        )

    sizes: list[tuple[str, int, float | None]] = []
    truncated = False
    try:
        # Session(profile_name=...) resolves a NAMED profile from the caller's
        # own AWS config -- the credential_ref is a name, never a key.
        session = (
            boto3.Session(profile_name=declaration.credential_ref)
            if declaration.credential_ref
            else boto3.Session()
        )
        paginator = session.client("s3").get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = str(obj.get("Key") or "")
                if key.endswith("/"):
                    continue
                modified = obj.get("LastModified")
                sizes.append(
                    (
                        key,
                        int(obj.get("Size") or 0),
                        modified.timestamp() if hasattr(modified, "timestamp") else None,
                    )
                )
                if len(sizes) >= max_items:
                    truncated = True
                    break
            if truncated:
                break
    except Exception as exc:  # boto3/botocore raise a wide family here
        return ScanOutcome(
            status=STATUS_MISSING_TOOLING,
            notes=[f"listing s3://{bucket}/{prefix} failed: {redact(str(exc))}"],
            needs=[
                "an AWS credential reference (named profile or instance role) with "
                f"s3:ListBucket on s3://{bucket}"
            ],
        )

    entries = [
        (Path(key), size, modified)
        for key, size, modified in sizes
        if Path(key).suffix.lower() in DATA_SUFFIXES or "_delta_log" in Path(key).parts
    ]
    # The uri_prefix must carry the scanned prefix too, or the emitted path drops
    # it (`s3://bucket/orders` for an object that actually lives under
    # `s3://bucket/data/orders`) -- the local branch of _group_paths keeps the
    # root, so this one has to as well.
    tables = _group_paths(
        Path(prefix or ""), entries, uri_prefix=f"s3://{bucket}/" + (f"{prefix}/" if prefix else "")
    )
    notes = [f"listed {len(sizes)} object(s) under s3://{bucket}/{prefix}"]
    if truncated:
        notes.append(f"listing truncated at max_items={max_items}")
    return ScanOutcome(status=STATUS_OK, tables=tables, notes=notes, truncated=truncated)


# -------------------------------------------------- object store via Unity Catalog

# In a cloud-first deployment the bucket/container is already registered as a UC
# EXTERNAL LOCATION, so Databricks already holds the credential. Requiring a
# SECOND credential set (boto3 + local AWS config) on the operator's machine is
# the wrong architecture and is what made discovery unusable on a clean box.
# `LIST '<uri>'` on a SQL warehouse enumerates any object store UC can reach,
# which is also the only discovery path adls/gcs have ever had.

_OBJECT_STORE_SCHEMES = {"s3": "s3", "adls": "abfss", "gcs": "gs"}

# ponytail: fixed depth cap, not a tunable. A deeper estate hits max_items first
# and reports `truncated` either way; raise it when a real layout needs it.
_MAX_LIST_DEPTH = 8

UC_NEED = (
    "register the location as a Unity Catalog external location with a storage "
    "credential (Databricks then holds the credential; no local cloud credential "
    "is needed) -- verify with "
    "`databricks storage-credentials validate --storage-credential-name <name> "
    "--external-location-name <location>`"
)

# What the LOCAL (non-UC) path would need per connector. Same text the stub
# scanners used, kept as the second half of an honest refusal.
_LOCAL_TOOL_NEEDS: dict[str, list[str]] = {
    "s3": [
        "or install boto3 (`uv add boto3`) and configure an AWS credential "
        "reference (named profile or instance role) with s3:ListBucket on the bucket"
    ],
    "adls": [
        "or install an azure-storage-file-datalake (or adlfs) client plus an Azure "
        "service-principal secret scope/key",
    ],
    "gcs": [
        "or install a google-cloud-storage (or gcsfs) client plus a service-account "
        "key file path or workload-identity binding name",
    ],
}

# s3 has a local scanner that could work, so a failure there is a missing tool /
# credential. adls/gcs have no local scanner at all, so `unsupported_yet` stays
# the accurate answer for the non-UC path.
_NO_PATH_STATUS = {"s3": STATUS_MISSING_TOOLING, "adls": STATUS_UNSUPPORTED, "gcs": STATUS_UNSUPPORTED}


class UnityCatalogGateway:
    """The three Unity Catalog reads discovery needs, on one duck-typed client.

    ``client`` is a ``databricks.sdk.WorkspaceClient``; tests pass a stand-in
    exposing ``external_locations.list()``, ``warehouses.list()`` and
    ``statement_execution.execute_statement()``.
    """

    def __init__(self, client: Any, *, preferred_warehouse_id: str = "") -> None:
        self.client = client
        self.preferred_warehouse_id = str(preferred_warehouse_id or "")
        self._warehouse: tuple[str, str] | None = None

    def external_locations(self) -> list[tuple[str, str]]:
        """``[(name, url)]`` -- names and urls only, never credential values."""
        found: list[tuple[str, str]] = []
        for location in self.client.external_locations.list():
            name = str(getattr(location, "name", "") or "")
            url = str(getattr(location, "url", "") or "")
            if name and url:
                found.append((name, url))
        return found

    def warehouse(self) -> tuple[str, str]:
        """``(warehouse_id, state)``. The configured warehouse wins; otherwise the
        first one the account exposes. State is "" when it could not be read."""
        if self._warehouse is None:
            chosen, state = self.preferred_warehouse_id, ""
            try:
                warehouses = list(self.client.warehouses.list())
            except Exception:
                warehouses = []
            match = next(
                (item for item in warehouses if str(getattr(item, "id", "") or "") == chosen), None
            )
            if match is None and not chosen and warehouses:
                match = warehouses[0]
            if match is not None:
                chosen = str(getattr(match, "id", "") or "")
                raw_state = getattr(match, "state", None)
                state = str(getattr(raw_state, "value", raw_state) or "")
            self._warehouse = (chosen, state)
        return self._warehouse

    def list_uri(self, uri: str) -> list[list[Any]]:
        """Rows of ``[path, name, size_bytes, modification_time_ms]``."""
        warehouse_id, _ = self.warehouse()
        response = self.client.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement=f"LIST '{uri}'",
            wait_timeout="50s",
        )
        while _statement_state(response) in {"PENDING", "RUNNING"}:
            time.sleep(2)
            response = self.client.statement_execution.get_statement(response.statement_id)
        state = _statement_state(response)
        if state != "SUCCEEDED":
            error = getattr(getattr(response, "status", None), "error", None)
            message = getattr(error, "message", "") or state
            raise RuntimeError(f"LIST failed ({state}): {redact(str(message))}")
        result = getattr(response, "result", None)
        return list(getattr(result, "data_array", None) or []) if result else []

    def fetch_base64(self, uri: str) -> str:
        """A single object's bytes, base64-encoded, read through the warehouse.

        Same credential path as :meth:`list_uri`: Unity Catalog already holds
        the storage credential, so a document beside the data is readable
        without a second cloud credential on the operator's machine.
        `binaryFile` is Databricks' own reader; `base64` carries the bytes
        through a SQL result set without corruption.

        Bounded on purpose -- see `core.intake.documents.MAX_DOCUMENT_BYTES`.
        A result cell is not a file-transfer protocol.
        """
        warehouse_id, _ = self.warehouse()
        safe = _assert_safe_uri(uri)
        statement = (
            "SELECT base64(content) AS b64 FROM "
            f"read_files('{safe}', format => 'binaryFile')"
        )
        response = self.client.statement_execution.execute_statement(
            warehouse_id=warehouse_id, statement=statement, wait_timeout="50s"
        )
        while _statement_state(response) in {"PENDING", "RUNNING"}:
            time.sleep(2)
            response = self.client.statement_execution.get_statement(response.statement_id)
        state = _statement_state(response)
        if state != "SUCCEEDED":
            error = getattr(getattr(response, "status", None), "error", None)
            message = getattr(error, "message", "") or state
            raise RuntimeError(f"binaryFile read failed ({state}): {redact(str(message))}")
        result = getattr(response, "result", None)
        rows = list(getattr(result, "data_array", None) or []) if result else []
        return str(rows[0][0]) if rows and rows[0] else ""


def scan_via_unity_catalog(
    declaration: SourceDeclaration, *, workspace: Path, repo_root: Path,
    max_items: int, max_seconds: float, gateway: UnityCatalogGateway | None = None,
) -> ScanOutcome:
    """List an object store through Unity Catalog -- no local cloud credential."""
    outcome, notes = _unity_catalog_attempt(
        declaration, workspace=workspace, max_items=max_items,
        max_seconds=max_seconds, gateway=gateway,
    )
    if outcome is not None:
        return outcome
    return ScanOutcome(
        status=_NO_PATH_STATUS.get(declaration.type, STATUS_MISSING_TOOLING),
        notes=notes,
        needs=[UC_NEED],
    )


def scan_object_store(
    declaration: SourceDeclaration, *, workspace: Path, repo_root: Path,
    max_items: int, max_seconds: float, gateway: UnityCatalogGateway | None = None,
) -> ScanOutcome:
    """Unity Catalog first, then the local SDK path, then an honest refusal."""
    outcome, notes = _unity_catalog_attempt(
        declaration, workspace=workspace, max_items=max_items,
        max_seconds=max_seconds, gateway=gateway,
    )
    if outcome is not None:
        return outcome
    if declaration.type == "s3" and importlib.util.find_spec("boto3") is not None:
        local = scan_s3(
            declaration, workspace=workspace, repo_root=repo_root,
            max_items=max_items, max_seconds=max_seconds,
        )
        return replace(local, notes=notes + list(local.notes))
    return ScanOutcome(
        status=_NO_PATH_STATUS.get(declaration.type, STATUS_MISSING_TOOLING),
        notes=notes,
        needs=[UC_NEED, *_LOCAL_TOOL_NEEDS.get(declaration.type, [])],
    )


def _unity_catalog_attempt(
    declaration: SourceDeclaration, *, workspace: Path, max_items: int,
    max_seconds: float, gateway: UnityCatalogGateway | None,
) -> tuple[ScanOutcome | None, list[str]]:
    """``(outcome, notes)``. ``None`` means UC could not be used and the caller
    should fall back -- the notes say why, so the refusal stays auditable."""
    notes: list[str] = []
    uri = _object_store_uri(declaration.type, declaration.location)
    if not uri:
        return None, [f"location {declaration.location!r} is not an object-store URI"]

    if gateway is None:
        gateway, note = _open_unity_catalog_gateway(workspace)
        if note:
            notes.append(note)
        if gateway is None:
            return None, notes

    try:
        locations = gateway.external_locations()
    except Exception as exc:
        notes.append(f"could not list Unity Catalog external locations: {redact(str(exc))}")
        return None, notes
    location_name = _matching_external_location(uri, locations)
    if not location_name:
        notes.append(f"{uri} is not under any Unity Catalog external location this account can list")
        return None, notes

    try:
        warehouse_id, state = gateway.warehouse()
    except Exception as exc:
        notes.append(f"could not resolve a SQL warehouse: {redact(str(exc))}")
        return None, notes
    if not warehouse_id:
        notes.append("no SQL warehouse is available to run the LIST")
        return None, notes
    if state and state.upper() != "RUNNING":
        notes.append(
            f"SQL warehouse {warehouse_id} is {state.upper()}; running the LIST starts it, "
            "so this scan pays a cold start"
        )

    try:
        rows, truncated = _list_tree(gateway, uri, max_items=max_items, max_seconds=max_seconds)
    except Exception as exc:
        notes.append(f"LIST on {uri} failed: {redact(str(exc))}")
        return None, notes

    base, prefix = _split_uri_base(uri)
    entries = [
        (Path(path[len(base):] if path.startswith(base) else path), size, mtime)
        for path, size, mtime in rows
    ]
    entries = [
        entry for entry in entries
        if entry[0].suffix.lower() in DATA_SUFFIXES or "_delta_log" in entry[0].parts
    ]
    tables = _group_paths(
        Path(prefix), entries, uri_prefix=base + (f"{prefix}/" if prefix else "")
    )
    notes.append(f"listed {len(rows)} object(s) under {uri}")
    notes.append(
        f"listed via Unity Catalog external location {location_name} on warehouse {warehouse_id}"
    )
    if truncated:
        notes.append(f"listing truncated at max_items={max_items} / max_seconds={max_seconds}")
    return ScanOutcome(status=STATUS_OK, tables=tables, notes=notes, truncated=truncated), notes


def _open_unity_catalog_gateway(workspace: Path) -> tuple[UnityCatalogGateway | None, str]:
    try:
        from core.config import resolve_databricks_config
        from core.execution.databricks_client import DatabricksClient

        layout = WorkspaceLayout(project_root=workspace)
        config = resolve_databricks_config(layout.enterprise_id())
        client = DatabricksClient(config)
        if not client.is_configured():
            return None, "Databricks is not configured, so Unity Catalog discovery was not attempted"
        return (
            UnityCatalogGateway(
                client.get_client(),
                # The configured warehouse wins; http_path is a path, not a secret.
                preferred_warehouse_id=str(config.http_path or "").rstrip("/").rsplit("/", 1)[-1],
            ),
            "",
        )
    except Exception as exc:
        return None, f"Unity Catalog discovery unavailable: {redact(str(exc))}"


def _list_tree(
    gateway: UnityCatalogGateway, base_uri: str, *, max_items: int, max_seconds: float,
) -> tuple[list[tuple[str, int, float | None]], bool]:
    """Breadth-first LIST under `base_uri`, bounded by the scan policy caps."""
    files: list[tuple[str, int, float | None]] = []
    queue: list[tuple[str, int]] = [(base_uri.rstrip("/") + "/", 0)]
    deadline = time.monotonic() + float(max_seconds)
    truncated = False
    while queue and not truncated:
        uri, depth = queue.pop(0)
        if time.monotonic() > deadline:
            truncated = True
            break
        for row in gateway.list_uri(uri):
            path, name, size, mtime = _parse_list_row(row)
            if not path:
                continue
            if name.endswith("/") or path.endswith("/"):  # a directory, not a file
                if depth + 1 <= _MAX_LIST_DEPTH:
                    queue.append((path.rstrip("/") + "/", depth + 1))
                else:
                    truncated = True
                continue
            files.append((path, size, mtime))
            if len(files) >= max_items:
                truncated = True
                break
    return files, truncated


def _parse_list_row(row: Any) -> tuple[str, str, int, float | None]:
    """``[path, name, size_bytes, modification_time_ms]`` -> typed values.

    The Statement Execution API returns JSON_ARRAY rows, i.e. strings.
    """
    values = list(row or [])
    path = str(values[0]) if len(values) > 0 and values[0] is not None else ""
    name = str(values[1]) if len(values) > 1 and values[1] is not None else ""
    size = _as_int(values[2]) if len(values) > 2 else None
    millis = _as_int(values[3]) if len(values) > 3 else None
    return path, name, int(size or 0), (millis / 1000.0 if millis else None)


def _matching_external_location(uri: str, locations: list[tuple[str, str]]) -> str:
    """The external location `uri` sits under, matched on SEGMENT boundaries so
    ``s3://b/data`` never claims ``s3://b/database``."""
    target = uri.rstrip("/") + "/"
    for name, url in locations:
        root = str(url).rstrip("/") + "/"
        if target.startswith(root):
            return name
    return ""


def _assert_safe_uri(uri: str) -> str:
    """A URI safe to embed in a SQL string literal, or raise.

    The statements here interpolate a path (`LIST '<uri>'`,
    `read_files('<uri>')`) because neither takes a bind parameter. A quote or a
    newline in that position is a statement break, so it is refused rather than
    escaped -- object-store URIs never legitimately contain either.
    """
    text = str(uri or "").strip()
    if not text or "'" in text or any(char in text for char in "\r\n"):
        raise ValueError(
            "refusing to build SQL for a URI containing a quote or newline: "
            f"{redact(text)!r}"
        )
    return text


def _object_store_uri(connector: str, location: str) -> str:
    """The declared location as a full object-store URI, or "" when it is not one."""
    text = str(location or "").strip()
    if not text or "'" in text or any(char in text for char in "\r\n"):
        return ""
    scheme, separator, rest = text.partition("://")
    if separator:
        return text if scheme and rest.strip("/") else ""
    default = _OBJECT_STORE_SCHEMES.get(connector, "")
    return f"{default}://{text.lstrip('/')}" if default else ""


def _split_uri_base(uri: str) -> tuple[str, str]:
    """``s3://bucket/a/b`` -> ``("s3://bucket/", "a/b")``."""
    scheme, _, rest = uri.partition("://")
    authority, _, key = rest.partition("/")
    return f"{scheme}://{authority}/", key.strip("/")


def _statement_state(response: Any) -> str:
    state = getattr(getattr(response, "status", None), "state", None)
    return str(getattr(state, "value", state) or "").upper().rsplit(".", 1)[-1]


# ----------------------------------------------------------------- uc_existing

def scan_uc_existing(
    declaration: SourceDeclaration, *, workspace: Path, repo_root: Path,
    max_items: int, max_seconds: float,
) -> ScanOutcome:
    """Tables that already exist in Unity Catalog. `location` is
    ``<catalog>.<schema>``; sizes come from DESCRIBE DETAIL (measured, not
    estimated)."""
    parts = [part for part in str(declaration.location).replace("/", ".").split(".") if part]
    if len(parts) < 2:
        return ScanOutcome(
            status=STATUS_BLOCKED,
            notes=[f"location {declaration.location!r} is not <catalog>.<schema>"],
            needs=["declare the location as <catalog>.<schema>"],
        )
    catalog, schema = parts[0], parts[1]

    try:
        from core.config import resolve_databricks_config
        from core.execution.databricks_client import DatabricksClient
        from core.sql_safety import assert_safe_identifier, quote_ident_backtick

        layout = WorkspaceLayout(project_root=workspace)
        client = DatabricksClient(resolve_databricks_config(layout.enterprise_id()))
        if not client.is_configured():
            return ScanOutcome(
                status=STATUS_MISSING_TOOLING,
                notes=["Databricks is not configured for this workspace"],
                needs=[
                    "DATABRICKS_HOST/DATABRICKS_TOKEN or a ~/.databrickscfg profile, "
                    "plus DATABRICKS_HTTP_PATH for a SQL warehouse"
                ],
            )
        fq_schema = ".".join(
            quote_ident_backtick(assert_safe_identifier(part, context="uc identifier"))
            for part in (catalog, schema)
        )
        columns, rows = client.execute_query(f"SHOW TABLES IN {fq_schema}")
        name_idx = _column_index(columns, "tableName", default=1)
        names = [str(row[name_idx]) for row in rows if len(row) > name_idx][:max_items]
    except Exception as exc:
        return ScanOutcome(
            status=STATUS_MISSING_TOOLING,
            notes=[f"listing {catalog}.{schema} failed: {redact(str(exc))}"],
            needs=[f"a Databricks connection with USE SCHEMA on {catalog}.{schema}"],
        )

    tables: list[DiscoveredTable] = []
    notes = [f"SHOW TABLES IN {catalog}.{schema} returned {len(names)} table(s)"]
    for name in names:
        fqn = f"{catalog}.{schema}.{name}"
        detail: dict[str, Any] = {}
        try:
            fq_table = ".".join(
                quote_ident_backtick(assert_safe_identifier(part, context="uc identifier"))
                for part in (catalog, schema, name)
            )
            detail_columns, detail_rows = client.execute_query(f"DESCRIBE DETAIL {fq_table}")
            if detail_rows:
                detail = dict(zip(detail_columns, detail_rows[0]))
        except Exception as exc:
            notes.append(f"DESCRIBE DETAIL {fqn} failed: {redact(str(exc))}")
        tables.append(
            DiscoveredTable(
                name=name,
                path=str(detail.get("location") or fqn),
                format=str(detail.get("format") or "").lower() or "unknown",
                size_bytes=_as_int(detail.get("sizeInBytes")),
                row_estimate=None,
                arrival_pattern=ARRIVAL_UNKNOWN,
                arrival_evidence=(
                    "Unity Catalog listing does not expose per-file arrival times; "
                    "arrival pattern not measured"
                ),
            )
        )
    notes.append("columns not read in this pass; DESCRIBE DETAIL supplies size and format only")
    return ScanOutcome(status=STATUS_OK, tables=tables, notes=notes)


# ------------------------------------------------------------- not-yet-built

def _unsupported_scanner(connector: str, needs: list[str]) -> Scanner:
    def scanner(declaration: SourceDeclaration, **_: Any) -> ScanOutcome:
        return ScanOutcome(
            status=STATUS_UNSUPPORTED,
            notes=[f"no discovery scanner for connector `{connector}` yet"],
            needs=needs,
        )

    return scanner


SCANNERS: dict[str, Scanner] = {
    "local_files": scan_local,
    # Object stores share one selector: Unity Catalog first (the credential is
    # already registered there), local SDK second, honest refusal last.
    "s3": scan_object_store,
    "adls": scan_object_store,
    "gcs": scan_object_store,
    "uc_existing": scan_uc_existing,
    "jdbc": _unsupported_scanner(
        "jdbc",
        [
            "a driver for the declared JDBC url, plus network reachability from the runner",
            "a credential reference: a secret scope/key holding the connection user and password",
            "read access to the source catalog's information_schema for table sizes",
        ],
    ),
    "sftp": _unsupported_scanner(
        "sftp",
        [
            "an SFTP client (paramiko) and host key trust",
            "a credential reference: a named key pair or secret scope/key",
        ],
    ),
    "kafka": _unsupported_scanner(
        "kafka",
        [
            "a Kafka admin/consumer client and broker reachability",
            "a credential reference: the SASL secret scope/key",
            "a schema registry reference for topic value schemas",
        ],
    ),
}


# ------------------------------------------------------------------- the run

@dataclass(frozen=True)
class DiscoveryResult:
    discovery_path: str
    connector: str
    status: str
    table_count: int
    working_set_estimate_bytes: int | None
    open_question_count: int

    def summary(self) -> dict[str, Any]:
        return attach_stage_routing(asdict(self), "source_discovery")


def run_discovery(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    max_items: int = 2000,
    max_seconds: float = 30.0,
) -> DiscoveryResult:
    repo_root_path = Path(repo_root).resolve()
    workspace_path = (repo_root_path / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    layout.ensure_runtime_dirs()

    declaration = load_source_declaration(layout)
    if declaration is None:
        outcome = ScanOutcome(
            status=STATUS_NOT_DECLARED,
            notes=["no source_declaration in workspace_settings.json"],
            needs=["run `declare-source` with --type and --location first"],
        )
        connector = ""
        location = ""
    else:
        connector = declaration.type
        location = declaration.location
        scanner = SCANNERS.get(connector)
        if scanner is None:
            outcome = ScanOutcome(
                status=STATUS_UNSUPPORTED,
                notes=[f"unknown connector `{connector}`"],
                needs=[f"register a scanner for `{connector}` in core.intake.discovery.SCANNERS"],
            )
        else:
            outcome = scanner(
                declaration,
                workspace=workspace_path,
                repo_root=repo_root_path,
                max_items=max_items,
                max_seconds=max_seconds,
            )

    working_set, size_note = _working_set(outcome)
    notes = list(outcome.notes)
    if size_note:
        notes.append(size_note)
    open_questions = _open_questions(outcome, working_set)

    payload = {
        "artifact_type": "intake/discovery.json",
        "version": DISCOVERY_VERSION,
        "generated_by": "discover-source",
        "workspace": _rel(workspace_path, repo_root_path),
        "connector": connector,
        "location": location,
        "status": outcome.status,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "scan_policy": {
            "max_items": max_items,
            "max_seconds": max_seconds,
            "content_read_policy": "metadata_and_paths_only",
            "truncated": outcome.truncated,
        },
        "tables": [table.to_dict() for table in outcome.tables],
        "working_set_estimate_bytes": working_set,
        "notes": notes,
        "needs": outcome.needs,
        "open_questions": open_questions,
    }
    out_path = layout.generated_dir / "intake" / "discovery.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return DiscoveryResult(
        discovery_path=_rel(out_path, repo_root_path),
        connector=connector,
        status=outcome.status,
        table_count=len(outcome.tables),
        working_set_estimate_bytes=working_set,
        open_question_count=len(open_questions),
    )


def load_discovery(layout: WorkspaceLayout) -> dict[str, Any]:
    path = layout.generated_dir / "intake" / "discovery.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _working_set(outcome: ScanOutcome) -> tuple[int | None, str]:
    """Sum of measured sizes, or None. Never an estimate: one unmeasured table
    makes the total unknown, and unknown is reported as unknown."""
    if not outcome.tables:
        return None, ""
    unmeasured = [table.name for table in outcome.tables if table.size_bytes is None]
    if unmeasured:
        return None, (
            "working set not computed: "
            f"{len(unmeasured)} table(s) have no measured byte size "
            f"(e.g. {', '.join(unmeasured[:3])})"
        )
    total = sum(int(table.size_bytes or 0) for table in outcome.tables)
    if outcome.truncated:
        return None, "working set not computed: the listing was truncated, so the total is partial"
    return total, ""


def _open_questions(outcome: ScanOutcome, working_set: int | None) -> list[str]:
    questions: list[str] = []
    if working_set is None:
        questions.append("volume_and_growth")
    if outcome.status in {STATUS_UNSUPPORTED, STATUS_MISSING_TOOLING, STATUS_BLOCKED, STATUS_NOT_DECLARED}:
        questions.append("volume_and_growth")
    return sorted(set(questions))


def _group_paths(
    root: Path,
    entries: list[tuple[Path, int, float | None]],
    *,
    uri_prefix: str = "",
) -> list[DiscoveredTable]:
    """One table per data file at the root, one table per directory below it.

    A flat drop (`root/orders.csv`) is one table per file; a part-file layout
    (`root/orders/part-*.parquet`) is one table whose size is the sum of its
    parts. Delta directories collapse to their table root.
    """
    grouped: dict[tuple[str, str], list[tuple[Path, int, float | None]]] = defaultdict(list)
    for entry in entries:
        path = entry[0]
        rel = _rel_parts(path, root)
        if "_delta_log" in rel:
            key_path = "/".join(rel[: rel.index("_delta_log")])
            grouped[(key_path, "delta")].append(entry)
        elif len(rel) <= 1:
            grouped[("/".join(rel), _format_of(path))].append(entry)
        else:
            grouped[("/".join(rel[:-1]), _format_of(path))].append(entry)

    grouped = _split_distinct_entities(grouped)

    # A bare stem is ambiguous the moment two folders hold the same entity
    # (hospital-a/patients.csv and hospital-b/patients.csv). Two tables with one
    # name collide in bronze naming and in the generated dbt models, so qualify
    # with the parent folder -- but only for the names that actually collide,
    # so an unambiguous estate keeps its short, readable names.
    raw_names: dict[tuple[str, str], str] = {}
    for (key_path, fmt), members in grouped.items():
        raw_names[(key_path, fmt)] = (
            Path(key_path).stem
            if len(members) == 1 and fmt != "delta"
            else (Path(key_path).name or root.name)
        )
    collisions = {n for n in raw_names.values() if list(raw_names.values()).count(n) > 1}

    tables: list[DiscoveredTable] = []
    for (key_path, fmt), members in sorted(grouped.items()):
        name = raw_names[(key_path, fmt)]
        if name in collisions:
            parent = Path(key_path).parent.name
            if parent:
                name = f"{parent}__{name}"
        display = f"{uri_prefix}{key_path}" if uri_prefix else str((root / key_path) if key_path else root)
        pattern, evidence = classify_arrival([mtime for _, _, mtime in members if mtime is not None])
        tables.append(
            DiscoveredTable(
                name=name or "root",
                path=display,
                format=fmt,
                size_bytes=sum(size for _, size, _ in members),
                row_estimate=None,
                arrival_pattern=pattern,
                arrival_evidence=evidence,
            )
        )
    return tables


def _shard_template(stem: str) -> str:
    """A filename with its varying shard token removed.

    `part-00001` and `part-00002` are two shards of ONE dataset; `patients` and
    `providers` are two datasets that merely share a folder. Collapsing digit
    runs is what separates those cases: shards of a set converge on the same
    template, distinct entities do not.
    """
    return re.sub(r"\d+", "#", stem.lower())


def _split_distinct_entities(
    grouped: dict[tuple[str, str], list[tuple[Path, int, float | None]]],
) -> dict[tuple[str, str], list[tuple[Path, int, float | None]]]:
    """Split a directory group whose files are DIFFERENT entities, not shards.

    The directory rule ("everything under one folder is one table") is right for
    a part-file layout and catastrophically wrong for a folder holding one file
    per entity: merging `patients.csv` and `transactions.csv` into a single
    bronze table silently unions unrelated schemas, and every downstream grain
    and join is then built on a table that never existed.

    A group survives intact only when every member reduces to the same shard
    template. Otherwise each file becomes its own table, keyed by its own stem.
    """
    out: dict[tuple[str, str], list[tuple[Path, int, float | None]]] = defaultdict(list)
    for (key_path, fmt), members in grouped.items():
        stems = {Path(p).stem for p, _, _ in members}
        templates = {_shard_template(s) for s in stems}
        if fmt == "delta" or len(members) == 1 or len(templates) == 1:
            out[(key_path, fmt)].extend(members)
            continue
        for member in members:
            stem = Path(member[0]).stem
            child = f"{key_path}/{stem}" if key_path else stem
            out[(child, fmt)].append(member)
    return out


def _rel_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return path.parts


def _format_of(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "pq":
        return "parquet"
    if suffix in {"jsonl", "ndjson"}:
        return "ndjson"
    return suffix or "unknown"


def _stat_of(path: Path) -> tuple[int, float | None]:
    try:
        stat = path.stat()
    except OSError:  # pragma: no cover - vanished between listing and stat
        return 0, None
    return int(stat.st_size), float(stat.st_mtime)


def _column_index(columns: list[str], name: str, *, default: int) -> int:
    lowered = [str(column).lower() for column in columns]
    target = name.lower()
    return lowered.index(target) if target in lowered else default


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _split_object_uri(location: str, scheme: str) -> tuple[str, str]:
    text = str(location or "").strip()
    prefix = f"{scheme}://"
    if text.startswith(prefix):
        text = text[len(prefix):]
    elif "://" in text:
        return "", ""
    bucket, _, key = text.partition("/")
    return bucket, key.strip("/")


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "ARRIVAL_CONTINUOUS",
    "ARRIVAL_ONE_SHOT",
    "ARRIVAL_PERIODIC",
    "ARRIVAL_SLA_LANE",
    "ARRIVAL_UNKNOWN",
    "CONTINUOUS_MEDIAN_GAP_SECONDS",
    "DiscoveredTable",
    "DiscoveryResult",
    "SCANNERS",
    "ScanOutcome",
    "Scanner",
    "UC_NEED",
    "UnityCatalogGateway",
    "classify_arrival",
    "implied_lane",
    "load_discovery",
    "run_discovery",
    "scan_object_store",
    "scan_via_unity_catalog",
]
