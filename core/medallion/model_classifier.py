"""
core/medallion/model_classifier.py — WebSearch-driven model classification + cache (P4).

Classifies discovered models by parameter count, context window, vision
capability, and benchmark composite. Cache TTL is 7 days, keyed by
(engine, model_id). Evidence snippets are stored alongside the classification.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "model_id": self.model_id,
            "parameter_count": self.parameter_count,
            "context_window": self.context_window,
            "vision_capable": self.vision_capable,
            "benchmark_composite": self.benchmark_composite,
            "classified_at": self.classified_at,
            "evidence": self.evidence,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelCapability":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


class ModelSearchFailed(RuntimeError):
    pass


# ── Public API ─────────────────────────────────────────────────────────────────

def classify(
    engine: str,
    model_id: str,
    *,
    web_search: Optional[Callable] = None,
    no_search: bool = False,
) -> ModelCapability:
    cached = _read_cache(engine, model_id)
    if cached and not _expired(cached):
        return ModelCapability.from_dict(cached)
    if no_search or web_search is None:
        raise ModelSearchFailed(
            f"No cache entry for {engine}:{model_id} and WebSearch is disabled (--no-search)"
        )
    cap = _search_and_extract(engine, model_id, web_search)
    _write_cache(cap)
    return cap


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cache_key(engine: str, model_id: str) -> str:
    return f"{engine}::{model_id}"


def _read_cache(engine: str, model_id: str) -> Optional[dict]:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data.get(_cache_key(engine, model_id))
    except Exception:
        return None


def _write_cache(cap: ModelCapability) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
    except Exception:
        data = {}
    data[_cache_key(cap.engine, cap.model_id)] = cap.to_dict()
    CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _expired(entry: dict) -> bool:
    classified_at = entry.get("classified_at", "")
    if not classified_at:
        return True
    try:
        ts = datetime.fromisoformat(classified_at)
        return datetime.now(timezone.utc) - ts > TTL
    except Exception:
        return True


# ── Search + extraction ────────────────────────────────────────────────────────

def _search_and_extract(
    engine: str, model_id: str, web_search: Callable
) -> ModelCapability:
    queries = [
        f'"{model_id}" context window parameters benchmarks 2026',
        f'"{model_id}" model card',
        f'"{model_id}" vs claude opus parameters',
    ]
    evidence: list[str] = []
    parameter_count: Optional[float] = None
    context_window: Optional[int] = None
    vision: Optional[bool] = None
    composite: Optional[float] = None

    for q in queries:
        try:
            results = web_search(q)
        except Exception:
            continue
        for r in results if isinstance(results, list) else []:
            snippet = r.get("snippet", "") if isinstance(r, dict) else str(r)
            evidence.append(snippet[:200])
            parameter_count = parameter_count or _extract_param_count(snippet)
            context_window = context_window or _extract_context_window(snippet)
            vision = vision if vision is not None else _extract_vision(snippet)
            composite = composite or _extract_benchmark_composite(snippet)
        if all(x is not None for x in [parameter_count, context_window]):
            break

    confidence = _estimate_confidence(parameter_count, context_window, composite)
    return ModelCapability(
        engine=engine,
        model_id=model_id,
        parameter_count=parameter_count,
        context_window=context_window,
        vision_capable=vision,
        benchmark_composite=composite,
        classified_at=datetime.now(timezone.utc).isoformat(),
        evidence=evidence[:10],
        confidence=confidence,
    )


def _extract_param_count(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*[Bb]\s*param", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)\s*billion\s+param", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _extract_context_window(text: str) -> Optional[int]:
    m = re.search(r"(\d[\d,]+)\s*(?:token|context)", text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(r"context\s+(?:window|length)[:\s]+(\d[\d,]+)", text, re.IGNORECASE)
    if m:
        return int(m.group(1).replace(",", ""))
    return None


def _extract_vision(text: str) -> Optional[bool]:
    low = text.lower()
    if "vision" in low or "multimodal" in low or "image" in low:
        return True
    if "text-only" in low or "no vision" in low:
        return False
    return None


def _extract_benchmark_composite(text: str) -> Optional[float]:
    m = re.search(r"(?:mmlu|hellaswag|gsm8k)[^\d]*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
    if m:
        return float(m.group(1)) / 100.0
    return None


def _estimate_confidence(
    param: Optional[float], ctx: Optional[int], bench: Optional[float]
) -> float:
    filled = sum(x is not None for x in [param, ctx, bench])
    return round(filled / 3.0, 2)
