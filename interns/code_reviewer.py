from pathlib import Path
from interns.base import InternBase

ROOT = Path(__file__).parent.parent

_SYSTEM = """You are a Code Review Intern.
Critique the provided code file and identify the single highest-impact edit that will improve code style, efficiency, or fix potential bugs.
Return ONLY this markdown structure:
## Critique
- [weakness 1]: [location and why]

## Suggested edit
[Exact edit description]

## Expected effect
[How it improves the code]

## Confidence: [high/medium/low]"""

class CodeReviewIntern(InternBase):
    name = "code_reviewer"

    def run(self, request: str, context: dict) -> str:
        code_content = context.get("editable_file_content", "")
        if not code_content:
             file_path = context.get("editable_file")
             if file_path:
                 code_content = self._read(ROOT / file_path, f"[{file_path} not found]")
             else:
                 code_content = "[No code provided]"
                 
        user = f"=== Code ===\n{code_content}\n\n=== Request ===\n{request}"
        
        response = self.engine.generate(_SYSTEM, user, max_tokens=900, model=self.cfg.models.prompt_engineer)
        if response: return response
        return "## Critique\nNone\n## Suggested edit\nNone\n## Expected effect\nNone\n## Confidence: low"
