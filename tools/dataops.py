"""``dataops``: data-engineering system-design knowledge, decisions, reviews and
drills -- the MinusDataOps vendor, with a workspace bridge.

Most subcommands (explain / decide / volume / drill / score) are pure vendor
knowledge and pass straight through to ``minus_dataops.cli``. The ``review``
subcommand additionally accepts ``--workspace <rel>``: it regenerates (or reuses)
the workspace's certified MinusAnalyst project exactly like ``workspace-dashboard``
does, then reviews that generated root *and* scans the workspace tree for data
formats/footprint -- so the system-design assessment is grounded in the real
project, not a guess.

    dataops explain modeling
    dataops decide batch_vs_stream --latency 5m --set needs_history=true
    dataops volume --scan workspaces/<ws>
    dataops review --workspace workspaces/<ws> --out <ws>/interns/reports/dataops_review.md
    dataops drill --scenario "clickstream analytics"
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import sys
from pathlib import Path

from core.paths import PROJECT_ROOT
from tools.dashboard_export_common import add_vendor_to_path, prepare_minus_root

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def _default_review_out(workspace: Path) -> Path:
    return workspace / "interns" / "reports" / "dataops_review.md"



@anchored("dataops")
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    add_vendor_to_path(PROJECT_ROOT)

    # Workspace bridge: only `review --workspace <rel>` needs core + project prep.
    if argv and argv[0] == "review" and "--workspace" in argv:
        i = argv.index("--workspace")
        try:
            workspace_rel = argv[i + 1]
        except IndexError:
            print("[x] --workspace needs a value", file=sys.stderr)
            return 2
        # strip --workspace <rel> from argv; the rest are passed to the vendor.
        rest = argv[:i] + argv[i + 2:]

        force = "--force" in rest
        no_refresh = "--no-refresh" in rest
        rest = [a for a in rest if a not in {"--force", "--no-refresh"}]

        try:
            _layout, root, _gen, warnings = prepare_minus_root(
                PROJECT_ROOT, workspace_rel, force=force, no_refresh=no_refresh)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"[x] {exc}", file=sys.stderr)
            return 2
        for w in warnings:
            print(f"[~] {w}", file=sys.stderr)

        workspace = (PROJECT_ROOT / workspace_rel).resolve()
        # Inject --root (generated project) and --scan (workspace tree) if absent,
        # and default an --out under the workspace's reports if none given.
        if "--root" not in rest:
            rest += ["--root", str(root)]
        if "--scan" not in rest:
            rest += ["--scan", str(workspace)]
        if "--out" not in rest and "--json" not in rest:
            rest += ["--out", str(_default_review_out(workspace))]
        argv = rest

    from minus_dataops.cli import main as vendor_main
    return vendor_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
