# dbt Core for Agentic CLI Tools

> Tailored for tools like Claude Code, Gemini CLI, Cursor, and custom LLM-powered CLIs.
> This is NOT a FastAPI/web-app guide — it is about how your agent uses dbt as a tool.

---

## Mental Model

Your app is an agent, not a server:

```
User prompt → Agent Loop → dbt as a TOOL → Warehouse
                                ↕
                    (reads lineage, runs models,
                     inspects tests, queries SQL)
```

dbt is a **tool your agent invokes**, exactly like how Claude Code runs shell commands or
Gemini CLI reads files. The agent decides when and what to run.

---

## Two Integration Patterns

| Pattern | Best For |
|---------|----------|
| **A — dbt MCP Server** | Claude Code / Gemini CLI / any MCP-compatible agent |
| **B — dbtRunner as Agent Tool** | Custom Python agent loop with function calling |

Start with Pattern A if building on an existing agent framework.
Use Pattern B if writing the agent loop from scratch in Python.

---

## Pattern A — dbt Official MCP Server (Recommended)

dbt Labs shipped an official MCP server (April 2025). It is the primary way AI agents interact
with dbt projects in 2025/2026.

### What the Agent Gets

```
Agent (MCP Client)
      ↓
 dbt MCP Server
      ↓
 ┌─────────────────────────────────────────────┐
 │ CLI Tools        │ Discovery Tools           │
 │  dbt_run         │  list_models              │
 │  dbt_build       │  get_model_details        │
 │  dbt_test        │  get_lineage              │
 │  dbt_compile     │  list_sources             │
 │                  │  get_run_results          │
 ├──────────────────┼───────────────────────────┤
 │ SQL Tools        │ Semantic Layer Tools      │
 │  execute_sql     │  list_metrics             │
 │  text_to_sql     │  query_metrics            │
 │                  │  get_dimensions           │
 └─────────────────────────────────────────────┘
```

### Local MCP Setup

```bash
# Install uv (package runner)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Set required env vars
export DBT_PROJECT_DIR="/path/to/your/dbt_project"   # where dbt_project.yml lives
export DBT_PATH="/path/to/.venv/bin/dbt"

# Run the dbt MCP server
uvx dbt-mcp
```

### Wire Into Agent Config

**Claude Code** (`.claude/settings.json`):
```json
{
  "mcpServers": {
    "dbt": {
      "command": "uvx",
      "args": ["dbt-mcp"],
      "env": {
        "DBT_PROJECT_DIR": "/path/to/your/dbt_project",
        "DBT_PATH": "/path/to/.venv/bin/dbt"
      }
    }
  }
}
```

**Gemini CLI** (`~/.gemini/settings.json`):
```json
{
  "mcpServers": {
    "dbt": {
      "command": "uvx",
      "args": ["dbt-mcp"],
      "env": {
        "DBT_PROJECT_DIR": "/path/to/your/dbt_project",
        "DBT_PATH": "/path/to/.venv/bin/dbt"
      }
    }
  }
}
```

**Custom Python MCP client:**
```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import asyncio

server_params = StdioServerParameters(
    command="uvx",
    args=["dbt-mcp"],
    env={
        "DBT_PROJECT_DIR": "/path/to/your/dbt_project",
        "DBT_PATH": "/usr/local/bin/dbt"
    }
)

async def run_agent_with_dbt():
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            # agent now has access to all dbt tools
```

### MCP Tool Reference

| Tool | What It Does |
|------|-------------|
| `dbt_run` | Run models (`--select` supported) |
| `dbt_build` | Run + test in one shot |
| `dbt_test` | Run tests only |
| `dbt_compile` | Compile SQL without executing |
| `dbt_list` | List models matching a selector |
| `list_models` | Discover all models + metadata |
| `get_model_details` | Get model SQL, config, columns |
| `get_lineage` | Upstream/downstream dependency graph |
| `list_sources` | All raw sources + freshness status |
| `execute_sql` | Run raw SQL against the warehouse |
| `text_to_sql` | Natural language → SQL (uses dbt context) |
| `list_metrics` | All Semantic Layer metrics |
| `query_metrics` | Query a metric with dimensions |

> Local MCP supports CLI commands (run, build, test). Remote MCP (dbt Cloud only) supports
> Semantic Layer and Discovery APIs. For a CLI agent working with local projects, use local MCP.

---

## Pattern B — dbtRunner as a Tool Function

For a custom Python agent with function calling (no MCP framework).

### Install

```bash
pip install dbt-core==1.11.11 dbt-postgres   # or your adapter
```

### dbt Tool Definitions

