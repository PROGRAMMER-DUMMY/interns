"""Pure decision logic for cross-filtering and drill-through.

Kept free of Dash so it can be unit-tested directly. The callback in
``callbacks.py`` is a thin adapter that feeds the trigger + click payload here.
"""

from __future__ import annotations

from typing import Any, Optional

# Sentinel meaning "leave the URL unchanged" (mirrors dash.no_update at the edge).
NO_NAV = object()


def apply_interaction(trigger: Any, value: Any, crossfilter: dict,
                      page_id: str) -> tuple[Optional[dict], Any]:
    """Return (new_crossfilter_or_None, nav_target).

    ``new_crossfilter_or_None`` is None when nothing should change (PreventUpdate).
    ``nav_target`` is a path string to navigate to, or NO_NAV to stay put.
    """
    if trigger is None or not value:
        return None, NO_NAV

    cf = {k: dict(v) for k, v in (crossfilter or {}).items()}
    page_cf = dict(cf.get(page_id, {}))

    if trigger == "reset-filters":
        cf[page_id] = {}
        return cf, NO_NAV

    if isinstance(trigger, dict) and trigger.get("kind") == "cf-remove":
        page_cf.pop(trigger["field"], None)
        cf[page_id] = page_cf
        return cf, NO_NAV

    if isinstance(trigger, dict) and trigger.get("kind") == "graph":
        dim = trigger.get("dim")
        if not dim:
            return None, NO_NAV
        point = value["points"][0]
        label = point.get("label", point.get("x", point.get("y")))
        drill = trigger.get("drill")
        if drill:  # drill-through: filter the target page, then navigate to it
            tgt = dict(cf.get(drill, {}))
            tgt[dim] = label
            cf[drill] = tgt
            return cf, f"/{drill}"
        # toggle cross-filter on the current page
        if page_cf.get(dim) == label:
            page_cf.pop(dim, None)
        else:
            page_cf[dim] = label
        cf[page_id] = page_cf
        return cf, NO_NAV

    return None, NO_NAV
