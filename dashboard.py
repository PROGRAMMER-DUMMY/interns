"""
Autoresearch Control Plane  —  dashboard.py v3
Run: uv run python dashboard.py [--port 8050]
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback_context, dash_table, dcc, html, no_update
import plotly.graph_objs as go

from core.dashboard_services import (
    BuildControlService,
    DashboardPaths,
    GitHistoryService,
    WorkspaceCommandService,
    read_json,
    tail_file,
)

# ── Root ──────────────────────────────────────────────────────────────────────
ROOT         = Path(__file__).parent
WORKSPACES   = ROOT / "workspaces"
GLOBAL_STATE = ROOT / "state"
AGENTS_STATE = ROOT / "core" / "agents" / "state"
CONFIG_TASKS = ROOT / "config" / "tasks.json"
PATHS = DashboardPaths(ROOT)
GIT_HISTORY = GitHistoryService(ROOT)
BUILD_CONTROL = BuildControlService(PATHS)
WORKSPACE_COMMANDS = WorkspaceCommandService(ROOT)

# ── Colour system ─────────────────────────────────────────────────────────────
C = dict(
    bg       = "#07111c",   # page background
    card     = "#0c1a27",   # card body
    card2    = "#0f2030",   # table row alt
    sidebar  = "#050e18",   # sidebar
    border   = "#1a2d3f",   # borders
    dim      = "#1e3045",   # thin dividers
    text     = "#cdd9e5",   # primary text
    muted    = "#768a9a",   # secondary text
    faint    = "#3a5168",   # very muted
    blue     = "#4f9cf9",   # accent
    green    = "#3dcb6a",   # success
    orange   = "#f5983a",   # warning
    red      = "#f25454",   # error
    purple   = "#b07ef5",   # purple
    yellow   = "#f5c430",   # gold
    teal     = "#35c8b4",   # teal
    hdr      = "#060f1a",   # card header bg
)

# Code type colour map
LANG_C = dict(SQL=C["blue"], Polars=C["purple"], PySpark=C["orange"],
              Python=C["teal"], Combined=C["yellow"])

FAST_MS = 2_000
SLOW_MS = 15_000
PAGE_IDS = ["workspace","build","medallion","lineage","blockers","diffs","budget","govern"]
NAV_ITEMS = [
    ("workspace", "Workspace"),
    ("build",     "Build"),
    ("medallion", "Medallion"),
    ("lineage",   "Lineage"),
    ("blockers",  "KPI Blockers"),
    ("diffs",     "Code Diffs"),
    ("budget",    "Budget & Tiers"),
    ("govern",    "Govern"),
]

# ── Path helpers ──────────────────────────────────────────────────────────────

def _ws(n: str) -> Path:            return WORKSPACES / n
def _med_state(n: str) -> Path:     return _ws(n) / "interns" / "state" / "medallion"
def _runs_dir(n: str) -> Path:      return _med_state(n) / "runs"
def _med_gen(n: str) -> Path:       return _ws(n) / "interns" / "generated" / "medallion"
def _reports(n: str) -> Path:       return _ws(n) / "interns" / "reports"
def _lock_path(n: str) -> Path:     return _med_state(n) / ".lock"
def _live_log(n: str) -> Path:      return _med_state(n) / "build_live.log"
def _pid_file(n: str) -> Path:      return _med_state(n) / "build.pid"

# ── Data helpers ──────────────────────────────────────────────────────────────

def _jread(p: Path, default=None) -> Any:
    return read_json(p, default)

def _workspaces() -> list[str]:
    if not WORKSPACES.exists():
        return []
    return sorted(p.name for p in WORKSPACES.iterdir() if p.is_dir() and not p.name.startswith("."))

def _runs(ws: str) -> list[dict]:
    rd = _runs_dir(ws)
    if not rd.exists():
        return []
    out = []
    for d in sorted(rd.iterdir(), reverse=True):
        rj = d / "run.json"
        if rj.exists():
            data = _jread(rj, {})
            data["_dir"] = str(d)
            out.append(data)
    return out[:60]

def _lineage(ws: str) -> dict:
    return _jread(_med_gen(ws) / "lineage.json", {"nodes": [], "edges": []})

def _blocker_panel(ws: str) -> dict:
    return _jread(_reports(ws) / "blocker_question_panel" / "current.json")

def _budget_state() -> dict:
    return _jread(AGENTS_STATE / "budget_state.json", {"spent": 0.0, "max_usd": 0.0, "history": []})

def _model_cache() -> dict:
    return _jread(AGENTS_STATE / "model_classification_cache.json", {})

def _active_task() -> dict:
    try:
        data = json.loads(CONFIG_TASKS.read_text())
        aid  = data.get("active_task", "")
        return next((t for t in data.get("tasks", []) if t["id"] == aid), {})
    except Exception:
        return {}

def _all_tasks() -> list[dict]:
    try:
        return json.loads(CONFIG_TASKS.read_text()).get("tasks", [])
    except Exception:
        return []

def _ws_artifacts(ws: str) -> dict:
    base = _ws(ws) / "interns"
    return {
        "mapping":  _jread(base / "generated" / "contracts" / "kpi_feature_mapping.json"),
        "profiles": _jread(base / "generated" / "profiles" / "profile_index.json", {"profiles": []}),
        "panel":    _jread(base / "reports" / "blocker_question_panel" / "current.json"),
    }

def _tail(p: Path, lines: int = 300) -> str:
    return tail_file(p, lines)
    if not p or not p.exists():
        return "(no log yet — trigger a build to see output)"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        ll   = text.splitlines()
        return "\n".join(ll[-lines:])
    except Exception as e:
        return f"(error reading log: {e})"

def _safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _ts_fmt(ts: str) -> str:
    if not ts:
        return ""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return ts[:16]

# ── Experiment / code helpers ─────────────────────────────────────────────────

def _detect_lang(path: Path, content: str) -> str:
    """Classify experiment code: SQL | Polars | PySpark | Python | Combined."""
    if path.suffix == ".sql":
        return "SQL"
    has_sql     = bool(re.search(r"\b(SELECT|INSERT|UPDATE|CREATE)\b", content, re.I))
    has_polars  = "import polars" in content or " pl." in content or "pl.read" in content
    has_pyspark = ("from pyspark" in content or "SparkSession" in content
                   or "spark.read" in content or ".toPandas()" in content)
    has_duckdb  = "import duckdb" in content or "duckdb.connect" in content
    kinds = []
    if has_sql or has_duckdb:
        kinds.append("SQL")
    if has_polars:
        kinds.append("Polars")
    if has_pyspark:
        kinds.append("PySpark")
    if not kinds:
        return "Python"
    return "+".join(kinds) if len(kinds) > 1 else kinds[0]

def _git_log_file(filepath: str, n: int = 20) -> list[dict]:
    """Return git log entries for a specific file."""
    return GIT_HISTORY.log_file(filepath, n)
    if not filepath:
        return []
    try:
        r = subprocess.run(
            ["git", "log", "--oneline", "--follow", f"-{n}", "--", filepath],
            capture_output=True, text=True, cwd=str(ROOT), timeout=5
        )
        entries = []
        for line in r.stdout.splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2:
                entries.append({"hash": parts[0], "message": parts[1]})
        return entries
    except Exception:
        return []

def _git_show_file(hash_: str, filepath: str) -> str:
    """Return file content at a given git commit."""
    return GIT_HISTORY.show_file(hash_, filepath)
    if not hash_ or not filepath:
        return ""
    try:
        r = subprocess.run(
            ["git", "show", f"{hash_}:{filepath}"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=5
        )
        return r.stdout
    except Exception:
        return ""

def _git_diff_file(hash_a: str, hash_b: str, filepath: str) -> str:
    """git diff of a specific file between two commits."""
    return GIT_HISTORY.diff_file(hash_a, hash_b, filepath)
    if not hash_a or not hash_b or not filepath:
        return "Select two revisions to compare."
    if hash_a == hash_b:
        return "Same commit — no diff."
    try:
        r = subprocess.run(
            ["git", "diff", hash_a, hash_b, "--", filepath],
            capture_output=True, text=True, cwd=str(ROOT), timeout=10
        )
        return r.stdout or f"No changes to {filepath} between {hash_a[:8]} and {hash_b[:8]}."
    except Exception as e:
        return f"git diff failed: {e}"

def _git_log_text(n: int = 20) -> str:
    return GIT_HISTORY.log_text(n)
    try:
        r = subprocess.run(["git", "log", "--oneline", f"-{n}"],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=5)
        return r.stdout or "No commits."
    except Exception as e:
        return f"git unavailable: {e}"

def _git_commit_at(ts: str) -> str:
    return GIT_HISTORY.commit_at(ts)
    if not ts:
        return ""
    try:
        r = subprocess.run(
            ["git", "log", f"--before={ts}", "-1", "--format=%H"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=5
        )
        return r.stdout.strip()
    except Exception:
        return ""

def _load_global_db() -> dict:
    db  = GLOBAL_STATE / "workspace.db"
    out: dict = {"results": [], "intern_logs": [], "governance": [], "alerts": [], "ideas": ""}
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute("SELECT * FROM experiments ORDER BY id ASC")
            for i, row in enumerate(cur.fetchall(), 1):
                out["results"].append({
                    "id": i, "commit": (row["run_id"] or "")[:8],
                    "metric": _safe_float(row["metrics"]),
                    "status": row["status"] or "unknown",
                    "description": (row["results"] or "")[:120],
                    "timestamp": row["timestamp"] or "",
                })
        except Exception:
            pass
        try:
            cur = con.execute("SELECT * FROM intern_activity ORDER BY id DESC LIMIT 50")
            for row in cur.fetchall():
                try:
                    det = json.loads(row["details"] or "{}")
                except Exception:
                    det = {}
                out["intern_logs"].append({
                    "intern": row["intern_name"] or "",
                    "activity": row["activity"] or "",
                    "elapsed_s": _safe_float(det.get("elapsed_s")),
                    "timestamp": row["timestamp"] or "",
                    "snippet": str(det.get("response", ""))[:200],
                })
        except Exception:
            pass
        for q, key, mapper in [
            ("SELECT * FROM governance_decisions ORDER BY id DESC LIMIT 20", "governance",
             lambda r: {"run_id": r["run_id"] or "", "decision": r["decision"] or "",
                        "approval": r["approval_state"] or "", "rationale": r["rationale"] or "",
                        "timestamp": r["timestamp"] or ""}),
            ("SELECT * FROM alerts ORDER BY id DESC LIMIT 30", "alerts",
             lambda r: {"severity": r["severity"] or "", "title": r["title"] or "",
                        "message": r["message"] or "", "run_id": r["run_id"] or "",
                        "timestamp": r["timestamp"] or ""}),
        ]:
            try:
                cur = con.execute(q)
                for row in cur.fetchall():
                    out[key].append(mapper(row))
            except Exception:
                pass
        try:
            cur = con.execute(
                "SELECT content FROM logs WHERE log_type='ideas' ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                out["ideas"] = row["content"] or ""
        except Exception:
            pass
        con.close()
    except Exception:
        pass
    return out

# ── Build control ─────────────────────────────────────────────────────────────

def _lock_state(ws: str) -> dict:
    return BUILD_CONTROL.lock_state(ws)
    lp = _lock_path(ws)
    if not lp.exists():
        return {"locked": False, "pid": 0, "age_s": 0}
    try:
        age  = time.time() - lp.stat().st_mtime
        data = _jread(lp, {})
        return {"locked": True, "pid": data.get("pid", 0), "age_s": int(age)}
    except Exception:
        return {"locked": True, "pid": 0, "age_s": 0}

def _pid_alive(pid: int) -> bool:
    return BUILD_CONTROL.pid_alive(pid)
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False

def _trigger_build(ws: str, target: str = "auto", layer: str = "", table: str = "") -> dict:
    return BUILD_CONTROL.trigger_build(ws, target, layer, table)
    sd = _med_state(ws)
    sd.mkdir(parents=True, exist_ok=True)
    cmd = ["uv", "run", "build-medallion", "--workspace", f"workspaces/{ws}"]
    if target and target != "auto":
        cmd += ["--target", target]
    if layer:
        cmd += ["--only-layer", layer]
    if table:
        cmd += ["--only-table", table]
    try:
        fh = open(_live_log(ws), "w", encoding="utf-8")
        kw: dict[str, Any] = {"stdout": fh, "stderr": subprocess.STDOUT, "cwd": str(ROOT)}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(cmd, **kw)
        _pid_file(ws).write_text(str(proc.pid), encoding="utf-8")
        return {"ok": True, "pid": proc.pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _kill_build(ws: str) -> dict:
    return BUILD_CONTROL.kill_build(ws)
    pp = _pid_file(ws)
    if not pp.exists():
        return {"ok": False, "error": "No PID file."}
    try:
        pid = int(pp.read_text().strip())
        sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM
        os.kill(pid, sig)
        pp.unlink(missing_ok=True)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def _run_cmd(ws: str, cmd_name: str, extra: list[str] | None = None) -> dict:
    return WORKSPACE_COMMANDS.run(ws, cmd_name, extra)
    cmd = ["uv", "run", cmd_name, "--workspace", f"workspaces/{ws}"] + (extra or [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=300)
        return {"ok": r.returncode == 0,
                "stdout": r.stdout[-3000:], "stderr": r.stderr[-500:]}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ── Design tokens ─────────────────────────────────────────────────────────────

CARD_S = {
    "background": C["card"],
    "border": f"1px solid {C['border']}",
    "borderRadius": "10px",
    "marginBottom": "16px",
    "boxShadow": "0 2px 12px rgba(0,0,0,.35)",
}
HDR_S = {
    "background": C["hdr"],
    "borderBottom": f"1px solid {C['border']}",
    "borderTop": f"3px solid {C['blue']}",
    "borderRadius": "10px 10px 0 0",
    "padding": "10px 16px",
    "display": "flex", "alignItems": "center", "gap": "8px",
}
HDR_TITLE = {
    "color": C["text"], "fontWeight": "600", "fontSize": "0.82rem",
    "letterSpacing": "0.04em", "textTransform": "uppercase", "margin": 0,
}
CELL = {
    "background": C["card"], "color": C["text"],
    "border": f"1px solid {C['dim']}", "fontSize": "0.76rem",
    "fontFamily": "'JetBrains Mono', 'Fira Code', monospace",
    "padding": "7px 10px", "textAlign": "left", "whiteSpace": "normal",
}
HCELL = {
    "background": C["hdr"], "color": C["muted"], "fontWeight": "600",
    "border": f"1px solid {C['dim']}", "fontSize": "0.7rem", "letterSpacing": "0.06em",
    "textTransform": "uppercase",
}
PRE_S = {
    "color": C["text"], "background": C["sidebar"],
    "padding": "14px", "borderRadius": "6px", "fontSize": "0.74rem",
    "lineHeight": "1.65", "overflowY": "auto", "maxHeight": "400px",
    "margin": 0, "whiteSpace": "pre-wrap",
    "fontFamily": "'JetBrains Mono', 'Fira Code', monospace",
    "border": f"1px solid {C['dim']}",
}
INPUT_S = {
    "background": C["sidebar"], "color": C["text"],
    "border": f"1px solid {C['border']}", "borderRadius": "6px",
    "padding": "7px 12px", "fontSize": "0.8rem", "width": "100%",
    "outline": "none",
}
DDL_S = {"background": C["card"], "color": "#000"}
PLOT_L = dict(
    paper_bgcolor=C["card"], plot_bgcolor=C["card"],
    font=dict(color=C["muted"], size=11),
    margin=dict(l=8, r=8, t=28, b=8),
    height=260,
    xaxis=dict(gridcolor=C["dim"], showline=False, zeroline=False),
    yaxis=dict(gridcolor=C["dim"], showline=False, zeroline=False),
    legend=dict(font=dict(color=C["muted"], size=10), bgcolor="rgba(0,0,0,0)"),
)

# ── UI component helpers ──────────────────────────────────────────────────────

def _card(title: str, accent: str = C["blue"]) -> dict:
    """Return card + header styles with given accent."""
    hdr = {**HDR_S, "borderTop": f"3px solid {accent}"}
    return {"card": CARD_S, "hdr": hdr}

def _mk_card(title: str, *children, accent: str = C["blue"], body_style: dict | None = None) -> dbc.Card:
    return dbc.Card([
        dbc.CardHeader(
            html.Span(title, style=HDR_TITLE),
            style={**HDR_S, "borderTop": f"3px solid {accent}"},
        ),
        dbc.CardBody(list(children),
                     style={"padding": "14px 16px", **(body_style or {})}),
    ], style=CARD_S)

def _kv(label: str, value: Any, color: str = "") -> html.Div:
    return html.Div([
        html.Span(label, style={"color": C["muted"], "fontSize": "0.74rem"}),
        html.Span(str(value if value is not None else "—"),
                  style={"color": color or C["text"], "float": "right",
                         "fontWeight": "600", "fontSize": "0.78rem"}),
    ], style={"padding": "7px 0", "borderBottom": f"1px solid {C['dim']}"})

def _badge(text: str, color: str) -> html.Span:
    return html.Span(text, style={
        "background": color + "18", "color": color,
        "border": f"1px solid {color}40",
        "borderRadius": "20px", "padding": "2px 10px",
        "fontSize": "0.7rem", "fontWeight": "700",
        "letterSpacing": "0.03em",
    })

def _lang_badge(lang: str) -> html.Span:
    color = LANG_C.get(lang.split("+")[0], C["teal"])
    return _badge(lang, color)

def _status_badge(status: str) -> html.Span:
    color = {"ok": C["green"], "failed": C["red"], "pending": C["faint"],
             "running": C["blue"], "degraded": C["orange"]}.get(status, C["muted"])
    return _badge(status, color)

def _sh(label: str, color: str = C["blue"]) -> html.Div:
    return html.Div(label, style={
        "color": color, "fontSize": "0.69rem", "fontWeight": "700",
        "textTransform": "uppercase", "letterSpacing": "0.07em",
        "marginBottom": "8px", "marginTop": "6px",
    })

def _btn(label: str, bid: str, color: str = "primary", outline: bool = False, **kw) -> dbc.Button:
    return dbc.Button(label, id=bid, color=color, size="sm", outline=outline,
                      className="me-1 mb-1", **kw)

def _dt(data: list[dict], cols: list[str], page: int = 10) -> dash_table.DataTable:
    return dash_table.DataTable(
        data=data, columns=[{"name": c, "id": c} for c in cols],
        page_size=page, sort_action="native",
        style_table={"overflowX": "auto"},
        style_cell=CELL, style_header=HCELL,
        style_data_conditional=[
            {"if": {"row_index": "odd"},
             "background": C["card2"]},
        ],
    )

def _empty_chart(msg: str) -> dcc.Graph:
    fig = go.Figure()
    fig.add_annotation(text=msg, x=0.5, y=0.5, xref="paper", yref="paper",
                       showarrow=False, font=dict(color=C["muted"], size=12))
    fig.update_layout(**PLOT_L)
    return dcc.Graph(figure=fig, config={"displayModeBar": False})

# ── Syntax highlighting ───────────────────────────────────────────────────────

SQL_KW = {
    "select","from","where","join","left","right","inner","outer","on","group","by",
    "order","having","with","as","union","all","distinct","limit","offset","case",
    "when","then","else","end","count","sum","avg","max","min","coalesce","cast",
    "filter","partition","over","row_number","rank","dense_rank","create","table",
    "view","insert","into","update","delete","drop","alter","if","not","in","is",
    "null","exists","and","or","between","like","ilike","true","false",
}

def _colorize(code: str, lang: str) -> list:
    """Return list of spans with line-level syntax colouring."""
    spans = []
    for line in (code or "").splitlines():
        stripped = line.strip()
        # Comment lines
        if stripped.startswith("--") or stripped.startswith("#"):
            color = C["faint"]
        # String literals (basic heuristic — line is mostly a string)
        elif stripped.startswith(("'", '"', '"""', "'''")):
            color = C["green"]
        # SQL keyword at start of (stripped) line
        elif lang in ("SQL", "Python") and stripped.split(" ")[0].lower().rstrip("(") in SQL_KW:
            color = C["blue"]
        # Polars method chains
        elif lang in ("Polars", "Combined") and re.match(r"\s*(pl\.|\.filter|\.select|\.with_columns|\.group_by)", line):
            color = C["purple"]
        # PySpark
        elif lang in ("PySpark", "Combined") and re.match(r"\s*(spark\.|df\.|\.show|\.toPandas)", line):
            color = C["orange"]
        # Python def/class/import
        elif re.match(r"\s*(def |class |import |from |@)", line):
            color = C["teal"]
        else:
            color = C["text"]
        spans.append(html.Span(line + "\n", style={"color": color}))
    return spans

