# Databricks Token Scopes Reference

> **For agent use:** This document maps every Databricks OAuth/PAT permission scope to its category, description, and the tasks that require it. Use it to determine the minimum required scopes before generating a token.

Machine-readable companion: `config/databricks_scopes.json` (47 scopes, keyed by name).

---

## This platform's scope needs (by `config/lock.toml [databricks] execution`)

Suggest the minimum set for the configured execution mode, then add per-feature scopes.

| Config | Minimum scopes |
|--------|----------------|
| `execution = "warehouse"` (default) | `sql`, `unity-catalog`, `mlflow`, `settings` |
| `execution = "jobs"` | above **+** `jobs`, `clusters`, `command-execution` |
| `execution = "connect"` (Databricks Connect) | above **+** `databricks-connect`, `clusters` |
| Uploading artifacts to Volumes/DBFS | **+** `files` |
| Reading creds from Databricks Secret Store | **+** `secrets` |
| `deploy-databricks-workspace` (folder/file upload) | **+** `workspace`, `files` |
| Query-history / cost analysis | **+** `query-history` |

> **Spec-only (no scope needed):** Genie (`prepare-genie-workspace`), dashboards, and
> jobs/UC *registration* in the deployer are **spec-gated** — the platform generates a
> reviewable spec/runbook and does NOT call those APIs. So `genie`/`dashboards` are not
> required by the token. (Warehouse `sql`, UC schema/table writes, MLflow, and jobs
> *execution* DO call APIs and need their scopes.)

Detection rule: on a permission error during `tools/databricks_setup.py`, map the failing
API to its scope (tables below) and report it as the missing scope to add.

---

## Quick Lookup by Task

| Task | Required Scopes |
|------|----------------|
| Run a job | `jobs`, `clusters` |
| Run a pipeline (DLT) | `pipelines`, `clusters` |
| Execute notebook/command interactively | `command-execution`, `clusters` |
| Submit a SQL query | `sql`, `query-history` |
| Read/write files (DBFS, Volumes) | `files` |
| Train and log ML experiments | `mlflow`, `clusters` |
| Serve a model endpoint | `model-serving` |
| Query a Vector Search index | `vector-search` |
| Use AI Gateway / LLM routing | `ai-gateway` |
| Use AI Search / Genie | `ai-search`, `genie` |
| Use Knowledge Assistants | `knowledge-assistants` |
| Use custom LLMs | `custom-llms` |
| Deploy a supervisor agent | `supervisor-agents` |
| Register/query Unity Catalog | `unity-catalog` |
| Classify data assets | `dataclassification` |
| Run data quality checks | `dataquality` |
| Monitor model quality | `qualitymonitor` |
| View query history | `query-history` |
| Create/manage dashboards | `dashboards` |
| Run forecasting | `forecasting` |
| Manage Delta Live Tables | `pipelines` |
| Install/manage libraries | `libraries` |
| Manage instance pools | `instance-pools` |
| Manage instance profiles | `instance-profiles` |
| Manage environments | `environments` |
| Manage global init scripts | `global-init-scripts` |
| Connect via Databricks Connect | `databricks-connect`, `clusters` |
| Manage workspace settings | `settings`, `workspace` |
| Manage secrets | `secrets` |
| Manage tags | `tags` |
| Share data (Delta Sharing) | `sharing` |
| Manage cleanrooms | `cleanrooms` |
| Manage users/groups (SCIM) | `scim`, `identity` |
| Manage access policies | `access-management` |
| Manage authentication configs | `authentication` |
| Provision workspaces | `provisioning` |
| Manage networking | `networking` |
| Manage clusters | `clusters` |
| View/manage billing | `billing` |
| Manage notifications | `notifications` |
| Receive/manage alerts | `alerts` |
| Set up disaster recovery | `disaster-recovery` |
| Manage marketplace listings | `marketplace` |
| Manage apps | `apps` |
| Deploy bundles (asset bundles) | `bundle` |
| Use Postgres | `postgres` |
| Search workspace resources | `search` |

---

## All Scopes by Category

### Admin & Auth

| Scope | Description | Typical Use |
|-------|-------------|-------------|
| `access-management` | Manage workspace access policies and entitlements | Granting/revoking user permissions |
| `authentication` | Manage authentication configurations (SSO, tokens) | Configuring identity providers |
| `billing` | View and manage billing and usage data | Cost monitoring, chargeback reporting |
| `identity` | Read and write identity resources (users, groups, service principals) | User provisioning workflows |
| `instance-profiles` | Manage EC2 instance profiles attached to clusters | Cluster IAM role configuration |
| `provisioning` | Provision and manage workspaces and accounts | Account-level automation |
| `scim` | SCIM API for user/group sync | Syncing users from external identity providers |
| `secrets` | Read and write secrets in secret scopes | Injecting credentials into jobs/notebooks |
| `settings` | Read and write workspace settings | Configuration automation |

---

### AI / ML

| Scope | Description | Typical Use |
|-------|-------------|-------------|
| `ai-gateway` | Access and configure the AI Gateway for LLM routing | Routing requests to LLM providers |
| `ai-search` | Use AI-powered search features | Semantic search over workspace assets |
| `custom-llms` | Register and serve custom LLMs | Deploying fine-tuned or private models |
| `genie` | Access Genie data rooms and natural language SQL | NL-to-SQL agents |
| `knowledge-assistants` | Use knowledge assistant features | Building RAG-based assistants |
| `mlflow` | Log experiments, register models, manage MLflow runs | ML experiment tracking and model registry |
| `model-serving` | Create and query model serving endpoints | Real-time inference |
| `supervisor-agents` | Deploy and manage supervisor agents | Orchestrating multi-agent workflows |
| `vector-search` | Create indexes and query Vector Search | Similarity search, embedding retrieval |

