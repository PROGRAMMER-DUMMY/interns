# Cost Ledger — agent-token capture (Phase 1a)

Companion to `phase-0-gap-matrix.md` (§Scope decisions, Phase 1a) and the grading
standard `modern-data-engineering-report.md` (§1a, Recommendations Stage 1). This
records the design so the capture points, join key, and the deferred verification
are not re-litigated or quietly forgotten.

## The load-bearing constraint

A child `uv run` process **cannot read its parent agent's token counters.** The
launched platform is ~95 independent console-scripts (`pyproject.toml:47-143`),
each a child of the driving CLI agent (Claude Code / Codex / Gemini). The agent's
token accounting lives in the agent process, exported out-of-band via its native
telemetry. No amount of scoping changes this; it is the same wall
`CLIEngine` hits in the other direction (`core/agents/llm_engine.py:84-88` captures
subprocess stdout only, never a token count).

So capture is **two mechanically different points**:

1. **Anchor** (in-process, synchronous) — write a correlation row per stage that
   makes agent tokens *joinable later*. It does not, and cannot, contain them.
2. **Ingest** (out-of-band, async) — collect the agent CLIs' native OTel export
   and fill the null token columns by joining on `agent_session_id`.

**Known alternative, recorded so it is not rediscovered:** the *only* way to get a
synchronous in-process token count is a gateway (LiteLLM / Helicone) in front of
the model, on the request path. That is a larger architectural commitment than
Phase 1 makes. The two-point model is the deliberate choice.

## Phase split

- **1a.1 (this deliverable) — anchor + schema + liveness + tests.** Fully in-repo,
  deterministic, green-gated. Produces a ledger that is *joinable but honestly
  empty*: every row carries `capture_method: anchor_only`, `cost_source:
  unreconciled`, and null tokens. Nothing renders a cost number.
- **1a.2 (pending) — live ingest.** Stand up an OTLP sink, verify Gemini's
  API-vs-Vertex token split per endpoint, fill the null columns. Gated on the
  byte-match below.

## Capture points (evidence)

| Point | Where | Evidence |
|---|---|---|
| Anchor seam | sequential pipeline runner | `core/orchestration/dagster_defs.py` `run_pipeline` — one anchor per stage, written *before* the stage runs |
| Stage dimension | stage keys | `core/orchestration/pipeline_stages.py:28` (`onboard`…`dashboard`) |
| Agent-native session id | environment | `CLAUDE_CODE_SESSION_ID` (live-verified exposed to subprocesses 2026-07-18) |
| Agent detection | environment | `CLAUDECODE`/`CLAUDE_CODE_ENTRYPOINT` (verified), `CODEX_SANDBOX` (report-backed) |
| Configured fallback | env/lock | `AUTORESEARCH_MAIN_AGENT` (`core/agents/cli_inspector.py:201`), `cfg.main_agent` (`core/config.py:106`) |
| Ledger store | per-workspace JSONL + `.md` | `<ws>/interns/reports/cost_ledger/{entries.jsonl,current.md}` |

## The two findings that shaped the schema

**Finding A — agent detectability.** The platform used a *declared* agent
(`cfg.main_agent`, default `"claude-code"`), never a detected one. Detection *is*
available from env markers — strong for Claude Code, report-backed for Codex,
**absent for Gemini CLI**. Resolution is env-detect → config → `"unknown"`, and
`agent_detection_source` records which fired. A Gemini run honestly carries
`agent_detection_source: config`; that beats a fabricated marker and is testable
in 1a.2 when someone drives with it.

**Finding B — the join key that doesn't join (the one that mattered).** The
platform's own `session_snapshot` id is `sha256(workspace|tool|now)[:10]`
(`tools/session_snapshot.py:1025-1028`) — an invented digest that never joins
against anything the agent emits. Anchored on it, 1a.1 would have shipped
complete, tested, and green, with rows that could never be matched to a token
count — discovered only in 1a.2 with the collector already standing up. The real
join key is the **agent-native** session id from the environment. The schema
demotes `platform_session_id` to a secondary correlation field and anchors on
`agent_session_id`.

## Schema (v1)

Every token/cost column is nullable and empty at anchor time — that nullability is
what makes surfacing the ledger later **purely additive**. Full field list:
`core/observability/cost_ledger.py` `AnchorEntry`. Key/agent/session fields:

```
run_id, workspace_id, pipeline_stage        # keys
agent_session_id, agent_session_source      # OTel join key (env-native)
platform_session_id                         # secondary correlation only
agent, agent_detected, agent_configured, agent_detection_source
input/output/cached/thinking_tokens = null  # 1a.2 fills; exclusive buckets
cost_usd = null, cost_source = unreconciled, capture_method = anchor_only
provider = null, model = null, endpoint_note = ""
```

