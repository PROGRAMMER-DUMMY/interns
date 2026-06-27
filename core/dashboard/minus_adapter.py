"""Generic adapter: interns workspace -> a vendored-MinusAnalyst project + data.

Turns ANY workspace's DQ-certified conformed model into the inputs the vendored
MinusAnalyst app (vendor/minus) consumes:
  - data/conformed.parquet   the certified-clean conformed star (columnar)
  - project.yaml             datasource + table + reusable measures
  - config/dashboards/*.yaml curated pages (KPI overview + detail)

Workspace-agnostic: measures come from the conformed model's fact numerics, and
which cuts are charted (and in what order they drill) is decided by a generic
importance score -- how much each dimension explains the measure -- not by
cardinality alone or any favoured column name. Nothing healthcare- or
workspace-specific is hardcoded. See ``core.dashboard.importance``.

Publishing is DQ-gated: if `dq.certify` fails, we do not (re)write the data, so a
running dashboard keeps the last-good snapshot.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from core.dashboard.model.aggregate import resolve_column
from core.dashboard.model.conformed import ConformedModel, build_conformed_model
from core.dashboard.model.cuts import build_kpi_model, headline_agg, measure_fmt, metric_goal
from core.dashboard.model.dq import certify
from core.dashboard.model.layers import list_gold_kpis, read_gold
from core.dashboard.importance import rank_dimensions, ranked_names
from core.storage.workspace_layout import WorkspaceLayout

_DATE_ISH = ("date", "_at", "_dt")


def minus_root(layout: WorkspaceLayout) -> Path:
    return layout.state_dir / "minus"


def _atomic_write(path: Path, text: str) -> None:
    """Write a spec file atomically (temp + os.replace) so a concurrently
    serving dashboard's config watcher never reads a half-written file -- the
    cause of transient 'config error' flashes when a refresh overlaps a serve."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _slug(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name)).strip("_").lower()
    return s or "m"


def _fact_table(model: ConformedModel) -> str:
    """Logical name of the single conformed (pre-joined) table the app reads.

    Derived from the workspace's inferred fact entity -- never a hardcoded
    domain word like "claims" -- so the generated project + widgets reference a
    name that reflects the ACTUAL workspace (e.g. ``transactions``) and the
    dashboard wiring stays workspace-agnostic.
    """
    return _slug(model.fact) or "data"


def _fact_pk(model: ConformedModel) -> str | None:
    for c in model.frame.columns:
        if c.lower() == f"{model.fact.rstrip('s')}id" or c.lower().endswith("id"):
            return c
    return None


# ---------------------------------------------------------------------------
# Measures
# ---------------------------------------------------------------------------


def _measure_specs(model: ConformedModel) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    table = _fact_table(model)
    for m in model.measures.values():
        slug = _slug(m.name)
        if m.agg == "count":
            specs.append({"name": slug, "label": m.name, "agg": "count",
                          "table": table, "fmt": m.fmt or "int"})
        else:
            specs.append({"name": slug, "label": m.name, "agg": m.agg,
                          "field": f"{table}.{m.column}",
                          "fmt": m.fmt or measure_fmt(f"{m.agg}({m.column})", m.column, "")})
    # NOTE: no domain-derived ratio measures here (was Collection Rate / Days in
    # A/R, keyed off "paid"/"amount"/"ar_days" substrings -- favoured RCM-shaped
    # workspaces). A composed/ratio measure must be declared in the workspace's
    # KPI registry / semantic contract so it applies to that workspace on merit,
    # not because a column name happened to match.
    return specs


def _measure_field_columns(measures: list[dict[str, Any]]) -> set[str]:
    """Source columns consumed by measures (excluded from dimension lists)."""
    cols: set[str] = set()
    for m in measures:
        fld = m.get("field")
        if fld and "." in fld:
            cols.add(fld.split(".", 1)[1])
    return cols


# ---------------------------------------------------------------------------
# Dimensions + chart selection
# ---------------------------------------------------------------------------


# A categorical dimension is only a readable chart up to this many categories;
# higher-cardinality columns (procedure/diagnosis codes, ids) make poor overview
# charts and are excluded (still reachable via Detail / drill).
_MAX_DIM_CARDINALITY = 50
# A dimension whose single most-common value covers more than this fraction of
# rows is effectively constant -- charting it yields a degenerate/empty figure
# (e.g. a SUFFIX column that is 97.7% blank). Excluded from overview charts.
_MAX_DIM_CONCENTRATION = 0.9


def _top_value_share(model: ConformedModel, column: str) -> float:
    """Fraction of rows held by the single most-common value of ``column``."""
    height = model.frame.height
    if not height:
        return 0.0
    try:
        vc = model.frame.get_column(column).value_counts(sort=True)
        return int(vc[vc.columns[-1]][0]) / height
    except Exception:
        return 0.0


_UUID_VAL_RE = re.compile(r"^[0-9a-f]{6,}-[0-9a-f-]{6,}", re.IGNORECASE)
_HEXID_VAL_RE = re.compile(r"^[0-9a-f]{12,}$", re.IGNORECASE)


def _is_identifier_dim(gold, col: str) -> bool:
    """True when a column's values are raw identifiers (UUIDs / long hex codes) --
    not a human-readable category. Such a column makes a useless chart axis (a
    stakeholder can't read a patient UUID). Generic; samples the values."""
    try:
        import polars as _pl
        vals = (gold.get_column(col).cast(_pl.Utf8, strict=False)
                .drop_nulls().to_list()[:50])
    except Exception:
        return False
    if len(vals) < 3:
        return False
    idlike = sum(1 for v in vals
                 if _UUID_VAL_RE.match(str(v)) or _HEXID_VAL_RE.match(str(v)))
    return idlike >= max(3, int(0.6 * len(vals)))


def _anonymize_identifier_column(gold, col: str, measure_col: str):
    """Replace an identifier column's UUIDs with ranked, PII-safe labels
    ("Patient 1", "Patient 2", ... by descending measure) so a "top entities"
    chart is readable without exposing raw IDs. Generic: the label stem comes
    from the column name."""
    try:
        import polars as _pl
        stem = _human(col).rstrip("s") or "Item"
        order = (gold.select([col, measure_col]).drop_nulls()
                 .group_by(col).agg(_pl.col(measure_col).sum().alias("__r"))
                 .sort("__r", descending=True))
        ids = order.get_column(col).to_list()
        mapping = {v: f"{stem} {i + 1}" for i, v in enumerate(ids)}
        return gold.with_columns(
            _pl.col(col).cast(_pl.Utf8, strict=False)
            .replace(mapping, default=_pl.col(col).cast(_pl.Utf8, strict=False))
            .alias(col))
    except Exception:
        return gold


def _measure_concentration(model: ConformedModel, dim: str, measure_col: str) -> float:
    """Share of the measure total held by the single biggest category of ``dim``.
    High -> one category dominates, so a grouped/absolute bar squishes the rest
    into invisibility (use a heatmap or share view instead). Generic."""
    try:
        import polars as _pl
        g = (model.frame.select([dim, measure_col]).drop_nulls()
             .group_by(dim).agg(_pl.col(measure_col).sum().alias("__m")))
        total = g.get_column("__m").sum()
        if not total or total <= 0:
            return 0.0
        return float(g.get_column("__m").max()) / float(total)
    except Exception:
        return 0.0


def _display_dimensions(model: ConformedModel) -> list[tuple[str, int]]:
    """(dimension, distinct) worth charting, ordered low-cardinality first,
    skipping raw dates (keep 'month'), constants, near-constant (dominant-value)
    columns, and high-cardinality columns."""
    import polars as _pl
    out: list[tuple[str, int]] = []
    for d in model.dimensions:
        dl = d.lower()
        if dl != "month" and any(t in dl for t in _DATE_ISH):
            continue
        # Continuous (Float) columns are MEASURES, not categories: a money/decimal
        # column makes a meaningless donut/bar dimension even at low cardinality
        # (e.g. BASE_ENCOUNTER_COST = 85.55, 142.58, ...). Exclude floats; keep Int
        # (a genuine low-cardinality code/rating can be a real category).
        try:
            if model.frame.schema.get(d) in (_pl.Float32, _pl.Float64):
                continue
        except Exception:
            pass
        distinct = model.frame.get_column(d).n_unique()
        if distinct <= 1 or (dl != "month" and distinct > _MAX_DIM_CARDINALITY):
            continue
        # Concentration guard: skip a dimension dominated by one value (>90%) --
        # it can't make a meaningful chart and renders near-empty.
        if dl != "month" and _top_value_share(model, d) > _MAX_DIM_CONCENTRATION:
            continue
        out.append((d, distinct))
    out.sort(key=lambda t: (t[0].lower() != "month", t[1]))  # month first, then low-card
    return out


