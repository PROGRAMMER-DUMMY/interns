"""Value formatting for the live dashboard (compact currency, percent, int)."""
from __future__ import annotations


def format_value(value, fmt: str | None) -> str:
    if value is None:
        return "-"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v != v:  # NaN
        return "-"
    f = (fmt or "").lower()
    if f == "percent":
        return f"{v:.1f}%"
    if f == "currency":
        return "$" + _compact(v)
    if f == "int":
        return f"{int(round(v)):,}"
    if f == "float":
        return f"{v:,.2f}"
    # default: compact for large magnitudes, else trim
    if abs(v) >= 1000:
        return _compact(v)
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _compact(v: float) -> str:
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{v / div:.1f}{suf}"
    return f"{v:,.0f}"


__all__ = ["format_value"]
