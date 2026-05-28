"""Chart-type inference from KPI registry shape.

Workspace-agnostic. Inputs: the KPI's definition (business_question, metric,
cuts, filters) + the columns the generated SQL produces. Outputs: a chart
spec dict that the renderer can use directly. The picker is intentionally
small and rule-based — the user can override any field via `user_overrides`.
"""
from __future__ import annotations

import re
from typing import Any


_DATE_NAME_PATTERN = re.compile(
    r"(date|month|year|week|day|time|timestamp|created|service|invoice|filed|posted)",
    re.IGNORECASE,
)
_TOP_N_PATTERN = re.compile(r"\btop\s*(\d+)\b", re.IGNORECASE)


def _split_cuts(cuts_text: str) -> list[str]:
    """Generic cut parser. Splits on commas and 'and'; trims parenthesized notes."""
    if not cuts_text:
        return []
    parts = re.split(r"[,;]| and ", cuts_text, flags=re.IGNORECASE)
    out: list[str] = []
    for part in parts:
        cleaned = re.sub(r"\(.*?\)", "", part).strip()
        if cleaned:
            out.append(cleaned)
    return out


def _is_date_like(name: str) -> bool:
    return bool(_DATE_NAME_PATTERN.search(name or ""))


def _detect_top_n(definition: dict[str, Any]) -> int | None:
    haystack = " ".join(
        [
            str(definition.get("business_question") or ""),
            str(definition.get("name") or ""),
            str(definition.get("description") or ""),
        ]
    )
    match = _TOP_N_PATTERN.search(haystack)
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None
    return None


def infer_chart(
    *,
    definition: dict[str, Any],
    result_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Return a default chart spec for a KPI.

    Rules (in order):

    1. Any cut name matches date/time pattern → `line` over time, color by next cut.
    2. Business question contains "top N" → `ranked_bar` with `limit=N`.
    3. Single dimensional cut → `bar`.
    4. Two dimensional cuts → `grouped_bar`.
    5. No cuts → `big_number` (single-metric tile).

    Returns a plain dict — never raises. Callers store this under
    `machine_defaults` and let users override per field via `user_overrides`.
    """
    metric = str(definition.get("metric") or "").strip()
    cuts_text = str(definition.get("cuts") or "").strip()
    cuts = _split_cuts(cuts_text)
    business_question = str(definition.get("business_question") or definition.get("name") or "").strip()

    available = list(result_columns or [])
    available_lower = {c.lower(): c for c in available}

    def _resolve_column(name: str) -> str:
        if not name:
            return ""
        if name in available:
            return name
        lower = available_lower.get(name.lower())
        if lower:
            return lower
        cleaned = re.sub(r"[^A-Za-z0-9]+", "", name).lower()
        for candidate in available:
            if re.sub(r"[^A-Za-z0-9]+", "", candidate).lower() == cleaned:
                return candidate
        return name

    date_cut = next((cut for cut in cuts if _is_date_like(cut)), "")
    non_date_cuts = [cut for cut in cuts if cut != date_cut]
    top_n = _detect_top_n(definition)

    spec: dict[str, Any] = {
        "title": business_question or metric or "KPI",
        "metric": metric,
        "y_label": metric,
    }

    if date_cut:
        spec.update(
            {
                "chart_type": "line",
                "x": _resolve_column(date_cut),
                "x_label": date_cut,
                "y": "value",
                "agg": "sum",
                "color": _resolve_column(non_date_cuts[0]) if non_date_cuts else "",
            }
        )
        return spec

    if top_n and cuts:
        primary = cuts[0]
        spec.update(
            {
                "chart_type": "ranked_bar",
                "x": _resolve_column(primary),
                "x_label": primary,
                "y": "value",
                "agg": "sum",
                "limit": top_n,
                "sort": "desc",
            }
        )
        return spec

    if len(cuts) == 1:
        spec.update(
            {
                "chart_type": "bar",
                "x": _resolve_column(cuts[0]),
                "x_label": cuts[0],
                "y": "value",
                "agg": "sum",
            }
        )
        return spec

    if len(cuts) >= 2:
        spec.update(
            {
                "chart_type": "grouped_bar",
                "x": _resolve_column(cuts[0]),
                "x_label": cuts[0],
                "y": "value",
                "agg": "sum",
                "color": _resolve_column(cuts[1]),
            }
        )
        return spec

    spec.update(
        {
            "chart_type": "big_number",
            "y": "value",
            "agg": "sum",
        }
    )
    return spec


__all__ = ["infer_chart"]