## Liveness gate

`liveness_check` (`cost_ledger.py`) fails a run when **either**:
- it produced **zero anchors** — capture is not live (the `assert_installed()`
  analogue; `assert_ledger_active` raises); or
- **unattributable anchors exceed 10%** of the run. Unattributable is defined
  precisely: `agent_session_id` empty **and** `agent_detection_source == "none"`
  — a row that can never join a token count and whose agent is a pure guess.

**Why 10%, and why a gate not a count.** The grading report's Stage-1 advance gate
is "every agent run and every pipeline has attributable cost with **<10%
UNATTRIBUTED**" (`modern-data-engineering-report.md`, Recommendations §Stage 1.1;
Benchmarks: ">10% → invest in governed tags before anything else"). Reusing that
exact bar keeps the ledger's liveness threshold identical to the platform's own
stated FinOps bar rather than inventing a number. The report's warning is that
weak attribution accumulates *quietly* until the ledger is worthless — a bare
count gets ignored, a failing gate does not. A softer join-coverage signal
(`without_session_id`: rows with no session id regardless of detection) is
**reported but not gated** in 1a.1, because config-only attribution (Gemini) is
legitimate until 1a.2 verifies its markers.

## Coverage boundary

> Anchor coverage is currently the `run_pipeline`/`pipeline-run` seam only.
> Individually-invoked `uv run <command>` calls emit no anchors; their spend is
> invisible to both the ledger and the liveness gate. An empty ledger therefore
> does not mean zero spend.

**Measured coverage (2026-07-18), not inferred.** Method: counted `uv run`
invocations across the 25 recorded session event files
(`.agents/sessions/**/events.jsonl`) plus workspace run logs.

| Path | Invocations |
|---|---|
| Seam (`run-kpi-pipeline` / `pipeline-run`) | **1** |
| Direct (individual `uv run <stage>`) | **95+** — `workspace-dashboard` ×56, `medallion design` ×21, `onboard-workspace` ×10, `resolve-kpi-features` ×8 (a floor; excludes `medallion build`, `run-kpi-execution-harness`, …) |

Roughly **1:95** — the anchored seam observes about **1%** of real invocations;
~99% of agent spend is currently invisible to the ledger. This is why coverage
(1a.2a) precedes live ingest (1a.2b): OTLP ingest against 1% coverage reconciles
almost nothing, and is undebuggable — a missing anchor is indistinguishable from a
failed join.

And note the gate structurally **cannot** catch this: the liveness gate fails a run
that anchored badly, but a run that never anchors at all produces no rows to fail
on. So the coverage gap is invisible from inside the ledger — same family as the
five systemic-pattern instances, one layer up (a mechanism whose *absence* on a
path can't trip the mechanism that guards it). Closing it means anchoring the
individually-invoked commands, not tightening the gate. Whether that is a 1a.2
follow-on or 1a.2's main event depends on the measured seam-vs-direct ratio (see
the phase-0 handoff) — if most real usage bypasses the seam, coverage is the phase,
not a footnote to it.

## Anchoring individually-invoked commands (1a.2a)

Coverage (§Coverage boundary) is closed by an **A2 shared decorator**:
`@anchored("<command>")` (`core/observability/cost_ledger.py`) on each console-script
entry-point function. It sets `__anchored__` — a fact the coverage test reads **at
import time**, not by AST-guessing a call or invoking the command — and writes one
honest-empty anchor per call, resolving `--workspace` from argv. Anchor-write
failures surface to stderr (`[cost-ledger] …`), never swallowed — the CLI analogue
of the seam's `_anchor_errors`; a decorated-but-silently-failing command writes
nothing and is then caught by the liveness gate (zero rows). A registry-driven
coverage test enumerates `[project.scripts]`, imports each target, and asserts it is
`__anchored__` **or** on the exemption list (with a required reason, no stale
entries) — so coverage cannot decay back toward 1% by attrition.

**Run grouping.** Individually-invoked commands are separate processes with no
shared parent, so `run_id_for(session, workspace)` derives a **stable** id from
`(agent_session_id, workspace_id)` (no time component) — every command in one
session against one workspace collapses into a single run instead of fragmenting
into one run per process. When the session id is absent (Finding A: weak for
Gemini), it degrades to a unique time-based mint. `run_id_source`
(`pipeline_seam` | `session_workspace` | `degraded_time`) records which derivation
produced each id — the seam's time-based mint and the decorator's session-derived
id are deliberately **not** unified, so they stay distinguishable instead of
inferred from format.

### Exemption list — how it is derived (the durable artifact, not the five entries)

