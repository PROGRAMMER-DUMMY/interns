# Docker Architecture Context: `docker`

This document provides an exhaustive reference for all components in [`docker`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docker).

---

## Executive Overview & Architectural Model

The `docker` directory contains containerization manifests, Dockerfiles, and Docker Compose configurations for running local Airflow, Astronomer Cosmos, and service environments.

---

## Subdirectories & Context Maps

- [`airflow/`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docker/airflow): Airflow container environment setup.

---

## File Details

### 1. [`airflow/Dockerfile`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docker/airflow/Dockerfile)

- **Exact Purpose**: Builds custom Airflow container image equipped with Databricks CLI, dbt-databricks, and astronomer-cosmos.

### 2. [`airflow/docker-compose.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docker/airflow/docker-compose.yaml)

- **Exact Purpose**: Compose specification launching Airflow webserver, scheduler, postgres metastore, and triggerer.

### 3. [`airflow/docker-compose.override.yaml`](file:///C:/Users/shubh/OneDrive/Desktop/interns/docker/airflow/docker-compose.override.yaml)

- **Exact Purpose**: Local developer overrides mounting workspace volumes and dbt profiles into the container.
