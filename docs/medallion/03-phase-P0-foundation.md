# 03 — Phase P0: Foundation (✅ shipped)

## Goal

Stand up the design-time half of the Medallion Architect with the DuckDB substrate. After P0:

- `uv run design-medallion --workspace <ws>` produces a complete `medallion/` directory.
- The deterministic seed-proposal path runs offline (no LLM key needed).
- The LLM-backed path runs when a key is configured.
- `validate-workspace-artifacts` checks the four most important invariants of the generated manifest.
- The unconfirmed star-schema decisions are surfaced via a dedicated design panel.

P0 does **not** include build/execute, Delta target, Governor wiring, KPI regeneration, dynamic model tiering, MLflow, or column-level lineage from SQL parse.

## Prerequisites

- Workspace has been onboarded: `domain_model.json`, `profile_index.json`, `kpi_registry.json` exist under `interns/generated/`.
- KPI feature resolution has run (recommended; not strictly required).

## Files shipped

### Core dataclasses (`core/medallion/*`)

| File | Lines | Responsibility |
|---|---|---|
| `__init__.py` | 47 | Public re-exports |
| `manifest.py` | 232 | `Manifest`, `Bronze/Silver/GoldTable`, `Budget`, `KpiRegeneration`, `compute_inputs_hash`, `manifest_to_yaml` |
| `star_schema.py` | 124 | `StarSchema`, `FactTable`, `DimensionTable`, `Relationship`, `unconfirmed_decisions()` |
| `silver_contract.py` | 192 | `SilverContract`, `TableContract`, `DerivedColumn`, `Assertion`, `TypeCast`, `NullPolicy`, `FormulaTemplates` |
| `lineage.py` | 80 | `Lineage`, `LineageNode`, `LineageEdge`, `trace_to_sources()` |
| `contracts_md.py` | 196 | `render_star_schema_md`, `render_silver_contract_md`, `render_lineage_md` |
| `design.py` | 510 | Orchestrator + DuckDB SQL emission + design panel writer |
| `design_cli.py` | 144 | argparse entrypoint with `--workspace`, `--cheap`, `--dry-run`, `--force`, `--engine`, `--model`, `--json` |

### Agent (`interns/medallion_architect.py`)

LLM-backed intern. Single-shot JSON-schema prompt. The orchestrator falls back to the deterministic seed proposal if `intern.design(context)` raises or returns invalid JSON.

### Integration changes

- `core/agents/registry.py`: added `medallion_architect` to `_BUILTIN_INTERNS`.
- `config/agents.toml`: added to `[interns.healthcare].active`.
- `pyproject.toml`: registered `design-medallion = "core.medallion.design_cli:main"`. Added `pyyaml>=6.0` dependency.
- `core/onboarding/workspace_artifact_validator.py`: added `_validate_medallion_manifest` method (4 checks).

## How to verify (acceptance for P0)

### 1. Help text is discoverable

```bash
uv run design-medallion --help
```

Expected: help text includes the description, all flags with one-line descriptions, three example invocations, and the exit codes table.

### 2. End-to-end on a real workspace

```bash
uv run design-medallion --workspace workspaces/Healthcare-RCM-Data-Platform --cheap --force
```

Expected output (counts may vary if datasets change):
```
bronze_sql_count:      13
silver_sql_count:      7
unconfirmed_decisions: 7
design_panel:          <path>/medallion_design_panel/current.json
```

### 3. Generated artifact integrity

```bash
ls workspaces/Healthcare-RCM-Data-Platform/interns/generated/medallion/
# manifest.yaml, star_schema.json/md, silver_contract.json/md, lineage.json/md, bronze/, silver/

uv run python -c "
import yaml
m = yaml.safe_load(open('workspaces/Healthcare-RCM-Data-Platform/interns/generated/medallion/manifest.yaml'))
assert m['schema_version'] == 1
assert m['target'] == 'duckdb'
assert m['inputs_hash'].startswith('sha256:')
assert len(m['layers']['bronze']) > 0
print('manifest ok')
"
```

### 4. Idempotency cache hit

Re-run without `--force`:

```bash
uv run design-medallion --workspace workspaces/Healthcare-RCM-Data-Platform --cheap
```

