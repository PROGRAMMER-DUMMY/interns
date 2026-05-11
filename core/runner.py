"""
core/runner.py — experiment runner.

Runs the configured experiment command, enforces the time budget,
calls the evaluator, and parses metrics from run.log.
"""
import os
import shlex
import sys
import time
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .parser import MetricParser, RegexLogParser

ROOT = Path(__file__).parent.parent

@dataclass
class RunResult:
    metric: Optional[float]
    token_count: int
    log_path: Path
    exit_code: int


class ExperimentRunner:
    def __init__(self, work_dir: Path, log_path: Path, parser: Optional[MetricParser] = None):
        self.work_dir = work_dir
        self.log_path = log_path
        self.parser = parser or RegexLogParser()

    def run(self, task: dict, time_budget: int, hard_timeout: int) -> RunResult:
        with open(self.log_path, "w", encoding="utf-8") as log_f:
            exp_code = self._run_experiment(
                task["experiment_cmd"], time_budget, hard_timeout, log_f
            )
            if exp_code in (0, 124):
                self._run_evaluator(task["evaluator_cmd"], log_f)

        if self.log_path.exists():
            log_content = self.log_path.read_text(encoding="utf-8", errors="replace")
        else:
            log_content = ""

        metric = self.parser.parse_metric(log_content, task.get("metric_key", "primary_metric"))
        all_metrics = self.parser.parse_all_metrics(log_content)
        token_count = int(all_metrics.get("token_count", 0))

        return RunResult(
            metric=metric,
            token_count=token_count,
            log_path=self.log_path,
            exit_code=exp_code
        )

    def _run_experiment(self, experiment_cmd: list[str], time_budget: int, hard_timeout: int, log_f) -> int:
        self._log(f"Starting: {' '.join(experiment_cmd)}", log_f)
        self._log(f"Budget: {time_budget}s | Hard timeout: {hard_timeout}s", log_f)

        env = os.environ.copy()
        env["AUTORESEARCH_TIME_BUDGET"] = str(time_budget)

        kwargs = dict(cwd=self.work_dir, env=env, stdout=log_f, stderr=subprocess.STDOUT)
        if sys.platform != "win32":
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(experiment_cmd, **kwargs)
        except FileNotFoundError as exc:
            self._log(f"ERROR — could not start experiment: {exc}", log_f)
            return 1

        start = time.time()
        while True:
            try:
                exit_code = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.time() - start > hard_timeout:
                self._kill(proc, f"hard timeout ({hard_timeout}s)", log_f)
                exit_code = 124
                break

        self._log(f"Experiment done. exit={exit_code}  wall={time.time()-start:.1f}s", log_f)
        return exit_code

    def _run_evaluator(self, evaluator_cmd: list[str], log_f) -> int:
        if isinstance(evaluator_cmd, str):
            evaluator_cmd = shlex.split(evaluator_cmd)
        self._log(f"Evaluator: {' '.join(evaluator_cmd)}", log_f)
        try:
            result = subprocess.run(evaluator_cmd, cwd=self.work_dir, stdout=log_f, stderr=subprocess.STDOUT)
            return result.returncode
        except FileNotFoundError as exc:
            self._log(f"ERROR — evaluator not found: {exc}", log_f)
            return 1

    def _parse_metric(self, metric_key: str = "primary_metric") -> Optional[float]:
        if not self.log_path.exists():
            return None
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith(f"{metric_key}:"):
                try:
                    return float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        return None

    def _parse_all_metrics(self) -> dict[str, float]:
        if not self.log_path.exists():
            return {}
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        in_block = False
        metrics: dict[str, float] = {}
        for line in text.splitlines():
            if line.strip() == "---":
                in_block = not in_block
                continue
            if in_block and ":" in line:
                key, _, val = line.partition(":")
                try:
                    metrics[key.strip()] = float(val.strip())
                except ValueError:
                    pass
        return metrics

    def _kill(self, proc: subprocess.Popen, reason: str, log_f) -> None:
        self._log(f"Killing process: {reason}", log_f)
        if sys.platform == "win32":
            proc.terminate()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _log(self, msg: str, log_f) -> None:
        log_f.write(f"[runner] {msg}\n")
        log_f.flush()
