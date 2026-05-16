"""
interns/data_engineer.py — Data Engineer Intern.

Focuses on schema mapping, join discovery, and relationship validation.
"""
from pathlib import Path
from interns.base import InternBase

_SYSTEM = """You are a Data Engineer Intern.
Your goal is to ensure high-quality schema mappings, discover reliable joins between datasets, and validate relationships.
You use data dictionaries, profiles, and data model documentation to prove relationships.

Your focus:
1. Mapping KPI features to physical columns.
2. Proposing joins based on common keys (Foreign Keys).
3. Ensuring referential integrity.
"""

class DataEngineerIntern(InternBase):
    name = "data_engineer"

    def run(self, request: str, context: dict) -> str:
        profiles = context.get("profiles", "")
        data_model = context.get("data_model_docs", "")
        
        user_prompt = f"=== Profiles ===\n{profiles}\n\n=== Data Model ===\n{data_model}\n\n=== Request ===\n{request}"
        
        response = self.engine.generate(_SYSTEM, user_prompt, max_tokens=self.cfg.intern_limits.max_output_tokens)
        return response or "Error: Data engineering task failed."
