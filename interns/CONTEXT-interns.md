# Interns Architecture Context: `interns`

This document provides an exhaustive reference for all components in [`interns`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns).

---

## Executive Overview & Architectural Model

The `interns` directory implements domain-specialized autonomous intern agents (Code Reviewer, Data Engineer, Insights Analyst, Medallion Architect, Methodology Analyst, SQL Specialist, Validation Specialist).

```
                      ┌─────────┐
                      │ base.py │
                      └────┬────┘
                           │
      ┌────────────────────┼────────────────────┐
      ▼                    ▼                    ▼
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ sql_special │    │ data_engin  │    │ medallion_a │
└─────────────┘    └─────────────┘    └─────────────┘
```

---

## File Details

### 1. [`base.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/base.py)

- **Exact Purpose**: Base abstract class for all specialized intern agents.
- **Key Functions / Classes**:
  - [`BaseIntern`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/base.py#L10-L35): Defines standard `run()`, `process_message()`, and `evaluate_task()` interfaces.

### 2. [`code_reviewer.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/code_reviewer.py)

- **Exact Purpose**: Intern agent specialized in code syntax review, lint checks, and security audits.
- **Key Functions / Classes**:
  - [`CodeReviewerIntern`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/code_reviewer.py#L15-L45): Audits generated code for safety and style.

### 3. [`data_engineer.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/data_engineer.py)

- **Exact Purpose**: Intern agent handling data ingestion, schema alignment, and dataset joins.
- **Key Functions / Classes**:
  - [`DataEngineerIntern`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/data_engineer.py#L15-L45): Formulates transformation logic for datasets.

### 4. [`insights.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/insights.py)

- **Exact Purpose**: Intern agent extracting business insights, trend summaries, and anomaly explanations from query results.
- **Key Functions / Classes**:
  - [`InsightsIntern`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/insights.py#L15-L60): Summarizes dataset results into executive narrative summaries.

### 5. [`medallion_architect.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/medallion_architect.py)

- **Exact Purpose**: Intern agent designing Bronze, Silver, and Gold medallion architecture layers.
- **Key Functions / Classes**:
  - [`MedallionArchitectIntern`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/medallion_architect.py#L20-L80): Formulates medallion layer loading logic.

### 6. [`methodology_analyst.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/methodology_analyst.py)

- **Exact Purpose**: Intern agent reconciling metric definitions against business methodology documents.
- **Key Functions / Classes**:
  - [`MethodologyAnalystIntern`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/methodology_analyst.py#L15-L50): Evaluates business definition fidelity.

### 7. [`sql_specialist.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/sql_specialist.py)

- **Exact Purpose**: Intern agent specializing in CTE optimization, window function tuning, and multi-engine SQL dialect generation.
- **Key Functions / Classes**:
  - [`SQLSpecialistIntern`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/sql_specialist.py#L15-L45): Generates and optimizes analytical SQL queries.

### 8. [`validation.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/validation.py)

- **Exact Purpose**: Intern agent executing test suites, validating contract outputs, and checking artifact completeness.
- **Key Functions / Classes**:
  - [`ValidationIntern`](file:///C:/Users/shubh/OneDrive/Desktop/interns/interns/validation.py#L15-L45): Runs verification suites against workspace outputs.