```python
# tools/dbt_tools.py
from dbt.cli.main import dbtRunner, dbtRunnerResult
from pathlib import Path
import json

DBT_PROJECT_DIR = str(Path(__file__).parent.parent / "dbt_project")
DBT_PROFILES_DIR = str(Path(__file__).parent.parent / "dbt_project" / "profiles")
BASE_ARGS = ["--project-dir", DBT_PROJECT_DIR, "--profiles-dir", DBT_PROFILES_DIR]


def dbt_run(select: str = None, full_refresh: bool = False) -> dict:
    args = ["run"] + BASE_ARGS
    if select:
        args += ["--select", select]
    if full_refresh:
        args.append("--full-refresh")
    return _parse_result(dbtRunner().invoke(args))


def dbt_test(select: str = None) -> dict:
    args = ["test"] + BASE_ARGS
    if select:
        args += ["--select", select]
    return _parse_result(dbtRunner().invoke(args))


def dbt_build(select: str = None) -> dict:
    args = ["build"] + BASE_ARGS
    if select:
        args += ["--select", select]
    return _parse_result(dbtRunner().invoke(args))


def dbt_list_models(select: str = None) -> dict:
    args = ["list"] + BASE_ARGS
    if select:
        args += ["--select", select]
    result = dbtRunner().invoke(args)
    return {"success": result.success, "models": list(result.result) if result.result else []}


def dbt_get_lineage(model_name: str) -> dict:
    manifest = _load_manifest()
    nodes = manifest.get("nodes", {})
    target_key = next((k for k in nodes if nodes[k].get("name") == model_name), None)
    if not target_key:
        return {"error": f"Model '{model_name}' not found in manifest"}
    node = nodes[target_key]
    return {
        "model": model_name,
        "depends_on": node.get("depends_on", {}).get("nodes", []),
        "refs": [r[1] for r in node.get("refs", [])],
        "sources": [f"{s[0]}.{s[1]}" for s in node.get("sources", [])],
    }


def dbt_get_last_run_results() -> dict:
    results_path = Path(DBT_PROJECT_DIR) / "target" / "run_results.json"
    if not results_path.exists():
        return {"error": "No run results found. Run dbt_run first."}
    with open(results_path) as f:
        data = json.load(f)
    return {
        "elapsed_time": data.get("elapsed_time"),
        "results": [
            {"node": r["unique_id"].split(".")[-1], "status": r["status"],
             "message": r.get("message", "")}
            for r in data.get("results", [])
        ],
    }


def _parse_result(result: dbtRunnerResult) -> dict:
    if result.exception:
        return {"success": False, "error": str(result.exception)}
    return {
        "success": result.success,
        "results": [{"node": r.node.name, "status": str(r.status)}
                    for r in (result.result or [])],
    }


def _load_manifest() -> dict:
    manifest_path = Path(DBT_PROJECT_DIR) / "target" / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError("manifest.json not found. Run 'dbt parse' or 'dbt compile' first.")
    with open(manifest_path) as f:
        return json.load(f)
```

### Wire Tools Into Agent Loop (Claude API)

```python
# agent.py
import anthropic
import json
from tools.dbt_tools import dbt_run, dbt_build, dbt_list_models, dbt_get_lineage, dbt_get_last_run_results

TOOLS = [
    {
        "name": "dbt_run",
        "description": "Run dbt models. Use this to execute transformations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "select": {"type": "string", "description": "dbt selector e.g. 'tag:daily' or '+fct_orders'"},
                "full_refresh": {"type": "boolean", "description": "Force full rebuild of incremental models"}
            }
        }
    },
    {
        "name": "dbt_build",
        "description": "Run and test dbt models together. Preferred over dbt_run for production.",
        "input_schema": {"type": "object", "properties": {"select": {"type": "string"}}}
    },
    {
        "name": "dbt_list_models",
        "description": "List available dbt models. Use before running to confirm model names.",
        "input_schema": {"type": "object", "properties": {"select": {"type": "string"}}}
    },
    {
        "name": "dbt_get_lineage",
        "description": "Get upstream/downstream lineage for a model.",
        "input_schema": {
            "type": "object",
            "properties": {"model_name": {"type": "string"}},
            "required": ["model_name"]
        }
    },
    {
        "name": "dbt_get_last_run_results",
        "description": "Get results of the last dbt run. Use after a run to check for failures.",
        "input_schema": {"type": "object", "properties": {}}
    },
]

TOOL_MAP = {
    "dbt_run": dbt_run, "dbt_build": dbt_build,
    "dbt_list_models": dbt_list_models, "dbt_get_lineage": dbt_get_lineage,
    "dbt_get_last_run_results": dbt_get_last_run_results,
}

client = anthropic.Anthropic()

def run_agent(user_prompt: str):
    messages = [{"role": "user", "content": user_prompt}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(block.text)
            break
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = TOOL_MAP[block.name](**block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
        messages.append({"role": "user", "content": tool_results})
```

