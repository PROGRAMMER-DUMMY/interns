# 08 — Phase P5: Column-Level Lineage + MLflow Polish

## Goal

After P5:

- `lineage.json` carries **true column-level edges** parsed from the emitted Silver/Gold SQL, not the placeholder layer-level edges from P0.
- Every Gold column can be traced back to its source CSV columns via `Lineage.trace_to_sources(...)`.
- `lineage.md` renders a per-Gold-column section showing the full derivation path — usable in PR review and HIPAA audit.
- `build-medallion` emits a richer MLflow run with column-level lineage as an artifact, per-table assertion pass-rate metrics, and a degraded-run tag that's queryable from the MLflow UI.
- A new `medallion-lineage trace` CLI lets operators query "what feeds gold.fact_claim.payor_id?" without reading SQL.

## Prerequisites

- P0 + P1 + P2 + P3 + P4 all shipped. P5 is the cherry on top — it makes the audit story complete.

## Requirements (must-haves)

1. **SQL-parsed column-level lineage**: parse every emitted `*.duckdb.sql` (and `*.spark.py`) to extract `(target_column ← source_column[, source_column2])` edges. Use `sqlglot` for SQL; AST walk for PySpark.
2. **Updated `lineage.json` shape**: every edge has `from_columns` and `to_columns` lists that are actual columns, not placeholders.
3. **`lineage.md` per-Gold-column trace**: a new section that renders the full path for each Gold column, with `transform_type` annotations.
4. **`medallion-lineage` CLI**: `uv run medallion-lineage trace --workspace <ws> --column gold.fact_claim.payor_id` returns the source columns and the SQL snippets used at each layer.
5. **MLflow per-run lineage artifact**: the per-run state dir's `lineage.json` (the build-time version with row counts attached) becomes an MLflow artifact.
6. **MLflow audit tags**: `degraded_run`, `pii_columns_hashed_count`, `assertions_passed_count`, `unconfirmed_decisions_at_design_time` as searchable tags.
7. **Per-table row-count delta metric**: `mlflow.log_metric("row_count_delta.silver_<table>", new_rows)` for incremental visibility.

## Architecture for this phase

### Module additions

```
core/medallion/
├── sql_lineage_parser.py     # sqlglot-based column-level lineage extraction
├── spark_lineage_parser.py   # AST walker for .spark.py
├── lineage_cli.py            # uv run medallion-lineage
└── lineage_md_renderer.py    # extends contracts_md.py with per-Gold-column path rendering
```

### `sql_lineage_parser.py` outline

```python
# core/medallion/sql_lineage_parser.py
import sqlglot
from sqlglot import expressions as exp
from core.medallion import LineageNode, LineageEdge

def extract_edges_from_sql(sql: str, *, target_table: str, dialect: str = "duckdb") -> list[LineageEdge]:
    """
    Parse `sql` and return edges of the form:
       LineageEdge(from_node=..., from_columns=[...], to_node=target_table, to_columns=[...], transform_type=...)
    The function walks the SELECT projection, identifies each output column,
    and traces it back to source-table.column references.
    """
    tree = sqlglot.parse_one(sql, read=dialect)
    edges: list[LineageEdge] = []
    if not isinstance(tree, (exp.Insert, exp.Create)):
        return edges
    select = tree.find(exp.Select)
    if select is None:
        return edges
    source_tables = {t.alias_or_name: _resolve_table(t) for t in select.find_all(exp.Table)}
    for projection in select.expressions:
        target_col, source_refs, transform_type = _resolve_projection(projection, source_tables)
        for src_table, src_cols in source_refs.items():
            edges.append(LineageEdge(
                from_node=src_table,
                from_columns=src_cols,
                to_node=target_table,
                to_columns=[target_col],
                transform_type=transform_type,
                reasoning=f"derived in {target_table} SELECT projection",
            ))
    return edges

def _resolve_projection(expr, source_tables):
    """
    Return (target_column_name, {source_table: [source_columns]}, transform_type).
    - Direct column reference: transform_type = "passthrough"
    - Function call: transform_type = "computed" with the function name
    - CASE: transform_type = "conditional"
    - Subquery: transform_type = "subquery"
    """
    if isinstance(expr, exp.Alias):
        target_name = expr.alias
        body = expr.this
    elif isinstance(expr, exp.Column):
        target_name = expr.name
        body = expr
    else:
        target_name = expr.sql()[:32]
        body = expr
    columns = list(body.find_all(exp.Column))
    refs: dict[str, list[str]] = {}
    for c in columns:
        t = c.table or "<unaliased>"
        actual = source_tables.get(t, t)
        refs.setdefault(actual, []).append(c.name)
    if isinstance(body, exp.Column):
        return target_name, refs, "passthrough"
    if body.find(exp.Case):
        return target_name, refs, "conditional"
    if body.find(exp.Subquery):
        return target_name, refs, "subquery"
    if body.find(exp.Func):
        fn = next(iter(body.find_all(exp.Func)))
        return target_name, refs, f"computed:{fn.key}"
    return target_name, refs, "computed"
```

