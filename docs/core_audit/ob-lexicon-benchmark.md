# ob-lexicon-benchmark — audit

## Purpose
Two small onboarding sub-packages.

- `core/onboarding/lexicon/` builds a per-workspace, evidence-DERIVED vocabulary
  (metric phrases, cut phrases, column aliases) that replaced the old curated keyword
  ladder ("derive don't curate"). It writes a generated contract
  `interns/generated/contracts/workspace_lexicon.json` and exposes an in-memory matcher
  (`infer_metric_and_cuts`, `aliases_for_column`). `vocabulary.py` is a thin loader over a
  separate `workspace_vocabulary.json` research artifact with a tiny generic-seed fallback.
- `core/onboarding/benchmark/` produces a project-native readiness scorecard and release-gate
  status over existing governed artifacts. It is the `prepare-agent-benchmark` console script
  and feeds `project_harness` release-gate checks.

## Files
| Package | File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- | --- |
| lexicon | `__init__.py` | 31 | Public surface re-export | `WorkspaceLexicon`, `build_workspace_lexicon`, `load_workspace_lexicon` |
| lexicon | `builder.py` | 705 | Derive/persist/load the lexicon + matcher | `WorkspaceLexicon`, `MetricPhrase`, `CutPhrase`, `ColumnAliasEntry`, `build_workspace_lexicon`, `load_workspace_lexicon`, `_harvest_from_*`, `_phrases_from_name`, `_dedupe_*` |
| lexicon | `vocabulary.py` | 77 | Loader for `workspace_vocabulary.json` + generic seed fallback | `terms_for`, `vocabulary_confidence`, `vocabulary_present`, re-exported `GENERIC_*_SEED` |
| benchmark | `__init__.py` | 8 | Public surface re-export | `AgentBenchmarkResult`, `AgentBenchmarkScorecardBuilder` |
| benchmark | `agent_benchmark.py` | 684 | Readiness scorecard + release gates + CLI | `AgentBenchmarkScorecardBuilder`, `AgentBenchmarkResult`, `_release_gates`, `_blocker_routes`, `_weighted_score`, `prepare_main` |

