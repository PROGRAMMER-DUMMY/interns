"""
core/registry.py — manages dynamic loading of built-in interns.
"""
import importlib
from core.agents.llm_engine import APIEngine, CLIEngine
from core.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT

_BUILTIN_INTERNS = {
    "insights":            "interns.insights.InsightsIntern",
    "code_reviewer":       "interns.code_reviewer.CodeReviewIntern",
    "methodology_analyst": "interns.methodology_analyst.MethodologyAnalystIntern",
    "sql_specialist":      "interns.sql_specialist.SQLSpecialistIntern",
    "data_engineer":       "interns.data_engineer.DataEngineerIntern",
    "validation":          "interns.validation.ValidationIntern",
    "medallion_architect": "interns.medallion_architect.MedallionArchitectIntern",
}

class InternRegistry:
    def __init__(self, cfg):
        self.cfg = cfg

    def get_intern(self, name: str):
        if name in _BUILTIN_INTERNS:
            module_path, cls_name = _BUILTIN_INTERNS[name].rsplit(".", 1)
            module = importlib.import_module(module_path)
            intern_cls = getattr(module, cls_name)
            
            # Engine selection honors the configured main_agent first (per the
            # "LLM via CLI agent, not SDK" decision: this platform routes
            # judgment through the orchestrating CLI agent, not a direct API
            # SDK). main_agent="api" is the only value that selects APIEngine;
            # every other value (claude-code/gemini-cli/codex) always uses
            # CLIEngine, regardless of whether a Google API key happens to be
            # configured -- a configured key must not silently override the
            # chosen CLI agent. force_cli forces CLIEngine even when
            # main_agent="api".
            if self.cfg.main_agent == "api" and not self.cfg.force_cli:
                engine = APIEngine(self.cfg.google_api_key)
            else:
                engine = CLIEngine(self.cfg.main_agent)
                    
            return intern_cls(self.cfg, engine)
            
        known = self.list_known_interns()
        raise ValueError(f"Unknown intern: '{name}'. Known: {known}")

    def list_known_interns(self) -> list[str]:
        return list(_BUILTIN_INTERNS.keys())
