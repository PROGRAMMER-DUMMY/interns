from __future__ import annotations

from datetime import datetime
from typing import Any

import dash_bootstrap_components as dbc
from dash import dash_table, html


COLORS = {
    "bg": "#F5F7FA",
    "card": "#FFFFFF",
    "card2": "#F8FAFC",
    "border": "rgba(15, 23, 42, 0.15)",
    "dim": "rgba(15, 23, 42, 0.08)",
    "text": "#172033",
    "muted": "#64748B",
    "faint": "#94A3B8",
    "blue": "#185FA5",
    "blue_bg": "#E6F1FB",
    "green": "#3B6D11",
    "green_bg": "#EAF3DE",
    "orange": "#854F0B",
    "orange_bg": "#FAEEDA",
    "red": "#A32D2D",
    "red_bg": "#FCEBEB",
    "teal": "#0F766E",
    "teal_bg": "#DDF7F3",
    "purple": "#5B4E9B",
}


CARD_STYLE = {
    "background": COLORS["card"],
    "border": f"0.5px solid {COLORS['border']}",
    "borderRadius": "12px",
    "marginBottom": "14px",
    "boxShadow": "0 1px 2px rgba(15, 23, 42, 0.03)",
}

HEADER_STYLE = {
    "background": COLORS["card"],
    "borderBottom": f"0.5px solid {COLORS['dim']}",
    "borderRadius": "12px 12px 0 0",
    "padding": "12px 16px 9px",
    "display": "flex",
    "alignItems": "center",
    "gap": "8px",
}

HEADER_TITLE_STYLE = {
    "color": COLORS["text"],
    "fontWeight": "500",
    "fontSize": "12.5px",
    "letterSpacing": "0",
    "textTransform": "none",
    "margin": 0,
}

PRE_STYLE = {
    "color": COLORS["text"],
    "background": COLORS["card2"],
    "padding": "14px",
    "borderRadius": "8px",
    "fontSize": "12px",
    "lineHeight": "1.65",
    "overflowY": "auto",
    "maxHeight": "400px",
    "margin": 0,
    "whiteSpace": "pre-wrap",
    "fontFamily": "'JetBrains Mono', 'Fira Code', Consolas, monospace",
    "border": f"0.5px solid {COLORS['dim']}",
}

CELL_STYLE = {
    "background": COLORS["card"],
    "color": COLORS["text"],
    "border": f"0.5px solid {COLORS['dim']}",
    "fontSize": "12px",
    "fontFamily": "Inter, Figtree, 'Segoe UI', system-ui, sans-serif",
    "padding": "7px 10px",
    "textAlign": "left",
    "whiteSpace": "normal",
}

HEADER_CELL_STYLE = {
    "background": COLORS["card2"],
    "color": COLORS["muted"],
    "fontWeight": "500",
    "border": f"0.5px solid {COLORS['dim']}",
    "fontSize": "10px",
    "letterSpacing": "0.08em",
    "textTransform": "uppercase",
}


def panel(title: str, *children: Any, accent: str = COLORS["blue"], body_style: dict | None = None):
    return dbc.Card(
        [
            dbc.CardHeader(
                html.Span(title, style=HEADER_TITLE_STYLE),
                style={**HEADER_STYLE, "boxShadow": f"inset 3px 0 0 {accent}"},
            ),
            dbc.CardBody(list(children), style={"padding": "14px 16px", **(body_style or {})}),
        ],
        className="enterprise-card",
        style=CARD_STYLE,
    )


def metric_card(label: str, value_id: str, trend_id: str, icon: str, accent: str):
    return dbc.Card(
        dbc.CardBody(
            [
                html.Div(
                    [
                        html.Span(
                            icon,
                            className="review-metric-icon",
                            style={"color": accent},
                        ),
                        html.Span(label, className="review-metric-label"),
                    ],
                    className="review-metric-heading",
                ),
                html.Div(id=value_id, className="review-metric-value"),
                html.Div(id=trend_id, className="review-metric-trend"),
            ],
            style={"padding": "14px 16px"},
        ),
        className="enterprise-card review-metric-card",
        style=CARD_STYLE,
    )


def kv(label: str, value: Any, color: str = ""):
    return html.Div(
        [
            html.Span(label, className="kv-label"),
            html.Span(
                str(value if value is not None else "-"),
                className="kv-value",
                style={"color": color or COLORS["text"]},
            ),
        ],
        className="kv-row",
    )


def badge(text: str, color: str):
    bg = {
        COLORS["blue"]: COLORS["blue_bg"],
        COLORS["green"]: COLORS["green_bg"],
        COLORS["orange"]: COLORS["orange_bg"],
        COLORS["red"]: COLORS["red_bg"],
        COLORS["teal"]: COLORS["teal_bg"],
    }.get(color, COLORS["card2"])
    return html.Span(text, className="status-pill", style={"background": bg, "color": color})


def button(label: str, bid: str, color: str = "primary", outline: bool = False, **kwargs):
    return dbc.Button(label, id=bid, color=color, size="sm", outline=outline, className="me-1 mb-1", **kwargs)


def data_table(data: list[dict], columns: list[str], page: int = 10):
    return dash_table.DataTable(
        data=data,
        columns=[{"name": column, "id": column} for column in columns],
        page_size=page,
        sort_action="native",
        style_table={"overflowX": "auto"},
        style_cell=CELL_STYLE,
        style_header=HEADER_CELL_STYLE,
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "background": COLORS["card2"]},
        ],
    )


def epoch_fmt(timestamp: int | str) -> str:
    if not timestamp:
        return ""
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%m-%d %H:%M")
    except (OSError, ValueError, TypeError):
        return str(timestamp)[:16]

