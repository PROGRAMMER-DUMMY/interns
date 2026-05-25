import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Union

from core.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT

@dataclass
class RunRecord:
    hash: str
    short: str
    message: str
    author: str
    relative: str
    date: str


class Workspace:
    """
    Unified Datastore and Git Operator.
    Consolidates previously scattered state files into a single SQLite DB,
    and handles git history and commits for the experiment loop.
    """
    def __init__(self, db_path: Union[str, Path] = ROOT / "state" / "workspace.db", work_dir: Path = ROOT):
        self.db_path = str(db_path)
        self.work_dir = work_dir
        
        # Ensure directory exists if not using an in-memory db
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self.conn:
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS experiments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    results TEXT,
                    status TEXT,
                    metrics TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    log_type TEXT,
                    content TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE TABLE IF NOT EXISTS intern_activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intern_name TEXT,
                    activity TEXT,
                    details TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS optimization_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    task_id TEXT,
                    artifact TEXT,
                    change_type TEXT,
                    change_types TEXT,
                    expected_reason TEXT,
                    actual_result TEXT,
                    decision TEXT,
                    baseline_metric REAL,
                    candidate_metric REAL,
                    metric_delta REAL,
                    direction TEXT,
                    correctness_passed INTEGER,
                    guardrails_failed TEXT,
                    execution_time_seconds REAL,
                    matching_score REAL,
                    confidence TEXT,
                    evidence TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS governance_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    task_id TEXT,
                    decision TEXT,
                    approval_state TEXT,
                    promotion_allowed INTEGER,
                    rationale TEXT,
                    gates TEXT,
                    evidence_pack TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id TEXT,
                    run_id TEXT,
                    task_id TEXT,
                    severity TEXT,
                    title TEXT,
                    message TEXT,
                    requires_human INTEGER,
                    evidence TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                );
            ''')

    def get_results_tsv_string(self) -> str:
        """Queries the experiments table and formats records into a TSV string."""
        cur = self.conn.execute(
            "SELECT run_id, metrics, status, results FROM experiments ORDER BY id ASC"
        )
        rows = cur.fetchall()
        if not rows:
            return ""
        lines = ["commit\tprimary_metric\tstatus\tdescription"]
        for r in rows:
            lines.append(f"{r['run_id']}\t{r['metrics']}\t{r['status']}\t{r['results']}")
        return "\n".join(lines) + "\n"

    def redact_keys(self, text: str, keys: Optional[List[str]] = None) -> str:
        """Helper to redact API keys from any logged context."""
        if not text:
            return text
            
        if keys is None:
            from core.config import load
            cfg = load()
            keys = [
                k for k in [
                    cfg.google_api_key,
                    cfg.anthropic_api_key,
                    cfg.databricks.token,
                ] if k
            ]
        
        redacted = str(text)
        for key in keys:
            if key and len(key) > 4:
                # Replace exact matches with a partially masked version
                redacted = redacted.replace(key, f"REDACTED_{key[:4]}...")
        return redacted

    # --- Replaced File I/O Methods ---

    def log_experiment(self, run_id: str, results: str, status: str, metrics: str) -> None:
        """Replaces results.tsv logging"""
        with self.conn:
            self.conn.execute(
                "INSERT INTO experiments (run_id, results, status, metrics) VALUES (?, ?, ?, ?)",
                (run_id, results, status, metrics)
            )

    def append_run_log(self, content: str) -> None:
        """Replaces run.log appending"""
        with self.conn:
            self.conn.execute(
                "INSERT INTO logs (log_type, content) VALUES (?, ?)",
                ("run_log", self.redact_keys(content))
            )

    def write_session_report(self, report: str) -> None:
        """Replaces session_report.md writing"""
        with self.conn:
            self.conn.execute(
                "INSERT INTO logs (log_type, content) VALUES (?, ?)",
                ("session_report", self.redact_keys(report))
            )

    def save_ideas(self, ideas: str) -> None:
        """Replaces ideas.md writing"""
        with self.conn:
            self.conn.execute(
                "INSERT INTO logs (log_type, content) VALUES (?, ?)",
                ("ideas", self.redact_keys(ideas))
            )

    def log_intern_activity(self, intern_name: str, activity: str, details: Any) -> None:
        """Replaces intern_log.jsonl appending"""
        details_str = json.dumps(details) if not isinstance(details, str) else details
        with self.conn:
            self.conn.execute(
                "INSERT INTO intern_activity (intern_name, activity, details) VALUES (?, ?, ?)",
                (intern_name, activity, self.redact_keys(details_str))
            )

    def save_loop_status(self, status: dict) -> None:
        """Replaces loop_status.json saving"""
        with self.conn:
            self.conn.execute(
                "INSERT INTO logs (log_type, content) VALUES (?, ?)",
                ("loop_status", json.dumps(status))
            )

    def load_loop_status(self) -> Optional[dict]:
        """Replaces loop_status.json loading"""
        cur = self.conn.execute(
            "SELECT content FROM logs WHERE log_type = 'loop_status' ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row and row['content']:
            return json.loads(row['content'])
        return None

    # --- Optimization Memory ---

    def log_optimization_memory(self, record: dict) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO optimization_memory (
                    run_id, task_id, artifact, change_type, change_types,
                    expected_reason, actual_result, decision, baseline_metric,
                    candidate_metric, metric_delta, direction, correctness_passed,
                    guardrails_failed, execution_time_seconds, matching_score,
                    confidence, evidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("run_id"),
                    record.get("task_id"),
                    record.get("artifact"),
                    record.get("change_type"),
                    json.dumps(record.get("change_types", [])),
                    record.get("expected_reason"),
                    record.get("actual_result"),
                    record.get("decision"),
                    record.get("baseline_metric"),
                    record.get("candidate_metric"),
                    record.get("metric_delta"),
                    record.get("direction"),
                    1 if record.get("correctness_passed") else 0,
                    json.dumps(record.get("guardrails_failed", [])),
                    record.get("execution_time_seconds"),
                    record.get("matching_score"),
                    record.get("confidence"),
                    self.redact_keys(json.dumps(record.get("evidence", {}))),
                ),
            )

    def get_recent_optimization_memory(self, limit: int = 10) -> list[dict]:
        cur = self.conn.execute(
            """
            SELECT * FROM optimization_memory
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [self._memory_row_to_dict(row) for row in cur.fetchall()]

    def get_optimization_pattern_stats(self, limit: int = 50) -> list[dict]:
        cur = self.conn.execute(
            """
            SELECT
                change_type,
                COUNT(*) AS attempts,
                SUM(CASE WHEN decision = 'keep' THEN 1 ELSE 0 END) AS keeps,
                AVG(CASE WHEN metric_delta IS NOT NULL THEN metric_delta ELSE 0 END) AS avg_metric_delta
            FROM optimization_memory
            GROUP BY change_type
            ORDER BY keeps DESC, attempts DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cur.fetchall():
            attempts = row["attempts"] or 0
            keeps = row["keeps"] or 0
            rows.append({
                "change_type": row["change_type"],
                "attempts": attempts,
                "keeps": keeps,
                "success_rate": round(keeps / attempts, 4) if attempts else 0.0,
                "avg_metric_delta": row["avg_metric_delta"],
            })
        return rows

    # --- Governance decisions and alerts ---

    def log_governance_decision(self, decision: dict) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO governance_decisions (
                    run_id, task_id, decision, approval_state, promotion_allowed,
                    rationale, gates, evidence_pack
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.get("run_id"),
                    decision.get("task_id"),
                    decision.get("decision"),
                    decision.get("approval_state"),
                    1 if decision.get("promotion_allowed") else 0,
                    decision.get("rationale"),
                    self.redact_keys(json.dumps(decision.get("gates", []))),
                    self.redact_keys(json.dumps(decision.get("evidence_pack", {}))),
                ),
            )
            for alert in decision.get("alerts", []):
                self.conn.execute(
                    """
                    INSERT INTO alerts (
                        alert_id, run_id, task_id, severity, title, message,
                        requires_human, evidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alert.get("alert_id"),
                        alert.get("run_id"),
                        alert.get("task_id"),
                        alert.get("severity"),
                        alert.get("title"),
                        alert.get("message"),
                        1 if alert.get("requires_human", True) else 0,
                        self.redact_keys(json.dumps(alert.get("evidence", {}))),
                    ),
                )

    def get_recent_governance_decisions(self, limit: int = 20) -> list[dict]:
        cur = self.conn.execute(
            """
            SELECT * FROM governance_decisions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cur.fetchall():
            data = dict(row)
            for key, default in (("gates", []), ("evidence_pack", {})):
                try:
                    data[key] = json.loads(data.get(key) or json.dumps(default))
                except json.JSONDecodeError:
                    data[key] = default
            data["promotion_allowed"] = bool(data.get("promotion_allowed"))
            rows.append(data)
        return rows

    def get_recent_alerts(self, limit: int = 30) -> list[dict]:
        cur = self.conn.execute(
            """
            SELECT * FROM alerts
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cur.fetchall():
            data = dict(row)
            try:
                data["evidence"] = json.loads(data.get("evidence") or "{}")
            except json.JSONDecodeError:
                data["evidence"] = {}
            data["requires_human"] = bool(data.get("requires_human"))
            rows.append(data)
        return rows

    def diff_file(self, editable_file: str) -> str:
        if not editable_file:
            return ""
        r = subprocess.run(
            ["git", "diff", "--", editable_file],
            capture_output=True,
            text=True,
            cwd=self.work_dir,
        )
        return r.stdout

    def _memory_row_to_dict(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        for key in ("change_types", "guardrails_failed"):
            try:
                data[key] = json.loads(data[key] or "[]")
            except json.JSONDecodeError:
                data[key] = []
        try:
            data["evidence"] = json.loads(data.get("evidence") or "{}")
        except json.JSONDecodeError:
            data["evidence"] = {}
        data["correctness_passed"] = bool(data.get("correctness_passed"))
        return data

    # --- Absorbed Git Operations from ExperimentHistory ---

    def commit(self, message: str, editable_file: str) -> None:
        if editable_file:
            subprocess.run(["git", "add", editable_file], cwd=self.work_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=self.work_dir, capture_output=True)

    def revert_file(self, editable_file: str) -> None:
        if editable_file:
            subprocess.run(["git", "checkout", "--", editable_file], cwd=self.work_dir, capture_output=True)

    def current_commit(self) -> str:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=self.work_dir
        )
        return r.stdout.strip()

    def get_history(self, limit: int = 60) -> List[RunRecord]:
        r = subprocess.run(
            ["git", "log", "--format=%H|%h|%s|%an|%ar|%ai", f"-{limit}"],
            capture_output=True, text=True, cwd=self.work_dir
        )
        commits = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("|", 5)
            if len(parts) == 6:
                commits.append(RunRecord(
                    hash=parts[0], short=parts[1], message=parts[2],
                    author=parts[3], relative=parts[4], date=parts[5]
                ))
        return commits

    def get_diff(self, commit_hash: str) -> dict:
        stat = subprocess.run(
            ["git", "show", "--stat", commit_hash],
            capture_output=True, text=True, cwd=self.work_dir
        )
        diff = subprocess.run(
            ["git", "show", commit_hash],
            capture_output=True, text=True, cwd=self.work_dir
        )
        return {"stat": stat.stdout, "diff": diff.stdout}
        
    def get_recent_log_stat(self, limit: int = 10) -> str:
        r = subprocess.run(
            ["git", "log", "-n", str(limit), "--stat"], 
            cwd=self.work_dir, capture_output=True, text=True, timeout=5
        )
        return r.stdout
