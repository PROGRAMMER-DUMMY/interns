"""
tools/optimizer_finder.py — performance hotspot finder for SQL and Python.

SQL mode:    DuckDB JSON profiling → operator-level hotspots + rule-based suggestions.
Python mode: cProfile + tracemalloc subprocess → CPU + memory hotspots.

Always exits 0 and writes hotspots.json even on timeout/error so the eval loop
never breaks. Prints a plain-text summary to stdout which evaluator.py captures
in run.log — interns read it automatically in the next iteration.

Usage:
  uv run python tools/optimizer_finder.py --target file.sql [--db analytics.duckdb]
  uv run python tools/optimizer_finder.py --target file.py
  uv run python tools/optimizer_finder.py --mode sql --target file.sql --timeout 20
"""
from __future__ import annotations

import argparse
import concurrent.futures
import cProfile
import io
import json
import os
import pstats
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent.parent

DEFAULT_DB  = str(ROOT / "state" / "analytics.duckdb")
DEFAULT_OUT = str(ROOT / "state" / "hotspots.json")


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class OptimizerResult:
    status: str = "ok"          # ok | timeout | error
    mode: str = "sql"
    target: str = ""
    duration_ms: float = 0.0
    sql_hotspots: list = field(default_factory=list)
    cpu_hotspots: list = field(default_factory=list)
    memory_hotspots: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    error: str = ""


# ── SQL profiling ─────────────────────────────────────────────────────────────

class SQLOptimizer:
    # Thresholds for rule-based suggestions
    SCAN_THRESHOLD   = 0.20   # flag SEQ_SCAN consuming >20% of runtime
    DOMINANCE_THRESHOLD = 0.65  # flag any operator consuming >65%
    AGG_THRESHOLD    = 0.25

    def __init__(self, db_path: str, timeout_s: int = 25):
        self.db_path = db_path
        self.timeout_s = timeout_s

    def analyze(self, sql_path: str) -> OptimizerResult:
        result = OptimizerResult(mode="sql", target=sql_path)
        sql = Path(sql_path).read_text(encoding="utf-8").rstrip().rstrip(";")
        t0 = time.time()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(self._run_duckdb_profile, sql)
            try:
                profile = future.result(timeout=self.timeout_s)
            except concurrent.futures.TimeoutError:
                result.status = "timeout"
                result.duration_ms = self.timeout_s * 1000.0
                result.suggestions.append(_sug(
                    "critical", "timeout",
                    f"EXPLAIN ANALYZE timed out ({self.timeout_s}s). "
                    "The query itself is the bottleneck. Try reducing data "
                    "scanned with earlier WHERE filters."
                ))
                return result
            except Exception as exc:
                result.status = "error"
                result.error = str(exc)[:400]
                result.suggestions.append(_sug(
                    "critical", "error",
                    f"DuckDB profiling failed: {exc}"
                ))
                return result

        result.duration_ms = round((time.time() - t0) * 1000, 1)

        if profile:
            root_timing = float(profile.get("timing", 0.0)) or 1.0
            hotspots = _walk_profile(profile, root_timing)
            hotspots.sort(key=lambda h: h["time_ms"], reverse=True)
            result.sql_hotspots = hotspots[:10]
            result.suggestions = self._apply_rules(hotspots, root_timing)
        else:
            result.suggestions.append(_sug(
                "info", "no_profile",
                "Profiling output was empty — DuckDB may not have written the profile file."
            ))

        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_duckdb_profile(self, sql: str) -> dict | None:
        import duckdb  # imported here so the module loads even without duckdb

        fd, profile_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)

        try:
            conn = duckdb.connect(self.db_path)
            fwd = profile_path.replace("\\", "/")   # DuckDB wants forward slashes

            conn.execute("PRAGMA enable_profiling='json'")
            conn.execute(f"PRAGMA profiling_output='{fwd}'")

            conn.execute("DROP TABLE IF EXISTS _optimizer_tmp")
            conn.execute(f"CREATE TABLE _optimizer_tmp AS (\n{sql}\n)")
            conn.execute("DROP TABLE IF EXISTS _optimizer_tmp")
            conn.execute("PRAGMA disable_profiling")
            conn.close()

            p = Path(profile_path)
            if p.exists() and p.stat().st_size > 2:
                return json.loads(p.read_text(encoding="utf-8"))
            return None
        finally:
            try:
                os.unlink(profile_path)
            except OSError:
                pass

    def _apply_rules(self, hotspots: list[dict], root_timing_s: float) -> list[dict]:
        suggestions: list[dict] = []
        table_counts: dict[str, int] = {}
        seen: set[str] = set()

        for h in hotspots:
            op    = h["operator"].upper()
            pct   = h["pct"] / 100.0
            table = h.get("table", "")

            if "SEQ_SCAN" in op and pct > self.SCAN_THRESHOLD and "seq_scan" not in seen:
                seen.add("seq_scan")
                suggestions.append(_sug(
                    "high", "full_table_scan",
                    f"SEQ_SCAN on '{table or 'table'}' consumes {h['pct']}% of runtime. "
                    "Push WHERE filters earlier or restructure CTEs to filter before joins."
                ))

            if ("CROSS" in op or "BLOCKWISE_NL" in op) and "cartesian" not in seen:
                seen.add("cartesian")
                suggestions.append(_sug(
                    "critical", "cartesian_product",
                    f"Possible cartesian product ({h['operator']}). "
                    "Ensure all JOIN conditions are explicit — no implicit cross-joins."
                ))

            if pct > self.DOMINANCE_THRESHOLD and "dominance" not in seen:
                seen.add("dominance")
                suggestions.append(_sug(
                    "high", "operator_dominates",
                    f"{h['operator']} dominates {h['pct']}% of total runtime. "
                    "Focus optimization effort here first."
                ))

            if ("HASH_GROUP_BY" in op or "PERFECT_HASH" in op) and pct > self.AGG_THRESHOLD and "heavy_agg" not in seen:
                seen.add("heavy_agg")
                suggestions.append(_sug(
                    "medium", "heavy_aggregation",
                    f"Aggregation ({h['operator']}) uses {h['pct']}% of runtime. "
                    "Pre-filter before GROUP BY to reduce input cardinality."
                ))

            if table:
                table_counts[table] = table_counts.get(table, 0) + 1

        for table, count in table_counts.items():
            if count > 1:
                suggestions.append(_sug(
                    "medium", "repeated_scan",
                    f"'{table}' appears {count}x in the plan. "
                    "Consider materialising it once as a CTE."
                ))

        if not suggestions:
            suggestions.append(_sug(
                "info", "no_issues",
                "No obvious anti-patterns detected. Query plan looks well-structured for DuckDB."
            ))

        return suggestions


