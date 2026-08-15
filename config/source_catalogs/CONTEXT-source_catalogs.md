# Config Source Catalogs Context: `config/source_catalogs`

This document provides an exhaustive reference for all components in `config/source_catalogs`.

---

## Executive Overview & Architectural Model

`config/source_catalogs` holds template definitions for external source catalogs (e.g. CMS public data APIs, local file copy ingestion, Databricks Unity Catalog tables).

---

## File Details

### 1. [`cms_public.json`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/source_catalogs/cms_public.json)

- **Exact Purpose**: Defines source catalog templates for public CMS API endpoints, local file copying, and Databricks Unity Catalog table metadata sources.
- **Key Sections**:
  - `cms_dataset_api` ([lines 6-34](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/source_catalogs/cms_public.json#L6-L34)): API template with GET method, offset pagination, rate limits (1 QPS), max rows (5,000), and max bytes (50MB).
  - `local_file_copy` ([lines 35-45](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/source_catalogs/cms_public.json#L35-L45)): Template for copying approved local files into workspace datasets tree.
  - `databricks_uc_table` ([lines 46-56](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/source_catalogs/cms_public.json#L46-L56)): Metadata source template for Databricks Unity Catalog tables.
- **Inputs & Outputs**:
  - *Inputs*: Ingestion commands (`declare-source`, `discover-source`).
  - *Outputs*: Structured source catalog template specifications.
- **Failure Modes & Edge Cases**:
  - Exceeding `max_rows` (5,000) or `max_bytes` (50,000,000) during API intake halts payload fetching.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None.
- 🔌 **Unwired Components**: None.
- 👯 **Logic & Code Duplication**: None.
- ⚠️ **Broken References & Mismatches**: None.
