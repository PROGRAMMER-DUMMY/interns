# Airflow does not run natively on Windows (it needs POSIX pwd/fork), so the
# scheduler lives in a container even for local development.
FROM apache/airflow:3.3.0

USER root
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
USER airflow

# Cosmos turns the generated dbt project into real Airflow tasks. Without it
# core/orchestration/airflow_dag.py falls back to a plain BashOperator running
# `dbt build` as a subprocess -- which works, but loses per-model task
# granularity and retries.
RUN pip install --no-cache-dir "astronomer-cosmos>=1.9" "dbt-databricks>=1.11,<2"

# The repo is MOUNTED at /opt/autoresearch (see docker-compose.override.yaml)
# rather than baked in, so a code change is picked up by re-parsing the DAG
# instead of rebuilding the image.
ENV PYTHONPATH=/opt/autoresearch
