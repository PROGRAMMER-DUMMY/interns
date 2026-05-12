"""
core/registry.py — manages dynamic loading of built-in interns.
"""
import importlib
from pathlib import Path
from core.agents.llm_engine import APIEngine, CLIEngine

ROOT = Path(__file__).resolve().parents[2]

_BUILTIN_INTERNS = {
    "insights":            "interns.insights.InsightsIntern",
    "code_reviewer":       "interns.code_reviewer.CodeReviewIntern",
    "methodology_analyst": "interns.methodology_analyst.MethodologyAnalystIntern",
}

class InternRegistry:
    def __init__(self, cfg):
        self.cfg = cfg

    def get_intern(self, name: str):
        if name in _BUILTIN_INTERNS:
            module_path, cls_name = _BUILTIN_INTERNS[name].rsplit(".", 1)
            module = importlib.import_module(module_path)
            intern_cls = getattr(module, cls_name)
            
            # Determine engine based on config
            if self.cfg.google_api_key and not self.cfg.force_cli:
                engine = APIEngine(self.cfg.google_api_key)
            else:
                engine = CLIEngine(self.cfg.main_agent)
                    
            return intern_cls(self.cfg, engine)
            
        known = self.list_known_interns()
        raise ValueError(f"Unknown intern: '{name}'. Known: {known}")

    def list_known_interns(self) -> list[str]:
        return list(_BUILTIN_INTERNS.keys())