def _colorize_diff(diff_text: str) -> list:
    spans = []
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            color, weight = C["blue"], "600"
        elif line.startswith("+"):
            color, weight = C["green"], "400"
        elif line.startswith("-"):
            color, weight = C["red"], "400"
        elif line.startswith("@@"):
            color, weight = C["purple"], "600"
        elif line.startswith(("diff ", "index ", "new file", "deleted file")):
            color, weight = C["yellow"], "600"
        else:
            color, weight = C["text"], "400"
        spans.append(html.Span(line + "\n", style={"color": color, "fontWeight": weight}))
    return spans

# ── Sidebar ───────────────────────────────────────────────────────────────────

def _sidebar() -> html.Div:
    nav_links = []
    for pid, label in NAV_ITEMS:
        nav_links.append(
            dcc.Link(label, href=f"/{pid}", id=f"nav-{pid}", style={
                "display": "block", "padding": "9px 20px 9px 18px",
                "color": C["muted"], "textDecoration": "none", "fontSize": "0.83rem",
                "borderLeft": "3px solid transparent",
                "borderRadius": "0 4px 4px 0",
                "transition": "color 0.15s",
            })
        )
    return html.Div([
        html.Div([
            html.Div("Autoresearch", style={
                "color": C["blue"], "fontWeight": "700", "fontSize": "1.05rem",
                "letterSpacing": "-0.02em",
            }),
            html.Div("Control Plane", style={
                "color": C["faint"], "fontSize": "0.69rem", "marginTop": "1px",
            }),
        ], style={"padding": "20px 20px 16px"}),
        html.Hr(style={"borderColor": C["dim"], "margin": "0"}),
        html.Div(nav_links, style={"padding": "8px 0"}),
        html.Hr(style={"borderColor": C["dim"], "margin": "0"}),
        html.Div(id="sidebar-ws", style={
            "padding": "10px 20px", "color": C["faint"], "fontSize": "0.72rem",
            "fontFamily": "monospace",
        }),
    ], style={
        "width": "195px", "minWidth": "195px", "background": C["sidebar"],
        "height": "100vh", "position": "fixed", "top": 0, "left": 0,
        "overflowY": "auto", "borderRight": f"1px solid {C['border']}",
        "zIndex": 1000,
    })

