"""
Autoresearch Dashboard
Run: python dashboard.py  (or: uv run python dashboard.py)

Reads from state/workspace.db (SQLite) when available,
falls back to state/*.tsv / state/*.json / state/*.jsonl flat files.
"""
import json
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import dash
import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objs as go

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
DB_PATH = STATE_DIR / "workspace.db"
RESULTS_TSV = STATE_DIR / "results.tsv"
LOOP_STATUS_JSON = STATE_DIR / "loop_status.json"
INTERN_LOG_JSONL = STATE_DIR / "intern_log.jsonl"
IDEAS_MD = STATE_DIR / "ideas.md"
CONFIG_TASKS = ROOT / "config" / "tasks.json"
TOOLS_REGISTRY = ROOT / ".agents" / "tools.json"

STATUS_COLORS = {
    "keep": "#28a745",
    "review": "#58a6ff",
    "discard": "#fd7e14",
    "crash": "#dc3545",
}
REFRESH_MS = 5_000


def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

# ─── Data loading ─────────────────────────────────────────────────────────────

def _active_task() -> dict:
    try:
        data = json.loads(CONFIG_TASKS.read_text())
        active_id = data.get("active_task", "")
        for t in data.get("tasks", []):
            if t["id"] == active_id:
                return t
    except Exception:
        pass
    return {}


def _task_workspace(task: dict) -> Path | None:
    workspace = task.get("workspace")
    if not workspace:
        return None
    path = ROOT / workspace
    return path if path.exists() else None


def _artifact_path(task: dict, *keys: str, fallback: Path | None = None) -> Path | None:
    value = task
    for key in keys:
        if not isinstance(value, dict):
            return fallback
        value = value.get(key)
    if value:
        return ROOT / str(value)
    return fallback


def _git_hygiene() -> dict:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=5,
        )
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc), "items": []}
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    changed = [line for line in lines if not line.startswith("??")]
    untracked = [line for line in lines if line.startswith("??")]
    return {
        "status": "clean" if not lines else "dirty",
        "count": len(lines),
        "changed": len(changed),
        "untracked": len(untracked),
        "items": lines[:30],
    }


def load_command_center(task: dict) -> dict:
    workspace = _task_workspace(task)
    tools = _read_json(TOOLS_REGISTRY, {"tools": [], "evidence_policy": {}})
    git = _git_hygiene()
    if not workspace:
        return {
            "workspace": "",
            "tools": tools,
            "mapping": {},
            "definitions": {"definitions": []},
            "profiles": {"profiles": []},
            "question_panel": {},
            "kpi_generation": {},
            "open_questions": "",
            "git": git,
        }

    mapping_path = _artifact_path(
        task,
        "semantic_contract",
        "feature_mapping",
        fallback=workspace / "interns" / "generated" / "contracts" / "kpi_feature_mapping.json",
    )
    definitions_path = workspace / "interns" / "generated" / "contracts" / "workspace_feature_definitions.json"
    profiles_path = workspace / "interns" / "generated" / "profiles" / "profile_index.json"
    question_panel_path = workspace / "interns" / "reports" / "blocker_question_panel" / "current.json"
    kpi_generation_session_path = (
        workspace / "interns" / "generated" / "requirements" / "kpi_generation_session.json"
    )
    kpi_generation_panel_path = workspace / "interns" / "reports" / "kpi_generation" / "current.json"
    kpi_generation_draft_path = (
        workspace / "interns" / "generated" / "requirements" / "kpi_registry_draft.json"
    )
    kpi_generation_proof_path = (
        workspace / "interns" / "generated" / "requirements" / "kpi_generation_production_proof.json"
    )
    open_questions_path = _artifact_path(
        task,
        "enterprise_artifacts",
        "open_questions",
        fallback=workspace / "interns" / "reports" / "open_questions.md",
    )
    open_questions = ""
    if open_questions_path and open_questions_path.exists():
        try:
            open_questions = open_questions_path.read_text(encoding="utf-8")
        except Exception:
            open_questions = ""
    return {
        "workspace": str(workspace.relative_to(ROOT)).replace("\\", "/"),
        "tools": tools,
        "mapping": _read_json(mapping_path, {}) if mapping_path else {},
        "definitions": _read_json(definitions_path, {"definitions": []}),
        "profiles": _read_json(profiles_path, {"profiles": []}),
        "question_panel": _read_json(question_panel_path, {}),
        "kpi_generation": {
            "session": _read_json(kpi_generation_session_path, {}),
            "current_panel": _read_json(kpi_generation_panel_path, {}),
            "draft": _read_json(kpi_generation_draft_path, {}),
            "production_proof": _read_json(kpi_generation_proof_path, {}),
        },
        "open_questions": open_questions,
        "git": git,
    }


