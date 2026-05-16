# 06 — Phase P3: PII at Rest

## Goal

After P3:

- Bronze raw data is **access-restricted** at rest — gitignored locally, separate UC schema with grants on Databricks.
- Silver columns marked PII are hashed with **SHA-256 + workspace-scoped salt** using the existing PII masker mechanism.
- The salt itself never appears in artifacts, logs, manifests, or run state — only `hash_salt_ref: workspace.medallion_salt`.
- Gold tables **may only derive from Silver**, structurally enforced by validator extension 5–8.
- A workspace passes a HIPAA-grade audit: no raw PII at rest in Silver or Gold; every PII column tracked in lineage; salt rotation is supported.

This phase is a **hard prerequisite for any production healthcare use**. Until P3 ships, do not run the agent on a workspace with real patient data.

## Prerequisites

- P0 + P1 + P2 shipped.
- `semantic_contract.json` is populated with `pii: true` (or `is_pii: true`) markers on every PII column in the workspace.
- A secret store is available (Databricks secret scope, OS env, or `~/.config/autoresearch/secrets.toml`).

## Requirements (must-haves)

1. **Bronze access restriction**:
   - Local: `state/medallion/bronze/` and `generated/medallion/bronze/` source CSV references are added to the workspace `.gitignore`. The `list-workspace-files` tool skips them. CI does not log their contents.
   - Databricks: Bronze tables are written to a **separate Unity Catalog schema** with grants distinct from the Silver/Gold consumers. Bronze schema grants include `SELECT` only for the build identity and a designated audit role.

2. **Silver PII hashing**:
   - Every Silver table's `silver_contract.pii_hash_columns` is hashed before the row hits the Silver table.
   - Hash function: SHA-256 with workspace-scoped salt prepended: `sha256(coalesce(value, '') || salt)`.
   - Salt source: secret store, never inline. The hash is **deterministic across runs in the same workspace**, so joins still work; **non-portable across workspaces**, so the same patient_id at workspace A and workspace B does not hash to the same value.
   - PII columns that are also part of a primary key are hashed in place; the hashed value becomes the join key. Downstream KPI SQL operates on hashes only.

3. **Gold-from-Silver-only invariant**:
   - Validator extension 3 (already in P0) blocks any `gold.*.derived_from` that references `bronze.*`.
   - Build-time guard: `build-medallion` re-validates the manifest before executing Gold; refuses to proceed if the invariant is violated.

4. **Unmarked PII handling**:
   - For every column not marked `pii: true` in `semantic_contract.json`, the agent surfaces a one-time blocker entry per column at design time: "is this PII?".
   - The blocker is **not** auto-resolved by the seed proposal — it must be human-answered before the column appears in any Silver table.
   - Answers are persisted into `workspace_feature_definitions.json` under `pii_column_overrides` so they don't re-prompt.