def _justify_widget_widths(widgets: list[dict[str, Any]]) -> None:
    """Reassign each widget's `width` so the group tiles the 12-col grid with no
    ragged rows (in place). A single full-width (12) widget keeps full width.
    Uses the shared layout planner, so the result passes the screener's
    layout-balance check."""
    from core.dashboard.layout_planner import plan_widths

    n = len(widgets)
    if n == 0:
        return
    if n == 1:
        widgets[0]["width"] = 12
        return
    for w, width in zip(widgets, plan_widths(n)):
        w["width"] = width


# Tiered scorecard: only the few most impactful single-number KPIs are text cards
# (Miller's Law -- a dozen bare numbers overwhelm); the rest become charts. Keep
# all-as-cards for a small scorecard, where a few tiles read fine.
_TIER_MIN_KPIS = 6          # below this, show every KPI as a card (small dashboard)
_TIER_TEXT_MAX = 5          # never more than 5 headline numbers
_TIER_TEXT_MIN = 3


def _select_hero_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The 3-5 most impactful KPIs as prominent headline cards (by impact score),
    in original importance order. Used when the chart tier is conformed-overview
    charts, so the headline stays a few key numbers."""
    n = min(_TIER_TEXT_MAX, max(_TIER_TEXT_MIN, round(len(cards) * 0.4)))
    top = {id(c) for c in sorted(cards, key=lambda c: -c.get("_hero_score", 0))[:n]}
    out: list[dict[str, Any]] = []
    for c in cards:
        c.pop("_hero_score", None)
        if id(c) in top:
            opts = dict(c.get("options") or {})
            opts["emphasis"] = "hero"
            c["options"] = opts
            c["height"] = 168
            out.append(c)
    return out


def _tier_kpis(cards: list[dict[str, Any]],
               charts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]],
                                                       list[dict[str, Any]]]:
    """Split KPIs into a Tier-1 headline row (3-5 most impactful single-number
    KPIs as text cards) and a Tier-2 chart grid (every other KPI as its own
    interactive chart). Returns (text_cards, chart_widgets). Each KPI shows in
    exactly one tier, so nothing is dropped: a KPI chosen for text shows its card;
    everyone else shows their chart (falling back to the card if it has none)."""
    chart_by_kpi: dict[str, dict[str, Any]] = {}
    for w in charts:
        chart_by_kpi.setdefault(w.get("_kpi_id"), w)

    def _kid(card) -> str:
        return card["id"].replace("k_", "", 1)

    # Small scorecard: keep every KPI as a card (+ all charts), uniform.
    if len(cards) < _TIER_MIN_KPIS:
        for c in cards:
            c.pop("_hero_score", None)
        for w in charts:
            w.pop("_kpi_id", None)
        _justify_widget_widths(cards)
        return cards, charts

    # Pick the headline text cards: highest impact score (clean number + trend;
    # ranking/distribution KPIs score low and fall to the chart tier where they
    # belong). Aim ~40% of KPIs, clamped to [3, 5].
    text_n = min(_TIER_TEXT_MAX, max(_TIER_TEXT_MIN, round(len(cards) * 0.4)))
    by_score = sorted(cards, key=lambda c: -c.get("_hero_score", 0))
    text_kids = {_kid(c) for c in by_score[:text_n]}

    hero_cards: list[dict[str, Any]] = []      # prominent headline numbers (<=5)
    compact_cards: list[dict[str, Any]] = []   # irreducible scalars w/ no chart
    chart_widgets: list[dict[str, Any]] = []
    for c in cards:                       # preserve original (importance) order
        kid = _kid(c)
        c.pop("_hero_score", None)
        ch = chart_by_kpi.get(kid)
        if kid in text_kids:
            opts = dict(c.get("options") or {})
            opts["emphasis"] = "hero"
            c["options"] = opts
            c["height"] = 168
            hero_cards.append(c)
        elif ch is not None:              # chartable KPI -> chart tier
            ch["height"] = 340
            chart_widgets.append(ch)
        else:                             # scalar with no chart -> small tile
            c.pop("subtitle", None)
            c["height"] = 118
            compact_cards.append(c)
    for w in chart_widgets:
        w.pop("_kpi_id", None)
    # Justify the hero row and (any) compact-scalar row independently, then stack:
    # prominent headlines first, small leftover numbers next, charts handled by the
    # caller. Keeps the headline tier to <=5 while never dropping a KPI.
    _justify_widget_widths(hero_cards)
    _justify_widget_widths(compact_cards)
    return hero_cards + compact_cards, chart_widgets


_TEMPORAL_BUCKET_NAMES = {"month": "month", "quarter": "quarter", "year": "year"}
# Grain ladder, coarse -> fine, for the drillable trend.
_TEMPORAL_LADDER = ("year", "quarter", "month", "day")
_TREND_MIN_BLOCKS = 2          # a grain needs >=2 periods to be a useful start
_TREND_MAX_START_BLOCKS = 40   # too many blocks at the start = unreadable; go coarser


def _temporal_drill(model, table: str) -> tuple[str, list[str]]:
    """Pick (start_grain, drill_path) for the lead trend from the grains actually
    present in the conformed frame. Start at the coarsest grain that has a
    readable number of blocks (>=2, not hundreds) so each period is a clickable
    bar; the drill path is that grain plus every finer grain present. Falls back
    to 'month' (always derived) when nothing else qualifies. Generic."""
    frame = getattr(model, "frame", None)
    if frame is None:
        return "month", ["month"]
    present = [g for g in _TEMPORAL_LADDER if g in frame.columns]
    if not present:
        return "month", ["month"]
    counts = {}
    for g in present:
        try:
            counts[g] = frame.get_column(g).n_unique()
        except Exception:
            counts[g] = 0
    # Coarsest grain with >=2 blocks; prefer one that isn't absurdly dense.
    start = None
    for g in present:                       # coarse -> fine
        if counts.get(g, 0) >= _TREND_MIN_BLOCKS:
            start = g
            if counts[g] <= _TREND_MAX_START_BLOCKS:
                break
    if start is None:
        return "month", ["month"]
    drill = present[present.index(start):]  # start grain + every finer grain
    return start, drill


def _trend_column(gold) -> tuple[str | None, str]:
    """A (column, period) to drive a KPI card's period-over-period trend, or
    (None, '') when the KPI has no time grain. Prefers a real Date/Datetime
    column; falls back to a temporal BUCKET column (year/quarter/month) the KPI
    grouped by. Period follows the bucket. Generic -- no domain name list."""
    import polars as _pl
    # 1. A column whose NAME is a known grain (year/quarter/month) -> that grain
    # is the period (so a year-bucketed gold says "vs prev year", not "quarter").
    for c in gold.columns:
        if c.lower() in _TEMPORAL_BUCKET_NAMES:
            return c, _TEMPORAL_BUCKET_NAMES[c.lower()]
    # 2. A generic Date/Datetime column -> INFER the period from the typical gap
    # between sorted distinct values, so the label matches the real grain.
    for c in gold.columns:
        try:
            if gold.schema.get(c) in (_pl.Date, _pl.Datetime):
                return c, _infer_period(gold.get_column(c))
        except Exception:
            pass
    return None, ""


def _infer_period(col) -> str:
    """Guess a period label (year/quarter/month) from the median gap between
    distinct dates in a temporal column. Generic -- no name assumptions."""
    try:
        vals = sorted(set(col.drop_nulls().to_list()))
        if len(vals) < 2:
            return "month"
        gaps = [(b - a).days for a, b in zip(vals, vals[1:]) if (b - a).days > 0]
        if not gaps:
            return "month"
        g = sorted(gaps)[len(gaps) // 2]      # median gap in days
        if g >= 300:
            return "year"
        if g >= 80:
            return "quarter"
        return "month"
    except Exception:
        return "month"


def _human(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name)).replace("_", " ").strip().title()


def _through_note(data_through) -> str:
    """A short 'reporting window' note for a page description, when the trailing
    period looks incomplete (empty string otherwise)."""
    if data_through is None:
        return ""
    try:
        label = data_through.strftime("%b %Y")
    except Exception:
        label = str(data_through)
    return (f" Complete data runs through {label}; the trailing period has far "
            "fewer records and is shown dashed/flagged on trends as incomplete.")


# ---------------------------------------------------------------------------
# Workspace-defined KPIs (gold-backed): surface the ACTUAL KPIs, not just
# generic measures. Each gold result becomes its own table + measure + a tile
# on a dedicated "KPIs" page, showing the validated answer.
# ---------------------------------------------------------------------------


_NAME_STOP = set(
    "what is the for of across and to a an by in on with above over under per "
    "years year age amount each their les value count total number".split())


def _distinct_keywords(titles: dict[str, str]) -> dict[str, str]:
    """A keyword unique to each KPI's business question, to disambiguate cards
    that share a measure label (e.g. two 'Paid Amount' KPIs -> Medicare/Commercial)."""
    ordered: dict[str, list[str]] = {}
    for kid, t in titles.items():
        seen: list[str] = []
        for w in re.findall(r"[A-Za-z]{4,}", str(t).lower()):
            if w not in _NAME_STOP and w not in seen:
                seen.append(w)
        ordered[kid] = seen
    freq: dict[str, int] = {}
    for seen in ordered.values():
        for w in set(seen):
            freq[w] = freq.get(w, 0) + 1
    return {kid: next((w.title() for w in seen if freq.get(w) == 1), "")
            for kid, seen in ordered.items()}


# Generic descriptive-column names a dimension table uses for its human label.
# No domain words -- these are universal "what is this row called" columns.
_DIM_NAME_COLUMNS = ("name", "description", "label", "title", "display_name")


def _build_fk_name_maps(layout) -> dict[str, dict[str, str]]:
    """Map each foreign-key column -> {fk_value: dimension_name}, from PROVEN
    relationships whose parent table has a descriptive column. Lets the dashboard
    replace a UUID/code FK with its readable name. Generic; empty on any missing
    evidence so it never breaks generation."""
    import json as _json

    out: dict[str, dict[str, str]] = {}
    try:
        from core.dashboard.model.layers import list_bronze_tables, read_bronze
        from core.medallion.design_naming import logical_entity_from_path

        contracts_path = (layout.contracts_dir / "relationship_contracts.json")
        if not contracts_path.exists():
            return out
        contracts = _json.loads(contracts_path.read_text(encoding="utf-8"))
        bronze_tables = list(list_bronze_tables(layout))
    except Exception:
        return out

    def _bronze_for(entity: str):
        # Match the bronze table for a logical entity. Bronze names vary across
        # medallion variants: "<entity>__<source>", the plural CSV stem
        # ("payers"), or the singular. Match on the normalized stem.
        e = entity.rstrip("s").lower()
        for bt in bronze_tables:
            stem = bt.split("__", 1)[0].rstrip("s").lower()
            if stem == e:
                return bt
        return entity
    for r in contracts.get("relationships") or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("state")) not in ("proven_data_model", "user_confirmed"):
            continue
        try:
            if float(r.get("confidence") or 0) < 0.9:
                continue
        except (TypeError, ValueError):
            continue
        fk_col = str(r.get("left_column") or "")
        parent_key = str(r.get("right_column") or "")
        parent_entity = logical_entity_from_path(str(r.get("right_dataset") or ""))
        if not fk_col or not parent_key or not parent_entity:
            continue
        try:
            dim = read_bronze(layout, _bronze_for(parent_entity))
        except Exception:
            dim = None
        if dim is None:
            continue
        name_col = next((c for c in dim.columns
                         if c.lower() in _DIM_NAME_COLUMNS), None)
        key_col = next((c for c in dim.columns if c == parent_key), None) \
            or next((c for c in dim.columns if c.lower() == parent_key.lower()), None)
        if not name_col or not key_col:
            continue
        try:
            mapping = {
                str(k): str(v)
                for k, v in zip(dim.get_column(key_col).to_list(),
                                dim.get_column(name_col).to_list())
                if k is not None and v is not None
            }
        except Exception:
            mapping = {}
        if mapping:
            out[fk_col] = mapping
    return out


def _resolve_fk_names(gold, fk_name_maps: dict[str, dict[str, str]]):
    """Replace any FK-coded column in a gold frame with its dimension name (so a
    payer UUID becomes 'Medicare'). Only columns that are an exact FK match and
    whose values look coded (no overlap with names) are remapped. No-op when no
    map applies."""
    import polars as _pl

    if not fk_name_maps:
        return gold
    for col in gold.columns:
        mapping = fk_name_maps.get(col)
        if not mapping:
            continue
        try:
            gold = gold.with_columns(
                _pl.col(col).cast(_pl.Utf8, strict=False)
                .replace(mapping, default=_pl.col(col).cast(_pl.Utf8, strict=False))
                .alias(col)
            )
        except Exception:
            # Older polars: fall back to a map_elements lookup.
            try:
                gold = gold.with_columns(
                    _pl.col(col).cast(_pl.Utf8, strict=False)
                    .map_elements(lambda v: mapping.get(str(v), v),
                                  return_dtype=_pl.Utf8).alias(col))
            except Exception:
                pass
    return gold


def _kpi_overview_charts(model: ConformedModel, fact_table: str,
                         fact_measures: list[dict[str, Any]]
                         ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the KPIs page's CHART tier on the conformed fact (not per-KPI gold),
    so every chart cross-filters the others + the slicers and drills in place --
    real Power-BI-style interactivity, which isolated gold tables can't give.

    Returns (charts, slicers). Charts: a drillable trend + the primary measure by
    its most decisive dimensions + a composition donut. Slicers: the dimensions
    those charts share. Generic; empty when the fact has no usable dims/measures.
    """
    dims = [(d, n) for d, n in _display_dimensions(model)
            if d not in _measure_field_columns(fact_measures)]
    cat_names = [d for d, _ in dims if d != "month"]
    if not cat_names and not any(d == "month" for d, _ in dims):
        return [], []

    def _mcol(m):
        f = m.get("field") or ""
        return f.split(".", 1)[1] if "." in f else f

    sums = [m for m in fact_measures if m.get("agg") == "sum"]
    primary_m = None
    if sums and cat_names:
        primary_m = max(sums, key=lambda m: max(
            [s for _, s in rank_dimensions(model.frame, cat_names, measure=_mcol(m),
                                           is_share=(m.get("fmt") == "percent"))] or [0]))
    primary_m = primary_m or (sums[0] if sums else (fact_measures[0] if fact_measures else None))
    if primary_m is None:
        return [], []
    primary = primary_m["name"]
    plabel = primary_m["label"]
    pcol = _mcol(primary_m)
    pshare = primary_m.get("fmt") == "percent"
    # Only chart READABLE dimensions: drop identifier columns (UUIDs) and coded
    # columns whose values are mostly bare numbers (e.g. SNOMED CODE 702927004) --
    # a stakeholder can't read them and they make poor axes.
    cat_names = [c for c in cat_names
                 if not _is_identifier_dim(model.frame, c)
                 and not _is_coded_dim(model.frame, c)]
    ranked = ranked_names(model.frame, cat_names, measure=pcol, is_share=pshare)

    charts: list[dict[str, Any]] = []
    # 1. Drillable trend (year -> quarter -> month -> day).
    start, ladder = _temporal_drill(model, fact_table)
    if start in model.frame.columns:
        w = {"id": "ko_trend", "type": "line", "measure": primary,
             "dimension": f"{fact_table}.{start}",
             "title": f"{plabel} over {_human(start)}", "height": 340}
        if len(ladder) > 1:
            w["drill_path"] = ladder
        charts.append(w)
    # 2-3. Primary by the two most decisive dimensions (drillable, cross-filtering).
    for i, cut in enumerate(ranked[:2]):
        n = dict(dims).get(cut, 0)
        long_lbl = _is_long_label_dim(model.frame, cut)
        wtype = "hbar" if (n > 8 or long_lbl) else "bar"
        rest = [c for c in ranked if c != cut][:2]
        charts.append({
            "id": f"ko_by_{_slug(cut)}", "type": wtype, "measure": primary,
            "dimension": f"{fact_table}.{cut}",
            "title": f"{plabel} by {_human(cut)}", "height": 340,
            "limit": 12 if wtype == "hbar" else None,
            "drill_path": [cut] + rest if rest else None})
    # 4. Composition donut on a low-cardinality dimension (record share).
    low = [c for c in ranked if 2 <= dict(dims).get(c, 99) <= 6]
    if low:
        charts.append({"id": "ko_donut", "type": "donut", "measure": "record_count",
                       "dimension": f"{fact_table}.{low[-1]}",
                       "title": f"Records by {_human(low[-1])}", "height": 340})
    # Drop None-valued keys (clean YAML).
    for w in charts:
        for k in [k for k, v in list(w.items()) if v is None]:
            w.pop(k)
    # Size for READABILITY: 2 charts per row (width 6) so each is large, not a
    # cramped 4-across sliver; a trailing odd chart spans full width. Taller, too.
    for i, w in enumerate(charts):
        last_odd = (i == len(charts) - 1) and (len(charts) % 2 == 1)
        w["width"] = 12 if last_odd else 6
        w["height"] = 360
    # Slicers: dimensions the charts share, low-cardinality, most-decisive first.
    slicers = [{"id": f"kf_{_slug(c)}", "field": f"{fact_table}.{c}",
                "label": _human(c), "type": "multi"}
               for c in ranked if 2 <= dict(dims).get(c, 99) <= 40][:4]
    return charts, slicers


