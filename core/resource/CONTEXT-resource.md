# Core Resource Architecture Context: `core/resource`

This document provides an exhaustive reference for all components in [`core/resource`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/resource).

---

## Executive Overview & Architectural Model

The `core/resource` package manages compute resource allocation, Databricks cluster/warehouse lifecycle, and memory management.

---

## File Details

### 1. [`cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/resource/cli.py)

- **Exact Purpose**: Command-line tools for inspecting and allocating compute resources.
- **Key Functions / Classes**:
  - [`main()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/resource/cli.py#L10-L30): Dispatches resource management commands.

### 2. [`manager.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/resource/manager.py)

- **Exact Purpose**: Resource manager balancing query concurrency, DuckDB thread count, and warehouse auto-scaling.
- **Key Functions / Classes**:
  - [`ResourceManager`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/resource/manager.py#L20-L80): Allocates memory limits and CPU cores for query workloads.
