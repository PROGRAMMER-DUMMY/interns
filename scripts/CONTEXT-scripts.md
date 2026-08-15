# Scripts Architecture Context: `scripts`

This document provides an exhaustive reference for all components in [`scripts`](file:///C:/Users/shubh/OneDrive/Desktop/interns/scripts).

---

## Executive Overview & Architectural Model

The `scripts` directory contains cross-platform shell and PowerShell wrappers for injecting environment variables and launching commands.

---

## File Details

### 1. [`with-env.ps1`](file:///C:/Users/shubh/OneDrive/Desktop/interns/scripts/with-env.ps1)

- **Exact Purpose**: PowerShell wrapper script loading environment variables from `.env` and executing child commands.
- **Inputs & Outputs**:
  - *Inputs*: `.env` file path, command string to execute.
  - *Outputs*: Child process output.

### 2. [`with-env.sh`](file:///C:/Users/shubh/OneDrive/Desktop/interns/scripts/with-env.sh)

- **Exact Purpose**: POSIX shell wrapper script exporting `.env` variables before launching commands.
- **Inputs & Outputs**:
  - *Inputs*: `.env` file path, command string.
  - *Outputs*: Executed process output.