---

## Project Layout

```
your_agent_cli/
├── agent.py
├── tools/
│   ├── dbt_tools.py
│   ├── file_tools.py
│   └── sql_tools.py
├── dbt_project/
│   ├── dbt_project.yml
│   ├── profiles/profiles.yml       # env-var based, no hardcoded creds
│   ├── models/
│   │   ├── staging/
│   │   ├── intermediate/
│   │   ├── marts/
│   │   └── feature_store/
│   └── target/                     # manifest.json, run_results.json
├── .env                            # never commit
└── pyproject.toml
```

---

## Model Selector Patterns

```python
dbt_run(select="tag:feature_store")          # run only feature store
dbt_run(select="+fct_user_activity")         # model + all upstream
dbt_run(select="fct_user_activity+")         # model + all downstream
dbt_run(select="marts/finance")              # everything in a folder
dbt_run(select="state:modified+")            # only changed models
dbt_run(select="result:error+")              # re-run failures + dependents
```

---

## Manifest Pre-loading (Session Performance)

Pre-parse the manifest once at session start to avoid 5–15s re-parse overhead per tool call:

```python
from dbt.cli.main import dbtRunner

class AgentSession:
    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self._manifest = None
        self._dbt = None

    def initialize(self):
        result = dbtRunner().invoke(["parse", "--project-dir", self.project_dir])
        self._manifest = result.result
        self._dbt = dbtRunner(manifest=self._manifest)

    def run(self, select: str = None) -> dict:
        args = ["run", "--project-dir", self.project_dir]
        if select:
            args += ["--select", select]
        return self._dbt.invoke(args)
```

---

## CLAUDE.md Context File for dbt Projects

Add a `CLAUDE.md` at the project root to give the agent persistent dbt project context:

```markdown
# dbt Project Context

## Warehouse
PostgreSQL on localhost. Schema: `analytics`. Dev target only — NEVER run against prod.

## Model Layers
- staging/      → raw source cleaning, prefix: stg_
- intermediate/ → business logic joins, prefix: int_
- marts/        → final analytics tables, prefix: fct_ / dim_
- feature_store/→ ML feature tables, prefix: feat_

## Key Tags
- tag:daily         → runs in nightly batch
- tag:feature_store → ML pipeline models
- tag:critical      → alert on failure

## Safety Rules
- Always use --select; never run dbt build with no selector
- Never use --full-refresh unless explicitly asked
- Run dbt_test after every dbt_run
- Check lineage before modifying a model used by others
```

---

## What the Agent Can Do

```
"Run the daily feature store pipeline"
→ dbt_build(select="tag:daily")

"Why did the user_lifetime_value model fail?"
→ dbt_get_last_run_results()
→ dbt_get_lineage("user_lifetime_value")
→ diagnose and suggest fix

"What models depend on the raw.events source?"
→ dbt_list_models(select="source:raw.events+")

"Show what breaks if I drop stg_orders"
→ dbt_get_lineage("stg_orders")
→ explain downstream impact
```

---

## Pattern Selection Guide

| Situation | Use |
|-----------|-----|
| Building on Claude Code / Gemini CLI / Cursor | dbt MCP Server |
| Custom Python agent with function calling | dbtRunner tools |
| Need Semantic Layer / governed metrics | Remote dbt MCP (dbt Cloud) |
| Agent reads lineage/metadata only | Read `manifest.json` directly or MCP Discovery tools |
| Agent needs to modify dbt models | dbtRunner compile + file tools |

---

## What NOT To Do

| Mistake | Why |
|---------|-----|
| `dbt run` with no selector from agent | Rebuilds everything; slow and costly |
| Prod warehouse credentials in agent | One bad run corrupts prod |
| Not pre-loading manifest for session agents | 5–15s re-parse overhead per call |
| Running dbtRunner concurrently in same process | Global Python variable collisions |
| Agent decides `--full-refresh` freely | Catastrophic on TB-scale tables |
| No `CLAUDE.md` / agent context file | Agent makes wrong selector choices |

---

## References

- [dbt Official MCP Server Docs](https://docs.getdbt.com/docs/dbt-ai/about-mcp)
- [Available MCP Tools](https://docs.getdbt.com/docs/dbt-ai/mcp-available-tools)
- [dbt Agent Skills (Feb 2026)](https://docs.getdbt.com/blog/dbt-agent-skills)
- [dbt Programmatic Invocations](https://docs.getdbt.com/reference/programmatic-invocations)
