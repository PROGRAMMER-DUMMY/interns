# Core Medallion Architecture Context: `core/medallion`

This document provides an exhaustive reference for all components in [`core/medallion`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion).

---

## Executive Overview & Architectural Model

The `core/medallion` package implements Medallion architecture (Bronze raw ingestion, Silver conformed transformations, Gold KPI business marts), design ratifiers, lineage parsers, incremental builders, and SQL linters.

---

## Key Modules & Responsibilities

- **Build Engine**: [`build.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/build.py#L40-L150), [`build_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/build_cli.py#L20-L60) - Executes medallion loading pipelines.
- **Design & Ratification**: [`design.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/design.py#L50-L180), [`design_ratify.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/design_ratify.py#L15-L65) - Generates medallion table design specs and validates join paths.
- **Incremental Loading**: [`incremental.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/incremental.py#L20-L80) - Generates incremental append/merge loading expressions.
- **Lineage Parsing**: [`sql_lineage_parser.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/sql_lineage_parser.py#L15-L50), [`spark_lineage_parser.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/spark_lineage_parser.py#L15-L50) - Parses SQL/Spark queries into table/column lineage graphs.
- **PII & Salt Store**: [`pii.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/pii.py#L10-L40), [`salt_store.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/salt_store.py#L15-L50) - Hashes sensitive columns using workspace salt keys. Lookup order is Databricks secret scope -> `AUTORESEARCH_WORKSPACE_SALT__<WORKSPACE>` -> `~/.config/autoresearch/secrets.toml`. `materialize_salt_if_missing` REFUSES (raises `RuntimeError`) when that toml exists but does not parse, instead of replacing it: the file is shared by every workspace on the box, and a dropped salt makes already-hashed PII unjoinable forever.
- **Emitters**: [`delta_emitter.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/delta_emitter.py#L20-L75), [`merge_emitter.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/merge_emitter.py#L15-L50) - Writes Delta Lake tables and SQL MERGE statements.

### Gold OBT unknown member (build-time introspection)

A fact OBT already LEFT JOINs its dimensions, so an early-arriving fact keeps its row — but every dimension attribute came back NULL, and those blank NULLs reach every consumer of the materialised table (CLI queries, the Dash app, CSV/Excel exports), not just wherever rendering happens to mask them.

This layer is **deliberately schema-agnostic**: gold emits `d.* EXCLUDE(key)`, silver emits `SELECT *` + `REPLACE`/`EXCLUDE`, and neither `Manifest.SilverTable` nor `TableContract` carries a column list. That is why it runs on any workspace with no schema registry, and why a per-column `COALESCE` cannot be written at design time. Verified against DuckDB 1.5.2: `d.COLUMNS(*)` is invalid (parsed as a scalar function) and `* EXCLUDE` cannot nest inside an expression, so there is no schema-agnostic SQL form either.

The columns are therefore resolved **at build time**, from the only authoritative schema that exists by then — the silver tables DuckDB has already built:

- [`introspect_columns(con, table)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/delta_emitter.py): `PRAGMA table_info` → `[(name, type)]`. Sub-millisecond. Returns `[]` on any failure.
- [`unknown_member_literal(duckdb_type)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/delta_emitter.py): numeric → `-1`, `DATE` → `DATE '1970-01-01'`, `TIMESTAMP` → the epoch, text → `UNKNOWN_MEMBER`. Returns `None` (leave NULL) for `BOOLEAN` — there is no honest unknown boolean — and for container types. **Container types are checked first**: `INTEGER[]` starts with `INTEGER`, and coalescing a list to `-1` is a runtime type error.
- [`emit_gold_duckdb(table, out_dir, *, dimension_columns=None)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/delta_emitter.py): with `dimension_columns` supplied, projects each dimension attribute explicitly and COALESCEs it; without it, falls back to star expansion — unchanged behaviour.
- [`build._resolve_gold_obt_sql(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/build.py): called from `_execute_gold` before the SQL is read. **Rewrites the `.duckdb.sql` artifact** rather than executing a different string, so the file on disk stays equal to what actually ran and lint/lineage/audit still describe the real statement.

`UNKNOWN_MEMBER` is the same sentinel the dbt path uses (`dbt_project_generator._UNKNOWN_MEMBER`), so one workspace never renders two spellings of the same concept across its local OBT and its warehouse models.

**Failure modes**: a dimension that has not been built yet, or a failed `PRAGMA`, yields `[]` → the design-time star-expansion SQL stands. `_resolve_gold_obt_sql` never raises: an un-COALESCEd OBT is uglier, not wrong, and must not fail a build. Non-fact tables and facts without foreign keys are untouched.

**Not changed**: the emitter's docstring has long claimed `<dim>_<col>` prefixing that the code never did. Renaming output columns now would break every downstream reader of gold, so the latent collision risk is left as-is rather than fixed as a side effect.

**Tests**: [`tests/test_gold_obt_unknown_member.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_gold_obt_unknown_member.py) — 12 cases against **real DuckDB**, asserting the emitted statement executes and the late-arriving fact reads a named unknown with its dimension types preserved.