# ── Python profiling ──────────────────────────────────────────────────────────

class PythonOptimizer:
    TOP_N   = 15
    TIMEOUT = 120  # seconds per profiling pass

    def analyze(self, target_path: str) -> OptimizerResult:
        result = OptimizerResult(mode="python", target=target_path)
        t0 = time.time()

        try:
            result.cpu_hotspots    = self._profile_cpu(target_path)
            result.memory_hotspots = self._profile_memory(target_path)
            result.suggestions     = self._apply_rules(result.cpu_hotspots, result.memory_hotspots)
        except Exception as exc:
            result.status = "error"
            result.error  = str(exc)[:400]
            result.suggestions.append(_sug("critical", "error", f"Python profiling failed: {exc}"))

        result.duration_ms = round((time.time() - t0) * 1000, 1)
        return result

    def _profile_cpu(self, path: str) -> list[dict]:
        fd, pstats_path = tempfile.mkstemp(suffix=".pstats")
        os.close(fd)
        try:
            subprocess.run(
                [sys.executable, "-m", "cProfile", "-o", pstats_path, path],
                capture_output=True, text=True, timeout=self.TIMEOUT
            )
            p = Path(pstats_path)
            if not p.exists() or p.stat().st_size == 0:
                return []

            stats = pstats.Stats(pstats_path, stream=io.StringIO())
            total = stats.total_tt or 1.0
            root  = str(ROOT)

            hotspots = []
            for (fname, lineno, fn), (cc, nc, tt, ct, _) in stats.stats.items():
                if root not in fname and "site-packages" in fname:
                    continue
                hotspots.append({
                    "file":    Path(fname).name,
                    "line":    lineno,
                    "fn":      fn,
                    "time_ms": round(ct * 1000, 2),
                    "pct":     round(ct / total * 100, 1),
                })

            hotspots.sort(key=lambda h: h["time_ms"], reverse=True)
            return hotspots[: self.TOP_N]
        finally:
            try:
                os.unlink(pstats_path)
            except OSError:
                pass

    def _profile_memory(self, path: str) -> list[dict]:
        # Run tracemalloc in a child process to avoid contaminating the main heap.
        wrapper = (
            "import tracemalloc, json, sys; "
            "tracemalloc.start(); "
            f"code = compile(open(r'{path}', encoding='utf-8').read(), r'{path}', 'exec'); "
            "exec(code, {'__name__': '__main__', '__file__': r'" + path + "'});"
        )
        collect = (
            "snap = tracemalloc.take_snapshot(); "
            "stats = snap.statistics('lineno')[:15]; "
            "total = sum(s.size for s in stats) or 1; "
            "print(json.dumps([{"
            "'file': s.traceback[0].filename, "
            "'line': s.traceback[0].lineno, "
            "'size_mb': round(s.size/1048576, 3), "
            "'pct': round(s.size/total*100, 1)"
            "} for s in stats]))"
        )
        full = wrapper + collect

        try:
            res = subprocess.run(
                [sys.executable, "-c", full],
                capture_output=True, text=True, timeout=self.TIMEOUT
            )
            out = res.stdout.strip()
            if out:
                data = json.loads(out)
                for h in data:
                    h["file"] = Path(h["file"]).name
                return data
        except Exception:
            pass
        return []

    def _apply_rules(self, cpu: list[dict], mem: list[dict]) -> list[dict]:
        suggestions: list[dict] = []

        if cpu and cpu[0]["pct"] > 60:
            h = cpu[0]
            suggestions.append(_sug(
                "high", "cpu_hotspot",
                f"{h['fn']} in {h['file']}:{h['line']} dominates {h['pct']}% "
                f"of CPU time ({h['time_ms']}ms). Optimise or parallelise this function."
            ))

        if mem and mem[0]["size_mb"] > 100:
            h = mem[0]
            suggestions.append(_sug(
                "high", "memory_hotspot",
                f"{h['file']}:{h['line']} allocates {h['size_mb']}MB "
                f"({h['pct']}% of tracked memory). Consider streaming or chunking."
            ))

        if not suggestions:
            suggestions.append(_sug("info", "no_issues", "No dominant hotspot detected."))

        return suggestions


