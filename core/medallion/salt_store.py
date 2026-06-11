"""
core/medallion/salt_store.py — Workspace-scoped PII salt retrieval (P3).

Lookup order:
  1. Databricks secret scope  (autoresearch / medallion_salt__<workspace>)
  2. OS environment variable  (AUTORESEARCH_WORKSPACE_SALT__<WORKSPACE>)
  3. ~/.config/autoresearch/secrets.toml  [workspaces.<workspace>.medallion_salt]

The salt never appears in artifacts, logs, manifests, or run state.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path


class SaltMissing(RuntimeError):
    """Raised when no medallion salt is configured for the workspace."""


def get_workspace_salt(workspace: str) -> str:
    """Return the salt for `workspace`, raising SaltMissing if not found."""
    # 1. Databricks secret scope
    try:
        from databricks.sdk import WorkspaceClient
        client = WorkspaceClient()
        secret = client.secrets.get_secret(
            scope="autoresearch",
            key=f"medallion_salt__{workspace}",
        )
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
        try:
            import toml
            data = toml.loads(cfg.read_text(encoding="utf-8"))
            salt = (data.get("workspaces", {}).get(workspace) or {}).get("medallion_salt")
            if salt:
                return salt
        except Exception:
            pass

    raise SaltMissing(
        f"No medallion salt configured for workspace `{workspace}`. "
        f"Run `uv run medallion init-salt --workspace {workspace}` to generate one, "
        f"or set env var `{env_name}`."
    )


def materialize_salt_if_missing(workspace: str) -> str:
    """
    Generate and persist a 256-bit salt if none exists. Returns the salt.
    The user runs this once per workspace before any build.
    """
    try:
        return get_workspace_salt(workspace)
    except SaltMissing:
        pass

    new_salt = secrets.token_hex(32)
    cfg_dir = Path.home() / ".config" / "autoresearch"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "secrets.toml"

    try:
        import toml
        data = toml.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    except Exception:
        data = {}

    data.setdefault("workspaces", {}).setdefault(workspace, {})["medallion_salt"] = new_salt

    try:
        import toml
        cfg.write_text(toml.dumps(data), encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(f"Could not write salt to {cfg}: {exc}") from exc

    try:
        os.chmod(cfg, 0o600)
    except OSError:
        pass

    return new_salt


def _init_salt_cli(argv=None) -> int:
    """CLI entrypoint for `uv run medallion init-salt --workspace <ws>`."""
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="medallion-init-salt",
                                description="Generate and persist a workspace PII salt.")
    p.add_argument("--workspace", required=True)
    args = p.parse_args(argv)
    try:
        materialize_salt_if_missing(args.workspace)
        print(f"[medallion-init-salt] Salt ready for workspace `{args.workspace}`")
        return 0
    except Exception as exc:
        print(f"[medallion-init-salt] ERROR: {exc}", file=sys.stderr)
        return 1