5. **Validator extensions 5–8**:
   - 5: Every Silver table referenced in `manifest.layers.silver` has a matching entry in `silver_contract.json`.
   - 6: Every PII-marked column in `semantic_contract.json` appears in `silver_contract.<table>.pii_hash_columns` for at least one Silver table.
   - 7: `star_schema.json` grain declarations are present for every fact table.
   - 8: No `dim_*` or `fact_*` references a Bronze table (redundant with #3 but separately specified for clarity).

6. **Salt rotation support**:
   - A `rotate-salt` workflow (manual): the salt store gets a new value; `build-medallion --rebuild-silver` re-hashes every Silver table from Bronze.
   - The previous salt's hashes are **not preserved** — rotation is a clean cut.

## Architecture for this phase

### Module additions

```
core/medallion/
├── pii.py                # salt lookup, hash function, validation helpers
└── salt_store.py         # abstraction over Databricks scope / OS env / local secrets file
```

### `pii.py` shape

```python
# core/medallion/pii.py
from __future__ import annotations
import hashlib
from typing import Optional

from core.medallion.salt_store import get_workspace_salt

def pii_hash_value(value: Optional[str], *, workspace: str) -> str:
    """SHA-256(value || salt). value is coalesced to empty string."""
    salt = get_workspace_salt(workspace)
    s = (value if value is not None else "")
    return hashlib.sha256((s + salt).encode("utf-8")).hexdigest()

def pii_hash_sql_duckdb(column: str, *, salt_ref: str) -> str:
    """
    Emit a DuckDB expression that hashes `column` with the salt resolved at
    execute time from `salt_ref`. The salt value is bound as a parameter,
    never inlined into SQL.
    """
    return f"sha256(coalesce(cast({column} AS VARCHAR), '') || $salt)"  # using DuckDB named parameters

def pii_hash_spark_expr(column: str) -> str:
    """Spark equivalent. Salt comes from a SparkSession-level UDF binding."""
    return f"sha2(concat(coalesce(cast({column} AS string), ''), get_workspace_salt()), 256)"

def pii_columns_for_silver_table(silver_contract: dict, table: str) -> list[str]:
    return silver_contract.get(table, {}).get("pii_hash_columns", []) or []
```

### `salt_store.py` shape

```python
# core/medallion/salt_store.py
import os
from pathlib import Path
from typing import Optional

class SaltMissing(RuntimeError):
    """Raised when the workspace salt cannot be located."""

def get_workspace_salt(workspace: str) -> str:
    # 1. Databricks secret scope
    try:
        from databricks.sdk import WorkspaceClient
        client = WorkspaceClient()
        secret = client.secrets.get_secret(scope="autoresearch", key=f"medallion_salt__{workspace}")
        if secret and secret.value:
            return secret.value
    except Exception:
        pass

    # 2. OS env
    env_name = f"AUTORESEARCH_WORKSPACE_SALT__{workspace.upper().replace('-', '_')}"
    val = os.environ.get(env_name)
    if val:
        return val

    # 3. ~/.config/autoresearch/secrets.toml
    cfg = Path.home() / ".config" / "autoresearch" / "secrets.toml"
    if cfg.exists():
        import toml
        data = toml.loads(cfg.read_text(encoding="utf-8"))
        salt = (data.get("workspaces", {}).get(workspace) or {}).get("medallion_salt")
        if salt:
            return salt

    raise SaltMissing(
        f"No medallion salt configured for workspace `{workspace}`. "
        f"Set Databricks secret `autoresearch/medallion_salt__{workspace}`, "
        f"env var `{env_name}`, or add to ~/.config/autoresearch/secrets.toml."
    )

def materialize_salt_if_missing(workspace: str) -> None:
    """One-time helper: generate a 256-bit salt and store it.
    The user runs this once per workspace before any build."""
    try:
        get_workspace_salt(workspace)
        return
    except SaltMissing:
        pass
    import secrets
    new_salt = secrets.token_hex(32)
    cfg_dir = Path.home() / ".config" / "autoresearch"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "secrets.toml"
    import toml
    data = toml.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    data.setdefault("workspaces", {}).setdefault(workspace, {})["medallion_salt"] = new_salt
    cfg.write_text(toml.dumps(data), encoding="utf-8")
    # Permissions: best-effort restrict on POSIX
    try:
        os.chmod(cfg, 0o600)
    except OSError:
        pass
```

### Silver SQL emission changes

DuckDB target — pass the salt as a parameter; never inline:

```python
con.execute(silver_sql, {"salt": get_workspace_salt(workspace)})
```

The emitted SQL uses `$salt`:

```sql
INSERT INTO silver.patient
SELECT
    source_system,
    sha256(coalesce(cast(patient_id AS VARCHAR), '') || $salt) AS patient_id,
    sha256(coalesce(cast(ssn AS VARCHAR), '') || $salt) AS ssn,
    ...
FROM unioned
ON CONFLICT (source_system, patient_id) DO UPDATE SET ...;
```

Spark target — register a session-scoped UDF that reads the salt once:

```python
spark.udf.register("get_workspace_salt", lambda: get_workspace_salt(workspace), "string")
```

### Unmarked-PII blocker workflow

```python
# core/medallion/design.py — addition in P3
def _build_pii_blockers(inputs: dict, semantic_contract: dict, workspace_definitions: dict) -> list[dict]:
    """Surface every column without an explicit pii flag as a blocker entry."""
    blockers = []
    overrides = workspace_definitions.get("pii_column_overrides", {})
    for ds in inputs["domain_model"]["datasets"]:
        ds_key = Path(ds["path"]).stem
        for col, dtype in ds.get("schema", {}).items():
            marker = _column_pii_marker(semantic_contract, ds_key, col)  # explicit_true / explicit_false / unmarked
            if marker == "unmarked" and overrides.get(f"{ds_key}.{col}") is None:
                blockers.append({
                    "id": f"pii_unmarked:{ds_key}.{col}",
                    "kind": "pii_unmarked",
                    "column": col,
                    "dataset": ds_key,
                    "dtype": dtype,
                    "sample_values": _sample_values_redacted(ds, col),  # see below
                    "options": [
                        {"label": "Mark as PII (recommended for healthcare names/ids/dates)", "answer": "pii"},
                        {"label": "Not PII", "answer": "not_pii"},
                        {"label": "Sensitive but not PII (audit-restrict, do not hash)", "answer": "restricted"},
                    ],
                })
    return blockers

def _sample_values_redacted(ds, col):
    """Return 3 samples with the middle 60% of characters replaced by *.
    Reason: the reviewer needs *shape* (looks-like-SSN, looks-like-date) without seeing actual PII."""
    ...
```

The blocker resolution writes back to `workspace_feature_definitions.json`:

```json
{
  "pii_column_overrides": {
    "patients.PatientID": "pii",
    "patients.MaritalStatus": "not_pii",
    "claims.ClaimID": "not_pii"
  }
}
```

The agent on subsequent runs reads this and stops re-prompting.

### Validator extensions 5–8

```python
# core/onboarding/workspace_artifact_validator.py

def _validate_medallion_manifest(self) -> None:
    # ... existing P0 checks 1–4 ...

    # check 5: every Silver table has a contract entry
    silver_contract_path = self.layout.generated_dir / "medallion" / "silver_contract.json"
    if not silver_contract_path.exists() and any(layers.get("silver")):
        self._error(manifest_path, "manifest declares Silver tables but silver_contract.json is missing")
        return
    if silver_contract_path.exists():
        silver_contract = self._load_json(silver_contract_path, required=False) or {}
        for s in layers.get("silver", []):
            if s["name"] not in silver_contract:
                self._error(silver_contract_path, f"silver.{s['name']} has no contract entry")

    # check 6: every PII-marked column appears in some Silver pii_hash_columns
    sc_path = self.layout.contracts_dir / "semantic_contract.json"
    if sc_path.exists() and silver_contract_path.exists():
        sc = self._load_json(sc_path, required=False) or {}
        pii_in_semantic = _collect_pii_columns(sc)  # {dataset.column: True}
        pii_in_silver = set()
        for tname, tc in silver_contract.items():
            if tname == "workspace":
                continue
            for col in tc.get("pii_hash_columns", []) or []:
                pii_in_silver.add(col.lower())
        missing = [k for k in pii_in_semantic if k.split('.')[-1].lower() not in pii_in_silver]
        for k in missing:
            self._error(silver_contract_path, f"PII column `{k}` is not hashed in any Silver table")

    # check 7: every fact has a grain
    star_path = self.layout.generated_dir / "medallion" / "star_schema.json"
    if star_path.exists():
        star = self._load_json(star_path, required=False) or {}
        for f in star.get("facts", []):
            if not f.get("grain"):
                self._error(star_path, f"fact `{f.get('name','?')}` has no grain declaration")

    # check 8: dim_*/fact_* may not reference bronze.* (redundant safety net)
    for g in layers.get("gold", []):
        for src in g.get("derived_from", []) or []:
            if str(src).startswith("bronze."):
                self._error(manifest_path, f"gold.{g['name']} references {src} — Gold may only derive from Silver")
```

## Implementation steps

### Step 1: salt store

Implement `core/medallion/salt_store.py` exactly as shown. Add a one-time CLI `medallion-init-salt` (optional) that just calls `materialize_salt_if_missing(workspace)`.

### Step 2: PII helpers

Implement `core/medallion/pii.py`. Add unit tests covering: deterministic across calls, different across workspaces, NULL coalescing, salt rotation produces different hash.

### Step 3: rewire Silver emission to use parameterized salt

Modify `design.py:_emit_silver_sql_duckdb` (or its P1 successor) to emit `$salt` placeholders. Modify `build.py` to bind the salt parameter at execute time.

For Spark, add the UDF registration in every emitted `*.spark.py` header.

### Step 4: unmarked-PII blocker workflow

Implement `_build_pii_blockers` in `design.py`. Add the blocker entries to `medallion_design_panel/current.json`. Add an `apply-pii-answer` CLI that writes back to `workspace_feature_definitions.json`.

### Step 5: validator checks 5–8

Add to `core/onboarding/workspace_artifact_validator.py`. Run on the Healthcare RCM workspace; expect to need to update `semantic_contract.json` to mark its actual PII columns.

### Step 6: Bronze access restriction

- **Local**: extend `.gitignore` rule in each workspace; modify `tools/list_workspace_files.py` to skip `state/medallion/bronze/` and `generated/medallion/bronze/` (the SQL files are fine — they reference paths, not data).
- **Databricks**: in `delta_emitter.py`, the Bronze script begins with:
  ```python
  spark.sql(f"CREATE SCHEMA IF NOT EXISTS {bronze_schema}")
  spark.sql(f"GRANT USAGE ON SCHEMA {bronze_schema} TO `audit-role`")
  # Bronze schema gets no other grants by default
  ```

### Step 7: salt rotation flow

Document in `10-operations.md`. New CLI flag `build-medallion --rebuild-silver` that drops Silver tables and re-runs Silver SQL from Bronze.

## Testing

```
tests/medallion/test_pii_hash.py             # determinism, workspace isolation, NULL handling
tests/medallion/test_salt_store.py           # all three lookup paths; missing raises
tests/medallion/test_unmarked_pii_blocker.py # detection; overrides suppress re-prompt
tests/medallion/test_validator_checks_5_to_8.py  # each check, positive and negative
tests/medallion/integration/test_silver_hash_e2e.py  # build a small workspace; verify Silver has hashed values, not raw
```

### Negative test (critical)

A deliberate test that adds a column named `dob_secret` to a source CSV, *does not* mark it as PII in `semantic_contract.json`, and runs `design-medallion`. The expected outcome: a blocker entry, and refusing to write Silver SQL that references this column until resolved.

## Acceptance criteria

1. Running `design-medallion` on a workspace with an unmarked-PII column surfaces a blocker; the column is **not** in Silver until resolved.
2. After resolution, the Silver SQL hashes the column with `sha256(... || $salt)`; the salt value never appears in the SQL.
3. Two different workspaces with the same `patient_id` produce different hashes.
4. The same workspace produces the same hash across runs (deterministic).
5. Salt rotation (new salt, rebuild Silver) produces different hashes.
6. `validate-workspace-artifacts` fails when a PII column is marked in `semantic_contract.json` but missing from `silver_contract.pii_hash_columns`.
7. `validate-workspace-artifacts` fails when `gold.*.derived_from` references `bronze.*` (already passes from P0, regression test for P3).
8. `.gitignore` excludes Bronze data dirs; `list-workspace-files` does not list them.
9. On Databricks, Bronze tables are in a separate UC schema with no grants to non-audit identities.

## Risks

| Risk | Mitigation |
|---|---|
| Salt accidentally checked into git via `secrets.toml` | `~/.config/...` is outside the repo; document this clearly; CI can grep PRs for hex strings of suspicious length |
| Salt rotation breaks join keys across older Silver/Gold | Document rotation as a "rebuild Silver" event, not in-place |
| Hashed-PK Silver tables cause Gold join failures | Gold tables also join on hashed PKs (the hashing is the new identity); document this |
| DuckDB `$salt` parameter binding edge cases | Use the DuckDB Python `prepare` + `execute(prepared, [salt])` pattern; tested in `test_silver_hash_e2e.py` |
| Spark `get_workspace_salt()` UDF closure captures stale salt across sessions | Register fresh per job; document |
| User forgets to set a salt; build fails | `SaltMissing` exit code is loud; `medallion-init-salt` CLI provides one-line bootstrap |
| `semantic_contract.json` PII flags out of date with reality | Validator check 6 catches missing-from-Silver; periodic audit recommended |

## Definition of Done

- [ ] `core/medallion/pii.py`, `salt_store.py` exist.
- [ ] Silver SQL uses parameterized salt; salt never inlined.
- [ ] Validator checks 5–8 ship; all pass on Healthcare RCM workspace after `semantic_contract.json` is brought up to date.
- [ ] All nine acceptance criteria pass.
- [ ] `10-operations.md` has a salt-rotation playbook.
- [ ] Security review (separate doc / linear issue) sign-off.
