"""
tools/databricks_setup.py — Databricks onboarding and validation script.

Run this once before using Databricks integration:
    uv run python tools/databricks_setup.py

What it does:
  1. Creates / updates .gitignore (security — before credentials are read)
  2. Validates DATABRICKS_HOST + DATABRICKS_TOKEN
  3. Creates MLflow experiment at /autoresearch/experiments
  4. Creates Delta catalog + schema (autoresearch.experiments)
  5. Runs a test SQL query on the configured backend
  6. Warns about Azure AAD token TTL if loop_on_databricks = true
  7. Prints a ready summary or clear error instructions
"""
from __future__ import annotations

import sys
from pathlib import Path

_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[1]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from core.paths import PROJECT_ROOT  # noqa: E402

ROOT = PROJECT_ROOT


REQUIRED_SCOPES = [
    ("jobs",          "jobs.submit/get_run/cancel -- JobsBackend execution"),
    ("clusters",      "auto-provisioned cluster for each job run"),
    ("mlflow",        "mlflow.start_run/log_metric/start_span/evaluate -- TelemetryBackend"),
    ("settings",      "current_user.me() -- connection health check"),
    ("sql",           "statement_execution.execute_statement -- test query + WarehouseBackend"),
    ("unity-catalog", "catalogs.create/schemas.create -- Delta table storage"),
]

OPTIONAL_SCOPES = [
    ("files",         "Upload scripts to DBFS if not using Git repos or workspace paths"),
    ("secrets",       "Store API keys in Databricks Secret Store"),
    ("query-history", "Analyse past SQL query performance"),
]


# Scope each setup step exercises -> named when that step hits a permission error.
STEP_SCOPE = {
    "validate": "settings",
    "mlflow": "mlflow",
    "schema": "unity-catalog",
    "query": "sql",
}


def required_scopes_for_config(db) -> list[tuple[str, str]]:
    """Minimum token scopes for the CURRENT config/lock.toml [databricks] settings.

    Drives dynamic scope suggestion: the set is derived from the configured
    execution mode and target catalog/schema, not hardcoded. See
    docs/reference/databricks_token_scopes.md for the full catalog and per-task mapping.
    """
    scopes = [
        ("settings", "current_user.me() -- connection health check"),
        ("unity-catalog", f"schema {db.catalog}.{db.schema} + Delta tables"),
        ("mlflow", "MLflow experiment tracking"),
    ]
    mode = (db.execution or "").lower()
    if mode == "warehouse" or db.http_path:
        scopes.append(("sql", "statement_execution on the SQL warehouse"))
    if mode == "jobs":
        scopes += [
            ("jobs", "submit/get/cancel job runs"),
            ("clusters", "auto-provisioned cluster per job run"),
            ("command-execution", "run commands on the cluster"),
        ]
    if mode == "connect":
        scopes += [
            ("databricks-connect", "Databricks Connect session"),
            ("clusters", "remote cluster for Connect"),
        ]
    seen, out = set(), []
    for scope, why in scopes:
        if scope not in seen:
            seen.add(scope)
            out.append((scope, why))
    return out


def print_required_scopes(cfg) -> None:
    """Print the config-tailored minimum scope set (dynamic suggestion)."""
    db = cfg.databricks
    print(f"\n  Minimum token scopes for execution = \"{db.execution}\":")
    for scope, why in required_scopes_for_config(db):
        print(f"    [x] {scope:<18} {why}")
    print("    Optional:    files (Volumes upload) | secrets | query-history")
    print("    Deploy-only: workspace, files  (deploy-databricks-workspace folder/file upload)")
    print("    Spec-only (NO scope/API call): genie, dashboards, jobs-registration are spec-gated")
    print("    Full catalog + per-task mapping: docs/reference/databricks_token_scopes.md")


def print_scope_guide() -> None:
    print("\n  When generating your Databricks Personal Access Token,")
    print("  select ONLY these scopes (leave everything else unchecked):\n")
    print("  REQUIRED:")
    for scope, reason in REQUIRED_SCOPES:
        print(f"    [x] {scope:<20}  {reason}")
    print("\n  OPTIONAL (enable if you need them):")
    for scope, reason in OPTIONAL_SCOPES:
        print(f"    [ ] {scope:<20}  {reason}")
    print()
    print("  Token URL: <your-workspace>/settings/user/tokens")
    print("  Token name: autoresearch  |  Lifetime: 90 days (or as needed)")


def _mask(token: str) -> str:
    if len(token) <= 8:
        return "****"
    return token[:4] + "****" + token[-4:]


def step_gitignore() -> None:
    """Ensure .gitignore exists and covers .env before any credentials are read."""
    gi = ROOT / ".gitignore"
    required = [".env", "*.token", ".databrickscfg"]
    if gi.exists():
        content = gi.read_text(encoding="utf-8")
        missing = [e for e in required if e not in content]
        if missing:
            with gi.open("a", encoding="utf-8") as f:
                f.write("\n# Databricks secrets\n")
                for entry in missing:
                    f.write(f"{entry}\n")
            print(f"  [.gitignore] Added: {', '.join(missing)}")
        else:
            print("  [.gitignore] OK — secrets entries present")
    else:
        gi.write_text(
            "# Secrets\n.env\n*.token\n.databrickscfg\n\n__pycache__/\n.venv/\nstate/\n",
            encoding="utf-8",
        )
        print("  [.gitignore] Created")


def step_load_config():
    from core.config import load
    cfg = load()
    return cfg