## Findings
| Tag | Location (pkg/file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [INTEGRATION] | lexicon/vocabulary.py:17-21 → workspace/research.py:40-47 | `GENERIC_FINANCIAL_SEED` = `amount,cost,price,fee,spend,revenue,income,profit,loss,balance,charge,margin`; `GENERIC_TEMPORAL_SEED` = date/time words; `GENERIC_IDENTIFIER_SUFFIXES` = id/code/key/uuid. These are checked-in word lists used as a fallback AND as live classifiers (research.py:325-345, feature_resolver.py:1241-1246). Generic finance/time English, not a specific domain (no Healthcare-RCM/payer/DRG leakage), so it does not violate workspace-agnosticism — but it IS curated vocabulary baked into source, in tension with "derive don't curate". Borderline-acceptable as a universal seed; flag for governance awareness. | Keep, but document as the single allowed universal seed and assert in a genericity test (one already exists: `tests/test_genericity_audit.py`) that no domain words creep in. |
| [NOT-PROD] | lexicon/builder.py:282,199 (`generated_at`) | `build_workspace_lexicon` stamps `datetime.now(timezone.utc)` into the persisted contract, so byte-identical inputs produce non-identical files. The MATCHER logic itself is deterministic (sorted phrases, sorted aliases), so resolution output is reproducible; only the artifact bytes/timestamp differ. | Acceptable for an audit/provenance field; if bit-reproducible contracts are required, source the timestamp from an injected clock or input fingerprint. |
| [NOT-PROD] | lexicon/builder.py:301, 637 | `load_workspace_lexicon` / `_read_json` catch bare `Exception` and downgrade to a warning + `None`/skip. Robust (missing artifact is not an error, per design) but a corrupt source is silently ignored — a malformed `kpi_registry.json` yields an empty lexicon with no surfaced error. | Differentiate "absent" (silent) from "present-but-unparseable" (warn loudly / count in `stats` so the empty result is explainable). |
| [BUG] | lexicon/builder.py:605-616 `_phrases_from_name` × :385-397 | For every authored `cuts` cell the builder emits the full n-gram phrase set (1–4 tokens) of the KPI name paired with EACH cut value. A long KPI name with multiple cuts produces a large phrase cross-product, and `infer_metric_and_cuts` (`:181-184`) will attach a cut whenever ANY sub-phrase of the name appears in another KPI's text — broad/false-positive cut inference across KPIs that share a common word. | Restrict cut-phrase keys to the cut term itself (or a "by <cut>" pattern) rather than the whole-name n-gram set; or require phrase length ≥2 for cut attribution. |
| [NOT-PROD] | lexicon/builder.py:47-48, 280, 398-401 | `cuts_headers` is harvested from `observed_headers`/`registry_headers` and stored, but `WorkspaceLexicon` never reads it back for matching (only round-tripped in `to_dict`). No consumer of `cuts_headers` was found in the repo grep. | Either wire `cuts_headers` into header detection in the registry parser or drop it from the contract to avoid implying it is used. |
| [MISSING] | benchmark/agent_benchmark.py:683-684 `prepare_main` | `prepare_main` always `return 0` even when `blocked_gate_count > 0`; a CI/release caller relying on exit code cannot detect a blocked release gate from this entrypoint. (The harness path reads the JSON instead, so it is covered there — but the CLI exit code is misleading.) | Return non-zero (e.g. `1`) when `result.blocked_gate_count` > 0, or add a `--strict` flag that does so. |
| [NOT-PROD] | benchmark/agent_benchmark.py:170 | Data-model score falls back to a hardcoded `90` when `path in {final, domain}` and `55` for draft, regardless of actual readiness payload contents. A finalized-but-thin model scores 90 with zero real readiness evidence. | Derive the fallback from concrete model evidence (table/relationship counts, blocker presence) rather than a constant. |
| [NOT-PROD] | benchmark/agent_benchmark.py:585-588 `_ratio_score` | `_ratio_score(x, 0)` returns `100.0`. Used by relationship (`:211`, total==0 short-circuit guards it) and source-to-target/kpi components where a zero-denominator (no KPIs) yields a perfect score, which can inflate readiness for an empty workspace. STT/KPI components guard with `status` checks (`total and blocked==0`) so the gate still blocks, but the numeric `score` is misleading. | Return `0.0` (or `None`/"n/a") for a zero denominator, or only call it when denominator>0. |
| [NOT-PROD] | benchmark/agent_benchmark.py:649-656 `_load_json` | Catches only `json.JSONDecodeError`, not `OSError`/`UnicodeDecodeError`; an unreadable-but-existing artifact would raise and abort `prepare()`. (`builder.py:_read_json` is broader.) | Broaden the except to `(OSError, ValueError)` for parity and resilience. |

## Cross-package coupling
- The two packages do not import each other. They are siblings under `core/onboarding/`.
- Lexicon is consumed by: `workspace/onboarding.py` (builds it at `:738`, infers metric/cuts at
  `:970`), `kpi/feature_resolver.py` (loads once at `:138`, passes to `safe_structural_alias`
  `:338` and `build_alias_index` `:627`; imports `GENERIC_FINANCIAL_SEED` at `:1241`),
  `kpi/text_parser.py` (`infer_metric_and_cuts` wrapper `:47-61`),
  `relationships/schema_alias_matching.py` (`lexicon.aliases_for_column` `:216`), and
  `vocabulary.terms_for` for `filter_terms` in the resolver. This is a real, live feed into
  KPI inference and schema-alias matching — not dead.
- `vocabulary.py` re-exports the seeds from `workspace/research.py`; the research module is the
  true owner and also writes the `workspace_vocabulary.json` that `terms_for` reads.
- Benchmark is consumed by: `pyproject.toml` console script `prepare-agent-benchmark`
  (`:85` → `prepare_main`) and `harness/project_harness.py` (`agent_benchmark` check block,
  release-gate / blocker-route surfacing at `:94,361,408,514,590,601`). It is NOT invoked from
  `workspace/workflow.py` or `run-kpi-pipeline` — it is a separate release-gate step.
- `_harvest_from_document_candidates` couples lexicon to
  `core.onboarding.documents.candidate_apply.merge_accepted_candidates` (import guarded; target
  confirmed to exist). It only attaches human-accepted glossary terms to columns that already
  exist — correctly governed, no column invention.

## Verdict
Both packages are production-wired and broadly sound. The lexicon is genuinely
workspace-derived: an empty workspace yields an empty lexicon, document terms are gated to
human-accepted candidates and existing columns only, and no DOMAIN-specific vocabulary
(healthcare/RCM/payer/DRG) is baked in. The only checked-in word lists are the generic
finance/time/identifier seeds in `workspace/research.py` (re-exported via `vocabulary.py`) — a
universal English seed, not a domain leak, but it should be governance-acknowledged as the one
sanctioned exception to "derive don't curate" and locked by a genericity test. The real
correctness risk is the cut-phrase n-gram cross-product ([BUG], builder.py:385-397) that can
over-attach cuts across KPIs sharing a common word. The benchmark scorecard is correct in shape
and wired into the harness, but has several scoring-fidelity soft spots (constant 90/55 model
fallback, `_ratio_score` returning 100 on zero denominator, `prepare_main` always exiting 0).
None are blockers; recommend fixing the cut-phrase over-generation and the benchmark exit code
before relying on the CLI in CI.
