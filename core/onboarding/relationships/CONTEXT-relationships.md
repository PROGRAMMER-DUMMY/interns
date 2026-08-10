# Relationships Package Architecture Context: `core/onboarding/relationships`

This document provides an exhaustive, file-by-file architectural and technical reference for all components in [`core/onboarding/relationships`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships).

---

## Executive Overview & Architectural Model

The `relationships` package forms the semantic backbone of the platform's dataset join and source-selection engine. It replaces basic table-size heuristics with a multi-factor relationship scoring model, builds governed foreign-key relationship contracts (`relationship_contracts.json`), performs schema-alias matching, and produces source-to-target execution plans (`source_to_target_plan.json`).

```
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                   Workspace Evidence                                        │
│               (Profiles, Data Model Docs, Diagram Sidecars, Lexicon, Decisions)              │
└──────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                               │
                                               ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                                    contracts.py                                             │
│                RelationshipContractBuilder / apply_relationship_answer                      │
│                  - Derives & merges relationship evidence (doc, diagram, profile)          │
│                  - Evaluates uniqueness, referential integrity & low cardinality            │
│                  - Emits relationship_contracts.json & relationship_contracts.md            │
└──────────────────────┬───────────────────────────────────────────────┬──────────────────────┘
                       │                                               │
                       ▼                                               ▼
┌─────────────────────────────────────────────┐ ┌─────────────────────────────────────────────┐
│          base_source_selector.py            │ │          schema_alias_matching.py            │
│            select_base_source()             │ │         load_schema_index() / alias_index() │
│  - Graph BFS coverage & fan-out safety      │ │  - Structural suffix & linguistic rules     │
│  - Grain & evidence scoring                 │ │  - Workspace lexicon alias resolution       │
│  - Base-source decision blocker panel       │ │  - Candidate source column profiling        │
└──────────────────────┬──────────────────────┘ └──────────────────────┬──────────────────────┘
                       │                                               │
                       └──────────────────────┬────────────────────────┘
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────┐
│                              source_to_target_planner.py                                    │
│                               SourceToTargetPlanner                                         │
│   - Orchestrates base-source selection, feature mapping, join graph & fan-trap validation   │
│   - Integrates resource management, context routing, and engine evolution recommendations   │
│   - Emits source_to_target_plan.json & source_to_target_plan.md                             │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/__init__.py)

- **Exact Purpose**: Package initialization file for relationship onboarding utilities.
- **Key Functions / Classes**: None (contains package docstring only).
- **Inputs & Outputs**:
  - *Inputs*: None.
  - *Outputs*: None.
- **Failure Modes & Edge Cases**: None.

---

### 2. [`base_source_selector.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py)