# ── Page skeletons ────────────────────────────────────────────────────────────

def _pg_workspace() -> html.Div:
    return html.Div([
        dbc.Row([
            dbc.Col(_mk_card("Workspace",
                dcc.Dropdown(id="ws-dropdown", placeholder="Select workspace…", style=DDL_S),
                html.Div(id="ws-status-body", style={"marginTop": "10px"}),
                accent=C["blue"]), md=5),
            dbc.Col(_mk_card("KPI & Blocker Panel",
                html.Div(id="ws-kpi-body"),
                accent=C["purple"]), md=4),
            dbc.Col(_mk_card("Actions",
                _btn("Onboard Workspace", "btn-onboard"),
                _btn("Validate Artifacts", "btn-validate"),
                _btn("Generate KPI SQL", "btn-gen-kpi-sql"),
                html.Hr(style={"borderColor": C["dim"], "margin": "8px 0"}),
                html.Div(id="ws-action-out", style={
                    **PRE_S, "maxHeight": "180px", "fontSize": "0.72rem", "marginTop": "0",
                }),
                accent=C["teal"]), md=3),
        ], className="g-3"),
        dbc.Row([
            dbc.Col(_mk_card("Dataset Profiles", html.Div(id="ws-profiles-body")), md=12),
        ], className="g-3"),
    ])

def _pg_build() -> html.Div:
    return html.Div([
        html.Div(id="build-lock-banner"),
        dbc.Row([
            dbc.Col(_mk_card("Trigger Build",
                dbc.Row([
                    dbc.Col(dcc.Dropdown(id="build-target",
                        options=[{"label": t, "value": t} for t in ["auto","duckdb","databricks"]],
                        value="auto", clearable=False, style=DDL_S), md=3),
                    dbc.Col(dcc.Input(id="build-only-layer",
                        placeholder="--only-layer (bronze|silver|gold)",
                        style=INPUT_S), md=4),
                    dbc.Col(dcc.Input(id="build-only-table",
                        placeholder="--only-table name",
                        style=INPUT_S), md=4),
                    dbc.Col(html.Div([
                        _btn("Run Build", "btn-run-build", color="success"),
                        _btn("Kill", "btn-kill-build", color="danger", outline=True),
                        _btn("Design", "btn-design", color="secondary", outline=True),
                    ]), md=1),
                ], className="g-2", align="end"),
            ), md=12),
        ], className="g-3"),
        dbc.Row([
            dbc.Col(_mk_card("Live Output", html.Pre(id="build-log", style=PRE_S),
                             accent=C["green"]), md=12),
        ], className="g-3"),
        dbc.Row([
            dbc.Col(_mk_card("Run History", html.Div(id="build-run-history")), md=12),
        ], className="g-3"),
    ])

