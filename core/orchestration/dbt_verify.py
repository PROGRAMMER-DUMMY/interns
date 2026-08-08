"""Verify a generated dbt project actually runs, with `dbt show`.

`generate-dbt-project` writes models and reports success. Nothing then asked
the warehouse whether those models compile, or whether they return anything --
which is exactly the built-but-not-wired gap the 2026-07-26 audit found
everywhere else: an artifact exists, so the run is called done.

`dbt show --select <model> --limit N` is the cheapest real answer: it compiles
the model AND executes it, without materialising anything. A compile error is a
non-zero exit; an empty preview is a zero-row result. Both are reported, and
both make the CLI exit non-zero -- a generated project that returns nothing is
not verified, it is a blank deliverable (the audited run shipped exactly that:
a chartless deck from a permission denial that failed soft).

Executing means touching the warehouse, so this sits behind the same G5 gate as
`run-dbt-backfill`: `AUTORESEARCH_ALLOW_REMOTE_EXECUTION` must be set by a
human's own shell. Refused is a distinct status from verified -- it never
claims verification it did not perform.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.contracts.versioning import register_contract
from core.onboarding.databricks.deploy_gates import check_remote_approval
from core.onboarding.kpi.dbt_project_generator import run_dbt_parse
from core.paths import PROJECT_ROOT
from core.storage.workspace_layout import WorkspaceLayout

REPORT_VERSION = 1
register_contract("dbt_verify/current.json", current_version=REPORT_VERSION)

# Preview rows per model. Enough to prove the query returns something; small
# enough that verifying every mart is cheap.
DEFAULT_LIMIT = 5


@dataclass(frozen=True)
class VerifyResult:
    current_json_path: str
    current_markdown_path: str
    status: str  # "verified" | "failed" | "refused_no_remote_approval"
    models_checked: int = 0
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _preview_row_count(stdout: str) -> int | None:
    """Rows in a `dbt show --output json` preview, or None if unparseable.

    dbt prints log lines around the payload, so the payload is found by scanning
    for a JSON object carrying the preview list. None means "could not tell" and
    is reported as such -- a parse miss is never converted into a failure claim.

    ponytail: line scan over dbt's own JSON output. If dbt changes the envelope,
    switch to `--log-format json` and read the structured event stream.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        for key in ("show", "rows", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return len(rows)
    return None


class DbtShowVerifier:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def run(self, *, limit: int = DEFAULT_LIMIT, select: str = "") -> VerifyResult:
        dbt_dir = self.workspace / "dbt"
        if not (dbt_dir / "dbt_project.yml").exists():
            raise FileNotFoundError(
                f"{dbt_dir}/dbt_project.yml not found -- run "
                "`generate-dbt-project --workspace <ws>` first."
            )
        models = (
            [select] if select
            else sorted(p.stem for p in (dbt_dir / "models" / "marts").glob("fct_*.sql"))
        )
        if not models:
            raise ValueError(
                f"no mart models under {dbt_dir}/models/marts -- nothing to verify. "
                "Re-run `generate-dbt-project --workspace <ws>`."
            )

        remote_gate = check_remote_approval()
        checks: list[dict[str, Any]] = []
        if remote_gate.ok:
            # Parse BEFORE spending a `dbt show` per model. `dbt parse` resolves
            # every ref/source/Jinja/config in one pass, so a dangling ref fails
            # here in seconds instead of once per model against the warehouse.
            # `subprocess.run` is resolved from THIS module so the same patch the
            # per-model checks are tested under also covers the parse call --
            # otherwise a test that mocks dbt here would still shell out for real.
            parse_ok, parse_detail = run_dbt_parse(
                dbt_dir, repo_root=self.repo_root, runner=subprocess.run
            )
            checks.append(
                {
                    "model": "(project)",
                    "command": ["dbt", "parse"],
                    "returncode": 0 if parse_ok else 1,
                    "rows": None,
                    "ok": parse_ok,
                    # A skipped parse (no dbt on PATH) is not a pass: it is
                    # unverified, the same distinction the per-model checks draw.
                    "unverified": parse_ok and parse_detail.startswith("[~]"),
                    "stdout_tail": parse_detail,
                    "stderr_tail": "",
                }
            )
            if not parse_ok:
                raise RuntimeError(
                    "`dbt parse` rejected the generated project; no model was "
                    f"executed. Fix the generator or the project at {dbt_dir}:\n"
                    f"{parse_detail}"
                )
            for model in models:
                cmd = [
                    "dbt", "show",
                    "--select", model,
                    "--limit", str(limit),
                    "--output", "json",
                    "--project-dir", str(dbt_dir),
                    "--profiles-dir", str(dbt_dir),
                ]
                proc = subprocess.run(
                    cmd, cwd=str(self.repo_root), capture_output=True, text=True
                )
                rows = _preview_row_count(proc.stdout) if proc.returncode == 0 else None
                checks.append({
                    "model": model,
                    "command": cmd,
                    "returncode": proc.returncode,
                    "rows": rows,
                    # A compile/run error fails. Zero rows fails -- an empty mart
                    # is the blank-deliverable failure mode, not a pass. An
                    # unparseable preview (rows is None on a clean exit) is
                    # neither: it is reported as unverified, not as success.
                    "ok": proc.returncode == 0 and rows is not None and rows > 0,
                    "unverified": proc.returncode == 0 and rows is None,
                    "stdout_tail": proc.stdout[-2000:],
                    "stderr_tail": proc.stderr[-2000:],
                })
            status = "verified" if all(c["ok"] for c in checks) else "failed"
        else:
            status = "refused_no_remote_approval"

        report = {
            "artifact_type": "dbt_verify/current.json",
            "version": REPORT_VERSION,
            "generated_by": "verify-dbt-project",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dbt_project_dir": _rel(dbt_dir, self.repo_root),
            "limit": limit,
            "models": models,
            "remote_approval_ok": remote_gate.ok,
            "remote_approval_blocking_reason": remote_gate.blocking_reason,
            "status": status,
            "checks": checks,
        }
        panel_dir = self.layout.reports_dir / "dbt_verify"
        panel_dir.mkdir(parents=True, exist_ok=True)
        current_json = panel_dir / "current.json"
        current_md = panel_dir / "current.md"
        current_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(_render_markdown(report), encoding="utf-8")

        if status == "refused_no_remote_approval":
            raise PermissionError(
                f"{remote_gate.blocking_reason} The dbt project is NOT verified. "
                f"Report written to {_rel(current_json, self.repo_root)}."
            )
        if status == "failed":
            broken = ", ".join(c["model"] for c in checks if not c["ok"])
            raise RuntimeError(
                f"dbt show verification failed for: {broken}. See "
                f"{_rel(current_md, self.repo_root)}."
            )
        return VerifyResult(
            _rel(current_json, self.repo_root),
            _rel(current_md, self.repo_root),
            status,
            models_checked=len(checks),
        )


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# dbt Project Verification (`dbt show`)",
        "",
        f"Project: `{report['dbt_project_dir']}`",
        f"Status: `{report['status']}`",
        f"Remote execution approved: "
        + ("yes" if report["remote_approval_ok"]
           else "NO -- " + report["remote_approval_blocking_reason"]),
        "",
    ]
    if not report["checks"]:
        lines += [
            f"Models that WOULD be checked: {', '.join(report['models'])}",
            "",
            "No model was executed, so nothing is verified.",
            "",
        ]
        return "\n".join(lines)
    lines += ["| Model | Exit | Rows | Result |", "|---|---|---|---|"]
    for check in report["checks"]:
        if check["ok"]:
            verdict = "[ok]"
        elif check["unverified"]:
            verdict = "[~] preview unreadable"
        elif check["returncode"] != 0:
            verdict = "[x] dbt error"
        else:
            verdict = "[x] zero rows"
        rows = "?" if check["rows"] is None else str(check["rows"])
        lines.append(f"| `{check['model']}` | {check['returncode']} | {rows} | {verdict} |")
    lines.append("")
    for check in report["checks"]:
        if check["ok"]:
            continue
        lines += [
            f"## `{check['model']}`",
            "",
            "```",
            (check["stderr_tail"] or check["stdout_tail"]).strip(),
            "```",
            "",
        ]
    return "\n".join(lines)


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


@anchored("verify-dbt-project")
def main(argv: list[str] | None = None) -> int:
    from core.onboarding.workspace.cli_runner import run_workspace_command

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--select", default="", help="verify one model instead of every mart")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args(argv)
    return run_workspace_command(
        command="verify-dbt-project",
        workspace=args.workspace,
        repo_root=args.repo_root,
        fn=lambda: DbtShowVerifier(args.repo_root, args.workspace).run(
            limit=args.limit, select=args.select,
        ),
        metadata={"select": args.select, "limit": args.limit},
    )


if __name__ == "__main__":
    raise SystemExit(main())
