# wiki — audit

## Purpose
`core/wiki/` auto-generates a per-workspace human-readable Markdown wiki that annotates the
JSON contract pipeline. JSON contracts stay the source of truth; wiki notes carry the *why*,
evidence, decision history, lineage, and `[[wiki-links]]` cross-links that prose captures
better than structured fields. The package writes two entity-note kinds — **feature notes**
(on `apply-kpi-panel-answer`, via `blocker_workflow._write_feature_wiki_note`) and **KPI
completion notes** (on `workspace-flow complete`, via `flow.py`). Each note splits into
machine-owned sections (refreshed every pass) and human-owned sections (`Why (user)`,
`Business context`, `Related notes` — preserved verbatim across upserts). It is read back at
panel time (`blocker_question_panel._prior_wiki_decision`) to surface prior decisions.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 29 | Public surface re-exporting layout/reader/writer | `WikiLayout`, `WikiNote`, `build_feature_scaffold`, `build_kpi_completion_scaffold`, `upsert_feature_note`, `upsert_kpi_note`, `read_feature_note` |
| `layout.py` | 54 | On-disk path/folder conventions + slug | `WikiLayout` (frozen dc), `slugify` |
| `lineage.py` | 268 | Derives lineage/decisions/cross-links from contracts; extracts result-shaping SQL | `find_contracts_dir`, `collect_lineage`, `render_lineage_section`, `collect_decisions`, `render_decision_section`, `collect_related_links`, `extract_result_shaping`, `_provenance_phrase` |
| `reader.py` | 93 | Parses frontmatter + `##` sections; exposes human fields | `WikiNote` (frozen dc), `read_note`, `read_feature_note`, `_split_frontmatter`, `_split_sections`, `_strip_todo` |
| `template.py` | 43 | Canonical section names + machine/human boundary + TODO markers | `MACHINE_SECTIONS`, `HUMAN_SECTIONS`, `ALL_SECTIONS`, `WHY_TODO_MARKER`, `empty_section`, `is_machine_section`, `is_human_section` |
| `writer.py` | 447 | Builds scaffolds, upserts notes preserving human sections, renders frontmatter+body | `build_feature_scaffold`, `upsert_feature_note`, `build_kpi_completion_scaffold`, `upsert_kpi_note`, `_render_note`, `_render_kpi_note`, history/evidence/rejected renderers |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [BUG] | writer.py:440 (`_render_note`), writer.py:315 (`_render_kpi_note`) | On rewrite, the renderer iterates only the fixed `ALL_SECTIONS` / `KPI_MACHINE+HUMAN` lists. Any human-added section whose heading is NOT one of the known names (e.g. a user adds `## Open questions` or `## Vendor notes`) is read into `existing.sections` but then silently DROPPED on the next regeneration — real data loss of human edits. Contradicts the module docstring ("human-owned sections preserved verbatim"). | Append any `existing.sections` keys not in the canonical order after the known sections, or emit a warning. Only the 3 named human sections are currently safe. |
| [BUG] | reader.py:76-87 (`_split_sections`) | Section bodies are split purely on `^## ` regex. A fenced code block inside a machine section that contains a line starting with `## ` (e.g. a SQL comment `## note`, or markdown in an evidence/error blob) will be mis-parsed as a new section heading, fragmenting the note and corrupting round-trip. `extract_result_shaping` emits SQL that could contain such lines. | Track fenced-code state (```` ``` ````) while scanning and ignore `##` inside fences. |
| [BUG] | writer.py:386-391 (`_parse_history`) + writer.py:118/296 | Decision-history round-trip keeps only lines starting with `- `. A multi-line history entry (none today, but `evidence_note` is free text and could wrap) or any indented continuation is dropped on the next upsert. History is also append-only with no dedup: re-running `apply-kpi-panel-answer` for the same option appends a duplicate identical bullet each time. | Dedup identical trailing entries before append; preserve continuation lines. |
| [INTEGRATION] | flow.py:753 (`build_kpi_completion_scaffold(kpi_id=..., entry=entry)`) | The KPI-completion hook does NOT pass `project_root`. Lineage then falls back to `find_contracts_dir(Path(entry['sql_path']))`, but `sql_path` is repo-RELATIVE (`flow.py:1331` uses `_rel(sql_file, repo_root)`). `find_contracts_dir` walking a relative path resolves correctly only when cwd == repo root; under any other cwd lineage/decisions/cross-links silently degrade to "_No lineage recorded._" while still claiming `validator_status: ok`. Tests always pass an explicit `project_root`, so this path is untested. | Pass `project_root=self.workspace` at flow.py:753 (the workspace root is already known) so resolution is cwd-independent. |
| [NOT-PROD] | writer.py:244 (`validator_status`) + frontmatter `last_validated_against_json` | `last_validated_against_json` is set to `now` and `validator_status` to `"ok"` whenever `status == "ok"`, but nothing in the wiki path actually runs the validator — the field asserts a validation that did not occur. Misleading provenance in a governed control plane. | Source these from the real validator result or rename to `last_written`. |
| [NOT-PROD] | flow.py:756 / blocker_workflow.py | KPI note write is wrapped in broad `except Exception` and recorded as a step, which is acceptable. But `upsert_feature_note` in `blocker_workflow` has no such guard visible at the call site (line 217) — a malformed panel/option dict raising mid-apply could abort `apply-kpi-panel-answer` after the contract was already written, leaving wiki and contract out of sync. | Wrap the feature-note upsert in try/except and record a non-fatal step, mirroring flow.py. |
| [BUG] | layout.py:49-53 (`slugify`) | Slug collision risk: `feature/foo`, `feature\foo`, and `feature foo` all collapse to `feature_foo` and map to the same note file. Two distinct entity IDs differing only by non-word chars clobber each other (last writer wins, machine sections overwritten). No collision detection. | Append a short hash of the raw id when the slug differs from the id, or store original id and detect collisions. |
| [BUG] | lineage.py:239-243 (`_RESULTS_VIEW_RE`) | The result-view regex is `.*?` non-greedy up to the first `;`. A `;` inside a string literal or inside the view body (e.g. nested statement, quoted value) truncates the captured shaping body early, producing an incomplete/invalid SQL snippet in the note. | Parse to statement boundaries or at least tolerate quoted `;`; acceptable for previews but flag. |
| [MISSING] | layout.py / whole package | No source-note writer exists. `source_note_path` is defined (layout.py:41) and the docstring advertises `kpis/features/sources`, but nothing ever calls `upsert_source_note` — sources notes are never generated, so dataset `[[wiki-links]]` from KPI lineage point at files that are never created (dangling links). | Either add a source-note writer or drop the unimplemented `sources` advertising and the dangling `[[dataset]]` links. |
| [MISSING] | lineage.py `_wikilink` / writer cross-links | `[[name]]` links are emitted with no verification that a target note file exists. Feature links resolve (feature notes are written), relationship and dataset links generally do not (no relationship/source notes). Link integrity is unchecked end-to-end. | Add a link-resolution/integrity check or only emit links to entity kinds that have writers. |
| [DEAD] | template.py:37-42 (`is_machine_section`, `is_human_section`) | Neither helper is referenced anywhere in the package or repo; the writer hard-codes its own `KPI_MACHINE_SECTIONS`/`KPI_HUMAN_SECTIONS` tuples instead of using `template.py`'s. | Remove the unused helpers or route the writer through them to avoid drift. |
| [DUP] | writer.py:124-136 vs template.py:5-16 | `writer.py` redeclares `KPI_MACHINE_SECTIONS` / `KPI_HUMAN_SECTIONS` independently of `template.py`'s `MACHINE_SECTIONS` / `HUMAN_SECTIONS`. Feature notes use `template.ALL_SECTIONS`; KPI notes use the local copies. Two sources of truth for the section contract — a future rename in one place silently desyncs the reader's human-preservation logic. | Centralize all section-name tuples in `template.py` and import. |
| [DUP] | writer.py:302-321 (`_render_kpi_note`) vs writer.py:428-446 (`_render_note`) | Two near-identical frontmatter-ordering + section-rendering functions differing only by section list and title prefix. | Extract one `_render(frontmatter, sections, title, section_order)`. |

## Cross-package coupling
- `core/onboarding/kpi/blocker_workflow.py` — calls `build_feature_scaffold` + `upsert_feature_note` (the `apply-kpi-panel-answer` hook). Wired and exercised.
- `core/onboarding/workspace/flow.py` — calls `build_kpi_completion_scaffold` + `upsert_kpi_note` at KPI completion (the `workspace-flow complete` hook). Wired but OPT-IN: gated behind `AUTORESEARCH_WIKI=1` / `AUTORESEARCH_SIDE_OUTPUTS=1` (flow.py:732-746). By default the KPI wiki is NOT written (step recorded as `skipped`). The docstring/CLAUDE framing implies default-on; the dashboard is default-on but the wiki is not.
- `core/onboarding/kpi/blocker_question_panel.py` — reads notes back via `read_feature_note`/`WikiLayout` to inject `prior_decision_wiki` into the panel. Read path is live.
- `core/onboarding/memory/wiki_memory.py` + `benchmark/agent_benchmark.py` — a SEPARATE "wiki memory" reuse-card subsystem (`prepare-wiki-memory`, `state/team_memory/wiki_memory_index.json`). Despite the shared "wiki" name it does NOT consume `core/wiki` notes; the entity-note wiki and the reuse-memory wiki are independent.
- `core/onboarding/workspace/cleanup.py` — deletes the `<workspace>/wiki/` tree on workspace removal; consistent with `WikiLayout.wiki_root`.
- Hard dependency on `pyyaml` (reader + writer); `re`, `json`, `pathlib`, `datetime` only otherwise. No Polars/DuckDB coupling. Lineage reads `source_to_target_plan.json`, `relationship_contracts.json`, `pipeline_decisions.json` best-effort.

## Verdict
**Partially production-ready.** The core idea is sound and the happy-path round-trip is tested:
the 3 named human sections (`Why (user)`, `Business context`, `Related notes`) are correctly
preserved across regeneration, machine sections refresh, and missing contracts degrade
gracefully without crashing. Encoding is consistently UTF-8 on every read/write, and JSON/YAML
parse failures fall back instead of raising.

But it is NOT safe against the realistic human-edit cases the feature exists to support:
(1) any human-added section with a non-canonical heading is silently dropped on the next
upsert — the headline data-loss bug; (2) `##` inside fenced SQL/error blobs mis-splits the
parser; (3) the flow hook omits `project_root`, making KPI lineage cwd-dependent and effectively
untested; (4) `[[wiki-links]]` to datasets/relationships are dangling because no source/relationship
note writers exist; (5) `validator_status: ok` is asserted without running a validator. Plus
section-name contract is duplicated across `template.py` and `writer.py` (desync risk) and history
is append-only with no dedup. Recommend: fix the unknown-section drop and the fenced-code split
before relying on human edits, pass `project_root` in flow, and unify the section tuples.
