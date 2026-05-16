"""
core/agents/cli_inspector.py — Detect installed agent CLIs and their default models.

When the loop runs in CLIEngine mode (no GOOGLE_API_KEY), the model that actually
answers every LLM call is whatever each CLI is locally configured to use — NOT
the model field the intern code passes (which the CLI subprocess ignores).

This module reads each CLI's local config file at loop startup so we can:
  * show the operator what model is really going to be hit
  * record the resolved model in run metadata so cost/quality regressions are
    attributable to a specific CLI/model state
  * fail fast when the configured main_agent's CLI is missing from PATH

The /model slash command inside each CLI is interactive-only and cannot be
queried headlessly; reading the config file is the only reliable mechanism.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Where each CLI stores its persistent settings on disk.
# Resolved per-OS via Path.home() so this works on Windows + Linux + macOS.
_CONFIG_PATHS = {
    "gemini":  Path.home() / ".gemini" / "settings.json",
    "claude":  Path.home() / ".claude" / "settings.json",
    "codex":   Path.home() / ".codex" / "config.toml",
}

# Maps the loop's main_agent value -> the executable name it maps to.
# Mirrors the dispatch tables in llm_engine.py and code_mutator.py.
_MAIN_AGENT_TO_EXE = {
    "gemini-cli":  "gemini",
    "claude-code": "claude",
    "codex":       "codex",
}


@dataclass
class CLIStatus:
    """Snapshot of one CLI's installation + configured model."""
    name:        str
    installed:   bool
    path:        Optional[str] = None
    model:       Optional[str] = None
    config_path: Optional[str] = None
    note:        str = ""
    raw_config_keys: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.installed and self.model is not None


def inspect_cli(exe: str) -> CLIStatus:
    """Return the install + default-model status for one CLI executable.

    Reads the CLI's local config file directly. Does NOT invoke the CLI itself
    (that would cost an LLM call). If the config is unreadable, returns an
    `installed=True` status with `model=None` and a `note` explaining why.
    """
    path = shutil.which(exe)
    status = CLIStatus(name=exe, installed=bool(path), path=path)
    if not status.installed:
        status.note = f"'{exe}' not in PATH"
        return status

    cfg_path = _CONFIG_PATHS.get(exe)
    if not cfg_path or not cfg_path.exists():
        status.note = f"no config at {cfg_path}"
        return status
    status.config_path = str(cfg_path)

    try:
        text = cfg_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        status.note = f"could not read {cfg_path.name}: {exc}"
        return status

    if exe == "gemini":
        status.model = _parse_gemini_model(text)
    elif exe == "claude":
        status.model = _parse_claude_model(text)
    elif exe == "codex":
        status.model = _parse_codex_model(text)

    if status.model is None:
        status.note = "config present but no `model` key found"
    return status


def inspect_all() -> dict[str, CLIStatus]:
    """Inspect every CLI the harness can route to."""
    return {exe: inspect_cli(exe) for exe in _CONFIG_PATHS.keys()}


def resolve_active_model(main_agent: str) -> Optional[str]:
    """Return the model that will actually answer calls for `main_agent`,
    or None if the CLI isn't installed / configured."""
    exe = _MAIN_AGENT_TO_EXE.get(main_agent)
    if not exe:
        return None
    return inspect_cli(exe).model


def render_startup_banner(main_agent: str, has_api_key: bool) -> str:
    """One-screen summary the loop can print at startup so the operator knows
    exactly which model is about to spend their tokens."""
    lines = ["-" * 60, "  CLI / model setup"]
    if has_api_key:
        lines.append("  Mode: APIEngine (GOOGLE_API_KEY set) — interns hit Gemini API directly.")
        lines.append("  Note: main agent (CodeMutator) still uses CLI subprocess.")
    else:
        lines.append("  Mode: CLIEngine (no API key) - ALL calls go through a CLI subprocess.")

    active_exe = _MAIN_AGENT_TO_EXE.get(main_agent, "?")
    lines.append(f"  main_agent in lock.toml: {main_agent}  ->  binary: {active_exe}")
    lines.append("")

    for exe, st in inspect_all().items():
        marker = "*" if exe == active_exe else " "
        if not st.installed:
            lines.append(f"  {marker} {exe:8s}  not installed  ({st.note})")
            continue
        model = st.model or "(unknown)"
        cfg = Path(st.config_path).name if st.config_path else "—"
        lines.append(f"  {marker} {exe:8s}  model={model}  (from {cfg})")
        if st.note and not st.model:
            lines.append(f"      note: {st.note}")

    # Warn if the chosen main_agent isn't usable
    chosen = inspect_cli(active_exe) if active_exe in _CONFIG_PATHS else None
    if chosen and not chosen.installed:
        lines.append("")
        lines.append(f"  WARNING: main_agent '{main_agent}' refers to '{active_exe}' "
                     "but it is NOT installed. The loop will fail every main-agent call.")
    elif chosen and chosen.model is None:
        lines.append("")
        lines.append(f"  WARNING: '{active_exe}' is installed but its default model is unknown. "
                     "Calls will use whatever the CLI picks at runtime.")
    lines.append("-" * 60)
    return "\n".join(lines)


# ── Per-CLI config parsers ───────────────────────────────────────────────────

def _parse_gemini_model(text: str) -> Optional[str]:
    """Gemini settings.json schema (as of v0.42):
        { "model": { "name": "gemini-3.1-pro-preview" }, ... }
    Older schemas used a top-level string. Handle both."""
    import json
    try:
        data = json.loads(text)
    except Exception:
        return None
    val = data.get("model")
    if isinstance(val, dict):
        return val.get("name") or None
    if isinstance(val, str):
        return val
    return None


def _parse_claude_model(text: str) -> Optional[str]:
    """Claude settings.json: { "model": "opus" } (or 'sonnet', or a full ID)."""
    import json
    try:
        data = json.loads(text)
    except Exception:
        return None
    val = data.get("model")
    return val if isinstance(val, str) else None


def _parse_codex_model(text: str) -> Optional[str]:
    """Codex config.toml: `model = "gpt-5.5"` at the top level. Use a tiny
    regex so we don't pull in toml if the file has odd ordering or comments."""
    # Try a real TOML parse first
    try:
        import toml
        data = toml.loads(text)
        val = data.get("model")
        if isinstance(val, str):
            return val
    except Exception:
        pass
    # Fall back to a regex for malformed configs
    m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else None


# ── Self-check entrypoint (handy from the shell) ──────────────────────────────

def main() -> int:
    """`python -m core.agents.cli_inspector` prints the current state."""
    main_agent = os.environ.get("AUTORESEARCH_MAIN_AGENT", "gemini-cli")
    has_api_key = bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    print(render_startup_banner(main_agent, has_api_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