def load_data():
    """Return dashboard state from SQLite or flat-file fallback."""
    results, intern_logs, loop_status, ideas = [], [], {}, ""
    governance, alerts = [], []

    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row

        cur = conn.execute("SELECT * FROM experiments ORDER BY id ASC")
        for i, row in enumerate(cur.fetchall(), 1):
            raw = row["metrics"]
            try:
                metric = float(raw)
            except (TypeError, ValueError):
                metric = None
            results.append({
                "id": i,
                "commit": (row["run_id"] or "")[:8],
                "metric": metric,
                "status": row["status"] or "unknown",
                "description": (row["results"] or "")[:120],
                "timestamp": row["timestamp"] or "",
            })

        cur = conn.execute(
            "SELECT * FROM intern_activity ORDER BY id DESC LIMIT 50"
        )
        for row in cur.fetchall():
            details = {}
            try:
                details = json.loads(row["details"] or "{}")
            except Exception:
                pass
            intern_logs.append({
                "intern": row["intern_name"] or "",
                "activity": row["activity"] or "",
                "elapsed_s": details.get("elapsed_s", ""),
                "timestamp": row["timestamp"] or "",
                "snippet": str(details.get("response", ""))[:200],
            })

        cur = conn.execute(
            "SELECT content FROM logs WHERE log_type='loop_status' ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row and row["content"]:
            loop_status = json.loads(row["content"])

        cur = conn.execute(
            "SELECT content FROM logs WHERE log_type='ideas' ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row and row["content"]:
            ideas = row["content"]

        try:
            cur = conn.execute(
                "SELECT * FROM governance_decisions ORDER BY id DESC LIMIT 20"
            )
            for row in cur.fetchall():
                try:
                    gates = json.loads(row["gates"] or "[]")
                except Exception:
                    gates = []
                governance.append({
                    "run_id": row["run_id"] or "",
                    "task_id": row["task_id"] or "",
                    "decision": row["decision"] or "",
                    "approval_state": row["approval_state"] or "",
                    "promotion_allowed": bool(row["promotion_allowed"]),
                    "rationale": row["rationale"] or "",
                    "failed_gates": ", ".join(
                        gate.get("gate_id", "")
                        for gate in gates
                        if not gate.get("passed", False) and gate.get("severity") == "error"
                    ),
                    "timestamp": row["timestamp"] or "",
                })
        except sqlite3.OperationalError:
            governance = []

        try:
            cur = conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 30")
            for row in cur.fetchall():
                alerts.append({
                    "severity": row["severity"] or "",
                    "title": row["title"] or "",
                    "message": row["message"] or "",
                    "run_id": row["run_id"] or "",
                    "requires_human": bool(row["requires_human"]),
                    "timestamp": row["timestamp"] or "",
                })
        except sqlite3.OperationalError:
            alerts = []

        conn.close()

    else:
        # Flat-file fallback
        if RESULTS_TSV.exists():
            lines = RESULTS_TSV.read_text().splitlines()
            for i, line in enumerate(lines[1:], 1):
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                try:
                    metric = float(parts[1])
                except (ValueError, IndexError):
                    metric = None
                results.append({
                    "id": i,
                    "commit": parts[0][:8],
                    "metric": metric,
                    "status": parts[3] if len(parts) > 3 else "unknown",
                    "description": (parts[4] if len(parts) > 4 else "")[:120],
                    "timestamp": "",
                })

        if LOOP_STATUS_JSON.exists():
            loop_status = json.loads(LOOP_STATUS_JSON.read_text())

        if INTERN_LOG_JSONL.exists():
            raw_lines = INTERN_LOG_JSONL.read_text().splitlines()
            for line in reversed(raw_lines[-50:]):
                try:
                    entry = json.loads(line)
                    intern_logs.append({
                        "intern": entry.get("intern", ""),
                        "activity": entry.get("request", "")[:80],
                        "elapsed_s": entry.get("elapsed_s", ""),
                        "timestamp": entry.get("ts", ""),
                        "snippet": str(entry.get("response", ""))[:200],
                    })
                except Exception:
                    pass

        if IDEAS_MD.exists():
            ideas = IDEAS_MD.read_text()

    return results, intern_logs, loop_status, ideas, governance, alerts


def get_git_diff() -> str:
    task = _active_task()
    editable = task.get("editable_file", "")
    if not editable:
        return "No editable file configured."
    try:
        log = subprocess.run(
            ["git", "log", "--oneline", "-15"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=5
        )
        return log.stdout or "No git log available."
    except Exception as e:
        return f"Git unavailable: {e}"


def get_editable_file_content() -> str:
    task = _active_task()
    editable = task.get("editable_file", "")
    if not editable:
        return ""
    path = ROOT / editable
    if path.exists():
        return path.read_text()
    # Try workspace dir
    ws = ROOT / "workspaces" / task.get("domain", "") / Path(editable).name
    if ws.exists():
        return ws.read_text()
    return f"File not found: {editable}"


# ─── App setup ────────────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    title="Autoresearch Dashboard",
    update_title=None,
)

# ─── Layout helpers ───────────────────────────────────────────────────────────

def _card(title: str, body_id: str, color: str = "#1e2a3a") -> dbc.Card:
    return dbc.Card(
        [
            dbc.CardHeader(
                title,
                style={
                    "background": "#0d1b2a",
                    "color": "#58a6ff",
                    "fontWeight": "600",
                    "fontSize": "0.85rem",
                    "letterSpacing": "0.05em",
                    "textTransform": "uppercase",
                    "borderBottom": "1px solid #243447",
                    "padding": "10px 16px",
                },
            ),
            dbc.CardBody(id=body_id, style={"padding": "14px", "background": color}),
        ],
        style={"border": "1px solid #243447", "borderRadius": "8px", "marginBottom": "16px"},
    )


def _render_kpi_generation_panel(kpi_generation: dict) -> html.Div:
    session = kpi_generation.get("session") or {}
    current = kpi_generation.get("current_panel") or {}
    draft = kpi_generation.get("draft") or {}
    proof = kpi_generation.get("production_proof") or {}
    if not any([session, current, draft, proof]):
        return html.Div(
            [
                html.Div(
                    "No KPI generation session found.",
                    style={"color": "#c9d1d9", "fontWeight": "600", "marginBottom": "8px"},
                ),
                html.Code(
                    "uv run prepare-kpi-generation --workspace <workspace>",
                    style={
                        "display": "block",
                        "background": "#0d1b2a",
                        "color": "#58a6ff",
                        "padding": "10px",
                        "borderRadius": "4px",
                        "fontSize": "0.78rem",
                    },
                ),
            ],
            style={"padding": "22px", "textAlign": "center"},
        )

    quality = session.get("quality_score") or current.get("quality_score") or {}
    options = current.get("options") or []
    draft_kpis = draft.get("kpis") or current.get("draft_kpis") or session.get("draft_kpis") or []
    draft_proofs = draft.get("proofs") or current.get("draft_proofs") or session.get("draft_proofs") or []
    competitive = (
        draft.get("competitive_review")
        or current.get("competitive_review")
        or session.get("competitive_review")
        or {}
    )
    checks = proof.get("checks") or []

    return html.Div(
        [
            dbc.Row(
                [
                    dbc.Col(
                        [
                            _subhead("Session"),
                            _mini_kv("Workspace", session.get("workspace") or current.get("workspace") or ""),
                            _mini_kv("Stage", current.get("stage") or session.get("current_stage", "")),
                            _mini_kv("Status", session.get("status", "not started")),
                            _mini_kv(
                                "Recommended",
                                current.get("recommended_option_id", ""),
                                "#28a745",
                            ),
                        ],
                        md=3,
                    ),
                    dbc.Col(
                        [
                            _subhead("Quality"),
                            _score_row(
                                "Implementation",
                                quality.get("implementation_readiness", 0),
                                "#58a6ff",
                            ),
                            _score_row("Business", quality.get("business_quality", 0), "#d2a8ff"),
                            _score_row("Overall", quality.get("overall_score", 0), "#28a745"),
                            html.Div(
                                f"{quality.get('kpi_count', 0)} KPI(s) scored",
                                style={"color": "#8b949e", "fontSize": "0.76rem", "marginTop": "8px"},
                            ),
                        ],
                        md=3,
                    ),
                    dbc.Col(
                        [
                            _subhead("Current Question"),
                            html.Div(
                                current.get("question", "No active KPI generation question."),
                                style={
                                    "color": "#f0f6fc",
                                    "fontWeight": "600",
                                    "lineHeight": "1.4",
                                    "marginBottom": "8px",
                                },
                            ),
                            html.Div(
                                current.get("why", ""),
                                style={"color": "#8b949e", "fontSize": "0.76rem", "lineHeight": "1.45"},
                            ),
                        ],
                        md=6,
                    ),
                ],
                className="g-3",
            ),
            html.Hr(style={"borderColor": "#243447", "margin": "14px 0"}),
            dbc.Row(
                [
                    dbc.Col(_option_list(options), md=4),
                    dbc.Col(_draft_kpi_table(draft_kpis), md=5),
                    dbc.Col(_proof_and_advisor_panel(draft_proofs, competitive, checks), md=3),
                ],
                className="g-3",
            ),
        ]
    )


def _subhead(label: str) -> html.Div:
    return html.Div(
        label,
        style={
            "color": "#58a6ff",
            "fontSize": "0.72rem",
            "fontWeight": "700",
            "textTransform": "uppercase",
            "marginBottom": "8px",
        },
    )


def _mini_kv(label: str, value: object, color: str = "#f0f6fc") -> html.Div:
    return html.Div(
        [
            html.Span(label, style={"color": "#8b949e", "fontSize": "0.74rem"}),
            html.Span(
                str(value or "-"),
                style={"color": color, "float": "right", "fontWeight": "600", "fontSize": "0.76rem"},
            ),
        ],
        style={"padding": "6px 0", "borderBottom": "1px solid #21262d"},
    )


def _score_row(label: str, value: object, color: str) -> html.Div:
    try:
        score = max(0, min(100, int(value)))
    except (TypeError, ValueError):
        score = 0
    return html.Div(
        [
            html.Div(
                [
                    html.Span(label, style={"color": "#8b949e", "fontSize": "0.74rem"}),
                    html.Span(
                        f"{score}%",
                        style={"color": color, "float": "right", "fontWeight": "700"},
                    ),
                ],
                style={"marginBottom": "4px"},
            ),
            html.Div(
                html.Div(style={"width": f"{score}%", "height": "6px", "background": color}),
                style={"height": "6px", "background": "#0d1b2a", "borderRadius": "3px"},
            ),
        ],
        style={"marginBottom": "9px"},
    )


def _option_list(options: list[dict]) -> html.Div:
    rows = [_subhead("Options")]
    if not options:
        rows.append(html.Div("No active options.", style={"color": "#8b949e", "fontSize": "0.78rem"}))
        return html.Div(rows)
    for option in options[:4]:
        rows.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(
                                option.get("option_id", ""),
                                style={"color": "#58a6ff", "fontWeight": "700"},
                            ),
                            html.Span(
                                option.get("value", ""),
                                style={"color": "#8b949e", "float": "right", "fontSize": "0.72rem"},
                            ),
                        ]
                    ),
                    html.Div(
                        option.get("label", ""),
                        style={"color": "#f0f6fc", "fontWeight": "600", "fontSize": "0.82rem"},
                    ),
                    html.Div(
                        option.get("description", ""),
                        style={"color": "#8b949e", "fontSize": "0.74rem", "lineHeight": "1.35"},
                    ),
                ],
                style={"padding": "8px 0", "borderBottom": "1px solid #21262d"},
            )
        )
    return html.Div(rows, style={"maxHeight": "295px", "overflowY": "auto"})


