# context — audit

## Purpose
`core/context/` is the bounded context/RAG layer over a workspace's derived artifacts. Two
independent capabilities live here:

1. **ContextRouter** (`router.py`) — indexes canonical workspace artifacts
   (`kpi_feature_mapping.json`, `profile_index.json`, relationship contracts, domain model,
   engine/genie/lessons memory, markdown long-memory) into small "pages", scores them against a
   named task, applies a section/byte/token budget, and writes
   `context_index.json` + `context_pages.jsonl` + a per-task manifest JSON + a human wiki `.md`.
   It is derived/read-only: source artifacts stay authoritative.
2. **doc_retrieval** (`doc_retrieval.py`) — a separate, deterministic keyword retriever over the
   repo's INTERNAL `docs/**/*.md`. Builds a manifest from headings + `index.md` "what it is"
   descriptions, scores a query, and returns up to `top_k` heading-bounded excerpts. No embeddings;
   pure token/keyword overlap.

There is no vector/embedding model anywhere — "RAG" here is lexical overlap only, which is a
deliberate determinism choice (good), not a gap.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 6 | Package surface; re-exports router symbols only (not doc_retrieval). | `ContextBudget`, `ContextRouter`, `ContextSelection` |
| `cli.py` | 9 | Thin entrypoint shim mapping `context-router` console script to `router.main`. | `main` (re-export) |
| `router.py` | 699 | Workspace-artifact indexer + budgeted selector + manifest/wiki writer. | `ContextBudget`, `ContextSelection`, `ContextRouter.build/_pages/_json_artifact_pages/_select_pages`, `_task_topics`, `main` |
| `doc_retrieval.py` | 284 | Lexical retriever over `docs/**/*.md`; heading-bounded excerpts. | `build_doc_manifest`, `retrieve_docs`, `_section_for_query`, `_index_keywords_by_file`, `_keywords` |
| `doc_retrieval_cli.py` | 66 | `retrieve-docs` console script wrapping `retrieve_docs`. | `main` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [INTEGRATION] | `doc_retrieval.py:224` / `doc_retrieval_cli.py:44` | `retrieve_docs` is invoked ONLY by its own CLI and `tests/test_doc_retrieval.py`. No agent/prompt-assembly/onboarding code path calls it. The docstring frames it as "an ambiguous model can call `retrieve_docs`", but nothing in the codebase wires it into prompt assembly or an Ask-User/disambiguation flow. Functional but orphaned as a runtime capability. | Wire it into the ambiguity/clarify path (e.g. blocker-panel or resolver) or document it explicitly as a manual operator CLI only. |
| [NOT-PROD] | `router.py:96-145` (esp. 103-104, 431) | `refresh` (`never`/`safe`/`force`) is validated and stamped into the manifest as `refresh_policy`, but has NO behavioral effect. `build()` always re-reads every artifact, recomputes SHA256/mtime, and rewrites index/pages/manifest/wiki regardless. `refresh="never"`/`"safe"` are indistinguishable from `"force"`; there is no staleness short-circuit comparing existing `artifact_sha256`/`artifact_mtime`. | Implement freshness gating: on `never`/`safe`, load the prior manifest, compare per-artifact sha256/mtime, and skip rebuild when unchanged; only `force` rebuilds unconditionally. |
| [DEAD] | `router.py:115,121-123` | `context_pages.jsonl` (full per-page excerpts) is written every build but never read back by any consumer. Downstream (`source_to_target_planner.py:129-130`) carries only `context_manifest`/`context_wiki` paths, and the manifest already embeds `selected_pages` with excerpts. The `.jsonl` dump is write-only. | Either consume `context_pages.jsonl` as the page store the manifest references, or drop the JSONL write to avoid an unread, ever-growing artifact. |
| [BUG] | `router.py:319,329` | `_page` calls `_sha256(path)` and `path.stat()` after the artifact was already shown to exist, but with no try/except. A TOCTOU delete or a permission/race between `path.exists()` (line 182) and the stat/open here raises and aborts the whole build. Markdown pages (`_text_artifact_pages:283`) similarly read with `errors="replace"` but the later `_sha256` can still raise on a vanished file. | Wrap `_sha256`/`stat` in try/except returning sentinel hash/mtime, or skip the page on `OSError`, so one transient file error does not fail the entire context pack. |
| [BUG] | `doc_retrieval.py:139` | The filename regex `` `?([\w./ -]+?\.md)`? `` allows spaces and `/` inside the captured name. On an `index.md` line referencing multiple docs or prose containing "... see the .md ..." it can capture spurious multi-token "filenames", polluting `index_kws` keys. Low impact (keyed by `Path(...).name`), but can mis-attribute keywords across docs whose names share a suffix. | Tighten to a backtick/whitespace-bounded pattern (e.g. anchor on word boundaries, disallow spaces unless backtick-quoted) and validate the captured name resolves to a real manifest path. |
| [DUP] | `doc_retrieval.py:113-117` vs `router.py:640-643` | Two separate `_truncate` implementations with different suffixes (`\n...[truncated]` vs `...[truncated]`) and different tail logic. Same for `_section_index` (router has `_markdown_sections`, doc_retrieval has `_section_index`) — overlapping markdown-section logic re-implemented per module. | Extract shared `_truncate` / markdown-section helpers into one util (e.g. `core/textutil`) to avoid drift. Non-blocking. |
| [MISSING] | `router.py:185,283` | JSON artifacts are read with `encoding="utf-8"` (no `errors=`), so a non-UTF8 byte in a contract aborts the build with `UnicodeDecodeError` (markdown path uses `errors="replace"`, inconsistent). Also `_json_artifact_pages` catches only `json.JSONDecodeError`, not `OSError`, so a read failure on an existing-but-unreadable file is unhandled here (unlike the manifest builder in doc_retrieval which catches `OSError`). | Read JSON with `errors="replace"` for parity and wrap the read in try/except `OSError` -> emit an `invalid_json`-style stub page rather than aborting. |
| [NOT-PROD] | `router.py:636-637` | `_estimate_tokens` uses `len(text)//4`, a crude heuristic that under/over-counts vs real tokenization; budgets are enforced against it. Acceptable for a coarse guard but the manifest presents `selected_estimated_tokens` as if precise. | Document it as an estimate in the manifest field name/comment, or back it with the real tokenizer if budgets become contractual. |