_CODE_VAL_RE = re.compile(
    r"^(?:\d[\d.\-]{2,}"            # bare numeric code: 702927004, 12.3-4
    r"|[A-Za-z]{2,8}[\-_]?\d{2,}"   # prefix+digits code: PAYOR4707, ICD10, ORG_12
    r")$")


def _is_coded_dim(frame, col: str) -> bool:
    """True when a column's values are mostly CODES (numeric like SNOMED/ICD/ZIP,
    or prefix+number like PAYOR4707) -- meaningless as a chart axis to a
    stakeholder. Sampled; generic, no column-name assumptions."""
    try:
        import polars as _pl
        vals = (frame.get_column(col).cast(_pl.Utf8, strict=False)
                .drop_nulls().to_list()[:50])
    except Exception:
        return False
    if len(vals) < 3:
        return False
    coded = sum(1 for v in vals if _CODE_VAL_RE.match(str(v).strip()))
    return coded >= max(3, int(0.6 * len(vals)))


def _is_long_label_dim(frame, col: str) -> bool:
    """True when a column's category labels are long (procedure/diagnosis names) --
    better as a horizontal bar so labels read left-to-right."""
    try:
        vals = frame.get_column(col).cast(str, strict=False).drop_nulls().to_list()[:20]
        return bool(vals) and (sum(len(v) for v in vals) / len(vals)) > 16
    except Exception:
        return False


