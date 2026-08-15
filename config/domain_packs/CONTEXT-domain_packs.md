# Config Domain Packs Context: `config/domain_packs`

This document provides an exhaustive reference for all components in `config/domain_packs`.

---

## Executive Overview & Architectural Model

`config/domain_packs` contains documentation (`_README.md`) regarding domain pack configurations. As noted in `_README.md`, this static JSON domain pack design was deprecated and superseded by dynamic vocabulary derivation from workspace evidence (`core/onboarding/kpi/text_parser.py` -> `lexicon.infer_metric_and_cuts`).

---

## File Details

### 1. [`_README.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/domain_packs/_README.md)

- **Exact Purpose**: Documents the historical domain pack JSON schema, priority rules, match clause fields, and deprecation notice explaining the shift to workspace evidence derivation.
- **Key Sections**:
  - Deprecation Notice ([lines 3-11](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/domain_packs/_README.md#L3-L11)): Explains that hand-curated domain packs were superseded by `core/onboarding/lexicon/`.
  - Pack Shape Schema ([lines 21-60](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/domain_packs/_README.md#L21-L60)): Documents expected JSON structure (`metric_rules`, `cut_rules`, `business_column_aliases`).
  - Priority Conventions ([lines 84-90](file:///C:/Users/shubh/OneDrive/Desktop/interns/config/domain_packs/_README.md#L84-L90)): Priority ranges (1-99 for domain overrides, 900+ for generic fallbacks).
- **Inputs & Outputs**:
  - *Inputs*: None (documentation file).
  - *Outputs*: None.
- **Failure Modes & Edge Cases**:
  - Adding `*.json` packs here will have no effect on onboarding because the loader was superseded.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: ⚠️ The curated domain pack mechanism described in `_README.md` is deprecated and un-wired in code.
- 🔌 **Unwired Components**: None.
- 👯 **Logic & Code Duplication**: None.
- ⚠️ **Broken References & Mismatches**: None.