### `spark_lineage_parser.py` outline

```python
# core/medallion/spark_lineage_parser.py
import ast
from core.medallion import LineageEdge

def extract_edges_from_spark_py(source: str, *, target_table: str) -> list[LineageEdge]:
    """
    Walk a generated *.spark.py file. Look for:
      - .withColumn("name", <expr>) calls → (name ← columns referenced in expr, transform_type="computed")
      - .select(...) calls → passthrough edges
      - DeltaTable.merge(...).whenMatched.../whenNotMatched... → merge semantics
    The emitter produces a consistent shape (P2), so the parser only needs to
    cover the shapes our emitter actually generates.
    """
    tree = ast.parse(source)
    edges: list[LineageEdge] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_with_column(node):
            target_col, source_cols = _extract_with_column(node)
            edges.append(LineageEdge(
                from_node=_infer_source_table(node, target_table),
                from_columns=source_cols,
                to_node=target_table,
                to_columns=[target_col],
                transform_type="computed:withColumn",
            ))
        # ... similar handlers for .select, .merge
    return edges
```

### Updated `lineage_md_renderer.py`

Append to `core/medallion/contracts_md.py` (don't fork — keep one MD file per contract). New section:

```markdown
## Gold column traces

### `gold.fact_claim`

- `claim_amount` ← `silver.claim.ClaimAmount` ← `bronze.claim__hospital1.ClaimAmount`, `bronze.claim__hospital2.ClaimAmount`
  - transform: `passthrough` (silver), `union+passthrough` (bronze)
- `payor_id_hash` ← `silver.claim.payor_id_hash` ← `bronze.claim__hospital1.PayorID`, `bronze.claim__hospital2.PayorID`
  - transform: `pii_hash:sha256+salt` (silver), `passthrough` (bronze)
- `age_at_service` ← `silver.encounter.age_at_service` ← `bronze.patient__hospital_a.DOB`, `bronze.encounter__hospital_a.ServiceDate`
  - transform: `computed:date_diff` (silver), `passthrough` (bronze)
```

### `lineage_cli.py` outline

```python
# core/medallion/lineage_cli.py
import argparse, json
from pathlib import Path
from core.medallion import Lineage
from core.storage.workspace_layout import WorkspaceLayout

def _build_parser():
    p = argparse.ArgumentParser(prog="medallion-lineage")
    sub = p.add_subparsers(dest="cmd", required=True)
    trace = sub.add_parser("trace", help="Trace a column back to its sources")
    trace.add_argument("--workspace", required=True)
    trace.add_argument("--column", required=True, help="e.g. gold.fact_claim.payor_id_hash")
    trace.add_argument("--json", action="store_true")
    return p

def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.cmd == "trace":
        return _trace(args)

def _trace(args):
    layout = WorkspaceLayout(project_root=Path(args.workspace).resolve())
    lineage_path = layout.generated_dir / "medallion" / "lineage.json"
    data = json.loads(lineage_path.read_text(encoding="utf-8"))
    lineage = Lineage.from_dict(data)
    layer, table, col = args.column.split(".", 2)
    sources = lineage.trace_to_sources(layer, table, col)
    if args.json:
        print(json.dumps({"column": args.column, "sources": sources}, indent=2))
    else:
        print(f"{args.column}")
        for node, src_col in sources:
            print(f"  ← {node}.{src_col}")
    return 0
```

Register in `pyproject.toml` as `medallion-lineage = "core.medallion.lineage_cli:main"`.

### MLflow extension (build-time)

Extend `core/medallion/mlflow_emit.py:finalize_run` (from P2):

```python
mlflow.set_tag("pii_columns_hashed_count", run_state["pii_columns_hashed_count"])
mlflow.set_tag("assertions_passed_count", run_state["assertions_passed_count"])
mlflow.set_tag("unconfirmed_decisions_at_design_time", run_state.get("unconfirmed_at_design", 0))
for table, status in run_state["per_table_status"].items():
    delta = status.get("row_count_delta", status.get("row_count_after", 0) - status.get("row_count_before", 0))
    mlflow.log_metric(f"row_count_delta.{table.replace('.', '_')}", delta)

# Lineage artifact (with build-time annotations)
lineage_with_runtime = _annotate_lineage_with_row_counts(lineage_json, run_state)
artifact_path = run_state_dir / "lineage_with_runtime.json"
artifact_path.write_text(json.dumps(lineage_with_runtime, indent=2), encoding="utf-8")
mlflow.log_artifact(str(artifact_path))
```

## Implementation steps

### Step 1: install `sqlglot`

Already a likely transitive of mlflow; verify and add to `pyproject.toml` deps explicitly:

```toml
"sqlglot>=23.0",
```

### Step 2: SQL lineage parser

Implement `sql_lineage_parser.py`. Cover four projection cases: passthrough, function call, CASE, subquery. Add tests for each.

### Step 3: Spark lineage parser

Implement `spark_lineage_parser.py`. Cover the three shapes the P2 emitter actually generates: `.withColumn`, `.select`, `.merge`. Do not try to be a general PySpark linter — only cover our own generated shapes.

### Step 4: integrate into design pass

In `core/medallion/design.py:_build_lineage`, replace the placeholder edges with parsed ones:

```python
def _build_lineage(workspace_name, manifest, silver_contract, *, sql_emit_dir):
    lineage = Lineage(workspace=workspace_name, nodes=[...])
    for layer_dir, target_prefix in [("bronze", "bronze."), ("silver", "silver."), ("gold", "gold.")]:
        for sql_path in (sql_emit_dir / layer_dir).glob("*.duckdb.sql"):
            target_table = target_prefix + sql_path.stem.replace(".duckdb", "")
            edges = extract_edges_from_sql(sql_path.read_text(encoding="utf-8"), target_table=target_table)
            lineage.edges.extend(edges)
    return lineage
```

### Step 5: MD renderer for Gold column traces

In `contracts_md.py:render_lineage_md`, add a "Gold column traces" section that calls `lineage.trace_to_sources(...)` for every column in every Gold node.

### Step 6: `medallion-lineage` CLI

Implement `lineage_cli.py`, register in `pyproject.toml`.

### Step 7: MLflow extension

Add the tags and lineage-with-runtime artifact emission in `mlflow_emit.py:finalize_run`.

## Testing

```
tests/medallion/test_sql_lineage_parser.py     # 4 projection cases; subquery; CTE
tests/medallion/test_spark_lineage_parser.py   # .withColumn / .select / .merge
tests/medallion/test_lineage_md.py             # Gold-column trace section appears, correct structure
tests/medallion/test_lineage_cli.py            # trace returns correct sources for known fixture
tests/medallion/integration/test_lineage_e2e.py  # design on fixture; verify a known Gold column traces to expected Bronze columns
```

## Acceptance criteria

1. `lineage.json` has at least one `from_columns` and one `to_columns` entry that is **non-empty for every Silver and Gold node** on Healthcare RCM.
2. `Lineage.trace_to_sources("gold", "fact_claim", "claim_amount")` returns a list of `(bronze.<table>, <column>)` pairs (not empty, not layer-level).
3. `lineage.md` includes a "Gold column traces" section showing per-Gold-column paths.
4. `uv run medallion-lineage trace --workspace ... --column gold.fact_claim.claim_amount` prints the source-column path.
5. After a successful `build-medallion`, MLflow run has tags `degraded_run`, `pii_columns_hashed_count`, `assertions_passed_count`, `unconfirmed_decisions_at_design_time`.
6. Per-table row-count-delta metrics appear in MLflow under the run.
7. `lineage_with_runtime.json` is attached to the MLflow run as an artifact.

## Risks

| Risk | Mitigation |
|---|---|
| sqlglot parse errors on unusual SQL constructs | Wrap in try/except; on parse failure, emit a single placeholder edge with `transform_type="unparsed"` and a warning |
| `withColumn` chains with intermediate variables defeat the AST walker | Our emitter is the only generator of these files; we can constrain the shape (no intermediate vars) to keep parsing simple. Document this constraint |
| Subqueries lose lineage detail | Best-effort: surface the subquery's referenced columns as sources; acceptable for audit (the source table is identified, even if the per-row logic is opaque) |
| MLflow artifact size explosion if lineage is huge | Cap by emitting `lineage_with_runtime_summary.json` (top-level counts) as a tag-friendly artifact; full lineage as the larger artifact |
| sqlglot version drift | Pin `sqlglot>=23.0,<25` to keep parse output stable |

## Definition of Done

- [ ] `sql_lineage_parser.py`, `spark_lineage_parser.py`, `lineage_cli.py` exist.
- [ ] `medallion-lineage` registered in `pyproject.toml`.
- [ ] `lineage.json` on Healthcare RCM has column-level edges for every Silver and Gold table.
- [ ] All 7 acceptance criteria pass.
- [ ] MLflow UI screenshots in PR description show the new tags + artifacts.
