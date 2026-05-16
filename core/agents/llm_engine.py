import json
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import Optional

class LLMEngine(ABC):
    @abstractmethod
    def generate(self, system: str, user: str, max_tokens: int, model: str) -> Optional[str]:
        pass

class APIEngine(LLMEngine):
    def __init__(self, api_key: str):
        self.api_key = api_key

    def generate(self, system: str, user: str, max_tokens: int, model: str) -> Optional[str]:
        import urllib.request
        import urllib.error
        try:
            payload = json.dumps({
                "system_instruction": {"parts": [{"text": system}]},
                "contents":          [{"parts": [{"text": user[:4000]}]}],
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

    def __init__(self, main_agent: str = "gemini-cli"):
        self.main_agent = main_agent

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
                timeout=60,
                encoding="utf-8",
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

class StubLLMEngine(LLMEngine):
    def __init__(self, stub_response: Optional[str] = None):
        self.stub_response = stub_response

    def generate(self, system: str, user: str, max_tokens: int, model: str) -> Optional[str]:
        return self.stub_response
