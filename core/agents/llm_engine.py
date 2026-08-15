import json
import logging
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import Optional

_log = logging.getLogger(__name__)

class LLMEngine(ABC):
    @abstractmethod
    def generate(self, system: str, user: str, max_tokens: int, model: str) -> Optional[str]:
        pass

# Gemini API request-body character budget for the `user` prompt. Content
# beyond this is truncated -- previously silently (a caller/model had no way
# to know part of the prompt was missing); now a warning is logged and an
# explicit marker is appended so the model itself knows content was cut.
_GEMINI_USER_TEXT_LIMIT = 4000
_TRUNCATION_MARKER_TEMPLATE = "\n[...truncated, {omitted} characters omitted...]"


def _truncate_with_signal(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    _log.warning(
        "APIEngine: user prompt truncated from %d to %d characters (%d omitted)",
        len(text), limit, omitted,
    )
    return text[:limit] + _TRUNCATION_MARKER_TEMPLATE.format(omitted=omitted)


class APIEngine(LLMEngine):
    def __init__(self, api_key: str):
        self.api_key = api_key

    def generate(self, system: str, user: str, max_tokens: int, model: str) -> Optional[str]:
        import urllib.request
        import urllib.error
        try:
            payload = json.dumps({
                "system_instruction": {"parts": [{"text": system}]},
                "contents":          [{"parts": [{"text": _truncate_with_signal(user, _GEMINI_USER_TEXT_LIMIT)}]}],
                "generationConfig":  {"temperature": 0.3, "maxOutputTokens": max_tokens},
            }).encode()
            url = (
                f"https://generativelanguage.googleapis.com/v1beta"
                f"/models/{model}:generateContent?key={self.api_key}"
            )
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.loads(resp.read())
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            text = re.sub(r"^```(?:json|markdown)?\s*|\s*```$", "", text.strip())
            return text
        except Exception as exc:
            print(f"[APIEngine] API call failed: {exc}", flush=True)
            return None

# Maps main_agent name -> (executable, extra args before the prompt text)
# These args put each CLI into non-interactive (headless) mode:
#   gemini  -p   → reads prompt from arg, exits after one response
#   claude  -p   → alias --print; prints response and exits
#   codex   exec → subcommand for non-interactive single-shot run
# Without these flags the subprocess launches an interactive TUI and hangs
# until the loop's wall-clock timeout fires.
_CLI_DISPATCH: dict[str, tuple[str, list[str]]] = {
    "gemini-cli":  ("gemini", ["-p"]),
    "claude-code": ("claude", ["-p"]),
    "codex":       ("codex",  ["exec"]),
}

class CLIEngine(LLMEngine):
    """Calls an installed CLI tool (gemini, claude, codex) to generate a response."""

    def __init__(self, main_agent: str = "gemini-cli", cwd: str | None = None, timeout_s: int = 60):
        self.main_agent = main_agent
        self.cwd = cwd
        self.timeout_s = timeout_s

    def generate(self, system: str, user: str, max_tokens: int, model: str) -> Optional[str]:
        dispatch = _CLI_DISPATCH.get(self.main_agent)
        if not dispatch:
            print(f"[CLIEngine] Unknown main_agent '{self.main_agent}' — skipping", flush=True)
            return None

        exe_name, prefix_args = dispatch
        exe_path = shutil.which(exe_name)
        if not exe_path:
            print(f"[CLIEngine] '{exe_name}' not found in PATH — skipping", flush=True)
            return None

        combined = f"{system}\n\n{user[:3000]}"
        cmd = [exe_path] + prefix_args + [combined]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=self.timeout_s,
                encoding="utf-8",
                cwd=self.cwd,
            )
            if result.returncode == 0:
                out = result.stdout.strip()
                # Strip ANSI escape codes some CLIs emit
                out = re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", out)
                return out or None
            print(f"[CLIEngine] {exe_name} exit {result.returncode}: {result.stderr[:200]}", flush=True)
            return None
        except subprocess.TimeoutExpired:
            print(f"[CLIEngine] {exe_name} timed out after 60s", flush=True)
            return None
        except Exception as exc:
            print(f"[CLIEngine] {exe_name} failed: {exc}", flush=True)
            return None
