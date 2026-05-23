from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import html

from dashboard_app.design import COLORS, PRE_STYLE, button, metric_card, panel


def layout() -> html.Div:
    """Build the enterprise Review page.

    The page is intentionally evidence-first. Refresh commands are separated
    from readiness and artifact coverage so review state remains easy to audit.
    """

    return html.Div(
        [
            dbc.Row(
                [
                    dbc.Col(metric_card("Proof status", "review-metric-status", "review-trend-status", "□", COLORS["blue"]), md=3),
                    dbc.Col(metric_card("Evidence graph", "review-metric-graph", "review-trend-graph", "◇", COLORS["teal"]), md=3),
                    dbc.Col(metric_card("Memory health", "review-metric-memory", "review-trend-memory", "◷", COLORS["orange"]), md=3),
                    dbc.Col(metric_card("Trajectory", "review-metric-trajectory", "review-trend-trajectory", "↻", COLORS["purple"]), md=3),
                ],
                className="g-2 review-row",
            ),
            dbc.Row(
                [
                    dbc.Col(panel("Readiness Gate", html.Div(id="review-summary"), accent=COLORS["green"]), md=4),
                    dbc.Col(
                        panel(
                            "Refresh Proof Artifacts",
                            html.Div(
                                [
                                    button("Reliability Suite", "btn-review-reliability", color="success"),
                                    button("Evidence Graph", "btn-review-graph", color="primary", outline=True),
                                    button("Memory Health", "btn-review-memory", color="secondary", outline=True),
                                    button("Project Harness", "btn-review-project", color="warning", outline=True),
                                ],
                                className="review-action-group",
                            ),
                            html.Div(
                                id="review-action-out",
                                style={**PRE_STYLE, "maxHeight": "150px", "fontSize": "11.5px", "marginTop": "8px"},
                            ),
                            accent=COLORS["teal"],
                        ),
                        md=4,
                    ),
                    dbc.Col(panel("Current Blocker", html.Div(id="review-blocker"), accent=COLORS["orange"]), md=4),
                ],
                className="g-2 review-row",
            ),
            dbc.Row(
                [
                    dbc.Col(panel("Artifact Coverage", html.Div(id="review-artifacts")), md=7),
                    dbc.Col(panel("Open Blockers", html.Div(id="review-blockers"), accent=COLORS["red"]), md=5),
                ],
                className="g-2 review-row",
            ),
            dbc.Row(
                [
                    dbc.Col(panel("Evidence Graph", html.Div(id="review-graph"), accent=COLORS["blue"]), md=6),
                    dbc.Col(panel("Trajectory", html.Div(id="review-trajectory"), accent=COLORS["purple"]), md=6),
                ],
                className="g-2 review-row",
            ),
        ],
        className="review-page enterprise-page",
    )
