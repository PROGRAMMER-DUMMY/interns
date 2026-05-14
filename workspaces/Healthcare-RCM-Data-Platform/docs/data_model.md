# Dimensional Data Model (Star Schema)

## Overview

The Gold Layer follows a **Star Schema design** to support efficient querying and reporting for healthcare RCM analytics.

---

## Fact Table: Fact_Transactions

This is the central table capturing financial transactions.

### Key Columns:
- Transaction_ID (PK)
- Patient_ID (FK)
- Provider_ID (FK)
- Dept_ID (FK)
- ICD_Code (FK)
- Encounter_ID
- Claims_ID

### Metrics:
- Charge_Amount
- Payor_Paid_Amount
- Patient_Paid_Amount
- Adjustment_Amount

### Dates:
- Service_Date
- Claim_Date
- Paid_Date

---

## Dimension Tables

### 1. Dim_Patient
- Patient details (name, DOB, gender)
- Surrogate key: SK_Patient_ID
- Tracks data across multiple hospitals

---

### 2. Dim_Provider
- Doctor details
- Linked with department
- Includes NPI reference

---

### 3. Dim_Department
- Department information
- Source-aware (multi-hospital support)

---

### 4. Dim_Diagnosis (ICD)
- Disease classification
- ICD code mapping

---

### 5. Dim_NPI
- Provider registry data from API

---

## Relationships

- Fact table joins all dimensions via foreign keys
- Enables:
  - Patient-level analysis
  - Provider performance tracking
  - Financial reporting

---

## Benefits

- Optimized for analytics queries
- Supports slicing & dicing
- Enables BI dashboards
