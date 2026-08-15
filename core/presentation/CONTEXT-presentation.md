# Core Presentation Architecture Context: `core/presentation`

This document provides an exhaustive reference for all components in [`core/presentation`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/presentation).

---

## Executive Overview & Architectural Model

The `core/presentation` package manages terminal output formatting, console table rendering, and multi-format dataset exporting.

---

## File Details

### 1. [`console_tables.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/presentation/console_tables.py)

- **Exact Purpose**: Renders styled ASCII console tables for CLI summaries, panel rendering, and status reports.
- **Key Functions / Classes**:
  - [`render_console_table(headers, rows)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/presentation/console_tables.py#L15-L50): Generates formatted table string.

### 2. [`exports.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/presentation/exports.py)

- **Exact Purpose**: Handles export of KPI results and data models to CSV, Excel, Parquet, and Markdown formats.
- **Key Functions / Classes**:
  - [`export_kpi_results(df, output_format, output_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/presentation/exports.py#L30-L100): Writes DataFrame to specified export file format.
