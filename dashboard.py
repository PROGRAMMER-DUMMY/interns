"""
Autoresearch Control Plane  —  dashboard.py v3
Run: uv run python dashboard.py [--port 8050]
"""
from __future__ import annotations

import base64
import csv
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

from core.config import load as load_config
from core.agents.llm_engine import APIEngine, CLIEngine
from dashboard_app.review import layout as review_page
from core.dashboard_services import (
    BuildControlService,
    DashboardPaths,
    GitHistoryService,
    ReviewerProofService,
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
REVIEWER_PROOF = ReviewerProofService(PATHS)

# ── Colour system ─────────────────────────────────────────────────────────────
C = dict(
    bg       = "#F5F7FA",
    card     = "#FFFFFF",
    card2    = "#F8FAFC",
    sidebar  = "#FFFFFF",
    border   = "rgba(15, 23, 42, 0.15)",
    dim      = "rgba(15, 23, 42, 0.08)",
    text     = "#172033",
    muted    = "#64748B",
    faint    = "#94A3B8",
    blue     = "#185FA5",
    blue_bg  = "#E6F1FB",
    green    = "#3B6D11",
    green_bg = "#EAF3DE",
    orange   = "#854F0B",
    orange_bg= "#FAEEDA",
    red      = "#A32D2D",
    red_bg   = "#FCEBEB",
    purple   = "#5B4E9B",
    yellow   = "#B7791F",
    teal     = "#0F766E",
    teal_bg  = "#DDF7F3",
    hdr      = "#FFFFFF",
)

# Code type colour map
LANG_C = dict(SQL=C["blue"], Polars=C["purple"], PySpark=C["orange"],
              Python=C["teal"], Combined=C["yellow"])

FAST_MS = 2_000
SLOW_MS = 15_000
PAGE_IDS = ["workspace","chat","review","assistant","build","medallion","lineage","blockers","diffs","budget","govern"]
NAV_ITEMS = [
    ("workspace", "Overview", "Main", "⌂", ""),
    ("chat",      "Chat", "Main", "◆", ""),
    ("review",    "Review", "Main", "□", ""),
    ("assistant", "Assistant", "Main", "A", ""),
    ("blockers",  "Blockers", "Main", "?", "4"),
    ("build",     "Build", "Tools", "↻", ""),
    ("medallion", "Medallion", "Tools", "▦", ""),
    ("lineage",   "Lineage", "Tools", "◇", ""),
    ("diffs",     "Code diffs", "Tools", "≠", ""),
    ("budget",    "Budget", "System", "$", ""),
    ("govern",    "Govern", "System", "⚙", ""),
]
NAV_LABELS = {pid: label for pid, label, *_ in NAV_ITEMS}

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

SECRET_PATTERNS = (
    ".env", ".databrickscfg", "id_rsa", "id_dsa", "private_key", "token",
    "secret", "password", "credential", "cookie", "key.pem",
)
TEXT_EXTS = {".txt", ".log", ".sql", ".py", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
MARKDOWN_EXTS = {".md", ".markdown"}
TABLE_EXTS = {".csv", ".tsv", ".parquet", ".xlsx", ".xls", ".duckdb", ".db", ".sqlite"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
JSON_EXTS = {".json", ".jsonl"}
MAX_RENDER_BYTES = 800_000
ARTIFACT_COLLECTIONS = [
    ("all", "All files"),
    ("requirements", "Requirements"),
    ("contracts", "Contracts"),
    ("profiles", "Profiles"),
    ("evidence", "Evidence"),
    ("reports", "Reports"),
    ("runs", "Runs / Results"),
    ("memory", "Memory / Wiki"),
    ("state", "State"),
    ("project", "Project / Global"),
]

PROJECT_SAFE_ROOTS = (
    "README.md", "CONTEXT.md", "TOOLS.md", "GEMINI.md", "AGENTS.md", "program.md",
    "config", ".agents", "docs", "skills",
)
PROJECT_ADVANCED_ROOTS = (
    "core", "tools", "interns", "tests", "dashboard_app", "assets", "state",
)

def _is_secret_path(path: Path) -> bool:
    lower = str(path).replace("\\", "/").lower()
    return any(pattern in lower for pattern in SECRET_PATTERNS)

def _is_raw_dataset_path(path: Path) -> bool:
    lower = str(path).replace("\\", "/").lower()
    return "/datasets/" in lower or "/data/" in lower or "/raw/" in lower

def _safe_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except Exception:
        return path.as_posix()

def _path_from_rel(rel: str) -> Path | None:
    if not rel:
        return None
    try:
        path = (ROOT / rel).resolve()
        if ROOT.resolve() not in [path, *path.parents]:
            return None
        return path
    except Exception:
        return None

def _artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in MARKDOWN_EXTS:
        return "Markdown"
    if suffix in JSON_EXTS:
        return "JSON"
    if suffix in TABLE_EXTS:
        return "Table"
    if suffix in IMAGE_EXTS:
        return "Image"
    if suffix in TEXT_EXTS:
        return "Text"
    return "File"

def _artifact_family(rel: str, kind: str) -> str:
    path = rel.replace("\\", "/").lower()
    name = Path(path).name
    if "/generated/requirements/" in path:
        return "requirements"
    if "/generated/contracts/" in path or "contract" in name or "mapping" in name:
        return "contracts"
    if "/generated/profiles/" in path or "profile" in name:
        return "profiles"
    if "/generated/evidence" in path or "proof" in name or "evidence" in name:
        return "evidence"
    if "/runs/" in path or "result" in name or name == "run.json":
        return "runs"
    if "/memory/" in path or "wiki" in path or "decision" in name or "question" in name:
        return "memory"
    if "/state/" in path or name.endswith(".db") or "metadata_store" in path:
        return "state"
    if "/reports/" in path or kind == "Markdown":
        return "reports"
    return "project" if not path.startswith("workspaces/") else "reports"

def _artifact_interpreter(path: Path) -> str:
    rel = _safe_rel(path).lower()
    name = path.name.lower()
    if name == "kpi_feature_mapping.json" or "kpi_registry" in name:
        return "KPI contract"
    if "profile" in rel and path.suffix.lower() == ".json":
        return "Profile summary"
    if "blocker_question_panel" in rel:
        return "Blocker panel"
    if "source_to_target_plan" in rel:
        return "Source plan"
    if "relationship" in rel and path.suffix.lower() == ".json":
        return "Relationship graph"
    if "memory" in rel or "decision" in name or "wiki" in rel:
        return "Memory timeline"
    return _artifact_kind(path)

def _scope_roots(scope: str, ws: str, advanced: list[str] | None) -> list[Path]:
    advanced_on = "advanced" in (advanced or [])
    if scope == "workspace":
        return [_ws(ws)] if ws else []
    roots = [ROOT / item for item in PROJECT_SAFE_ROOTS]
    if advanced_on:
        roots.extend(ROOT / item for item in PROJECT_ADVANCED_ROOTS)
    return roots

def _artifact_inventory(scope: str, ws: str, advanced: list[str] | None = None, limit: int = 1200) -> list[dict]:
    rows: list[dict] = []
    for root in _scope_roots(scope, ws, advanced):
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
        for path in paths:
            if len(rows) >= limit:
                return rows
            rel = _safe_rel(path)
            if _is_secret_path(path) or "__pycache__" in rel or ".git/" in rel:
                continue
            try:
                size = path.stat().st_size
                mtime = int(path.stat().st_mtime)
            except OSError:
                continue
            kind = _artifact_kind(path)
            rows.append({
                "label": rel,
                "value": rel,
                "scope": scope,
                "kind": kind,
                "family": _artifact_family(rel, kind),
                "interpreter": _artifact_interpreter(path),
                "size": size,
                "updated": _epoch_fmt(mtime),
            })
    return rows

def _filter_artifact_rows(rows: list[dict], collection: str | None, query: str | None) -> list[dict]:
    collection = collection or "all"
    term = (query or "").lower().strip()
    if collection != "all":
        rows = [row for row in rows if row.get("family") == collection]
    if term:
        rows = [
            row for row in rows
            if term in row["label"].lower()
            or term in row["kind"].lower()
            or term in row.get("interpreter", "").lower()
        ]
    return rows

def _collection_options(rows: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        family = row.get("family", "project")
        counts[family] = counts.get(family, 0) + 1
    total = len(rows)
    options = []
    for key, label in ARTIFACT_COLLECTIONS:
        count = total if key == "all" else counts.get(key, 0)
        options.append({"label": f"{label} ({count})", "value": key, "disabled": count == 0})
    return options

def _artifact_options(scope: str, ws: str, advanced: list[str] | None, query: str = "") -> list[dict]:
    term = (query or "").lower().strip()
    rows = _artifact_inventory(scope, ws, advanced)
    if term:
        rows = [row for row in rows if term in row["label"].lower() or term in row["kind"].lower()]
    return [
        {"label": f"{row['kind']} · {row['label']}", "value": row["value"]}
        for row in rows[:250]
    ]

def _read_text_limited(path: Path, max_bytes: int = MAX_RENDER_BYTES) -> str:
    data = path.read_bytes()[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    if path.stat().st_size > max_bytes:
        text += f"\n\n[truncated at {max_bytes:,} bytes]"
    return text

def _render_json(path: Path):
    if path.suffix.lower() == ".jsonl":
        lines = _read_text_limited(path).splitlines()[:200]
        return html.Pre("\n".join(lines), style={**PRE_S, "maxHeight": "560px"})
    payload = _jread(path, None)
    if payload is None:
        return html.Pre(_read_text_limited(path), style={**PRE_S, "maxHeight": "560px"})
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        cols = list(payload[0].keys())[:12]
        return html.Div([
            _dt([{k: item.get(k, "") for k in cols} for item in payload[:200]], cols, page=12),
            html.Pre(json.dumps(payload, indent=2)[:12000], style={**PRE_S, "maxHeight": "260px", "marginTop": "10px"}),
        ])
    return html.Pre(json.dumps(payload, indent=2)[:40000], style={**PRE_S, "maxHeight": "560px"})

def _render_kpi_contract(payload: Any):
    if not isinstance(payload, dict):
        return None
    kpis = payload.get("kpis") or payload.get("items") or []
    if not isinstance(kpis, list) or not kpis:
        return None
    rows = []
    for kpi in kpis[:200]:
        if not isinstance(kpi, dict):
            continue
        blocked = kpi.get("blocked_features") or kpi.get("unresolved_features") or []
        rows.append({
            "KPI": kpi.get("kpi_id") or kpi.get("id") or "",
            "Name": kpi.get("name") or str(kpi.get("question") or "")[:90],
            "Ready": "No" if blocked else "Yes",
            "Blocked": len(blocked) if isinstance(blocked, list) else str(blocked),
        })
    return _dt(rows, ["KPI", "Name", "Ready", "Blocked"], page=12) if rows else None

def _render_profile_summary(payload: Any):
    if not isinstance(payload, dict):
        return None
    profiles = payload.get("profiles") or payload.get("tables") or []
    if isinstance(profiles, list) and profiles:
        rows = []
        for item in profiles[:200]:
            if isinstance(item, dict):
                rows.append({
                    "Dataset": item.get("dataset") or item.get("table") or item.get("name") or item.get("path") or "",
                    "Rows": item.get("row_count") or item.get("rows") or "",
                    "Columns": item.get("column_count") or item.get("columns") or "",
                    "Status": item.get("status") or item.get("profile_status") or "",
                })
        return _dt(rows, ["Dataset", "Rows", "Columns", "Status"], page=12) if rows else None
    columns = payload.get("columns") or payload.get("schema") or []
    if isinstance(columns, list) and columns:
        rows = []
        for col in columns[:250]:
            if isinstance(col, dict):
                rows.append({
                    "Column": col.get("name") or col.get("column") or "",
                    "Type": col.get("type") or col.get("dtype") or "",
                    "Nulls": col.get("null_count") or col.get("nulls") or "",
                    "Distinct": col.get("distinct_count") or col.get("n_unique") or "",
                })
        return _dt(rows, ["Column", "Type", "Nulls", "Distinct"], page=14) if rows else None
    return None

def _render_blocker_panel(payload: Any):
    if not isinstance(payload, dict):
        return None
    options = payload.get("options") or []
    rows = [
        {
            "Option": item.get("option_id", ""),
            "Label": item.get("label", ""),
            "Recommended": "Yes" if item.get("recommended") else "",
        }
        for item in options
        if isinstance(item, dict)
    ]
    return html.Div([
        html.Div(payload.get("blocker") or payload.get("feature") or "Blocker panel", className="assistant-interpreter-title"),
        html.Div(payload.get("question") or "", className="assistant-interpreter-copy"),
        _dt(rows, ["Option", "Label", "Recommended"], page=8) if rows else html.Div("No options in this panel.", className="assistant-empty"),
    ])

def _render_source_plan(payload: Any):
    if not isinstance(payload, dict):
        return None
    plans = payload.get("kpis") or payload.get("plans") or payload.get("items") or []
    if not isinstance(plans, list) or not plans:
        return None
    rows = []
    for item in plans[:200]:
        if isinstance(item, dict):
            blockers = item.get("blockers") or item.get("blocked_reasons") or []
            rows.append({
                "KPI": item.get("kpi_id") or item.get("id") or "",
                "Status": item.get("status") or ("blocked" if blockers else "ready"),
                "Datasets": len(item.get("datasets") or item.get("sources") or []),
                "Blockers": len(blockers) if isinstance(blockers, list) else str(blockers),
            })
    return _dt(rows, ["KPI", "Status", "Datasets", "Blockers"], page=12) if rows else None

def _summary_cards(items: list[tuple[str, Any, str | None]]):
    return html.Div([
        html.Div([
            html.Div(label, className="assistant-summary-card-label"),
            html.Div(str(value), className="assistant-summary-card-value", style={"color": color or C["text"]}),
        ], className="assistant-summary-card")
        for label, value, color in items
    ], className="assistant-summary-card-grid")

def _render_domain_model(payload: Any):
    if not isinstance(payload, dict):
        return None
    entities = payload.get("entities") or payload.get("tables") or payload.get("datasets") or []
    relationships = payload.get("relationships") or payload.get("joins") or payload.get("edges") or []
    entity_rows = []
    if isinstance(entities, dict):
        entities = [{"name": key, **(value if isinstance(value, dict) else {})} for key, value in entities.items()]
    for entity in (entities or [])[:250]:
        if isinstance(entity, dict):
            fields = entity.get("fields") or entity.get("columns") or []
            entity_rows.append({
                    "Entity": entity.get("name") or entity.get("id") or entity.get("table") or entity.get("path") or "",
                    "Grain": entity.get("grain") or entity.get("primary_grain") or "",
                    "Columns": len(fields) if isinstance(fields, list) else len(entity.get("schema") or []) if isinstance(entity.get("schema"), list) else "",
                    "Source": entity.get("source") or entity.get("profile_path") or entity.get("path") or "",
            })
    rel_count = len(relationships) if isinstance(relationships, list) else 0
    return html.Div([
        _summary_cards([
            ("Entities", len(entity_rows), C["blue"]),
            ("Relationships", rel_count, C["teal"]),
            ("Status", payload.get("status") or "loaded", C["green"]),
        ]),
        _dt(entity_rows, ["Entity", "Grain", "Columns", "Source"], page=12) if entity_rows else html.Div("No entities found.", className="assistant-empty"),
    ])

def _render_relationship_contracts(payload: Any):
    if not isinstance(payload, dict):
        return None
    relationships = payload.get("relationships") or payload.get("contracts") or payload.get("edges") or []
    if isinstance(relationships, dict):
        relationships = [{"id": key, **(value if isinstance(value, dict) else {})} for key, value in relationships.items()]
    rows = []
    for rel in (relationships or [])[:250]:
        if not isinstance(rel, dict):
            continue
        rows.append({
            "Relationship": rel.get("relationship_id") or rel.get("id") or rel.get("name") or "",
            "From": rel.get("left_dataset") or rel.get("from") or rel.get("source") or rel.get("left") or "",
            "To": rel.get("right_dataset") or rel.get("to") or rel.get("target") or rel.get("right") or "",
            "Usage": rel.get("state") or rel.get("executable_usage") or rel.get("usage") or rel.get("status") or "",
            "Proof": rel.get("confidence") or rel.get("evidence_state") or rel.get("proof") or "",
        })
    return html.Div([
        _summary_cards([
            ("Contracts", len(rows), C["blue"]),
            ("Executable", sum(1 for row in rows if str(row["Usage"]).lower() in {"proven_data_model", "user_confirmed", "allowed", "true"}), C["green"]),
            ("Blocked", sum(1 for row in rows if "block" in str(row["Usage"]).lower()), C["red"]),
        ]),
        _dt(rows, ["Relationship", "From", "To", "Usage", "Proof"], page=12) if rows else html.Div("No relationship contracts found.", className="assistant-empty"),
    ])

def _render_kpi_proof_packet(payload: Any):
    if not isinstance(payload, dict):
        return None
    items = payload.get("kpis") or payload.get("proofs") or payload.get("items") or []
    if isinstance(items, dict):
        items = [{"id": key, **(value if isinstance(value, dict) else {})} for key, value in items.items()]
    rows = []
    for item in (items or [])[:250]:
        if isinstance(item, dict):
            rows.append({
                "KPI": item.get("kpi_id") or item.get("id") or "",
                "Status": item.get("status") or item.get("readiness") or item.get("recommendation_class") or "",
                "SQL": "Yes" if item.get("sql") or item.get("query") or item.get("sql_path") or item.get("sql_summary") else "",
                "Evidence": len(item.get("evidence") or item.get("artifacts") or item.get("mapping_rows") or []),
                "Blockers": len(item.get("blockers") or item.get("missing_artifacts") or []),
            })
    status = payload.get("status") or payload.get("readiness") or "loaded"
    return html.Div([
        _summary_cards([
            ("Proofs", len(rows), C["blue"]),
            ("Ready", sum(1 for row in rows if str(row["Status"]).lower() in {"ready", "passed", "ok"}), C["green"]),
            ("Status", status, C["green"] if str(status).lower() in {"ready", "passed", "ok"} else C["orange"]),
        ]),
        _dt(rows, ["KPI", "Status", "SQL", "Evidence", "Blockers"], page=12) if rows else html.Div("No KPI proof rows found.", className="assistant-empty"),
    ])

def _render_workflow_artifact(payload: Any):
    if not isinstance(payload, dict):
        return None
    steps = payload.get("actions") or payload.get("steps") or payload.get("events") or payload.get("stages") or payload.get("checks") or []
    if isinstance(steps, dict):
        steps = [{"name": key, **(value if isinstance(value, dict) else {})} for key, value in steps.items()]
    rows = []
    for step in (steps or [])[:250]:
        if isinstance(step, dict):
            rows.append({
                "Step": step.get("name") or step.get("stage") or step.get("id") or step.get("event_type") or step.get("command") or "",
                "Status": step.get("status") or step.get("result") or "",
                "Message": str(step.get("message") or step.get("summary") or step.get("description") or "")[:120],
            })
    return html.Div([
        _summary_cards([
            ("Status", payload.get("status") or payload.get("result") or "loaded", C["blue"]),
            ("Steps", len(rows), C["teal"]),
            ("Findings", len(payload.get("findings") or payload.get("blockers") or payload.get("warnings") or []), C["orange"]),
        ]),
        _dt(rows, ["Step", "Status", "Message"], page=12) if rows else html.Div("No workflow steps found.", className="assistant-empty"),
    ])

def _render_memory_timeline(payload: Any):
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get("cards") or payload.get("entries") or payload.get("decisions") or payload.get("items") or payload.get("records") or []
    else:
        return None
    if isinstance(entries, dict):
        entries = [{"id": key, **(value if isinstance(value, dict) else {})} for key, value in entries.items()]
    rows = []
    for entry in (entries or [])[:250]:
        if isinstance(entry, dict):
            rows.append({
                "When": entry.get("created_at_display") or entry.get("timestamp") or entry.get("created_at") or "",
                "Type": entry.get("definition_type") or entry.get("type") or entry.get("status") or entry.get("source") or "",
                "Artifact": entry.get("artifact") or entry.get("path") or "",
                "Text": str(entry.get("canonical_name") or entry.get("text") or entry.get("decision") or entry.get("summary") or entry.get("message") or "")[:160],
            })
    return html.Div([
        _summary_cards([
            ("Entries", len(rows), C["blue"]),
            ("Decisions", sum(1 for row in rows if "decision" in str(row["Type"]).lower()), C["green"]),
            ("Open", sum(1 for row in rows if "open" in str(row["Type"]).lower()), C["orange"]),
        ]),
        _dt(rows, ["When", "Type", "Artifact", "Text"], page=12) if rows else html.Div("No memory entries found.", className="assistant-empty"),
    ])

def _render_interpreted_json(path: Path, payload: Any):
    rel = _safe_rel(path).lower()
    name = path.name.lower()
    if "domain_model" in name:
        return _render_domain_model(payload)
    if "relationship" in rel and path.suffix.lower() == ".json":
        return _render_relationship_contracts(payload)
    if "kpi_proof_packet" in rel or "proof_packet" in name:
        return _render_kpi_proof_packet(payload)
    if "workflow" in rel or "trajectory" in rel or "harness" in rel or "reliability" in rel:
        return _render_workflow_artifact(payload)
    if "memory" in rel or "wiki" in rel or "decision" in name:
        return _render_memory_timeline(payload)
    if name == "kpi_feature_mapping.json" or "kpi_registry" in name:
        return _render_kpi_contract(payload)
    if "profile" in rel:
        return _render_profile_summary(payload)
    if "blocker_question_panel" in rel:
        return _render_blocker_panel(payload)
    if "source_to_target_plan" in rel:
        return _render_source_plan(payload)
    return None

def _render_table(path: Path):
    suffix = path.suffix.lower()
    if suffix in {".parquet", ".xlsx", ".xls", ".duckdb", ".db", ".sqlite"}:
        return html.Div([
            html.Div("Structured data file", className="assistant-interpreter-title"),
            html.Div(
                "Preview is intentionally metadata-only here. Use generated profiles for row samples, column stats, and safe inspection.",
                className="assistant-interpreter-copy",
            ),
            _dt([{
                "File": path.name,
                "Type": suffix.lstrip("."),
                "Size": path.stat().st_size,
                "Path": _safe_rel(path),
            }], ["File", "Type", "Size", "Path"], page=1),
        ])
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    if _is_raw_dataset_path(path):
        return html.Div([
            html.Div("Raw dataset preview blocked", className="assistant-interpreter-title"),
            html.Div(
                "This file is listed for navigation, but raw dataset contents are not rendered in the dashboard. Use workspace profiles for safe inspection.",
                className="assistant-interpreter-copy",
            ),
            _dt([{
                "File": path.name,
                "Type": suffix.lstrip("."),
                "Size": path.stat().st_size,
                "Path": _safe_rel(path),
            }], ["File", "Type", "Size", "Path"], page=1),
        ])
    text = _read_text_limited(path, 300_000)
    rows = list(csv.DictReader(text.splitlines(), delimiter=delimiter))
    if not rows:
        return html.Pre(text[:20000], style={**PRE_S, "maxHeight": "560px"})
    cols = list(rows[0].keys())[:16]
    return _dt([{k: row.get(k, "") for k in cols} for row in rows[:250]], cols, page=14)

def _render_image(path: Path):
    suffix = path.suffix.lower().lstrip(".")
    mime = "image/svg+xml" if suffix == "svg" else f"image/{'jpeg' if suffix == 'jpg' else suffix}"
    data = base64.b64encode(path.read_bytes()[:MAX_RENDER_BYTES]).decode("ascii")
    return html.Img(src=f"data:{mime};base64,{data}", className="assistant-image-preview")

def _render_artifact(rel: str):
    path = _path_from_rel(rel)
    if not path or not path.exists() or not path.is_file():
        return html.Div("Select an artifact to preview.", className="assistant-empty")
    if _is_secret_path(path):
        return html.Div("This file is hidden by the secret guard.", className="assistant-empty")
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    header = html.Div([
        html.Div(_safe_rel(path), className="assistant-artifact-path"),
        html.Div([
            html.Span(_artifact_interpreter(path), className="assistant-interpreter-badge"),
            html.Span(f"{_artifact_kind(path)} · {size:,} bytes", className="assistant-artifact-meta"),
        ], className="assistant-artifact-meta-row"),
    ], className="assistant-artifact-header")
    if size > MAX_RENDER_BYTES and path.suffix.lower() in IMAGE_EXTS:
        body = html.Div("Image is too large for inline preview.", className="assistant-empty")
    elif path.suffix.lower() in MARKDOWN_EXTS:
        body = dcc.Markdown(_read_text_limited(path), className="assistant-markdown")
    elif path.suffix.lower() in JSON_EXTS:
        payload = _jread(path, None) if path.suffix.lower() == ".json" else None
        interpreted = _render_interpreted_json(path, payload)
        body = html.Div([
            html.Div(interpreted, className="assistant-interpreter") if interpreted else html.Div(
                "Generic JSON preview", className="assistant-interpreter-title"
            ),
            _render_json(path),
        ])
    elif path.suffix.lower() in TABLE_EXTS:
        body = _render_table(path)
    elif path.suffix.lower() in IMAGE_EXTS:
        body = _render_image(path)
    elif path.suffix.lower() in TEXT_EXTS:
        body = html.Pre(_read_text_limited(path), style={**PRE_S, "maxHeight": "560px"})
    else:
        body = html.Div("Preview unavailable for this file type.", className="assistant-empty")
    return html.Div([header, body])

def _chat_artifact_context(matches: list[dict], ws: str, scope: str) -> str:
    lines = [
        f"Workspace: {ws or 'none selected'}",
        f"Scope: {scope or 'workspace'}",
        "Relevant artifacts:",
    ]
    for row in matches[:8]:
        lines.append(
            f"- {row.get('interpreter') or row.get('kind')}: {row.get('label')} "
            f"({row.get('size', 0)} bytes)"
        )
    return "\n".join(lines)

_DASHBOARD_DANGEROUS_TERMS = (
    "delete", "remove", "rm ", "rmdir", "erase", "wipe", "reset --hard",
    "checkout --", "clean -fd", "drop table", "truncate", "push", "deploy",
    "remote execution", "databricks", "token", "secret", ".env", "password",
)
_DASHBOARD_CONFIRM_TERMS = ("approve", "approved", "confirm", "confirmed", "yes do it", "go ahead")

def _dashboard_needs_confirmation(question: str) -> bool:
    text = f" {question.lower()} "
    return any(term in text for term in _DASHBOARD_DANGEROUS_TERMS) and not any(
        term in text for term in _DASHBOARD_CONFIRM_TERMS
    )

def _dashboard_llm_chat(question: str, history: list[dict], matches: list[dict], ws: str, scope: str) -> str | None:
    if os.environ.get("AUTORESEARCH_ALLOW_DASHBOARD_LLM") != "1":
        return None
    if _dashboard_needs_confirmation(question):
        return (
            "That request can change or expose sensitive project state. Reply with an explicit approval "
            "such as `approved: <exact action>` before I run it from the dashboard chat."
        )
    try:
        cfg = load_config()
        agent = os.environ.get("AUTORESEARCH_DASHBOARD_CHAT_AGENT") or cfg.main_agent
        model = os.environ.get("AUTORESEARCH_DASHBOARD_CHAT_MODEL") or cfg.models.prompt_engineer
        recent = []
        for item in history[-6:]:
            role = item.get("role", "assistant")
            text = str(item.get("text", ""))[:900]
            if text:
                recent.append(f"{role}: {text}")
        system = (
            "You are the real interactive agent behind the Autoresearch dashboard. Work from the local "
            f"repository root: {ROOT}. The user expects the dashboard chat to behave like the CLI agent: "
            "inspect files, explain code and artifacts, plan changes, and perform ordinary local project work "
            "when the request is clear. For casual greetings or general questions, answer normally; ask for "
            "active workflow confirmation only before doing project work that depends on a workspace/file set. "
            "Follow AGENTS.md and repo governance. Keep generated project outputs "
            "under the active workspace's interns/ tree. Never reveal secrets or raw secret values. Do not "
            "delete files, run destructive git operations, push, deploy, mutate remote systems, expose .env "
            "contents, or run Databricks/remote execution unless the user explicitly approves the exact action. "
            "Prefer safe local commands and existing project tools. If you make or recommend changes, summarize "
            "the touched files and verification. Use the provided artifact context for inline dashboard previews. "
            "Keep responses concise and action-oriented."
        )
        user = (
            f"Conversation:\n{chr(10).join(recent) or '(none)'}\n\n"
            f"{_chat_artifact_context(matches, ws, scope)}\n\n"
            f"User message: {question}"
        )
        if agent == "api" and cfg.google_api_key:
            answer = APIEngine(cfg.google_api_key).generate(system, user, max_tokens=700, model=model)
            if answer:
                return answer
        timeout_s = int(os.environ.get("AUTORESEARCH_DASHBOARD_CHAT_TIMEOUT", "120"))
        candidates = [agent if agent != "api" else cfg.main_agent, "codex", "gemini-cli", "claude-code"]
        tried: list[str] = []
        for candidate in dict.fromkeys(item for item in candidates if item):
            tried.append(candidate)
            answer = CLIEngine(candidate, cwd=str(ROOT), timeout_s=timeout_s).generate(
                system, user, max_tokens=700, model=model
            )
            if answer:
                return answer
        return f"Real chat backend did not return a response. Tried: {', '.join(tried)}."
    except Exception as exc:
        return f"Real chat failed to start: {exc}"

def _model_graph(ws: str):
    if not ws:
        return _empty_chart("Select a workspace."), html.Div("No workspace selected.", className="assistant-empty")
    base = _ws(ws) / "interns" / "generated"
    contracts_dir = base / "contracts"
    rel_contracts = _jread(contracts_dir / "relationship_contracts.json", {})
    domain_model = _jread(contracts_dir / "domain_model.json", {})
    source_plan = _jread(contracts_dir / "source_to_target_plan.json", {})
    graph = _jread(base / "evidence_graph" / "graph.json", {})
    profiles = _jread(base / "profiles" / "profile_index.json", {"profiles": []})
    nodes_raw = graph.get("nodes") if isinstance(graph, dict) else []
    edges_raw = graph.get("edges") if isinstance(graph, dict) else []
    names: list[str] = []
    edge_records: list[dict[str, str]] = []
    relationships = rel_contracts.get("relationships") if isinstance(rel_contracts, dict) else []
    if isinstance(relationships, dict):
        relationships = [{"relationship_id": key, **(value if isinstance(value, dict) else {})} for key, value in relationships.items()]
    for rel in relationships or []:
        if not isinstance(rel, dict):
            continue
        src = str(rel.get("left_dataset") or rel.get("from") or rel.get("source") or rel.get("left") or "")
        dst = str(rel.get("right_dataset") or rel.get("to") or rel.get("target") or rel.get("right") or "")
        if src and dst:
            names.extend([src, dst])
            edge_records.append({
                "source": src,
                "target": dst,
                "state": str(rel.get("state") or rel.get("executable_usage") or rel.get("status") or ""),
                "label": str(rel.get("relationship_id") or rel.get("id") or rel.get("name") or ""),
            })
    datasets = domain_model.get("datasets") if isinstance(domain_model, dict) else []
    if isinstance(datasets, dict):
        datasets = [{"path": key, **(value if isinstance(value, dict) else {})} for key, value in datasets.items()]
    for dataset in datasets or []:
        if isinstance(dataset, dict):
            name = str(dataset.get("path") or dataset.get("name") or dataset.get("table") or "")
            if name:
                names.append(name)
    plan_kpis = source_plan.get("kpis") if isinstance(source_plan, dict) else []
    for kpi in plan_kpis or []:
        if not isinstance(kpi, dict):
            continue
        for src in kpi.get("selected_source_datasets") or []:
            if src:
                names.append(str(src))
    for node in nodes_raw or []:
        if isinstance(node, dict):
            name = str(node.get("id") or node.get("name") or node.get("label") or "")
            kind = str(node.get("kind") or node.get("type") or "").lower()
            if name and any(token in f"{name} {kind}".lower() for token in ("table", "dataset", "profile", "model", "entity")):
                names.append(name)
        elif node:
            names.append(str(node))
    if not names:
        for item in profiles.get("profiles") or []:
            name = item.get("dataset") or item.get("table") or item.get("name") or item.get("path")
            if name:
                names.append(str(name))
    names = list(dict.fromkeys(names))[:40]
    if not names:
        return _empty_chart("No data model artifacts found."), html.Div(
            "Run onboarding or build relationship contracts to populate model evidence.",
            className="assistant-empty",
        )
    import math
    coords = {
        name: (
            math.cos((2 * math.pi * i) / max(len(names), 1)),
            math.sin((2 * math.pi * i) / max(len(names), 1)),
        )
        for i, name in enumerate(names)
    }
    edge_x: list[float | None] = []
    edge_y: list[float | None] = []
    known = set(names)
    for edge in edge_records or edges_raw or []:
        if isinstance(edge, dict):
            src = str(edge.get("source") or edge.get("from") or edge.get("src") or "")
            dst = str(edge.get("target") or edge.get("to") or edge.get("dst") or "")
        elif isinstance(edge, (list, tuple)) and len(edge) >= 2:
            src, dst = str(edge[0]), str(edge[1])
        else:
            continue
        if src in known and dst in known:
            edge_x += [coords[src][0], coords[dst][0], None]
            edge_y += [coords[src][1], coords[dst][1], None]
    node_x = [coords[name][0] for name in names]
    node_y = [coords[name][1] for name in names]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=edge_x, y=edge_y, mode="lines", line=dict(color="#94A3B8", width=1), hoverinfo="none"))
    fig.add_trace(go.Scatter(
        x=node_x, y=node_y, mode="markers+text", text=[Path(n).stem[:22] for n in names],
        textposition="bottom center", marker=dict(size=18, color=C["blue"]), hovertext=names,
        hoverinfo="text",
    ))
    fig.update_layout(**PLOT_L, showlegend=False, xaxis=dict(visible=False), yaxis=dict(visible=False), height=420)
    rel_rows = [
        {
            "Relationship": edge.get("label", ""),
            "From": edge.get("source", ""),
            "To": edge.get("target", ""),
            "State": edge.get("state", ""),
        }
        for edge in edge_records[:80]
    ]
    details = html.Div([
        _summary_cards([
            ("Entities", len(names), C["blue"]),
            ("Relationships", len(edge_records), C["teal"]),
            ("Source", "contracts" if edge_records or datasets else "evidence/profile", C["purple"]),
        ]),
        _dt(rel_rows, ["Relationship", "From", "To", "State"], page=10) if rel_rows else _dt(
            [{"Entity": Path(name).stem, "Source": name} for name in names],
            ["Entity", "Source"],
            page=10,
        ),
    ])
    return dcc.Graph(figure=fig, config={"displayModeBar": False}), details

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

def _epoch_fmt(ts: int | str) -> str:
    if not ts:
        return ""
    try:
        from datetime import datetime
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]

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
    "border": f"0.5px solid {C['border']}",
    "borderRadius": "12px",
    "marginBottom": "14px",
    "boxShadow": "0 1px 2px rgba(15, 23, 42, 0.03)",
}
HDR_S = {
    "background": C["hdr"],
    "borderBottom": f"0.5px solid {C['dim']}",
    "borderRadius": "12px 12px 0 0",
    "padding": "12px 16px 9px",
    "display": "flex", "alignItems": "center", "gap": "8px",
}
HDR_TITLE = {
    "color": C["text"], "fontWeight": "500", "fontSize": "12.5px",
    "letterSpacing": "0", "textTransform": "none", "margin": 0,
}
CELL = {
    "background": C["card"], "color": C["text"],
    "border": f"0.5px solid {C['dim']}", "fontSize": "12px",
    "fontFamily": "Inter, Figtree, 'Segoe UI', system-ui, sans-serif",
    "padding": "7px 10px", "textAlign": "left", "whiteSpace": "normal",
}
HCELL = {
    "background": C["card2"], "color": C["muted"], "fontWeight": "500",
    "border": f"0.5px solid {C['dim']}", "fontSize": "10px", "letterSpacing": "0.08em",
    "textTransform": "uppercase",
}
PRE_S = {
    "color": C["text"], "background": C["card2"],
    "padding": "14px", "borderRadius": "8px", "fontSize": "12px",
    "lineHeight": "1.65", "overflowY": "auto", "maxHeight": "400px",
    "margin": 0, "whiteSpace": "pre-wrap",
    "fontFamily": "'JetBrains Mono', 'Fira Code', Consolas, monospace",
    "border": f"0.5px solid {C['dim']}",
}
INPUT_S = {
    "background": C["card2"], "color": C["text"],
    "border": f"0.5px solid {C['border']}", "borderRadius": "8px",
    "padding": "7px 12px", "fontSize": "12.5px", "width": "100%",
    "outline": "none",
}
DDL_S = {"background": C["card"], "color": C["text"], "fontSize": "12.5px"}
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
    hdr = {**HDR_S, "boxShadow": f"inset 3px 0 0 {accent}"}
    return {"card": CARD_S, "hdr": hdr}

def _mk_card(title: str, *children, accent: str = C["blue"], body_style: dict | None = None) -> dbc.Card:
    return dbc.Card([
        dbc.CardHeader(
            html.Span(title, style=HDR_TITLE),
            style={**HDR_S, "boxShadow": f"inset 3px 0 0 {accent}"},
        ),
        dbc.CardBody(list(children),
                     style={"padding": "14px 16px", **(body_style or {})}),
    ], style=CARD_S)

def _kv(label: str, value: Any, color: str = "") -> html.Div:
    return html.Div([
        html.Span(label, style={"color": C["muted"], "fontSize": "11px"}),
        html.Span(str(value if value is not None else "—"),
                  style={"color": color or C["text"], "float": "right",
                         "fontWeight": "500", "fontSize": "12px"}),
    ], style={"padding": "7px 0", "borderBottom": f"0.5px solid {C['dim']}"})

def _badge(text: str, color: str) -> html.Span:
    bg = {
        C["blue"]: C["blue_bg"],
        C["green"]: C["green_bg"],
        C["orange"]: C["orange_bg"],
        C["red"]: C["red_bg"],
        C["teal"]: C["teal_bg"],
    }.get(color, C["card2"])
    return html.Span(text, style={
        "background": bg, "color": color,
        "border": f"0.5px solid {C['dim']}",
        "borderRadius": "999px", "padding": "2px 8px",
        "fontSize": "10px", "fontWeight": "500",
        "letterSpacing": "0",
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
        "color": color, "fontSize": "10px", "fontWeight": "500",
        "textTransform": "uppercase", "letterSpacing": "0.07em",
        "marginBottom": "8px", "marginTop": "6px",
    })

def _btn(label: str, bid: str, color: str = "primary", outline: bool = False, **kw) -> dbc.Button:
    return dbc.Button(label, id=bid, color=color, size="sm", outline=outline,
                      className="me-1 mb-1", **kw)

def _metric_card(label: str, value_id: str, trend_id: str, icon: str, accent: str) -> dbc.Card:
    return dbc.Card(dbc.CardBody([
        html.Div([
            html.Span(icon, style={"color": accent, "fontSize": "14px", "marginRight": "7px"}),
            html.Span(label, style={"color": C["muted"], "fontSize": "11px"}),
        ], style={"display": "flex", "alignItems": "center", "marginBottom": "8px"}),
        html.Div(id=value_id, style={
            "color": C["text"], "fontSize": "22px", "fontWeight": "500",
            "lineHeight": "1.15",
        }),
        html.Div(id=trend_id, style={"fontSize": "11px", "marginTop": "6px"}),
    ], style={"padding": "14px 16px"}), style=CARD_S)

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
    grouped: dict[str, list[tuple[str, str, str, str]]] = {}
    for pid, label, group, icon, badge in NAV_ITEMS:
        grouped.setdefault(group, []).append((pid, label, icon, badge))
    nav_groups = []
    for group, items in grouped.items():
        nav_groups.append(html.Div(group, style={
            "color": C["faint"], "fontSize": "10px", "fontWeight": "500",
            "letterSpacing": "0.08em", "textTransform": "uppercase",
            "padding": "14px 12px 6px",
        }))
        for pid, label, icon, badge in items:
            nav_groups.append(
                dcc.Link([
                    html.Span(icon, style={
                        "width": "16px", "display": "inline-flex", "justifyContent": "center",
                        "marginRight": "8px", "color": C["muted"], "fontSize": "16px",
                    }),
                    html.Span(label, style={"flex": "1"}),
                    html.Span(badge, style={
                        "display": "inline-flex" if badge else "none",
                        "alignItems": "center", "justifyContent": "center",
                        "minWidth": "14px", "height": "14px", "padding": "0 5px",
                        "borderRadius": "999px", "background": C["red"], "color": "#FFFFFF",
                        "fontSize": "10px", "fontWeight": "500",
                    }),
                ], href=f"/{pid}", id=f"nav-{pid}", style={
                    "display": "flex", "alignItems": "center", "padding": "7px 8px",
                    "margin": "1px 8px", "color": C["muted"], "textDecoration": "none",
                    "fontSize": "12.5px", "fontWeight": "400", "borderRadius": "8px",
                })
            )
    return html.Div([
        html.Div([
            html.Div("A", style={
                "width": "24px", "height": "24px", "borderRadius": "6px",
                "background": C["blue"], "color": "#FFFFFF", "display": "flex",
                "alignItems": "center", "justifyContent": "center", "fontSize": "13px",
                "fontWeight": "500", "marginRight": "9px",
            }),
            html.Div([
                html.Div("Autoresearch", style={
                    "color": C["text"], "fontWeight": "500", "fontSize": "13px",
                }),
                html.Div("Enterprise control", style={
                    "color": C["muted"], "fontSize": "11px", "marginTop": "1px",
                }),
            ]),
        ], style={
            "height": "52px", "padding": "12px 14px", "display": "flex",
            "alignItems": "center", "borderBottom": f"0.5px solid {C['border']}",
        }),
        html.Div(nav_groups, style={"padding": "4px 0 70px"}),
        html.Div(id="sidebar-ws", style={
            "position": "absolute", "bottom": 0, "left": 0, "right": 0,
            "padding": "11px 12px", "color": C["muted"], "fontSize": "10px",
            "borderTop": f"0.5px solid {C['border']}", "background": C["sidebar"],
        }),
    ], style={
        "width": "200px", "minWidth": "200px", "background": C["sidebar"],
        "height": "100vh", "position": "fixed", "top": 0, "left": 0,
        "overflowY": "auto", "borderRight": f"0.5px solid {C['border']}",
        "zIndex": 1000, "fontFamily": "Inter, Figtree, 'Segoe UI', system-ui, sans-serif",
    })


def _topbar() -> html.Div:
    return html.Div(
        [
            html.Div(
                [
                    html.Span(id="page-title", className="topbar-title"),
                    html.Span(id="header-context", className="topbar-context"),
                ],
                className="topbar-heading",
            ),
            html.Div(
                [
                    html.Span("⌕", className="topbar-search-icon", **{"aria-hidden": "true"}),
                    dcc.Input(
                        id="topbar-search-input",
                        className="topbar-search-input",
                        placeholder="Search",
                        type="text",
                    ),
                    html.Div(
                        id="topbar-search-results",
                        className="topbar-popover topbar-search-popover",
                        role="status",
                        **{"aria-live": "polite"},
                    ),
                ],
                className="topbar-search",
                role="search",
                title="Search",
            ),
            html.Div(
                [
                    html.Button(
                        [
                            html.Span("!", className="topbar-action-icon", **{"aria-hidden": "true"}),
                            html.Span(id="topbar-notification-badge", className="topbar-notification-badge"),
                        ],
                        id="topbar-notifications",
                        className="topbar-action",
                        title="Notifications",
                        **{"aria-label": "Notifications", "aria-controls": "topbar-action-panel", "aria-haspopup": "dialog"},
                    ),
                    html.Button(
                        html.Span("▦", className="topbar-action-icon", **{"aria-hidden": "true"}),
                        id="topbar-apps",
                        className="topbar-action",
                        title="App switcher",
                        **{"aria-label": "App switcher", "aria-controls": "topbar-action-panel", "aria-haspopup": "dialog"},
                    ),
                    html.Button(
                        html.Span("⚙", className="topbar-action-icon", **{"aria-hidden": "true"}),
                        id="topbar-settings",
                        className="topbar-action",
                        title="Settings",
                        **{"aria-label": "Settings", "aria-controls": "topbar-action-panel", "aria-haspopup": "dialog"},
                    ),
                    html.Div(
                        id="topbar-action-panel",
                        className="topbar-popover topbar-action-popover",
                        role="dialog",
                        style={"display": "none"},
                        **{"aria-live": "polite"},
                    ),
                    dcc.Store(id="topbar-action-open", data={"active": ""}),
                ],
                className="topbar-actions",
            ),
            html.Div(id="header-badges", style={"display": "none"}),
        ],
        className="enterprise-topbar",
    )


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
                dcc.Dropdown(id="ws-kpi-sql-select", placeholder="Select KPI for SQL",
                             style={**DDL_S, "margin": "8px 0"}),
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

def _pg_review() -> html.Div:
    return review_page()

def _pg_chat() -> html.Div:
    scope_options = [
        {"label": "Workspace artifacts", "value": "workspace"},
        {"label": "Project / Global", "value": "project"},
    ]
    return html.Div([
        dbc.Row([
            dbc.Col(_mk_card("Real Agent Chat",
                html.Div(id="assistant-chat-status", className="assistant-terminal-note"),
                html.Div(id="assistant-chat-log", className="assistant-chat-log"),
                dbc.Row([
                    dbc.Col(dcc.Dropdown(
                        id="chat-scope",
                        options=scope_options,
                        value="workspace",
                        clearable=False,
                        style=DDL_S,
                    ), md=3),
                    dbc.Col(dcc.Checklist(
                        id="chat-advanced",
                        options=[{"label": "Use project files as context", "value": "advanced"}],
                        value=[],
                        className="assistant-checklist",
                    ), md=3),
                    dbc.Col(dcc.Input(
                        id="assistant-chat-input",
                        placeholder="Ask the dashboard agent to inspect files, explain results, or do local project work...",
                        n_submit=0,
                        style=INPUT_S,
                    ), md=4),
                    dbc.Col(_btn("Send", "assistant-chat-send"), md=2),
                ], className="g-2"),
                accent=C["green"],
            ), md=12),
        ], className="g-3"),
    ])

def _pg_assistant() -> html.Div:
    scope_options = [
        {"label": "Workspace artifacts", "value": "workspace"},
        {"label": "Project / Global", "value": "project"},
    ]
    return html.Div([
        dbc.Row([
            dbc.Col(_mk_card("Assistant Controls",
                dbc.Row([
                    dbc.Col(dcc.Dropdown(
                        id="assistant-scope",
                        options=scope_options,
                        value="workspace",
                        clearable=False,
                        style=DDL_S,
                    ), md=3),
                    dbc.Col(dcc.Checklist(
                        id="assistant-advanced",
                        options=[{"label": "Show advanced project files", "value": "advanced"}],
                        value=[],
                        className="assistant-checklist",
                    ), md=3),
                    dbc.Col(dcc.Input(
                        id="assistant-filter",
                        placeholder="Filter artifacts...",
                        debounce=True,
                        style=INPUT_S,
                    ), md=6),
                ], className="g-2"),
                accent=C["blue"]), md=12),
        ], className="g-3"),
        dbc.Row([
            dbc.Col(dbc.Card(dbc.CardBody([
                dcc.Tabs(
                    id="assistant-tabs",
                    value="browser",
                    className="assistant-tabs",
                    children=[
                        dcc.Tab(label="Browser", value="browser", children=[
                            html.Div([
                                html.Div([
                                    html.Div([
                                        html.Div("Collections", className="assistant-artifact-panel-title"),
                                    ], className="assistant-artifact-panel-header"),
                                    dcc.RadioItems(
                                        id="assistant-collection",
                                        value="all",
                                        options=[{"label": label, "value": key} for key, label in ARTIFACT_COLLECTIONS],
                                        className="assistant-artifact-collection-list",
                                    ),
                                ], className="assistant-artifact-rail"),
                                html.Div([
                                    html.Div([
                                        html.Div("Files", className="assistant-artifact-panel-title"),
                                        dcc.Dropdown(
                                            id="assistant-artifact-select",
                                            placeholder="Jump to file...",
                                            style=DDL_S,
                                        ),
                                    ], className="assistant-artifact-toolbar"),
                                    dcc.Store(id="assistant-artifact-rows", data=[]),
                                    html.Div(id="assistant-artifact-table", className="assistant-artifact-file-list"),
                                ], className="assistant-artifact-files"),
                                html.Div([
                                    html.Div([
                                        html.Div("Interpreter", className="assistant-artifact-panel-title"),
                                    ], className="assistant-artifact-panel-header"),
                                    html.Div(id="assistant-artifact-preview", className="assistant-artifact-preview-body"),
                                ], className="assistant-artifact-preview-pane"),
                            ], className="assistant-artifact-explorer assistant-tab-pane"),
                        ]),
                        dcc.Tab(label="Data Model", value="model", children=[
                            dbc.Row([
                                dbc.Col(_mk_card("Model Graph",
                                    html.Div(id="assistant-model-graph"),
                                    accent=C["purple"],
                                ), md=7),
                                dbc.Col(_mk_card("Model Evidence",
                                    html.Div(id="assistant-model-details"),
                                ), md=5),
                            ], className="g-3 assistant-tab-pane"),
                        ]),
                        dcc.Tab(label="Annotations", value="annotations", children=[
                            dbc.Row([
                                dbc.Col(_mk_card("Draft Notes",
                                    dcc.Textarea(
                                        id="assistant-annotation-text",
                                        placeholder="Draft artifact notes or decisions here...",
                                        className="assistant-textarea",
                                    ),
                                    html.Div([
                                        _btn("Save Draft", "assistant-annotation-save", color="secondary"),
                                        _btn("Promote Decision", "assistant-annotation-promote", color="success", outline=True),
                                    ], className="assistant-action-row"),
                                    html.Pre(id="assistant-annotation-output", style={**PRE_S, "maxHeight": "260px", "marginTop": "10px"}),
                                    accent=C["yellow"],
                                ), md=12),
                            ], className="g-3 assistant-tab-pane"),
                        ]),
                    ],
                ),
            ]), style=CARD_S), md=12),
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
            dbc.Col(_mk_card("Build Actions", html.Div(id="build-action-out", style={
                **PRE_S, "maxHeight": "130px", "fontSize": "0.72rem",
            }), accent=C["blue"]), md=12),
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
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    title="Autoresearch",
    update_title=None,
    suppress_callback_exceptions=True,
)

_pages = html.Div([
    html.Div(_pg_workspace(), id="page-workspace", style={"display": "none"}),
    html.Div(_pg_chat(),      id="page-chat",      style={"display": "none"}),
    html.Div(_pg_review(),    id="page-review",    style={"display": "none"}),
    html.Div(_pg_assistant(), id="page-assistant", style={"display": "none"}),
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
    dcc.Store(id="assistant-chat-store", data=[]),
    dcc.Interval(id="fast-tick", interval=FAST_MS, n_intervals=0),
    dcc.Interval(id="slow-tick", interval=SLOW_MS, n_intervals=0),
    _sidebar(),
    _topbar(),
    html.Div([
        # Top strip
        html.Div([
            html.Span(id="legacy-page-title", style={
                "color": C["text"], "fontWeight": "500", "fontSize": "14px",
                "letterSpacing": "0",
            }),
            html.Span(id="legacy-header-context", style={
                "color": C["muted"], "fontSize": "11px", "marginLeft": "12px",
                "fontFamily": "'JetBrains Mono', Consolas, monospace",
            }),
            html.Div([
                html.Span("⌕", style={"color": C["muted"], "fontSize": "14px", "marginRight": "7px"}),
                html.Span("Search", style={"color": C["faint"], "fontSize": "12px"}),
            ], style={
                "width": "200px", "height": "32px", "background": C["card2"],
                "borderRadius": "8px", "display": "flex", "alignItems": "center",
                "padding": "0 10px", "marginLeft": "auto",
                "border": f"0.5px solid {C['dim']}",
            }),
            html.Div([
                html.Button("◷", style={
                    "width": "32px", "height": "32px", "borderRadius": "8px",
                    "border": f"0.5px solid {C['border']}", "background": C["card"],
                    "color": C["muted"], "fontSize": "16px",
                }),
                html.Button("▦", style={
                    "width": "32px", "height": "32px", "borderRadius": "8px",
                    "border": f"0.5px solid {C['border']}", "background": C["card"],
                    "color": C["muted"], "fontSize": "16px",
                }),
                html.Button("⚙", style={
                    "width": "32px", "height": "32px", "borderRadius": "8px",
                    "border": f"0.5px solid {C['border']}", "background": C["card"],
                    "color": C["muted"], "fontSize": "16px",
                }),
            ], style={"display": "flex", "gap": "8px", "marginLeft": "10px"}),
            html.Div(id="legacy-header-badges", style={"display": "none"}),
        ], style={
            "display": "none", "height": "52px", "background": C["hdr"],
            "borderBottom": f"0.5px solid {C['border']}",
            "padding": "0 20px", "alignItems": "center",
        }),
        html.Div(_pages, style={"padding": "16px 20px", "overflowY": "auto"}),
    ], style={
        "marginLeft": "200px", "minHeight": "100vh", "background": C["bg"],
        "paddingTop": "52px",
        "fontFamily": "Inter, Figtree, 'Segoe UI', system-ui, sans-serif",
    }),
], style={"background": C["bg"], "color": C["text"]})


# ── Routing ───────────────────────────────────────────────────────────────────

@app.callback(
    [Output(f"page-{pid}", "style") for pid in PAGE_IDS]
    + [Output(f"nav-{pid}", "style") for pid in PAGE_IDS]
    + [Output("page-title", "children"), Output("header-context", "children")],
    [Input("url", "pathname"), Input("ws-store", "data")],
)
def _route(pathname: str, ws: str):
    page = (pathname or "/workspace").lstrip("/") or "workspace"
    if page not in PAGE_IDS:
        page = "workspace"
    styles = [{"display": "block"} if pid == page else {"display": "none"} for pid in PAGE_IDS]
    nav_styles = []
    for pid in PAGE_IDS:
        active = pid == page
        nav_styles.append({
            "display": "flex", "alignItems": "center", "padding": "7px 8px",
            "margin": "1px 8px", "textDecoration": "none", "fontSize": "12.5px",
            "fontWeight": "400", "borderRadius": "8px",
            "background": C["blue_bg"] if active else "transparent",
            "color": C["blue"] if active else C["muted"],
        })
    title  = NAV_LABELS.get(page, page.capitalize())
    ctx    = f"Workspace: {ws}" if ws else ""
    return styles + nav_styles + [title, ctx]

# ── Workspace callbacks ───────────────────────────────────────────────────────

@app.callback(
    Output("topbar-search-results", "children"),
    [Input("topbar-search-input", "value"), Input("ws-store", "data")],
)
def _topbar_search(query: str, ws: str):
    term = (query or "").strip().lower()
    if not term:
        return []

    items: list[dict[str, str]] = []
    for pid, label, group, _icon, _badge in NAV_ITEMS:
        haystack = f"{label} {group} {pid}".lower()
        if term in haystack:
            items.append({"label": label, "kind": "Page", "href": f"/{pid}"})

    for workspace in _workspaces():
        if term in workspace.lower():
            items.append({"label": workspace, "kind": "Workspace", "href": "/workspace"})

    for task in _all_tasks():
        label = task.get("name") or task.get("id") or ""
        haystack = f"{task.get('id', '')} {label} {task.get('workspace', '')}".lower()
        if term in haystack:
            items.append({"label": label, "kind": "Task", "href": "/diffs"})

    if ws:
        proof = REVIEWER_PROOF.load(ws)
        for artifact in proof.get("artifacts") or []:
            label = artifact.get("artifact", "")
            path = artifact.get("path", "")
            if term in f"{label} {path}".lower():
                items.append({"label": label, "kind": "Artifact", "href": "/review"})
        for blocker in proof.get("blockers") or []:
            if term in str(blocker).lower():
                items.append({"label": str(blocker)[:80], "kind": "Blocker", "href": "/review"})
        for row in _artifact_inventory("workspace", ws, [], limit=120):
            haystack = f"{row.get('label', '')} {row.get('kind', '')} {row.get('interpreter', '')}".lower()
            if term in haystack:
                items.append({"label": row.get("label", "")[:90], "kind": row.get("interpreter", "Artifact"), "href": "/assistant"})
                if len(items) >= 10:
                    break

    if not items:
        return html.Div("No matches", className="topbar-popover-empty")

    return html.Div(
        [
            dcc.Link(
                [
                    html.Span(item["kind"], className="topbar-result-kind"),
                    html.Span(item["label"], className="topbar-result-label"),
                ],
                href=item["href"],
                className="topbar-result-row",
            )
            for item in items[:8]
        ]
    )


@app.callback(
    Output("topbar-notification-badge", "children"),
    [Input("slow-tick", "n_intervals"), Input("ws-store", "data")],
)
def _topbar_notification_badge(_n, ws: str):
    count = 0
    if ws:
        proof = REVIEWER_PROOF.load(ws)
        count += len(proof.get("blockers") or [])
        count += len(proof.get("missing_artifacts") or [])
    count += len(_load_global_db().get("alerts") or [])
    if count <= 0:
        return ""
    return "9+" if count > 9 else str(count)


@app.callback(
    [
        Output("topbar-action-panel", "children"),
        Output("topbar-action-open", "data"),
        Output("topbar-action-panel", "style"),
    ],
    [
        Input("topbar-notifications", "n_clicks"),
        Input("topbar-apps", "n_clicks"),
        Input("topbar-settings", "n_clicks"),
    ],
    [State("ws-store", "data"), State("url", "pathname"), State("topbar-action-open", "data")],
    prevent_initial_call=True,
)
def _topbar_action_panel(_notifications, _apps, _settings, ws: str, pathname: str, open_panel: dict | str | None):
    ctx = callback_context.triggered[0]["prop_id"].split(".")[0]
    active_panel = open_panel.get("active", "") if isinstance(open_panel, dict) else (open_panel or "")
    if ctx == active_panel:
        return [], {"active": ""}, {"display": "none"}

    if ctx == "topbar-notifications":
        proof = REVIEWER_PROOF.load(ws) if ws else {}
        blockers = proof.get("blockers") or []
        missing = proof.get("missing_artifacts") or []
        rows = [
            ("Workspace", ws or "No workspace selected"),
            ("Readiness", (proof.get("status") or "missing").title() if proof else "No proof loaded"),
            ("Blockers", str(len(blockers))),
            ("Missing artifacts", str(len(missing))),
        ]
        return _topbar_panel("Notifications", rows, "/review"), {"active": ctx}, {"display": "block"}

    if ctx == "topbar-apps":
        rows = [
            ("Review", "Proof trail and readiness gates"),
            ("Workspace", "Onboarding and generated profiles"),
            ("Blockers", "KPI mapping decisions"),
            ("Build", "Medallion build controls"),
            ("Govern", "Activity, alerts, and decisions"),
        ]
        return _topbar_panel("App switcher", rows, pathname or "/review"), {"active": ctx}, {"display": "block"}

    rows = [
        ("Theme", "Enterprise light"),
        ("Refresh interval", f"{int(SLOW_MS / 1000)}s slow / {int(FAST_MS / 1000)}s fast"),
        ("Remote execution", "Requires explicit environment approval"),
        ("Workspace", ws or "Not selected"),
    ]
    return _topbar_panel("Settings", rows, "/govern"), {"active": ctx}, {"display": "block"}


def _topbar_panel(title: str, rows: list[tuple[str, str]], href: str):
    return html.Div(
        [
            html.Div(title, className="topbar-panel-title"),
            *[
                html.Div(
                    [
                        html.Span(label, className="topbar-panel-label"),
                        html.Span(value, className="topbar-panel-value"),
                    ],
                    className="topbar-panel-row",
                )
                for label, value in rows
            ],
            dcc.Link("Open related page", href=href, className="topbar-panel-link"),
        ]
    )


@app.callback(
    [
        Output("assistant-artifact-select", "options"),
        Output("assistant-artifact-table", "children"),
        Output("assistant-artifact-rows", "data"),
        Output("assistant-collection", "options"),
    ],
    [
        Input("assistant-scope", "value"),
        Input("ws-store", "data"),
        Input("assistant-advanced", "value"),
        Input("assistant-filter", "value"),
        Input("assistant-collection", "value"),
    ],
)
def _assistant_artifacts(scope: str, ws: str, advanced: list[str] | None, query: str, collection: str):
    all_rows = _artifact_inventory(scope or "workspace", ws or "", advanced)
    rows = _filter_artifact_rows(all_rows, collection, query)
    options = [{"label": f"{row['kind']} - {row['label']}", "value": row["value"]} for row in rows[:250]]
    table_rows = [
        {
            "Family": row.get("family", ""),
            "Kind": row["kind"],
            "Interpreter": row.get("interpreter", row["kind"]),
            "Path": row["label"],
            "Size": row["size"],
            "Updated": row["updated"],
        }
        for row in rows
    ]
    if table_rows:
        table = dash_table.DataTable(
            id="assistant-file-table",
            data=table_rows,
            columns=[{"name": c, "id": c} for c in ["Family", "Kind", "Interpreter", "Path", "Size", "Updated"]],
            page_size=14,
            row_selectable="single",
            selected_rows=[],
            sort_action="native",
            tooltip_data=[
                {column: {"value": str(value), "type": "text"} for column, value in row.items()}
                for row in table_rows
            ],
            tooltip_duration=None,
            style_table={"overflowX": "auto", "paddingBottom": "8px"},
            style_cell={
                **CELL,
                "fontSize": "11.5px",
                "height": "auto",
                "minWidth": "80px",
                "maxWidth": "260px",
                "overflow": "hidden",
                "textOverflow": "ellipsis",
            },
            style_cell_conditional=[
                {
                    "if": {"column_id": "Path"},
                    "minWidth": "280px",
                    "maxWidth": "520px",
                    "whiteSpace": "normal",
                    "overflowWrap": "anywhere",
                    "textOverflow": "clip",
                },
                {"if": {"column_id": "Size"}, "width": "88px"},
                {"if": {"column_id": "Updated"}, "width": "108px"},
            ],
            style_header=HCELL,
            style_data_conditional=[
                {"if": {"row_index": "odd"}, "background": C["card2"]},
            ],
        )
    else:
        table = html.Div("No artifacts found for this scope.", className="assistant-empty")
    return options, table, table_rows, _collection_options(all_rows)


@app.callback(
    Output("assistant-artifact-preview", "children"),
    [Input("assistant-artifact-select", "value"), Input("assistant-file-table", "selected_rows")],
    State("assistant-artifact-rows", "data"),
)
def _assistant_preview(rel: str, selected_rows: list[int] | None, rows: list[dict] | None):
    ctx = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if not rel and not selected_rows:
        return html.Div(
            "Select a file from the Files list or use Jump to file to open an interpreter preview.",
            className="assistant-empty",
        )
    if ctx == "assistant-file-table" and selected_rows and rows:
        idx = selected_rows[0]
        if 0 <= idx < len(rows):
            rel = rows[idx].get("Path") or rel
    return _render_artifact(rel or "")


@app.callback(
    [Output("assistant-model-graph", "children"), Output("assistant-model-details", "children")],
    [Input("assistant-tabs", "value"), Input("ws-store", "data")],
)
def _assistant_model(_tab: str, ws: str):
    return _model_graph(ws or "")


def _assistant_chat_view(history: list[dict]):
    if not history:
        return html.Div(
            "Ask about generated artifacts, reports, profiles, Markdown, data models, or safe commands.",
            className="assistant-empty",
        )
    items = []
    for item in history:
        children = [
            html.Div(item.get("role", "assistant").title(), className="assistant-chat-role"),
            dcc.Markdown(item.get("text", ""), className="assistant-chat-message"),
        ]
        artifacts = item.get("artifacts") or []
        if artifacts:
            children.append(html.Div([
                html.Div([
                    html.Div(rel, className="assistant-chat-artifact-title"),
                    html.Div(_render_artifact(rel), className="assistant-chat-artifact-preview"),
                ], className="assistant-chat-artifact")
                for rel in artifacts[:3]
            ], className="assistant-chat-artifacts"))
        if item.get("model_view"):
            children.append(html.Div(
                "Open the Data Model tab for the relationship graph, or search domain_model / relationship_contracts in Browser.",
                className="assistant-chat-hint",
            ))
        items.append(html.Div(children, className=f"assistant-chat-item assistant-chat-{item.get('role', 'assistant')}"))
    return html.Div(items)


@app.callback(Output("assistant-chat-status", "children"), Input("url", "pathname"))
def _assistant_chat_status(_pathname: str):
    if os.environ.get("AUTORESEARCH_ALLOW_DASHBOARD_LLM") == "1":
        agent = os.environ.get("AUTORESEARCH_DASHBOARD_CHAT_AGENT") or load_config().main_agent
        return f"Real agent chat enabled via {agent}, with fallback to codex/gemini/claude. Destructive, secret, git push, and remote actions require explicit approval."
    return "Real agent chat is disabled. Relaunch with AUTORESEARCH_ALLOW_DASHBOARD_LLM=1 to connect the dashboard to the configured CLI/API agent."


@app.callback(
    [
        Output("assistant-chat-store", "data"),
        Output("assistant-chat-log", "children"),
        Output("assistant-chat-input", "value"),
    ],
    [Input("assistant-chat-send", "n_clicks"), Input("assistant-chat-input", "n_submit")],
    [
        State("assistant-chat-input", "value"),
        State("assistant-chat-store", "data"),
        State("chat-scope", "value"),
        State("ws-store", "data"),
        State("chat-advanced", "value"),
    ],
    prevent_initial_call=True,
)
def _assistant_chat(_clicks, _submit, query: str, history: list[dict] | None, scope: str, ws: str, advanced: list[str] | None):
    history = list(history or [])
    question = (query or "").strip()
    if not question:
        return history, _assistant_chat_view(history), no_update
    rows = _artifact_inventory(scope or "workspace", ws or "", advanced)
    terms = [token for token in re.split(r"\W+", question.lower()) if len(token) > 2]
    scored = []
    for row in rows:
        haystack = f"{row['label']} {row['kind']}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1]["label"]))
    matches = [row for _score, row in scored[:6]]
    llm_answer = _dashboard_llm_chat(question, history, matches, ws or "", scope or "workspace")
    if llm_answer:
        answer = llm_answer
    elif matches:
        lines = ["Matching artifacts:"]
        lines.extend(f"- {row['kind']}: {row['label']}" for row in matches)
        answer = "\n".join(lines) + "\n\nInline previews are shown below for the first matching artifacts."
    elif question.lower() in {"hi", "hii", "hello", "hey"}:
        answer = (
            "Hi. I can help you inspect the dashboard workspace.\n\n"
            "Try asking things like:\n"
            "- show KPI proof packet\n"
            "- find domain model\n"
            "- show relationship contracts\n"
            "- find markdown reports\n"
            "- show CSV files\n"
            "- what can I see here?"
        )
    elif any(word in question.lower() for word in ("help", "what can", "how", "show me", "available")):
        answer = (
            "This chat can search and preview artifacts from the current Assistant scope. "
            "It can render Markdown, JSON summaries, CSV/TSV tables, SVG/images, logs, KPI mappings, "
            "proof packets, relationship contracts, profiles, and memory/wiki artifacts. "
            "Use the scope selector above to switch between workspace artifacts and project/global files."
        )
    else:
        answer = (
            "The real agent backend is not responding, so I can only use local artifact search right now. "
            "Relaunch the dashboard with `AUTORESEARCH_ALLOW_DASHBOARD_LLM=1` and a configured CLI agent "
            "to inspect files, edit code, and run approved local work from this chat."
        )
    model_terms = {"data", "model", "domain", "relationship", "lineage", "graph", "diagram"}
    history.extend([
        {"role": "user", "text": question},
        {
            "role": "assistant",
            "text": answer,
            "artifacts": [row["label"] for row in matches[:3]],
            "model_view": bool(model_terms.intersection(set(terms))),
        },
    ])
    history = history[-20:]
    return history, _assistant_chat_view(history), ""


@app.callback(
    Output("assistant-annotation-output", "children"),
    [Input("assistant-annotation-save", "n_clicks"), Input("assistant-annotation-promote", "n_clicks")],
    [
        State("assistant-annotation-text", "value"),
        State("assistant-artifact-select", "value"),
        State("ws-store", "data"),
    ],
    prevent_initial_call=True,
)
def _assistant_annotations(_save, _promote, text: str, artifact: str, ws: str):
    note = (text or "").strip()
    if not note:
        return "Write a note before saving."
    if not ws:
        return "Select a workspace before saving annotations."
    ctx = callback_context.triggered[0]["prop_id"].split(".")[0]
    now = int(time.time())
    record = {
        "created_at": now,
        "created_at_display": _epoch_fmt(now),
        "artifact": artifact or "",
        "text": note,
        "source": "dashboard_assistant",
    }
    if ctx == "assistant-annotation-save":
        out_dir = _ws(ws) / "interns" / "state" / "dashboard_assistant"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "draft_annotations.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return f"Saved draft annotation: {_safe_rel(path)}"
    out_dir = _ws(ws) / "interns" / "generated" / "memory"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "dashboard_decisions.jsonl"
    record["status"] = "promoted_decision"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return f"Promoted governed decision: {_safe_rel(path)}"


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
    Output("ws-kpi-sql-select", "options"),
    [Input("slow-tick", "n_intervals"), Input("ws-store", "data"), Input("ws-action-out", "children")],
)
def _kpi_sql_options(_n, ws, _action_out):
    if not ws:
        return []
    mapping = _ws_artifacts(ws)["mapping"]
    options = []
    for kpi in mapping.get("kpis") or []:
        kpi_id = kpi.get("kpi_id") or kpi.get("id")
        if not kpi_id:
            continue
        summary = kpi.get("name") or kpi.get("question") or kpi_id
        blocked = bool(kpi.get("blocked_features") or kpi.get("unresolved_features"))
        label = f"{kpi_id} - {str(summary)[:70]}"
        if blocked:
            label += " (blocked)"
        options.append({"label": label, "value": kpi_id, "disabled": blocked})
    return options


@app.callback(
    Output("ws-action-out", "children"),
    [Input("btn-onboard","n_clicks"), Input("btn-validate","n_clicks"),
     Input("btn-gen-kpi-sql","n_clicks")],
    [State("ws-store","data"), State("ws-kpi-sql-select", "value")],
    prevent_initial_call=True,
)
def _ws_action(ob, vb, gb, ws, kpi_id):
    if not ws:
        return "No workspace selected."
    ctx = callback_context.triggered[0]["prop_id"].split(".")[0]
    cmd = {"btn-onboard": "onboard-workspace",
           "btn-validate": "validate-workspace-artifacts",
           "btn-gen-kpi-sql": "generate-kpi-sql"}.get(ctx)
    if not cmd:
        return no_update
    extra = None
    if ctx == "btn-gen-kpi-sql":
        if not kpi_id:
            return "Select a ready KPI before generating SQL."
        extra = ["--kpi-id", kpi_id]
    r = _run_cmd(ws, cmd, extra)
    return (r.get("stdout") or "") + (r.get("stderr") or "") + (r.get("error") or "")

# Review callbacks

@app.callback(
    [Output("review-metric-status","children"), Output("review-trend-status","children"),
     Output("review-metric-graph","children"), Output("review-trend-graph","children"),
     Output("review-metric-memory","children"), Output("review-trend-memory","children"),
     Output("review-metric-trajectory","children"), Output("review-trend-trajectory","children"),
     Output("review-summary","children"), Output("review-artifacts","children"),
     Output("review-blockers","children"), Output("review-graph","children"),
     Output("review-trajectory","children"), Output("review-blocker","children")],
    [Input("slow-tick","n_intervals"), Input("ws-store","data"),
     Input("review-action-out","children")],
)
def _review_refresh(_n, ws, _action_out):
    none_msg = html.Div("Select a workspace.", style={"color": C["muted"]})
    if not ws:
        return ["-", "", "-", "", "-", "", "-", "", *([none_msg] * 6)]

    proof = REVIEWER_PROOF.load(ws)
    summary = proof.get("summary") or {}
    status = proof.get("status", "missing")
    status_color = {"ready": C["green"], "incomplete": C["orange"], "blocked": C["red"]}.get(
        status, C["muted"]
    )
    summary_body = html.Div([
        _kv("Status", status, status_color),
        _kv("Reliability", summary.get("reliability_status"),
            C["green"] if summary.get("reliability_status") == "passed" else C["orange"]),
        _kv("Workflow guard", summary.get("workflow_guard_status"),
            C["green"] if summary.get("workflow_guard_status") == "passed" else C["orange"]),
        _kv("Project score", summary.get("project_score") or "not run", C["blue"]),
        _kv("Graph", f"{summary.get('graph_nodes', 0)} nodes / {summary.get('graph_edges', 0)} edges", C["blue"]),
        _kv("Memory", f"{summary.get('memory_entries', 0)} entries / {summary.get('memory_findings', 0)} findings", C["purple"]),
        _kv("Trajectory", f"{summary.get('trajectory_events', 0)} events", C["teal"]),
        _kv("Updated", _epoch_fmt(proof.get("generated_at")), C["muted"]),
    ])
    metric_status = status.title()
    status_trend = html.Span(
        "No blockers" if status == "ready" else ("Needs attention" if status == "blocked" else "Artifacts missing"),
        style={"color": C["green"] if status == "ready" else C["red"] if status == "blocked" else C["orange"]},
    )
    metric_graph = f"{summary.get('graph_nodes', 0)}"
    graph_trend = html.Span(f"{summary.get('graph_edges', 0)} edges", style={"color": C["teal"]})
    metric_memory = f"{summary.get('memory_entries', 0)}"
    memory_trend = html.Span(
        f"{summary.get('memory_findings', 0)} findings",
        style={"color": C["green"] if summary.get("memory_findings", 0) == 0 else C["orange"]},
    )
    metric_trajectory = f"{summary.get('trajectory_events', 0)}"
    trajectory_trend = html.Span("Replayable events", style={"color": C["purple"]})

    artifact_rows = [
        {
            "Artifact": item.get("artifact", ""),
            "Status": item.get("status", ""),
            "Updated": _epoch_fmt(item.get("updated", "")),
            "Path": item.get("path", ""),
        }
        for item in proof.get("artifacts") or []
    ]
    artifacts_body = _dt(artifact_rows, ["Artifact", "Status", "Updated", "Path"], page=12) \
        if artifact_rows else html.Div("No proof artifacts.", style={"color": C["muted"]})

    blockers = proof.get("blockers") or []
    blocker_body = html.Div([
        html.Div(item, style={
            "color": C["text"], "fontSize": "0.76rem", "padding": "7px 0",
            "borderBottom": f"1px solid {C['dim']}",
        }) for item in blockers
    ], style={"maxHeight": "260px", "overflowY": "auto"}) if blockers else html.Div(
        "No blocking proof findings in the loaded artifacts.", style={"color": C["muted"]}
    )

    graph = proof.get("graph") or {}
    kind_rows = [
        {"Kind": k, "Count": v}
        for k, v in sorted((graph.get("node_kinds") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    ][:14]
    introduced = graph.get("introduced_terms") or []
    graph_body = html.Div([
        _dt(kind_rows, ["Kind", "Count"], page=8) if kind_rows else html.Div(
            "No graph built yet.", style={"color": C["muted"]}
        ),
        html.Div("Introduced terms", style={
            "color": C["muted"], "fontSize": "0.72rem", "marginTop": "10px",
            "textTransform": "uppercase", "fontWeight": "700",
        }),
        html.Div(", ".join(str(item.get("term") or item) for item in introduced[:20])
                 or "No introduced terms detected.",
                 style={"color": C["text"], "fontSize": "0.76rem", "lineHeight": "1.45"}),
    ])

    trajectory = proof.get("trajectory") or {}
    status_rows = [
        {"Status": k, "Count": v}
        for k, v in sorted((trajectory.get("status_counts") or {}).items())
    ]
    recent = [
        {
            "Type": record.get("event_type", ""),
            "Status": record.get("status", ""),
            "Summary": str(record.get("summary", ""))[:90],
        }
        for record in trajectory.get("recent") or []
    ]
    trajectory_body = html.Div([
        _dt(status_rows, ["Status", "Count"], page=5) if status_rows else html.Div(
            "No trajectory summary yet.", style={"color": C["muted"]}
        ),
        html.Div(style={"height": "10px"}),
        _dt(recent, ["Type", "Status", "Summary"], page=6) if recent else html.Div(
            "No recent trajectory events.", style={"color": C["muted"]}
        ),
    ])

    panel = proof.get("blocker") or {}
    opts = panel.get("options") or []
    blocker_panel_body = html.Div([
        _kv("Feature", panel.get("feature") or "none", C["orange"] if panel else C["muted"]),
        _kv("Status", panel.get("status") or "missing"),
        _kv("Applies to", ", ".join(panel.get("applies_to_kpis") or []) or "-"),
        html.Div(panel.get("question") or "No active blocker panel.",
                 style={"color": C["text"], "fontWeight": "600", "fontSize": "0.82rem",
                        "marginTop": "8px", "lineHeight": "1.45"}),
        html.Div([
            html.Div([
                html.Span(o.get("option_id", ""), style={
                    "color": C["blue"], "fontFamily": "monospace", "fontWeight": "700",
                    "marginRight": "8px",
                }),
                html.Span(o.get("label", ""), style={"color": C["text"], "fontSize": "0.76rem"}),
            ], style={"padding": "5px 0", "borderBottom": f"1px solid {C['dim']}"})
            for o in opts[:4]
        ], style={"marginTop": "8px"}),
    ])

    return (
        metric_status,
        status_trend,
        metric_graph,
        graph_trend,
        metric_memory,
        memory_trend,
        metric_trajectory,
        trajectory_trend,
        summary_body,
        artifacts_body,
        blocker_body,
        graph_body,
        trajectory_body,
        blocker_panel_body,
    )

@app.callback(
    Output("review-action-out","children"),
    [Input("btn-review-reliability","n_clicks"), Input("btn-review-graph","n_clicks"),
     Input("btn-review-memory","n_clicks"), Input("btn-review-project","n_clicks")],
    State("ws-store","data"), prevent_initial_call=True,
)
def _review_actions(rb, gb, mb, pb, ws):
    if not ws:
        return "No workspace selected."
    ctx = callback_context.triggered[0]["prop_id"].split(".")[0]
    if ctx == "btn-review-reliability":
        r = _run_cmd(ws, "run-reliability-suite")
    elif ctx == "btn-review-graph":
        r = _run_cmd(ws, "build-workspace-evidence-graph")
    elif ctx == "btn-review-memory":
        r = _run_cmd(ws, "validate-memory-health")
    elif ctx == "btn-review-project":
        r = _run_cmd(ws, "validate-project-harness")
    else:
        return no_update
    return (r.get("stdout") or "")[-2200:] + (r.get("stderr") or "") + (r.get("error") or "")

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
    Output("build-action-out","children"),
    Input("btn-run-build","n_clicks"),
    [State("ws-store","data"), State("build-target","value"),
     State("build-only-layer","value"), State("build-only-table","value")],
    prevent_initial_call=True,
)
def _do_run(_n, ws, target, layer, table):
    if not ws:
        return "No workspace selected."
    result = _trigger_build(ws, target or "auto", layer or "", table or "")
    if result.get("ok"):
        return f"Started build for workspaces/{ws}. PID: {result.get('pid')}"
    return f"Build failed to start: {result.get('error') or result}"

@app.callback(Output("build-action-out","children", allow_duplicate=True), Input("btn-kill-build","n_clicks"),
              State("ws-store","data"), prevent_initial_call=True)
def _do_kill(_n, ws):
    if not ws:
        return "No workspace selected."
    result = _kill_build(ws)
    if result.get("ok"):
        return f"Requested build stop for workspaces/{ws}."
    return f"Kill failed: {result.get('error') or result}"

@app.callback(Output("build-action-out","children", allow_duplicate=True), Input("btn-design","n_clicks"),
              State("ws-store","data"), prevent_initial_call=True)
def _do_design(_n, ws):
    if not ws:
        return "No workspace selected."
    result = _run_cmd(ws, "design-medallion")
    return (result.get("stdout") or "")[-2200:] + (result.get("stderr") or "") + (result.get("error") or "")

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