def _pg_medallion() -> html.Div:
    return html.Div([
        dbc.Row([
            dbc.Col(_mk_card("Latest Run", html.Div(id="med-latest-run")), md=4),
            dbc.Col(_mk_card("Table Status", html.Div(id="med-table-status")), md=8),
        ], className="g-3"),
        dbc.Row([
            dbc.Col(_mk_card("Row Count Trends",   html.Div(id="med-row-chart"),  accent=C["blue"]),   md=6),
            dbc.Col(_mk_card("Build Duration",       html.Div(id="med-dur-chart"),  accent=C["orange"]), md=6),
        ], className="g-3"),
        dbc.Row([
            dbc.Col(_mk_card("KPI Metric Trends",    html.Div(id="med-kpi-chart"),    accent=C["purple"]), md=6),
            dbc.Col(_mk_card("Assertion Heatmap",    html.Div(id="med-assert-chart"), accent=C["red"]),    md=6),
        ], className="g-3"),
    ])

def _pg_lineage() -> html.Div:
    return html.Div([
        dbc.Row([
            dbc.Col(_mk_card("Table Lineage — Sankey", html.Div(id="lin-sankey"),
                             accent=C["teal"]), md=12),
        ], className="g-3"),
        dbc.Row([
            dbc.Col(_mk_card("Column Drill-Down",
                dcc.Dropdown(id="lin-table-select", placeholder="Select a table…",
                             style={**DDL_S, "marginBottom": "10px"}),
                html.Div(id="lin-column-body"),
            ), md=12),
        ], className="g-3"),
    ])

def _pg_blockers() -> html.Div:
    return html.Div([
        dbc.Row([
            dbc.Col(_mk_card("Panel Actions",
                _btn("Refresh Panel", "btn-blocker-refresh"),
                _btn("Validate", "btn-blocker-validate"),
                html.Div(id="blocker-action-out", style={
                    **PRE_S, "maxHeight": "140px", "marginTop": "8px",
                }),
                accent=C["orange"]), md=3),
            dbc.Col(_mk_card("Current Blocker Panel", html.Div(id="blocker-panel-body")), md=9),
        ], className="g-3"),
        dbc.Row([
            dbc.Col(_mk_card("Apply Answer",
                dbc.Row([
                    dbc.Col(dcc.Dropdown(id="blocker-option-select",
                        placeholder="Select option ID…", style=DDL_S), md=5),
                    dbc.Col(dcc.Input(id="blocker-domain-input",
                        placeholder="Domain (e.g. claims)", style=INPUT_S), md=4),
                    dbc.Col(_btn("Apply", "btn-blocker-apply", color="success"), md=3),
                ], className="g-2", align="end"),
                html.Div(id="blocker-apply-out", style={
                    **PRE_S, "maxHeight": "140px", "marginTop": "10px",
                }),
                accent=C["green"]), md=12),
        ], className="g-3"),
    ])

def _pg_diffs() -> html.Div:
    return html.Div([
        # Task + file context bar
        dbc.Row([
            dbc.Col(_mk_card("Experiment Context",
                dbc.Row([
                    dbc.Col([
                        _sh("Task"),
                        dcc.Dropdown(id="diff-task-select", placeholder="Select task…",
                                     style=DDL_S),
                    ], md=4),
                    dbc.Col(html.Div(id="diff-file-info", style={"paddingTop": "20px"}), md=8),
                ], className="g-3"),
                accent=C["yellow"]), md=12),
        ], className="g-3"),
        # Code viewer + revision history
        dbc.Row([
            dbc.Col(_mk_card("Current Code",
                html.Div(id="diff-lang-badge", style={"marginBottom": "8px"}),
                html.Pre(id="diff-code-view", style={**PRE_S, "maxHeight": "480px"}),
                accent=C["blue"]), md=7),
            dbc.Col(_mk_card("Revision History",
                html.Div(id="diff-rev-history"),
                accent=C["purple"]), md=5),
        ], className="g-3"),
        # Compare two revisions
        dbc.Row([
            dbc.Col(_mk_card("Compare Revisions",
                dbc.Row([
                    dbc.Col([
                        _sh("Revision A (older)"),
                        dcc.Dropdown(id="diff-rev-a", placeholder="Commit A…", style=DDL_S),
                    ], md=5),
                    dbc.Col(html.Div("→", style={
                        "textAlign": "center", "color": C["muted"],
                        "fontSize": "1.5rem", "paddingTop": "22px",
                    }), md=1),
                    dbc.Col([
                        _sh("Revision B (newer)"),
                        dcc.Dropdown(id="diff-rev-b", placeholder="Commit B…", style=DDL_S),
                    ], md=5),
                    dbc.Col(_btn("Diff", "btn-diff-go", color="primary"), md=1),
                ], className="g-2", align="end"),
                html.Pre(id="diff-output", style={**PRE_S, "maxHeight": "520px",
                                                   "marginTop": "12px"}),
                accent=C["red"]), md=12),
        ], className="g-3"),
    ])

def _pg_budget() -> html.Div:
    return html.Div([
        dbc.Row([
            dbc.Col(_mk_card("Budget Tracker",    html.Div(id="budget-tracker-body"), accent=C["orange"]), md=4),
            dbc.Col(_mk_card("Model Tier Cache",  html.Div(id="budget-tiers-body"),   accent=C["purple"]), md=5),
            dbc.Col(_mk_card("Cumulative Cost",   html.Div(id="budget-cost-chart"),   accent=C["yellow"]), md=3),
        ], className="g-3"),
    ])

def _pg_govern() -> html.Div:
    return html.Div([
        dbc.Row([
            dbc.Col(_mk_card("Intern Activity", html.Div(id="gov-intern-feed"),
                             accent=C["blue"]), md=6),
            dbc.Col(_mk_card("Git Log", html.Pre(id="gov-git-log", style=PRE_S),
                             accent=C["faint"]), md=6),
        ], className="g-3"),
        dbc.Row([
            dbc.Col(_mk_card("Governance Decisions", html.Div(id="gov-decisions"),
                             accent=C["purple"]), md=7),
            dbc.Col(_mk_card("Human Alerts", html.Div(id="gov-alerts"),
                             accent=C["red"]), md=5),
        ], className="g-3"),
        dbc.Row([
            dbc.Col(_mk_card("Experiment Results", html.Div(id="gov-results"),
                             accent=C["green"]), md=8),
            dbc.Col(_mk_card("Ideas & Hypotheses",
                html.Pre(id="gov-ideas", style={**PRE_S, "maxHeight": "260px"}),
                accent=C["teal"]), md=4),
        ], className="g-3"),
    ])

# ── App setup ─────────────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    external_stylesheets=[dbc.themes.CYBORG],
    title="Autoresearch",
    update_title=None,
    suppress_callback_exceptions=True,
)

_pages = html.Div([
    html.Div(_pg_workspace(), id="page-workspace", style={"display": "none"}),
    html.Div(_pg_build(),     id="page-build",     style={"display": "none"}),
    html.Div(_pg_medallion(), id="page-medallion", style={"display": "none"}),
    html.Div(_pg_lineage(),   id="page-lineage",   style={"display": "none"}),
    html.Div(_pg_blockers(),  id="page-blockers",  style={"display": "none"}),
    html.Div(_pg_diffs(),     id="page-diffs",     style={"display": "none"}),
    html.Div(_pg_budget(),    id="page-budget",    style={"display": "none"}),
    html.Div(_pg_govern(),    id="page-govern",    style={"display": "none"}),
])

app.layout = html.Div([
    dcc.Location(id="url"),
    dcc.Store(id="ws-store",    data=""),
    dcc.Store(id="task-store",  data=""),
    dcc.Interval(id="fast-tick", interval=FAST_MS, n_intervals=0),
    dcc.Interval(id="slow-tick", interval=SLOW_MS, n_intervals=0),
    _sidebar(),
    html.Div([
        # Top strip
        html.Div([
            html.Span(id="page-title", style={
                "color": C["text"], "fontWeight": "700", "fontSize": "0.95rem",
                "letterSpacing": "-0.01em",
            }),
            html.Span(id="header-context", style={
                "color": C["faint"], "fontSize": "0.78rem", "marginLeft": "12px",
                "fontFamily": "monospace",
            }),
            html.Div(id="header-badges", style={"marginLeft": "auto", "display": "flex", "gap": "6px"}),
        ], style={
            "background": C["hdr"], "borderBottom": f"1px solid {C['border']}",
            "padding": "11px 24px", "display": "flex", "alignItems": "center",
        }),
        html.Div(_pages, style={"padding": "20px 24px"}),
    ], style={"marginLeft": "195px", "minHeight": "100vh", "background": C["bg"]}),
], style={"background": C["bg"]})

