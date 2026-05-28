# Domain Packs

Domain packs replace the hardcoded keyword inference that previously lived in
`core/onboarding/kpi/text_parser.py` and the column-alias dictionary in
`core/onboarding/relationships/schema_alias_matching.py`.

Each pack is a JSON file in this directory. The loader (`core/onboarding/domain_packs.py`)
reads every `*.json` here and combines the rules. The `--domain <name>` flag on
KPI commands narrows the set to `<name>.json` plus `generic.json`.

## Pack shape

```jsonc
{
  "name": "rcm",
  "description": "Revenue Cycle Management / Healthcare",

  // First metric rule that matches wins. Lower priority numbers fire first.
  "metric_rules": [
    {
      "priority": 10,
      "match": { "contains_any": ["amount paid", "paid amount"] },
      "metric": "amount paid"
    }
  ],

  // All cut rules that match fire and append (deduped). Priority controls order.
  "cut_rules": [
    {
      "priority": 10,
      "match": { "contains_any": ["lob", "line of business"] },
      "cut": "LOB",
      "branches": [
        { "if_text_contains": "medicare", "cut": "LOB = Medicare" },
        { "if_text_contains": "commercial", "cut": "LOB = Commercial" }
      ]
    }
  ],

  // Column-name aliases used by schema-alias matching. Key is normalized
  // (lowercase, alphanumeric). Values are the substrings KPI features might
  // use that should map to this physical column.
  "business_column_aliases": {
    "paidamount": ["paid", "amountpaid", "amount"]
  },

  // KPI registry header variants this domain typically uses (lowercase).
  "cuts_headers": ["cuts with drg consolidated"]
}
```

## Match clause fields

All optional; rule matches if every present clause is satisfied.

- `contains_any` (list of strings): rule fires if ANY substring appears in the lowercased text.
- `contains_all` (list of strings): ALL substrings must appear.
- `not_contains_any` (list of strings): rule does NOT fire if any of these appear.
- `regex_any` (list of regex patterns): rule fires if ANY regex matches (case-insensitive).

## Branches (cut rules only)

Use when one matched rule should produce different cut values depending on
sub-clauses. First branch whose `if_text_contains` is present wins; if no
branch matches, the rule-level `cut` is used as the default.

## Adding a new domain

1. Copy `generic.json` to `<your-domain>.json`.
2. Add metric rules at priorities 100-499; cut rules at 100-499.
3. Add column aliases for any business-term to physical-column mappings.
4. No code changes required.

## Priority conventions

- 1-99: highly domain-specific overrides (RCM healthcare-specific terms).
- 100-499: domain-specific rules.
- 500-899: cross-domain rules (e-commerce + healthcare overlap, etc.).
- 900+: truly generic fallbacks (Time, Age, etc.).
