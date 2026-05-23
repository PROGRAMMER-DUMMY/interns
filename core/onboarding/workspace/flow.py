"""User-facing workspace workflow sessions.

This module is the quiet front door for agent UIs.  It runs the existing
governed tools in-process, persists a small session record, and returns compact
panels instead of streaming every lower-level command to the main chat.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.harness.trajectory_recorder import record_trajectory_event_safe
from core.onboarding.kpi.blocker_workflow import apply_kpi_panel_answer, prepare_kpi_blocker_panel
from core.onboarding.kpi.execution_harness import KPIExecutionHarness
from core.onboarding.kpi.generation_workflow import KPIGenerationWorkflow
from core.onboarding.kpi.sql_generator import DuckDBKPISQLGenerator
from core.onboarding.relationships.contracts import RelationshipContractBuilder
from core.onboarding.relationships.source_to_target_planner import SourceToTargetPlanner
from core.onboarding.workspace.onboarding import WorkspaceOnboarder
from core.onboarding.workspace.validation import WorkspaceArtifactValidator
from core.presentation.console_tables import render_query_result_table
from core.storage.workspace_layout import WorkspaceLayout
from tools.list_workspace_files import list_workspace_files


FLOW_VERSION = 1
INTENTS = {"kpi_generation", "usual_workflow", "full_kpi_sql"}


@dataclass(frozen=True)
class WorkspaceFlowResult:
    session_id: str
    workspace: str
    state_path: str
    stage: str
    status: str
    current_panel_path: str
    current_markdown_path: str
    next_step: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class WorkspaceFlow:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        domain: str = "healthcare",
        session_id: str = "",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.domain = domain
        self.session_id = session_id or _new_session_id()

    @classmethod
    def from_session(
        cls,
        repo_root: str | Path,
        session_id: str,
    ) -> "WorkspaceFlow":
        root = Path(repo_root).resolve()
        state = _find_session(root, session_id)
        return cls(
            root,
            state["workspace"],
            domain=state.get("domain", "healthcare"),
            session_id=session_id,
        )

    def start(self, *, intent: str = "kpi_generation") -> WorkspaceFlowResult:
        self._validate_workspace()
        if intent not in INTENTS:
            raise ValueError(f"unsupported intent: {intent}")
        self.layout.ensure_runtime_dirs()
        state = self._base_state(intent)
        self._record_event("workflow_start", "running", f"Started workspace-flow with intent `{intent}`.")
        listing = list_workspace_files(self.repo_root, self.workspace_rel).to_dict()
        self._record_step(state, "list_workspace_files", "ok", {"file_count": len(listing.get("files", []))})

        if intent == "kpi_generation":
            result = KPIGenerationWorkflow(self.repo_root, self.workspace_rel).prepare()
            panel = _read_json(self.repo_root / result.current_json_path)
            self._record_step(state, "prepare_kpi_generation", "ok", result.summary())
            return self._save_panel(
                state,
                panel=_compact_panel(
                    stage="kpi_generation_route",
                    status="needs_user_answer",
                    source_panel=panel,
                    instruction="Choose whether to generate/revise KPIs by interview or use the existing KPI workflow.",
                    artifact_paths=[result.current_json_path, result.current_markdown_path],
                ),
                source="kpi_generation",
            )

        return self._advance_until_stop(state)

    def answer(
        self,
        *,
        answer: str,
        custom_definition: str = "",
        evidence_note: str = "",
    ) -> WorkspaceFlowResult:
        state = self._load_state()
        current = state.get("current_panel", {})
        source = current.get("source", "")
        if source == "kpi_generation":
            result = KPIGenerationWorkflow(self.repo_root, self.workspace_rel).apply_answer(answer=answer)
            panel = _read_json(self.repo_root / result.current_json_path)
            self._record_step(
                state,
                "apply_kpi_generation_answer",
                "ok",
                result.summary(),
                decision=answer,
            )
            if result.stage == "usual_workflow_selected":
                state["intent"] = "usual_workflow"
                return self._advance_until_stop(state)
            return self._save_panel(
                state,
                panel=_compact_panel(
                    stage=f"kpi_generation_{result.stage}",
                    status="needs_user_answer" if result.status == "awaiting_user_answer" else result.status,
                    source_panel=panel,
                    instruction=_panel_instruction(panel),
                    artifact_paths=[result.current_json_path, result.current_markdown_path],
                ),
                source="kpi_generation",
            )

        if source == "kpi_blocker":
            result = apply_kpi_panel_answer(
                self.repo_root,
                self.workspace_rel,
                answer=answer,
                domain=self.domain,
                custom_definition=custom_definition,
                evidence_note=evidence_note,
            )
            self._record_step(state, "apply_kpi_panel_answer", "ok", result.summary(), decision=answer)
            return self._advance_until_stop(state)

        raise ValueError("current workflow stage is not waiting for a supported answer")

    def status(self) -> WorkspaceFlowResult:
        state = self._load_state()
        return self._result_from_state(state)

    def results(self, *, preview_rows: int = 20) -> WorkspaceFlowResult:
        state = self._load_state()
        preview = self._write_result_preview(preview_rows=preview_rows)
        self._record_step(state, "preview_kpi_results", "ok", preview)
        return self._save_panel(
            state,
            panel={
                "stage": "results",
                "status": "complete",
                "instruction": "Review generated KPI SQL and result previews.",
                "question": "",
                "options": [],
                "recommended_option_id": "",
                "artifact_paths": [preview["json_path"], preview["markdown_path"]],
                "summary": preview,
            },
            source="results",
        )

    def _advance_until_stop(self, state: dict[str, Any]) -> WorkspaceFlowResult:
        if not (self.layout.contracts_dir / "kpi_registry.json").exists():
            onboarding = WorkspaceOnboarder(self.repo_root, self.workspace_rel).run()
            self._record_step(state, "onboard_workspace", "ok", onboarding.summary())

        prepared = prepare_kpi_blocker_panel(
            self.repo_root,
            self.workspace_rel,
            domain=self.domain,
            onboard_if_missing=False,
        )
        self._record_step(state, "prepare_kpi_blocker_panel", "ok", prepared.summary())
        panel = _read_json(self.repo_root / prepared.question_panel_path)
        if panel.get("status") == "needs_user_answer":
            return self._save_panel(
                state,
                panel=_compact_panel(
                    stage="kpi_blocker",
                    status="needs_user_answer",
                    source_panel=panel,
                    instruction=_panel_instruction(panel),
                    artifact_paths=[prepared.question_panel_path, prepared.question_panel_markdown_path],
                ),
                source="kpi_blocker",
            )

        relationships = RelationshipContractBuilder(self.repo_root, self.workspace_rel).build()
        self._record_step(state, "build_relationship_contracts", "ok", relationships.summary())
        plan = SourceToTargetPlanner(self.repo_root, self.workspace_rel, target_engine="sql").build()
        self._record_step(state, "plan_source_to_target", "ok", plan.summary())
        if plan.blocked_kpi_count:
            return self._save_panel(
                state,
                panel={
                    "stage": "source_to_target_blocked",
                    "status": "blocked",
                    "instruction": "Source-to-target planning found blockers. Review the plan report.",
                    "question": "Resolve the blockers in the source-to-target plan before generating SQL.",
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [plan.json_path, plan.markdown_path],
                    "summary": plan.summary(),
                },
                source="source_to_target",
            )

        generated = []
        for idx in range(1, plan.kpi_count + 1):
            kpi_id = f"kpi_{idx:03d}"
            generated.append(DuckDBKPISQLGenerator(self.repo_root, self.workspace_rel).generate(kpi_id).summary())
        self._record_step(state, "generate_kpi_sql", "ok", {"generated": generated})
        harness = KPIExecutionHarness(self.repo_root, self.workspace_rel).run()
        self._record_step(
            state,
            "run_kpi_execution_harness",
            "ok" if harness.ok else "failed",
            harness.summary(),
            validation="run-kpi-execution-harness",
        )
        validation = WorkspaceArtifactValidator(self.repo_root, self.workspace_rel).run()
        self._record_step(
            state,
            "validate_workspace_artifacts",
            "ok" if validation.ok else "failed",
            validation.summary(),
            validation="validate-workspace-artifacts",
        )
        if not harness.ok:
            return self._save_panel(
                state,
                panel={
                    "stage": "execution_harness_failed",
                    "status": "blocked",
                    "instruction": "Generated KPI SQL did not pass the execution harness.",
                    "question": "Fix the generated SQL so each KPI creates its exact final result view.",
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [harness.manifest_path, harness.report_path],
                    "summary": harness.summary(),
                },
                source="execution_harness",
            )
        if not validation.ok:
            return self._save_panel(
                state,
                panel={
                    "stage": "validation_failed",
                    "status": "blocked",
                    "instruction": "Generated workspace artifacts failed validation.",
                    "question": "Fix validation errors before reviewing KPI results.",
                    "options": [],
                    "recommended_option_id": "",
                    "artifact_paths": [self.workspace_rel + "/interns/generated/evidence/kpi_execution_harness.json"],
                    "summary": validation.summary(),
                },
                source="validation",
            )
        preview = self._write_result_preview(preview_rows=20)
        self._record_step(state, "preview_kpi_results", "ok", preview)
        return self._save_panel(
            state,
            panel={
                "stage": "complete",
                "status": "complete",
                "instruction": "KPI SQL generation and previews are complete.",
                "question": "",
                "options": [],
                "recommended_option_id": "",
                "artifact_paths": [
                    self.workspace_rel + "/interns/generated/solutions",
                    preview["json_path"],
                    preview["markdown_path"],
                ],
                "summary": {
                    "generated_kpi_count": len(generated),
                    "validation": validation.summary(),
                    "results": preview,
                },
            },
            source="complete",
        )

    def _write_result_preview(self, *, preview_rows: int) -> dict[str, Any]:
        import duckdb

        self.layout.reports_dir.mkdir(parents=True, exist_ok=True)
        sql_files = sorted(path for path in self.layout.solutions_dir.glob("kpi_*.sql") if path.name != "kpi_metrics.sql")
        result_dir = self.layout.reports_dir / "kpi_results"
        evidence_dir = self.layout.evidence_dir / "kpi_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        conn = duckdb.connect(":memory:")
        old_cwd = Path.cwd()
        try:
            import os

            os.chdir(self.repo_root)
            for sql_file in sql_files:
                kpi_id = sql_file.stem
                sql_text = sql_file.read_text(encoding="utf-8")
                entry: dict[str, Any] = {
                    "kpi_id": kpi_id,
                    "sql_path": _rel(sql_file, self.repo_root),
                    "status": "ok",
                }
                try:
                    conn.execute(sql_text)
                    view = _result_view(conn, kpi_id)
                    if view:
                        cursor = conn.execute(f'SELECT * FROM "{view}" LIMIT {int(preview_rows)}')
                        entry["preview_markdown"] = render_query_result_table(cursor)
                        entry["result_view"] = view
                    else:
                        entry["status"] = "error"
                        entry["error"] = f"Exact result view `{kpi_id}_results` was not created."
                        entry["preview_markdown"] = "(no KPI result view found)"
                except Exception as exc:
                    entry["status"] = "error"
                    entry["error"] = str(exc)
                entries.append(entry)
        finally:
            import os

            os.chdir(old_cwd)
            conn.close()

        json_path = evidence_dir / "current.json"
        md_path = result_dir / "current.md"
        payload = {
            "artifact_type": "kpi_results/current.json",
            "version": FLOW_VERSION,
            "workspace": self.workspace_rel,
            "generated_at": _now(),
            "preview_rows": preview_rows,
            "kpis": entries,
        }
        json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        md_path.write_text(_render_results_markdown(payload), encoding="utf-8")
        return {
            "json_path": _rel(json_path, self.repo_root),
            "markdown_path": _rel(md_path, self.repo_root),
            "kpi_count": len(entries),
            "error_count": sum(1 for item in entries if item.get("status") != "ok"),
        }

    def _base_state(self, intent: str) -> dict[str, Any]:
        return {
            "artifact_type": "workspace_flow_session.json",
            "version": FLOW_VERSION,
            "session_id": self.session_id,
            "workspace": self.workspace_rel,
            "domain": self.domain,
            "intent": intent,
            "created_at": _now(),
            "updated_at": _now(),
            "status": "running",
            "stage": "start",
            "hidden_steps": [],
            "current_panel": {},
        }

    def _save_panel(
        self,
        state: dict[str, Any],
        *,
        panel: dict[str, Any],
        source: str,
    ) -> WorkspaceFlowResult:
        panel = {**panel, "source": source, "session_id": self.session_id, "workspace": self.workspace_rel}
        state["updated_at"] = _now()
        state["stage"] = panel.get("stage", "")
        state["status"] = panel.get("status", "")
        state["current_panel"] = panel
        self._write_state(state)
        panel_dir = self._session_dir()
        panel_json = panel_dir / "current.json"
        panel_md = panel_dir / "current.md"
        panel_json.write_text(json.dumps(panel, indent=2, default=str) + "\n", encoding="utf-8")
        panel_md.write_text(_render_panel_markdown(panel), encoding="utf-8")
        state["current_panel_path"] = _rel(panel_json, self.repo_root)
        state["current_markdown_path"] = _rel(panel_md, self.repo_root)
        self._write_state(state)
        event_type = "blocker_question" if panel.get("status") == "needs_user_answer" else "workflow_panel"
        self._record_event(
            event_type,
            str(panel.get("status") or ""),
            f"Workspace-flow saved panel `{panel.get('stage', '')}`.",
            artifact=_rel(panel_json, self.repo_root),
            metadata={"source": source, "session_id": self.session_id, "stage": panel.get("stage")},
        )
        return self._result_from_state(state)

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            raise FileNotFoundError(f"workspace-flow session not found: {self.session_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")

    def _result_from_state(self, state: dict[str, Any]) -> WorkspaceFlowResult:
        panel = state.get("current_panel", {})
        return WorkspaceFlowResult(
            session_id=self.session_id,
            workspace=self.workspace_rel,
            state_path=_rel(self._state_path(), self.repo_root),
            stage=str(state.get("stage") or panel.get("stage") or ""),
            status=str(state.get("status") or panel.get("status") or ""),
            current_panel_path=str(state.get("current_panel_path") or ""),
            current_markdown_path=str(state.get("current_markdown_path") or ""),
            next_step=_next_step(panel),
        )

    def _session_dir(self) -> Path:
        return self.layout.state_dir / "workflow_sessions" / self.session_id

    def _state_path(self) -> Path:
        return self._session_dir() / "session.json"

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _record_step(
        self,
        state: dict[str, Any],
        stage: str,
        status: str,
        detail: dict[str, Any],
        *,
        decision: str | None = None,
        validation: str | None = None,
    ) -> None:
        state["hidden_steps"].append(_step(stage, status, detail))
        self._record_event(
            "workflow_step",
            status,
            f"workspace-flow step `{stage}` completed with status `{status}`.",
            decision=decision,
            validation=validation,
            metadata={"session_id": self.session_id, "stage": stage, "detail": detail},
        )

    def _record_event(
        self,
        event_type: str,
        status: str,
        summary: str,
        *,
        artifact: str | None = None,
        decision: str | None = None,
        validation: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        record_trajectory_event_safe(
            self.repo_root,
            self.workspace_rel,
            event_type=event_type,
            status=status,
            summary=summary,
            artifact=artifact,
            decision=decision,
            validation=validation,
            metadata=metadata,
        )


def _compact_panel(
    *,
    stage: str,
    status: str,
    source_panel: dict[str, Any],
    instruction: str,
    artifact_paths: list[str],
) -> dict[str, Any]:
    return {
        "stage": stage,
        "status": status,
        "instruction": instruction,
        "question": source_panel.get("question", ""),
        "options": source_panel.get("options", []),
        "recommended_option_id": source_panel.get("recommended_option_id", ""),
        "why": source_panel.get("why", ""),
        "artifact_paths": artifact_paths,
        "source_panel_summary": {
            key: source_panel.get(key)
            for key in ("stage", "status", "feature", "applies_to_kpis", "reuse_scope")
            if key in source_panel
        },
    }


def _render_panel_markdown(panel: dict[str, Any]) -> str:
    lines = [
        f"# Workspace Flow: {panel.get('stage', '')}",
        "",
        f"- Session: `{panel.get('session_id', '')}`",
        f"- Workspace: `{panel.get('workspace', '')}`",
        f"- Status: `{panel.get('status', '')}`",
        "",
        "## Instruction",
        "",
        str(panel.get("instruction", "")),
        "",
    ]
    if panel.get("question"):
        lines.extend(["## Question", "", str(panel.get("question", "")), ""])
    if panel.get("options"):
        lines.extend(["## Options", ""])
        for option in panel.get("options", []):
            lines.extend(
                [
                    f"### {option.get('option_id', '')}: {option.get('label', '')}",
                    "",
                    str(option.get("business_summary") or option.get("description") or ""),
                    "",
                ]
            )
    if panel.get("recommended_option_id"):
        lines.extend(["## Suggested Default", "", f"`{panel.get('recommended_option_id')}`", ""])
    if panel.get("artifact_paths"):
        lines.extend(["## Artifacts", ""])
        lines.extend(f"- `{path}`" for path in panel.get("artifact_paths", []))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_results_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# KPI Query Results",
        "",
        f"- Workspace: `{payload.get('workspace', '')}`",
        f"- KPI count: {len(payload.get('kpis', []))}",
        "",
    ]
    for entry in payload.get("kpis", []):
        lines.extend(
            [
                f"## {entry.get('kpi_id')}",
                "",
                f"- SQL: `{entry.get('sql_path', '')}`",
                f"- Status: `{entry.get('status', '')}`",
                "",
            ]
        )
        if entry.get("error"):
            lines.extend(["```text", str(entry["error"]), "```", ""])
        else:
            lines.extend([str(entry.get("preview_markdown", "")), ""])
    return "\n".join(lines).rstrip() + "\n"


def _result_view(conn: Any, kpi_id: str) -> str:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.views WHERE lower(table_name) = lower(?)",
        [f"{kpi_id}_results"],
    ).fetchall()
    return str(rows[0][0]) if rows else ""


def _find_session(repo_root: Path, session_id: str) -> dict[str, Any]:
    matches = list(repo_root.glob(f"workspaces/*/interns/state/workflow_sessions/{session_id}/session.json"))
    if not matches:
        raise FileNotFoundError(f"workspace-flow session not found: {session_id}")
    return json.loads(matches[0].read_text(encoding="utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _step(stage: str, status: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {"stage": stage, "status": status, "detail": detail, "timestamp": _now()}


def _panel_instruction(panel: dict[str, Any]) -> str:
    return str(panel.get("instruction") or panel.get("recommended_answer") or "Choose an option.")


def _next_step(panel: dict[str, Any]) -> str:
    if panel.get("status") == "needs_user_answer":
        return "Answer this panel with `workspace-flow answer --session <id> --answer <option>`."
    if panel.get("status") == "complete":
        return "Review result artifacts or run `workspace-flow results --session <id>`."
    return "Inspect the current panel artifact."


def _new_session_id() -> str:
    return "wf_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workspace-flow")
    parser.add_argument("--repo-root", default=".")
    sub = parser.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start")
    start.add_argument("--workspace", required=True)
    start.add_argument("--domain", default="healthcare")
    start.add_argument("--intent", choices=sorted(INTENTS), default="kpi_generation")

    status = sub.add_parser("status")
    status.add_argument("--session", required=True)

    answer = sub.add_parser("answer")
    answer.add_argument("--session", required=True)
    answer.add_argument("--answer", required=True)
    answer.add_argument("--custom-definition", default="")
    answer.add_argument("--evidence-note", default="")

    results = sub.add_parser("results")
    results.add_argument("--session", required=True)
    results.add_argument("--preview-rows", type=int, default=20)

    args = parser.parse_args(argv)
    if args.cmd == "start":
        result = WorkspaceFlow(
            args.repo_root,
            args.workspace,
            domain=args.domain,
        ).start(intent=args.intent)
    elif args.cmd == "status":
        result = WorkspaceFlow.from_session(args.repo_root, args.session).status()
    elif args.cmd == "answer":
        result = WorkspaceFlow.from_session(args.repo_root, args.session).answer(
            answer=args.answer,
            custom_definition=args.custom_definition,
            evidence_note=args.evidence_note,
        )
    elif args.cmd == "results":
        result = WorkspaceFlow.from_session(args.repo_root, args.session).results(
            preview_rows=args.preview_rows,
        )
    else:
        raise SystemExit(2)
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