# ── Routing ───────────────────────────────────────────────────────────────────

@app.callback(
    [Output(f"page-{pid}", "style") for pid in PAGE_IDS]
    + [Output("page-title", "children"), Output("header-context", "children")],
    [Input("url", "pathname"), Input("ws-store", "data")],
)
def _route(pathname: str, ws: str):
    page = (pathname or "/workspace").lstrip("/") or "workspace"
    if page not in PAGE_IDS:
        page = "workspace"
    styles = [{"display": "block"} if pid == page else {"display": "none"} for pid in PAGE_IDS]
    title  = dict(NAV_ITEMS).get(page, page.capitalize())
    ctx    = f"// {ws}" if ws else ""
    return styles + [title, ctx]

# ── Workspace callbacks ───────────────────────────────────────────────────────

@app.callback(
    [Output("ws-dropdown", "options"), Output("ws-dropdown", "value")],
    Input("url", "pathname"),
)
def _init_ws_dd(_):
    wss  = _workspaces()
    opts = [{"label": w, "value": w} for w in wss]
    task = _active_task()
    presel = (task.get("workspace") or "").replace("workspaces/", "").strip("/")
    value  = presel if presel in wss else (wss[0] if wss else None)
    return opts, value

@app.callback(Output("ws-store", "data"), Input("ws-dropdown", "value"), prevent_initial_call=True)
def _set_ws(ws): return ws or ""

@app.callback(Output("sidebar-ws", "children"), Input("ws-store", "data"))
def _sidebar_ws(ws): return ws or "—"

@app.callback(
    [Output("ws-status-body", "children"), Output("ws-kpi-body", "children"),
     Output("ws-profiles-body", "children")],
    [Input("slow-tick", "n_intervals"), Input("ws-store", "data")],
)
def _ws_refresh(_n, ws):
    none_msg = html.Div("Select a workspace.", style={"color": C["muted"], "padding": "16px"})
    if not ws:
        return none_msg, none_msg, none_msg
    arts    = _ws_artifacts(ws)
    summary = arts["mapping"].get("summary", {})
    profiles= arts["profiles"].get("profiles", [])
    panel   = arts["panel"]
    opts    = panel.get("options") or []
    lock    = _lock_state(ws)

    status = html.Div([
        _kv("Path",      f"workspaces/{ws}"),
        _kv("KPI ready", summary.get("ready_kpi_count", "—"),   C["green"]),
        _kv("Blocked",   summary.get("blocked_kpi_count", "—"),  C["orange"]),
        _kv("Unresolved",summary.get("unresolved_feature_count","—"), C["orange"]),
        _kv("Profiles",  len(profiles), C["blue"]),
        _kv("Design",    "present" if (_med_gen(ws) / "manifest.yaml").exists() else "absent",
            C["green"] if (_med_gen(ws) / "manifest.yaml").exists() else C["muted"]),
        _kv("Build lock",
            f"active (PID {lock['pid']})" if lock["locked"] else "clear",
            C["red"] if lock["locked"] else C["green"]),
    ])

    kpi_body = html.Div([
        html.Div(panel.get("question") or "No active blocker panel.",
                 style={"color": C["text"], "fontWeight": "600", "fontSize": "0.84rem",
                        "marginBottom": "8px"}),
        html.Div(panel.get("blocker") or "",
                 style={"color": C["muted"], "fontSize": "0.76rem", "marginBottom": "10px"}),
        *[html.Div([
            html.Span(o.get("option_id",""), style={"color": C["blue"], "fontWeight":"700",
                                                     "fontFamily":"monospace"}),
            html.Span("  " + o.get("label",""),
                      style={"color": C["text"], "fontSize":"0.78rem"}),
          ], style={"padding":"5px 0", "borderBottom": f"1px solid {C['dim']}"})
          for o in opts[:4]],
    ], style={"maxHeight": "220px", "overflowY": "auto"}) if opts else \
    html.Div("No blocker panel. Run Onboard Workspace first.",
             style={"color": C["muted"], "fontSize": "0.8rem"})

    if profiles:
        rows = [{"Dataset": Path(p.get("path","")).name,
                 "Rows":    p.get("row_count",""),
                 "Cols":    len(p.get("schema") or {}),
                 "Source":  ", ".join(p.get("sources_used",[])),
                 "Warnings":len(p.get("warnings",[])),
                 } for p in profiles[:30]]
        prof_body = _dt(rows, ["Dataset","Rows","Cols","Source","Warnings"], page=12)
    else:
        prof_body = html.Div("No profiles yet.", style={"color": C["muted"]})

    return status, kpi_body, prof_body

@app.callback(
    Output("ws-action-out", "children"),
    [Input("btn-onboard","n_clicks"), Input("btn-validate","n_clicks"),
     Input("btn-gen-kpi-sql","n_clicks")],
    State("ws-store","data"), prevent_initial_call=True,
)
def _ws_action(ob, vb, gb, ws):
    if not ws:
        return "No workspace selected."
    ctx = callback_context.triggered[0]["prop_id"].split(".")[0]
    cmd = {"btn-onboard": "onboard-workspace",
           "btn-validate": "validate-workspace-artifacts",
           "btn-gen-kpi-sql": "generate-kpi-sql"}.get(ctx)
    if not cmd:
        return no_update
    r = _run_cmd(ws, cmd)
    return (r.get("stdout") or "") + (r.get("stderr") or "") + (r.get("error") or "")

# ── Build callbacks ───────────────────────────────────────────────────────────

@app.callback(
    [Output("build-lock-banner","children"), Output("build-log","children")],
    [Input("fast-tick","n_intervals"), Input("ws-store","data")],
)
def _build_fast(_n, ws):
    if not ws:
        return html.Div(), "(select a workspace)"
    lock = _lock_state(ws)
    log  = _tail(_live_log(ws))
    if lock["locked"]:
        age   = lock["age_s"]
        pid   = lock["pid"]
        alive = _pid_alive(pid)
        banner = dbc.Alert([
            html.Strong("Build running  "),
            _badge(f"PID {pid}", C["blue"]),
            html.Span(f"  {age//60}m {age%60}s elapsed",
                      style={"color": C["muted"], "fontSize": "0.8rem", "marginLeft": "8px"}),
            html.Span("  process alive" if alive else "  process gone",
                      style={"color": C["green"] if alive else C["orange"],
                             "marginLeft": "8px", "fontSize": "0.8rem"}),
        ], color="dark",
            style={"border": f"1px solid {C['orange']}", "borderLeft": f"4px solid {C['orange']}",
                   "borderRadius": "6px", "marginBottom": "12px", "padding": "10px 16px"})
    else:
        banner = html.Div()
    return banner, log

@app.callback(
    Output("build-run-history","children"),
    [Input("slow-tick","n_intervals"), Input("ws-store","data")],
)
def _run_history(_n, ws):
    if not ws:
        return html.Div("Select a workspace.", style={"color": C["muted"]})
    runs = _runs(ws)
    if not runs:
        return html.Div("No runs yet.", style={"color": C["muted"], "padding": "20px"})
    rows = []
    for r in runs:
        tst = r.get("per_table_status") or {}
        ok  = sum(1 for v in tst.values() if v.get("status") == "ok")
        fail= sum(1 for v in tst.values() if v.get("status") == "failed")
        rows.append({
            "Run ID":   r.get("run_id","")[:22],
            "Started":  _ts_fmt(r.get("started_at","")),
            "Target":   r.get("target_actual",""),
            "OK":       ok,
            "Failed":   fail,
            "Rows":     f"{sum(v.get('row_count_after',0) for v in tst.values()):,}",
            "Elapsed s":round(r.get("elapsed_seconds",0),1),
            "Degraded": "yes" if r.get("degraded_run") else "",
        })
    return _dt(rows, ["Run ID","Started","Target","OK","Failed","Rows","Elapsed s","Degraded"], page=15)

@app.callback(
    Output("build-lock-banner","style"),
    Input("btn-run-build","n_clicks"),
    [State("ws-store","data"), State("build-target","value"),
     State("build-only-layer","value"), State("build-only-table","value")],
    prevent_initial_call=True,
)
def _do_run(_n, ws, target, layer, table):
    if ws:
        _trigger_build(ws, target or "auto", layer or "", table or "")
    return {}

@app.callback(Output("build-log","style"), Input("btn-kill-build","n_clicks"),
              State("ws-store","data"), prevent_initial_call=True)
