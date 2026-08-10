"""Ingestion code GENERATOR -- emits Databricks-native ingestion, runs nothing.

Per discovered table it picks the method the platform would actually use:

| connector        | signal                | emitted                                  |
|------------------|-----------------------|------------------------------------------|
| s3 / adls / gcs  | ``is_streaming: true``| Auto Loader (``cloudFiles``) python job  |
| s3 / adls / gcs  | one-shot batch        | ``COPY INTO`` SQL                        |
| jdbc             | key resolvable        | watermark-bounded pull + MERGE (Lakeflow Connect is the managed path) |
| jdbc             | ``one_shot: true``    | snapshot append guarded by a marker row  |
| jdbc             | no key resolvable     | nothing; manifest records ``blocked_no_idempotency_key`` |
| kafka            | --                    | throttled structured streaming read stub |
| sftp / local_files / uc_existing | --    | nothing; recorded as a note              |

Rules the emitted code always satisfies (pipeline_practices_gap_research.md):

* checkpoints and inferred-schema state go under a dedicated ``_checkpoints``
  volume, outside any customer object-lifecycle expiration prefix -- the single
  most common generated-infra mistake is writing stream state into a bucket
  prefix with a 30-day expiry rule, which corrupts the stream;
* schema evolution mode is always explicit, never left to the default;
* the target is always ``<catalog>_<env>.<bronze>.<table>``;
* credentials appear as REFERENCE NAMES ONLY (secret scope + key), never values;
* additive only -- nothing emitted here removes or rewrites existing data;
* every job is safe to re-run, and the manifest says how: ``idempotency: {mode:
  merge|append_once|streaming_checkpoint|refused, key: [...], watermark: <col|null>}``.
  A job whose safety cannot be established is REFUSED, not emitted unsafely.

Output lands in ``workspaces/<ws>/ingestion/`` (git-tracked, like ``dbt/``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from textwrap import indent
from typing import Any

from core.onboarding.panel_contract import attach_stage_routing
from core.provisioning.plan import (
    CHECKPOINT_VOLUME,
    OBJECT_STORE_CONNECTORS,
    load_discovery,
    load_provision_plan,
    load_source_declaration,
)
from core.storage.workspace_layout import WorkspaceLayout

MANIFEST_VERSION = 1
# STAGE_ROUTING key (core/onboarding/workspace/delegation.py) this generator belongs to.
ROUTING_STAGE = "ingestion_generation"

METHOD_AUTO_LOADER = "auto_loader"
METHOD_COPY_INTO = "copy_into"
METHOD_JDBC = "jdbc_batch"
METHOD_KAFKA = "kafka_stream"
METHOD_UNSUPPORTED = "unsupported"

# How a job stays safe to re-run (manifest ``idempotency.mode``).
MODE_MERGE = "merge"                        # upsert on a key; retries update, never duplicate
MODE_APPEND_ONCE = "append_once"            # appends guarded by a one-time marker / file bookkeeping
MODE_STREAMING = "streaming_checkpoint"     # the checkpoint owns the offset
MODE_REFUSED = "refused"                    # no safe emission; nothing written
STATUS_BLOCKED_NO_KEY = "blocked_no_idempotency_key"

MARKER_TABLE = "_ingest_markers"
# Records per Kafka micro-batch. Mirrors Auto Loader's maxFilesPerTrigger: without
# a cap the first trigger swallows the whole topic backlog (audit A9).
KAFKA_MAX_OFFSETS_PER_TRIGGER = 1000000

_TEMPORAL_TYPE_TOKENS = ("timestamp", "datetime", "date", "time")
_NUMERIC_TYPE_TOKENS = ("int", "long", "serial", "numeric", "decimal", "number")
# Best-first. A column that advances on every UPDATE beats one that only advances
# on INSERT: a create-time column misses in-place updates and the pull skips them.
_WATERMARK_NAME_TOKENS = (
    "updated", "modified", "changed", "ingested", "loaded", "extracted",
    "event", "created", "inserted",
)
_MONOTONIC_NAME_TOKENS = ("version", "seq", "rowversion", "scn", "lsn")

_FORMAT_SQL = {
    "parquet": "PARQUET",
    "csv": "CSV",
    "json": "JSON",
    "avro": "AVRO",
    "orc": "ORC",
    "delta": "DELTA",
    "text": "TEXT",
}

# Formats whose column names live in a header line rather than in the file's
# own metadata. Only these take 'header'/'inferSchema' COPY INTO options.
_TEXT_DELIMITED_FORMATS = {"CSV"}

_LIFECYCLE_NOTE = (
    "Checkpoint + inferred-schema state live in a dedicated _checkpoints volume,\n"
    "# outside the source prefix on purpose: if an object-lifecycle expiration rule\n"
    "# ever reaches this state the stream is corrupted and must restart from zero."
)


@dataclass
class IngestionResult:
    ingestion_dir: str
    manifest_path: str
    connector: str
    catalog: str
    files: list[str] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return attach_stage_routing({
            "ingestion_dir": self.ingestion_dir,
            "manifest_path": self.manifest_path,
            "connector": self.connector,
            "catalog": self.catalog,
            "job_count": len(self.jobs),
            "files": self.files,
            "jobs": self.jobs,
            "notes": self.notes,
            "ok": self.ok,
        }, ROUTING_STAGE)


def safe_name(value: str) -> str:
    """Table/file-safe identifier from an arbitrary discovered table name."""
    cleaned = re.sub(r"[^0-9a-zA-Z_]+", "_", str(value or "").strip().lower()).strip("_")
    if not cleaned:
        cleaned = "table"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned


def choose_method(connector: str, table: dict[str, Any]) -> str:
    if connector in OBJECT_STORE_CONNECTORS:
        return METHOD_AUTO_LOADER if table.get("is_streaming") else METHOD_COPY_INTO
    if connector == "jdbc":
        return METHOD_JDBC
    if connector == "kafka":
        return METHOD_KAFKA
    return METHOD_UNSUPPORTED


def _columns(table: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        col for col in (table.get("columns") or [])
        if isinstance(col, dict) and str(col.get("name") or "").strip()
    ]


def _declared_names(declaration: dict[str, Any], field_name: str) -> list[str]:
    """Read an optional declaration field that may be a string or a list."""
    value = declaration.get(field_name)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _infer_key(table: dict[str, Any]) -> tuple[list[str], str]:
    """Identity columns for the MERGE, or ``([], "")`` when none is defensible.

    Only the table's OWN identity counts: a bare ``customer_id`` on an orders
    table is a foreign key, and merging on it would collapse rows."""
    columns = _columns(table)
    by_name = {str(col["name"]).strip().lower(): str(col["name"]).strip() for col in columns}

    for field_name in ("primary_key", "primary_keys"):
        declared = [
            str(item).strip() for item in (table.get(field_name) or [])
            if isinstance(item, str) and item.strip()
        ] if isinstance(table.get(field_name), list) else []
        if declared:
            return declared, f"key from discovery.json {field_name} {declared}"

    flagged = [
        str(col["name"]).strip() for col in columns
        if col.get("primary_key") is True or col.get("is_primary_key") is True
    ]
    if flagged:
        return flagged, f"key from discovery.json primary-key flag on {flagged}"

    # ponytail: naive singularization (trailing 's'). Declare jdbc_key_columns for
    # anything irregular -- the declared path always wins.
    stem = safe_name(str(table.get("name") or "").split(".")[-1])
    candidates = ["id", f"{stem}_id"]
    if stem.endswith("s"):
        candidates.append(f"{stem[:-1]}_id")
    for candidate in candidates:
        if candidate in by_name:
            name = by_name[candidate]
            return [name], (
                f"key inferred from discovery.json column {name!r} "
                f"(identity of table stem {stem!r})"
            )
    return [], ""


def _infer_watermark(table: dict[str, Any]) -> tuple[str, str]:
    """A strictly-increasing column to bound the pull, by name + type heuristic."""
    best: tuple[int, str, str] | None = None
    for col in _columns(table):
        name = str(col["name"]).strip()
        lowered = name.lower()
        col_type = str(col.get("type") or "").lower()
        temporal = any(token in col_type for token in _TEMPORAL_TYPE_TOKENS)
        numeric = any(token in col_type for token in _NUMERIC_TYPE_TOKENS)
        rank: int | None = None
        kind = ""
        if temporal:
            kind = "timestamp"
            for index, token in enumerate(_WATERMARK_NAME_TOKENS):
                if token in lowered:
                    rank = index
                    break
            else:
                if lowered.endswith(("_at", "_ts", "_date", "_time")):
                    rank = len(_WATERMARK_NAME_TOKENS)
        elif numeric and any(token in lowered for token in _MONOTONIC_NAME_TOKENS):
            rank, kind = len(_WATERMARK_NAME_TOKENS) + 1, "monotonic id"
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, name, kind)
    if best is None:
        return "", ""
    return best[1], (
        f"watermark inferred from discovery.json column {best[1]!r} ({best[2]})"
    )


def resolve_idempotency(
    table: dict[str, Any], declaration: dict[str, Any]
) -> dict[str, Any]:
    """How a JDBC pull can be re-run safely (audit A2).

    Declared beats inferred beats refusal. ``one_shot: true`` is the only way to
    get an append, and even then the emitted job is marker-guarded."""
    evidence: list[str] = []

    key = _declared_names(declaration, "jdbc_key_columns")
    key_source = "declared" if key else "inferred"
    if key:
        evidence.append(f"key declared in source_declaration.jdbc_key_columns {key}")
    else:
        key, key_evidence = _infer_key(table)
        if key_evidence:
            evidence.append(key_evidence)

    watermark = (_declared_names(declaration, "jdbc_watermark_column") or [""])[0]
    if watermark:
        evidence.append(
            "watermark declared in source_declaration.jdbc_watermark_column "
            f"{watermark!r}"
        )
    else:
        watermark, watermark_evidence = _infer_watermark(table)
        if watermark_evidence:
            evidence.append(watermark_evidence)

    if declaration.get("one_shot") is True:
        return {
            "mode": MODE_APPEND_ONCE, "key": [], "watermark": None,
            "source": "declared",
            "evidence": "source_declaration.one_shot=true: full snapshot appended "
                        f"once, guarded by the {MARKER_TABLE} marker",
        }
    if key:
        return {
            "mode": MODE_MERGE, "key": key, "watermark": watermark or None,
            "source": key_source, "evidence": "; ".join(evidence),
        }
    return {
        "mode": MODE_REFUSED, "key": [], "watermark": watermark or None,
        "source": "none",
        "evidence": "; ".join(evidence) or "no key column resolved from discovery.json",
        "needs": [
            "source_declaration.jdbc_key_columns: the column(s) identifying a row "
            f"in {table.get('name')!r}, so re-runs can MERGE instead of duplicate",
            "source_declaration.jdbc_watermark_column (optional): a strictly-"
            "increasing column that bounds the incremental pull",
            "source_declaration.one_shot: true -- only if this table is a single "
            "full-snapshot load; the emitted job then appends once behind a marker",
        ],
    }


def _static_idempotency(method: str) -> dict[str, Any]:
    if method in (METHOD_AUTO_LOADER, METHOD_KAFKA):
        return {
            "mode": MODE_STREAMING, "key": [], "watermark": None,
            "source": "checkpoint",
            "evidence": "the stream checkpoint owns the read position; a restart "
                        "resumes instead of re-reading",
        }
    return {
        "mode": MODE_APPEND_ONCE, "key": [], "watermark": None,
        "source": "copy_into",
        "evidence": "COPY INTO file bookkeeping skips files already ingested",
    }


def generate_ingestion(
    repo_root: str | Path, workspace: str | Path
) -> IngestionResult:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())

    plan = load_provision_plan(layout)
    if not plan:
        raise FileNotFoundError(
            "no provision_plan.json; run `plan-provisioning --workspace <ws>` first "
            "(it fixes the catalog/env and the checkpoint root this generator targets)."
        )
    discovery = load_discovery(layout)
    declaration = load_source_declaration(layout)

    connector = str(plan.get("connector") or discovery.get("connector") or "").lower()
    catalog = str(plan.get("catalog") or "")
    bronze = str(plan.get("bronze_schema") or "bronze")
    checkpoint_root = str(
        plan.get("checkpoint_root") or f"/Volumes/{catalog}/{bronze}/{CHECKPOINT_VOLUME}"
    )
    credential_ref = str(
        plan.get("credential_ref") or declaration.get("credential_ref") or ""
    )
    source_root = str(plan.get("source_location") or declaration.get("location") or "")

    out_dir = layout.project_root / "ingestion"
    out_dir.mkdir(parents=True, exist_ok=True)

    files: list[str] = []
    jobs: list[dict[str, Any]] = []
    notes: list[str] = []

    for table in discovery.get("tables") or []:
        name = safe_name(table.get("name"))
        method = choose_method(connector, table)
        target = f"{catalog}.{bronze}.{name}"
        checkpoint = f"{checkpoint_root}/{name}"
        source_path = str(table.get("path") or "").strip() or f"{source_root.rstrip('/')}/{name}"
        fmt = str(table.get("format") or plan.get("format_hint") or "parquet").lower()

        if method == METHOD_UNSUPPORTED:
            notes.append(
                f"[~] {name}: connector {connector!r} has no generated ingestion; "
                "stage it into object storage (or register it in Unity Catalog) first."
            )
            continue

        job_name = f"ingest_{name}"
        extra: dict[str, Any] = {}
        idempotency = (
            resolve_idempotency(table, declaration) if method == METHOD_JDBC
            else _static_idempotency(method)
        )

        if idempotency["mode"] == MODE_REFUSED:
            needs = idempotency.pop("needs")
            notes.append(
                f"[blocked] {name}: no idempotency key resolved; no job emitted "
                "(an unbounded append would duplicate the table on every re-run). "
                f"Declare one of: {', '.join(need.split(':')[0] for need in needs)}."
            )
            jobs.append({
                "job_name": job_name, "table": table.get("name"), "method": method,
                "status": STATUS_BLOCKED_NO_KEY, "file": None, "needs": needs,
                "target_table": target, "source": source_path, "format": fmt,
                "checkpoint_path": None, "trigger": None,
                "credential_ref": credential_ref or None,
                "idempotency": idempotency,
            })
            continue

        if method == METHOD_AUTO_LOADER:
            filename, body = f"ingest_{name}.py", _auto_loader_py(
                name, source_path, target, checkpoint, fmt
            )
        elif method == METHOD_COPY_INTO:
            filename, body = f"ingest_{name}.sql", _copy_into_sql(
                name, source_path, target, fmt, table.get("columns") or []
            )
        elif method == METHOD_JDBC:
            filename = f"ingest_{name}.py"
            body = _jdbc_py(
                name, source_root, str(table.get("name") or name), target,
                credential_ref, idempotency, job_name,
                f"{catalog}.{bronze}.{MARKER_TABLE}",
            )
        else:  # METHOD_KAFKA
            registry = str(declaration.get("schema_registry_url") or "").strip()
            filename, body = f"ingest_{name}.py", _kafka_py(
                name, source_root, str(table.get("name") or name), target,
                checkpoint, credential_ref, registry,
            )
            extra = {
                "max_offsets_per_trigger": KAFKA_MAX_OFFSETS_PER_TRIGGER,
                "schema": "avro_schema_registry" if registry else "unverified",
                "schema_registry_url": registry or None,
            }
            if not registry:
                notes.append(
                    f"[~] {name}: no source_declaration.schema_registry_url; kafka "
                    "values land as raw strings and their schema is unverified."
                )

        (out_dir / filename).write_text(body, encoding="utf-8")
        files.append(f"ingestion/{filename}")
        jobs.append({
            "job_name": job_name,
            "table": table.get("name"),
            "method": method,
            "status": "emitted",
            "file": f"ingestion/{filename}",
            "target_table": target,
            "source": source_path,
            "format": fmt,
            "checkpoint_path": checkpoint if method in (METHOD_AUTO_LOADER, METHOD_KAFKA) else None,
            "trigger": "availableNow" if method in (METHOD_AUTO_LOADER, METHOD_KAFKA) else "one_shot",
            "credential_ref": credential_ref or None,
            "idempotency": idempotency,
            **extra,
        })

    manifest = {
        "artifact_type": "ingestion/jobs_manifest.json",
        "version": MANIFEST_VERSION,
        "generated_by": "generate-ingestion",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": layout.project_root.name,
        "connector": connector,
        "catalog": catalog,
        "bronze_schema": bronze,
        "checkpoint_root": checkpoint_root,
        "checkpoint_lifecycle_policy": (
            "exempt: the _checkpoints volume must never be covered by an object "
            "lifecycle expiration rule"
        ),
        "credential_ref": credential_ref or None,
        "additive_only": True,
        "job_count": len(jobs),
        "blocked_count": sum(1 for job in jobs if job.get("status") == STATUS_BLOCKED_NO_KEY),
        "jobs": jobs,
        "notes": notes,
    }
    manifest_path = out_dir / "jobs_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return IngestionResult(
        ingestion_dir=_rel(out_dir, repo_root),
        manifest_path=_rel(manifest_path, repo_root),
        connector=connector,
        catalog=catalog,
        files=files,
        jobs=jobs,
        notes=notes,
        ok=manifest["blocked_count"] == 0,
    )


# -- emitters ---------------------------------------------------------------


def _header(
    title: str, target: str,
    behaviour: str = "this job appends; it never removes or rewrites existing rows",
) -> str:
    return (
        f'"""{title}\n\n'
        "Generated by `generate-ingestion` -- edit the generator, not this file.\n"
        f"Target: {target}\n"
        f"Additive only: {behaviour}.\n"
        '"""\n'
    )