def _draft_kpi_table(draft_kpis: list[dict]) -> html.Div:
    if not draft_kpis:
        return html.Div(
            [_subhead("Draft KPIs"), html.Div("No draft KPI registry yet.", style={"color": "#8b949e"})]
        )
    rows = [
        {
            "KPI": item.get("kpi_id", ""),
            "Question": item.get("business_question", ""),
            "Metric": item.get("metric", ""),
            "Status": item.get("status", ""),
        }
        for item in draft_kpis
    ]
    return html.Div(
        [
            _subhead("Draft KPI Registry"),
            dash_table.DataTable(
                data=rows,
                columns=[{"name": c, "id": c} for c in ["KPI", "Question", "Metric", "Status"]],
                page_size=5,
                style_table={"overflowX": "auto"},
                style_cell={
                    "background": "#0d1b2a",
                    "color": "#c9d1d9",
                    "border": "1px solid #21262d",
                    "fontSize": "0.74rem",
                    "fontFamily": "monospace",
                    "padding": "6px 8px",
                    "textAlign": "left",
                    "whiteSpace": "normal",
                    "height": "auto",
                },
                style_header={
                    "background": "#161b22",
                    "color": "#58a6ff",
                    "fontWeight": "600",
                    "border": "1px solid #21262d",
                },
            ),
        ]
    )