---

### Compute

| Scope | Description | Typical Use |
|-------|-------------|-------------|
| `clusters` | Create, start, stop, and configure clusters | Job execution, interactive compute |
| `command-execution` | Execute commands on running clusters | Notebook execution, remote code run |
| `databricks-connect` | Use Databricks Connect for local dev | Local IDE -> remote cluster execution |
| `environments` | Manage serverless compute environments | Serverless notebook/job environments |
| `global-init-scripts` | Manage global cluster init scripts | Cluster bootstrap automation |
| `instance-pools` | Create and manage instance pools | Reducing cluster startup times |
| `libraries` | Install and manage cluster libraries | Dependency management on clusters |

---

### Workflows

| Scope | Description | Typical Use |
|-------|-------------|-------------|
| `jobs` | Create, run, and manage Databricks Jobs | Scheduled and triggered pipelines |
| `pipelines` | Create and manage Delta Live Tables pipelines | Streaming and batch DLT pipelines |

---

### Analytics

| Scope | Description | Typical Use |
|-------|-------------|-------------|
| `dashboards` | Create and manage Lakeview dashboards | BI and reporting automation |
| `forecasting` | Access forecasting features | Automated time-series forecasting |
| `query-history` | Read query history and profiles | Auditing, cost attribution, optimization |
| `sql` | Execute SQL queries via Databricks SQL | SQL analytics, BI tools |

---

### Data Governance

| Scope | Description | Typical Use |
|-------|-------------|-------------|
| `cleanrooms` | Create and manage data cleanrooms | Privacy-preserving data collaboration |
| `dataclassification` | Classify and tag data assets | Automated PII/sensitivity tagging |
| `dataquality` | Run and manage data quality rules | Data observability pipelines |
| `sharing` | Manage Delta Sharing (data sharing with external parties) | Cross-org data sharing |
| `unity-catalog` | Full access to Unity Catalog (metastore, schemas, tables, volumes) | Metadata management, lineage, access control |

---

### Monitoring

| Scope | Description | Typical Use |
|-------|-------------|-------------|
| `alerts` | Create and manage SQL alerts | Threshold-based notifications |
| `notifications` | Manage notification destinations | Alerting to Slack, email, PagerDuty |
| `qualitymonitor` | Monitor model and feature quality over time | Drift detection, model observability |

---

### Storage

| Scope | Description | Typical Use |
|-------|-------------|-------------|
| `files` | Read and write files in DBFS and Unity Catalog Volumes | File upload/download, data ingestion |
| `postgres` | Access Databricks-managed PostgreSQL | Storing metadata or app state |

---

### Platform

| Scope | Description | Typical Use |
|-------|-------------|-------------|
| `apps` | Deploy and manage Databricks Apps | Hosting Gradio/Streamlit apps |
| `bundle` | Work with Databricks Asset Bundles (DABs) | CI/CD deployment of jobs, pipelines, etc. |
| `disaster-recovery` | Configure and trigger disaster recovery | High-availability workspace setup |
| `marketplace` | Browse and install Marketplace listings | Data and model discovery |
| `networking` | Manage networking configurations (VPCs, private endpoints) | Network security automation |
| `search` | Search across workspace resources | Asset discovery |
| `tags` | Read and write resource tags | Cost tagging, governance |
| `workspace` | Access workspace objects (notebooks, folders, repos) | Notebook management, Repos integration |

---

## Recommended Scope Sets

### Minimum — read-only audit agent
```
query-history, sql, unity-catalog, tags, search, workspace
```

### Data engineering agent (jobs + pipelines)
```
jobs, pipelines, clusters, files, secrets, unity-catalog, libraries, environments
```

### ML / AI agent
```
mlflow, model-serving, vector-search, ai-gateway, clusters, files, secrets, unity-catalog
```

### Admin / provisioning agent
```
access-management, identity, scim, provisioning, settings, authentication, secrets
```

### Full platform agent (broad access)
```
jobs, pipelines, clusters, command-execution, files, sql, mlflow, model-serving,
vector-search, ai-gateway, unity-catalog, secrets, settings, workspace, identity,
scim, dashboards, alerts, notifications, qualitymonitor, sharing
```

---

## Notes

- **Principle of least privilege:** Always use the minimum scopes needed for a given task. Avoid broad scopes like `access-management` or `provisioning` unless the agent explicitly needs to manage users or workspaces.
- **`unity-catalog` vs individual scopes:** `unity-catalog` covers catalog-level metadata access. For specific operations (e.g., data classification, quality monitoring), the individual scopes (`dataclassification`, `dataquality`) are also required.
- **`clusters` is a common dependency:** Most compute-heavy tasks (jobs, pipelines, command execution, Databricks Connect) require `clusters` even if not obvious.
- **Secrets scope:** `secrets` is required whenever a job or notebook reads a secret via `dbutils.secrets.get()`.
- **Token types:** These scopes apply to both Personal Access Tokens (PATs) and OAuth M2M tokens. For production agents, OAuth M2M with scoped service principals is preferred over PATs.
