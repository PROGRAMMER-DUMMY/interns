# ob-relationships — audit

## Purpose
Infers governed join/relationship (FK) contracts between workspace datasets from
four evidence sources (data-model docs, dictionary CSV rows, parsed diagram
sidecars, finalized data-model contract, and raw profile column overlap),
gates each edge for executability via cardinality / dimension-key uniqueness /
referential-integrity checks, records human-vs-agent provenance on approvals,
selects the base (fact) source that anchors each KPI's FROM clause via a
relationship-graph score, and emits a per-KPI source-to-target plan that the
SQL/Polars/PySpark generators and the local warehouse consume. Human-gate
provenance (BUG-014) and fan-trap avoidance are the safety-critical concerns.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 2 | Package docstring only | — |
| `base_source_selector.py` | 588 | Score referenced datasets as base/fact candidates; flag near-ties; emit panel questions | `select_base_source`, `_score_candidates`, `_score_coverage_and_fanout`, `_shortest_path`, `base_source_blocker`, `base_source_panel_questions`, `load_pinned_base_sources`, `CandidateScore`, `BaseSourceSelection` |
| `contracts.py` | 2043 | Build/approve relationship contracts; uniqueness/RI gating; merge evidence | `RelationshipContractBuilder`, `apply_relationship_answer`, `find_executable_relationship`, `_relationship`, `_promote_documented_relationships`, `_profile_relationship_candidates`, `_pair_relationships`, `_relationships_from_diagram_sidecars`, `_root_fact_dataset`, `_left_key_resolution_ratio`, `_column_uniqueness`, `_merge_relationships`, `_preserve_user_decided_relationships` |
| `schema_alias_matching.py` | 239 | Structural + lexicon alias index for feature resolution | `aliases_for_column`, `alias_index`, `schema_index_from_profiles`, `source_columns`, `safe_structural_alias`, `sample_query`, `sample_output` |
| `source_to_target_planner.py` | 749 | Build per-KPI source-to-target plan (sources, joins, grain, medallion, blockers) | `SourceToTargetPlanner`, `_plan_kpi`, `_selected_sources_for_kpi`, `_join_plan`, `_relationship_graph_connected`, `_grain_from_kpi` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | base_source_selector.py:283-287 | `_score_grain` substring match `token_norm in column or column in token_norm` is bidirectional and unanchored: grain token `id` matches any column ending/containing `id` (e.g. `paid`, `void`, `district`); short tokens inflate `grain_ratio` and can flip the base-source pick. Same loose substring risk as the alias finding. | Require exact normalized equality, or gate substring matches on a minimum token length (e.g. >=4) and word/suffix boundaries. |
| [BUG] | base_source_selector.py:153-165 (`pinned`) | When a KPI base is pinned by a human decision, the pinned dataset is honored only if it is already in the referenced `datasets` set. A legitimate human override to a dataset NOT in the current refs is silently dropped and the score-based pick wins, defeating the human gate. | If `pinned` does not match a ref, still set `base_source=pinned` (or emit a blocker) rather than discarding the recorded decision. |
| [BUG] | contracts.py:500-509 | `foreign key to the <name>` fallback hard-codes the target key column as `Id` (`_resolve_column("Id", ...)`). A dimension whose PK is `party_key`/`code` yields no right_column and the documented FK is dropped, or resolves to an unrelated `Id` column if one exists. | Resolve the right column from the documented text, the FK column stem, or the dimension's unique key, not a literal `"Id"`. |
| [BUG] | contracts.py:1733-1748 (`_shared_join_columns`) | Profile-candidate pairing only considers shared columns whose normalized name ends with `id`/`code`. Real keys named `*_key`, `*_no`, `*_num`, `*_ref`, or natural keys (email, npi) are never proposed as profile relationships, so valid joins are missed unless documented elsewhere. | Broaden the key-name heuristic (add `key`/`no`/`num`/`ref`/`nbr` suffixes) or fall back to uniqueness-driven detection independent of name suffix. |
| [BUG] | schema_alias_matching.py:207-213 | `aliases_for_column` suffix loop is a near-no-op / mildly wrong: for each of `id/code/date/type/status` it strips the suffix and re-appends the SAME suffix, producing the original string (no real variant). The intended cross-suffix expansion (`*_id` <-> `*_code`) never happens, so the "structural alias" promise is largely unmet while the `id`/`identifier` pair (210-213) does fire. | Either generate genuine cross-suffix variants (strip suffix, emit base + each other suffix) with explicit risk gating, or delete the dead loop and document that only id/identifier is structural. |
| [BUG] | contracts.py:1564-1586 (`_left_key_resolution_ratio`) | RI ratio compares stripped string values across sides; for numeric keys read from CSV one side may carry `"100"` and another `"100.0"`/leading zeros, yielding artificial 0% resolution -> a valid join is marked `referential_integrity_failed` and blocked. Only CSV is read; Parquet/other always return None (RI silently skipped). | Normalize numeric/typed key values before set intersection; extend distinct-value reads beyond CSV (or document that non-CSV skips RI). |
| [NOT-PROD] | contracts.py:1672-1706, 1589-1623 | Uniqueness and RI fall back to a FULL bounded CSV `DictReader` scan per column on every build, with no row cap, sampling bound, or size guard. On large local datasets this is O(rows x columns x dataset-pairs) and can dominate build time / memory. | Cap rows read (reservoir/first-N with a documented bound) and reuse a single pass per dataset rather than re-reading per column/pair. |
| [BUG] | schema_alias_matching.py:214-218 | `lexicon.aliases_for_column` failures are swallowed by a bare `except Exception: pass`. A malformed lexicon silently degrades alias coverage with no diagnostic, so resolution regressions are invisible. | Narrow the except and at minimum log/record the lexicon failure. |
| [INTEGRATION] | source_to_target_planner.py:114-117, 238-249 | Planner consumes `load_relationship_contracts` + `find_executable_relationship`; emits `join_proof_missing` when multiple sources are not relationship-graph-connected. Confirmed feeding `sql_generator.py`, `polars_generator.py`, `pyspark_generator.py`, `local_warehouse.py`. Wiring is sound. | None — verified live. |
| [INTEGRATION] | contracts.py:276, 2014; provenance.py:50-62 | BUG-014 provenance is correctly centralized: `decision_source_for(confirmed_by)` maps empty/agent-token identities to `source: agent` and only real names to `human`, applied in both `apply_relationship_answer` and the CLI metadata. flow.py mirrors the same `_decision_source`. Provenance recording is correct. | None — verified correct. |
| [INTEGRATION] | base_source_selector.py:395-431; blocker_question_panel.py:129-162 | `base_source_panel_questions` is consumed by the blocker panel and routed through `intent_facet` -> `record_intent_answer` -> pipeline_decisions.json, then re-honored via `load_pinned_base_sources`/`pinned`. End-to-end near-tie -> human-pin loop is wired. | None — verified live (but see pinned [BUG] above). |
| [INTEGRATION] | contracts.py:1853-1864 vs validation.py:1169-1174 | `_executable_allowed` here and `_relationship_executable` in the validator are intentionally identical (state in executable set AND `allowed_in_sql_generation is True`). Parity is maintained; the `user_confirmed`-history validator gate (validation.py:606) is scoped to `state==user_confirmed`, so builder-emitted `proven_data_model` edges do not falsely trip it. | None — parity confirmed; keep the two in sync if either changes. |
| [DUP] | contracts.py:1927-1932, base_source_selector.py:575-580, source_to_target_planner.py:693-714 | `_norm`, `_norm_path`, `_rel`, `_repo_path`, `_source_group`, `_schema` are copy-pasted across all three modules (the comment "mirror contracts.py semantics" acknowledges it). Divergence risk: `_source_group` in selector/planner differs from `contracts._dataset_group`. | Extract shared path/normalization helpers into one module (e.g. `relationships/_paths.py`) and import. |
| [MISSING] | contracts.py:1029-1037, 1372-1383 | Fan-trap (two facts joined through one shared dimension producing a many-to-many double-count) is NOT detected. Edges are gated only per-pair on dimension-key uniqueness + RI; a multi-source plan that chains fact->dim<-fact is allowed if each pair is individually valid. The base-source fan-out scoring (selector) only penalizes one->many traversal, not the classic fan-trap topology. | Add a plan-level chasm/fan-trap check in `_join_plan` (flag when two non-dimension sources share a dimension and both are on the many side). |
| [MISSING] | contracts.py:374-426 | The free-text `joins ... on COL` regex (line 383) emits `proven_data_model` at confidence 0.92 WITHOUT routing through the uniqueness/RI gate that documented/profile/diagram edges get (it calls `_relationship` with no `dimension_key_unique`/`referential_integrity_ratio`, so both default None -> gating skipped). A documented sentence join becomes executable on text alone. | Run the same `_column_uniqueness`/`_left_key_resolution_ratio` gate for regex-parsed doc joins as for documented-table joins, or emit them as `documented_data_model` for promotion. |
| [BUG] | contracts.py:1830 (`_canonical_key`) | `tuple(sorted([left, right])[0] + sorted([left, right])[1])` concatenates two 2-tuples into a 4-tuple but re-sorts on every index; works but is fragile and recomputes `sorted(...)` twice. If `left == right` (self-pair, already guarded upstream) ordering is undefined. | Compute `pair = sorted([left, right])` once and return `(*pair[0], *pair[1])`. |

