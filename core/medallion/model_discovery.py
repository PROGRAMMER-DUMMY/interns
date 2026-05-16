"""
core/medallion/model_discovery.py — Model discovery dispatch (P4).

Queries the active CLI (/model, /models) or REST API to discover
which models are available in the current session.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DiscoveredModel:
    engine: str
    model_id: str
    raw_metadata: dict = field(default_factory=dict)


_DISCOVERY_DISPATCH: dict[str, tuple[str, list[str]]] = {
    "claude-code": ("claude",  ["/model"]),
    "gemini-cli":  ("gemini",  ["/models"]),
    "codex":       ("codex",   ["/model"]),
}


def discover_models(engine: str, cfg=None, *, timeout: int = 10) -> list[DiscoveredModel]:
    """
    Discover models for the given engine. Returns an empty list on any failure.
    Falls back to API discovery when the CLI is unavailable.
    """
    if engine in _DISCOVERY_DISPATCH:
        models = discover_via_cli(engine, timeout=timeout)
        if models:
            return models
    if engine == "gemini-api":
        api_key = _gemini_api_key(cfg)
        if api_key:
            try:
                return discover_via_api(engine, api_key=api_key)
            except Exception:
                pass
    return []


def discover_via_cli(engine: str, *, timeout: int = 10) -> list[DiscoveredModel]:
    dispatch = _DISCOVERY_DISPATCH.get(engine)
    if not dispatch:
        return []
    exe_name, args = dispatch
    exe = shutil.which(exe_name)
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if out.returncode != 0 and not out.stdout.strip():
        return []
    return _parse_discovery_output(engine, out.stdout)


def discover_via_api(engine: str, *, api_key: str) -> list[DiscoveredModel]:
    if engine != "gemini-api":
        return []
    import json as _json
    import urllib.request
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = _json.loads(resp.read())
    return [
        DiscoveredModel(
            engine=engine,
            model_id=m["name"].split("/")[-1],
            raw_metadata=m,
        )
        for m in data.get("models", [])
    ]


def _parse_discovery_output(engine: str, stdout: str) -> list[DiscoveredModel]:
    """Conservative per-engine parsers — parse known output shapes only."""
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    models: list[DiscoveredModel] = []
    seen: set[str] = set()
    for ln in lines:
        token = ln.split()[0] if ln.split() else ""
        if _looks_like_model_id(token) and token not in seen:
            models.append(DiscoveredModel(engine=engine, model_id=token))
            seen.add(token)
    return models


def _looks_like_model_id(token: str) -> bool:
    prefixes = ("claude", "gemini", "gemma", "gpt", "mistral", "llama", "command")
    return (
        any(token.lower().startswith(p) for p in prefixes)
        or ("/" in token and len(token) > 5)
        or ("-" in token and len(token) > 8)
    )


def _gemini_api_key(cfg) -> Optional[str]:
    import os
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
