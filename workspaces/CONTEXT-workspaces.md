# Workspaces Architecture Context: `workspaces`

This document provides an exhaustive reference for project workspace storage in [`workspaces`](file:///C:/Users/shubh/OneDrive/Desktop/interns/workspaces).

---

## Executive Overview & Architectural Model

The `workspaces` directory is the isolated customer project execution area. Each subdirectory represents a project workspace containing raw datasets, local configuration, and isolated optimizer state under `interns/`.

```
workspaces/<project>/
├── datasets/                 # Source data (local files)
├── docs/                     # Data dictionaries, requirements, PRDs
├── dbt/                      # Generated local dbt project
└── interns/                  # Generated optimizer state (git ignored)
    ├── state/                # workspace.db, workspace.lock, events.jsonl
    ├── runs/                 # Historical execution outputs
    ├── generated/            # Requirements, contracts, profiles, evidence
    └── reports/              # Human-readable markdown reports
```

---

## Directory Details

### 1. [`rcm`](file:///C:/Users/shubh/OneDrive/Desktop/interns/workspaces/rcm)

- **Exact Purpose**: Revenue Cycle Management (RCM) sample healthcare workspace containing claim, billing, and patient datasets, KPI definitions, and conformed data models.
