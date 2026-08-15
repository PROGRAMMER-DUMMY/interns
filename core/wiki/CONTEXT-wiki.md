# Core Wiki Architecture Context: `core/wiki`

This document provides an exhaustive reference for all components in [`core/wiki`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki).

---

## Executive Overview & Architectural Model

The `core/wiki` package handles automated dataset wiki generation, data dictionary publishing, and data lineage documentation generation.

---

## File Details

### 1. [`layout.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki/layout.py)

- **Exact Purpose**: Defines Markdown wiki layout and table of contents structures.
- **Key Functions / Classes**:
  - [`generate_wiki_layout(pages)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki/layout.py#L10-L30): Formats wiki layout tree.

### 2. [`lineage.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki/lineage.py)

- **Exact Purpose**: Renders visual Mermaid data lineage diagrams for wiki pages.
- **Key Functions / Classes**:
  - [`generate_lineage_mermaid(lineage_data)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki/lineage.py#L20-L70): Renders flowcharts showing source-to-gold table lineage.

### 3. [`reader.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki/reader.py)

- **Exact Purpose**: Reads and parses existing workspace wiki documentation.
- **Key Functions / Classes**:
  - [`read_workspace_wiki(wiki_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki/reader.py#L15-L50): Loads wiki pages into memory.

### 4. [`template.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki/template.py)

- **Exact Purpose**: Markdown templates for data dictionaries, table summaries, and column descriptions.

### 5. [`writer.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki/writer.py)

- **Exact Purpose**: Generates and writes data model wiki documents to `interns/reports/wiki/`.
- **Key Functions / Classes**:
  - [`write_workspace_wiki(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki/writer.py#L30-L110): Assembles and writes complete workspace dataset wiki.
