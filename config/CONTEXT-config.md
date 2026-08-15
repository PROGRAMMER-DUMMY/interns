# Config Architecture Context: `config`

This document provides an exhaustive reference for all components in [`config`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config).

---

## Executive Overview & Architectural Model

The `config` directory houses platform configuration files, enterprise multi-tenant credentials, lockfiles, task definitions, optimization playbooks, and Databricks scope mapping schemas.

---

## Subdirectories & Context Maps

- [`domain_packs/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/domain_packs/CONTEXT-domain_packs.md): Industry domain vocabulary and KPI packs (healthcare, finance, supply chain). See [`domain_packs/CONTEXT-domain_packs.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/domain_packs/CONTEXT-domain_packs.md).
- [`enterprises/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/enterprises/CONTEXT-enterprises.md): Enterprise multi-tenant lockfiles and Databricks profiles. See [`enterprises/CONTEXT-enterprises.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/enterprises/CONTEXT-enterprises.md).
- [`source_catalogs/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/source_catalogs/CONTEXT-source_catalogs.md): Standard catalog definitions for Unity Catalog and remote databases. See [`source_catalogs/CONTEXT-source_catalogs.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/source_catalogs/CONTEXT-source_catalogs.md).

---

## File Details

### 1. [`agents.toml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/agents.toml)

- **Exact Purpose**: System configuration defining agent model defaults, temperature settings, and tool permissions.

### 2. [`databricks_scopes.json`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/databricks_scopes.json)

- **Exact Purpose**: Defines multi-tenant enterprise scope mappings for Unity Catalog catalogs and schemas.

### 3. [`lock.toml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/lock.toml)

- **Exact Purpose**: Global environment lockfile storing default Databricks host, warehouse ID, catalog, and credential pointers.

### 4. [`lock.toml.example`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/lock.toml.example)

- **Exact Purpose**: Example template for `lock.toml` configuration.

### 5. [`optimization_playbook.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/optimization_playbook.yaml)

- **Exact Purpose**: Declarative optimization playbook specifying rules, join strategy heuristics, and medallion loading transformations.

### 6. [`tasks.json`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/tasks.json)

- **Exact Purpose**: Active task configuration index specifying current workspace path, active intent, execution parameters, and contract targets.