def _do_kill(_n, ws):
    if ws:
        _kill_build(ws)
    return PRE_S

@app.callback(Output("ws-action-out","style"), Input("btn-design","n_clicks"),
              State("ws-store","data"), prevent_initial_call=True)
def _do_design(_n, ws):
    if ws:
        _run_cmd(ws, "design-medallion")
    return {}

# ── Medallion callbacks ───────────────────────────────────────────────────────

@app.callback(
    [Output("med-latest-run","children"), Output("med-table-status","children"),
     Output("med-row-chart","children"),  Output("med-dur-chart","children"),
     Output("med-kpi-chart","children"),  Output("med-assert-chart","children")],
    [Input("slow-tick","n_intervals"), Input("ws-store","data")],
)
def _med_refresh(_n, ws):
    none_msg = html.Div("Select a workspace.", style={"color": C["muted"]})
    if not ws:
        return [none_msg]*6
    runs = _runs(ws)
    if not runs:
        no_r = html.Div("No runs yet. Trigger a build.", style={"color": C["muted"], "padding": "20px"})
        return [no_r]*6

    latest  = runs[0]
    tst     = latest.get("per_table_status") or {}
    ok_cnt  = sum(1 for v in tst.values() if v.get("status") == "ok")
    fail_cnt= sum(1 for v in tst.values() if v.get("status") == "failed")

    latest_body = html.Div([
        _kv("Run ID",   latest.get("run_id","")[:20]),
        _kv("Started",  _ts_fmt(latest.get("started_at",""))),
        _kv("Target",   latest.get("target_actual",""), C["blue"]),
        _kv("Elapsed",  f"{latest.get('elapsed_seconds',0):.1f}s"),
        _kv("OK tables",ok_cnt, C["green"]),
        _kv("Failed",   fail_cnt, C["red"] if fail_cnt else C["muted"]),
        _kv("Degraded", "yes" if latest.get("degraded_run") else "no",
            C["orange"] if latest.get("degraded_run") else C["green"]),
    ])

    tbl_rows = []
    for tbl, s in sorted(tst.items()):
        color = {"ok": C["green"], "failed": C["red"], "pending": C["faint"]}.get(
            s.get("status","pending"), C["muted"])
        tbl_rows.append(html.Div([
            _badge(s.get("status","?"), color),
            html.Span(tbl, style={"color": C["text"], "fontFamily": "monospace",
                                   "fontSize": "0.79rem", "marginLeft": "8px"}),
            html.Span(f"{s.get('row_count_after',0):,} rows",
                      style={"color": C["muted"], "fontSize": "0.73rem", "marginLeft": "8px"}),
            html.Span(f"  {s.get('elapsed_s',0):.1f}s",
                      style={"color": C["faint"], "fontSize": "0.71rem"}),
        ], style={"padding": "6px 0", "borderBottom": f"1px solid {C['dim']}",
                  "display": "flex", "alignItems": "center", "gap": "4px", "flexWrap": "wrap"}))
    table_status = html.Div(tbl_rows, style={"maxHeight": "240px", "overflowY": "auto"}) \
        if tbl_rows else html.Div("No table data.", style={"color": C["muted"]})

    # Collect table names across all runs
    tbl_names: set[str] = set()
    for r in runs:
        tbl_names.update((r.get("per_table_status") or {}).keys())

    def _run_ts_list(): return [r.get("started_at","") for r in reversed(runs)]

    # Row count trend
    if tbl_names and len(runs) > 1:
        fig = go.Figure()
        for tbl in sorted(tbl_names):
            ys = [(r.get("per_table_status") or {}).get(tbl, {}).get("row_count_after") for r in reversed(runs)]
            fig.add_trace(go.Scatter(x=_run_ts_list(), y=ys, name=tbl, mode="lines+markers",
                                     line=dict(width=1.5), marker=dict(size=4)))
        fig.update_layout(**PLOT_L, showlegend=True)
        row_chart = dcc.Graph(figure=fig, config={"displayModeBar": False})
    else:
        row_chart = _empty_chart("Need 2+ runs for trend.")

    # Duration breakdown
    if tbl_names and len(runs) > 1:
        xlabels = [r.get("run_id","")[:10] for r in reversed(runs)]
        fig2 = go.Figure()
        for tbl in sorted(tbl_names):
            ys = [(r.get("per_table_status") or {}).get(tbl, {}).get("elapsed_s", 0) for r in reversed(runs)]
            fig2.add_trace(go.Bar(x=xlabels, y=ys, name=tbl))
        fig2.update_layout(**PLOT_L, barmode="stack", showlegend=True)
        dur_chart = dcc.Graph(figure=fig2, config={"displayModeBar": False})
    else:
        dur_chart = _empty_chart("Need 2+ runs.")

    # KPI metric trend
    kpi_names: set[str] = set()
    for r in runs:
        kpi_names.update((r.get("kpi_diff") or {}).keys())
    if kpi_names and len(runs) > 1:
        fig3 = go.Figure()
        for kpi in sorted(kpi_names):
            ys = []
            for r in reversed(runs):
                diff = (r.get("kpi_diff") or {}).get(kpi, {})
                ys.append(diff.get("after") or diff.get("value"))
            fig3.add_trace(go.Scatter(x=_run_ts_list(), y=ys, name=kpi, mode="lines+markers",
                                       line=dict(width=1.5)))
        fig3.update_layout(**PLOT_L, showlegend=True)
        kpi_chart = dcc.Graph(figure=fig3, config={"displayModeBar": False})
    else:
        kpi_chart = _empty_chart("No KPI diff data yet.")

    # Assertion heatmap
    recent = runs[:15]
    if tbl_names and recent:
        rlabels = [r.get("run_id","")[:10] for r in reversed(recent)]
        all_tbls = sorted(tbl_names)
        z = []
        for tbl in all_tbls:
            row = []
            for r in reversed(recent):
                st = (r.get("per_table_status") or {}).get(tbl, {}).get("status","pending")
                row.append(0 if st == "ok" else (1 if st == "failed" else 0.5))
            z.append(row)
        fig4 = go.Figure(go.Heatmap(
            z=z, x=rlabels, y=all_tbls,
            colorscale=[[0, C["green"]], [0.5, C["orange"]], [1, C["red"]]],
            showscale=False,
        ))
        fig4.update_layout(**PLOT_L, height=max(180, 28*len(all_tbls)), xaxis=dict(side="top"))
        assert_chart = dcc.Graph(figure=fig4, config={"displayModeBar": False})
    else:
        assert_chart = _empty_chart("No assertion data.")

    return latest_body, table_status, row_chart, dur_chart, kpi_chart, assert_chart

# ── Lineage callbacks ─────────────────────────────────────────────────────────

@app.callback(
    [Output("lin-sankey","children"), Output("lin-table-select","options")],
    [Input("slow-tick","n_intervals"), Input("ws-store","data")],
)
def _lin_refresh(_n, ws):
    if not ws:
        return _empty_chart("Select a workspace."), []
    data  = _lineage(ws)
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    if not nodes:
        return _empty_chart("No lineage. Run design-medallion first."), []

    labels = [f"{n['layer']}.{n['table']}" for n in nodes]
    idx    = {lbl: i for i, lbl in enumerate(labels)}
    SRC, TGT, VAL, HTEXT = [], [], [], []
    for e in edges:
        fi = idx.get(e.get("from_node",""))
        ti = idx.get(e.get("to_node",""))
        if fi is not None and ti is not None:
            nc = len(e.get("from_columns") or []) or 1
            SRC.append(fi)
            TGT.append(ti)
            VAL.append(nc)
            HTEXT.append(f"{e.get('transform_type','?')} ({nc} cols)")

    lc = {"bronze": "#cd7f32", "silver": "#a8b8c8", "gold": "#f5c430"}
    node_colors = [lc.get(n["layer"], C["blue"]) for n in nodes]
    fig = go.Figure(go.Sankey(
        node=dict(label=labels, color=node_colors, pad=14, thickness=18,
                  hovertemplate="%{label}<extra></extra>"),
        link=dict(source=SRC, target=TGT, value=VAL,
                  customdata=HTEXT, hovertemplate="%{customdata}<extra></extra>",
                  color="rgba(79,156,249,0.15)"),
    ))
    fig.update_layout(**PLOT_L, height=380)
    opts = [{"label": lbl, "value": lbl} for lbl in labels]
    return dcc.Graph(figure=fig, config={"displayModeBar": False}), opts

