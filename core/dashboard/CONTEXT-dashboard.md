# Core Dashboard Architecture Context: `core/dashboard`

This document provides an exhaustive reference for components in [`core/dashboard`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard).

---

## Executive Overview & Architectural Model

The `core/dashboard` package manages web dashboard layout generation, UI card density, chart knowledge models, export screeners, minus adapters, and interactive agent panels.

```
┌─────────────────────┐        ┌─────────────────────┐
│   layout_planner    ├───────►│    design_md.py     │
└──────────┬──────────┘        └──────────┬──────────┘
           │                              │
           ▼                              ▼
┌─────────────────────┐        ┌─────────────────────┐
│    agent_panel.py   ├───────►│    renderer.py      │
└─────────────────────┘        └─────────────────────┘
```

---

## Subdirectories & Context Maps

- [`model/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/CONTEXT-model.md): Dashboard data models, cross-filtering, measure semantics, and parity tests. See [`model/CONTEXT-model.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/CONTEXT-model.md).
- [`ui/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/ui/CONTEXT-ui.md): Web UI components, card renderers, and interactive layout elements. See [`ui/CONTEXT-ui.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/ui/CONTEXT-ui.md).

---

## File Details

### 1. [`agent_panel.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/agent_panel.py)

- **Exact Purpose**: Generates agent panel specifications for embedded interactive agent review cards.

### 2. [`cdp.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/cdp.py)

- **Exact Purpose**: Customer Data Platform dashboard rendering handlers and metric cards.

### 3. [`chart_knowledge.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/chart_knowledge.py)

- **Exact Purpose**: Rules engine mapping KPI data types and cardinalities to appropriate visualization chart types (bar, line, scatter, KPI card, sankey).

### 4. [`default_design.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/default_design.md)

- **Exact Purpose**: Default visual design specification markdown document.

### 5. [`design_md.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/design_md.py)

- **Exact Purpose**: Parses design specification markdown into active layout engine rules.

### 6. [`export.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/export.py)

- **Exact Purpose**: Exports dashboard definitions to external formats (PowerBI PBIR, Tableau, Streamlit, HTML).

### 7. [`export_screener.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/export_screener.py)

- **Exact Purpose**: Screeners and validators verifying exported dashboard layout fidelity against source specs.

### 8. [`grid_backend.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/grid_backend.py)

- **Exact Purpose**: CSS grid and flexbox layout engine for computing element positioning and card density.

### 9. [`importance.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/importance.py)

- **Exact Purpose**: Calculates metric visual importance scores to rank chart prominence.

### 10. [`inference.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/inference.py)

- **Exact Purpose**: Visual layout inference engine auto-generating dashboard structures from KPI datasets.

### 11. [`kpi_scope.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/kpi_scope.py)

- **Exact Purpose**: Scopes KPI metrics and slicers to specific dashboard pages and tabs.

### 12. [`layout_planner.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/layout_planner.py)

- **Exact Purpose**: High-level planner calculating row, column, and grid span layouts for dashboard cards.

### 13. [`minus_adapter.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/minus_adapter.py)

- **Exact Purpose**: Compatibility adapter layer handling dashboard model transformations.

### 14. [`profile.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/profile.py)

- **Exact Purpose**: Profiler generating visualization readiness scores for workspace datasets.

### 15. [`renderer.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/renderer.py)

- **Exact Purpose**: Primary dashboard HTML/CSS/JS rendering engine.

### 16. [`screener.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/screener.py)

- **Exact Purpose**: Automated quality screener checking layout density, contrast ratios, and responsive breakpoints.

### 17. [`spec.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/spec.py)

- **Exact Purpose**: Schema definition for dashboard layout specifications.

### 18. [`suggestions.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/suggestions.py)

- **Exact Purpose**: Generates AI recommendations for additional charts, drill-downs, and slicers.
