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

    def generated(self, name: str) -> Path:
        return self.workspace(name) / "interns" / "generated"

    def state(self, name: str) -> Path:
        return self.workspace(name) / "interns" / "state"

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


class ReviewerProofService:
    """Aggregate workspace proof artifacts for the dashboard.

    This service is intentionally read-only. Commands that refresh artifacts stay
    in WorkspaceCommandService so the dashboard can keep reads and mutations
    separately testable.
    """

    def __init__(self, paths: DashboardPaths) -> None:
        self.paths = paths

    def load(self, workspace: str) -> dict[str, Any]:
        reports = self.paths.reports(workspace)
        generated = self.paths.generated(workspace)
        state = self.paths.state(workspace)
        graph = read_json(generated / "evidence_graph" / "graph.json", {})
        trajectory = read_json(reports / "trajectory" / "current.json", {})
        workflow_guard = read_json(reports / "workflow_guard_harness" / "current.json", {})
        reliability = read_json(reports / "reliability_suite" / "current.json", {})
        memory = read_json(reports / "memory_health" / "current.json", {})
        blocker = read_json(reports / "blocker_question_panel" / "current.json", {})
        project = read_json(generated / "evidence" / "project_harness.json", {})
        proof_packet = read_json(reports / "kpi_proof_packet" / "current.json", {})

        artifacts = [
            self._artifact("Reliability suite", reports / "reliability_suite" / "current.json", reliability),
            self._artifact("Workflow guardrails", reports / "workflow_guard_harness" / "current.json", workflow_guard),
            self._artifact("Evidence graph", generated / "evidence_graph" / "graph.json", graph),
            self._artifact("Memory health", reports / "memory_health" / "current.json", memory),
            self._artifact("Trajectory", reports / "trajectory" / "current.json", trajectory),
            self._artifact("Blocker panel", reports / "blocker_question_panel" / "current.json", blocker),
            self._artifact("Project harness", generated / "evidence" / "project_harness.json", project),
            self._artifact("KPI proof packet", reports / "kpi_proof_packet" / "current.json", proof_packet),
        ]
        missing = [item["path"] for item in artifacts if item["status"] == "missing"]
        blockers = self._blockers(workflow_guard, reliability, memory, project)
        return {
            "workspace": f"workspaces/{workspace}",
            "generated_at": self._latest_timestamp(artifacts),
            "status": "blocked" if blockers else ("incomplete" if missing else "ready"),
            "artifacts": artifacts,
            "missing_artifacts": missing,
            "blockers": blockers,
            "summary": self._summary(
                graph=graph,
                trajectory=trajectory,
                workflow_guard=workflow_guard,
                reliability=reliability,
                memory=memory,
                blocker=blocker,
                project=project,
                proof_packet=proof_packet,
            ),
            "graph": self._graph_summary(graph),
            "trajectory": self._trajectory_summary(trajectory, state / "trajectory.jsonl"),
            "workflow_guard": workflow_guard,
            "reliability": reliability,
            "memory": memory,
            "blocker": blocker,
            "project": project,
            "proof_packet": proof_packet,
        }

    def _artifact(self, label: str, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        exists = path.exists()
        stat = path.stat() if exists else None
        status = "present" if exists else "missing"
        if exists and payload.get("ok") is False:
            status = "blocked"
        return {
            "artifact": label,
            "status": status,
            "path": self._rel(path),
            "updated": int(stat.st_mtime) if stat else "",
            "generated_by": payload.get("generated_by", ""),
            "artifact_type": payload.get("artifact_type", ""),
        }

    def _summary(
        self,
        *,
        graph: dict[str, Any],
        trajectory: dict[str, Any],
        workflow_guard: dict[str, Any],
        reliability: dict[str, Any],
        memory: dict[str, Any],
        blocker: dict[str, Any],
        project: dict[str, Any],
        proof_packet: dict[str, Any],
    ) -> dict[str, Any]:
        graph_summary = graph.get("summary") or {}
        memory_summary = memory.get("summary") or {}
        trajectory_records = trajectory.get("events") or trajectory.get("records") or []
        return {
            "reliability_status": reliability.get("status", "missing"),
            "workflow_guard_status": workflow_guard.get("status", "missing"),
            "project_status": project.get("status", "missing"),
            "project_score": project.get("score", ""),
            "memory_status": memory.get("status", "missing"),
            "memory_entries": memory_summary.get("entry_count", 0),
            "memory_findings": len(memory.get("findings") or []),
            "graph_nodes": graph_summary.get("node_count", 0),
            "graph_edges": graph_summary.get("edge_count", 0),
            "introduced_terms": graph_summary.get("introduced_term_count", 0),
            "trajectory_events": len(trajectory_records),
            "active_blocker": blocker.get("feature", ""),
            "proof_kpis": len(proof_packet.get("kpis") or proof_packet.get("items") or []),
        }

    def _graph_summary(self, graph: dict[str, Any]) -> dict[str, Any]:
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        kind_counts: dict[str, int] = {}
        for node in nodes:
            kind = str(node.get("kind") or node.get("type") or "unknown")
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
        edge_counts: dict[str, int] = {}
        for edge in edges:
            kind = str(edge.get("type") or "unknown")
            edge_counts[kind] = edge_counts.get(kind, 0) + 1
        return {
            "node_kinds": kind_counts,
            "edge_types": edge_counts,
            "introduced_terms": (graph.get("queries") or {}).get("introduced_terms") or [],
        }

    def _trajectory_summary(self, trajectory: dict[str, Any], jsonl_path: Path) -> dict[str, Any]:
        records = trajectory.get("events") or trajectory.get("records") or []
        status_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for record in records:
            status = str(record.get("status") or "unknown")
            event_type = str(record.get("event_type") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            type_counts[event_type] = type_counts.get(event_type, 0) + 1
        return {
            "path": self._rel(jsonl_path),
            "status_counts": status_counts,
            "event_type_counts": type_counts,
            "recent": records[-12:],
        }

    def _blockers(
        self,
        workflow_guard: dict[str, Any],
        reliability: dict[str, Any],
        memory: dict[str, Any],
        project: dict[str, Any],
    ) -> list[str]:
        blockers = []
        for finding in workflow_guard.get("findings") or []:
            if finding.get("severity") in {"error", "critical", "blocker"}:
                blockers.append(f"Workflow guardrail: {finding.get('message') or finding.get('code')}")
        for check in reliability.get("checks") or []:
            if check.get("status") == "failed":
                blockers.append(f"Reliability check failed: {check.get('name') or check.get('check')}")
        for finding in memory.get("findings") or []:
            if finding.get("severity") == "critical":
                blockers.append(f"Memory health: {finding.get('message') or finding.get('code')}")
        blockers.extend(str(item) for item in project.get("blockers") or [])
        return blockers[:20]

    def _latest_timestamp(self, artifacts: list[dict[str, Any]]) -> int | str:
        stamps = [item["updated"] for item in artifacts if isinstance(item.get("updated"), int)]
        return max(stamps) if stamps else ""

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.paths.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()