@app.callback(
    Output("lin-column-body","children"),
    [Input("lin-table-select","value"), Input("ws-store","data")],
)
def _lin_cols(selected, ws):
    if not ws or not selected:
        return html.Div("Select a table above.", style={"color": C["muted"]})
    edges = (_lineage(ws).get("edges") or [])
    rows = []
    for e in edges:
        if e.get("from_node") == selected:
            for fc, tc in zip(e.get("from_columns",[]), e.get("to_columns",[])):
                rows.append({"Dir":"→", "From Col": fc, "To Table": e["to_node"],
                             "To Col": tc, "Transform": e.get("transform_type","?")})
        elif e.get("to_node") == selected:
            for fc, tc in zip(e.get("from_columns",[]), e.get("to_columns",[])):
                rows.append({"Dir":"←", "From Table": e["from_node"], "From Col": fc,
                             "To Col": tc, "Transform": e.get("transform_type","?")})
    if not rows:
        return html.Div(f"No column edges for {selected}.", style={"color": C["muted"]})
    return _dt(rows, ["Dir","From Table","From Col","To Table","To Col","Transform"], page=20)

# ── Blocker callbacks ─────────────────────────────────────────────────────────

@app.callback(
    [Output("blocker-panel-body","children"), Output("blocker-option-select","options")],
    [Input("slow-tick","n_intervals"), Input("ws-store","data"),
     Input("blocker-action-out","children")],
)
def _blocker_refresh(_n, ws, _):
    if not ws:
        return html.Div("Select a workspace.", style={"color":C["muted"]}), []
    panel = _blocker_panel(ws)
    opts  = panel.get("options") or []
    if not panel:
        body = html.Div([
            html.Div("No blocker panel.", style={"color": C["muted"], "marginBottom": "8px"}),
            html.Code("uv run blocker-question-panel --workspace workspaces/<ws>",
                      style={"color": C["blue"], "background": C["sidebar"],
                             "padding": "8px 12px", "borderRadius":"4px",
                             "display":"block", "fontSize":"0.76rem"}),
        ])
        return body, []

    body = html.Div([
        html.Div([
            _badge(panel.get("feature","Blocker"), C["blue"]),
            html.Span("  " + (panel.get("reuse_scope") or ""),
                      style={"color": C["muted"], "fontSize": "0.74rem"}),
        ], style={"marginBottom": "8px"}),
        html.Div(panel.get("question",""), style={"color": C["text"], "fontWeight":"600",
                                                   "fontSize":"0.88rem", "marginBottom":"6px"}),
        html.Div(panel.get("blocker",""), style={"color": C["muted"], "fontSize":"0.76rem",
                                                  "marginBottom":"8px"}),
        html.Div([
            html.Span("Recommended: ", style={"color":C["faint"]}),
            html.Span(panel.get("recommended_answer","-"), style={"color":C["green"],"fontWeight":"600"}),
        ], style={"marginBottom":"8px"}),
        html.Div(panel.get("why",""), style={"color":C["muted"],"fontSize":"0.75rem","marginBottom":"10px"}),
        html.Hr(style={"borderColor":C["dim"],"margin":"8px 0"}),
        *[html.Div([
            html.Div([
                html.Span(o.get("option_id",""), style={"color":C["blue"],"fontWeight":"700",
                                                         "fontFamily":"monospace","fontSize":"0.82rem"}),
                _badge("JSON" if o.get("json_backed") else "manual",
                       C["green"] if o.get("json_backed") else C["muted"]),
            ], style={"display":"flex","gap":"8px","alignItems":"center","marginBottom":"4px"}),
            html.Div(o.get("label",""), style={"color":C["text"],"fontWeight":"600","fontSize":"0.82rem"}),
            html.Div(o.get("business_summary") or o.get("description") or "",
                     style={"color":C["muted"],"fontSize":"0.74rem"}),
            html.Code(o.get("formula","") or "", style={
                "display":"block" if o.get("formula") else "none",
                "background":C["sidebar"],"color":C["text"],"padding":"6px 10px",
                "marginTop":"4px","borderRadius":"4px","whiteSpace":"pre-wrap","fontSize":"0.72rem",
            }),
          ], style={"padding":"8px 0","borderBottom":f"1px solid {C['dim']}"})
          for o in opts[:6]],
    ], style={"maxHeight":"440px","overflowY":"auto"})

    dd_opts = [{"label": f"{o.get('option_id','')}  —  {o.get('label','')}",
                "value": o.get("option_id","")} for o in opts]
    return body, dd_opts

@app.callback(
    Output("blocker-action-out","children"),
    [Input("btn-blocker-refresh","n_clicks"), Input("btn-blocker-validate","n_clicks")],
    State("ws-store","data"), prevent_initial_call=True,
)
def _blocker_actions(rb, vb, ws):
    if not ws:
        return "No workspace."
    ctx = callback_context.triggered[0]["prop_id"].split(".")[0]
    cmd = "blocker-question-panel" if ctx=="btn-blocker-refresh" else "validate-workspace-artifacts"
    r   = _run_cmd(ws, cmd)
    return (r.get("stdout") or "")[-2000:] + (r.get("stderr") or "")

@app.callback(
    Output("blocker-apply-out","children"),
    Input("btn-blocker-apply","n_clicks"),
    [State("ws-store","data"), State("blocker-option-select","value"),
     State("blocker-domain-input","value")],
    prevent_initial_call=True,
)
def _apply_blocker(_n, ws, option_id, domain):
    if not ws or not option_id:
        return "Select workspace + option."
    extra = ["--answer", option_id] + (["--domain", domain] if domain else [])
    r = _run_cmd(ws, "apply-kpi-panel-answer", extra)
    return (r.get("stdout") or "")[-2000:] + (r.get("stderr") or "")

# ── Code Diffs callbacks ──────────────────────────────────────────────────────

@app.callback(
    Output("diff-task-select","options"),
    Input("url","pathname"),
)
def _diff_task_opts(_):
    tasks = _all_tasks()
    active = _active_task().get("id","")
    opts = []
    for t in tasks:
        label = t.get("name") or t.get("id","?")
        if t.get("id") == active:
            label += " ★"
        opts.append({"label": label, "value": t.get("id","")})
    if not opts:
        opts = [{"label": "(no tasks configured)", "value": ""}]
    return opts

@app.callback(
    Output("task-store","data"),
    Input("diff-task-select","value"),
    prevent_initial_call=True,
)
def _set_task(v): return v or ""

@app.callback(
    [Output("diff-file-info","children"), Output("diff-lang-badge","children"),
     Output("diff-code-view","children"), Output("diff-rev-history","children"),
     Output("diff-rev-a","options"),      Output("diff-rev-b","options")],
    [Input("slow-tick","n_intervals"), Input("task-store","data")],
)
def _diffs_context(_n, task_id):
    tasks = _all_tasks()
    task  = next((t for t in tasks if t.get("id") == task_id), None)
    if task is None:
        task = _active_task()

    editable = task.get("editable_file") or task.get("sql_file") or ""
    if not editable:
        empty = html.Div("No editable_file configured in this task.",
                         style={"color": C["muted"], "padding": "12px"})
        return empty, html.Div(), "(no file)", html.Div(), [], []

    path = ROOT / editable
    content = ""
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            content = f"(could not read {editable})"

    lang = _detect_lang(path, content)

    # Execution backend indicator from task
    exp_cmd  = task.get("experiment_cmd", "")
    backend  = "Databricks Jobs" if "databricks" in str(exp_cmd).lower() else \
               "Databricks Warehouse" if ".sql" in str(exp_cmd).lower() else \
               "DuckDB (local)"

    file_info = html.Div([
        _kv("File",    editable),
        _kv("Backend", backend, C["blue"]),
        _kv("Task",    task.get("name") or task.get("id","—")),
        _kv("Size",    f"{len(content):,} chars  ·  {content.count(chr(10))+1} lines"),
    ])

    lang_badge = html.Div([
        _lang_badge(lang),
        html.Span(f"  {lang} experiment", style={"color": C["muted"], "fontSize": "0.76rem",
                                                   "marginLeft": "6px"}),
    ])

    # Code view with syntax colouring
    code_view = _colorize(content, lang) if content else ["(empty file)"]

    # Revision history for this specific file
    log_entries = _git_log_file(editable)
    if log_entries:
        rev_rows = []
        for e in log_entries:
            rev_rows.append(html.Div([
                html.Span(e["hash"], style={"color": C["blue"], "fontFamily": "monospace",
                                             "fontSize": "0.76rem", "fontWeight": "600"}),
                html.Span("  " + e["message"][:60], style={"color": C["text"], "fontSize": "0.78rem"}),
            ], style={"padding": "6px 0", "borderBottom": f"1px solid {C['dim']}"}))
        rev_history = html.Div(rev_rows, style={"maxHeight": "400px", "overflowY": "auto"})
        rev_opts = [{"label": f"{e['hash']}  {e['message'][:45]}", "value": e["hash"]}
                    for e in log_entries]
    else:
        rev_history = html.Div("No git history for this file yet.",
                               style={"color": C["muted"]})
        rev_opts = []

    return file_info, lang_badge, code_view, rev_history, rev_opts, rev_opts