def _kpi_artifacts(model: ConformedModel, fact_table: str,
                   fact_measures: list[dict[str, Any]], data_through=None):
    """Return (tables, measures, page, exports) for every gold KPI.

    exports maps table-name -> gold frame (written to parquet by generate()).
    ``data_through`` is informational (for the page note); no rows are dropped --
    the trailing incomplete period is marked on trend charts, not removed, so the
    KPI totals keep matching the validated gold results.

    ``fact_table`` / ``fact_measures`` describe the conformed row-grain table; a
    KPI's hero chart is re-rooted onto it (so every slicer can filter it) when that
    provably reproduces the gold result -- see ``_reroot_hero_to_fact``.
    """
    layout = model.layout
    tables: list[dict[str, Any]] = []
    measures: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []     # ALL KPI cards together (one scorecard row)
    charts: list[dict[str, Any]] = []    # one lead chart per KPI, beside the row
    exports: dict[str, pl.DataFrame] = {}

    # FK -> name lookup maps, built once from proven relationships: a cut that is
    # a foreign key (e.g. encounters.PAYER = a UUID) is replaced with the parent
    # dimension's descriptive NAME so charts read "Medicare", not the UUID.
    fk_name_maps = _build_fk_name_maps(layout)

    infos = []  # (kpi_id, km, gold)
    for kpi_id in list_gold_kpis(layout):
        gold = read_gold(layout, kpi_id)
        if gold is None or gold.height == 0:
            continue
        gold = _resolve_fk_names(gold, fk_name_maps)
        infos.append((kpi_id, build_kpi_model(layout, kpi_id, gold), gold))

    # Distinct, business-meaningful card names: card_label, disambiguated with a
    # keyword from the question when two KPIs share a label (Paid Amount -> ... Medicare).
    kws = _distinct_keywords({kid: km.title for kid, km, _ in infos})
    label_freq: dict[str, int] = {}
    for _, km, _g in infos:
        label_freq[km.card_label] = label_freq.get(km.card_label, 0) + 1

    n = max(1, len(infos))
    # Justified 12-column layout: each card/hero gets a span so EVERY row fills
    # the grid -- no ragged trailing gaps for any KPI count (the old
    # `max(3, 12//n)` only tiled cleanly when n divided 12: 3/4/6/12). Cards and
    # their heroes share the same per-position width so heroes stay column-aligned
    # under their card. Generic across workspaces; verified by the screener's
    # layout-balance check.
    from core.dashboard.layout_planner import plan_widths
    card_widths = plan_widths(n)
    for idx, (kpi_id, km, gold) in enumerate(infos):
        span = card_widths[idx]
        tname = kpi_id
        mslug = _slug(f"{kpi_id}_{km.measure}")
        is_share = km.kind == "share"
        # fmt + headline aggregation derive from the metric's MEANING (count vs
        # money vs average vs share), never a flat currency/sum default. The old
        # `percent else currency` + `agg:"sum"` was fit to RCM's all-money KPIs
        # and silently mislabelled counts as `$` and summed per-group averages.
        fmt = measure_fmt(km.metric, km.measure, km.y_format)
        agg = headline_agg(km.metric, km.measure, km.y_format)
        # goal drives SEMANTIC delta/target color: for a lower-is-better metric
        # (cost, denial, readmission, aging) a down-trend is GOOD (green), so the
        # renderer colors by good/bad, not raw direction.
        goal = metric_goal(km.metric, km.measure, km.title)
        exports[tname] = gold
        tables.append({"name": tname, "source": "conformed_src",
                       "file": f"{tname}.parquet", "label": km.card_label})
        measures.append({"name": mslug, "label": km.card_label, "agg": agg,
                         "field": f"{tname}.{km.measure}", "fmt": fmt, "goal": goal})

        name = km.card_label
        if label_freq.get(km.card_label, 0) > 1 and kws.get(kpi_id):
            name = f"{km.card_label} - {kws[kpi_id]}"
        question = _card_subtitle(km.title)

        # A summed share = 100% (meaningless headline) -> show the largest
        # segment's share instead (max). The card shows the BUSINESS label
        # ("% Over 24 Hours"); "Largest Segment" stays only as the internal
        # measure label, not the cryptic card title.
        if is_share:
            card_measure = _slug(f"{kpi_id}_top")
            measures.append({"name": card_measure, "label": "Largest Segment",
                             "agg": "max", "field": f"{tname}.{km.measure}",
                             "fmt": "percent"})
            card_title = name
        else:
            card_measure = mslug
            card_title = name

        card = {"id": f"k_{tname}", "type": "kpi", "measure": card_measure,
                "title": card_title, "subtitle": question, "width": span, "height": 150}
        # Period-over-period trend (▲/▼ % vs prior). Generic temporal detection,
        # not an RCM-shaped name list: any Date/Datetime column, OR a temporal
        # BUCKET column (year/quarter/month) the KPI grouped by -- so a KPI whose
        # gold is bucketed by year still shows a trend. Period granularity follows
        # the bucket (year-bucket -> compare by year).
        datecol, period = _trend_column(gold)
        if datecol:
            card["compare"] = f"{tname}.{datecol}"
            card["compare_period"] = period
        # Hero-worthiness: a clean single-number story with a trend makes the best
        # headline tile; a top-N ranking is really a chart, not a hero number.
        is_rank = bool(re.search(r"\btop\b|\bmost\b|\bhighest\b|\bwhich\b",
                                 (km.title or "").lower()))
        card["_hero_score"] = ((2 if datecol else 0)
                               + (1 if not is_rank and not is_share else 0)
                               + (-1 if is_rank else 0))
        cards.append(card)

        # ONE compact analytics view (no per-KPI tabs): under each KPI card sits
        # a single readable, drillable hero chart, aligned in the same column.
        # Depth comes from in-place drill-down, breadth from the shared slicers --
        # so the whole scorecard reads at a glance and nothing scrolls.
        panels = _kpi_panel_widgets(tname, mslug, km.card_label, km.measure,
                                    gold, km.panels or [])
        if not panels:
            panels = _kpi_breakdown_charts(tname, mslug, km.card_label, gold,
                                           km.cuts, km.measure, is_share)
        hero = _pick_hero(panels)
        # An anonymous-identifier axis (e.g. "top patients" by patient UUID) is
        # unreadable + PII. Relabel the categories to ranked, PII-safe labels
        # ("Patient 1, 2, ...") so the chart stays useful (the heavy-utilizer
        # distribution) without exposing raw IDs.
        if hero and hero.get("dimension"):
            _idcol = hero["dimension"].split(".")[-1]
            if _is_identifier_dim(gold, _idcol):
                gold = _anonymize_identifier_column(gold, _idcol, km.measure)
                exports[tname] = gold
        if hero:
            hero["width"] = span          # aligns under its card (n-across)
            hero["height"] = 420
            hero.pop("tab", None)         # untabbed -> joins the single grid
            if hero["type"] in ("bar", "hbar"):
                xcol = hero["dimension"].split(".")[-1]
                # Rank the other cuts by how much they explain THIS KPI's measure,
                # so the colour split and the drill order surface the most
                # decisive dimension first -- generic, not "first cut that fits".
                other = [c for c in km.cuts
                         if c != xcol and c.lower() != "month" and c in gold.columns]
                ranked = ranked_names(gold, other, measure=km.measure, is_share=is_share)
                # Enrich a single-series hero with the most-important low-cardinality
                # second cut (a colour split) -- more signal, no x overlap.
                if not hero.get("breakdown"):
                    for c in ranked:
                        if 2 <= gold.get_column(c).n_unique() <= 6:
                            hero["breakdown"] = f"{tname}.{c}"
                            break
                bd = (hero.get("breakdown") or "").split(".")[-1]
                rest = [c for c in ranked if c != bd]   # most-important first
                if rest:
                    hero["drill_path"] = [xcol] + rest[:2]   # depth up to 3 levels
            # Re-root onto the row-grain fact when it provably reproduces gold, so
            # every page slicer can filter the chart (gold-bound otherwise). The
            # KPI's intrinsic scope (incl. ranges like age>50) comes from its own
            # SQL, carried as overridable base_filters.
            sql_scope = _kpi_sql_scope(layout, kpi_id, list(model.frame.columns))
            rerooted = _reroot_hero_to_fact(hero, km, gold, fact_table,
                                            model.frame, fact_measures, sql_scope,
                                            project_measures=measures)
            if rerooted is not None:
                hero = rerooted
                # A re-rooted temporal line is now on the conformed fact (which has
                # the full year/quarter/month/day ladder) -- make it DRILLABLE like
                # the Analysis trend: click a period to drill into it.
                if hero.get("type") == "line":
                    xcol = hero["dimension"].split(".")[-1]
                    if xcol in _TEMPORAL_LADDER:
                        start, dpath = _temporal_drill(model, fact_table)
                        # Re-root the line to the chosen start grain + ladder.
                        hero["dimension"] = f"{fact_table}.{start}"
                        if len(dpath) > 1:
                            hero["drill_path"] = dpath
                        base = (hero.get("title") or "").split(" over ")[0].strip()
                        hero["title"] = f"{base} over {_human(start)}"
            hero["_kpi_id"] = kpi_id      # used to keep only hero KPIs' charts
            charts.append(hero)
        else:
            # Scalar KPI (no breakdown to chart): synthesize a drillable TREND on
            # the conformed fact (the measure over time) so it can move to the
            # chart tier instead of crowding the headline row. Only when a fact
            # measure provably reproduces the KPI's total.
            tr = _synth_scalar_trend(km, gold, fact_table, model,
                                     fact_measures, measures)
            if tr is not None:
                tr["_kpi_id"] = kpi_id
                charts.append(tr)

    if not cards:
        return tables, measures, None, exports

    # Logical/global slicers: a dimension shared by >=2 KPI gold tables (and low
    # cardinality) becomes a page-level filter. engine.applicable_filters rebinds
    # it to each KPI's own column, so e.g. one Gender slicer filters every
    # gender-bearing KPI at once -- a "decisive" cut across the scorecard --
    # while KPIs lacking that dimension simply ignore it.
    col_tables: dict[str, list] = {}
    for _kid, km2, g in infos:
        for c in g.columns:
            if c == km2.measure or c.lower().endswith("id") or c.lower() == "month":
                continue
            col_tables.setdefault(c, []).append(g)
    kpi_filters = []
    for col in sorted(col_tables):
        golds = col_tables[col]
        if len(golds) < 2:
            continue
        if max(g.get_column(col).n_unique() for g in golds) <= 40:
            kpi_filters.append({"id": f"kf_{_slug(col)}", "field": col,
                                "label": _human(col), "type": "multi"})
    # Tiered scorecard (executive-dashboard convention, Miller's Law). For a large
    # scorecard: lead with the 3-5 most impactful KPIs as headline numbers, then a
    # tier of CONFORMED-fact charts that genuinely drill + cross-filter each other
    # and the slicers (isolated per-KPI gold charts can't -- different tables/
    # grains). For a small scorecard (RCM's 3): keep every KPI as a card + its
    # chart, which already reads fine.
    overview_charts, overview_slicers = _kpi_overview_charts(
        model, fact_table, fact_measures)
    if len(cards) >= _TIER_MIN_KPIS and overview_charts:
        for w in charts:
            w.pop("_kpi_id", None)
        hero_cards = _select_hero_cards(cards)   # reads + strips _hero_score
        _justify_widget_widths(hero_cards)
        # overview charts already carry 2-per-row widths (don't re-justify).
        cards, charts = hero_cards, overview_charts
        page_filters = overview_slicers
        desc = ("Click any bar/period to cross-filter and drill every chart; "
                "'↑ up' resets." + _through_note(data_through))
    else:
        cards, charts = _tier_kpis(cards, charts)   # justifies card groups
        _justify_widget_widths(charts)
        page_filters = kpi_filters[:4]
        desc = ("Each KPI shows its headline number with a chart beneath it. "
                "Use the filters to slice every KPI at once; click any bar to "
                "drill into it, then '↑ up' to go back." + _through_note(data_through))
    page = {
        "id": "kpis", "title": "KPIs", "order": 5,
        "description": desc,
        "filters": page_filters,
        "widgets": cards + charts,
    }
    return tables, measures, page, exports


