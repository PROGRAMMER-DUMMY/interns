from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class DashboardPaths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspaces = root / "workspaces"
        self.global_state = root / "state"
        self.agents_state = root / "core" / "agents" / "state"
        self.config_tasks = root / "config" / "tasks.json"

    def workspace(self, name: str) -> Path:
        return self.workspaces / name

    def medallion_state(self, name: str) -> Path:
        return self.workspace(name) / "interns" / "state" / "medallion"

    def runs_dir(self, name: str) -> Path:
        return self.medallion_state(name) / "runs"

    def medallion_generated(self, name: str) -> Path:
        return self.workspace(name) / "interns" / "generated" / "medallion"

    def reports(self, name: str) -> Path:
        return self.workspace(name) / "interns" / "reports"

    def lock_path(self, name: str) -> Path:
        return self.medallion_state(name) / ".lock"

    def live_log(self, name: str) -> Path:
        return self.medallion_state(name) / "build_live.log"

    def pid_file(self, name: str) -> Path:
        return self.medallion_state(name) / "build.pid"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if path and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {} if default is None else default


def tail_file(path: Path, lines: int = 300) -> str:
    if not path or not path.exists():
        return "(no log yet - trigger a build to see output)"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except OSError as exc:
        return f"(error reading log: {exc})"


class GitHistoryService:
    def __init__(self, root: Path) -> None:
        self.root = root

    def log_file(self, filepath: str, n: int = 20) -> list[dict]:
        if not filepath:
            return []
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "--follow", f"-{n}", "--", filepath],
                capture_output=True,
                text=True,
                cwd=str(self.root),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []

        entries = []
        for line in result.stdout.splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2:
                entries.append({"hash": parts[0], "message": parts[1]})
        return entries

    def show_file(self, commit_hash: str, filepath: str) -> str:
        if not commit_hash or not filepath:
            return ""
        try:
            result = subprocess.run(
                ["git", "show", f"{commit_hash}:{filepath}"],
                capture_output=True,
                text=True,
                cwd=str(self.root),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout

    def diff_file(self, hash_a: str, hash_b: str, filepath: str) -> str:
        if not hash_a or not hash_b or not filepath:
            return "Select two revisions to compare."
        if hash_a == hash_b:
            return "Same commit - no diff."
        try:
            result = subprocess.run(
                ["git", "diff", hash_a, hash_b, "--", filepath],
                capture_output=True,
                text=True,
                cwd=str(self.root),
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"git diff failed: {exc}"
        return result.stdout or f"No changes to {filepath} between {hash_a[:8]} and {hash_b[:8]}."

    def log_text(self, n: int = 20) -> str:
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", f"-{n}"],
                capture_output=True,
                text=True,
                cwd=str(self.root),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"git unavailable: {exc}"
        return result.stdout or "No commits."

    def commit_at(self, timestamp: str) -> str:
        if not timestamp:
            return ""
        try:
            result = subprocess.run(
                ["git", "log", f"--before={timestamp}", "-1", "--format=%H"],
                capture_output=True,
                text=True,
                cwd=str(self.root),
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout.strip()


class BuildControlService:
    def __init__(self, paths: DashboardPaths) -> None:
        self.paths = paths

    def lock_state(self, workspace: str) -> dict:
        lock_path = self.paths.lock_path(workspace)
        if not lock_path.exists():
            return {"locked": False, "pid": 0, "age_s": 0}
        try:
            age = time.time() - lock_path.stat().st_mtime
            data = read_json(lock_path, {})
            return {"locked": True, "pid": data.get("pid", 0), "age_s": int(age)}
        except OSError:
            return {"locked": True, "pid": 0, "age_s": 0}

    def pid_alive(self, pid: int) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, PermissionError):
            return False

    def trigger_build(
        self,
        workspace: str,
        target: str = "auto",
        layer: str = "",
        table: str = "",
    ) -> dict:
        state_dir = self.paths.medallion_state(workspace)
        state_dir.mkdir(parents=True, exist_ok=True)
        cmd = ["uv", "run", "build-medallion", "--workspace", f"workspaces/{workspace}"]
        if target and target != "auto":
            cmd += ["--target", target]
        if layer:
            cmd += ["--only-layer", layer]
        if table:
            cmd += ["--only-table", table]

        log_handle = None
        try:
            log_handle = self.paths.live_log(workspace).open("w", encoding="utf-8")
            kwargs: dict[str, Any] = {
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "cwd": str(self.paths.root),
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            proc = subprocess.Popen(cmd, **kwargs)
            log_handle.close()
            self.paths.pid_file(workspace).write_text(str(proc.pid), encoding="utf-8")
            return {"ok": True, "pid": proc.pid}
        except (OSError, ValueError) as exc:
            if log_handle is not None and not log_handle.closed:
                log_handle.close()
            return {"ok": False, "error": str(exc)}

    def kill_build(self, workspace: str) -> dict:
        pid_file = self.paths.pid_file(workspace)
        if not pid_file.exists():
            return {"ok": False, "error": "No PID file."}
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM
            os.kill(pid, sig)
            pid_file.unlink(missing_ok=True)
            return {"ok": True}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}


class WorkspaceCommandService:
    def __init__(self, root: Path) -> None:
        self.root = root

    def run(self, workspace: str, command_name: str, extra: list[str] | None = None) -> dict:
        cmd = ["uv", "run", command_name, "--workspace", f"workspaces/{workspace}"] + (
            extra or []
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.root),
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout[-3000:],
            "stderr": result.stderr[-500:],
        }
