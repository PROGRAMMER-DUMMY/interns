# agents — audit

## Purpose
`core/agents/` owns the intelligence-provider abstraction and intern orchestration for the
autonomous experiment loop. It defines the `LLMEngine` strategy interface (API vs CLI subprocess vs
stub), a dynamic intern `InternRegistry` (lazy `importlib` loading of seven built-in interns), the
`InternBus` that routes/retries/logs every intern call and emits MLflow LLM traces, the `CodeMutator`
that spawns the configured main-agent CLI to rewrite an `editable_file` (with size guard, code-block
extraction, and per-language syntax validation), and `cli_inspector` which reads each CLI's on-disk
config to report the model that will actually answer calls.

## Files
| File | Lines | Purpose | Key classes/functions |
| --- | --- | --- | --- |
| `__init__.py` | 14 | Package exports | re-exports `APIEngine`, `CLIEngine`, `LLMEngine`, `StubLLMEngine`, `InternBus`, `InternRegistry` |
| `llm_engine.py` | 104 | Provider strategy interface | `LLMEngine` (ABC), `APIEngine` (Gemini REST), `CLIEngine` (gemini/claude/codex subprocess), `StubLLMEngine`, `_CLI_DISPATCH` |
| `code_mutator.py` | 354 | Main-agent file rewrite step | `CodeMutator`, `MutationResult`, `_build_prompt`, `_validate_syntax`, `_extract_code_block`, `_detect_lang`, `_strip_ansi` |
| `intern_bus.py` | 141 | Intern routing/retry/logging | `InternBus.invoke/_invoke_once/_dispatch/_log/list_active`, `truncate_for_chain`, `_is_failure` |
| `registry.py` | 43 | Dynamic intern loader | `InternRegistry.get_intern/list_known_interns`, `_BUILTIN_INTERNS` |
| `cli_inspector.py` | 209 | CLI install/model detection | `CLIStatus`, `inspect_cli`, `inspect_all`, `resolve_active_model`, `render_startup_banner`, per-CLI parsers, `main()` |

