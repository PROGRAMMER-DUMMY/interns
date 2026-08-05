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

import json
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
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
    evidence = (
        f"{len(stamps)} file(s), {len(distinct)} distinct modification times spanning "
        f"{distinct[-1] - distinct[0]:.0f}s, median gap {median:.0f}s "
        f"(threshold {CONTINUOUS_MEDIAN_GAP_SECONDS:.0f}s)"
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
    tables = _group_paths(Path(prefix or ""), entries, uri_prefix=f"s3://{bucket}/")
    notes = [f"listed {len(sizes)} object(s) under s3://{bucket}/{prefix}"]
    if truncated:
        notes.append(f"listing truncated at max_items={max_items}")
    return ScanOutcome(status=STATUS_OK, tables=tables, notes=notes, truncated=truncated)


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
    "s3": scan_s3,
    "uc_existing": scan_uc_existing,
    "adls": _unsupported_scanner(
        "adls",
        [
            "an azure-storage-file-datalake (or adlfs) client",
            "a credential reference: a UC storage credential name or an Azure "
            "service-principal secret scope/key",
        ],
    ),
    "gcs": _unsupported_scanner(
        "gcs",
        [
            "a google-cloud-storage (or gcsfs) client",
            "a credential reference: a service-account key file path or workload-identity binding name",
        ],
    ),
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

    tables: list[DiscoveredTable] = []
    for (key_path, fmt), members in sorted(grouped.items()):
        name = Path(key_path).stem if len(members) == 1 and fmt != "delta" else (Path(key_path).name or root.name)
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
    "classify_arrival",
    "implied_lane",
    "load_discovery",
    "run_discovery",
]
