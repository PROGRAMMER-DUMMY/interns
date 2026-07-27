# Local Airflow

Airflow does not run natively on Windows -- it needs POSIX `pwd`/fork -- so the
scheduler runs in Docker even for local development. Pinned to
**apache/airflow:3.3.0**, which is where Assets, `AssetOrTimeSchedule` and asset
partitioning live. Pinning to 2.x would make `stage_assets()` a permanent no-op.

## Start

```bash
cd docker/airflow
cp .env.example .env            # then set AUTORESEARCH_PIPELINE_WORKSPACE
docker compose -f docker-compose.yaml -f docker-compose.override.yaml up -d
```

UI at <http://localhost:8080>. The `astro-airflow-mcp` server already registered
in `.mcp.json` defaults to that address, so an agent can query DAG state
directly once this is running.

## What is mounted, and why

| Mount | Mode | Why |
|---|---|---|
| `../../` -> `/opt/autoresearch` | ro | The repo is mounted, not baked in, so a code change is picked up by re-parsing the DAG instead of rebuilding the image. |
| `../../workspaces` -> same path | rw | Generated artifacts land there. |
| `./dags` | ro | `build_dag()` is a *factory* -- it returns a DAG rather than writing a file -- so `autoresearch_pipeline.py` is the stub Airflow discovers. |

## What is deliberately NOT set

`AUTORESEARCH_ALLOW_REMOTE_EXECUTION` is absent from the compose file on
purpose. Baking it into an image makes it permanent and defeats the gate; it
must be set by a human in the shell that runs the pipeline. A test asserts it
stays out of every non-comment line.

## Backfill

Not `catchup`. `build_dag()` sets `catchup=False`, and a backfill is a separate,
human-confirmed `run-dbt-backfill` invocation with a span bound. Airflow
scheduling a year of catch-up runs unattended is the failure this avoids.
