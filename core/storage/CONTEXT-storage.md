# Core Storage Architecture Context: `core/storage`

This document provides an exhaustive reference for all components in [`core/storage`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage).

---

## Executive Overview & Architectural Model

The `core/storage` package manages file I/O safety, metadata store persistence, external data roots, workspace layout initialization, and cross-platform process locks (`workspace_lock`).

```
┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│  workspace_lock  ├───────►│  atomic_io.py    ├───────►│ metadata_store   │
└──────────────────┘        └──────────────────┘        └──────────────────┘
         │                           │                           │
         ▼                           ▼                           ▼
┌──────────────────┐        ┌──────────────────┐        ┌──────────────────┐
│  workspace.py    │        │ workspace_layout │        │ external_data.py │
└──────────────────┘        └──────────────────┘        └──────────────────┘
```

---

## File Details

### 1. [`atomic_io.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/atomic_io.py)

- **Exact Purpose**: Atomic file writing utility preventing partial write corruption by writing to temporary files before atomic replacement.
- **Key Functions / Classes**:
  - [`atomic_write_file(target_path, content)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/atomic_io.py#L15-L55): Writes content safely with atomic replace.
  - [`atomic_write_json(target_path, data)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/atomic_io.py#L56-L85): Writes formatted JSON atomically.
- **Inputs & Outputs**:
  - *Inputs*: File path, string content or dictionary data.
  - *Outputs*: None (writes file to disk).
- **Failure Modes & Edge Cases**:
  - Cleans up temporary `.tmp` files if writing fails mid-stream.

### 2. [`external_data.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/external_data.py)

- **Exact Purpose**: Manages external dataset roots and mappings outside the main git repository structure.
- **Key Functions / Classes**:
  - [`ExternalDataReader`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/external_data.py#L25-L80): Resolves external data root mappings defined in `config/external_data_roots.json`.
- **Inputs & Outputs**:
  - *Inputs*: External URI or root key.
  - *Outputs*: Normalized local or network file path.
- **Failure Modes & Edge Cases**:
  - Raises error if mapped external path is inaccessible or unmounted.

### 3. [`metadata_store.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/metadata_store.py)

- **Exact Purpose**: Structured metadata persistence engine storing JSON artifacts as Delta tables or JSON fallback stores in `interns/state/`.
- **Key Functions / Classes**:
  - [`MetadataStore`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/metadata_store.py#L30-L110): Interface for getting/setting collection documents.
- **Inputs & Outputs**:
  - *Inputs*: Collection name, document ID, JSON data.
  - *Outputs*: Stored document object.
- **Failure Modes & Edge Cases**:
  - Fallback from Delta storage to JSON files if DuckDB Delta extension is unavailable.

### 4. [`workspace.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/workspace.py)

- **Exact Purpose**: Workspace instance management, status tracking, and metadata operations.
- **Key Functions / Classes**:
  - [`Workspace`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/workspace.py#L30-L120): Represents an active project workspace directory and its state.
- **Inputs & Outputs**:
  - *Inputs*: Workspace root path.
  - *Outputs*: Workspace object.
- **Failure Modes & Edge Cases**:
  - Auto-initializes missing directory structures on first instantiation.

### 5. [`workspace_layout.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/workspace_layout.py)

- **Exact Purpose**: Defines canonical subfolder layout for workspaces under `workspaces/<project>/interns/`.
- **Key Functions / Classes**:
  - [`ensure_workspace_layout(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/workspace_layout.py#L20-L75): Creates mandatory subdirectories (`state/`, `runs/`, `generated/`, `reports/`).
- **Inputs & Outputs**:
  - *Inputs*: Workspace directory path.
  - *Outputs*: Layout directory paths dictionary.
- **Failure Modes & Edge Cases**:
  - Idempotent execution; safe to run repeatedly without modifying existing files.

### 6. [`workspace_lock.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/workspace_lock.py)

- **Exact Purpose**: Cross-platform process mutex ensuring only one agent or CLI command mutates a workspace at a time.
- **Key Functions / Classes**:
  - [`workspace_lock(workspace_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/storage/workspace_lock.py#L25-L95): Context manager acquiring file lock in `interns/state/workspace.lock`.
- **Inputs & Outputs**:
  - *Inputs*: Workspace directory path, timeout.
  - *Outputs*: Context manager yielding lock handle.
- **Failure Modes & Edge Cases**:
  - Fails fast with exit code 2 (`WorkspaceLockTimeout`) if another command holds the lock.
