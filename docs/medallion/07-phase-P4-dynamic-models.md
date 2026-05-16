# 07 — Phase P4: Dynamic Model Tiering

## Goal

After P4:

- At every `design-medallion` run start, the agent discovers active models via the running CLI's own `/model` (or `/models`) command — no hardcoded model list.
- Unknown models are classified by **WebSearch** against their published capabilities (parameter count, context window, vision, benchmarks).
- Classifications are cached for 7 days, keyed by `(engine, model_id)`, with full evidence trail.
- The discovered set is **ranked**; tiers (heavy/medium/light) are derived from the ranking, not from name patterns.
- Task classes declare a **minimum tier**, never a default; the agent picks the cheapest discovered tier ≥ minimum.
- Three prompt strategies (Heavy/Medium/Light) match the tier; Light tier uses heavily decomposed, JSON-schema-validated prompts with retry cap 5.
- The agent works correctly on Gemma 4 as well as on Opus 4.7 (proven by a `--cheap` end-to-end run on the Healthcare RCM workspace).
- A per-run USD budget cap halts the run with `BUDGET_EXCEEDED` and surfaces a `"completed N of M, resume?"` blocker.
- Content-addressed cache keys include `(model_tier, prompt_strategy_version)` so outputs from different tiers never collide.

## Prerequisites

- P0 + P1 shipped. P2/P3 are independent of P4 and may ship in any order after P0+P1.

## Requirements (must-haves)

1. **Discovery dispatch** per engine:
   - `claude-code` → `claude /model`
   - `gemini-cli` → `gemini /models`
   - `codex` → `codex /model`
   - `gemini-api` (direct API) → REST `GET /v1beta/models`

2. **No hardcoded tier YAML, no name-regex patterns**. The cache + WebSearch is the only path to a tier label.

3. **WebSearch classification**:
   - Query template: `"<model_id>" context window parameters benchmarks 2026`
   - Extracted signals: param count, context window, vision capability, benchmark composite (when found).
   - Cache file: `core/agents/state/model_classification_cache.json`, keyed `(engine, model_id)`, TTL 7 days.
   - Cache entry includes `evidence` field — snippets that justified the classification.

4. **Tier auto-assignment** from the *ranked discovered set*:
   - N=1 → single-tier mode (every task uses that model).
   - N=2 → top=heavy, bottom=light.
   - N=3 → top=heavy, middle=medium, bottom=light.
   - N≥4 → top tertile heavy, middle tertile medium, bottom tertile light.

5. **Task-class minimum tier** (not default):
   - `bronze_append_sql`, `silver_transform_sql`, `silver_derived_column`, `lineage_explanation_md` → minimum `light`.
   - `silver_contract_rules`, `kpi_sql_regeneration` → minimum `medium`.
   - `star_schema_design` → minimum `medium`.

6. **Prompt strategy by tier**:
   - **Heavy**: one prompt, full context, freeform JSON output.
   - **Medium**: decomposed per-entity (one call per fact/dim/Silver table).
   - **Light**: heavily decomposed (one call per relationship / per derived column / per assertion). JSON schema embedded inline; on schema-validation failure, retry with `"Your previous output failed: <error>. Return only valid JSON matching this schema."` Retry cap 5.

7. **Escalation/de-escalation**:
   - Escalate when (a) two consecutive failures at current tier on same task, (b) `requires_judgment: true` + cache miss, (c) validator flags low confidence.
   - De-escalate when (a) budget burn > 60% with > 40% tables remaining, (b) cache near-hit (schema delta only), (c) `--cheap` flag.
   - Both operations swap **model AND prompt strategy** together.

8. **Budget cap**: USD spend tracked via token accounting in `core/agents/llm_engine.py` (extend with per-call cost estimation). On exceed, write a blocker `completed_N_of_M:resume?` and exit `BUDGET_EXCEEDED`.

9. **Content-addressed cache**: `state/medallion/medallion_cache/<sha256>.json` where the hash includes `(system_prompt, relevant_contract_excerpts, table_schema, task_class, model_tier, prompt_strategy_version)`.

10. **`--no-search` flag** forces cache-only operation; fails loudly if a discovered model has no cached classification.