## Cross-package coupling
- **`ContextRouter` IS wired** into prompt/plan assembly via
  `core/onboarding/relationships/source_to_target_planner.py:106-113` (task `plan-source-to-target`,
  `refresh="safe"`), which embeds `context_manifest`/`context_wiki`/`context_budget` into the
  source-to-target plan. This is the one real production consumer.
- Depends on `core.storage.workspace_layout.WorkspaceLayout` for all artifact dirs
  (`contracts_dir`, `profiles_dir`, `memory_dir`, `evidence_dir`, `requirements_dir`, `reports_dir`,
  `generated_dir`) — tightly coupled to that layout's attribute names.
- `doc_retrieval_cli` depends on `core.paths.PROJECT_ROOT`.
- Console scripts registered in `pyproject.toml`: `context-router` -> `core.context.cli:main`,
  `retrieve-docs` -> `core.context.doc_retrieval_cli:main`. `context-router` is referenced in
  `AGENTS.md`, `README.md`, `TOOLS.md`, `.agents/*` skill manifests, and `dashboard.py:3366`
  (artifact browser points at `context_index.json`). `retrieve-docs` has no skill-manifest wiring.
- `__init__.py` exports only router symbols; `doc_retrieval` is reachable only by its full module
  path — reinforces the [INTEGRATION] finding that it is a side capability.

## Verdict
Both modules are clean, deterministic, well-tested in isolation, workspace-agnostic, and free of
secret-leak risk. `ContextRouter` is genuinely integrated and the core selection/budget logic is
sound. Two issues hold it back from "fully production-ready": the **`refresh` flag is inert**
(no staleness gating despite sha256/mtime being computed for exactly that purpose), and the
**unguarded `_sha256`/`stat` in `_page` can abort an entire build on a single file race**.
`doc_retrieval` works correctly but is an **orphaned capability** — nothing in the live agent loop
calls the retriever, so its stated "model pulls the right doc into context" purpose is unrealized.
Net: solid foundation, ship-blocking only on the refresh-no-op and the build-abort-on-file-race;
the dead JSONL write and dup helpers are cleanup.