def _auto_loader_py(
    name: str, source_path: str, target: str, checkpoint: str, fmt: str
) -> str:
    return (
        _header(f"Auto Loader ingestion for {name}.", target)
        + f'''
SOURCE_PATH = "{source_path}"
TARGET_TABLE = "{target}"
# {_LIFECYCLE_NOTE}
CHECKPOINT_PATH = "{checkpoint}"
SCHEMA_PATH = CHECKPOINT_PATH + "/_schema"

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()

stream = (
    spark.readStream.format("cloudFiles")
    .option("cloudFiles.format", "{fmt}")
    .option("cloudFiles.schemaLocation", SCHEMA_PATH)
    # Explicit on purpose: left unset, new source columns would not be carried
    # forward into bronze and the gap is silent.
    .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
    # addNewColumns handles an ADDED column. It does nothing for a value whose
    # TYPE changed underneath -- that parses to NULL, indistinguishable from a
    # real null, and reaches a KPI as a quietly wrong number. The rescued
    # column is independent of evolution mode and keeps that data visible and
    # queryable instead (`WHERE _rescued_data IS NOT NULL`).
    .option("cloudFiles.rescuedDataColumn", "_rescued_data")
    # Ingest-side cost throttle; whichever limit is hit first governs.
    .option("cloudFiles.maxFilesPerTrigger", 1000)
    # Bounds the replay horizon; backfillInterval catches files missed by
    # notification retention.
    .option("cloudFiles.maxFileAge", "90 days")
    .option("cloudFiles.backfillInterval", "1 day")
    .load(SOURCE_PATH)
)

query = (
    stream.writeStream.outputMode("append")
    .option("checkpointLocation", CHECKPOINT_PATH)
    .option("mergeSchema", "true")
    # Triggered batch: rate-limited multi-micro-batch processing at batch cost.
    .trigger(availableNow=True)
    .toTable(TARGET_TABLE)
)
query.awaitTermination()
'''
    )


