"""
interns/validation.py — Validation Intern.

Focuses on statistical profiling and quality gate enforcement.
"""
from pathlib import Path
from interns.base import InternBase

_SYSTEM = """You are a Validation Intern.
Your goal is to enforce quality gates on data outputs. 
You compare results against source profiles to detect statistical drift or "silent failures" (like excessive NULLs).

Your focus:
1. Comparing row counts and distributions.
2. Checking for unexpected NULL percentages.
3. Validating that business rules (from the semantic contract) are respected in the output.
"""

class ValidationIntern(InternBase):
    name = "validation"

    def run(self, request: str, context: dict) -> str:
        results = context.get("execution_results", "")
        source_profile = context.get("source_profile", "")
        
        user_prompt = f"=== Execution Results ===\n{results}\n\n=== Source Profile ===\n{source_profile}\n\n=== Request ===\n{request}"
        
        response = self.engine.generate(_SYSTEM, user_prompt, max_tokens=self.cfg.intern_limits.max_output_tokens)
        return response or "Error: Validation failed."