The exemption rule is **structural**: a command is exemptable when it takes no
`--workspace` (nothing to key a ledger row to) or is out-of-launch-scope / dev-meta
tooling. But the first attempt to *derive* the list mechanically — "does the entry
point's module contain a literal `--workspace`" — **misclassified `medallion` and
`harness`**, which dispatch `--workspace` to subcommands (`medallion build
--workspace …`), so the literal never appears in the top-level module. That would
have dropped `medallion` — the **#2 most-invoked command** (21× in the measurement)
— into the exempt bucket, passing every test: coverage green, exemptions all
reasoned, nothing to catch it.

The list is therefore **curated, not grep-derived**, with two safeguards:
1. The decorator reads `--workspace` from argv generically, so it captures the arg
   regardless of where a subcommand defines it.
2. **Decorate when uncertain.** Decorating a workspace-less command is harmless (it
   writes no row); exempting a real pipeline command loses spend silently and
   permanently. The asymmetry is not symmetric, so the default is to decorate.

Current exemptions (5): `loop` (out-of-scope subsystem), `green-gate`,
`validate-git-hygiene` (dev/CI), `generate-skill-adapters` (repo codegen),
`retrieve-docs` (flat `--query` parser, verified no `--workspace` via any subcommand
path). These five are the *current output* of the rule above — a future maintainer
must not "improve" the list by re-deriving it structurally, which reintroduces the
`medallion` bug.

## Ingest (1a.2b): source, byte-match result, attribution ceiling

The spike that gates 1a.2b (byte-match + co-occurrence + dimensionality) **passed**,
against a better source than OTLP. Claude Code writes a per-session transcript to
`~/.claude/projects/<cwd>/<session-uuid>.jsonl` as a byproduct of running:
- **Byte-match:** the transcript filename and a per-record `sessionId` field both
  byte-equal `CLAUDE_CODE_SESSION_ID` = the anchor's `agent_session_id`. The join
  key binds.
- **Co-occurrence:** token counts live on the `message.usage` object *on the same
  record* as `sessionId` + `timestamp` — not split across a metric counter and a
  span attribute (the failure mode the expanded gate was built to catch).
- **Exclusive buckets already:** `input_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `output_tokens` — Anthropic reports these mutually
  exclusive, so the report's inclusive/exclusive normalization is a no-op here.

**Source decision: the transcript, not an OTLP sink.** It already exists (no new
component to keep alive — an absent transcript is a far louder signal than an OTLP
sink configured-but-quietly-not-exporting), and it sidesteps token normalization.

**Trade-offs recorded:**
- The transcript is a **Claude Code-specific artifact**. This is a *per-agent*
  reader, not one unified OTLP pipeline: Codex and Gemini need their own readers.
  Gemini stays honestly-degraded (§5); Codex gets a reader when someone drives with
  it. Do not assume the transcript approach generalizes.
- The **OTel span `session.id` was never verified** — the transcript made it
  unnecessary. If anyone later wants an OTLP path, that byte-match is still
  outstanding.

**Attribution ceiling — higher than the deferral implied.** Stage-level attribution
stays deferred (run-level first, as scoped) — but it was deferred partly on the
assumption that agent telemetry would only give a *session total*. The spike's
fourth sub-answer disproved that: usage is **per-turn, with timestamps**. So the
ceiling on stage-level precision is higher than the deferral suggested; this is
recorded, not re-opened. Run-level ships first.

**Privacy boundary (hard).** The transcript is full of conversation content, but the
reader emits ONLY numbers, timestamps, and the session id — message text / prompts /
responses / tool results are never extracted, stored, or surfaced.

## Deferred verification — the 1a.2 byte-match gate (honest-limit discipline)

Confirmed 2026-07-18: `CLAUDE_CODE_SESSION_ID` is exposed to subprocesses, stable,
and matches this session's scratchpad path segment. **Not yet byte-matched against
the OTel `session.id` span attribute** — that needs a live OTLP capture, which is
1a.2's collector. This is a *strong inference, not a verification*, and it must not
quietly become an assumption:

> **1a.2 gate.** The first live OTLP capture MUST byte-match
> `CLAUDE_CODE_SESSION_ID` against the OTel `session.id` span attribute before any
> ingest is trusted. If it does not match, 1a.2 **stops** and the join key is
> revisited.

This gate is also written into every run's `current.md` summary, so it travels
with the artifact.

## Systemic pattern — a fifth instance

Added to `phase-0-gap-matrix.md` §"Systemic pattern": **the join key that doesn't
join.** Same family as built-but-not-wired — a mechanism that *looks* complete and
is structurally inert. Two rounds, two target errors — the `llm_engine.py`
mis-target (Phase 1a retarget) and this join key — **both caught by empirical
verification, not by reasoning from the report.** Generalized defense: before
building against an audit finding, verify **the target and the join key
empirically** (grep who calls it; check env/runtime for the actual identifier),
not just that the named mechanism exists.
