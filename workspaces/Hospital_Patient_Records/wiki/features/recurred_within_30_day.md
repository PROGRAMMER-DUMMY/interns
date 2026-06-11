---
contract_ref: workspaces/Hospital_Patient_Records/interns/generated/contracts/workspace_feature_definitions.json#recurred_within_30_day
entity_type: feature
entity_id: recurred_within_30_day
summary: '`recurred_within_30_day` resolved via custom definition: EXISTS (SELECT
  1 FROM "encounters" p WHERE p."PATIENT" = "PATIENT" AND p."Id" <> "Id" AND CAST(p."STOP"
  AS TIMESTAMP) < CAST("START" AS TIMESTA'
tags:
- feature
applies_to_kpis:
- kpi_009
- kpi_010
created: '2026-06-11T13:57:40.098889+00:00'
updated: '2026-06-11T13:57:40.098889+00:00'
last_validated_against_json: '2026-06-11T13:57:40.098889+00:00'
validator_status: ok
---

# Feature: recurred_within_30_day

## Current state

- **Feature**: `recurred_within_30_day`
- **Accepted option**: `custom` — Enter a custom definition
- **Resolution**: custom business definition

> EXISTS (SELECT 1 FROM "encounters" p WHERE p."PATIENT" = "PATIENT" AND p."Id" <> "Id" AND CAST(p."STOP" AS TIMESTAMP) < CAST("START" AS TIMESTAMP) AND CAST("START" AS TIMESTAMP) <= CAST(p."STOP" AS TIMESTAMP) + INTERVAL 30 DAY)

## Decision history

- 2026-06-11T13:57:40.098889+00:00: Accepted `custom` — Enter a custom definition (EXISTS (SELECT 1 FROM "encounters" p WHERE p."PATIENT" = "PATIENT" AND p."Id" <> "Id" AND CAST(p."STOP" AS TIMESTAMP) < CAST("START" AS TIMESTAMP) AND CAST("START" AS TIMESTAMP) <= CAST(p."STOP" AS TIMESTAMP) + INTERVAL 30 DAY)) — Human-confirmed by Shubham (session 2026-06-11): readmission = same PATIENT with a previous encounter ending within 30 days before this START; corrected from the auto-candidate's Id self-join (Id is unique per encounter, can never recur).

## Evidence

- `{'file': 'workspaces/Hospital_Patient_Records/interns/generated/profiles/encounters.csv.profile.json', 'purpose': 'derivation_evidence'}`
- `{'file': 'workspaces/Hospital_Patient_Records/hospital_analytics_questions.sql', 'purpose': 'kpi_source'}`

## Rejected options

### option_a: Accept candidate formula from `recurrence_within_window`

Whether the same Id has a prior event within 30 day(s) before this one (a self-join over the event table).

```json
{
  "derived_column_name": "recurred_within_30_day",
  "source_pattern_id": "recurrence_within_window",
  "business_meaning": "Whether the same Id has a prior event within 30 day(s) before this one (a self-join over the event table).",
  "formula": "EXISTS (SELECT 1 FROM <self> p WHERE p.\"Id\" = \"Id\" AND p.\"START\" < \"START\" AND \"START\" <= p.\"START\" + INTERVAL 30 DAY)",
  "input_columns": [
    {
      "input_name": "entity",
      "column": "Id",
      "dataset": "C:\\Users\\shubh\\OneDrive\\Desktop\\interns\\workspaces\\Hospital_Patient_Records\\encounters.csv",
      "dtype": "String",
      "role": "formula_input",
      "profile_path": "workspaces/Hospital_Patient_Records/interns/generated/profiles/encounters.csv.profile.json",
      "row_count": 27891,
      "observed_values": [
        "d1004dd3-9d75-ffa3-a38f-ed39037c45a0",
        "917cc444-4e05-68c9-60fd-5375a529dcbe",
        "da181dd9-e2a4-c501-d3d2-608ad0364e27",
        "bc6b6bb2-51d1-831d-dd31-a261103ebb30",
        "46cc4bcf-b4b3-59f1-262d-c6a921a41684",
        "5a729897-6e8b-b2cd-a599-0b3347cd9266",
        "60cd2f8b-adc5-4cf4-9c33-5b2e7bde79de",
        "c384abae-a341-e6a6-2a5c-4ef0ed3e3e1d"
      ],
      "value_profile": {
        "sample_values": [
          "d1004dd3-9d75-ffa3-a38f-ed39037c45a0",
          "917cc444-4e05-68c9-60fd-5375a529dcbe",
          "da181dd9-e2a4-c501-d3d2-608ad0364e27",
          "bc6b6bb2-51d1-831d-dd31-a261103ebb30",
          "46cc4bcf-b4b3-59f1-262d-c6a921a41684",
          "5a729897-6e8b-b2cd-a599-0b3347cd9266",
          "60cd2f8b-adc5-4cf4-9c33-5b2e7bde79de",
          "c384abae-a341-e6a6-2a5c-4ef0ed3e3e1d"
        ],
        "sample_min": null,
        "sample_max": null,
        "exact_min": null,
        "exact_max": null,
        "metadata_min": null,
        "metadata_max": null,
        "null_count": null,
        "profile_source": null,
        "note": "Values come from bounded profile evidence, not a full raw-data dump."
      },
      "semantic_meaning_sources": [
        {
          "file": "workspaces/Hospital_Patient_Records/interns/generated/profiles/encounters.csv.profile.json",
          "field": "Id",
          "meaning": "Column `Id` is bound to required derivation input `entity` by schema/profile name matching.",
          "evidence_state": "schema_profile_inferred"
        }
      ],
      "reason": "Used for `entity` because `Id` was the closest available profiled column in `C:\\Users\\shubh\\OneDrive\\Desktop\\interns\\workspaces\\Hospital_Patient_Records\\encounters.csv`. This is candidate evidence, not a business definition.",
      "example_value": "d1004dd3-9d75-ffa3-a38f-ed39037c45a0",
      "evidence_state": "profile_inferred"
    },
    {
      "input_name": "event_time",
      "column": "START",
      "dataset": "C:\\Users\\shubh\\OneDrive\\Desktop\\interns\\workspaces\\Hospital_Patient_Records\\encounters.csv",
      "dtype": "String",
      "role": "formula_input",
      "profile_path": "workspaces/Hospital_Patient_Records/interns/generated/profiles/encounters.csv.profile.json",
      "row_count": 27891,
      "observed_values": [
        "2017-03-03T08:39:12Z",
        "2018-04-23T22:04:02Z",
        "2018-02-14T12:24:02Z",
        "2013-07-12T03:45:15Z",
        "2012-05-05T14:56:58Z",
        "2017-12-01T22:56:14Z",
        "2020-11-17T15:23:00Z",
        "2012-12-08T14:47:36Z"
      ],
      "value_profile": {
        "sample_values": [
          "2017-03-03T08:39:12Z",
          "2018-04-23T22:04:02Z",
          "2018-02-14T12:24:02Z",
          "2013-07-12T03:45:15Z",
          "2012-05-05T14:56:58Z",
          "2017-12-01T22:56:14Z",
          "2020-11-17T15:23:00Z",
          "2012-12-08T14:47:36Z"
        ],
        "sample_min": null,
        "sample_max": null,
        "exact_min": null,
        "exact_max": null,
        "metadata_min": null,
        "metadata_max": null,
        "null_count": null,
        "profile_source": null,
        "note": "Values come from bounded profile evidence, not a full raw-data dump."
      },
      "semantic_meaning_sources": [
        {
          "file": "workspaces/Hospital_Patient_Records/interns/generated/profiles/encounters.csv.profile.json",
          "field": "START",
          "meaning": "Column `START` is bound to required derivation input `event_time` by schema/profile name matching.",
          "evidence_state": "schema_profile_inferred"
        }
      ],
      "reason": "Used for `event_time` because `START` was the closest available profiled column in `C:\\Users\\shubh\\OneDrive\\Desktop\\interns\\workspaces\\Hospital_Patient_Records\\encounters.csv`. This is candidate evidence, not a business definition.",
      "example_value": "2017-03-03T08:39:12Z",
      "evidence_state": "profile_inferred"
    }
  ],
  "example": {
    "example_type": "synthetic_formula_example",
    "input": {
      "Id": "d1004dd3-9d75-ffa3-a38f-ed39037c45a0",
      "START": "2017-03-03T08:39:12Z"
    },
    "output": {
      "recurred_within_30_day": "apply the formula to the example inputs"
    },
    "substituted_formula": "EXISTS (SELECT 1 FROM <self> p WHERE p.\"Id\" = \"Id\" AND p.\"START\" < \"START\" AND \"START\" <= p.\"START\" + INTERVAL 30 DAY)",
    "warning": "Example demonstrates formula mechanics only; it is not workspace ground truth."
  },
  "evidence_sources": [
    {
      "file": "workspaces/Hospital_Patient_Records/interns/generated/profiles/encounters.csv.profile.json",
      "dataset": "C:\\Users\\shubh\\OneDrive\\Desktop\\interns\\workspaces\\Hospital_Patient_Records\\encounters.csv",
      "column": "Id",
      "evidence_type": "profile_schema",
      "evidence": "Input column `Id` is present in generated profile evidence.",
      "evidence_state": "schema_presence_only"
    },
    {
      "file": "workspaces/Hospital_Patient_Records/interns/generated/profiles/encounters.csv.profile.json",
      "dataset": "C:\\Users\\shubh\\OneDrive\\Desktop\\interns\\workspaces\\Hospital_Patient_Records\\encounters.csv",
      "column": "START",
      "evidence_type": "profile_schema",
      "evidence": "Input column `START` is present in generated profile evidence.",
      "evidence_state": "schema_presence_only"
    }
  ],
  "derivation_reasoning": {
    "summary": "Question implies recurrence within 30 day(s) of a prior event; needs a self-join on Id ordered by START. Candidate only -- confirm the window semantics.",
    "why_this_formula": "Question implies recurrence within 30 day(s) of a prior event; needs a self-join on Id ordered by START. Candidate only -- confirm the window semantics.",
    "why_not_ground_truth": "No source artifact explicitly defines this derived column with this formula.",
    "remaining_risk": "Input columns are profile-backed, but the business meaning still needs confirmation.",
    "evidence_state": "candidate_derivation_not_ground_truth"
  },
  "evidence_state": "candidate_derivation_not_ground_truth",
  "confidence": "low",
  "needs_user_confirmation": true
}
```

## Why (user)

<!-- TODO: explain why this option was the right business choice -->

## Business context

<!-- Optional: business rules, vendor quirks, edge cases -->

## Related notes

<!-- [[wiki-links]] to other entity notes -->
