# develop_spec/tracing.md — observability + tracing

Where to look when you need to see what a run actually did. All local-safe and
stdlib-first; remote telemetry is lazy and gated.

## Structured command events (always on, local)

`core/observability/events.py` emits best-effort JSONL events per CLI command via
a `time_command(...)` context manager (used across `core/execution/backend.py`,
`core/agents/*`, the KPI CLIs, etc.).

- Events land at: `<workspace>/interns/state/events.jsonl`
- Each event carries the command, timing, and JSON-safe details.
- `core/observability/parser.py` reads/aggregates the JSONL — use it (or read the
  tail of the file) instead of re-deriving timings by hand.
- The module intentionally avoids `core.` imports to stay dependency-cycle-free;
  keep it that way if you extend it.

## Telemetry backend (strategy)

`core/observability/telemetry_backend.py`:

- `LocalTelemetry` — wraps the local SQLite Workspace. **Always active**, zero
  extra deps. This is what you get in development mode.
- `DatabricksTelemetry` — MLflow 3 (experiment tracking, LLM tracing, GenAI
  eval). **Lazy import, remote, gated** — not used in local dev unless explicitly
  enabled with access configured.

## Session audit (separate concern)

`session-snapshot` records end-user conversation/command/file-change history under
`.agents/sessions/` (git-ignored). See `docs/session_snapshot.md`. That is
end-user audit, distinct from the per-command event trace above and from this
dev changelog.

## When debugging a harness run

1. Read the stage `.md` the wrapper pointed to.
2. Tail `<ws>/interns/state/events.jsonl` for the command timeline.
3. Only then open machine JSON, and only the slice you need (never whole audit
   files — see token discipline in `guidelines.md`).
4. Reproduce with the `diagnose` discipline before changing code.
