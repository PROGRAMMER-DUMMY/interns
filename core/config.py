"""
core/config.py — typed settings loaded from config/lock.toml.

This is the single source of truth for all configurable values.
Agents and interns import `load()` from here.
Nothing reads lock.toml directly except this module.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import toml

from core.paths import PROJECT_ROOT

@dataclass
class DatabricksConfig:
    """Parsed from lock.toml [databricks] block + environment variables."""
    enabled: bool = False
    execution: str = "duckdb"          # duckdb | jobs | warehouse | connect
    loop_on_databricks: bool = False
    fallback: str = "duckdb"           # duckdb | fail
    host: str = ""
    token: str = ""
    http_path: str = ""                # required for warehouse mode
    catalog: str = "autoresearch"
    schema: str = "experiments"
    trace_redact: bool = True
    http_timeout_sec: int = 60
    # HIPAA coverage: True only when a signed BAA + compliance security profile
    # are in place for this target. Default False -> a trial/default workspace
    # is NOT HIPAA-covered and the PHI gate blocks identifiable-PHI upload/exec.
    phi_covered: bool = False

    def is_active(self) -> bool:
        return self.enabled and bool(self.host) and bool(self.token)

    @classmethod
    def from_lock(cls, lock: dict) -> "DatabricksConfig":
        block = lock.get("databricks", {})
        if not block or not block.get("enabled", False):
            return cls()
        host = os.environ.get(block.get("host_env", "DATABRICKS_HOST"), "")
        token = os.environ.get(block.get("token_env", "DATABRICKS_TOKEN"), "")
        http_path = os.environ.get(block.get("http_path_env", "DATABRICKS_HTTP_PATH"), "")
        return cls(
            enabled=block.get("enabled", False),
            execution=block.get("execution", "duckdb"),
            loop_on_databricks=block.get("loop_on_databricks", False),
            fallback=block.get("fallback", "duckdb"),
            host=host,
            token=token,
            http_path=http_path,
            catalog=block.get("catalog", "autoresearch"),
            schema=block.get("schema", "experiments"),
            trace_redact=block.get("trace_redact", True),
            http_timeout_sec=block.get("http_timeout_sec", 60),
            phi_covered=block.get("phi_covered", False),
        )

ROOT = PROJECT_ROOT
_LOCK_PATH = ROOT / "config" / "lock.toml"
_AGENTS_PATH = ROOT / "config" / "agents.toml"


@dataclass
class ModelConfig:
    llm_judge: str = "gemini-2.0-flash"
    deep_research: str = "gemini-2.0-flash"
    prompt_engineer: str = "gemini-2.0-flash"
    eval_intern: str = "gemini-2.0-flash"
    fallback_strategy: str = "rule_based"


@dataclass
class Config:
    # resolved from env vars named in lock.toml
    google_api_key: str = ""
    anthropic_api_key: str = ""
    databricks: DatabricksConfig = field(default_factory=DatabricksConfig)

    models: ModelConfig = field(default_factory=ModelConfig)

    # cli
    gemini_cli: str = "gemini"
    python_cmd: str = "python"

    # backends
    primary_backend: str = "api"
    force_cli: bool = False

    # main agent driving the loop
    main_agent: str = "claude-code"  # claude-code | gemini-cli | codex | api
    main_agent_timeout_sec: int = 300
    max_editable_file_kb: int = 50
    prior_output_max_chars: int = 1800
    intern_retry_backoff_s: int = 5

    # limits
    max_token_count: int = 800
    max_run_seconds: int = 30
    hard_timeout_seconds: int = 90
    max_experiments_session: int = 100
    deep_research_every_n: int = 5
    stuck_threshold: int = 3

    # paths (computed)
    root: Path = field(default_factory=lambda: ROOT)

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def workspace_db(self) -> Path:
        return self.state_dir / "workspace.db"

    @property
    def run_log(self) -> Path:
        return self.state_dir / "run.log"

    def has_api_key(self) -> bool:
        return bool(self.google_api_key)

    def use_cli(self) -> bool:
        return self.force_cli or not self.google_api_key


def load() -> Config:
    """Load config from lock.toml + environment. Call once at startup."""
    lock = _load_lock()

    google_key = ""
    for env_var in [
        lock.get("api", {}).get("google_api_key_env", "GOOGLE_API_KEY"),
        lock.get("api", {}).get("gemini_api_key_env", "GEMINI_API_KEY"),
    ]:
        val = os.environ.get(env_var, "")
        if val:
            google_key = val
            break

    anthropic_key = os.environ.get(
        lock.get("api", {}).get("anthropic_api_key_env", "ANTHROPIC_API_KEY"), ""
    )

    m = lock.get("models", {})
    lim = lock.get("limits", {})
    cli = lock.get("cli", {})
    back = lock.get("backends", {})

    return Config(
        google_api_key=google_key,
        anthropic_api_key=anthropic_key,
        databricks=DatabricksConfig.from_lock(lock),
        models=ModelConfig(
            llm_judge=m.get("llm_judge", "gemini-2.0-flash"),
            deep_research=m.get("deep_research", "gemini-2.0-flash"),
            prompt_engineer=m.get("prompt_engineer", "gemini-2.0-flash"),
            eval_intern=m.get("eval_intern", "gemini-2.0-flash"),
            fallback_strategy=m.get("fallback_strategy", "rule_based"),
        ),
        gemini_cli=cli.get("gemini", "gemini"),
        python_cmd=cli.get("python", "python"),
        primary_backend=back.get("primary", "api"),
        force_cli=back.get("force_cli", False),
        main_agent=lock.get("agent", {}).get("main_agent", "claude-code"),
        main_agent_timeout_sec=lock.get("agent", {}).get("main_agent_timeout_sec", 300),
        max_editable_file_kb=lock.get("agent", {}).get("max_editable_file_kb", 50),
        prior_output_max_chars=lock.get("agent", {}).get("prior_output_max_chars", 1800),
        intern_retry_backoff_s=lock.get("agent", {}).get("intern_retry_backoff_s", 5),
        max_token_count=lim.get("max_token_count", 800),
        max_run_seconds=lim.get("max_run_seconds", 30),
        hard_timeout_seconds=lim.get("hard_timeout_seconds", 90),
        max_experiments_session=lim.get("max_experiments_session", 100),
        deep_research_every_n=lim.get("deep_research_every_n", 5),
        stuck_threshold=lim.get("stuck_threshold_discards", 3),
    )


def _load_lock() -> dict:
    if not _LOCK_PATH.exists():
        return {}
    return toml.load(_LOCK_PATH)