def _copy_into_sql(
    name: str, source_path: str, target: str, fmt: str, columns: list[Any]
) -> str:
    fileformat = _FORMAT_SQL.get(fmt, fmt.upper())
    # COPY INTO defaults CSV `header` to FALSE, so without this the header line
    # lands as a DATA row and every column comes back `_c0, _c1, ...`. That is
    # a silently wrong bronze table, which is worse than a loud failure -- the
    # names only surface as nonsense much later, in feature resolution.
    # Self-describing formats (parquet/avro/orc/json) carry their own names and
    # must NOT be given these options. (F23)
    # A value that will not parse into its inferred column type lands as NULL,
    # indistinguishable from a real null and silently wrong by the time it
    # reaches a KPI. Rescued, it stays visible and queryable
    # (`WHERE _rescued_data IS NOT NULL`) -- the inspection step a bronze
    # failure otherwise has nothing to offer.
    format_options = "'mergeSchema' = 'true'"
    if fileformat in _TEXT_DELIMITED_FORMATS:
        format_options += (
            ", 'header' = 'true', 'inferSchema' = 'true'"
            ", 'rescuedDataColumn' = '_rescued_data'"
        )
    cols = ""
    typed = [
        f"  {safe_name(col.get('name'))} {col.get('type', 'STRING')}"
        for col in columns
        if isinstance(col, dict) and col.get("name")
    ]
    if typed:
        cols = " (\n" + ",\n".join(typed) + "\n)"
    return f"""-- COPY INTO ingestion for {name}.
-- Generated by `generate-ingestion` -- edit the generator, not this file.
-- Target: {target}
-- Additive only: COPY INTO is idempotent by file bookkeeping -- files already
-- ingested are skipped on re-run, and nothing existing is rewritten.

CREATE TABLE IF NOT EXISTS {target}{cols};

COPY INTO {target}
FROM '{source_path}'
FILEFORMAT = {fileformat}
FORMAT_OPTIONS ({format_options})
COPY_OPTIONS ('mergeSchema' = 'true');
"""


