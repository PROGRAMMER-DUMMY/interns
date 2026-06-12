"""One front door for medallion operations (Phase 1 consolidation).

    uv run medallion <design|build|lineage|init-salt> [args...]
    uv run medallion list
"""
from __future__ import annotations

import importlib
import sys

SUBCOMMANDS: dict[str, tuple[str, str, str]] = {
    "design": ("core.medallion.design_cli", "main", "design the medallion layer plan"),
    "build": ("core.medallion.build_cli", "main", "build bronze/silver/gold layers"),
    "lineage": ("core.medallion.lineage_cli", "main", "report medallion lineage"),
    "init-salt": ("core.medallion.salt_store", "_init_salt_cli", "initialise the hashing salt store"),
    "plan-deploy": ("core.medallion.deploy_plan", "main", "emit a validated Databricks deployment plan (plan only)"),
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"list", "--list", "-h", "--help"}:
        print("usage: medallion <subcommand> [args...]\n\nsubcommands:")
        for name, (_, _, purpose) in sorted(SUBCOMMANDS.items()):
            print(f"  {name:<10} {purpose}")
        return 0
    sub = argv[0]
    if sub not in SUBCOMMANDS:
        print(f"medallion: unknown subcommand `{sub}`. Run `medallion list`.")
        return 2
    module_name, attr, _ = SUBCOMMANDS[sub]
    entry = getattr(importlib.import_module(module_name), attr)
    result = entry(argv[1:])
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
