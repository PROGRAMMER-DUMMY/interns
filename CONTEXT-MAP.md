# CONTEXT-MAP.md — Master Project Context Tree & Maintenance Guide

## Overview
`CONTEXT-MAP.md` is the central navigation tree and operating standard for context documentation across **interns** (Data Engineering Optimization & Governance Control Plane).

Every directory and subfolder in this repository maintains a dedicated, file-by-file **`CONTEXT-[folder].md`** document detailing the purpose, interfaces, failure modes, and architectural roles of every contained file inside it.

> **Note**: As per workspace isolation policies, customer execution workspaces under `workspaces/` are excluded from repository context mapping.

---

## 🌲 Master Context Tree

```
interns (Workspace Root)
│
├── CONTEXT-MAP.md                                         # Master context tree & maintenance guide
├── AGENTS.md                                              # Agent operating guide & governance rules
├── CONTEXT.md                                             # Domain language & platform architecture
├── README.md                                              # Repository overview, layout, and setup
├── TOOLS.md                                               # Detailed project tools reference
├── pyproject.toml                                         # Project dependencies & governed CLI entrypoints
│
├── assets/                                                # Web UI stylesheets, scripts, & lightboxes
│   └── CONTEXT-assets.md                                  # [`CONTEXT-assets.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/assets/CONTEXT-assets.md)
│
├── config/                                                # Configuration, lockfiles, and playbooks
│   ├── CONTEXT-config.md                                  # [`CONTEXT-config.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/CONTEXT-config.md)
│   ├── domain_packs/                                      # [`CONTEXT-domain_packs.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/domain_packs/CONTEXT-domain_packs.md)
│   ├── enterprises/                                       # [`CONTEXT-enterprises.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/enterprises/CONTEXT-enterprises.md)
│   └── source_catalogs/                                   # [`CONTEXT-source_catalogs.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/source_catalogs/CONTEXT-source_catalogs.md)
│
├── core/                                                  # Main platform orchestration & governance engine
│   ├── CONTEXT-core.md                                    # [`CONTEXT-core.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/CONTEXT-core.md)
│   ├── agents/                                            # [`CONTEXT-agents.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/CONTEXT-agents.md)
│   ├── blueprint/                                         # [`CONTEXT-blueprint.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/CONTEXT-blueprint.md)
│   │   └── tables/                                        # [`CONTEXT-tables.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/blueprint/tables/CONTEXT-tables.md)
│   ├── context/                                           # [`CONTEXT-context.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/context/CONTEXT-context.md)
│   ├── contracts/                                         # [`CONTEXT-contracts.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/contracts/CONTEXT-contracts.md)
│   ├── dashboard/                                         # [`CONTEXT-dashboard.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/CONTEXT-dashboard.md)
│   │   ├── model/                                         # [`CONTEXT-model.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/model/CONTEXT-model.md)
│   │   └── ui/                                            # [`CONTEXT-ui.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dashboard/ui/CONTEXT-ui.md)
│   ├── dev/                                               # [`CONTEXT-dev.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/dev/CONTEXT-dev.md)
│   ├── evolution/                                         # [`CONTEXT-evolution.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/evolution/CONTEXT-evolution.md)
│   ├── execution/                                         # [`CONTEXT-execution.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/execution/CONTEXT-execution.md)
│   ├── governance/                                        # [`CONTEXT-governance.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/governance/CONTEXT-governance.md)
│   ├── intake/                                            # [`CONTEXT-intake.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/CONTEXT-intake.md)
│   ├── medallion/                                         # [`CONTEXT-medallion.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/medallion/CONTEXT-medallion.md)
│   ├── observability/                                     # [`CONTEXT-observability.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/observability/CONTEXT-observability.md)
│   ├── onboarding/                                        # [`CONTEXT-onboarding.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/CONTEXT-onboarding.md)
│   │   ├── benchmark/                                     # [`CONTEXT-benchmark.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/benchmark/CONTEXT-benchmark.md)
│   │   ├── data_model/                                    # [`CONTEXT-data_model.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/data_model/CONTEXT-data_model.md)
│   │   ├── databricks/                                    # [`CONTEXT-databricks.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/databricks/CONTEXT-databricks.md)
│   │   ├── documents/                                     # [`CONTEXT-documents.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/documents/CONTEXT-documents.md)
│   │   ├── features/                                      # [`CONTEXT-features.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/features/CONTEXT-features.md)
│   │   ├── harness/                                       # [`CONTEXT-harness.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/harness/CONTEXT-harness.md)
│   │   ├── kpi/                                           # [`CONTEXT-kpi.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/kpi/CONTEXT-kpi.md)
│   │   ├── lexicon/                                       # [`CONTEXT-lexicon.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/lexicon/CONTEXT-lexicon.md)
│   │   ├── memory/                                        # [`CONTEXT-memory.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/memory/CONTEXT-memory.md)
│   │   ├── relationships/                                 # [`CONTEXT-relationships.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/CONTEXT-relationships.md)
│   │   ├── sources/                                       # [`CONTEXT-sources.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/sources/CONTEXT-sources.md)
│   │   └── workspace/                                     # [`CONTEXT-workspace.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/workspace/CONTEXT-workspace.md)
│   ├── optimization/                                      # [`CONTEXT-optimization.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/optimization/CONTEXT-optimization.md)
│   ├── orchestration/                                     # [`CONTEXT-orchestration.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/orchestration/CONTEXT-orchestration.md)
│   ├── presentation/                                      # [`CONTEXT-presentation.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/presentation/CONTEXT-presentation.md)
│   ├── profiling/                                         # [`CONTEXT-profiling.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/profiling/CONTEXT-profiling.md)
│   ├── provisioning/                                      # [`CONTEXT-provisioning.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/CONTEXT-provisioning.md)
│   ├── resource/                                          # [`CONTEXT-resource.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/resource/CONTEXT-resource.md)
│   ├── skills/                                            # [`CONTEXT-skills.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/skills/CONTEXT-skills.md)
│   ├── storage/                                           # [`CONTEXT-storage.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/CONTEXT-storage.md)
│   └── wiki/                                              # [`CONTEXT-wiki.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/wiki/CONTEXT-wiki.md)
│
├── dashboard_app/                                         # [`CONTEXT-dashboard_app.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/dashboard_app/CONTEXT-dashboard_app.md)
│
├── docker/                                                # Docker manifests & Airflow container configs
│   └── CONTEXT-docker.md                                  # [`CONTEXT-docker.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docker/CONTEXT-docker.md)
│
├── docs/                                                  # [`CONTEXT-docs.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/CONTEXT-docs.md)
│   ├── agents/                                            # [`CONTEXT-agents.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/agents/CONTEXT-agents.md)
│   ├── bugs/                                              # [`CONTEXT-bugs.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/bugs/CONTEXT-bugs.md)
│   ├── core_audit/                                        # [`CONTEXT-core_audit.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/core_audit/CONTEXT-core_audit.md)
│   ├── design/                                            # [`CONTEXT-design.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/design/CONTEXT-design.md)
│   ├── enterprise/                                        # [`CONTEXT-enterprise.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/enterprise/CONTEXT-enterprise.md)
│   ├── plans/                                             # [`CONTEXT-plans.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/plans/CONTEXT-plans.md)
│   ├── prd/                                               # [`CONTEXT-prd.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/prd/CONTEXT-prd.md)
│   ├── reference/                                         # [`CONTEXT-reference.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/reference/CONTEXT-reference.md)
│   └── superpowers/                                       # [`CONTEXT-superpowers.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docs/superpowers/CONTEXT-superpowers.md)
│
├── interns/                                               # [`CONTEXT-interns.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/CONTEXT-interns.md)
├── plugins/                                               # [`CONTEXT-plugins.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/plugins/CONTEXT-plugins.md)
├── scripts/                                               # [`CONTEXT-scripts.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/scripts/CONTEXT-scripts.md)
├── skills/                                                # [`CONTEXT-skills.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/skills/CONTEXT-skills.md)
├── spikes/                                                # [`CONTEXT-spikes.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/spikes/CONTEXT-spikes.md)
├── state/                                                 # Global team memory & persistent state
│   └── CONTEXT-state.md                                   # [`CONTEXT-state.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/state/CONTEXT-state.md)
│
├── tests/                                                 # [`CONTEXT-tests.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/CONTEXT-tests.md)
│   ├── fixtures/                                          # [`CONTEXT-fixtures.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/fixtures/CONTEXT-fixtures.md)
│   ├── onboarding/                                        # [`CONTEXT-onboarding.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/onboarding/CONTEXT-onboarding.md)
│   └── regressions/                                       # [`CONTEXT-regressions.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/regressions/CONTEXT-regressions.md)
│
├── tools/                                                 # [`CONTEXT-tools.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tools/CONTEXT-tools.md)
│
└── vendor/                                                # Vendored packages & minus framework modules
    └── CONTEXT-vendor.md                                  # [`CONTEXT-vendor.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/vendor/CONTEXT-vendor.md)
```

---

## 🛠️ Operating Guide: How to Maintain & Refer to Context Files

1. **Before making changes**: Read the corresponding `CONTEXT-[folder].md` to understand dependencies, invariants, and failure modes.
2. **Clickable Links**: All file references MUST use GitHub-style markdown links with the `file://` URI scheme using forward slashes (e.g., `[`config.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/config.py)`).
3. **Atomic Updates**: Code modifications must include an immediate update to the corresponding `CONTEXT-[folder].md`.
4. **Zero Spec Drift**: Ensure line numbers, signatures, and file listings in context files match disk reality exactly.
5. **Enforced in CI**: [`tests/test_context_map_drift.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_context_map_drift.py) checks drift in **both** directions — a context file this map links to but that is missing on disk, and a source directory on disk that has no `CONTEXT-<dir>.md`. It also locks a baseline of CLI entry points missing a `### <command>` section in `TOOLS.md`, failing only when a **new** undocumented command appears. Rules 1–4 were unenforced before this; the map claimed `core/skills/CONTEXT-skills.md` for months while the file did not exist.