## Cross-package coupling
- **Downstream consumers (verified):** `core/onboarding/kpi/{sql,polars,pyspark}_generator.py`,
  `local_warehouse.py` all delegate base-source selection and read
  `source_to_target_plan.json` / `relationship_contracts.json`. The selector
  docstring's claim of being the single FROM-clause source of truth holds.
- **Provenance:** `core/governance/provenance.py` is the shared BUG-014 authority;
  `apply_relationship_answer` and flow.py both route through it. Correct.
- **Validation parity:** `core/onboarding/workspace/validation.py` re-implements
  `_relationship_executable` and the summary recount; must stay byte-aligned with
  `contracts._executable_allowed` / `_recompute_summary`.
- **Panel loop:** `core/onboarding/kpi/blocker_question_panel.py` and the
  intent-facet/pipeline_decisions path close the near-tie human-decision loop.
- **Contracts/versioning:** `register_contract` for `relationship_contracts.json`
  and `source_to_target_plan.json`; artifact contracts validated on load.
- **Inputs:** profile_index.json, domain_model.json, data_model_contract.json,
  diagram sidecars (`generated/data_model_images/*.model.json`), data-model docs.
- No `[DEAD]` exports found — every public symbol is referenced by tests and/or
  downstream packages.

## Verdict
Architecturally strong and notably governance-aware: provenance (BUG-014) is
correctly centralized and verified human-vs-agent; per-edge uniqueness and
referential-integrity gating with a tri-state (True/False/None) "preserve prior
gating when unknown" discipline is the right conservative model; diagram-intent
never overrides observed data quality; near-tie base selection escalates to a
human panel instead of silently picking. However it is **not fully
production-ready** as-is. The highest-impact gaps are: (1) loose substring
matching in grain scoring and the broken structural-suffix alias loop (correctness
+ silent false negatives), (2) the free-text `joins on COL` regex marking edges
executable WITHOUT the uniqueness/RI gate, (3) no plan-level fan-trap detection
(only per-pair fan-out), (4) the hard-coded `"Id"` target column and `id`/`code`-only
shared-key heuristic dropping legitimate joins, and (5) unbounded full-CSV scans
per column/pair as a scale risk. Fix the executable-without-gate regex path and
the fan-trap gap first (data-corruption class), then the alias/grain matching
correctness, then performance. Provenance and validator parity need no change.
