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
from datetime import datetime, timezone
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

STATUS_COLORS = {"keep": "#28a745", "discard": "#fd7e14", "crash": "#dc3545"}
REFRESH_MS = 5_000

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


def load_data():
    """Return (results, intern_logs, loop_status, ideas_text)."""
    results, intern_logs, loop_status, ideas = [], [], {}, ""

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

    return results, intern_logs, loop_status, ideas


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

        # ── Ideas ─────────────────────────────────────────────────────────────
        _card("Ideas & Hypotheses", "ideas-panel"),
    ],
)

# ─── Callbacks ────────────────────────────────────────────────────────────────

@app.callback(
    [
        Output("header-task-label", "children"),
        Output("status-bar", "children"),
        Output("metric-chart", "children"),
        Output("status-pie", "children"),
        Output("intern-feed", "children"),
        Output("git-log", "children"),
        Output("results-table", "children"),
        Output("ideas-panel", "children"),
    ],
    Input("tick", "n_intervals"),
)
def refresh(_n):
    results, intern_logs, loop_status, ideas = load_data()
    task = _active_task()

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
        pie_colors = [STATUS_COLORS.get(l, "#8b949e") for l in labels]
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
                {"if": {"filter_query": '{Status} = "discard"'}, "color": "#fd7e14"},
                {"if": {"filter_query": '{Status} = "crash"'}, "color": "#dc3545"},
                {"if": {"row_index": "odd"}, "background": "#0b1622"},
            ],
        )
    else:
        results_table = html.Div("No experiment results yet.", style={"color": "#8b949e", "textAlign": "center", "padding": "40px"})

    # ── Ideas panel ───────────────────────────────────────────────────────────
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
        metric_chart,
        status_pie,
        intern_feed,
        git_log,
        results_table,
        ideas_panel,
    )


if __name__ == "__main__":
    port = int(os.environ.get("DASH_PORT", 8050))
    print(f"\n  Autoresearch Dashboard -> http://localhost:{port}\n")
    app.run(debug=False, port=port)
