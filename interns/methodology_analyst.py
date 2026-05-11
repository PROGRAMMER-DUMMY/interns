import json
import re
from interns.base import InternBase

_SYSTEM = """You are an expert Data Architect. Your job is to read methodology documents, data dictionaries, and info-models provided as Markdown text, and extract the underlying data schema strictly as a JSON object.

Extract:
1. Column definitions (expected data type, descriptions).
2. Allowed values / business constraints for categorical columns.
3. Primary keys.
4. Foreign key relationships.

Output ONLY valid JSON matching this schema:
{
  "columns": {
    "column_name": {
      "expected_type": "string|int|float|boolean|date|datetime",
      "description": "Brief definition",
      "is_primary_key": true/false,
      "allowed_values": ["Value1", "Value2"] // only if categorical/enum
    }
  },
  "relationships": [
    {
      "source_col": "column_name",
      "target_table": "target_table_name",
      "target_col": "target_column_name"
    }
  ]
}"""

class MethodologyAnalystIntern(InternBase):
    name = "methodology_analyst"

    def run(self, request: str, context: dict) -> str:
        document_text = context.get("document_text", "")
        
        user_prompt = f"Here is the document content:\n\n{document_text[:60000]}\n\nPlease extract the schema."
        
        # We use a large max_tokens for JSON output
        response = self.engine.generate(_SYSTEM, user_prompt, max_tokens=2000, model=self.cfg.models.deep_research)
        
        if response:
            return response
            
        # Fallback if no LLM configured or CLI agent is running without API key
        return "{}"
