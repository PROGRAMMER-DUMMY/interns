---
contract_ref: workspaces/Hospital_Patient_Records/interns/generated/contracts/workspace_feature_definitions.json#over_24_hour
entity_type: feature
entity_id: over_24_hour
summary: '`over_24_hour` resolved to derived formula: date_diff(''hour'', CAST("START"
  AS TIMESTAMP), CAST("STOP" AS TIMESTAMP)) >= 24'
tags:
- feature
applies_to_kpis:
- kpi_003
created: '2026-06-11T13:58:16.427630+00:00'
updated: '2026-06-11T13:58:16.427630+00:00'
last_validated_against_json: '2026-06-11T13:58:16.427630+00:00'
validator_status: ok
---

# Feature: over_24_hour

## Current state

- **Feature**: `over_24_hour`
- **Accepted option**: `option_a` — Accept candidate formula from `duration_bucket`
- **Resolution**: derived formula

```sql
date_diff('hour', CAST("START" AS TIMESTAMP), CAST("STOP" AS TIMESTAMP)) >= 24
```

## Decision history

- 2026-06-11T13:58:16.427630+00:00: Accepted `option_a` — Accept candidate formula from `duration_bucket` (derived formula) — Human-confirmed by Shubham (session 2026-06-11): over_24_hour = elapsed encounter duration START->STOP >= 24 hours.

## Evidence

- `{'file': 'workspaces/Hospital_Patient_Records/interns/generated/profiles/encounters.csv.profile.json', 'purpose': 'derivation_evidence'}`
- `{'file': 'workspaces/Hospital_Patient_Records/hospital_analytics_questions.sql', 'purpose': 'kpi_source'}`

## Rejected options

### custom: Enter a custom definition

Provide a different accepted rule for `over_24_hour`.

## Why (user)

<!-- TODO: explain why this option was the right business choice -->

## Business context

<!-- Optional: business rules, vendor quirks, edge cases -->

## Related notes

<!-- [[wiki-links]] to other entity notes -->