def _proof_and_advisor_panel(
    draft_proofs: list[dict],
    competitive: dict,
    checks: list[dict],
) -> html.Div:
    missing = competitive.get("missing_discussion_points") or []
    suggestions = competitive.get("ranked_suggestions") or []
    rows = [
        _subhead("Proof & Advisor"),
        _mini_kv("Draft proof", f"{len(draft_proofs)} KPI(s)", "#58a6ff"),
        _mini_kv("Production checks", len(checks), "#d2a8ff"),
        _mini_kv("Missing points", len(missing), "#fd7e14" if missing else "#28a745"),
    ]
    if missing:
        rows.append(
            html.Div(
                ", ".join(missing[:8]),
                style={"color": "#fd7e14", "fontSize": "0.74rem", "lineHeight": "1.4"},
            )
        )
    if suggestions:
        rows.append(html.Div("Advisor", style={"color": "#8b949e", "fontSize": "0.74rem", "marginTop": "8px"}))
        rows.extend(
            html.Div(
                suggestion,
                style={
                    "color": "#c9d1d9",
                    "fontSize": "0.74rem",
                    "padding": "5px 0",
                    "borderBottom": "1px solid #21262d",
                },
            )
            for suggestion in suggestions[:3]
        )
    if checks:
        rows.append(html.Div("Final Gate", style={"color": "#8b949e", "fontSize": "0.74rem", "marginTop": "8px"}))
        rows.extend(
            html.Div(
                [
                    html.Span(check.get("check", ""), style={"color": "#c9d1d9"}),
                    html.Span(
                        check.get("status", ""),
                        style={"color": "#fd7e14", "float": "right", "fontSize": "0.72rem"},
                    ),
                ],
                style={"fontSize": "0.72rem", "padding": "4px 0"},
            )
            for check in checks[:5]
        )
    return html.Div(rows, style={"maxHeight": "295px", "overflowY": "auto"})


app.layout = dbc.Container(
    fluid=True,
    style={"background": "#0b1622", "minHeight": "100vh", "padding": "20px"},
    children=[
        dcc.Interval(id="tick", interval=REFRESH_MS, n_intervals=0),

        # ── Header ────────────────────────────────────────────────────────────
        dbc.Row(
            dbc.Col(
                html.Div(
                    [
                        html.H3(
                            "Autoresearch",
                            style={"color": "#58a6ff", "margin": 0, "fontWeight": "700"},
                        ),
                        html.Span(
                            id="header-task-label",
                            style={"color": "#8b949e", "fontSize": "0.85rem", "marginLeft": "16px"},
                        ),
                    ],
                    style={"display": "flex", "alignItems": "baseline", "marginBottom": "16px"},
                )
            )
        ),

        # ── Status bar ────────────────────────────────────────────────────────
        dbc.Row(id="status-bar", className="g-3 mb-2"),

        dbc.Row(
            [
                dbc.Col(_card("Foundation Status", "foundation-panel"), md=4),
                dbc.Col(_card("Top Blockers", "blocker-panel"), md=4),
                dbc.Col(_card("Tool Registry", "tool-panel"), md=4),
            ],
            className="g-3",
        ),

        dbc.Row(
            [
                dbc.Col(_card("Question Panel", "question-panel"), md=12),
            ],
            className="g-3",
        ),

        dbc.Row(
            [
                dbc.Col(_card("KPI Generation", "kpi-generation-panel"), md=12),
            ],
            className="g-3",
        ),

        dbc.Row(
            [
                dbc.Col(_card("Workspace Evidence", "evidence-panel"), md=7),
                dbc.Col(_card("Git Hygiene", "git-hygiene-panel"), md=5),
            ],
            className="g-3",
        ),

        # ── Metric chart + Status pie ─────────────────────────────────────────
        dbc.Row(
            [
                dbc.Col(_card("Metric History", "metric-chart"), md=8),
                dbc.Col(_card("Run Status Distribution", "status-pie"), md=4),
            ],
            className="g-3",
        ),

        # ── Intern feed + Git diff ─────────────────────────────────────────────
        dbc.Row(
            [
                dbc.Col(_card("Intern Activity (last 20)", "intern-feed"), md=6),
                dbc.Col(_card("Git Log (recent commits)", "git-log"), md=6),
            ],
            className="g-3",
        ),

        # ── Results table ─────────────────────────────────────────────────────
        _card("Experiment Results", "results-table"),

        dbc.Row(
            [
                dbc.Col(_card("Governance Decisions", "governance-panel"), md=7),
                dbc.Col(_card("Human Alerts", "alerts-panel"), md=5),
            ],
            className="g-3",
        ),

        # ── Ideas ─────────────────────────────────────────────────────────────
        _card("Ideas & Hypotheses", "ideas-panel"),
    ],
)

