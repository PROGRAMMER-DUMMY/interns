"""
core/agents/code_mutator.py — the main-agent code-mutation step.

This is the missing link in the autonomous experiment loop. Between intern
analysis and experiment execution, this module spawns the configured main
agent (gemini-cli / claude-code / codex) as a subprocess with:

  * the full current editable_file content
  * truncated reports from every intern that ran this iteration
  * the semantic contract (what the rewrite MUST preserve)
  * the best metric so far + optimization plan

The agent is asked to output the rewritten file inside a single fenced code
block on stdout. This module extracts the code block, syntax-validates it,
and returns a `MutationResult` describing what to do next.

The loop is responsible for actually writing the file (or skipping the write
in --dry-run mode).
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from core.config import Config
from core.paths import PROJECT_ROOT

if TYPE_CHECKING:
    from core.observability.telemetry_backend import TelemetryBackend


# Maps `main_agent` config name -> (executable, args-before-prompt-text).
# Each entry puts the CLI in non-interactive mode so it produces one response
# and exits. Without these flags the subprocess launches an interactive TUI
# and hangs until the wall-clock timeout fires.
_CLI_DISPATCH: dict[str, tuple[str, list[str]]] = {
    "gemini-cli":  ("gemini", ["-p"]),
    "claude-code": ("claude", ["-p"]),
    "codex":       ("codex",  ["exec"]),
}

_CODE_BLOCK_RE = re.compile(
    r"```(?:[a-zA-Z0-9_+-]*)\n(?P<body>.*?)\n```",
    re.DOTALL,
)


@dataclass
class MutationResult:
    """Outcome of one main-agent code-mutation attempt."""
    success: bool
    new_content: Optional[str] = None
    error: Optional[str] = None
    elapsed_s: float = 0.0
    lang: str = ""
    raw_output: str = ""
    prompt_chars: int = 0
    intern_reports_used: int = 0
    diagnostics: dict = field(default_factory=dict)

    @property
    def diff_summary(self) -> str:
        if self.success:
            return f"OK ({len(self.new_content or '')} chars, lang={self.lang})"
        return f"FAIL ({self.error or 'unknown'})"


class CodeMutator:
    """Spawn the main agent CLI and capture a rewritten editable_file."""

    def __init__(self, cfg: Config, telemetry: Optional["TelemetryBackend"] = None,
                 root: Optional[Path] = None):
        self.cfg = cfg
        self.telemetry = telemetry
        self.root = root or PROJECT_ROOT
        self.timeout_s = int(getattr(cfg, "main_agent_timeout_sec", 300))
        self.max_kb = int(getattr(cfg, "max_editable_file_kb", 50))

    # ── Public ────────────────────────────────────────────────────────────────

    def mutate(
        self,
        editable_file: str,
        file_content: str,
        intern_reports: list[str],
        semantic_contract_text: str,
        best_metric: Optional[float],
        optimization_plan: str,
        experiment_count: int,
    ) -> MutationResult:
        """Spawn main agent, capture rewritten file content."""
        started = time.time()
        lang = self._detect_lang(editable_file)

        # Pre-flight: file size guard
        size_kb = len(file_content.encode("utf-8")) / 1024.0
        if size_kb > self.max_kb:
            return MutationResult(
                success=False,
                error=f"editable_file is {size_kb:.1f}KB > max {self.max_kb}KB",
                lang=lang,
                elapsed_s=round(time.time() - started, 2),
                intern_reports_used=len(intern_reports),
            )

        # Pre-flight: dispatch lookup
        dispatch = _CLI_DISPATCH.get(self.cfg.main_agent)
        if not dispatch:
            return MutationResult(
                success=False,
                error=f"unknown main_agent '{self.cfg.main_agent}'",
                lang=lang,
                elapsed_s=round(time.time() - started, 2),
                intern_reports_used=len(intern_reports),
            )
        exe_name, prefix_args = dispatch
        exe_path = shutil.which(exe_name)
        if not exe_path:
            return MutationResult(
                success=False,
                error=f"'{exe_name}' not in PATH",
                lang=lang,
                elapsed_s=round(time.time() - started, 2),
                intern_reports_used=len(intern_reports),
            )

        # Build prompt
        prompt = self._build_prompt(
            editable_file=editable_file,
            file_content=file_content,
            intern_reports=intern_reports,
            semantic_contract_text=semantic_contract_text,
            best_metric=best_metric,
            optimization_plan=optimization_plan,
            experiment_count=experiment_count,
            lang=lang,
        )

        # Spawn agent
        try:
            proc = subprocess.run(
                [exe_path] + prefix_args + [prompt],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                encoding="utf-8",
                cwd=str(self.root),
            )
        except subprocess.TimeoutExpired:
            return MutationResult(
                success=False,
                error=f"main agent timed out after {self.timeout_s}s",
                lang=lang,
                elapsed_s=round(time.time() - started, 2),
                prompt_chars=len(prompt),
                intern_reports_used=len(intern_reports),
            )
        except Exception as exc:
            return MutationResult(
                success=False,
                error=f"main agent subprocess failed: {exc}",
                lang=lang,
                elapsed_s=round(time.time() - started, 2),
                prompt_chars=len(prompt),
                intern_reports_used=len(intern_reports),
            )

        elapsed = round(time.time() - started, 2)

        if proc.returncode != 0:
            return MutationResult(
                success=False,
                error=f"exit {proc.returncode}: {(proc.stderr or '')[:200]}",
                lang=lang,
                elapsed_s=elapsed,
                raw_output=proc.stdout or "",
                prompt_chars=len(prompt),
                intern_reports_used=len(intern_reports),
            )

        # Extract code block
        raw_out = self._strip_ansi(proc.stdout or "")
        new_content = self._extract_code_block(raw_out)
        if new_content is None:
            return MutationResult(
                success=False,
                error="no fenced code block in agent output",
                lang=lang,
                elapsed_s=elapsed,
                raw_output=raw_out,
                prompt_chars=len(prompt),
                intern_reports_used=len(intern_reports),
            )

        if not new_content.strip():
            return MutationResult(
                success=False,
                error="code block was empty",
                lang=lang,
                elapsed_s=elapsed,
                raw_output=raw_out,
                prompt_chars=len(prompt),
                intern_reports_used=len(intern_reports),
            )

        # Syntax validation
        ok, syntax_err = self._validate_syntax(new_content, lang)
        if not ok:
            return MutationResult(
                success=False,
                error=f"syntax invalid: {syntax_err}",
                lang=lang,
                elapsed_s=elapsed,
                raw_output=raw_out,
                prompt_chars=len(prompt),
                intern_reports_used=len(intern_reports),
            )

        return MutationResult(
            success=True,
            new_content=new_content,
            lang=lang,
            elapsed_s=elapsed,
            raw_output=raw_out,
            prompt_chars=len(prompt),
            intern_reports_used=len(intern_reports),
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _detect_lang(editable_file: str) -> str:
        s = (editable_file or "").lower()
        if s.endswith(".py"):
            return "python"
        if s.endswith(".sql"):
            return "sql"
        if s.endswith((".yaml", ".yml")):
            return "yaml"
        if s.endswith(".json"):
            return "json"
        if s.endswith(".toml"):
            return "toml"
        return "text"

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", text)

    @staticmethod
    def _extract_code_block(text: str) -> Optional[str]:
        """Return body of the first fenced code block, or None."""
        m = _CODE_BLOCK_RE.search(text or "")
        if not m:
            return None
        return m.group("body")

    @staticmethod
    def _validate_syntax(content: str, lang: str) -> tuple[bool, Optional[str]]:
        """Best-effort syntax check per language. Unknown languages always pass."""
        if lang == "python":
            try:
                ast.parse(content)
                return True, None
            except SyntaxError as exc:
                return False, f"line {exc.lineno}: {exc.msg}"
        if lang == "sql":
            # Basic sanity: must contain at least one recognised SQL keyword.
            stripped = content.strip().lower()
            if not stripped:
                return False, "empty SQL"
            has_kw = any(
                kw in stripped
                for kw in ("select", "with", "insert", "update", "delete",
                           "create", "drop", "alter", "merge")
            )
            if not has_kw:
                return False, "no SQL keywords found"
            # Reject obvious markdown bleed-through
            if stripped.startswith("```") or "```" in stripped[-10:]:
                return False, "stray markdown fence inside body"
            return True, None
        if lang == "json":
            import json as _json
            try:
                _json.loads(content)
                return True, None
            except Exception as exc:
                return False, str(exc)
        if lang == "toml":
            import toml as _toml
            try:
                _toml.loads(content)
                return True, None
            except Exception as exc:
                return False, str(exc)
        return True, None

    def _build_prompt(
        self,
        *,
        editable_file: str,
        file_content: str,
        intern_reports: list[str],
        semantic_contract_text: str,
        best_metric: Optional[float],
        optimization_plan: str,
        experiment_count: int,
        lang: str,
    ) -> str:
        """Construct the main-agent prompt.

        Constraints baked into the prompt:
          * Output ONLY a single fenced code block — no commentary
          * Must respect the semantic contract
          * Must aim to improve the metric
        """
        reports_block = "\n\n".join(
            f"### Intern report #{i+1}\n{r.strip()}"
            for i, r in enumerate(intern_reports)
            if r and r.strip()
        ) or "(no intern reports this iteration)"

        return (
            "You are the main optimization agent driving an autonomous experiment loop.\n"
            f"This is experiment iteration #{experiment_count}.\n"
            f"Best metric so far: {best_metric}\n"
            f"Target file: {editable_file}\n"
            f"Detected language: {lang}\n\n"
            "## Your task\n"
            "Rewrite the file below to improve the metric, while strictly respecting the\n"
            "semantic contract. Do not change the file's purpose, output schema, or\n"
            "interface — only its implementation.\n\n"
            "## Hard output rules\n"
            f"- Output ONLY a single fenced code block: ```{lang} ... ```\n"
            "- Do NOT add commentary before, after, or inside the block.\n"
            "- Do NOT include partial files. Output the full replacement content.\n"
            "- If you cannot safely improve the file, output the file unchanged inside the code block.\n\n"
            "## Semantic contract (must be preserved)\n"
            f"{semantic_contract_text}\n\n"
            "## Optimization plan\n"
            f"{optimization_plan}\n\n"
            "## Intern reports (analysis from the intern chain)\n"
            f"{reports_block}\n\n"
            "## Current file content\n"
            f"```{lang}\n{file_content}\n```\n"
        )