## Findings
| Tag | Location (file:line) | Description | Suggested fix |
| --- | --- | --- | --- |
| [INTEGRATION] | `llm_engine.py:13-38` + `config.py:148-159` | The only API engine is Gemini (`generativelanguage.googleapis.com`), yet `config` loads `anthropic_api_key` and the default `main_agent` is `claude-code`. There is NO Anthropic/Claude API engine. Whenever the "api" path is taken, calls hit Gemini regardless of provider intent. The CONTEXT/CLAUDE framing is Claude-primary but the API abstraction is Gemini-only. | Add an `AnthropicEngine` (and select by provider), or rename `APIEngine` to `GeminiEngine` and document that the API path is Gemini-only; route `anthropic_api_key` to a real engine. |
| [INTEGRATION] | `registry.py:30-36` | Engine selection ignores `main_agent`. It picks `APIEngine` whenever `google_api_key` is set (even if `main_agent="claude-code"`/`"codex"`), else `CLIEngine`. So `main_agent="api"` is never honored as a distinct branch, and a Google key silently overrides a Claude/Codex CLI choice for interns. | Branch on `main_agent`: `"api"`→APIEngine, else CLIEngine(main_agent); respect `force_cli` and the provider explicitly. |
| [BUG] | `llm_engine.py:36-38`, `code_mutator.py:164-172`, `intern_bus.py:108-110,118` | Broad `except Exception` blocks swallow everything and only `print(...)` (APIEngine returns `None`). HTTP 4xx/401/timeout vs transient errors are indistinguishable to callers; non-fatal telemetry failures are printed and dropped. Auth failures look identical to "model declined". | Catch specific exceptions (`urllib.error.HTTPError`/`URLError`, `TimeoutExpired`); surface status/category in the return or via logging instead of bare prints. |
| [BUG] | `llm_engine.py:92` | Timeout message hardcodes "timed out after 60s" but `self.timeout_s` is configurable (default 60). Misleading when timeout differs. | Interpolate `{self.timeout_s}`. |
| [NOT-PROD] | `llm_engine.py:26-29` | API key is placed in the URL query string (`?key=...`). Query-string secrets are logged by proxies/servers and can leak via tracebacks/`urlopen` error reprs. | Send the key via header (`x-goog-api-key`) rather than URL; ensure it is never echoed in error strings. |
| [NOT-PROD] | `llm_engine.py:17-38` | APIEngine has no retry/backoff and a single fixed 40s timeout; one transient failure → `None` for the whole intern call. CLIEngine/InternBus retry, but APIEngine does not participate in any backoff at its own layer. | Add bounded retry on 429/5xx/timeout with jittered backoff. |
| [NOT-PROD] | `code_mutator.py:147-154` | The main-agent CLI is spawned with the full rewrite prompt and `cwd=PROJECT_ROOT`; the agent is trusted to return only a code block. The returned content is syntax-validated but the loop then writes it to `editable_file` — i.e. attacker-influenced intern reports flow into a prompt that produces code written to disk and later executed by the experiment harness. No sandbox/diff-approval gate here. | Keep human/governance gate before write (loop is responsible); document the trust boundary; consider diff-size/critical-path guards. NOTE: module itself does not `exec`/`eval`. |
| [NOT-PROD] | `code_mutator.py:273-288` | SQL "validation" is a keyword substring check (`select`/`with`/...). It is not a parser and will accept malformed SQL or DDL the contract forbids (e.g. `DROP`/`ALTER` pass the keyword gate). | Use a real SQL parser (sqlglot) or restrict allowed statement types against the semantic contract. |
| [NOT-PROD] | `code_mutator.py:296-302`, `297` | `_validate_syntax` imports `toml` lazily; if `toml` is not installed the `import` raises inside the `try` only for the parse — actually the `import toml as _toml` is inside `try`, so a missing dependency is reported as a syntax error rather than an environment error. JSON/TOML validation also silently passes unknown langs. | Distinguish ImportError (env) from parse error; consider `tomllib` (stdlib 3.11+). |
| [DEAD] | `llm_engine.py:98-103`, `__init__.py:12` | `StubLLMEngine` is exported but referenced nowhere in `core/`, `tests/`, `dashboard.py`, or `interns/`. No test constructs it. | Remove, or use it in tests where engines are currently stubbed ad hoc. |
| [DUP] | `llm_engine.py:47-51`, `code_mutator.py:42-46`, `cli_inspector.py:37-41` | `_CLI_DISPATCH` (agent→exe+args) is duplicated verbatim in two files and a third `_MAIN_AGENT_TO_EXE` mapping duplicates the agent→exe half. Three copies must stay in sync (a comment even says "mirrors the dispatch tables"). | Define one mapping in a shared module and import it everywhere. |
| [DUP] | `llm_engine.py:34,87`, `code_mutator.py:253-254` | ANSI/code-fence stripping regex (`\x1b\[[0-9;]*[mGKHF]`) is duplicated across CLIEngine and CodeMutator. | Extract a shared `strip_ansi` helper. |
| [MISSING] | `intern_bus.py:91-98` | `list_active` defaults domain to `"prompt_optimisation"` and on any exception silently falls back to a hardcoded `["prompt_engineer", "insights", "eval"]` — names that are NOT in `_BUILTIN_INTERNS` (which has `insights`, `code_reviewer`, etc.). A missing/corrupt `agents.toml` yields intern names the registry will reject with `ValueError`. | Make the fallback use real registry names, or fail loudly; align the bare `except` to log the cause. |
| [BUG] | `intern_bus.py:94-98` | Bare `except Exception: return [...]` hides toml-parse/IO errors and a stale default domain (`prompt_optimisation`) inconsistent with the loop's `prompt_optimization`/KPI domains. | Log the exception; validate the domain against config. |
| [NOT-PROD] | `code_mutator.py:48-51` | `_CODE_BLOCK_RE` requires a trailing `\n``` ` — output where the closing fence has no preceding newline (or trailing whitespace) won't match and is reported as "no fenced code block", a false negative on otherwise-valid agent output. | Relax the regex to tolerate optional trailing whitespace / missing final newline. |

## Cross-package coupling
- `orchestration/loop.py` is the primary consumer: constructs `InternBus` and `CodeMutator`, calls
  `bus.list_active`, `bus.invoke` (implied via chain), `mutator.mutate`, `truncate_for_chain`,
  `render_startup_banner`, `resolve_active_model`, and consumes `MutationResult`.
- `registry.py` lazily imports `interns.*` (top-level `interns/` package) — all 7 mapped modules
  (`insights`, `code_reviewer`, `methodology_analyst`, `sql_specialist`, `data_engineer`,
  `validation`, `medallion_architect`) exist; `interns/base.py` consumes the `LLMEngine` type.
- `intern_bus.py` depends on `core.storage.workspace.Workspace.log_intern_activity` and optionally
  `core.observability.telemetry_backend.TelemetryBackend.log_intern_trace`.
- `dashboard.py` calls `inspect_all` and constructs `APIEngine` directly for its chat panel.
- Config knobs (`core/config.py`): `google_api_key`, `anthropic_api_key`, `force_cli`, `main_agent`,
  `main_agent_timeout_sec`, `max_editable_file_kb`, `intern_retry_backoff_s` — all consumed here.
- Tests: `tests/test_cli_inspector.py`, `tests/test_code_mutator.py`, `tests/test_loop_integration.py`
  exercise inspector, mutator, and InternBus retry/chain. No test covers `APIEngine`, `CLIEngine`,
  or `StubLLMEngine`.

## Verdict
The intern routing/registry/bus and CLI inspector are solid, well-documented, and tested. The two
material risks are integration-level, not logic bugs: (1) the API abstraction is Gemini-only despite
a Claude-primary architecture and a loaded-but-unused `anthropic_api_key`, and (2) `registry.get_intern`
selects the engine from `google_api_key` and ignores `main_agent`, so provider intent is silently
overridden. `CodeMutator` does NOT `exec`/`eval` (it returns content; the loop writes it) and has a
size guard + syntax check, but its SQL validation is a keyword substring heuristic and the agent
output flows to disk-then-execution, so the governance/write gate must live downstream. Broad
`except Exception` + `print` patterns, query-string API key, no API-layer retry, triplicated dispatch
tables, and a dead `StubLLMEngine` are the cleanup items. Not yet production-grade as a multi-provider
engine; acceptable for the Gemini/CLI-driven local loop it actually implements.