@app.callback(
    Output("diff-output","children"),
    Input("btn-diff-go","n_clicks"),
    [State("diff-rev-a","value"), State("diff-rev-b","value"),
     State("task-store","data")],
    prevent_initial_call=True,
)
def _show_diff(_n, rev_a, rev_b, task_id):
    tasks    = _all_tasks()
    task     = next((t for t in tasks if t.get("id") == task_id), _active_task())
    editable = task.get("editable_file") or task.get("sql_file") or ""
    diff_text = _git_diff_file(rev_a or "", rev_b or "", editable)
    if not diff_text:
        return ["Select Rev A and Rev B first."]
    return _colorize_diff(diff_text)

# ── Budget callbacks ──────────────────────────────────────────────────────────

@app.callback(
    [Output("budget-tracker-body","children"), Output("budget-tiers-body","children"),
     Output("budget-cost-chart","children")],
    Input("slow-tick","n_intervals"),
)
def _budget_refresh(_n):
    b       = _budget_state()
    cache   = _model_cache()
    spent   = b.get("spent", 0.0)
    cap     = b.get("max_usd", 0.0)
    history = b.get("history") or []
    pct     = (spent / cap * 100) if cap else 0
    bar_col = C["red"] if pct > 90 else C["orange"] if pct > 70 else C["green"]

    tracker = html.Div([
        _kv("Spent",     f"${spent:.4f}", bar_col),
        _kv("Cap",       f"${cap:.2f}"),
        _kv("Remaining", f"${max(0,cap-spent):.4f}", C["green"]),
        _kv("Burn %",    f"{pct:.1f}%", bar_col),
        _kv("Charges",   len(history)),
        html.Div(style={"height":"6px","background":C["dim"],"borderRadius":"3px","marginTop":"12px"}),
        html.Div(style={"width":f"{min(100,pct):.1f}%","height":"6px",
                        "background": bar_col,"borderRadius":"3px","marginTop":"-6px"}),
    ])

    if cache:
        tier_rows = [{"Model": mid[:32], "Tier": info.get("tier","?"),
                      "Context": str(info.get("context_window","?")),
                      "Vision": "yes" if info.get("vision") else "",
                      "Cached": _ts_fmt(info.get("cached_at",""))}
                     for mid, info in list(cache.items())[:20]]
        tiers = _dt(tier_rows, ["Model","Tier","Context","Vision","Cached"])
    else:
        tiers = html.Div("No model cache.", style={"color":C["muted"]})

    if len(history) > 1:
        ts_list = [h.get("ts") or h.get("timestamp","") for h in history]
        cum, total = [], 0.0
        for h in history:
            total += h.get("usd",0)
            cum.append(total)
        fig = go.Figure(go.Scatter(x=ts_list, y=cum, mode="lines+markers",
                                   line=dict(color=C["orange"],width=2),
                                   marker=dict(size=4)))
        fig.update_layout(**PLOT_L, yaxis=dict(**PLOT_L.get("yaxis",{}), title="USD"))
        cost = dcc.Graph(figure=fig, config={"displayModeBar": False})
    else:
        cost = _empty_chart("No cost history.")

    return tracker, tiers, cost

# ── Govern callbacks ──────────────────────────────────────────────────────────

@app.callback(
    [Output("gov-intern-feed","children"), Output("gov-git-log","children"),
     Output("gov-decisions","children"),   Output("gov-alerts","children"),
     Output("gov-ideas","children"),       Output("gov-results","children")],
    Input("slow-tick","n_intervals"),
)
def _govern_refresh(_n):
    db = _load_global_db()

    INTERN_C = {"prompt_engineer": C["blue"], "code_reviewer": C["green"],
                "insights": C["purple"], "methodology_analyst": C["orange"],
                "deep_research": C["red"]}

    intern_rows = []
    for e in (db["intern_logs"] or [])[:20]:
        el  = e.get("elapsed_s")
        els = f"{el:.1f}s" if isinstance(el, (int,float)) else ""
        col = INTERN_C.get(e.get("intern",""), C["muted"])
        intern_rows.append(html.Div([
            html.Div([
                html.Span(e.get("intern","?"), style={"color":col,"fontWeight":"600","fontSize":"0.79rem",
                                                       "fontFamily":"monospace"}),
                html.Span(f"  {els}", style={"color":C["muted"],"fontSize":"0.73rem","marginLeft":"4px"}),
                html.Span(_ts_fmt(e.get("timestamp","")),
                          style={"color":C["faint"],"float":"right","fontSize":"0.71rem"}),
            ], style={"marginBottom":"3px"}),
            html.Div(str(e.get("snippet") or e.get("activity") or "")[:140],
                     style={"color":C["text"],"fontSize":"0.76rem","lineHeight":"1.4"}),
        ], style={"padding":"7px 10px","borderBottom":f"1px solid {C['dim']}","fontFamily":"monospace"}))

    intern_feed = html.Div(intern_rows, style={"maxHeight":"300px","overflowY":"auto"}) \
        if intern_rows else html.Div("No intern activity.", style={"color":C["muted"],"padding":"20px"})

    git_log = _git_log_text()

    gov_rows = db.get("governance") or []
    decisions = _dt([{"Run": r["run_id"][:12], "Decision": r["decision"],
                       "Approval": r["approval"], "Rationale": r["rationale"][:70],
                       "Time": _ts_fmt(r["timestamp"])} for r in gov_rows],
                    ["Run","Decision","Approval","Rationale","Time"], page=10) \
        if gov_rows else html.Div("No decisions.", style={"color":C["muted"]})

    alert_divs = []
    for a in (db.get("alerts") or [])[:15]:
        sc = {"critical":C["red"],"warning":C["orange"]}.get(a["severity"],C["blue"])
        alert_divs.append(html.Div([
            html.Div([_badge(a["severity"],sc),
                      html.Span(f"  {a.get('run_id','')[:12]}",
                                style={"color":C["muted"],"fontSize":"0.73rem"}),
                      html.Span(_ts_fmt(a.get("timestamp","")),
                                style={"color":C["faint"],"float":"right","fontSize":"0.71rem"})],
                     style={"marginBottom":"4px"}),
            html.Div(a.get("title",""), style={"color":C["text"],"fontWeight":"600","fontSize":"0.8rem"}),
            html.Div(a.get("message","")[:120], style={"color":C["muted"],"fontSize":"0.74rem"}),
        ], style={"padding":"8px 0","borderBottom":f"1px solid {C['dim']}"}))
    alerts_body = html.Div(alert_divs, style={"maxHeight":"300px","overflowY":"auto"}) \
        if alert_divs else html.Div("No alerts.", style={"color":C["muted"]})

    ideas = db.get("ideas") or "No ideas yet."

    results = db.get("results") or []
    STATUS_COL = {"keep": C["green"],"review": C["blue"],"discard": C["orange"],"crash": C["red"]}
    if results:
        rrows = [{"#": r["id"], "Commit": r["commit"],
                  "Metric": f"{r['metric']:.4f}" if r["metric"] is not None else "—",
                  "Status": r["status"], "Description": r["description"][:80],
                  "Time": _ts_fmt(r.get("timestamp",""))}
                 for r in reversed(results[-50:])]
        results_body = dash_table.DataTable(
            data=rrows, columns=[{"name": c, "id": c} for c in ["#","Commit","Metric","Status","Description","Time"]],
            page_size=20, sort_action="native", filter_action="native",
            style_table={"overflowX":"auto"},
            style_cell=CELL, style_header=HCELL,
            style_data_conditional=[
                {"if": {"filter_query": f'{{Status}} = "{s}"'}, "color": col}
                for s, col in STATUS_COL.items()
            ] + [{"if": {"row_index": "odd"}, "background": C["card2"]}],
        )
    else:
        results_body = html.Div("No experiment results.", style={"color":C["muted"]})

    return intern_feed, git_log, decisions, alerts_body, ideas, results_body

# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port",  type=int, default=int(os.environ.get("DASH_PORT", 8050)))
    p.add_argument("--debug", action="store_true")
    a = p.parse_args()
    print(f"\n  Autoresearch Control Plane  ->  http://localhost:{a.port}\n")
    app.run(debug=a.debug, port=a.port)
