"""Generic, domain-free dimension importance for dashboard selection.

Given a frame, a measure, and candidate categorical dimensions, rank the
dimensions by how much they EXPLAIN the measure -- so the dashboard surfaces the
most informative cuts on its own, for ANY workspace, with no domain keywords and
no favoured column names. This is the "feature importance" that decides what is
worth charting, what becomes the breakdown colour, in what order to drill, and
which dimensions earn a global slicer.

Two regimes, chosen by the measure's nature, not its name:

- Additive measures (a summed value): correlation ratio eta^2 =
  between-group variance / total variance. A dimension whose group means are far
  apart (the measure really moves along it) scores high -> it tells a story.
- Share / count measures (a percentage, or plain row counts): concentration of
  the per-group weight via 1 - normalized entropy. A flat split is
  uninformative; a dominant segment is the headline.

Both are scaled by coverage (non-null fraction) and a cardinality penalty, since
a 200-category cut is unreadable however predictive. Pure-Python + Polars only;
no sklearn/scipy dependency.
"""
from __future__ import annotations

import math

import polars as pl

__all__ = ["score_dimension", "rank_dimensions", "ranked_names"]


def _eta_squared(frame: pl.DataFrame, dim: str, measure: str) -> float:
    """Share of the measure's total variance explained by grouping on ``dim``."""
    s = frame.select([dim, measure]).drop_nulls()
    if s.height == 0:
        return 0.0
    y = s.get_column(measure).cast(pl.Float64, strict=False)
    mean_total = y.mean()
    if mean_total is None:
        return 0.0
    ss_total = float(((y - mean_total) ** 2).sum() or 0.0)
    if ss_total <= 0:
        return 0.0
    grp = s.group_by(dim).agg(
        pl.col(measure).cast(pl.Float64, strict=False).mean().alias("m"),
        pl.len().alias("n"),
    )
    ss_between = float(
        (grp.get_column("n") * (grp.get_column("m") - mean_total) ** 2).sum() or 0.0
    )
    return max(0.0, min(1.0, ss_between / ss_total))


def _concentration(frame: pl.DataFrame, dim: str, measure: str | None) -> float:
    """1 - normalized entropy of the per-group weight (sum of ``measure``, or row
    count when ``measure`` is None). 0 = perfectly even split, ->1 = dominated by
    one group."""
    if measure is None:
        grp = frame.select(dim).drop_nulls().group_by(dim).agg(pl.len().alias("w"))
    else:
        grp = (
            frame.select([dim, measure])
            .drop_nulls()
            .group_by(dim)
            .agg(pl.col(measure).cast(pl.Float64, strict=False).sum().alias("w"))
        )
    weights = [abs(w) for w in grp.get_column("w").to_list() if w is not None]
    k = len(weights)
    total = sum(weights)
    if k <= 1 or total <= 0:
        return 0.0
    ent = -sum((w / total) * math.log(w / total) for w in weights if w > 0)
    ent_max = math.log(k)
    if ent_max <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - ent / ent_max))


def _cardinality_penalty(k: int, *, ideal: int = 12) -> float:
    """1.0 up to ``ideal`` categories, decaying gently beyond (readability)."""
    if k <= ideal:
        return 1.0
    return ideal / float(k)


def score_dimension(
    frame: pl.DataFrame,
    dim: str,
    measure: str | None,
    *,
    is_share: bool = False,
) -> float:
    """Importance of ``dim`` for ``measure`` in [0, 1]. ``measure=None`` scores by
    how unevenly row count distributes (for count-style measures); ``is_share``
    forces the concentration regime for percentage measures."""
    if dim not in frame.columns or frame.height == 0:
        return 0.0
    col = frame.get_column(dim)
    k = col.n_unique()
    if k <= 1:
        return 0.0
    if measure is None or is_share or measure not in frame.columns:
        base = _concentration(frame, dim, measure if measure in frame.columns else None)
    else:
        base = _eta_squared(frame, dim, measure)
    coverage = 1.0 - (col.null_count() / max(1, frame.height))
    return base * coverage * _cardinality_penalty(k)


def rank_dimensions(
    frame: pl.DataFrame,
    dims,
    *,
    measure: str | None = None,
    is_share: bool = False,
    max_cardinality: int = 50,
) -> list[tuple[str, float]]:
    """Candidate ``dims`` ranked by importance (desc). Drops constants, dates'
    callers should pre-filter; here we only drop missing/constant/over-cardinality
    columns. Ties keep input order (stable sort)."""
    scored: list[tuple[str, float]] = []
    for d in dims:
        if d not in frame.columns:
            continue
        k = frame.get_column(d).n_unique()
        if k <= 1 or k > max_cardinality:
            continue
        scored.append((d, score_dimension(frame, d, measure, is_share=is_share)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


def ranked_names(frame: pl.DataFrame, dims, **kw) -> list[str]:
    """Just the dimension names, most-important first."""
    return [d for d, _ in rank_dimensions(frame, dims, **kw)]
