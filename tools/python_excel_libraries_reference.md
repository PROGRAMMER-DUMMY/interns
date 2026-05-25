# Python Excel Libraries - Industry Standard Reference

Use this reference when choosing how to read or write Excel artifacts in this repo, especially KPI registries and stakeholder-facing workbooks.

## Decision Table

| Scenario | Library |
|---|---|
| Read clean flat Excel into a dataframe | `polars` or `pandas` when allowed at a boundary |
| Read merged cells / colored rows / styled headers | `openpyxl` |
| Read legacy `.xls` files | `xlrd` via a dataframe boundary |
| Generate formatted Excel reports | `xlsxwriter` |
| Need formula results, not formula strings | `xlwings` |
| Automate Excel like a macro | `xlwings` |
| Large files | dataframe reader with chunking or streaming where supported |
| Write and read same file with styles | `openpyxl` |

## Library Notes

### pandas

Use for flat, clean tabular data, ETL pipelines, and data analysis workflows when dataframe boundary use is acceptable.

Can read/write `.xlsx`, `.xls`, `.csv`, `.ods`, read multiple sheets, skip rows, set headers, select columns, and chunk CSV reads.

Limitations:
- Does not preserve merged-cell semantics directly.
- Does not expose cell styles, colors, or formatting.
- Does not evaluate Excel formulas.

### openpyxl

Use for `.xlsx` files with merged cells, colored rows, styled headers, complex layouts, or when modifying existing workbooks without losing styles.

Can read/write merged cells, inspect formatting, unmerge and forward-fill values, add charts/images/conditional formatting, and modify existing workbooks.

Limitations:
- Does not read legacy `.xls`.
- Does not evaluate formulas; it reads formula strings or cached values depending on load mode.
- Loads full files into memory.

### xlrd

Use only for legacy `.xls` files from older enterprise systems.

Limitations:
- Does not read modern `.xlsx`.
- Read-only.
- Limited formatting support.

### xlsxwriter

Use for writing new formatted `.xlsx` reports, dashboards, charts, conditional formatting, validation, autofilters, and freeze panes.

Limitations:
- Write-only.
- Cannot modify existing workbooks.

### xlwings

Use only when Excel must be automated directly, formula-evaluated values are required, or existing VBA/macros/pivot refreshes must run.

Limitations:
- Requires Excel installed.
- Not suitable for headless/server/cloud/Docker workflows.

## Repo Guidance

- Treat original KPI registry workbooks as authoritative source artifacts for business question, metric, grain/cuts, filters, and source wording.
- Use `openpyxl` when KPI registry layout depends on merged cells, continuation rows, colors, or styled headers.
- Use `xlsxwriter` for generated presentation workbooks.
- Keep dataframe use aligned with the repo rule: prefer Polars; use pandas only when a third-party API requires it and keep conversion at the boundary.
- Do not treat Excel source wording as proof of executable joins, temporal anchors, or derived formulas unless the workbook explicitly defines them.
