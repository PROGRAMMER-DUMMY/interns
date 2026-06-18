"""Declarative config models — the contract a CLI agent edits.

Every field carries a ``description`` because these flow into the exported JSON
Schema (``minus schema``), which is handed to the agent so it can author and
self-correct YAML without reading the Python source.

Field references use dotted ``"table.column"`` strings. Measures are referenced
by their ``name``. Keep names stable: widgets and relationships point at them.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Enums (kept as Literals so they render cleanly in JSON Schema)
# ---------------------------------------------------------------------------

SourceType = Literal["csv", "parquet", "sql"]

Aggregation = Literal[
    "sum", "avg", "min", "max", "count", "count_distinct", "median", "first",
]

# Derived measures combine other measures rather than raw columns.
DerivedKind = Literal["ratio", "percent_of_total", "yoy", "expression"]

WidgetType = Literal[
    "kpi", "bar", "hbar", "line", "area", "pie", "donut",
    "scatter", "heatmap", "table",
    # dense / multi-cut visuals
    "stacked_bar",       # one bar per x, segments = breakdown (composition)
    "combo",             # measures[0] as bars + measures[1] as a line on a 2nd axis
    "small_multiples",   # the measure faceted by a dimension (trellis)
]

FilterType = Literal["dropdown", "multi", "date_range", "range"]

RelationshipKind = Literal["many_to_one", "one_to_many", "one_to_one"]

# Granularity for a KPI's period-over-period "trend delta".
ComparePeriod = Literal["year", "quarter", "month"]


class _Base(BaseModel):
    # Reject unknown keys so a typo'd YAML field fails loudly (helps the agent).
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class DataSource(_Base):
    """A place data is read from. v1 ships a CSV connector only."""

    name: str = Field(..., description="Unique id, referenced by tables.")
    type: SourceType = Field(
        "csv",
        description="Connector type: 'csv' (file/folder of CSVs), 'parquet' "
        "(file/folder of Parquet), or 'sql' (a sqlite .db file).",
    )
    path: str = Field(
        ...,
        description="File or folder path (relative to project root) for the connector.",
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Connector-specific options, e.g. {'sep': ',', 'encoding': 'utf-8'}.",
    )


class Table(_Base):
    """A logical table mapped onto a file within a datasource."""

    name: str = Field(..., description="Unique table id used in field refs and relationships.")
    source: str = Field(..., description="Name of the DataSource this table reads from.")
    file: Optional[str] = Field(
        None,
        description="File name within a folder datasource (e.g. 'patients.csv'). "
        "Omit when the datasource path is a single file.",
    )
    primary_key: Optional[str] = Field(
        None, description="Column that uniquely identifies a row (used for joins/counts)."
    )
    label: Optional[str] = Field(None, description="Human-friendly display name.")


class Relationship(_Base):
    """A join between two tables, enabling cross-table widgets (star schema)."""

    from_table: str = Field(..., description="The 'many' side table (the fact, usually).")
    from_column: str = Field(..., description="Foreign-key column on from_table.")
    to_table: str = Field(..., description="The 'one' side table (the dimension).")
    to_column: str = Field(..., description="Key column on to_table (its primary key).")
    kind: RelationshipKind = Field(
        "many_to_one", description="Cardinality from from_table to to_table."
    )


# ---------------------------------------------------------------------------
# Measures (DAX-lite)
# ---------------------------------------------------------------------------


class Measure(_Base):
    """A reusable, named metric.

    Two flavors:
      * Aggregate — set ``agg`` + ``field`` (e.g. sum of claims.ClaimAmount).
      * Derived   — set ``kind`` + the relevant operands (ratio/percent/yoy/expression).
    """

    name: str = Field(..., description="Unique id used by widgets, e.g. 'total_claims'.")
    label: Optional[str] = Field(None, description="Display name shown on the dashboard.")

    # Aggregate form
    agg: Optional[Aggregation] = Field(
        None, description="Aggregation function for a plain measure."
    )
    field: Optional[str] = Field(
        None, description="Dotted 'table.column' to aggregate. Omit for plain count."
    )
    table: Optional[str] = Field(
        None,
        description="Table to aggregate over. Required for 'count' measures that "
        "have no field; otherwise inferred from 'field'.",
    )

    # Derived form
    kind: Optional[DerivedKind] = Field(
        None,
        description="Derived measure type. 'ratio'/'percent_of_total'/'yoy'/'expression'.",
    )
    numerator: Optional[str] = Field(None, description="Measure name for ratio numerator.")
    denominator: Optional[str] = Field(None, description="Measure name for ratio denominator.")
    of: Optional[str] = Field(
        None, description="Base measure name for percent_of_total / yoy."
    )
    date_field: Optional[str] = Field(
        None, description="Dotted date 'table.column' used by yoy to bucket years."
    )
    expression: Optional[str] = Field(
        None,
        description="For kind='expression': a formula over measure names, "
        "e.g. '(paid_amount / claim_amount) * 100'.",
    )

    fmt: Optional[str] = Field(
        None,
        description="Display format: 'currency', 'percent', 'int', 'float', or a "
        "Python format spec like ',.2f'.",
    )

    target: Optional[float] = Field(
        None,
        description="Optional KPI benchmark/target. When set, the KPI tile shows "
        "the target and colors the value green/red according to 'goal'.",
    )
    goal: Literal["higher", "lower"] = Field(
        "higher",
        description="Whether higher or lower is better, for target coloring.",
    )

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not v or " " in v:
            raise ValueError("measure name must be non-empty and contain no spaces")
        return v


# ---------------------------------------------------------------------------
# Filters & widgets
# ---------------------------------------------------------------------------


class Filter(_Base):
    """A slicer. Page-level filters apply to every widget on the page."""

    id: str = Field(..., description="Unique id within its page.")
    field: str = Field(..., description="Dotted 'table.column' this filter operates on.")
    label: Optional[str] = Field(None, description="Display label for the control.")
    type: FilterType = Field("dropdown", description="Control type.")
    default: Any = Field(None, description="Optional default value(s).")


class ConditionalRule(_Base):
    """Conditional formatting for a measure column in a table widget."""

    column: str = Field(..., description="Measure name (table column) the rule styles.")
    type: Literal["data_bar", "color_scale", "icon"] = Field(
        ..., description="data_bar = in-cell bar; color_scale = heat background; "
        "icon = ▲/▼ by sign."
    )
    color: Optional[str] = Field(None, description="Bar/high color (hex). Defaults to accent.")


class Widget(_Base):
    """A single visual tile."""

    id: str = Field(..., description="Unique id within its page.")
    type: WidgetType = Field(..., description="Visual type.")
    title: Optional[str] = Field(None, description="Tile heading.")

    measure: Optional[str] = Field(
        None, description="Measure name for KPI / single-series charts."
    )
    measures: list[str] = Field(
        default_factory=list,
        description="Multiple measure names (multi-series charts, table columns).",
    )
    dimension: Optional[str] = Field(
        None, description="Primary group-by field 'table.column' (x axis / categories)."
    )
    breakdown: Optional[str] = Field(
        None, description="Secondary group-by 'table.column' for color/series split."
    )
    base_filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Built-in scope filters ({'table.column': value}) baked into this "
        "widget, e.g. a KPI's intrinsic 'LineOfBusiness = Commercial'. They are the "
        "DEFAULT view but are overridden by any page slicer / cross-filter selection on "
        "the same column, so the chart can be re-scoped interactively without changing "
        "what the KPI means by default.",
    )

    sort: Optional[Literal["asc", "desc"]] = Field(
        "desc", description="Sort direction by the primary measure."
    )
    limit: Optional[int] = Field(None, description="Keep only the top-N categories.")

    width: int = Field(
        6, ge=1, le=12, description="Grid width in 12-column units."
    )
    height: int = Field(320, description="Tile height in pixels.")
    tab: Optional[str] = Field(
        None,
        description="Optional tab label. Widgets sharing a tab render together in "
        "a dcc.Tab so each view fits the screen without scrolling.",
    )
    subtitle: Optional[str] = Field(
        None,
        description="Optional one-line context shown under a KPI card's value "
        "(e.g. the business question the KPI answers).",
    )

    compare: Optional[str] = Field(
        None,
        description="Dotted 'table.column' date field to compute a period-over-period "
        "delta for a KPI.",
    )
    compare_period: ComparePeriod = Field(
        "year", description="Granularity for the compare delta."
    )

    drilldown: Optional[str] = Field(
        None, description="Page id to navigate to when a data point is clicked."
    )
    drill_path: list[str] = Field(
        default_factory=list,
        description="Ordered dimension columns for in-place drill-down. Clicking a "
        "category re-renders THIS chart by the next dimension, filtered to the "
        "clicked value (PowerBI-style). drill_path[0] is the top level.",
    )
    conditional: list[ConditionalRule] = Field(
        default_factory=list,
        description="Conditional formatting rules for table widgets (data bars, "
        "color scales, icons).",
    )
    export: bool = Field(
        False, description="Show a 'CSV' button to download this widget's data."
    )
    options: dict[str, Any] = Field(
        default_factory=dict, description="Extra plotly/widget options."
    )


class Dashboard(_Base):
    """One page of the report."""

    id: str = Field(..., description="Unique page id (used in nav + drilldown targets).")
    title: str = Field(..., description="Page title shown in the sidebar and header.")
    icon: Optional[str] = Field(None, description="Optional emoji/char for the nav item.")
    order: int = Field(100, description="Sort order in the sidebar (lower = higher).")
    description: Optional[str] = Field(None, description="Short subtitle under the title.")

    filters: list[Filter] = Field(
        default_factory=list, description="Page-level slicers applied to all widgets."
    )
    widgets: list[Widget] = Field(
        default_factory=list, description="Tiles, laid out left-to-right on a 12-col grid."
    )


# ---------------------------------------------------------------------------
# Project-level config
# ---------------------------------------------------------------------------


class ThemeConfig(_Base):
    name: str = Field("claude", description="Theme name; 'claude' ships by default.")
    accent: Optional[str] = Field(None, description="Override accent color (hex).")
    canvas: Optional[str] = Field(None, description="Override page background (hex).")


class AgentConfig(_Base):
    """How the in-app chat panel / `minus edit` reaches your CLI agent.

    No API key. The command is run as a subprocess in the project directory and
    uses whatever auth your CLI already has. ``{prompt}`` is substituted with the
    fully-built instruction (request + schema path + current config + field catalog).
    """

    command: list[str] = Field(
        default_factory=lambda: ["claude", "-p", "{prompt}"],
        description="Argv template. '{prompt}' is replaced with the built instruction.",
    )
    cwd: Optional[str] = Field(
        None, description="Working dir for the agent (defaults to project root)."
    )
    timeout: int = Field(600, description="Seconds before the agent call is aborted.")
    enabled: bool = Field(
        True, description="Set false to hide the in-app chat panel."
    )


class Alert(_Base):
    """A data-driven threshold alert evaluated by ``minus alerts``."""

    name: str = Field(..., description="Unique alert id.")
    measure: str = Field(..., description="Measure name to evaluate (as a scalar).")
    op: Literal["<", "<=", ">", ">=", "==", "!="] = Field(
        ..., description="Comparison operator against 'value'."
    )
    value: float = Field(..., description="Threshold the measure is compared to.")
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional filters ({'table.column': value}) scoping the measure.",
    )
    message: Optional[str] = Field(None, description="Custom message when it fires.")


class NotifyConfig(_Base):
    """Where fired alerts / subscription snapshots are delivered."""

    channel: Literal["console", "file", "webhook", "email"] = Field(
        "console", description="Delivery channel for notifications."
    )
    file: Optional[str] = Field(None, description="Path for the 'file' channel (appended).")
    webhook: Optional[str] = Field(None, description="POST URL for the 'webhook' channel.")
    email: Optional[dict[str, Any]] = Field(
        None,
        description="SMTP settings for 'email': "
        "{host, port, sender, recipients:[...], user, password_env}.",
    )


class Project(_Base):
    """The root config. Lives in ``project.yaml`` at the project root."""

    name: str = Field("MinusAnalyst", description="Report/app name shown in the header.")
    datasources: list[DataSource] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    measures: list[Measure] = Field(
        default_factory=list, description="Reusable measures available to all pages."
    )
    theme: ThemeConfig = Field(default_factory=ThemeConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    alerts: list[Alert] = Field(
        default_factory=list, description="Threshold alerts evaluated by `minus alerts`."
    )
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    refresh_seconds: Optional[int] = Field(
        None, description="If set, the running app re-reads source data this often."
    )
    dashboards_dir: str = Field(
        "config/dashboards",
        description="Folder (relative to project root) holding one YAML per page.",
    )
    data_through: Optional[str] = Field(
        None,
        description="Last COMPLETE reporting period (ISO date). Trend charts mark "
                    "anything after it as an incomplete trailing period.",
    )

    # convenience lookups -------------------------------------------------
    def table(self, name: str) -> Table:
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(f"unknown table: {name!r}")

    def measure(self, name: str) -> Measure:
        for m in self.measures:
            if m.name == name:
                return m
        raise KeyError(f"unknown measure: {name!r}")

    def datasource(self, name: str) -> DataSource:
        for d in self.datasources:
            if d.name == name:
                return d
        raise KeyError(f"unknown datasource: {name!r}")
