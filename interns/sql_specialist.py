"""
interns/sql_specialist.py — SQL Specialist Intern.

Generates and repairs dialect-specific SQL (DuckDB/Databricks).
"""
from pathlib import Path
from interns.base import InternBase

_SYSTEM = """You are a SQL Specialist Intern.
Your goal is to generate and repair dialect-specific SQL (DuckDB/Databricks) for healthcare analytics.
You focus on syntactical correctness, efficient joins, and following the data model.

When fixing errors:
1. Analyze the error message (e.g., Binder Error, Syntax Error).
2. Inspect the current SQL and the available schema/profile evidence.
3. Provide a corrected version of the SQL.
"""

class SQLSpecialistIntern(InternBase):
    name = "sql_specialist"

    def run(self, request: str, context: dict) -> str:
        # Implementation details will be expanded as needed
        sql = context.get("current_sql", "")
        error = context.get("error_message", "")
        schema = context.get("schema_evidence", "")
        
        user_prompt = f"=== Current SQL ===\n{sql}\n\n=== Error ===\n{error}\n\n=== Schema Evidence ===\n{schema}\n\n=== Request ===\n{request}"
        
        response = self.engine.generate(_SYSTEM, user_prompt, max_tokens=self.cfg.intern_limits.max_output_tokens)
        return response or "Error: SQL generation failed."
