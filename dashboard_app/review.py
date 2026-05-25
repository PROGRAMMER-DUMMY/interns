from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import html

from dashboard_app.design import COLORS, PRE_STYLE, button, metric_card, panel


def layout() -> html.Div:
    """Build the enterprise Review page."""

    return html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Review readiness", className="review-hero-eyebrow"),
                            html.Div(id="review-hero-title", className="review-hero-title"),
                            html.Div(id="review-hero-copy", className="review-hero-copy"),
                        ],
                        className="review-hero-main",
                    ),
                    html.Div(id="review-hero-next", className="review-hero-next"),
                ],
                className="review-hero",
            ),
            dbc.Row(
                [
                    dbc.Col(metric_card("Proof status", "review-metric-status", "review-trend-status", "", COLORS["blue"]), md=3),
                    dbc.Col(metric_card("Evidence graph", "review-metric-graph", "review-trend-graph", "", COLORS["teal"]), md=3),
                    dbc.Col(metric_card("Memory health", "review-metric-memory", "review-trend-memory", "", COLORS["orange"]), md=3),
                    dbc.Col(metric_card("Trajectory", "review-metric-trajectory", "review-trend-trajectory", "", COLORS["purple"]), md=3),
                ],
                className="g-3 review-row review-metrics-row",
            ),
            dbc.Row(
                [
                    dbc.Col(panel("Readiness Gate", html.Div(id="review-summary"), accent=COLORS["green"]), md=4),
                    dbc.Col(
                        panel(
                            "Build Missing Proof",
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Div("Recommended first", className="review-action-eyebrow"),
                                            html.Div("Reliability Suite", className="review-action-title"),
                                            html.Div("Refresh workflow guardrails and local proof checks.", className="review-action-copy"),
                                        ],
                                        className="review-action-text",
                                    ),
                                    button("Run", "btn-review-reliability", color="success"),
                                ],
                                className="review-primary-action",
                            ),
                            html.Div(
                                [
                                    button("Evidence Graph", "btn-review-graph", color="link"),
                                    button("Memory Health", "btn-review-memory", color="link"),
                                    button("Project Harness", "btn-review-project", color="link"),
                                ],
                                className="review-secondary-actions",
                            ),
                            html.Div(
                                id="review-action-out",
                                style={**PRE_STYLE, "maxHeight": "150px", "fontSize": "11.5px", "marginTop": "8px"},
                            ),
                            accent=COLORS["teal"],
                        ),
                        md=5,
                    ),
                    dbc.Col(
                        html.Div(
                            [
                                panel("Current Blocker", html.Div(id="review-blocker"), accent=COLORS["orange"]),
                                panel("Open Blockers", html.Div(id="review-blockers"), accent=COLORS["red"]),
                            ],
                            className="review-blocker-stack",
                        ),
                        md=3,
                    ),
                ],
                className="g-3 review-row",
            ),
            dbc.Row(
                [
                    dbc.Col(panel("Artifact Coverage", html.Div(id="review-artifacts")), md=12),
                ],
                className="g-3 review-row",
            ),
            dbc.Row(
                [
                    dbc.Col(panel("Evidence Graph", html.Div(id="review-graph"), accent=COLORS["blue"]), md=6),
                    dbc.Col(panel("Trajectory", html.Div(id="review-trajectory"), accent=COLORS["purple"]), md=6),
                ],
                className="g-3 review-row",
            ),
        ],
        className="review-page enterprise-page",
    )