11. **`--calibrate` flag** runs a one-shot calibration probe per discovered model (a tiny JSON-generation task), reranks them by accuracy + latency, overrides search-derived ranking. Result cached.

## Architecture for this phase

### Module additions

```
core/agents/
└── llm_engine.py                  # extend with discover_models() + cost estimation

core/medallion/
├── model_discovery.py             # /model dispatch + parse
├── model_classifier.py            # WebSearch-driven classification + cache
├── tier_router.py                 # task_class → tier, escalation/de-escalation, prompt strategy
├── budget.py                      # USD tracking, cap enforcement, blocker writer
└── prompt_strategies.py           # Heavy / Medium / Light strategy implementations
```

### `model_discovery.py` shape

```python
# core/medallion/model_discovery.py
from dataclasses import dataclass, field
from pathlib import Path
import json, shutil, subprocess
from typing import Optional

@dataclass
class DiscoveredModel:
    engine: str
    model_id: str
    raw_metadata: dict = field(default_factory=dict)

_DISCOVERY_DISPATCH = {
    "claude-code":  ("claude",  ["/model"]),
    "gemini-cli":   ("gemini",  ["/models"]),
    "codex":        ("codex",   ["/model"]),
}

def discover_via_cli(engine: str, *, timeout: int = 10) -> list[DiscoveredModel]:
    dispatch = _DISCOVERY_DISPATCH.get(engine)
    if not dispatch:
        return []
    exe_name, args = dispatch
    exe = shutil.which(exe_name)
    if not exe:
        return []
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return []
    if out.returncode != 0:
        return []
    return _parse_discovery_output(engine, out.stdout)

def discover_via_api(engine: str, *, api_key: str) -> list[DiscoveredModel]:
    if engine == "gemini-api":
        import urllib.request, json as _json
        with urllib.request.urlopen(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}", timeout=15
        ) as resp:
            data = _json.loads(resp.read())
        return [
            DiscoveredModel(engine=engine, model_id=m["name"].split("/")[-1], raw_metadata=m)
            for m in data.get("models", [])
        ]
    return []

def _parse_discovery_output(engine: str, stdout: str) -> list[DiscoveredModel]:
    """Per-engine output parsers. /model and /models output formats vary."""
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    models = []
    for ln in lines:
        # Conservative: take the first whitespace-delimited token that looks like a model id
        token = ln.split()[0]
        if "/" in token or "-" in token or token.startswith("gpt") or token.startswith("claude") or token.startswith("gemini") or token.startswith("gemma"):
            models.append(DiscoveredModel(engine=engine, model_id=token))
    return models
```

The output parser is intentionally conservative — different CLIs format `/model` output differently. Treat it as best-effort and let WebSearch + cache reconcile.

### `model_classifier.py` shape

```python
# core/medallion/model_classifier.py
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json, hashlib
from typing import Optional

CACHE_PATH = Path("core/agents/state/model_classification_cache.json")
TTL = timedelta(days=7)

@dataclass
class ModelCapability:
    engine: str
    model_id: str
    parameter_count: Optional[float] = None    # billions
    context_window: Optional[int] = None
    vision_capable: Optional[bool] = None
    benchmark_composite: Optional[float] = None
    classified_at: str = ""
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0

def classify(engine: str, model_id: str, *, web_search) -> ModelCapability:
    cached = _read_cache(engine, model_id)
    if cached and not _expired(cached):
        return ModelCapability(**cached)
    if web_search is None:
        # Cache-only mode; raise a typed error so the orchestrator can handle it.
        raise ModelSearchFailed(f"No cache entry for {engine}:{model_id} and WebSearch is disabled")
    cap = _search_and_extract(engine, model_id, web_search)
    _write_cache(cap)
    return cap

class ModelSearchFailed(RuntimeError):
    pass

def _search_and_extract(engine, model_id, web_search) -> ModelCapability:
    queries = [
        f'"{model_id}" context window parameters benchmarks 2026',
        f'"{model_id}" model card',
        f'"{model_id}" vs claude opus',
    ]
    evidence = []
    parameter_count = None
    context_window = None
    vision = None
    composite = None
    for q in queries:
        results = web_search(q)  # function passed in; returns list of {title, url, snippet}
        for r in results:
            snippet = r.get("snippet", "")
            evidence.append(snippet[:200])
            parameter_count = parameter_count or _extract_param_count(snippet)
            context_window = context_window or _extract_context_window(snippet)
            vision = vision if vision is not None else _extract_vision(snippet)
            composite = composite or _extract_benchmark_composite(snippet)
        if all(x is not None for x in [parameter_count, context_window, vision, composite]):
            break
    confidence = _estimate_confidence(parameter_count, context_window, composite)
    return ModelCapability(
        engine=engine, model_id=model_id,
        parameter_count=parameter_count, context_window=context_window,
        vision_capable=vision, benchmark_composite=composite,
        classified_at=datetime.now(timezone.utc).isoformat(),
        evidence=evidence, confidence=confidence,
    )

def _extract_param_count(text: str) -> Optional[float]:
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*[Bb]\s+param", text)
    if m: return float(m.group(1))
    m = re.search(r"(\d+)\s*billion", text, re.IGNORECASE)
    if m: return float(m.group(1))
    return None

# similar extractors for context window, vision capability, benchmark composite
```

