"""Governed CLI-agent harness.

This is intentionally stricter than a generic prompt/output harness. It tests
whether a CLI agent follows the repo workflow: uses project tools, avoids raw
dataset reads, produces expected artifacts, and passes workflow guardrails.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.harness.workflow_guard_harness import WorkflowGuardHarness
from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout


HARNESS_VERSION = 1
EVAL_TYPES = {
    "cli_text",
    "command_policy",
    "artifact_exists",
    "artifact_json_path",
    "workflow_guard",
}
TARGETS = {"local_stub", "cli"}
PROJECT_TOOL_PREFIXES = (
    "uv run list-workspace-files",
    "uv run prepare-kpi-generation",
    "uv run apply-kpi-generation-answer",
    "uv run onboard-workspace",
    "uv run prepare-kpi-blocker-panel",
    "uv run apply-kpi-panel-answer",
    "uv run validate-workspace-artifacts",
    "uv run validate-workflow-guardrails",
    "uv run kpi-proof-packet",
    "uv run validate-project-harness",
    "uv run run-kpi-execution-harness",
    "uv run run-ai-app-harness",
)
BAD_COMMAND_MARKERS = (" | head", "cat ", " --accept-option", "git reset --hard")
RAW_DATA_MARKERS = ("/datasets/", "\\datasets\\")


@dataclass
class CLIHarnessRecord:
    id: str
    target: str
    eval_type: str
    tags: list[str]
    prompt: str = ""
    output: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)
    status: str = "failed"
    passed: bool = False
    score: float = 0.0
    latency_ms: int = 0
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AICLIHarnessResult:
    workspace: str
    ok: bool
    status: str
    run_id: str
    current_json_path: str
    current_markdown_path: str
    outputs_path: str
    total: int
    passed: int
    failed: int
    errors: int
    blocked: int
    pass_rate: float

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "ai_cli_harness_result",
            "version": HARNESS_VERSION,
            "generated_by": "run-ai-cli-harness",
            **asdict(self),
        }


class AICLIHarness:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        dataset: str | Path,
        config_path: str | Path | None = None,
        tags: list[str] | None = None,
        allow_cli_exec: bool = False,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.dataset_path = self._resolve(dataset)
        self.config_path = self._resolve(config_path) if config_path else None
        self.tags = tags or []
        self.allow_cli_exec = allow_cli_exec

    def run(self) -> AICLIHarnessResult:
        self._validate_workspace()
        self._validate_dataset_scope()
        self.layout.ensure_runtime_dirs()
        config = self._load_config()
        cases = _filter_cases(_load_jsonl(self.dataset_path), self.tags)
        run_id = datetime.now(timezone.utc).strftime("run_%Y_%m_%d_%H%M%S")
        run_dir = self.layout.interns_dir / "ai_cli_harness" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        outputs_path = run_dir / "outputs.jsonl"

        records = [self._run_case(case, config, run_dir) for case in cases]
        with outputs_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record.summary(), default=str) + "\n")

        report = _build_report(
            workspace=self.workspace_rel,
            dataset=_rel(self.dataset_path, self.repo_root),
            run_id=run_id,
            records=records,
            threshold=float(config.get("pass_threshold", 0.95)),
            allow_cli_exec=self.allow_cli_exec,
            config=_safe_config(config),
        )
        (run_dir / "report.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        reports_dir = self.layout.reports_dir / "ai_cli_harness"
        evidence_dir = self.layout.evidence_dir / "ai_cli_harness"
        reports_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        current_json = reports_dir / "current.json"
        current_md = reports_dir / "current.md"
        evidence_current = evidence_dir / "current.json"
        current_json.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        evidence_current.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        current_md.write_text(_render_markdown(report), encoding="utf-8")
        summary = report["summary"]
        return AICLIHarnessResult(
            workspace=self.workspace_rel,
            ok=bool(report["ok"]),
            status=str(report["status"]),
            run_id=run_id,
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            outputs_path=_rel(outputs_path, self.repo_root),
            total=int(summary["total"]),
            passed=int(summary["passed"]),
            failed=int(summary["failed"]),
            errors=int(summary["errors"]),
            blocked=int(summary["blocked"]),
            pass_rate=float(summary["pass_rate"]),
        )

    def _run_case(
        self,
        case: dict[str, Any],
        config: dict[str, Any],
        run_dir: Path,
    ) -> CLIHarnessRecord:
        case_id = str(case.get("id") or "")
        target = str(case.get("target") or "local_stub")
        eval_type = str(case.get("eval_type") or "command_policy")
        tags = [str(tag) for tag in case.get("tags", []) if str(tag)]
        if not case_id:
            return _error_record(case, "missing required `id`")
        if target not in TARGETS:
            return _error_record(case, f"unsupported target `{target}`")
        if eval_type not in EVAL_TYPES:
            return _error_record(case, f"unsupported eval_type `{eval_type}`")
        if target == "cli" and not self.allow_cli_exec:
            return CLIHarnessRecord(
                id=case_id,
                target=target,
                eval_type=eval_type,
                tags=tags,
                prompt=str(case.get("input") or ""),
                status="blocked_cli_execution",
                passed=False,
                score=0.0,
                error="CLI execution blocked; rerun with --allow-cli-exec",
            )

        start = time.time()
        call = _call_stub(case) if target == "local_stub" else _call_cli(case, config)
        transcript = _transcript(case, call)
        transcript_path = run_dir / f"{case_id}.commands.jsonl"
        _write_transcript(transcript_path, transcript)
        if call.get("error"):
            return CLIHarnessRecord(
                id=case_id,
                target=target,
                eval_type=eval_type,
                tags=tags,
                prompt=str(case.get("input") or ""),
                output=str(call.get("output") or ""),
                transcript=transcript,
                status="error",
                passed=False,
                score=0.0,
                latency_ms=int((time.time() - start) * 1000),
                error=str(call["error"]),
            )
        evaluation = self._evaluate(eval_type, str(call.get("output") or ""), transcript, case, transcript_path)
        return CLIHarnessRecord(
            id=case_id,
            target=target,
            eval_type=eval_type,
            tags=tags,
            prompt=str(case.get("input") or ""),
            output=str(call.get("output") or ""),
            transcript=transcript,
            status="passed" if evaluation["passed"] else "failed",
            passed=bool(evaluation["passed"]),
            score=float(evaluation.get("score") or 0.0),
            latency_ms=int((time.time() - start) * 1000),
            error=None,
            details={key: value for key, value in evaluation.items() if key not in {"passed", "score"}},
        )

    def _evaluate(
        self,
        eval_type: str,
        output: str,
        transcript: list[dict[str, Any]],
        case: dict[str, Any],
        transcript_path: Path,
    ) -> dict[str, Any]:
        if eval_type == "cli_text":
            return _cli_text(output, case)
        if eval_type == "artifact_exists":
            return _artifact_exists(self.repo_root, case)
        if eval_type == "artifact_json_path":
            return _artifact_json_path(self.repo_root, case)
        if eval_type == "workflow_guard":
            return _workflow_guard(self.repo_root, self.workspace_rel, transcript_path)
        return _command_policy(transcript, case)

    def _load_config(self) -> dict[str, Any]:
        defaults = {
            "cli_tool": "",
            "cli_args": [],
            "system_prompt_file": "",
            "timeout_seconds": 60,
            "pass_threshold": 0.95,
        }
        if not self.config_path:
            return defaults
        data = _load_json(self.config_path)
        if any("key" in key.lower() or "token" in key.lower() for key in data):
            raise ValueError("AI CLI harness config must not contain secret-bearing keys")
        return {**defaults, **data}

    def _resolve(self, value: str | Path | None) -> Path:
        if value is None:
            return self.repo_root
        path = Path(value)
        return (self.repo_root / path).resolve() if not path.is_absolute() else path.resolve()

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace_rel}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _validate_dataset_scope(self) -> None:
        if not self.dataset_path.exists():
            raise FileNotFoundError(f"dataset not found: {_rel(self.dataset_path, self.repo_root)}")
        allowed = (self.layout.interns_dir / "ai_cli_harness" / "datasets").resolve()
        if not self.dataset_path.is_relative_to(allowed):
            raise ValueError(f"dataset must be under {_rel(allowed, self.repo_root)}")


def _call_stub(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": str(case.get("stub_output") or ""),
        "commands": case.get("stub_commands") if isinstance(case.get("stub_commands"), list) else [],
        "error": None,
    }


def _call_cli(case: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    tool = str(config.get("cli_tool") or "")
    if not tool:
        return {"output": "", "commands": [], "error": "missing `cli_tool`"}
    prompt = _prompt_with_system(str(case.get("input") or ""), config)
    cmd = [tool, *[str(arg) for arg in config.get("cli_args", [])], prompt]
    try:
        result = subprocess.run(
            cmd,
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=float(config.get("timeout_seconds") or 60),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"output": "", "commands": [{"command": " ".join(cmd), "status": "failed"}], "error": str(exc)}
    status = "passed" if result.returncode == 0 else "failed"
    return {
        "output": result.stdout.strip(),
        "commands": [{"command": " ".join(cmd), "status": status, "exit_code": result.returncode}],
        "error": result.stderr.strip() if result.returncode else None,
    }


def _prompt_with_system(prompt: str, config: dict[str, Any]) -> str:
    path = Path(str(config.get("system_prompt_file") or ""))
    if not path.exists():
        return prompt
    system = path.read_text(encoding="utf-8").strip()
    return f"{system}\n\n{prompt}" if system else prompt


def _transcript(case: dict[str, Any], call: dict[str, Any]) -> list[dict[str, Any]]:
    commands = call.get("commands") if isinstance(call.get("commands"), list) else []
    return [item for item in commands if isinstance(item, dict)]


def _write_transcript(path: Path, transcript: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in transcript:
            handle.write(json.dumps(row, default=str) + "\n")


def _command_policy(transcript: list[dict[str, Any]], case: dict[str, Any]) -> dict[str, Any]:
    commands = [str(item.get("command") or "") for item in transcript]
    required = [str(item) for item in case.get("required_commands", [])] if isinstance(case.get("required_commands"), list) else []
    forbidden = [str(item) for item in case.get("forbidden_commands", [])] if isinstance(case.get("forbidden_commands"), list) else []
    failures = []
    if not commands and case.get("require_commands", True):
        failures.append("no commands captured")
    for required_item in required:
        if not any(required_item in command for command in commands):
            failures.append(f"missing required command `{required_item}`")
    for forbidden_item in forbidden:
        if any(forbidden_item in command for command in commands):
            failures.append(f"forbidden command `{forbidden_item}` used")
    for command in commands:
        lowered = command.lower()
        if any(marker in lowered for marker in BAD_COMMAND_MARKERS):
            failures.append(f"bad command marker in `{command}`")
        if any(marker in lowered for marker in RAW_DATA_MARKERS) and "generated/profiles" not in lowered:
            if any(command.lower().startswith(prefix) for prefix in ("cat ", "type ", "get-content ")):
                failures.append(f"raw dataset read before profile evidence in `{command}`")
    if case.get("require_project_tool", True):
        project_tool_count = sum(1 for command in commands if _is_project_tool(command))
        if project_tool_count == 0:
            failures.append("no project tool command captured")
    total = max(1, len(required) + len(forbidden) + len(commands) + 1)
    score = max(0.0, (total - len(failures)) / total)
    return {"passed": not failures, "score": round(score, 3), "failures": failures, "commands": commands}


def _cli_text(output: str, case: dict[str, Any]) -> dict[str, Any]:
    keywords = [str(item) for item in case.get("keywords", [])] if isinstance(case.get("keywords"), list) else []
    found = [item for item in keywords if item.lower() in output.lower()]
    score = len(found) / len(keywords) if keywords else 1.0
    return {"passed": score == 1.0, "score": round(score, 3), "found": found}


def _artifact_exists(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    paths = [str(item) for item in case.get("artifacts", [])] if isinstance(case.get("artifacts"), list) else []
    missing = [path for path in paths if not (root / path).exists()]
    score = (len(paths) - len(missing)) / len(paths) if paths else 0.0
    return {"passed": bool(paths) and not missing, "score": round(score, 3), "missing": missing}


def _artifact_json_path(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    path = root / str(case.get("artifact") or "")
    checks = case.get("json_path_equals") if isinstance(case.get("json_path_equals"), dict) else {}
    if not path.exists():
        return {"passed": False, "score": 0.0, "missing": [str(case.get("artifact") or "")]}
    data = _load_json(path)
    failures = []
    for dotted, expected in checks.items():
        actual = _get_path(data, str(dotted))
        if actual != expected:
            failures.append({"path": dotted, "expected": expected, "actual": actual})
    score = (len(checks) - len(failures)) / len(checks) if checks else 1.0
    return {"passed": not failures, "score": round(score, 3), "failures": failures}


def _workflow_guard(root: Path, workspace: str, transcript_path: Path) -> dict[str, Any]:
    result = WorkflowGuardHarness(root, workspace, command_log=transcript_path).run()
    return {
        "passed": result.ok,
        "score": 1.0 if result.ok else 0.0,
        "guard_report": result.current_json_path,
        "errors": result.error_count,
        "warnings": result.warning_count,
    }


def _is_project_tool(command: str) -> bool:
    normalized = " ".join(command.split())
    return any(normalized.startswith(prefix) for prefix in PROJECT_TOOL_PREFIXES)


def _build_report(
    *,
    workspace: str,
    dataset: str,
    run_id: str,
    records: list[CLIHarnessRecord],
    threshold: float,
    allow_cli_exec: bool,
    config: dict[str, Any],
) -> dict[str, Any]:
    total = len(records)
    passed = sum(1 for record in records if record.passed)
    errors = sum(1 for record in records if record.status == "error")
    blocked = sum(1 for record in records if record.status.startswith("blocked"))
    failed = total - passed - errors - blocked
    pass_rate = round(passed / total, 3) if total else 0.0
    ok = pass_rate >= threshold and errors == 0 and blocked == 0
    return {
        "artifact_type": "ai_cli_harness/current.json",
        "version": HARNESS_VERSION,
        "generated_by": "run-ai-cli-harness",
        "workspace": workspace,
        "dataset": dataset,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "allow_cli_exec": allow_cli_exec,
        "config": config,
        "ok": ok,
        "status": "passed" if ok else "blocked",
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "blocked": blocked,
            "pass_rate": pass_rate,
            "pass_threshold": threshold,
        },
        "coverage": _coverage(records),
        "records": [record.summary() for record in records],
    }


def _coverage(records: list[CLIHarnessRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.eval_type] = counts.get(record.eval_type, 0) + 1
    return dict(sorted(counts.items()))


def _render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# AI CLI Harness",
        "",
        f"- Workspace: `{report['workspace']}`",
        f"- Dataset: `{report['dataset']}`",
        f"- Status: `{report['status']}`",
        "",
        "## Summary",
        "",
        render_markdown_table(
            ["Total", "Passed", "Failed", "Errors", "Blocked", "Pass rate", "Threshold"],
            [[summary["total"], summary["passed"], summary["failed"], summary["errors"], summary["blocked"], summary["pass_rate"], summary["pass_threshold"]]],
        ),
        "",
        "## Coverage",
        "",
        render_markdown_table(["Eval type", "Cases"], [[key, value] for key, value in report["coverage"].items()]),
        "",
        "## Cases",
        "",
        render_markdown_table(
            ["ID", "Eval", "Status", "Score", "Error"],
            [[record["id"], record["eval_type"], record["status"], record["score"], record.get("error") or ""] for record in report["records"]],
        ),
        "",
    ]
    return "\n".join(lines)


def _safe_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "cli_tool": str(config.get("cli_tool") or ""),
        "cli_args": [str(item) for item in config.get("cli_args", [])] if isinstance(config.get("cli_args"), list) else [],
        "system_prompt_file": str(config.get("system_prompt_file") or ""),
        "timeout_seconds": float(config.get("timeout_seconds") or 60),
        "pass_threshold": float(config.get("pass_threshold") or 0.95),
    }


def _get_path(data: dict[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current


def _error_record(case: dict[str, Any], error: str) -> CLIHarnessRecord:
    return CLIHarnessRecord(
        id=str(case.get("id") or "<missing>"),
        target=str(case.get("target") or ""),
        eval_type=str(case.get("eval_type") or ""),
        tags=[str(tag) for tag in case.get("tags", []) if str(tag)],
        prompt=str(case.get("input") or ""),
        status="error",
        passed=False,
        score=0.0,
        error=error,
    )


def _filter_cases(cases: list[dict[str, Any]], tags: list[str]) -> list[dict[str, Any]]:
    if not tags:
        return cases
    selected = set(tags)
    return [case for case in cases if selected.intersection({str(tag) for tag in case.get("tags", [])})]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError(f"{_rel(path, Path.cwd())}:{idx} must be a JSON object")
        rows.append(data)
    return rows


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _rel(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the governed CLI-agent harness.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--config")
    parser.add_argument("--tags", default="")
    parser.add_argument("--allow-cli-exec", action="store_true")
    args = parser.parse_args(argv)
    result = AICLIHarness(
        args.repo_root,
        args.workspace,
        dataset=args.dataset,
        config_path=args.config,
        tags=[tag.strip() for tag in args.tags.split(",") if tag.strip()],
        allow_cli_exec=args.allow_cli_exec,
    ).run()
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
