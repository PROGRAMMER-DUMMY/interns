"""
interns/base.py — base class for all interns.

Provides LLM call helpers that respect config/lock.toml backend settings.
All model IDs, CLI commands, and API routes come from cfg (lock.toml).
Interns never hardcode models or API keys.
"""
from pathlib import Path
from typing import Optional

from core.config import Config
from core.llm_engine import LLMEngine

ROOT = Path(__file__).parent.parent


class InternBase:
    name: str = "base"

    def __init__(self, cfg: Config, engine: LLMEngine):
        self.cfg = cfg
        self.engine = engine

    def run(self, request: str, context: dict) -> str:
        raise NotImplementedError

    # ── File helpers ──────────────────────────────────────────────────────────

    def _read(self, path: Path, fallback: str = "") -> str:
        try:
            return path.read_text(encoding="utf-8") if path.exists() else fallback
        except Exception:
            return fallback