# ── Profile tree walker ───────────────────────────────────────────────────────

def _walk_profile(node: dict, root_timing: float) -> list[dict]:
    results: list[dict] = []
    _recurse(node, root_timing, results)
    return results


def _recurse(node: dict, root_timing: float, out: list[dict]) -> None:
    name   = node.get("name", "UNKNOWN")
    timing = float(node.get("timing", 0.0))
    extra  = node.get("extra_info") or {}
    table  = ""
    if isinstance(extra, dict):
        table = str(extra.get("Table", extra.get("table", "")))

    pct = round(timing / root_timing * 100, 1) if root_timing > 0 else 0.0
    if timing > 0:
        out.append({
            "operator": name,
            "table":    table,
            "time_ms":  round(timing * 1000, 1),
            "pct":      pct,
        })

    for child in node.get("children", []):
        _recurse(child, root_timing, out)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sug(severity: str, type_: str, message: str) -> dict:
    return {"severity": severity, "type": type_, "message": message}


def _print_summary(r: OptimizerResult) -> None:
    SEV = {"critical": "[CRITICAL]", "high": "[HIGH]", "medium": "[MEDIUM]", "info": "[INFO]"}
    print(f"\n=== optimizer_finder | {r.mode.upper()} | {Path(r.target).name} | status={r.status} | {r.duration_ms:.0f}ms ===")

    if r.sql_hotspots:
        print("  -- SQL operator hotspots (top 5 by runtime) --")
        for h in r.sql_hotspots[:5]:
            label = h["operator"] + (f" [{h['table']}]" if h.get("table") else "")
            print(f"    {h['pct']:5.1f}%  {h['time_ms']:8.1f}ms  {label}")

    if r.cpu_hotspots:
        print("  -- CPU hotspots (cumulative time) --")
        for h in r.cpu_hotspots[:5]:
            print(f"    {h['pct']:5.1f}%  {h['time_ms']:8.1f}ms  {h['fn']}  ({h['file']}:{h['line']})")

    if r.memory_hotspots:
        print("  -- Memory hotspots --")
        for h in r.memory_hotspots[:5]:
            print(f"    {h['pct']:5.1f}%  {h['size_mb']:6.1f}MB  {h['file']}:{h['line']}")

    if r.suggestions:
        print("  -- Suggestions --")
        for s in r.suggestions:
            print(f"    {SEV.get(s['severity'], '[INFO]')} {s['message']}")

    print("=== optimizer_finder done ===\n")


def _write_json(r: OptimizerResult, out_path: str) -> None:
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(r), indent=2), encoding="utf-8")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Performance hotspot finder for SQL and Python.")
    ap.add_argument("--target",  required=True, help="Path to .sql or .py file")
    ap.add_argument("--mode",    choices=["sql", "python", "auto"], default="auto")
    ap.add_argument("--db",      default=DEFAULT_DB,  help="DuckDB database (SQL mode)")
    ap.add_argument("--out",     default=DEFAULT_OUT, help="Output JSON path")
    ap.add_argument("--timeout", type=int, default=25, help="Timeout seconds (SQL mode)")
    args = ap.parse_args()

    mode = args.mode
    if mode == "auto":
        mode = "sql" if args.target.endswith(".sql") else "python"

    if mode == "sql":
        result = SQLOptimizer(db_path=args.db, timeout_s=args.timeout).analyze(args.target)
    else:
        result = PythonOptimizer().analyze(args.target)

    _print_summary(result)
    _write_json(result, args.out)
    print(f"[optimizer_finder] hotspots written -> {args.out}")


if __name__ == "__main__":
    main()