def _kpi_sql_scope(layout: WorkspaceLayout, kpi_id: str, fact_columns: list[str]):
    """The KPI's intrinsic scope parsed from its generated SQL, or None when it
    can't be safely/fully captured (then the chart stays gold-bound)."""
    from core.dashboard.kpi_scope import scope_from_sql

    sql_path = layout.solutions_dir / f"{kpi_id}.sql"
    if not sql_path.exists():
        return None
    try:
        return scope_from_sql(sql_path.read_text(encoding="utf-8"), fact_columns)
    except Exception:
        return None


def _ci_col(name: str, cols: list[str]) -> str | None:
    """Case-insensitive column lookup ('payorid' -> 'PayorID'), or None."""
    if name in cols:
        return name
    low = name.lower()
    return next((c for c in cols if c.lower() == low), None)


def _apply_scope_filter(frame: pl.DataFrame, scope: dict[str, Any]) -> pl.DataFrame:
    """Filter ``frame`` by a scope of equality + range predicates (the same forms
    carried as ``base_filters`` and understood by the query engine)."""
    for col, val in scope.items():
        if isinstance(val, dict):
            if val.get("from") is not None:
                frame = frame.filter(pl.col(col) >= val["from"])
            if val.get("to") is not None:
                frame = frame.filter(pl.col(col) <= val["to"])
            if val.get("gt") is not None:
                frame = frame.filter(pl.col(col) > val["gt"])
            if val.get("lt") is not None:
                frame = frame.filter(pl.col(col) < val["lt"])
        else:
            frame = frame.filter(pl.col(col) == val)
    return frame


def _synth_scalar_trend(km, gold, fact_table, model, fact_measures,
                        project_measures) -> dict[str, Any] | None:
    """A drillable TREND line for a scalar KPI (no chartable breakdown), sourced
    from the conformed fact: the KPI's measure over the temporal grain ladder.
    Returns the widget, or None when no fact measure reproduces the KPI total (so
    the KPI stays a headline card rather than showing a fabricated trend)."""
    import polars as _pl
    frame = getattr(model, "frame", None)
    if frame is None:
        return None
    fcols = list(frame.columns)
    start, dpath = _temporal_drill(model, fact_table)
    if start not in fcols:
        return None
    try:
        gtotal = round(float(gold.get_column(km.measure).sum()), 2)
    except Exception:
        return None
    if not gtotal:
        return None

    def _rel_ok(a: float, b: float) -> bool:
        return abs(a - b) <= max(2.0, 0.01 * abs(b))

    measure_name = None
    # 1. count / count-distinct KPI -> count the underlying column on the fact.
    cnt = _count_measure_for_kpi(km, fcols)
    if cnt is not None:
        col, distinct = cnt
        try:
            tot = float(frame.get_column(col).n_unique() if distinct
                        else frame.get_column(col).drop_nulls().len())
        except Exception:
            tot = None
        if tot is not None and _rel_ok(tot, gtotal):
            measure_name = f"{fact_table}_{_slug(km.kpi_id)}_cnt"
            if not any(m.get("name") == measure_name for m in project_measures or []):
                project_measures.append({
                    "name": measure_name, "label": km.card_label,
                    "agg": "count_distinct" if distinct else "count",
                    "field": f"{fact_table}.{col}",
                    "fmt": measure_fmt(km.metric, km.measure, km.y_format)})
    # 2. additive sum KPI -> a matching sum measure already on the fact.
    if measure_name is None:
        for fm in fact_measures or []:
            if fm.get("agg") != "sum" or not fm.get("field"):
                continue
            src = _ci_col(fm["field"].split(".")[-1], fcols)
            if src is None:
                continue
            try:
                tot = float(frame.get_column(src).sum())
            except Exception:
                continue
            if _rel_ok(tot, gtotal):
                measure_name = fm["name"]
                break
    if measure_name is None:
        return None
    w = {"id": f"t_{km.kpi_id}", "type": "line", "measure": measure_name,
         "dimension": f"{fact_table}.{start}",
         "title": f"{km.card_label} over {_human(start)}", "height": 340}
    if len(dpath) > 1:
        w["drill_path"] = dpath
    return w


