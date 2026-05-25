"""
interns/insights.py — Insights Intern.

Analyzes run logs, synthesises experiment results, identifies patterns, and ranks hypotheses.
"""
from datetime import datetime, timezone
from interns.base import InternBase
from core.paths import PROJECT_ROOT
from core.storage.workspace import Workspace

ROOT = PROJECT_ROOT

_SYSTEM = """You are an Insights Intern for an autonomous prompt optimisation experiment.
You analyze run logs and experiment results to identify patterns in why experiments succeeded or failed, and write precise, evidence-based hypotheses.

Your output must contain both an analysis and a ranked list of hypotheses.

Use this format (no deviations):

## Analysis
- [Pattern 1]: [Explanation]
- [Pattern 2]: [Explanation]

## Recommendation
[What to focus on next]

## Ranked hypotheses
### Rank 1 — [title]
**Change:** [exact edit to make]
**Why:** [evidence from results or literature]
**Expected effect:** [criteria improved, approximate delta]
**Risk:** [low / medium / high]

### Rank 2 — [title]
[same format]

## Confidence: [high/medium/low]"""

class InsightsIntern(InternBase):
    name = "insights"

    def run(self, request: str, context: dict) -> str:
        workspace = Workspace(self.cfg.workspace_db)
        results = context.get("results_tsv", "")
        run_log = context.get("run_log_content", "")
        
        if not run_log:
             run_log = self._read(self.cfg.run_log, "[run.log not found]")
        
        n = context.get("experiment_count", 0)
        best = context.get("best_metric", "not yet established")

        now = datetime.now(timezone.utc)
        user = f"=== Results TSV (n={n}, best={best}) ===\n{results[-3000:] if results else 'No experiments yet.'}\n\n=== Last Run Log ===\n{run_log[-2000:]}\n\n=== Request ===\n{request}"

        workspace.append_run_log(f"\\n{'='*50}\\nTimestamp: {now.isoformat()}\\n=== SYSTEM ===\\n{_SYSTEM}\\n=== USER ===\\n{user}\\n")
        
        response = self.engine.generate(_SYSTEM, user, max_tokens=1500, model=self.cfg.models.deep_research)
        
        if response:
            workspace.append_run_log(f"\\n{'='*50}\\nTimestamp: {now.isoformat()}\\n=== RESPONSE ===\\n{response}\\n")
            workspace.save_ideas(response)
            return response
            
        fallback = "## Analysis\nNone\n## Recommendation\nNone\n## Ranked hypotheses\nNone\n## Confidence: low"
        workspace.save_ideas(fallback)
        return fallback
