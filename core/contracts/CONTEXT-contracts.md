# Core Contracts Architecture Context: `core/contracts`

This document provides an exhaustive reference for all components in [`core/contracts`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/contracts).

---

## Executive Overview & Architectural Model

The `core/contracts` package manages schema versioning, schema migration registries, and contract compatibility checks across system artifacts.

---

## File Details

### 1. [`versioning.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/contracts/versioning.py)

- **Exact Purpose**: Defines per-artifact `ContractVersion` schemas, migration registry mappings, and version validation logic.
- **Key Functions / Classes**:
  - [`ContractVersion`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/contracts/versioning.py#L15-L45): Represents semver object for JSON contract payloads.
  - [`migrate_contract(data, target_version)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/contracts/versioning.py#L50-L95): Applies registered migration transformations up to target version.
- **Inputs & Outputs**:
  - *Inputs*: Raw contract JSON dictionary, target semver string.
  - *Outputs*: Migrated contract dictionary matching target version.
- **Failure Modes & Edge Cases**:
  - Raises ValueError if no migration path exists between versions.