def step_validate_connection(cfg) -> bool:
    from core.execution.databricks_client import DatabricksClient
    db = cfg.databricks

    print(f"\n  Host  : {db.host or '(not set)'}")
    print(f"  Token : {_mask(db.token) if db.token else '(not set)'}")
    print(f"  Mode  : {db.execution}")

    if not db.is_active():
        print("\n  ERROR: Databricks is not configured.")
        print("  Set [databricks] enabled = true in config/lock.toml")
        print("  and set DATABRICKS_HOST + DATABRICKS_TOKEN env vars.")
        return False

    client = DatabricksClient(db)
    ok, msg = client.health_check()
    if ok:
        print(f"  Connection: OK — {msg}")
    else:
        print(f"  Connection: FAILED — {msg}")
        if "403" in msg or "permission" in msg.lower():
            print(f"  -> if this is a permission error, add token scope: {STEP_SCOPE['validate']}")
        if "401" in msg:
            print("\n  Tip: Generate a new Personal Access Token in your Databricks workspace:")
            print("  User Settings → Access Tokens → Generate New Token")
            if db.host and "azuredatabricks" in db.host:
                print("  Azure users: AAD tokens expire after 1 hour. Use a PAT for long sessions.")
    return ok


def step_create_mlflow_experiment(cfg) -> bool:
    from core.execution.databricks_client import DatabricksClient
    client = DatabricksClient(cfg.databricks)
    try:
        exp_id = client.create_mlflow_experiment("/autoresearch/experiments")
        print(f"  MLflow experiment: /autoresearch/experiments (id={exp_id})")
        return True
    except Exception as exc:
        print(f"  MLflow experiment setup failed: {exc}")
        print(f"  -> if this is a permission error, add token scope: {STEP_SCOPE['mlflow']}")
        return False


def step_create_delta_schema(cfg) -> bool:
    from core.execution.databricks_client import DatabricksClient
    client = DatabricksClient(cfg.databricks)
    catalog = cfg.databricks.catalog
    schema = cfg.databricks.schema
    try:
        client.ensure_delta_schema(catalog, schema)
        print(f"  Delta schema: {catalog}.{schema} — ready")
        return True
    except Exception as exc:
        print(f"  Delta schema creation failed (non-fatal): {exc}")
        print(f"  -> if this is a permission error, add token scope: {STEP_SCOPE['schema']}")
        print("  (also requires UC grants on the catalog: USE CATALOG, CREATE SCHEMA)")
        return False


def step_test_query(cfg) -> bool:
    db = cfg.databricks
    if not db.http_path:
        print("  Test query: SKIPPED (DATABRICKS_HTTP_PATH not set)")
        print("  To enable: add your SQL Warehouse HTTP path to .env")
        return True

    try:
        from core.execution.databricks_client import DatabricksClient
        client = DatabricksClient(db).get_client()
        warehouse_id = db.http_path.rstrip("/").split("/")[-1]
        resp = client.statement_execution.execute_statement(
            warehouse_id=warehouse_id,
            statement="SELECT 1 AS autoresearch_test",
            wait_timeout="30s",
        )
        state = str(resp.status.state)
        ok = "SUCCEEDED" in state
        print(f"  SQL Warehouse {warehouse_id[:8]}...: {'OK' if ok else 'FAILED'} (state={state})")
        return ok
    except Exception as exc:
        print(f"  Test query failed: {exc}")
        print(f"  -> if this is a permission error, add token scope: {STEP_SCOPE['query']}")
        return False


def step_discover_capabilities(cfg) -> None:
    from core.execution.databricks_client import DatabricksClient
    try:
        caps = DatabricksClient(cfg.databricks).discover_capabilities()
    except Exception as exc:
        print(f"  Capability discovery failed: {exc}")
        return
    print("  Capability summary:")
    for key in [
        "current_user",
        "unity_catalog",
        "catalogs",
        "sql_warehouses",
        "jobs",
        "mlflow",
        "http_path_configured",
    ]:
        print(f"    {key}: {caps.get(key)}")


def step_aad_warning(cfg) -> None:
    db = cfg.databricks
    if not db.loop_on_databricks:
        return
    if db.host and "azuredatabricks" in db.host:
        if db.token and not db.token.startswith("dapi"):
            print("\n  WARNING: loop_on_databricks = true detected on Azure Databricks.")
            print("  Your token does not look like a PAT (expected 'dapi' prefix).")
            print("  AAD user tokens expire after 1 hour and will silently fail mid-run.")
            print("  Recommendation: Use an OAuth M2M service principal token for long-running jobs.")


def main() -> None:
    print("=" * 60)
    print("  autoresearch — Databricks Setup")
    print("=" * 60)
    print_scope_guide()

    print("\n[1/6] Checking .gitignore...")
    step_gitignore()

    print("\n[2/6] Loading config...")
    try:
        cfg = step_load_config()
    except Exception as exc:
        print(f"  ERROR loading config: {exc}")
        sys.exit(1)

    print("\n[scopes] Token scopes required for your config:")
    print_required_scopes(cfg)

    print("\n[3/6] Validating Databricks connection...")
    if not step_validate_connection(cfg):
        sys.exit(1)

    print("\n[4/6] Creating MLflow experiment...")
    step_create_mlflow_experiment(cfg)

    print("\n[5/6] Creating Delta schema...")
    step_create_delta_schema(cfg)

    print("\n[6/6] Running test query...")
    step_test_query(cfg)

    print("\n[extra] Discovering account capabilities...")
    step_discover_capabilities(cfg)

    step_aad_warning(cfg)

    print("\n" + "=" * 60)
    print("  Setup complete. You can now run:")
    print("    uv run loop")
    print("  or:")
    print("    docker-compose up")
    print("=" * 60)


if __name__ == "__main__":
    main()
