# Core Context Architecture Context: `core/context`

This document provides an exhaustive reference for all components in [`core/context`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/context).

---

## Executive Overview & Architectural Model

The `core/context` package implements semantic document retrieval, context routing, and query relevance scoring over workspace requirement docs and data dictionaries.

---

## File Details

### 1. [`doc_retrieval.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/context/doc_retrieval.py)

- **Exact Purpose**: Retrieves relevant sections from project documents, PDFs, PRDs, and methodology notes based on semantic query matching.
- **Key Functions / Classes**:
  - [`DocumentRetriever`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/context/doc_retrieval.py#L30-L110): Indexes and retrieves relevant document chunks.

### 2. [`doc_retrieval_cli.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/context/doc_retrieval_cli.py)

- **Exact Purpose**: CLI wrapper for testing document retrieval queries against workspace documentation.
- **Key Functions / Classes**:
  - [`main()`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/context/doc_retrieval_cli.py#L15-L45): Executes CLI search queries.

### 3. [`router.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/context/router.py)

- **Exact Purpose**: Context router determining which documentation files, profiles, or evidence graphs should be loaded into prompt context for given intent requests.
- **Key Functions / Classes**:
  - [`ContextRouter`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/context/router.py#L40-L150): Routes intent requests to optimal context payloads while adhering to token budgets.