- **Exact Purpose**: Graph-based fact/base dataset selector for KPI plans. Replaces naive table-size heuristics with a multi-factor scoring model (coverage, grain, fan-out safety, relationship evidence, profile presence).
- **Key Functions / Classes**:
  - [`CandidateScore`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L58-L91): Dataclass capturing individual dataset candidate scores, coverage ratios, grain matches, fan-out joins, safe joins, and shortest join paths.
  - [`BaseSourceSelection`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L94-L124): Dataclass holding the winning base source, ranked candidates, near-tie flag, tie candidates, and structured rationale.
  - [`select_base_source(refs, profile_map, relationships, grain_dimensions, pinned)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L126-L186): Main entry point. Scores candidate datasets, checks human pinned decisions, flags near-ties (`NEAR_TIE_MARGIN = 0.35`), and ranks candidates.
  - [`_score_candidates(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L188-L220): Computes weighted scores (`_W_COVERAGE=4.0`, `_W_GRAIN=1.0`, `_W_FANOUT_PENALTY=1.5`, `_W_FACT_EDGE=0.25`, `_W_DIRECT_REF=0.2`, `_W_PROFILED=0.25`) and sorts by score, row count, and dataset path.
  - [`_score_coverage_and_fanout(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L222-L267): BFS graph search over executable relationship edges to compute reachability and track one->many row multiplication penalties.
  - [`_score_grain(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L268-L300): Matches KPI grain dimension tokens against candidate schema columns using word boundary matching.
  - [`_executable_edges(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L301-L321): Maps relationship contracts into normalized candidate dataset pairs.
  - [`_shortest_path(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L323-L352): Bidirectional BFS to find shortest edge path between start and goal datasets.
  - [`load_pinned_base_sources(contracts_dir)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L354-L366): Reads recorded human decisions from `pipeline_decisions.json` under key `base_source_decisions`.
  - [`base_source_blocker(kpi_id, selection)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L369-L401): Constructs blocker payload when base selection is near-tied.
  - [`base_source_panel_questions(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L403-L439): Reads `source_to_target_plan.json` and builds structured question panels for ambiguous base selections.
  - [`_base_source_panel_question(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L442-L540): Generates blocker question options with intent facet payload (`facet: base_source`).
  - Local helpers: [`_executable_allowed`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L548-L552), [`_row_count`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L555-L559), [`_schema`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L562-L564), [`_source_group`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L567-L574), [`_strip_function_wrappers`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L577-L580), [`_norm`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L583-L584), [`_norm_path`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L587-L588), [`_rel`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L591-L595).
- **Inputs & Outputs**:
  - *Inputs*: Flat feature source references, dataset profiles (`profile_index.json`), relationship contracts (`relationship_contracts.json`), KPI grain dimensions, and `pipeline_decisions.json`.
  - *Outputs*: `BaseSourceSelection` dataclass, blocker question panels, and structured rationale logs.
- **Failure Modes & Edge Cases**:
  - Empty feature references return `BaseSourceSelection(base_source="", decision_source="no_refs")`.
  - Unreachable datasets are tracked in `unreachable_datasets` without causing exception crashes.
  - Scores within `NEAR_TIE_MARGIN` (0.35) raise `near_tie=True` to trigger human review rather than making an arbitrary choice.

---

### 3. [`contracts.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py)

- **Exact Purpose**: Generates, validates, updates, and persists governed relationship contracts (`relationship_contracts.json` and `relationship_contracts.md`) from multi-source evidence (data model docs, diagram sidecars, profile schemas, and prior human decisions).
- **Key Functions / Classes**:
  - [`RelationshipContractResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L59-L67): Dataclass summarizing contract generation outputs.
  - [`RelationshipApprovalResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L71-L82): Dataclass returning the outcome of applying a user decision to a relationship edge.
  - [`RelationshipContractBuilder`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L84-L277): Main class orchestrating document parsing, diagram sidecar inspection, profile candidate matching, multi-source relationship merging, decision preservation, and output rendering.
  - [`load_relationship_contracts(repo_root, workspace)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L279-L294): Reads and validates `relationship_contracts.json` against `RELATIONSHIP_CONTRACTS_CONTRACT`.
  - [`apply_relationship_answer(repo_root, workspace, relationship_id, answer, ...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L297-L401): Governed CLI runner target (`apply-relationship-answer`). Updates relationship state (`user_confirmed` or `rejected`), updates approval policy, appends decision history, and records decision in `decision_history.md`.
  - [`find_executable_relationship(relationships, left_source, right_source)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L404-L426): Searches for an executable relationship between two datasets, handling left/right swapping if needed.
  - [`_parse_relationships_from_docs(docs, profiles)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L429-L487): Extracts join statements and foreign-key rows from data-model documentation.
  - [`_parse_foreign_key_dictionary_rows(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L490-L609): Parses CSV data dictionary rows for explicit foreign key declarations.
  - [`_qualified_column_refs(text)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L612-L632): Extracts `table.column` qualified references from doc text with slash expansion.
  - [`_parse_documented_table_relationships(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L635-L714): Extracts documented lineage relationships from markdown data-model tables.
  - [`_promote_documented_relationships(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L717-L793): Promotes documented joins to `proven_data_model` when profile key overlap and uniqueness checks pass.
  - [`_profile_relationship_candidates(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L817-L845): Infers candidate relationships from shared column names between dataset profiles.
  - [`_pair_relationships(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L848-L899): Scores shared join key candidates by uniqueness and selects optimal dimension-side join keys.
  - [`_profile_relationship_from_candidate(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L901-L970): Constructs relationship contract dicts from profile candidates, determining dimension key uniqueness and referential integrity.
  - [`_promote_profile_relationships_with_doc_context(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L973-L1014): Promotes profile candidates to `proven_data_model` if corroborated by entity documentation and confirmed uniqueness/RI.
  - [`_relationship(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1099-L1237): Constructs governed relationship contract dictionary with full policy, cardinality, uniqueness, referential integrity, and decision history fields.
  - [`_relationships_from_finalized_model(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1240-L1299): Ingests relationships from `data_model_contract.json` when status is `finalized`.
  - [`_root_fact_dataset(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1302-L1377): In-degree graph algorithm to determine root-fact table among candidate fact tables.
  - [`_relationships_from_diagram_sidecars(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1380-L1533): Consumes parsed image sidecars (`*.model.json`) from `generated/data_model_images/`, applying low-cardinality dimension rules (`LOW_CARDINALITY_DIMENSION_ROWS = 50`).
  - [`_preserve_user_decided_relationships(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1581-L1620): Prevents silent data loss by preserving prior `user_confirmed`, `rejected`, and `proven_data_model` states across rebuilds.
  - [`_merge_relationships(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1623-L1642): Merges relationships from multiple evidence sources by canonical key and state rank (`_state_rank`).
  - Validation & calculation helpers: [`_left_key_resolution_ratio`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1678-L1700), [`_distinct_values`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1703-L1737), [`_column_uniqueness`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1740-L1756), [`_ratio_from_dataset`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1786-L1820), [`_is_non_csv_source`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1837-L1844), [`_uc_skip_reasons`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1846-L1867), [`_shared_join_columns`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1880-L1895), [`_dataset_terms`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1913-L1936), [`_canonical_key`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1968-L1977), [`_relationship_id`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1980-L1987), [`_state_rank`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1990-L1997), [`_render_markdown`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L2044-L2071), [`main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L2101-L2108), [`apply_main`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L2112-L2161).
- **Inputs & Outputs**:
  - *Inputs*: Workspace profiles (`profile_index.json`), documentation files (`docs/`), domain model (`domain_model.json`), image sidecars (`generated/data_model_images/*.model.json`), finalized data model contracts, and CLI user answers (`--answer approve|reject|keep_blocked`).
  - *Outputs*: `relationship_contracts.json`, `relationship_contracts.md`, and updated `decision_history.md`.
- **Failure Modes & Edge Cases**:
  - Non-unique dimension key (`_is_unique_ratio` < 0.99) sets `fan_out_risk=True` and forces `executable=False`.
  - Low referential integrity (`_REFERENTIAL_INTEGRITY_THRESHOLD` < 0.5) marks `referential_integrity_failed` and blocks executable SQL generation.

**Key-overlap evidence on Unity Catalog (2026-08-11).** `_left_key_resolution_ratio` is what separates a real foreign key from two columns that merely share a name, and it read values with `csv.DictReader` -- returning `None` for anything else. On a cloud workspace (Delta tables in UC, no local file) that meant NO join could ever reach `proven_data_model`; every one needed a human to confirm it, and the referential dbt tests generated from those contracts fired only for hand-confirmed joins. The value check silently switched off on the path becoming default.

`_uc_key_overlap_ratio(client, left_fqn, left_col, right_fqn, right_col)` now computes the same ratio for a UC pair **in SQL**, returning ONE ROW:

```sql
SELECT count(*) AS left_distinct, count(r.k) AS resolved
FROM (SELECT DISTINCT `<left>` AS k FROM <left_fqn>) l
LEFT JOIN (SELECT DISTINCT `<right>` AS k FROM <right_fqn>) r ON l.k = r.k
```

Three constraints are deliberate and load-bearing:
- **No value set crosses the wire.** Materializing distinct keys client-side is fine at GB and fatal at TB (a 500M-cardinality key does not fit in memory).
- **No `LIMIT`.** A bounded sample of left keys intersected with a bounded sample of right keys under-counts overlap catastrophically and would report a valid FK as broken. An approximate answer is worse than none here, because this ratio gates whether the join may be used at all.
- **Every undeterminable path returns `None`**, never `0.0` -- "signal absent" is not "no key resolves", matching the CSV path's own contract.

Identifiers go through `assert_safe_identifier` before interpolation: a generated contract is not a trust boundary. The client is passed as a zero-arg **factory** and resolved only inside the UC branch -- `DatabricksClient.is_configured()` costs ~3s, and a pure-local workspace must never pay it (measured at 20s across one test run before this was made lazy).
  - Data loss floor prevents overwriting a populated contract file with an empty list on rebuild.
  - `apply_relationship_answer` checks `confirmed_by`: empty or agent names record `source: agent`, while human names record `source: human`.

---

### 4. [`schema_alias_matching.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/schema_alias_matching.py)

- **Exact Purpose**: Performs domain-agnostic schema alias matching for KPI feature resolution using structural suffix rules and workspace-derived vocabulary from `WorkspaceLexicon`.
- **Key Functions / Classes**:
  - [`load_schema_index(profile_index_path)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/schema_alias_matching.py#L39-L43): Reads `profile_index.json` and delegates to `schema_index_from_profiles`.
  - [`schema_index_from_profiles(profiles)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/schema_alias_matching.py#L46-L87): Builds normalized column index from profiles, including nested leaf fields inside Struct/List types.
  - [`alias_index(schema_index, lexicon)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/schema_alias_matching.py#L90-L111): Builds an alias lookup dictionary from structural rules and `WorkspaceLexicon`.
  - [`source_columns(evidences)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/schema_alias_matching.py#L114-L193): Transforms matched column evidences into physical mapping proof structures with value profiles and sample queries.
  - [`sample_query(evidence)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/schema_alias_matching.py#L196-L200): Generates a 5-row sample SQL query string.
  - [`sample_output(evidence)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/schema_alias_matching.py#L203-L213): Formats observed sample values into mock query output dicts.
  - [`aliases_for_column(column, lexicon)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/schema_alias_matching.py#L216-L246): Derives structural suffix variants (`id`, `code`, `date`, `type`, `status`, `identifier`) and merges lexicon aliases.
  - [`safe_structural_alias(feature, candidates, lexicon)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/schema_alias_matching.py#L248-L260): Checks if a candidate column match is safe to resolve automatically as a structural alias.
  - [`candidate_labels(candidates)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/schema_alias_matching.py#L263-L264): Formats up to 5 top candidate column labels for prompt/display panels.
- **Inputs & Outputs**:
  - *Inputs*: Profile index list, feature evidences, and optional `WorkspaceLexicon` instance.
  - *Outputs*: Schema index dictionary, alias index map, source column proof objects, and sample queries.
- **Failure Modes & Edge Cases**:
  - Non-existent `profile_index.json` returns an empty schema index `{}`.
  - `aliases_for_column` safely suppresses exceptions when interacting with `lexicon.aliases_for_column`.

---

### 5. [`source_to_target_planner.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py)

- **Exact Purpose**: Builds data-model-backed source-to-target plans (`source_to_target_plan.json` and `source_to_target_plan.md`) for KPI implementations across `sql`, `polars`, `pyspark`, or `hybrid` target engines.
- **Key Functions / Classes**:
  - [`SourceToTargetPlanResult`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L42-L55): Dataclass summarizing source-to-target plan build counts and target engine.
  - [`SourceToTargetPlanner`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L58-L200): Main class orchestrating contract loading, resource management (`ResourceManager`), engine recommendation (`EngineEvolutionMemory`), context routing (`ContextRouter`), relationship validation, and plan generation.
  - [`_complexity_engine_recommendations(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L189-L199): Invokes `KPIEngineRecommender` to get complexity-aware engine advice for each KPI.
  - [`_plan_kpi(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L201-L320): Constructs per-KPI source-to-target plan, selecting base source, identifying join requirements, checking fan traps, and establishing medallion layers.
  - [`_plan_feature(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L322-L364): Validates source dataset and column profiling status for each KPI feature.
  - [`_rejected_sources(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L366-L382): Identifies profiled datasets that were not required by KPI feature mappings.
  - [`_join_plan(selected_sources, profiles, relationships)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L447-L492): Builds join plan, finds executable relationship edges, checks connectivity, and evaluates fan-trap risks.
  - [`_fan_trap_risks(executable)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L495-L536): Detects fan-trap / chasm-trap topologies where multiple "many"-side sources join the same "one"-side dimension without a direct relationship edge.
  - [`_relationship_graph_connected(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L539-L561): DFS connectivity check verifying selected source datasets form a single connected component via executable relationships.
  - [`_selected_sources_for_kpi(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L563-L610): Selects base source table via `select_base_source` and collects connected dataset dependencies.
  - [`_grain_from_kpi(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L626-L643): Formats target grain dimensions from KPI cuts or resolved features.
  - [`_temporal_anchor(...)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L646-L657): Identifies date/age/time features requiring temporal anchor confirmation.
  - [`_kpi_is_undefined(kpi)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L730-L736): Helper identifying KPIs with empty metric AND cuts (deferred KPIs).
  - [`_render_markdown(plan)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L675-L727): Renders human-readable `source_to_target_plan.md`.
  - CLI runner: [`main(argv)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L773-L807).
- **Inputs & Outputs**:
  - *Inputs*: `kpi_feature_mapping.json`, `domain_model.json`, `profile_index.json`, `relationship_contracts.json`, target engine (`sql` | `polars` | `pyspark` | `hybrid`), context budget (`small` | `standard` | `deep`).
  - *Outputs*: `source_to_target_plan.json` and `source_to_target_plan.md`.
- **Failure Modes & Edge Cases**:
  - Unsupported `target_engine` raises `ValueError`.
  - Disconnected relationship graph between selected sources generates a `join_proof_missing` blocker.
  - Unresolved features generate `unresolved_features` blockers.
  - Deferred KPIs (empty metric and cuts) are excluded from `blocked_kpi_count` so defined KPIs can proceed.

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**:
  - None identified. All helper functions (`_dataset_terms`, `_qualified_column_refs`, `_root_fact_dataset`, `_fan_trap_risks`) are actively wired and called.
- 🔌 **Unwired Components**:
  - None. CLI commands `plan-source-to-target`, `build-relationship-contracts`, `apply-relationship-answer` are entry points registered in `pyproject.toml`.
- 👯 **Logic & Code Duplication**:
  - `_executable_allowed` is defined in both [`base_source_selector.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L548-L552) and [`contracts.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L2000-L2011). `base_source_selector.py` uses a local fallback rather than importing directly from `contracts.py`.
  - Path normalization helpers (`_norm`, `_norm_path`, `_rel`, `_repo_path`, `_schema`) are duplicated across [`base_source_selector.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/base_source_selector.py#L562-L595), [`contracts.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/contracts.py#L1963-L2084), and [`source_to_target_planner.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/onboarding/relationships/source_to_target_planner.py#L748-L769).
- ⚠️ **Broken References & Mismatches**:
  - None. All imports across Polars, versioning, contract schema validation, and storage layout are valid.
