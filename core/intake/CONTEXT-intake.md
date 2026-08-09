# Core Intake Architecture Context: `core/intake`

This document provides an exhaustive reference for all components in [`core/intake`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake).

---

## Executive Overview & Architectural Model

The `core/intake` package implements cloud-native data intake workflows (`declare-source` -> `discover-source` -> `prepare-intake-panel` -> `prepare-blueprint`).

---

## File Details

### 1. [`cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/cli.py)

- **Exact Purpose**: Entry point for intake CLI subcommands (`declare-source`, `discover-source`, `prepare-intake-panel`, `apply-intake-answer`).
- **Key Functions / Classes**:
  - [`main()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/cli.py#L20-L75): Parses arguments and dispatches intake commands.

### 2. [`declaration.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/declaration.py)

- **Exact Purpose**: Manages source declaration contracts for cloud-native data sources (JDBC, S3, ADLS, Unity Catalog).
- **Key Functions / Classes**:
  - [`declare_source(workspace_dir, source_config)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/declaration.py#L25-L80): Validates and persists source declaration.

### 3. [`discovery.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/discovery.py)

- **Exact Purpose**: Discovers schemas, tables, and column metadata from declared external sources.
- **Key Functions / Classes**:
  - [`discover_source_metadata(declaration)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/discovery.py#L40-L150): Connects to source and inspects data structures.
  - `_group_paths(root, entries, uri_prefix="")`: one table per data file at the root, one per directory below it; Delta dirs collapse to their table root. `_split_distinct_entities` then re-splits a directory whose files are DIFFERENT entities rather than shards of one (F6) — a folder of `patients.csv`/`transactions.csv` must not union into one bronze table.
  - **`DiscoveredTable.path` is a REAL location, and consumers depend on that.** `generate-ingestion` writes it verbatim into `COPY INTO ... FROM '<path>'`. A split entity is therefore keyed by the file's `name` (with extension), not its `stem` — keying on the stem emitted `.../departments` for an object named `departments.csv` and every job failed `[PATH_NOT_FOUND] ... SQLSTATE 42K03` on the live warehouse (F22). Table naming is independent: it takes `Path(key).stem` downstream either way.
  - Scans are metadata-only (`content_read_policy: metadata_and_paths_only`) — names, paths, formats, byte sizes, modification times. No file is ever opened, which is why CSV header presence cannot be derived here (see F23 in `docs/plans/rcm_replay_findings.md`).

### 4. [`documents.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/documents.py)

- **Exact Purpose**: Processes intake documentation, requirements PDFs, and data dictionaries.
- **Key Functions / Classes**:
  - [`process_intake_documents(doc_paths)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/documents.py#L20-L75): Extracts semantic rules from uploaded intake docs.

### 5. [`domain_detect.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/domain_detect.py)

- **Exact Purpose**: Automatically detects business domain (e.g. healthcare/RCM, finance, supply chain) from dataset schema terminology.
- **Key Functions / Classes**:
  - [`detect_business_domain(columns)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/domain_detect.py#L15-L60): Scores schema columns against `domain_vocabulary.json`.

### 6. [`interview.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/interview.py)

- **Exact Purpose**: Interactive intake interview engine driving question panels and recording user answers.
- **Key Functions / Classes**:
  - [`prepare_intake_interview_panel(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/intake/interview.py#L35-L130): Renders intake question panels (`current.json` / `current.md`).