def _count_measure_for_kpi(km, fcols: list[str]) -> tuple[str, bool] | None:
    """For a count/count-distinct KPI, the (fact_column, is_distinct) to count so
    it can be reproduced on the row-grain fact. None for non-count metrics or when
    the column isn't on the fact. count(*) -> count any present id-ish column."""
    metric = (km.metric or "").lower().strip()
    m = re.match(r"\s*count\s*\(\s*(distinct\s+)?(.+?)\s*\)", metric)
    if not m:
        return None
    distinct = bool(m.group(1))
    arg = m.group(2).strip()
    if arg in ("*", "1"):
        # count rows -> count any non-null column present on the fact.
        col = _ci_col("Id", fcols) or (fcols[0] if fcols else None)
        return (col, False) if col else None
    col = _ci_col(arg, fcols)
    return (col, distinct) if col else None


def _reroot_hero_to_fact(hero, km, gold, fact_table, fact_frame,
                         fact_measures, sql_scope=None,
                         project_measures=None) -> dict[str, Any] | None:
    """Re-source a KPI's gold-bound hero chart onto the row-grain conformed table
    so EVERY page slicer (gender, line of business, ...) can filter it -- but only
    when doing so PROVABLY reproduces the validated gold result.

    The KPI's intrinsic scope (e.g. ``LineOfBusiness = 'Commercial'``) lives only
    in its SQL, not in any structured field. We recover the equality part of that
    scope generically as the gold columns that are constant across every gold row,
    then verify: recomputing the measure from conformed under that scope must match
    gold at the chart's grain (within rounding). If it matches we rebind the chart
    to conformed and carry the scope as overridable ``base_filters`` (so the KPI's
    meaning is the default view, yet the user can re-scope it). If it does NOT match
    -- a range scope like ``age > 50``, a precomputed share, an unmapped measure --
    we return None and the chart stays gold-bound, untouched. Never changes the KPI.
    """
    if hero.get("type") not in ("bar", "hbar", "line") or not hero.get("dimension"):
        return None
    fcols = list(fact_frame.columns)
    gcols = list(gold.columns)
    x_name = hero["dimension"].split(".")[-1]
    xf = _ci_col(x_name, fcols)
    xg = _ci_col(x_name, gcols)
    if xf is None or xg is None:
        return None

    # Scope = the KPI's intrinsic WHERE. Prefer the authoritative scope parsed
    # from the KPI's own SQL (captures ranges like age>50, not just equalities);
    # fall back to constant gold columns when SQL scope is unavailable.
    if sql_scope is not None:
        scope = {(_ci_col(k, fcols) or k): v for k, v in sql_scope.items()}
        if any(_ci_col(k, fcols) is None for k in sql_scope):
            return None                # a scope column isn't in the row grain
    else:
        chart_dims = {x_name.lower()}
        if hero.get("breakdown"):
            chart_dims.add(hero["breakdown"].split(".")[-1].lower())
        scope = {}
        for c in gcols:
            if c == km.measure or c.lower() in chart_dims:
                continue
            uniq = gold.get_column(c).drop_nulls().unique()
            if uniq.len() == 1:
                fc = _ci_col(c, fcols)
                if fc is not None:
                    scope[fc] = uniq.item()

    # Gold reference at the chart's x grain: {x_value: measure_value}.
    gref = gold.group_by(xg).agg(pl.col(km.measure).sum().alias("__m"))
    gmap = {r[xg]: round(float(r["__m"]), 2) for r in gref.iter_rows(named=True)
            if r["__m"] is not None}
    if not gmap:
        return None

    sub = _apply_scope_filter(fact_frame, scope)

    # A count / count-distinct KPI (not additive, so no SUM measure reproduces it)
    # -- reproduce it directly on the fact by counting the underlying column. This
    # lets a temporal count KPI (encounters per year) re-root onto conformed and
    # become DRILLABLE. Registers a count measure on the fact and points at it.
    cnt = _count_measure_for_kpi(km, fcols)
    if cnt is not None and fact_measures is not None:
        col, distinct = cnt
        agg = (pl.col(col).n_unique() if distinct else pl.col(col).count())
        rec = sub.group_by(xf).agg(agg.cast(pl.Float64).round(2).alias("__m"))
        rmap = {r[xf]: round(float(r["__m"]), 2) for r in rec.iter_rows(named=True)
                if r["__m"] is not None}
        # Counts are re-derived from the conformed (cleaned, joined) layer, which
        # is the dashboard's accepted CHART source -- it can differ from the gold
        # count by a hair (a dropped/deduped row). Accept a small RELATIVE match
        # per period (<=1% and <=2 absolute) so the drillable trend is allowed; the
        # headline CARD still shows the exact gold number (it reads gold, not this).
        def _close(a: float, b: float) -> bool:
            return abs(a - b) <= max(2.0, 0.01 * abs(b))
        if gmap and all(_close(rmap.get(k, float("inf")), v)
                        for k, v in gmap.items()):
            mname = f"{fact_table}_{_slug(km.kpi_id)}_cnt"
            if not any(m.get("name") == mname for m in project_measures or []):
                (project_measures if project_measures is not None else []).append({
                    "name": mname, "label": km.card_label,
                    "agg": "count_distinct" if distinct else "count",
                    "field": f"{fact_table}.{col}",
                    "fmt": measure_fmt(km.metric, km.measure, km.y_format),
                })
            new = dict(hero)
            new["measure"] = mname
            new.pop("measures", None)
            new["dimension"] = f"{fact_table}.{xf}"
            if scope:
                new["base_filters"] = {f"{fact_table}.{k}": v for k, v in scope.items()}
            if hero.get("breakdown"):
                bc = _ci_col(hero["breakdown"].split(".")[-1], fcols)
                new["breakdown"] = f"{fact_table}.{bc}" if bc else None
                if not bc:
                    new.pop("breakdown", None)
            return new

    # Try each additive sum measure on the fact; the first that reproduces gold wins.
    for fm in fact_measures:
        if fm.get("agg") != "sum" or not fm.get("field"):
            continue
        src = _ci_col(fm["field"].split(".")[-1], fcols)
        if src is None:
            continue
        rec = sub.group_by(xf).agg(pl.col(src).sum().round(2).alias("__m"))
        rmap = {r[xf]: round(float(r["__m"]), 2) for r in rec.iter_rows(named=True)
                if r["__m"] is not None}
        if all(abs(rmap.get(k, float("inf")) - v) <= 0.01 for k, v in gmap.items()):
            new = dict(hero)
            new["measure"] = fm["name"]
            new.pop("measures", None)
            new["dimension"] = f"{fact_table}.{xf}"
            if scope:
                new["base_filters"] = {f"{fact_table}.{k}": v for k, v in scope.items()}
            # Gold is the KPI's top-N result, so its x-cardinality is N: keep the
            # chart top-N (re-ranked live from conformed, so re-scoping recomputes it).
            if hero["type"] in ("bar", "hbar"):
                new["limit"] = gold.get_column(xg).n_unique()
            if hero.get("breakdown"):
                bc = _ci_col(hero["breakdown"].split(".")[-1], fcols)
                new["breakdown"] = f"{fact_table}.{bc}" if bc else None
                if not bc:
                    new.pop("breakdown", None)
            if hero.get("drill_path"):
                dp = [_ci_col(c, fcols) for c in hero["drill_path"]]
                new["drill_path"] = [c for c in dp if c]
            return new
    return None