Expected:
```
[design-medallion] inputs_hash matches prior manifest — nothing to do.
```

### 5. Validator detects manifest tampering

```bash
uv run python -c "
import yaml
from pathlib import Path
p = Path('workspaces/Healthcare-RCM-Data-Platform/interns/generated/medallion/manifest.yaml')
m = yaml.safe_load(p.read_text())
m['inputs_hash'] = 'sha256:0'*8
p.write_text(yaml.safe_dump(m, sort_keys=False))
"

uv run validate-workspace-artifacts --workspace workspaces/Healthcare-RCM-Data-Platform
```

Expected: at least one error mentioning `inputs_hash` mismatch. Restore the file before continuing.

### 6. Round-trip the dataclasses

```bash
uv run python -c "
from core.medallion import Manifest
import yaml, json
m = yaml.safe_load(open('workspaces/Healthcare-RCM-Data-Platform/interns/generated/medallion/manifest.yaml'))
M = Manifest.from_dict({**m, 'layers': m['layers']})  # adapt shape
print('round trip ok')
"
```

## Known P0 limitations (resolved by later phases)

| Limitation | Resolved in |
|---|---|
| No Delta/Spark SQL emission | P2 |
| No `build-medallion` (cannot actually execute the pipeline) | P1 |
| No Governor routing for Medallion errors | P1 |
| No KPI SQL regeneration against Gold | P1 |
| No PII access restriction at rest; salt not consumed yet | P3 |
| No dynamic model discovery; agent uses whatever `core.config.load()` returns | P4 |
| No content-addressed cache for LLM calls | P4 |
| No vision-OCR for `DataModel.png` | P4 (uses model discovery to find a vision-capable tier) |
| Lineage edges are layer-level placeholders, not column-level | P5 |
| Validator checks 5–8 (Silver contract completeness, PII coverage) | P3 |
| Seed-proposal derived-feature linkage references `ServiceDate` from a different table than the host Silver table | P1 |

The last item is worth flagging in P1: the deterministic seed lifts `kpi_001_age.json`'s derived column into `silver.patient` but its formula references `ServiceDate` which lives in `silver.encounter`. The seed is intentionally minimal; LLM proposals fix this by either materializing `age` in `silver.encounter` or computing it as a join-time projection. P1 wiring of LLM-backed design must produce the correct host table.

## Extension points

If you need to add a new contract or change manifest shape:

1. **Bump `SCHEMA_VERSION`** in `core/medallion/manifest.py`.
2. Update the validator's known-versions list.
3. Add a one-shot migration in `design.py: _read_existing_manifest` that detects the old version and rebuilds from scratch (no in-place migration — the manifest is regenerable).
4. Update the MD renderer in `contracts_md.py`.
5. Add a row to the integration table in `01-architecture.md` and a check to the validator if appropriate.

If you need to add a new task-class that the LLM should handle:

1. Add the task-class name to the prompt template in `interns/medallion_architect.py`.
2. Add a default tier to the per-task-class table in `01-architecture.md` and `07-phase-P4-dynamic-models.md`.
3. Add an output validator (jsonschema) for the new output shape (P4).

If you need to add a new dataclass field to an existing contract:

1. Add as optional with a sensible default (preserve backwards compatibility within `schema_version=1`).
2. Update `from_dict` to handle absence.
3. Update `to_dict` to omit when unset (or always emit with the default — pick one and document it).
4. Update the MD renderer.

## Risks captured

| Risk | Status | Mitigation |
|---|---|---|
| LLM returns invalid JSON | mitigated | `_parse_json` fallback regex; on failure → seed proposal |
| Deterministic seed produces wrong-host derived column | accepted for P0 | LLM path resolves it; tracked above |
| YAML emit drift vs PyYAML reader | mitigated | All emit goes through a single function; round-trip test in this doc §6 |
| Validator and orchestrator drift on the hash-input list | open | P1 work item: factor the list into a single module-level constant `HASHABLE_INPUTS` and import from both sides |
| Windows path separators in artifacts | mitigated | `_safe_relative_posix` everywhere |
| Stale design panel after manifest regenerates | open | Currently the design panel is overwritten on each run; if the user manually resolved some items in the previous panel that no longer apply, they vanish. Acceptable for P0; P1 should preserve resolution history |