# ─── Callbacks ────────────────────────────────────────────────────────────────

@app.callback(
    [
        Output("header-task-label", "children"),
        Output("status-bar", "children"),
        Output("foundation-panel", "children"),
        Output("blocker-panel", "children"),
        Output("tool-panel", "children"),
        Output("question-panel", "children"),
        Output("kpi-generation-panel", "children"),
        Output("evidence-panel", "children"),
        Output("git-hygiene-panel", "children"),
        Output("metric-chart", "children"),
        Output("status-pie", "children"),
        Output("intern-feed", "children"),
        Output("git-log", "children"),
        Output("results-table", "children"),
        Output("governance-panel", "children"),
        Output("alerts-panel", "children"),
        Output("ideas-panel", "children"),
    ],
    Input("tick", "n_intervals"),
)
def refresh(_n):
    results, intern_logs, loop_status, ideas, governance, alerts = load_data()
    task = _active_task()
    command_center = load_command_center(task)

    # ── Header label ─────────────────────────────────────────────────────────
    task_label = task.get("name", "No active task")

    # ── Status bar KPI cards ──────────────────────────────────────────────────
    exp_count = loop_status.get("experiment_count", len(results))
    best_metric = loop_status.get("best_metric", None)
    if best_metric is None and results:
        valid = [r["metric"] for r in results if r["metric"] is not None]
        best_metric = max(valid) if valid else None

    state = loop_status.get("state", loop_status.get("running", False))
    if isinstance(state, bool):
        state = "running" if state else "idle"
    state_color = "#28a745" if state == "running" else "#8b949e"

    last_updated = loop_status.get("last_updated", "")
    if last_updated:
        try:
            dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            last_updated = dt.strftime("%H:%M:%S UTC")
        except Exception:
            pass

    def _kpi(label, value, color="#f0f6fc"):
        return dbc.Col(
            dbc.Card(
                dbc.CardBody(
                    [
                        html.Div(label, style={"color": "#8b949e", "fontSize": "0.7rem", "textTransform": "uppercase", "letterSpacing": "0.06em"}),
                        html.Div(value, style={"color": color, "fontSize": "1.6rem", "fontWeight": "700", "marginTop": "4px"}),
                    ],
                    style={"padding": "12px 16px"},
                ),
                style={"background": "#0d1b2a", "border": "1px solid #243447", "borderRadius": "8px"},
            ),
            xs=6, sm=4, md=3, lg=2,
        )

    status_bar = [
        _kpi("Experiments", str(exp_count)),
        _kpi("Best Metric", f"{best_metric:.4f}" if best_metric is not None else "—", "#58a6ff"),
        _kpi("State", state.upper(), state_color),
        _kpi("Last Updated", last_updated or "—"),
        _kpi(
            "Keep Rate",
            f"{sum(1 for r in results if r['status']=='keep')}/{len(results)}" if results else "—",
            "#28a745",
        ),
        _kpi(
            "Max Possible",
            str(loop_status.get("max_experiments", "100")),
        ),
    ]

    # ── Metric history chart ──────────────────────────────────────────────────
    mapping = command_center.get("mapping", {})
    summary = mapping.get("summary", {})
    blockers = mapping.get("blocker_clusters", [])
    definitions = command_center.get("definitions", {}).get("definitions", [])
    profiles = command_center.get("profiles", {}).get("profiles", [])
    question_panel = command_center.get("question_panel", {})
    kpi_generation = command_center.get("kpi_generation", {})
    tools = command_center.get("tools", {}).get("tools", [])
    evidence_policy = command_center.get("tools", {}).get("evidence_policy", {})
    git = command_center.get("git", {})
    open_question_count = sum(
        1
        for line in command_center.get("open_questions", "").splitlines()
        if line.strip().startswith("- ")
    )

    def _kv(label, value, color="#f0f6fc"):
        return html.Div(
            [
                html.Span(label, style={"color": "#8b949e", "fontSize": "0.75rem"}),
                html.Span(str(value), style={"color": color, "float": "right", "fontWeight": "600"}),
            ],
            style={"padding": "6px 0", "borderBottom": "1px solid #21262d"},
        )

    foundation_panel = html.Div(
        [
            _kv("Workspace", command_center.get("workspace") or "none", "#58a6ff"),
            _kv("KPI ready", summary.get("ready_kpi_count", 0), "#28a745"),
            _kv("KPI blocked", summary.get("blocked_kpi_count", 0), "#fd7e14"),
            _kv("Unresolved features", summary.get("unresolved_feature_count", 0), "#fd7e14"),
            _kv("Workspace definitions", len(definitions), "#d2a8ff"),
            _kv("Dataset profiles", len(profiles), "#58a6ff"),
            _kv("Open questions", open_question_count, "#f0f6fc"),
        ]
    )

    if blockers:
        blocker_panel = html.Div(
            [
                html.Div(
                    [
                        html.Span(item.get("feature", ""), style={"color": "#f0f6fc", "fontWeight": "600"}),
                        html.Span(f"{item.get('count', 0)} KPI(s)", style={"color": "#58a6ff", "float": "right"}),
                        html.Div(item.get("risk", ""), style={"color": "#8b949e", "fontSize": "0.72rem", "marginTop": "2px"}),
                    ],
                    style={"padding": "8px 0", "borderBottom": "1px solid #21262d"},
                )
                for item in blockers[:8]
            ],
            style={"maxHeight": "285px", "overflowY": "auto"},
        )
    else:
        blocker_panel = html.Div("No unresolved blocker clusters detected.", style={"color": "#8b949e", "textAlign": "center", "padding": "40px"})

    question_options = question_panel.get("options") or []
    if question_panel:
        question_panel_view = html.Div(
            [
                html.Div(
                    [
                        html.Span(
                            question_panel.get("feature", "Current blocker"),
                            style={"color": "#f0f6fc", "fontWeight": "700", "fontSize": "1rem"},
                        ),
                        html.Span(
                            question_panel.get("reuse_scope", ""),
                            style={"color": "#58a6ff", "float": "right", "fontSize": "0.78rem"},
                        ),
                    ],
                    style={"marginBottom": "8px"},
                ),
                html.Div(question_panel.get("blocker", ""), style={"color": "#c9d1d9", "marginBottom": "8px"}),
                html.Div(question_panel.get("question", ""), style={"color": "#f0f6fc", "fontWeight": "600", "marginBottom": "8px"}),
                html.Div(
                    [
                        html.Span("Recommended: ", style={"color": "#8b949e"}),
                        html.Span(question_panel.get("recommended_answer", ""), style={"color": "#28a745"}),
                    ],
                    style={"marginBottom": "6px"},
                ),
                html.Div(question_panel.get("why", ""), style={"color": "#8b949e", "fontSize": "0.78rem", "marginBottom": "10px"}),
                html.Div(
                    [
                        html.Div(
                            [
                                html.Div(
                                    [
                                        html.Span(option.get("option_id", ""), style={"color": "#58a6ff", "fontWeight": "700"}),
                                        html.Span(" JSON" if option.get("json_backed") else " Manual", style={"color": "#8b949e", "float": "right"}),
                                    ]
                                ),
                                html.Div(option.get("label", ""), style={"color": "#f0f6fc", "fontWeight": "600"}),
                                html.Div(option.get("business_summary", ""), style={"color": "#8b949e", "fontSize": "0.75rem"}),
                                html.Code(
                                    option.get("formula", ""),
                                    style={
                                        "display": "block" if option.get("formula") else "none",
                                        "color": "#c9d1d9",
                                        "background": "#0d1b2a",
                                        "padding": "6px",
                                        "marginTop": "6px",
                                        "whiteSpace": "pre-wrap",
                                    },
                                ),
                            ],
                            style={"borderTop": "1px solid #21262d", "padding": "8px 0"},
                        )
                        for option in question_options[:4]
                    ],
                    style={"maxHeight": "260px", "overflowY": "auto"},
                ),
            ]
        )
    else:
        question_panel_view = html.Div(
            "No blocker question panel found. Run uv run blocker-question-panel --workspace <workspace> after feature resolution.",
            style={"color": "#8b949e", "textAlign": "center", "padding": "28px"},
        )

    kpi_generation_panel = _render_kpi_generation_panel(kpi_generation)

    tool_panel = html.Div(
        [
            _kv("Dataset evidence order", "profile -> bounded sample", "#58a6ff"),
            _kv("Registry tools", len(tools), "#d2a8ff"),
            *[
                html.Div(
                    [
                        html.Div(tool.get("name", ""), style={"color": "#f0f6fc", "fontWeight": "600"}),
                        html.Div(tool.get("safety", ""), style={"color": "#8b949e", "fontSize": "0.72rem"}),
                    ],
                    style={"padding": "7px 0", "borderBottom": "1px solid #21262d"},
                )
                for tool in tools[:6]
            ],
            html.Div(
                f"Raw data rule: {evidence_policy.get('raw_dataset_rule', 'profile-first')}",
                style={"color": "#8b949e", "fontSize": "0.74rem", "marginTop": "8px"},
            ),
        ],
        style={"maxHeight": "285px", "overflowY": "auto"},
    )

    evidence_rows = []
    for profile in profiles[:20]:
        schema = profile.get("schema") or {}
        evidence_rows.append(
            {
                "Dataset": Path(profile.get("path", "")).name,
                "Rows": profile.get("row_count", ""),
                "Cols": len(schema),
                "Mode": ", ".join(profile.get("sources_used", [])),
                "Warnings": len(profile.get("warnings", [])),
            }
        )
    if evidence_rows:
        evidence_panel = dash_table.DataTable(
            data=evidence_rows,
            columns=[{"name": c, "id": c} for c in ["Dataset", "Rows", "Cols", "Mode", "Warnings"]],
            page_size=8,
            style_table={"overflowX": "auto"},
            style_cell={
                "background": "#0d1b2a",
                "color": "#c9d1d9",
                "border": "1px solid #21262d",
                "fontSize": "0.76rem",
                "fontFamily": "monospace",
                "padding": "6px 8px",
                "textAlign": "left",
            },
            style_header={
                "background": "#161b22",
                "color": "#58a6ff",
                "fontWeight": "600",
                "border": "1px solid #21262d",
            },
        )
    else:
        evidence_panel = html.Div("No profile index found.", style={"color": "#8b949e", "textAlign": "center", "padding": "40px"})

    git_items = git.get("items", [])
    git_hygiene_panel = html.Div(
        [
            _kv("Status", git.get("status", "unknown"), "#28a745" if git.get("status") == "clean" else "#fd7e14"),
            _kv("Changed files", git.get("changed", 0), "#f0f6fc"),
            _kv("Untracked files", git.get("untracked", 0), "#f0f6fc"),
            html.Pre(
                "\n".join(git_items) if git_items else "clean",
                style={
                    "color": "#c9d1d9",
                    "background": "#0d1b2a",
                    "padding": "10px",
                    "borderRadius": "4px",
                    "fontSize": "0.72rem",
                    "maxHeight": "190px",
                    "overflowY": "auto",
                    "whiteSpace": "pre-wrap",
                    "marginTop": "10px",
                },
            ),
        ]
    )

    valid_results = [r for r in results if r["metric"] is not None]
    if valid_results:
        x = [r["id"] for r in valid_results]
        y = [r["metric"] for r in valid_results]
        colors = [STATUS_COLORS.get(r["status"], "#8b949e") for r in valid_results]
        best_y = max(y) if y else 0

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=x, y=y, mode="lines",
            line=dict(color="#243447", width=1.5),
            showlegend=False, hoverinfo="skip",
        ))
        fig.add_trace(go.Scatter(
            x=x, y=y, mode="markers",
            marker=dict(color=colors, size=8, line=dict(color="#0b1622", width=1)),
            text=[f"#{r['id']} {r['commit']}<br>{r['status']}<br>{r['metric']:.4f}" for r in valid_results],
            hovertemplate="%{text}<extra></extra>",
            name="Experiments",
        ))
        fig.add_hline(
            y=best_y,
            line_dash="dot",
            line_color="#58a6ff",
            annotation_text=f"Best: {best_y:.4f}",
            annotation_position="top right",
            annotation_font_color="#58a6ff",
        )
        fig.update_layout(
            paper_bgcolor="#1e2a3a", plot_bgcolor="#1e2a3a",
            font=dict(color="#8b949e", size=11),
            xaxis=dict(title="Experiment #", gridcolor="#243447", showline=False),
            yaxis=dict(title="Primary Metric", gridcolor="#243447", showline=False),
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=False,
            height=280,
        )
        metric_chart = dcc.Graph(figure=fig, config={"displayModeBar": False})
    else:
        metric_chart = html.Div("No metric data yet.", style={"color": "#8b949e", "textAlign": "center", "padding": "60px"})

    # ── Status pie ────────────────────────────────────────────────────────────
    if results:
        from collections import Counter
        counts = Counter(r["status"] for r in results)
        labels = list(counts.keys())
        values = list(counts.values())
        pie_colors = [STATUS_COLORS.get(label, "#8b949e") for label in labels]
        pie_fig = go.Figure(go.Pie(
            labels=labels, values=values,
            marker=dict(colors=pie_colors, line=dict(color="#0b1622", width=2)),
            textfont=dict(color="#f0f6fc"),
            hole=0.45,
        ))
        pie_fig.update_layout(
            paper_bgcolor="#1e2a3a", plot_bgcolor="#1e2a3a",
            font=dict(color="#8b949e"),
            margin=dict(l=10, r=10, t=10, b=10),
            showlegend=True,
            legend=dict(font=dict(color="#8b949e")),
            height=280,
        )
        status_pie = dcc.Graph(figure=pie_fig, config={"displayModeBar": False})
    else:
        status_pie = html.Div("No runs yet.", style={"color": "#8b949e", "textAlign": "center", "padding": "60px"})

    # ── Intern feed ───────────────────────────────────────────────────────────
    INTERN_COLORS = {
        "prompt_engineer": "#58a6ff",
        "code_reviewer": "#3fb950",
        "insights": "#d2a8ff",
        "methodology_analyst": "#ffa657",
        "deep_research": "#f78166",
    }
    if intern_logs:
        rows = []
        for entry in intern_logs[:20]:
            elapsed = entry.get("elapsed_s", "")
            elapsed_str = f"{elapsed:.1f}s" if isinstance(elapsed, (int, float)) else str(elapsed)
            intern_name = entry.get("intern", "unknown")
            color = INTERN_COLORS.get(intern_name, "#8b949e")
            ts = entry.get("timestamp", "")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ts = dt.strftime("%H:%M:%S")
                except Exception:
                    ts = ts[:19]

            snippet = entry.get("snippet", "") or entry.get("activity", "")
            snippet = snippet[:120].replace("\n", " ")

            rows.append(
                html.Div(
                    [
                        html.Div(
                            [
                                html.Span(intern_name, style={"color": color, "fontWeight": "600", "fontSize": "0.8rem"}),
                                html.Span(f"  {elapsed_str}", style={"color": "#8b949e", "fontSize": "0.75rem", "marginLeft": "8px"}),
                                html.Span(ts, style={"color": "#484f58", "fontSize": "0.72rem", "float": "right"}),
                            ],
                            style={"marginBottom": "3px"},
                        ),
                        html.Div(snippet, style={"color": "#c9d1d9", "fontSize": "0.78rem", "lineHeight": "1.4"}),
                    ],
                    style={
                        "padding": "8px 10px",
                        "borderBottom": "1px solid #21262d",
                        "fontFamily": "monospace",
                    },
                )
            )
        intern_feed = html.Div(rows, style={"overflowY": "auto", "maxHeight": "320px"})
    else:
        intern_feed = html.Div("No intern activity yet.", style={"color": "#8b949e", "textAlign": "center", "padding": "60px"})

    # ── Git log ───────────────────────────────────────────────────────────────
    git_text = get_git_diff()
    git_log = html.Pre(
        git_text,
        style={
            "color": "#c9d1d9",
            "background": "#0d1b2a",
            "padding": "12px",
            "borderRadius": "4px",
            "fontSize": "0.75rem",
            "lineHeight": "1.6",
            "overflowY": "auto",
            "maxHeight": "320px",
            "margin": 0,
            "whiteSpace": "pre-wrap",
            "fontFamily": "monospace",
        },
    )

    # ── Results table ─────────────────────────────────────────────────────────
    if results:
        table_data = []
        for r in reversed(results):
            table_data.append({
                "#": r["id"],
                "Commit": r["commit"],
                "Metric": f"{r['metric']:.4f}" if r["metric"] is not None else "—",
                "Status": r["status"],
                "Description": r["description"],
                "Time": r["timestamp"][:19] if r["timestamp"] else "",
            })

        results_table = dash_table.DataTable(
            data=table_data,
            columns=[{"name": c, "id": c} for c in ["#", "Commit", "Metric", "Status", "Description", "Time"]],
            sort_action="native",
            filter_action="native",
            page_size=20,
            style_table={"overflowX": "auto"},
            style_cell={
                "background": "#0d1b2a",
                "color": "#c9d1d9",
                "border": "1px solid #21262d",
                "fontSize": "0.8rem",
                "fontFamily": "monospace",
                "padding": "6px 10px",
                "textAlign": "left",
                "whiteSpace": "normal",
                "maxWidth": "300px",
                "overflow": "hidden",
                "textOverflow": "ellipsis",
            },
            style_header={
                "background": "#161b22",
                "color": "#58a6ff",
                "fontWeight": "600",
                "border": "1px solid #21262d",
                "textTransform": "uppercase",
                "fontSize": "0.72rem",
                "letterSpacing": "0.05em",
            },
            style_data_conditional=[
                {"if": {"filter_query": '{Status} = "keep"'}, "color": "#28a745"},
                {"if": {"filter_query": '{Status} = "review"'}, "color": "#58a6ff"},
                {"if": {"filter_query": '{Status} = "discard"'}, "color": "#fd7e14"},
                {"if": {"filter_query": '{Status} = "crash"'}, "color": "#dc3545"},
                {"if": {"row_index": "odd"}, "background": "#0b1622"},
            ],
        )
    else:
        results_table = html.Div("No experiment results yet.", style={"color": "#8b949e", "textAlign": "center", "padding": "40px"})

    # ── Ideas panel ───────────────────────────────────────────────────────────
    if governance:
        governance_table = dash_table.DataTable(
            data=[
                {
                    "Run": row["run_id"],
                    "Decision": row["decision"],
                    "Approval": row["approval_state"],
                    "Promote": "yes" if row["promotion_allowed"] else "no",
                    "Failed Gates": row["failed_gates"],
                    "Rationale": row["rationale"],
                    "Time": row["timestamp"][:19],
                }
                for row in governance
            ],
            columns=[
                {"name": c, "id": c}
                for c in ["Run", "Decision", "Approval", "Promote", "Failed Gates", "Rationale", "Time"]
            ],
            page_size=10,
            style_table={"overflowX": "auto"},
            style_cell={
                "background": "#0d1b2a",
                "color": "#c9d1d9",
                "border": "1px solid #21262d",
                "fontSize": "0.76rem",
                "fontFamily": "monospace",
                "padding": "6px 8px",
                "textAlign": "left",
                "whiteSpace": "normal",
            },
            style_header={
                "background": "#161b22",
                "color": "#58a6ff",
                "fontWeight": "600",
                "border": "1px solid #21262d",
                "fontSize": "0.72rem",
            },
            style_data_conditional=[
                {"if": {"filter_query": '{Decision} = "approved"'}, "color": "#28a745"},
                {"if": {"filter_query": '{Decision} = "needs_review"'}, "color": "#58a6ff"},
                {"if": {"filter_query": '{Decision} = "rejected"'}, "color": "#dc3545"},
            ],
        )
    else:
        governance_table = html.Div("No governance decisions yet.", style={"color": "#8b949e", "textAlign": "center", "padding": "40px"})

    if alerts:
        alert_rows = []
        for alert in alerts[:12]:
            color = "#dc3545" if alert["severity"] == "critical" else "#58a6ff"
            alert_rows.append(
                html.Div(
                    [
                        html.Div(
                            [
                                html.Span(alert["severity"], style={"color": color, "fontWeight": "700"}),
                                html.Span(f"  {alert['run_id']}", style={"color": "#8b949e", "marginLeft": "8px"}),
                                html.Span(alert["timestamp"][:19], style={"color": "#484f58", "float": "right"}),
                            ],
                            style={"fontSize": "0.75rem", "marginBottom": "4px"},
                        ),
                        html.Div(alert["title"], style={"color": "#f0f6fc", "fontWeight": "600", "fontSize": "0.8rem"}),
                        html.Div(alert["message"], style={"color": "#c9d1d9", "fontSize": "0.76rem", "lineHeight": "1.4"}),
                    ],
                    style={"padding": "9px 10px", "borderBottom": "1px solid #21262d"},
                )
            )
        alerts_panel = html.Div(alert_rows, style={"overflowY": "auto", "maxHeight": "320px"})
    else:
        alerts_panel = html.Div("No human alerts yet.", style={"color": "#8b949e", "textAlign": "center", "padding": "40px"})

    if ideas:
        ideas_panel = html.Pre(
            ideas,
            style={
                "color": "#c9d1d9",
                "fontSize": "0.8rem",
                "lineHeight": "1.7",
                "margin": 0,
                "whiteSpace": "pre-wrap",
                "fontFamily": "monospace",
                "maxHeight": "260px",
                "overflowY": "auto",
            },
        )
    else:
        ideas_panel = html.Div("No ideas generated yet.", style={"color": "#8b949e", "textAlign": "center", "padding": "40px"})

    return (
        task_label,
        status_bar,
        foundation_panel,
        blocker_panel,
        tool_panel,
        question_panel_view,
        kpi_generation_panel,
        evidence_panel,
        git_hygiene_panel,
        metric_chart,
        status_pie,
        intern_feed,
        git_log,
        results_table,
        governance_table,
        alerts_panel,
        ideas_panel,
    )


if __name__ == "__main__":
    port = int(os.environ.get("DASH_PORT", 8050))
    print(f"\n  Autoresearch Dashboard -> http://localhost:{port}\n")
    app.run(debug=False, port=port)