_JDBC_READER = '''    spark.read.format("jdbc")
    .option("url", JDBC_URL)
    .option("dbtable", {source_expr})
    .option("user", dbutils.secrets.get(scope=CREDENTIAL_SCOPE, key="username"))  # noqa: F821
    .option("password", dbutils.secrets.get(scope=CREDENTIAL_SCOPE, key="password"))  # noqa: F821
    .load()'''


def _jdbc_py(
    name: str, jdbc_url: str, source_table: str, target: str, credential_ref: str,
    idempotency: dict[str, Any], job_name: str, marker_table: str,
) -> str:
    """Merge-by-default (audit A2); marker-guarded append only for one_shot."""
    scope = credential_ref or "<declare source_declaration.credential_ref>"
    behaviour = (
        "this job appends once behind a marker guard; re-runs are a no-op"
        if idempotency["mode"] == MODE_APPEND_ONCE
        else "this job upserts on KEY_COLUMNS; it never removes or rewrites "
             "rows outside the incoming key set"
    )
    lead = (
        _header(f"JDBC ingestion for {name}.", target, behaviour)
        + f'''
# Lakeflow Connect is the managed ingestion path for supported databases; this
# spark.read.jdbc job is the portable fallback stub.

JDBC_URL = "{jdbc_url}"
SOURCE_TABLE = "{source_table}"
TARGET_TABLE = "{target}"
# Secret scope NAME only -- credential values never appear in generated code.
CREDENTIAL_SCOPE = "{scope}"
'''
    )
    if idempotency["mode"] == MODE_APPEND_ONCE:
        reader = indent(_JDBC_READER.format(source_expr="SOURCE_TABLE"), "    ")
        return lead + f'''JOB_NAME = "{job_name}"
MARKER_TABLE = "{marker_table}"

# ONE-SHOT (source_declaration.one_shot = true): a full snapshot append. The
# marker row is what makes a retry a no-op -- an unguarded re-run would append a
# second full copy of the source (audit A2).

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()

spark.sql(
    f"CREATE TABLE IF NOT EXISTS {{MARKER_TABLE}} "
    "(job_name STRING, target_table STRING, ingested_at TIMESTAMP) USING DELTA"
)
marked = spark.sql(
    f"SELECT count(*) AS n FROM {{MARKER_TABLE}} WHERE job_name = '{{JOB_NAME}}'"
).collect()[0]["n"]

if marked:
    print(f"[ok] {{JOB_NAME}} already ingested; this re-run is a no-op.")
else:
    df = (
{reader}
    )
    df.write.format("delta").mode("append").option("mergeSchema", "true").saveAsTable(
        TARGET_TABLE
    )
    spark.sql(
        f"INSERT INTO {{MARKER_TABLE}} VALUES "
        f"('{{JOB_NAME}}', '{{TARGET_TABLE}}', current_timestamp())"
    )
'''

    watermark = idempotency.get("watermark")
    return lead + f'''KEY_COLUMNS = {json.dumps(idempotency["key"])}
# None = unbounded pull; the MERGE keeps the re-run correct either way, it just
# costs a full source scan every time.
WATERMARK_COLUMN = {json.dumps(watermark) if watermark else "None"}

# IDEMPOTENCY (audit A2): the pull is bounded by the watermark already landed in
# bronze and the write is a MERGE on KEY_COLUMNS, so a retry or a re-trigger
# updates the same rows instead of appending a second copy of the table.

from delta.tables import DeltaTable
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()

predicate = ""
if WATERMARK_COLUMN and spark.catalog.tableExists(TARGET_TABLE):
    landed = spark.sql(
        f"SELECT max({{WATERMARK_COLUMN}}) AS high_water FROM {{TARGET_TABLE}}"
    ).collect()
    high_water = landed[0]["high_water"] if landed else None
    if high_water is not None:
        predicate = f"{{WATERMARK_COLUMN}} > '{{high_water}}'"

source_expr = (
    f"(SELECT * FROM {{SOURCE_TABLE}} WHERE {{predicate}}) AS incremental"
    if predicate
    else SOURCE_TABLE
)
df = (
{_JDBC_READER.format(source_expr="source_expr")}
)

if spark.catalog.tableExists(TARGET_TABLE):
    condition = " AND ".join(f"t.{{column}} <=> s.{{column}}" for column in KEY_COLUMNS)
    (
        DeltaTable.forName(spark, TARGET_TABLE)
        .alias("t")
        .merge(df.alias("s"), condition)
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )
else:
    # First run: creating the table IS the load; there is nothing to merge into.
    df.write.format("delta").option("mergeSchema", "true").saveAsTable(TARGET_TABLE)
'''


