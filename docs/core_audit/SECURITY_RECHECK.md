# Security Re-check — Independent Spot Audit

**Date:** 2026-06-14
**Scope:** Surfaces 1-5 (secret handling, SSRF, injection, path traversal, deserialization) across
`core/`, `tools/`, `.gitignore`, `.env.example`, and the secrets hook.
**Overall posture verdict:** Largely sound. All critical P1-P6 controls are in place and correct.
Two residual medium findings remain open (ConnectBackend token-in-env, and optimizer_finder path
interpolation into exec'd code), both already documented in `docs/core_audit/execution.md` as
[NOT-PROD]. No committed secrets, no shell=True, no unsafe YAML, no pickle, no default-on remote
calls.

---

## Surface 1 — Secret handling / leakage

**Verdict: [ok]**

Files checked:
- `.gitignore` — covers `.env`, `.env.*` (excepts `.env.example`), `config/lock.toml`,
  `*.databrickscfg`, `*.pem`, `*.key`, `*.p12`, `credentials*`, `state/`, `*.log`.
  All sensitive paths are gitignored.
- `.env.example` — placeholder values only (`dapi<your-personal-access-token>`,
  `ghp_<your-token>`). No real token values. File is tracked intentionally.
- `core/config.py` — secrets read from environment variables (`os.environ.get(...)`) only;
  never printed, never logged. Config object fields (`token`, `google_api_key`,
  `anthropic_api_key`) carry runtime values in memory; no `__repr__` or `__str__` override,
  but no print/log path discovered.
- `.claude/hooks/guard_secrets.py` — PreToolUse hook blocks Read/Edit/Write on `.env`,
  `.databrickscfg`, `.pem`, `.key`, `.p12`, `.pfx`, `credentials*`, `.netrc`, `.pgpass`.
  Also blocks Bash display verbs (`cat`, `type`, `Get-Content`, `bat`, etc.) against those
  patterns, and blocks `printenv`, `Get-ChildItem Env:`, bare `env |`. Regex logic is correct
  and the `!.env.example` exclusion in `.gitignore` is mirrored in the hook's `_NOT_TEMPLATE`
  pattern — template files are never blocked.
- High-entropy / real-secret grep across all tracked files (`ghp_[A-Za-z0-9]{10+}`,
  `dapi[a-f0-9]{16+}`, `sk-[A-Za-z0-9]{30+}`, `AKIA[A-Z0-9]{16}`): **no matches**.
- `DATABRICKS_TOKEN` / `ANTHROPIC_API_KEY` / `GITHUB_PERSONAL_ACCESS_TOKEN` grep: all
  occurrences are env-var name references in code, config examples, and doc strings only —
  not literal secret values.
- `core/execution/databricks_client.py` health_check error path logs `str(exc)` which
  may contain host names but not token values. Acceptable.

**Residual note:** `ConnectBackend` (backend.py:377-380) injects `self.cfg.token` into the
child-process environment as `DATABRICKS_TOKEN`. This is already logged as [NOT-PROD] in
`docs/core_audit/execution.md`. It is not a committed-secret issue but a runtime-exposure
concern (see Surface 2 / Residual risks).

---

## Surface 2 — SSRF / outbound requests

**Verdict: [ok]**

Files checked:
- `core/agents/llm_engine.py` — `APIEngine.generate()` POSTs to
  `generativelanguage.googleapis.com` with a hardcoded HTTPS URL. Not user-controllable;
  URL is constructed from a fixed base + model name from config.
- `core/onboarding/sources/catalog.py` — uses `aiohttp`; all fetches pass through
  `assert_url_egress_allowed(url)` (catalog.py:1215) before any network IO. That function
  (catalog.py:1180-1207) rejects non-http(s) schemes, unresolvable hosts, and any IP that is
  loopback, private (RFC1918), link-local (169.254.x.x — blocks cloud metadata endpoint),
  reserved, multicast, or unspecified. An operator escape hatch
  (`AUTORESEARCH_ALLOW_PRIVATE_EGRESS=1`) exists and is documented. The URL itself comes
  from workspace `source_selection.json`, which requires explicit approval
  (`approve_final_preview`) before actions are executed.
- `core/onboarding/harness/ai_app_harness.py` — remote AI (`http_ai` target) gated behind
  `allow_remote_ai=False` default (ai_app_harness.py:92, 181). Blocked path returns a
  structured error record rather than making a network call.
- `core/onboarding/data_model/image_parser.py` — `allow_remote_vision=False` default;
  `confirm_sensitive_upload=False` default. The `_remote_status` function (image_parser.py:441)
  returns `remote_call_made: False` at both guards. Remote multimodal provider interface is
  documented as intentionally not implemented.
- `core/onboarding/workspace/onboarding.py:230-231` — hardcodes both flags to `False` in the
  standard onboarding path. Remote vision cannot be reached without an explicit CLI flag.
- `core/onboarding/databricks/workspace_deployer.py` — HTTP calls go to the configured
  Databricks host only (from config, not user input). Requires Databricks to be enabled in
  `lock.toml` and `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`.
- Remote execution gate (`backend.py:535`) checks `AUTORESEARCH_ALLOW_REMOTE_EXECUTION != "1"`
  and falls back to DuckDB with a logged denial.

---

## Surface 3 — Injection

**Verdict: [~] minor (pre-existing, documented)**

Files checked:
- `core/sql_safety.py` — centralized escaping layer: `quote_ident_sql` (ANSI double-quote
  escaping), `quote_ident_backtick` (backtick escaping), `escape_sql_literal` (single-quote
  escaping), `validate_expression_safe` (denylist for `;`, `--`, `/*`, `*/`, DDL keywords),
  `render_python_scalar_literal` (uses `repr()` for string values — correct), `map_comparison_op`
  (maps through a table, defaults safely). Module is stdlib-only and well-tested.
- `core/onboarding/kpi/sql_generator.py` — all column and table identifiers go through
  `self.quote_ident(...)` / `quote_ident()` throughout. `validate_expression_safe` is called
  on derived formulas (sql_generator.py:593). Identifier quoting coverage appears complete
  across the select-item, view, staging, join, and result-view builders.
- `core/execution/databricks_client.py:142-174` — `write_delta` was previously a SQL injection
  surface (raw f-string interpolation of record JSON). It now uses a bound parameter
  (`:rows` with `StatementParameterListItem`) and validates/backtick-quotes the target
  table parts via `assert_safe_identifier` + `quote_ident_backtick`. Fixed.
- `core/onboarding/kpi/polars_generator.py` and `pyspark_generator.py` — both reference
  `sql_safety` and use `render_python_scalar_literal` for filter values. Spot-checked;
  no raw concatenation of user text into Python source strings found in the paths checked.
- `core/orchestration/runner.py` and `core/execution/backend.py` — subprocess calls use
  `normalize_command()` which runs `shlex.split()` on string commands and returns argv lists.
  `subprocess.Popen(argv, ...)` without `shell=True` throughout — no shell injection
  surface. Grep for `shell=True` across all Python: **zero matches**.

**[~] Finding — tools/optimizer_finder.py:270-271:**
`PythonOptimizer._profile_memory` builds a Python `-c` command string by f-string
interpolating `path` (a CLI `--target` argument) into code passed to `exec(...)` inside a
subprocess (`sys.executable -c <string>`). If `path` contains single-quote characters,
the string-embedding breaks syntactically; a crafted path with embedded code could run
arbitrary Python in the subprocess. This is a **developer tool**, not a user-facing API, and
the tool is invoked from the eval loop with internally-generated file paths. Risk is LOW in
practice. But the construction is fragile and worth hardening.

Recommendation: pass `path` via a temp file or environment variable rather than embedding it
in a `-c` string. Alternatively restrict `path` to paths that are `is_relative_to(ROOT)` and
reject names containing quotes/backslashes before constructing the subprocess string.

No `os.system(...)` calls found anywhere. No `eval()`/`exec()` in core/ (tools/profiler.py
has a local `_exec` name — it is a regular function, not the builtin; confirmed by context).

---

## Surface 4 — Path traversal

**Verdict: [ok]**

Files checked:
- `core/onboarding/kpi/proof_packet.py:57-58, 170-171` — `self.workspace` is
  `(self.repo_root / workspace).resolve()`; then validated as
  `self.workspace.is_relative_to(self.repo_root)` and `self.workspace != self.repo_root`.
  Path escapes are caught before any file IO.
- `core/onboarding/workspace/validation.py:85-86, 116` — same pattern:
  `resolve()` then `is_relative_to(self.repo_root)`.
- `core/onboarding/data_model/image_parser.py:232-238` — `_validate_workspace()` uses
  `self.workspace.relative_to(self.repo_root)` (raises `ValueError` on escape).
- `core/execution/backend.py:618-619` — `_resource_decision_for_task` resolves the workspace
  path and checks `is_relative_to(ROOT)` before passing to `ResourceManager`.
- `core/execution/backend.py:582-584` — `_phi_gate_failure_for_task` also checks
  `is_relative_to(ROOT)` before invoking the PHI gate.
- `core/storage/workspace_layout.py` — layout paths are derived from `project_root` (already
  resolved by all callers); no raw user-path passthrough to file writes.
- `core/onboarding/sources/external_intake_workflow.py:42-43` — uses
  `Path(external_root).expanduser().resolve()`. No `is_relative_to` check on the external root
  itself (it is intentionally outside the repo). The workspace derived from it IS constrained
  inside the repo via `_normalize_workspace`.
- File writes in all generators derive from `WorkspaceLayout` paths, not from raw user input.

No path-traversal vectors found. The `resolve() + is_relative_to()` pattern is applied
consistently at the workspace validation layer.

---

## Surface 5 — Deserialization / unsafe parsing

**Verdict: [ok]**

Files checked:
- `pickle` grep across entire repo Python: **zero matches** in core/ and tools/.
- `yaml.load` (unsafe) grep: **zero matches**. All YAML reads use `yaml.safe_load`
  (`core/wiki/reader.py:67`, `core/dashboard/design_md.py:140`,
  `core/skills/adapter_generator.py:433`, `core/medallion/build.py:745`,
  `core/medallion/deploy_plan.py:214`, `core/onboarding/workspace/validation.py:989`).
- `eval()` / `exec()` in core/: **zero matches**. The `exec` in `tools/optimizer_finder.py`
  is inside a subprocess `-c` string (see Surface 3 finding); it is not a Python `exec()`
  call inside the main process.
- `tools/dashboard_verify.py:168` — `_parse_eval` function name is misleading; it parses
  JSON via `json.loads()` with fallback string extraction. No `eval()` builtin involved.
- JSON parsing throughout uses `json.loads()` on file/API content — no code execution path.
- `toml.load()` in `core/config.py` is used for `config/lock.toml` only, which is gitignored
  and operator-controlled.

---

## Sampling note

**Covered:** `.gitignore`, `.env.example`, secrets hook, `core/config.py`, `core/sql_safety.py`,
`core/execution/backend.py` (full read), `core/execution/databricks_client.py` (full read),
`core/onboarding/kpi/sql_generator.py` (partial), `core/onboarding/sources/catalog.py`
(SSRF section), `core/onboarding/data_model/image_parser.py` (gate section),
`core/onboarding/harness/ai_app_harness.py` (gate section), `core/onboarding/kpi/proof_packet.py`
(path validation), `core/onboarding/workspace/validation.py` (path validation),
`tools/optimizer_finder.py` (exec path), `tools/dashboard_verify.py` (eval function name),
`docs/core_audit/execution.md` (prior audit cross-reference). Pattern-searched all Python in
`core/` and `tools/` for `shell=True`, `pickle`, `yaml.load`, `eval(`, `exec(`, `subprocess`,
`is_relative_to`, `httpx`, `aiohttp`, `urllib.request`, and secret-key patterns.

**NOT covered:** ~95k LOC exhaustively. Did not audit: `core/orchestration/loop.py`,
`core/onboarding/kpi/polars_generator.py` / `pyspark_generator.py` (full read),
`core/governance/phi_gate.py` internals, `core/storage/external_data.py`,
`dashboard.py`, `core/medallion/*` (beyond deploy_plan.py), test suite, skill YAML files,
`.mcp.json` server configurations, or workspace-specific generated artifacts. The sampling
was risk-stratified toward known injection and SSRF surfaces.

---

## Prioritized residual risks

| Priority | Surface | Location | Description |
|----------|---------|----------|-------------|
| med | Secret in child env | `core/execution/backend.py:377-380` | `ConnectBackend` places `DATABRICKS_TOKEN` into the subprocess environment via `os.environ.copy()` + override. Token is visible in `/proc/<pid>/environ` on Linux or process-listing tools during the subprocess lifetime. Already flagged [NOT-PROD] in execution.md. Recommend OAuth/SP tokens or short-lived scoped tokens; scrub env before subprocess start. |
| low | Path interpolation in exec string | `tools/optimizer_finder.py:270-271` | `--target` path is f-string interpolated into a Python `-c` string run as subprocess. Crafted file paths with embedded quotes could cause syntax errors; in adversarial input could run code. Developer tool only. Recommend passing path via env var or a temp file rather than code-embedding. |
| low | ConnectBackend file handle leak | `core/execution/backend.py:386` | `open(log_path, "w")` handle passed to `_run_subprocess` is never closed (no `with` block). Not a security issue but a resource leak in Databricks Connect mode. |
| info | `IsolatedDuckDBBackend` dead code | `core/execution/backend.py:205-256` | Defined but unreachable via the factory; env-stripping security benefit is never exercised. Remove or wire it in behind a flag. |

**Critical:** none.
**High:** none.