### `tier_router.py` shape

```python
# core/medallion/tier_router.py
from dataclasses import dataclass, field
from typing import Optional

TIERS = ("heavy", "medium", "light")
TASK_MINIMUMS = {
    "bronze_append_sql":       "light",
    "silver_transform_sql":    "light",
    "silver_derived_column":   "light",
    "silver_contract_rules":   "medium",
    "kpi_sql_regeneration":    "medium",
    "star_schema_design":      "medium",
    "lineage_explanation_md":  "light",
}

@dataclass
class TierAssignment:
    by_tier: dict[str, list[str]] = field(default_factory=dict)   # tier -> [model_id, ...]
    ranking: list[str] = field(default_factory=list)              # best to least capable

def rank_models(capabilities: list["ModelCapability"]) -> list[str]:
    def score(c):
        return (
            c.benchmark_composite or 0,
            c.parameter_count or 0,
            c.context_window or 0,
            1 if c.vision_capable else 0,
        )
    return [c.model_id for c in sorted(capabilities, key=score, reverse=True)]

def assign_tiers(ranking: list[str]) -> TierAssignment:
    n = len(ranking)
    if n == 0:
        return TierAssignment()
    if n == 1:
        return TierAssignment(by_tier={"heavy": ranking, "medium": ranking, "light": ranking}, ranking=ranking)
    if n == 2:
        return TierAssignment(by_tier={"heavy": [ranking[0]], "medium": [ranking[0]], "light": [ranking[1]]}, ranking=ranking)
    if n == 3:
        return TierAssignment(by_tier={"heavy": [ranking[0]], "medium": [ranking[1]], "light": [ranking[2]]}, ranking=ranking)
    third = n // 3
    return TierAssignment(by_tier={
        "heavy":  ranking[:third],
        "medium": ranking[third:2*third],
        "light":  ranking[2*third:],
    }, ranking=ranking)

def pick_model(task_class: str, assignment: TierAssignment) -> tuple[str, str]:
    """Return (tier, model_id). Cheapest tier >= minimum."""
    minimum = TASK_MINIMUMS[task_class]
    for tier in ("light", "medium", "heavy"):
        if _tier_at_or_above(tier, minimum) and assignment.by_tier.get(tier):
            return (tier, assignment.by_tier[tier][0])
    raise InsufficientModelCapability(f"No discovered tier meets minimum `{minimum}` for {task_class}")

def _tier_at_or_above(tier: str, minimum: str) -> bool:
    order = {"light": 0, "medium": 1, "heavy": 2}
    return order[tier] >= order[minimum]

class InsufficientModelCapability(RuntimeError):
    pass
```

### `prompt_strategies.py` shape