def _kafka_py(
    name: str, bootstrap: str, topic: str, target: str, checkpoint: str,
    credential_ref: str, schema_registry_url: str = "",
) -> str:
    scope = credential_ref or "<declare source_declaration.credential_ref>"
    if schema_registry_url:
        registry_block = f'''SCHEMA_REGISTRY_URL = "{schema_registry_url}"
# Registry basic-auth comes from the same secret scope -- reference name only.
SCHEMA_REGISTRY_OPTIONS = {{
    "confluent.schema.registry.basic.auth.credentials.source": "USER_INFO",
    "confluent.schema.registry.basic.auth.user.info": dbutils.secrets.get(  # noqa: F821
        scope=CREDENTIAL_SCOPE, key="schema_registry_auth"
    ),
}}
'''
        value_import = "from pyspark.sql.avro.functions import from_avro\n"
        value_select = '''        from_avro(
            F.col("value"), TOPIC + "-value", SCHEMA_REGISTRY_URL, SCHEMA_REGISTRY_OPTIONS
        ).alias("value"),'''
        schema_note = (
            "# Values are decoded with the declared schema registry, so a producer-side\n"
            "# schema change fails loudly here instead of landing as unparsed bytes."
        )
    else:
        registry_block = ""
        value_import = ""
        value_select = '        F.col("value").cast("string").alias("kafka_value"),'
        schema_note = (
            "# [~] No source_declaration.schema_registry_url: values land as raw strings\n"
            "# and their schema is UNVERIFIED (manifest records schema: unverified).\n"
            "# Declare a registry url to decode and validate them instead."
        )
    return (
        _header(f"Kafka structured streaming ingestion stub for {name}.", target)
        + f'''
BOOTSTRAP_SERVERS = "{bootstrap}"
TOPIC = "{topic}"
TARGET_TABLE = "{target}"
# {_LIFECYCLE_NOTE}
CHECKPOINT_PATH = "{checkpoint}"
# Secret scope NAME only -- credential values never appear in generated code.
CREDENTIAL_SCOPE = "{scope}"
{registry_block}
# THROTTLE (audit A9): maxOffsetsPerTrigger caps records per micro-batch. Without
# it the first trigger reads the entire topic backlog in one batch.
{schema_note}

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
{value_import}
spark = SparkSession.builder.getOrCreate()

stream = (
    spark.readStream.format("kafka")
    .option("kafka.bootstrap.servers", BOOTSTRAP_SERVERS)
    .option("subscribe", TOPIC)
    # earliest on first start; the checkpoint owns the offset from then on.
    .option("startingOffsets", "earliest")
    .option("maxOffsetsPerTrigger", {KAFKA_MAX_OFFSETS_PER_TRIGGER})
    .option(
        "kafka.sasl.jaas.config",
        dbutils.secrets.get(scope=CREDENTIAL_SCOPE, key="jaas_config"),  # noqa: F821
    )
    .load()
    .select(
        F.col("key").cast("string").alias("kafka_key"),
{value_select}
        "topic",
        "partition",
        "offset",
        "timestamp",
    )
)

query = (
    stream.writeStream.outputMode("append")
    .option("checkpointLocation", CHECKPOINT_PATH)
    .option("mergeSchema", "true")
    .trigger(availableNow=True)
    .toTable(TARGET_TABLE)
)
query.awaitTermination()
'''
    )


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "KAFKA_MAX_OFFSETS_PER_TRIGGER",
    "METHOD_AUTO_LOADER",
    "METHOD_COPY_INTO",
    "METHOD_JDBC",
    "METHOD_KAFKA",
    "METHOD_UNSUPPORTED",
    "MODE_APPEND_ONCE",
    "MODE_MERGE",
    "MODE_REFUSED",
    "MODE_STREAMING",
    "STATUS_BLOCKED_NO_KEY",
    "IngestionResult",
    "choose_method",
    "generate_ingestion",
    "resolve_idempotency",
    "safe_name",
]