def _pick_hero(panels: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The single chart that best headlines a KPI in the compact view -- chosen
    by the KPI's INTENT, not a fixed type order:

    - A temporal KPI's story IS its trend, so a line over time wins (and it
      carries a cut as a second series, e.g. by gender), even though it isn't
      drillable -- the trend is the point.
    - Otherwise a readable, DRILLABLE categorical bar (so 'click to drill'
      works and breadth comes from drilling, not a second near-identical chart).
    - Never a label-crammed heatmap as the headline.
    """
    if not panels:
        return None
    line = next((w for w in panels if w["type"] == "line"), None)
    if line:
        return line
    for wtype in ("hbar", "bar"):
        for w in panels:
            if w["type"] == wtype:
                return w
    for w in panels:
        if w["type"] != "heatmap":
            return w
    return panels[0]


def _kpi_breakdown_charts(tname, mslug, label, gold, cuts,
                          measure=None, is_share=False) -> list[dict[str, Any]]:
    """Up to 2 charts per KPI: the measure by its MOST IMPORTANT cuts (ranked by
    how much each explains the measure, not registry order), diversified
    (month -> line, one donut, else bar/hbar), 2-up grid."""
    out: list[dict[str, Any]] = []
    donut_budget = 1
    cats = [c for c in cuts if c in gold.columns and c.lower() != "month"]
    ranked = ranked_names(gold, cats, measure=measure, is_share=is_share)
    # Keep a leading month cut (its story is the trend) ahead of the ranked cats.
    months = [c for c in cuts if c in gold.columns and c.lower() == "month"]
    ordered = months + [c for c in ranked if c not in months]
    for cut in ordered[:2]:
        if cut not in gold.columns:
            continue
        distinct = gold.get_column(cut).n_unique()
        # Long category labels (procedure/diagnosis descriptions) are unreadable
        # as rotated x-ticks on a VERTICAL bar -- they clip/overlap. A horizontal
        # bar reads them naturally left-to-right. Detect from the actual values.
        try:
            _vals = gold.get_column(cut).cast(str, strict=False).drop_nulls().to_list()
            long_labels = bool(_vals) and (
                sum(len(v) for v in _vals[:20]) / min(len(_vals), 20)) > 16
        except Exception:
            long_labels = False
        common = {"id": f"c_{tname}_{_slug(cut)}", "measure": mslug,
                  "dimension": f"{tname}.{cut}",
                  "title": f"{label} by {_human(cut)}", "height": 320, "width": 6}
        if cut.lower() == "month":
            out.append({**common, "type": "line"})
        elif distinct <= 5 and donut_budget > 0 and not long_labels:
            out.append({**common, "type": "donut"})
            donut_budget -= 1
        elif distinct > 12 or long_labels:
            # Horizontal bar for high-cardinality OR long-label categories.
            out.append({**common, "type": "hbar", "limit": 12})
        else:
            out.append({**common, "type": "bar"})
    return out


# interns chart_type -> MinusAnalyst widget type. The interns recommendation
# engine already chose rich panels per KPI (multi-series, breakdowns, heatmaps);
# we render THOSE so each KPI is shown across its cuts, not minimally.
_CHART_TYPE_MAP = {
    "line": "line", "stacked_area": "area",
    "bar": "bar", "grouped_bar": "bar", "stacked_bar_percent": "bar",
    "ranked_bar": "hbar", "donut": "donut", "pie": "pie",
    "heatmap": "heatmap", "scatter": "scatter",
}


def _card_subtitle(title: str, *, max_len: int = 64) -> str:
    """A concise one-line card subtitle from the full business question. Keeps the
    first clause (up to a comma) when that already conveys the gist, else a clean
    truncation. The vendor shows the FULL question on hover, so nothing is lost.
    Generic -- no domain assumptions."""
    t = (title or "").strip()
    if len(t) <= max_len:
        return t
    # Prefer cutting at the first clause boundary if it lands in a readable range.
    head = t.split(",", 1)[0].strip()
    if 16 <= len(head) <= max_len:
        return head
    return t[: max_len - 1].rstrip() + "…"


def _clean_panel_title(raw: str, measure_col: str, label: str, x: str) -> str:
    if not raw:
        return f"{label} by {_human(x)}"
    phrase = _human(measure_col)  # e.g. "Sum Paidamount"
    if phrase and label and phrase.lower() in raw.lower():
        return re.sub(re.escape(phrase), label, raw, flags=re.IGNORECASE)
    return raw


def _kpi_panel_widgets(tname, mslug, label, measure_col, gold, panels) -> list[dict[str, Any]]:
    """Render the interns-recommended panels for a KPI as MinusAnalyst widgets on
    its gold table -- multi-series lines, grouped bars, heatmaps, etc. (rich,
    showing the metric across its cuts), instead of a couple of minimal charts."""
    cols = list(gold.columns)
    out: list[dict[str, Any]] = []
    seen: set = set()
    for i, panel in enumerate(panels[:8]):
        ct = str(panel.get("chart_type") or "").lower()
        if ct in ("big_number", ""):
            continue
        x = resolve_column(panel.get("x"), cols)
        if x is None:
            continue
        color = resolve_column(panel.get("color"), cols)
        # Dedup on (x, color) so a 2-categorical interaction panel (e.g. a
        # heatmap of dept x age_band) survives alongside a plain bar on dept.
        key = (x, color or "")
        if key in seen:
            continue
        seen.add(key)
        wtype = _CHART_TYPE_MAP.get(ct, "bar")
        # A vertical bar with LONG category labels (procedure/diagnosis names)
        # clips/overlaps its x-ticks. Switch a plain bar to a horizontal bar when
        # the category values are long, so labels read left-to-right. Generic.
        if wtype == "bar" and not color:
            try:
                _v = gold.get_column(x).cast(str, strict=False).drop_nulls().to_list()
                if _v and (sum(len(s) for s in _v[:20]) / min(len(_v), 20)) > 16:
                    wtype = "hbar"
                    panel = {**panel, "limit": panel.get("limit") or 12}
            except Exception:
                pass
        w = {"id": f"p_{tname}_{i}", "type": wtype, "measure": mslug,
             "dimension": f"{tname}.{x}",
             "title": _clean_panel_title(panel.get("title"), measure_col, label, x),
             "width": 6, "height": 320}
        if color and color != x and wtype in ("bar", "line", "area", "heatmap"):
            w["breakdown"] = f"{tname}.{color}"
        if ct == "ranked_bar" or panel.get("limit"):
            w["limit"] = int(panel.get("limit") or 12)
        out.append(w)
    return out


# ---------------------------------------------------------------------------
# Project + dashboards
# ---------------------------------------------------------------------------


def build_minus_project(model: ConformedModel) -> tuple[dict, list[dict], dict[str, pl.DataFrame]]:
    measures = _measure_specs(model)
    pk = _fact_pk(model)
    table = _fact_table(model)
    kpi_tables, kpi_measures, kpi_page, kpi_exports = _kpi_artifacts(
        model, table, measures, data_through=model.data_through)
    project = {
        "name": f"{model.layout.project_root.name}",
        "datasources": [{"name": "conformed_src", "type": "parquet", "path": "data"}],
        "tables": [{"name": table, "source": "conformed_src",
                    "file": "conformed.parquet", "primary_key": pk,
                    "label": _human(model.fact) or "Records"}]
                  + kpi_tables,
        "measures": measures + kpi_measures,
        "theme": {"name": "claude"},
        "agent": {"enabled": False},
        "dashboards_dir": "config/dashboards",
    }
    # Last complete reporting period (ISO) -- trend renderers mark anything beyond
    # it as an incomplete trailing period (dashed + flagged), without dropping data.
    if model.data_through is not None:
        project["data_through"] = str(model.data_through)

    mfields = _measure_field_columns(measures)
    dims = [(d, dist) for d, dist in _display_dimensions(model) if d not in mfields]
    analysis = _analysis_page(model, measures, dims)
    # Two sections only: KPIs (the defined-KPI scorecard) + Analysis (silver-layer
    # exploration with dense, multi-cut charts). No separate Overview/Detail.
    pages = [kpi_page, analysis] if kpi_page else [analysis]
    return project, pages, kpi_exports


def _analysis_page(model: ConformedModel, measures, dims) -> dict[str, Any]:
    """Build the Analysis page: complex, multi-cut charts on the conformed (silver)
    model, in three tabs (Trends / Breakdowns / Detail) so nothing scrolls.

    Each chart carries several cuts: a combo (value bars + rate line + target),
    small multiples (a trend faceted by a category), a stacked bar (two cuts), a
    grouped bar, one donut, and conditional-format detail tables.
    """
    table = _fact_table(model)
    distinct = dict(dims)
    cat_names = [d for d, _ in dims if d != "month"]

    def _measure_col(m):
        f = m.get("field") if m else None
        return f.split(".", 1)[1] if f and "." in f else None

    def _spread(m):  # peak importance of a measure across the available cuts
        col = _measure_col(m)
        scores = [s for _, s in rank_dimensions(model.frame, cat_names, measure=col,
                                                is_share=(m.get("fmt") == "percent"))]
        return max(scores) if scores else 0.0

    sums = [m for m in measures if m.get("agg") == "sum"]
    # Primary = the measure the data MOVES on most across cuts (highest spread),
    # not a favoured column name. Falls back to first sum, then any measure.
    if sums and cat_names:
        primary_m = max(sums, key=_spread)
    else:
        primary_m = sums[0] if sums else (measures[0] if measures else None)
    primary = primary_m["name"] if primary_m else "record_count"
    plabel = primary_m["label"] if primary_m else "Value"
    primary_col = _measure_col(primary_m)
    primary_share = bool(primary_m and primary_m.get("fmt") == "percent")
    # A generic "rate" line for the combo: any percentage measure other than the
    # primary (e.g. a registry-declared ratio), if the workspace has one.
    rate_m = next((m for m in measures
                   if m.get("fmt") == "percent" and m["name"] != primary), None)
    svc_date = next((c for c in model.frame.columns
                     if c.lower() in ("servicedate", "service_date")), None)

    def kpi_card(m, tab):
        w = {"id": f"a_k_{m['name']}", "type": "kpi", "measure": m["name"],
             "width": 3, "height": 132, "tab": tab}
        if svc_date:
            w["compare"] = f"{table}.{svc_date}"
            w["compare_period"] = "quarter"
        return w

    # KPI strip: primary first, then the rest in declared order (no hardcoded names).
    row = ([primary_m] if primary_m else []) + \
          [m for m in measures if m is not primary_m]
    row = row[:4]

    temporal = "month" if any(d == "month" for d, _ in dims) else None
    # Temporal DRILL ladder for the lead trend: start at the coarsest grain that
    # has enough blocks to read (so each period is a big, clickable bar behind the
    # line), and drill finer on click -- year -> quarter -> month -> day. Only
    # grains actually present with >=2 distinct values are used.
    trend_start, trend_drill = _temporal_drill(model, table)
    # Cuts ranked by how much they explain the primary measure -- the most
    # decisive dimension leads (drives x, breakdown, drill order, detail tables).
    ranked_cats = ranked_names(model.frame, cat_names, measure=primary_col,
                               is_share=primary_share)
    low = [c for c in ranked_cats if distinct.get(c, 99) <= 6]  # good split cuts
    cat1 = ranked_cats[0] if ranked_cats else None       # most-important main cut
    facet_cat = low[0] if low else None
    stack_cat = low[1] if len(low) > 1 else facet_cat
    group_cat = low[2] if len(low) > 2 else None
    donut_cat = low[-1] if low else None

    A: list[dict[str, Any]] = []

    # ---- Trends: KPI strip + combo (value+rate+target) + small multiples ----
    _trend_cards = [kpi_card(m, "Trends") for m in row]
    _justify_widget_widths(_trend_cards)   # fill the row (3 cards -> 4,4,4)
    A += _trend_cards
    if temporal:
        if rate_m:
            A.append({"id": "a_combo", "type": "combo",
                      "measures": [primary, rate_m["name"]],
                      "dimension": f"{table}.{temporal}",
                      "title": f"{plabel} (bars) & {rate_m['label']} (line) over Month",
                      "width": 12, "height": 410, "tab": "Trends"})
        else:
            # Drillable trend: starts coarse (e.g. Year), light bars behind the
            # line mark each period, click a period to drill finer (-> quarter ->
            # month -> day) with an "up" crumb. Reuses the dimension-drill machinery.
            w = {"id": "a_trend", "type": "line", "measure": primary,
                 "dimension": f"{table}.{trend_start}",
                 "title": f"{plabel} over {_human(trend_start)}", "width": 12,
                 "height": 410, "tab": "Trends"}
            if len(trend_drill) > 1:
                w["drill_path"] = trend_drill
            A.append(w)
        if facet_cat:
            # Judge by cardinality, don't hardcode: a few series read best
            # OVERLAID on one line chart (direct comparison); many series would
            # overplot, so fall back to small multiples (trellis) only then.
            # Use the COARSE trend grain (e.g. Year), not raw months -- a multi-
            # series line over ~120 months is unreadable spaghetti; ~12 years per
            # series reads cleanly.
            facet_n = dict(dims).get(facet_cat, 99)
            sm_type = "line" if facet_n <= 7 else "small_multiples"
            A.append({"id": "a_sm", "type": sm_type, "measure": primary,
                      "dimension": f"{table}.{trend_start}",
                      "breakdown": f"{table}.{facet_cat}",
                      "title": f"{plabel} over {_human(trend_start)}, by {_human(facet_cat)}",
                      "width": 12, "height": 320, "tab": "Trends"})

    # ---- Breakdowns: stacked (2 cuts) + grouped + one donut ----
    cat_cols = [d for d, _ in dims if d != "month"]   # drill hierarchy source

    def _drill(xcol, exclude):
        rest = [c for c in cat_cols if c not in exclude][:2]
        return [xcol] + rest if rest else []

    if cat1 and stack_cat and cat1 != stack_cat:
        # Many categories on a vertical stacked bar = unreadable wall of bars.
        # Generic rule: beyond ~12 categories, go horizontal and keep the top-N
        # by value -- stakeholders read the leaders + each bar's total at a glance.
        many = distinct.get(cat1, 0) > 12
        w = {"id": "a_stack", "type": "hbar" if many else "stacked_bar",
             "measure": primary, "dimension": f"{table}.{cat1}",
             "breakdown": f"{table}.{stack_cat}",
             "title": (f"Top {_human(cat1)} by {plabel}, split by {_human(stack_cat)}"
                       if many else
                       f"{plabel} by {_human(cat1)}, split by {_human(stack_cat)}"),
             "width": 12, "height": 440, "tab": "Breakdowns",
             "drill_path": _drill(cat1, {cat1, stack_cat})}
        if many:
            w["limit"] = 12
        A.append(w)
    if group_cat and facet_cat and group_cat != facet_cat:
        # A grouped bar across a SKEWED category (one race/payer holds most of the
        # measure) squishes every other group to an invisible sliver. Rather than
        # change the chart type, the renderer auto-applies a LOG value axis when it
        # detects this skew -- so the small bars stay visible AND comparable while
        # the chart stays a familiar bar (data labels still show exact values).
        A.append({"id": "a_group", "type": "bar", "measure": primary,
                  "dimension": f"{table}.{group_cat}", "breakdown": f"{table}.{facet_cat}",
                  "title": f"{plabel} by {_human(group_cat)} x {_human(facet_cat)}",
                  "width": 6, "height": 340, "tab": "Breakdowns",
                  "drill_path": _drill(group_cat, {group_cat, facet_cat})})
    if donut_cat:
        A.append({"id": "a_donut", "type": "donut", "measure": "record_count",
                  "dimension": f"{table}.{donut_cat}",
                  "title": f"Records by {_human(donut_cat)}", "width": 6,
                  "height": 340, "tab": "Breakdowns"})

    # ---- Detail: conditional-format tables for the most-important dims ----
    table_measures = [m["name"] for m in measures][:4]
    conditional = [{"column": mn,
                    "type": "color_scale" if next((m for m in measures if m["name"] == mn), {}).get("fmt") == "percent"
                            else "data_bar"} for mn in table_measures]
    for cat in ranked_cats[:2]:
        A.append({"id": f"a_tbl_{_slug(cat)}", "type": "table", "title": f"By {_human(cat)}",
                  "dimension": f"{table}.{cat}", "measures": table_measures,
                  "sort": "desc", "limit": 50, "width": 12, "height": 460,
                  "export": True, "conditional": conditional, "tab": "Detail"})

    # Slicers: the most-important cuts first (so the top filters are the decisive
    # dimensions), capped to readable cardinality.
    filters = [{"id": f"f_{_slug(d)}", "field": f"{table}.{d}", "label": _human(d),
                "type": "multi"} for d in ranked_cats if distinct.get(d, 99) <= 40][:4]
    return {
        "id": "analysis", "title": "Analysis", "order": 10,
        "description": "Silver-layer exploration with dense, multi-cut charts. "
                       "Click any element to cross-filter the page." + _through_note(model.data_through),
        "filters": filters,
        "widgets": A,
    }


# ---------------------------------------------------------------------------
# Generate (DQ-gated publish)
# ---------------------------------------------------------------------------


def generate(layout: WorkspaceLayout, *, force: bool = False,
             refresh_seconds: int = 0) -> dict[str, Any]:
    """Build + certify the conformed model, then (if certified) write the
    MinusAnalyst root. Returns a status dict; never raises on a DQ failure.

    DQ-gated publish: when certification fails (and not ``force``), nothing is
    written, so a running dashboard keeps the last-good snapshot. ``refresh_seconds``
    > 0 makes the generated project tell the live app to re-read data that often.
    """
    model = build_conformed_model(layout)
    if model is None:
        return {"ok": False, "reason": "no conformed model (bronze/edges missing)",
                "root": str(minus_root(layout)), "published": False}
    report = certify(model)
    root = minus_root(layout)
    if not report.get("ok") and not force:
        return {"ok": False, "reason": "DQ certification failed",
                "failed": report.get("failed"), "root": str(root), "published": False}

    data_dir = root / "data"
    dash_dir = root / "config" / "dashboards"
    data_dir.mkdir(parents=True, exist_ok=True)
    dash_dir.mkdir(parents=True, exist_ok=True)

    model.frame.write_parquet(data_dir / "conformed.parquet")
    project, pages, kpi_exports = build_minus_project(model)
    if refresh_seconds and refresh_seconds > 0:
        project["refresh_seconds"] = int(refresh_seconds)
    for tname, frame in kpi_exports.items():
        frame.write_parquet(data_dir / f"{tname}.parquet")
    _atomic_write(root / "project.yaml",
                  yaml.safe_dump(project, sort_keys=False, allow_unicode=True, width=100))
    # clear stale page files, then write each spec atomically
    keep = {f"{p['id']}.yaml" for p in pages}
    for old in dash_dir.glob("*.y*ml"):
        if old.name not in keep:
            old.unlink()
    for page in pages:
        _atomic_write(dash_dir / f"{page['id']}.yaml",
                      yaml.safe_dump(page, sort_keys=False, allow_unicode=True, width=100))
    return {
        "ok": True, "published": True, "root": str(root),
        "fact": model.fact, "rows": model.frame.height,
        "measures": [m["name"] for m in project["measures"]],
        "pages": [p["id"] for p in pages],
        "certified": report.get("ok"), "force": force,
        "refresh_seconds": int(refresh_seconds or 0),
    }


__all__ = ["build_minus_project", "generate", "minus_root"]