```python
# core/medallion/prompt_strategies.py
from dataclasses import dataclass
from typing import Any, Callable

STRATEGY_VERSION = "1.0"  # bump on any change; cache key depends on this

@dataclass
class PromptStrategy:
    tier: str
    decompose: Callable[[str, dict], list[dict]]   # (task_class, context) -> [prompt_dict, ...]
    validate:  Callable[[str, str], tuple[bool, str]]  # (task_class, raw_response) -> (ok, error_message)
    max_retries: int

def heavy_strategy() -> PromptStrategy:
    return PromptStrategy(
        tier="heavy",
        decompose=_heavy_single_shot,
        validate=_validate_json,
        max_retries=2,
    )

def medium_strategy() -> PromptStrategy:
    return PromptStrategy(
        tier="medium",
        decompose=_medium_per_entity,
        validate=_validate_json,
        max_retries=3,
    )

def light_strategy() -> PromptStrategy:
    return PromptStrategy(
        tier="light",
        decompose=_light_atomic,
        validate=_validate_json_schema,   # stricter
        max_retries=5,
    )
```

### `budget.py` shape

```python
# core/medallion/budget.py
import json
from pathlib import Path
from typing import Optional

PRICING = {
    # USD per million tokens (input, output). Refreshed in this file when models change.
    # Keys are engine + model_id; entries fall back to a default if missing.
}

class BudgetTracker:
    def __init__(self, max_usd: float, *, state_path: Path):
        self.max_usd = max_usd
        self.state_path = state_path
        self.spent = 0.0
        self.history = []

    def charge(self, *, engine: str, model_id: str, in_tokens: int, out_tokens: int) -> float:
        rate = PRICING.get(f"{engine}:{model_id}") or PRICING.get("__default__") or (3.0, 15.0)
        usd = (in_tokens / 1e6) * rate[0] + (out_tokens / 1e6) * rate[1]
        self.spent += usd
        self.history.append({"engine": engine, "model_id": model_id, "usd": usd, "tokens": in_tokens + out_tokens})
        self._persist()
        return usd

    def remaining(self) -> float:
        return max(self.max_usd - self.spent, 0.0)

    def burn_rate(self, tables_done: int, tables_total: int) -> float:
        return self.spent / max(self.max_usd, 0.01)

    def should_deescalate(self, tables_done: int, tables_total: int) -> bool:
        progress = tables_done / max(tables_total, 1)
        return self.burn_rate(tables_done, tables_total) > 0.6 and progress < 0.6

    def exceeded(self) -> bool:
        return self.spent > self.max_usd

    def _persist(self) -> None:
        self.state_path.write_text(json.dumps({
            "max_usd": self.max_usd,
            "spent": self.spent,
            "history": self.history,
        }, indent=2), encoding="utf-8")
```

### Cache key extension

Anywhere the agent caches an LLM output, the key includes:

```python
import hashlib
def cache_key(*, system_prompt: str, context_excerpts: dict, task_class: str, model_tier: str) -> str:
    payload = json.dumps({
        "sp": hashlib.sha256(system_prompt.encode()).hexdigest(),
        "ctx": hashlib.sha256(json.dumps(context_excerpts, sort_keys=True).encode()).hexdigest(),
        "tc": task_class,
        "tier": model_tier,
        "strategy_v": prompt_strategies.STRATEGY_VERSION,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()
```

## Implementation steps

### Step 1: discovery dispatch

Implement `model_discovery.py`. Smoke-test against any installed CLI. The output parser is engine-specific and conservative — write per-engine parsers, not one regex.

### Step 2: classification cache

Implement `model_classifier.py`. Wire the `WebSearch` tool as the search backend. The first run against unfamiliar models is slow (network); subsequent runs hit cache.

### Step 3: tier router

Implement `tier_router.py`. Add unit tests for the N=1/2/3/4+ cases.

### Step 4: prompt strategies

Implement `prompt_strategies.py`. The decomposition functions encapsulate the actual prompt-building logic for each strategy. The `validate` function for light tier uses `jsonschema.validate`.

### Step 5: budget tracker

Implement `budget.py`. Wire it into `MedallionArchitectIntern.run` to charge per LLM call. Update PRICING when new models ship.

### Step 6: integrate into orchestrator

In `core/medallion/design.py`, replace the simple `intern.design(inputs)` call with:

```python
discovered = discover_models(engine, cfg)
capabilities = [classify(m.engine, m.model_id, web_search=web_search_tool) for m in discovered]
ranking = rank_models(capabilities)
assignment = assign_tiers(ranking)
budget = BudgetTracker(cfg.budget.max_usd_per_run, state_path=run_state_dir / "budget.json")

result_blocks = {}
for task_class in PROPOSAL_TASK_CLASSES:
    tier, model_id = pick_model(task_class, assignment)
    strategy = strategy_for_tier(tier)
    output = run_task_with_strategy(task_class, context, intern, strategy, model_id, budget, cache)
    result_blocks[task_class] = output
    if budget.exceeded():
        write_budget_exceeded_blocker(...)
        raise MedallionExit(EXIT_BUDGET_EXCEEDED, ...)
```

### Step 7: `--engine`, `--model`, `--no-search`, `--calibrate` flags

Already added to `design_cli.py` in P0 for `--engine` and `--model`. Add `--no-search` and `--calibrate` here.

### Step 8: cache key extension

Update every `cache_key(...)` call site to include `(model_tier, prompt_strategies.STRATEGY_VERSION)`. Old cache entries become invalid; the cache directory can be cleared.

## Testing

```
tests/medallion/test_model_discovery.py        # parser per engine; missing CLI gracefully empty
tests/medallion/test_model_classifier.py       # cache hit/miss; TTL expiry; evidence captured
tests/medallion/test_tier_router.py            # N=1/2/3/4+; minimum enforcement; insufficient raises
tests/medallion/test_prompt_strategies.py      # light tier schema-validates; retries with corrective prompt
tests/medallion/test_budget.py                 # tracking; deescalation; exceeded
tests/medallion/integration/test_gemma_e2e.py  # design-medallion --engine gemini-api --model gemma-4 --cheap end-to-end on fixture workspace
```

The Gemma e2e test is the canonical "works on light tier" proof.

## Acceptance criteria

1. `uv run design-medallion --workspace workspaces/Healthcare-RCM-Data-Platform` discovers the active models in the current CLI session (verifiable in `state/medallion/runs/<run_id>/discovered_models.json`).
2. WebSearch classification populates the cache; a second run within 7 days does not re-search.
3. Tier assignment matches the ranking (N=3 case verifiable by inspection).
4. Running with only one model active produces single-tier mode without crashing.
5. Running with `--model gemma-4 --cheap` produces a valid star schema and Silver contract — proving the light tier prompt strategy.
6. Running with a synthetic 0.001 USD cap on a real workspace exits `BUDGET_EXCEEDED` with a resume blocker.
7. Cache hits between runs are visible in `run.json:cache_hits` and reduce LLM call count to near-zero on unchanged inputs.
8. `--no-search` against an unfamiliar model exits `MODEL_SEARCH_FAILED`.
9. `INSUFFICIENT_MODEL_CAPABILITY` fires when only a light-tier model is available but a task requires medium.

## Risks

| Risk | Mitigation |
|---|---|
| `/model` output format changes in CLI updates | Engine-specific parsers are isolated; one parser broken doesn't break others; integration tests catch this |
| WebSearch returns noisy or contradictory results | Three-query fallback; ranks by confidence; cache has `evidence` for manual override |
| Cache becomes stale and misclassifies | 7-day TTL; explicit `--calibrate` to override |
| Prompt strategy `STRATEGY_VERSION` not bumped on a change | Code review check; cache regeneration on every PR that touches `prompt_strategies.py` |
| Budget pricing drifts as vendors update | PRICING dict is one file; update on vendor announcement; CI alert if a new model is seen without a PRICING entry |
| Light-tier output validates JSON but is semantically wrong | Schema validation catches shape, not content; mitigation is to verify against existing seed-proposal shape and reject if structurally invalid |
| `/model` requires an interactive TTY | Discovery dispatch runs the CLI in non-interactive mode; if that fails, fall back to `GET /v1beta/models` for API engines, surface blocker for CLI-only engines |

## Definition of Done

- [ ] All 5 new modules under `core/medallion/` exist.
- [ ] `discover_models` returns at least one model on a configured CI environment.
- [ ] WebSearch classifications cache to `model_classification_cache.json` with evidence.
- [ ] All 9 acceptance criteria pass.
- [ ] Gemma 4 end-to-end on Healthcare RCM completes successfully.
- [ ] `core/agents/state/.gitignore` excludes the classification cache (it contains web snippets, not secrets, but is reproducible).
