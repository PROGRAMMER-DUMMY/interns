"""Dependency-free AI application test harness.

V1 is CI-safe by default: local stub targets run without network, HTTP targets
are blocked unless explicitly enabled, and secret values are read only from env.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT
from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout


HARNESS_VERSION = 2
EVAL_TYPES = {
    "exact_match",
    "schema_check",
    "keyword",
    "sql_semantic",
    "kpi_mapping",
    "result_table",
}
TARGETS = {"local_stub", "http_ai"}


@dataclass
class AITestRecord:
    id: str
    target: str
    eval_type: str
    tags: list[str]
    input: str
    output: str
    expected: Any = None
    latency_ms: int = 0
    error: str | None = None
    status: str = "failed"
    passed: bool = False
    score: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AIAppHarnessResult:
    workspace: str
    ok: bool
    status: str
    run_id: str
    report_path: str
    current_json_path: str
    current_markdown_path: str
    outputs_path: str
    total: int
    passed: int
    failed: int
    errors: int
    blocked: int
    baseline_regressions: int
    pass_rate: float

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "ai_app_harness_result",
            "version": HARNESS_VERSION,
            "generated_by": "run-ai-app-harness",
            **asdict(self),
        }


class AIAppHarness:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        dataset: str | Path,
        config_path: str | Path | None = None,
        tags: list[str] | None = None,
        allow_remote_ai: bool = False,
        baseline_path: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.dataset_path = self._resolve_dataset(dataset)
        self.config_path = self._resolve_optional(config_path) if config_path else None
        self.tags = tags or []
        self.allow_remote_ai = allow_remote_ai
        self.baseline_path = self._resolve_optional(baseline_path) if baseline_path else None

    def run(self) -> AIAppHarnessResult:
        self._validate_workspace()
        self._validate_dataset_scope()
        self.layout.ensure_runtime_dirs()
        config = self._load_config()
        cases = _filter_cases(_load_jsonl(self.dataset_path), self.tags)
        baseline = _load_baseline(self.baseline_path)
        run_id = datetime.now(timezone.utc).strftime("run_%Y_%m_%d_%H%M%S")
        run_dir = self.layout.interns_dir / "ai_harness" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        outputs_path = run_dir / "outputs.jsonl"

        records: list[AITestRecord] = []
        for case in cases:
            record = self._run_case(case, config)
            records.append(record)
            with outputs_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.summary(), default=str) + "\n")

        baseline_regressions = _baseline_regressions(records, baseline)
        report = _build_report(
            workspace=self.workspace_rel,
            dataset=_rel(self.dataset_path, self.repo_root),
            config=_safe_config_summary(config),
            run_id=run_id,
            records=records,
            threshold=float(config.get("pass_threshold", 0.8)),
            allow_remote_ai=self.allow_remote_ai,
            baseline_regressions=baseline_regressions,
            baseline_path=_rel(self.baseline_path, self.repo_root) if self.baseline_path else "",
        )
        report_path = run_dir / "report.json"
        report_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

        reports_dir = self.layout.reports_dir / "ai_app_harness"
        evidence_dir = self.layout.evidence_dir / "ai_app_harness"
        reports_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        current_json = reports_dir / "current.json"
        current_md = reports_dir / "current.md"
        evidence_current = evidence_dir / "current.json"
        current_json.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        evidence_current.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        current_md.write_text(_render_markdown(report), encoding="utf-8")

        result = AIAppHarnessResult(
            workspace=self.workspace_rel,
            ok=bool(report["ok"]),
            status=str(report["status"]),
            run_id=run_id,
            report_path=_rel(report_path, self.repo_root),
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            outputs_path=_rel(outputs_path, self.repo_root),
            total=int(report["summary"]["total"]),
            passed=int(report["summary"]["passed"]),
            failed=int(report["summary"]["failed"]),
            errors=int(report["summary"]["errors"]),
            blocked=int(report["summary"]["blocked"]),
            baseline_regressions=len(baseline_regressions),
            pass_rate=float(report["summary"]["pass_rate"]),
        )
        return result

    def _run_case(self, case: dict[str, Any], config: dict[str, Any]) -> AITestRecord:
        target = str(case.get("target") or "local_stub")
        eval_type = str(case.get("eval_type") or "exact_match")
        case_id = str(case.get("id") or "")
        input_text = str(case.get("input") or "")
        tags = [str(tag) for tag in case.get("tags", []) if str(tag)]
        if target not in TARGETS:
            return _invalid_record(case, f"unsupported target `{target}`")
        if eval_type not in EVAL_TYPES:
            return _invalid_record(case, f"unsupported eval_type `{eval_type}`")
        if not case_id:
            return _invalid_record(case, "missing required `id`")
        if target == "http_ai" and not self.allow_remote_ai:
            return AITestRecord(
                id=case_id,
                target=target,
                eval_type=eval_type,
                tags=tags,
                input=input_text,
                output="",
                expected=case.get("expected"),
                error="remote AI target blocked; rerun with --allow-remote-ai",
                status="blocked_remote_ai",
                passed=False,
                score=0.0,
            )

        call = _call_local_stub(case) if target == "local_stub" else _call_http_ai(input_text, config)
        output = str(call.get("output") or "")
        if call.get("error"):
            return AITestRecord(
                id=case_id,
                target=target,
                eval_type=eval_type,
                tags=tags,
                input=input_text,
                output=output,
                expected=case.get("expected"),
                latency_ms=int(call.get("latency_ms") or 0),
                error=str(call["error"]),
                status="error",
                passed=False,
                score=0.0,
            )
        evaluation = _evaluate(eval_type, output, case)
        return AITestRecord(
            id=case_id,
            target=target,
            eval_type=eval_type,
            tags=tags,
            input=input_text,
            output=output,
            expected=case.get("expected"),
            latency_ms=int(call.get("latency_ms") or 0),
            error=None,
            status="passed" if evaluation["passed"] else "failed",
            passed=bool(evaluation["passed"]),
            score=float(evaluation.get("score") or 0.0),
            details={k: v for k, v in evaluation.items() if k not in {"passed", "score"}},
        )

    def _load_config(self) -> dict[str, Any]:
        defaults = {
            "api_url": "",
            "api_key_env": "",
            "model": "",
            "max_tokens": 1000,
            "timeout_seconds": 30,
            "pass_threshold": 0.8,
            "auth_header_name": "Authorization",
            "auth_header_prefix": "Bearer ",
            "extra_headers": {},
        }
        if not self.config_path:
            return defaults
        data = _load_json(self.config_path)
        if "api_key" in data:
            raise ValueError("AI harness config must not contain `api_key`; use `api_key_env`")
        return {**defaults, **data}

    def _resolve_dataset(self, dataset: str | Path) -> Path:
        path = Path(dataset)
        return (self.repo_root / path).resolve() if not path.is_absolute() else path.resolve()

    def _resolve_optional(self, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        path = Path(value)
        return (self.repo_root / path).resolve() if not path.is_absolute() else path.resolve()

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace_rel}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _validate_dataset_scope(self) -> None:
        if not self.dataset_path.exists():
            raise FileNotFoundError(f"dataset not found: {_rel(self.dataset_path, self.repo_root)}")
        allowed = (self.layout.interns_dir / "ai_harness" / "datasets").resolve()
        if not self.dataset_path.is_relative_to(allowed):
            raise ValueError(f"dataset must be under {_rel(allowed, self.repo_root)}")


def _call_local_stub(case: dict[str, Any]) -> dict[str, Any]:
    start = time.time()
    output = case.get("stub_output", case.get("input", ""))
    return {"output": output, "latency_ms": int((time.time() - start) * 1000), "error": None}


def _call_http_ai(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    start = time.time()
    api_url = str(config.get("api_url") or "")
    key_env = str(config.get("api_key_env") or "")
    api_key = os.environ.get(key_env) if key_env else ""
    if not api_url:
        return _call_error(start, "missing `api_url` in AI harness config")
    if key_env and not api_key:
        return _call_error(start, f"environment variable `{key_env}` is not set")
    payload = json.dumps(
        {
            "model": config.get("model"),
            "max_tokens": int(config.get("max_tokens") or 1000),
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update({str(k): str(v) for k, v in (config.get("extra_headers") or {}).items()})
    if api_key:
        header_name = str(config.get("auth_header_name") or "Authorization")
        prefix = str(config.get("auth_header_prefix") or "")
        headers[header_name] = f"{prefix}{api_key}"
    request = urllib.request.Request(url=api_url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=float(config.get("timeout_seconds") or 30)) as response:
            body = json.loads(response.read().decode("utf-8"))
        return {
            "output": _extract_output(body),
            "latency_ms": int((time.time() - start) * 1000),
            "error": None,
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return _call_error(start, str(exc))


def _call_error(start: float, message: str) -> dict[str, Any]:
    return {"output": "", "latency_ms": int((time.time() - start) * 1000), "error": message}


def _extract_output(body: dict[str, Any]) -> str:
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    content = body.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            return first["text"]
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    raise KeyError("could not extract text output from HTTP response")


def _evaluate(eval_type: str, output: str, case: dict[str, Any]) -> dict[str, Any]:
    if eval_type == "schema_check":
        return _schema_check(output, case.get("expected_keys", []))
    if eval_type == "keyword":
        return _keyword_check(output, case.get("keywords", []))
    if eval_type == "sql_semantic":
        return _sql_semantic_check(output, case)
    if eval_type == "kpi_mapping":
        return _kpi_mapping_check(output, case)
    if eval_type == "result_table":
        return _result_table_check(output, case)
    return _exact_match(output, str(case.get("expected") or ""))


def _exact_match(output: str, expected: str) -> dict[str, Any]:
    passed = output.strip().lower() == expected.strip().lower()
    return {"passed": passed, "score": 1.0 if passed else 0.0}


def _schema_check(output: str, expected_keys: Any) -> dict[str, Any]:
    keys = [str(key) for key in expected_keys] if isinstance(expected_keys, list) else []
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return {"passed": False, "score": 0.0, "missing_keys": keys, "parse_error": True}
    missing = [key for key in keys if key not in parsed]
    score = (len(keys) - len(missing)) / len(keys) if keys else 0.0
    return {"passed": not missing, "score": round(score, 3), "missing_keys": missing}


def _keyword_check(output: str, keywords: Any) -> dict[str, Any]:
    words = [str(keyword) for keyword in keywords] if isinstance(keywords, list) else []
    lower = output.lower()
    found = [word for word in words if word.lower() in lower]
    score = len(found) / len(words) if words else 0.0
    return {"passed": score == 1.0, "score": round(score, 3), "found": found}


def _sql_semantic_check(output: str, case: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_sql(output)
    checks: list[tuple[str, bool]] = []
    checks.extend(_contains_checks("required_clauses", normalized, case.get("required_clauses")))
    checks.extend(_contains_checks("required_tables", normalized, case.get("required_tables")))
    checks.extend(_contains_checks("required_columns", normalized, case.get("required_columns")))
    checks.extend(_contains_checks("required_joins", normalized, case.get("required_joins")))
    checks.extend(_contains_checks("required_filters", normalized, case.get("required_filters")))
    checks.extend(
        (f"forbidden:{item}", _needle(str(item)) not in normalized)
        for item in _string_list(case.get("forbidden_patterns"))
    )
    if not checks:
        return {"passed": False, "score": 0.0, "missing": ["no sql semantic assertions configured"]}
    failed = [name for name, passed in checks if not passed]
    score = (len(checks) - len(failed)) / len(checks)
    return {
        "passed": not failed,
        "score": round(score, 3),
        "missing": failed,
        "assertion_count": len(checks),
    }


def _kpi_mapping_check(output: str, case: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("expected_mappings")
    expected_rows = expected if isinstance(expected, list) else []
    if not expected_rows:
        return {"passed": False, "score": 0.0, "missing": ["no expected_mappings configured"]}
    parsed = _parse_json_object(output)
    if parsed is None:
        return {"passed": False, "score": 0.0, "missing": ["output is not valid JSON"]}
    actual_rows = _mapping_rows_from_payload(parsed)
    checks: list[tuple[str, bool]] = []
    for item in expected_rows:
        if not isinstance(item, dict):
            continue
        feature = str(item.get("feature") or "")
        label = feature or "<missing-feature>"
        actual = actual_rows.get(_key(feature), {})
        for field_name, actual_keys in {
            "column": ("column", "mapping_or_source_column", "source_column"),
            "dataset": ("dataset", "source_dataset"),
            "state": ("state", "status"),
            "join_key": ("join_key", "relationship_key"),
            "grain": ("grain",),
            "filter": ("filter", "where"),
        }.items():
            expected_value = item.get(field_name)
            if expected_value in (None, ""):
                continue
            actual_value = _first_present(actual, actual_keys)
            checks.append((f"{label}.{field_name}", _matches_value(actual_value, expected_value)))
    if not checks:
        return {"passed": False, "score": 0.0, "missing": ["no comparable mapping assertions"]}
    failed = [name for name, passed in checks if not passed]
    score = (len(checks) - len(failed)) / len(checks)
    return {
        "passed": not failed,
        "score": round(score, 3),
        "missing": failed,
        "assertion_count": len(checks),
    }


def _result_table_check(output: str, case: dict[str, Any]) -> dict[str, Any]:
    parsed = _parse_json_object(output)
    if parsed is None:
        return {"passed": False, "score": 0.0, "missing": ["output is not valid JSON"]}
    table = _table_from_payload(parsed)
    columns = [str(column) for column in table["columns"]]
    rows = table["rows"]
    row_count = int(table["row_count"])
    checks: list[tuple[str, bool]] = []
    expected_columns = _string_list(case.get("expected_columns"))
    checks.extend((f"column:{column}", column in columns) for column in expected_columns)
    if case.get("exact_columns") is True and expected_columns:
        checks.append(("exact_columns", columns == expected_columns))
    if case.get("min_rows") is not None:
        checks.append(("min_rows", row_count >= int(case.get("min_rows") or 0)))
    if case.get("max_rows") is not None:
        checks.append(("max_rows", row_count <= int(case.get("max_rows") or 0)))
    checks.extend(_expected_value_checks(rows, case.get("expected_values")))
    if not checks:
        return {"passed": False, "score": 0.0, "missing": ["no result table assertions configured"]}
    failed = [name for name, passed in checks if not passed]
    score = (len(checks) - len(failed)) / len(checks)
    return {
        "passed": not failed,
        "score": round(score, 3),
        "missing": failed,
        "assertion_count": len(checks),
        "result_signature": {
            "columns": columns,
            "row_count": row_count,
            "pinned_values": _pinned_values(rows, case.get("expected_values")),
        },
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError(f"{_rel(path, PROJECT_ROOT)}:{idx} must be a JSON object")
        cases.append(data)
    return cases


def _parse_json_object(output: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.lower().replace('"', "").replace("`", "").split())


def _needle(value: str) -> str:
    return " ".join(value.lower().replace('"', "").replace("`", "").split())


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _contains_checks(prefix: str, haystack: str, values: Any) -> list[tuple[str, bool]]:
    return [(f"{prefix}:{item}", _needle(str(item)) in haystack) for item in _string_list(values)]


def _key(value: Any) -> str:
    return str(value or "").strip().lower()


def _mapping_rows_from_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("mappings", "mapping_rows", "features"):
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    kpis = payload.get("kpis")
    if isinstance(kpis, list):
        for kpi in kpis:
            if not isinstance(kpi, dict):
                continue
            for key in ("mappings", "mapping_rows", "features"):
                value = kpi.get(key)
                if isinstance(value, list):
                    rows.extend(item for item in value if isinstance(item, dict))
    return {
        _key(row.get("feature") or row.get("name") or row.get("kpi_feature")): row
        for row in rows
        if row.get("feature") or row.get("name") or row.get("kpi_feature")
    }


def _first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
    return None


def _matches_value(actual: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_matches_value(actual, item) for item in expected)
    if actual is None:
        return False
    return _key(actual) == _key(expected)


def _table_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        rows = payload.get("data") if isinstance(payload.get("data"), list) else []
    rows = [row for row in rows if isinstance(row, dict)]
    columns = payload.get("columns")
    if not isinstance(columns, list):
        columns = list(rows[0].keys()) if rows else []
    row_count = payload.get("row_count", len(rows))
    return {"columns": columns, "rows": rows, "row_count": int(row_count or 0)}


def _expected_value_checks(rows: list[dict[str, Any]], values: Any) -> list[tuple[str, bool]]:
    checks = []
    for idx, item in enumerate(values if isinstance(values, list) else []):
        if not isinstance(item, dict):
            continue
        column = str(item.get("column") or "")
        expected = item.get("value")
        row_selector = item.get("row")
        label = str(item.get("label") or f"value:{idx + 1}:{column}")
        row = _select_row(rows, row_selector)
        actual = row.get(column) if row and column else None
        tolerance = float(item.get("tolerance") or 0)
        checks.append((label, _value_equal(actual, expected, tolerance)))
    return checks


def _select_row(rows: list[dict[str, Any]], selector: Any) -> dict[str, Any] | None:
    if selector is None:
        return rows[0] if rows else None
    if isinstance(selector, int):
        return rows[selector] if 0 <= selector < len(rows) else None
    if isinstance(selector, dict):
        for row in rows:
            if all(_matches_value(row.get(str(key)), value) for key, value in selector.items()):
                return row
    return None


def _value_equal(actual: Any, expected: Any, tolerance: float) -> bool:
    if actual is None:
        return False
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return abs(float(actual) - float(expected)) <= tolerance
    return _key(actual) == _key(expected)


def _pinned_values(rows: list[dict[str, Any]], values: Any) -> list[dict[str, Any]]:
    pinned = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        column = str(item.get("column") or "")
        row = _select_row(rows, item.get("row"))
        pinned.append(
            {
                "label": str(item.get("label") or column),
                "column": column,
                "row": item.get("row"),
                "value": row.get(column) if row and column else None,
            }
        )
    return pinned


def _filter_cases(cases: list[dict[str, Any]], tags: list[str]) -> list[dict[str, Any]]:
    if not tags:
        return cases
    selected = set(tags)
    return [case for case in cases if selected.intersection({str(tag) for tag in case.get("tags", [])})]


def _invalid_record(case: dict[str, Any], error: str) -> AITestRecord:
    return AITestRecord(
        id=str(case.get("id") or "<missing>"),
        target=str(case.get("target") or ""),
        eval_type=str(case.get("eval_type") or ""),
        tags=[str(tag) for tag in case.get("tags", []) if str(tag)],
        input=str(case.get("input") or ""),
        output="",
        expected=case.get("expected"),
        error=error,
        status="error",
        passed=False,
        score=0.0,
    )


def _build_report(
    *,
    workspace: str,
    dataset: str,
    config: dict[str, Any],
    run_id: str,
    records: list[AITestRecord],
    threshold: float,
    allow_remote_ai: bool,
    baseline_regressions: list[dict[str, Any]],
    baseline_path: str,
) -> dict[str, Any]:
    total = len(records)
    passed = sum(1 for record in records if record.passed)
    errors = sum(1 for record in records if record.status == "error")
    blocked = sum(1 for record in records if record.status.startswith("blocked"))
    failed = total - passed - errors - blocked
    pass_rate = round(passed / total, 3) if total else 0.0
    avg_latency = (
        round(sum(record.latency_ms for record in records) / total, 1) if total else 0.0
    )
    ok = pass_rate >= threshold and blocked == 0 and not baseline_regressions
    status = "passed" if ok else "blocked"
    return {
        "artifact_type": "ai_app_harness/current.json",
        "version": HARNESS_VERSION,
        "generated_by": "run-ai-app-harness",
        "workspace": workspace,
        "dataset": dataset,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "allow_remote_ai": allow_remote_ai,
        "config": config,
        "ok": ok,
        "status": status,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "blocked": blocked,
            "pass_rate": pass_rate,
            "avg_latency_ms": avg_latency,
            "pass_threshold": threshold,
            "baseline_regressions": len(baseline_regressions),
        },
        "coverage": _coverage(records),
        "baseline": {"path": baseline_path, "regressions": baseline_regressions},
        "records": [record.summary() for record in records],
    }


def _coverage(records: list[AITestRecord]) -> dict[str, Any]:
    by_eval: dict[str, int] = {}
    by_target: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    for record in records:
        by_eval[record.eval_type] = by_eval.get(record.eval_type, 0) + 1
        by_target[record.target] = by_target.get(record.target, 0) + 1
        for tag in record.tags:
            by_tag[tag] = by_tag.get(tag, 0) + 1
    reliability_checks = {
        "deterministic_content": sum(by_eval.get(name, 0) for name in ("exact_match", "schema_check", "keyword")),
        "sql_semantics": by_eval.get("sql_semantic", 0),
        "kpi_mapping": by_eval.get("kpi_mapping", 0),
        "result_regression": by_eval.get("result_table", 0),
        "adversarial": by_tag.get("adversarial", 0),
    }
    return {
        "by_eval_type": dict(sorted(by_eval.items())),
        "by_target": dict(sorted(by_target.items())),
        "by_tag": dict(sorted(by_tag.items())),
        "reliability_checks": reliability_checks,
    }


def _safe_config_summary(config: dict[str, Any]) -> dict[str, Any]:
    key_env = str(config.get("api_key_env") or "")
    return {
        "api_url": str(config.get("api_url") or ""),
        "api_key_env": key_env,
        "api_key_status": "set" if key_env and os.environ.get(key_env) else "missing" if key_env else "not_configured",
        "model": str(config.get("model") or ""),
        "max_tokens": int(config.get("max_tokens") or 0),
        "timeout_seconds": float(config.get("timeout_seconds") or 0),
        "pass_threshold": float(config.get("pass_threshold") or 0.8),
    }


def _load_baseline(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.exists():
        return {}
    data = _load_json(path)
    records = data.get("records") if isinstance(data.get("records"), list) else []
    return {str(record.get("id") or ""): record for record in records if isinstance(record, dict)}


def _baseline_regressions(
    records: list[AITestRecord],
    baseline: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    regressions = []
    for record in records:
        previous = baseline.get(record.id)
        if not previous or previous.get("passed") is not True:
            continue
        previous_score = float(previous.get("score") or 0.0)
        if not record.passed or record.score < previous_score:
            regressions.append(
                {
                    "id": record.id,
                    "previous_passed": True,
                    "current_passed": record.passed,
                    "previous_score": previous_score,
                    "current_score": record.score,
                }
            )
            continue
        signature_diff = _result_signature_regression(record, previous)
        if signature_diff:
            regressions.append({"id": record.id, **signature_diff})
    return regressions


def _result_signature_regression(
    record: AITestRecord,
    previous: dict[str, Any],
) -> dict[str, Any] | None:
    if record.eval_type != "result_table":
        return None
    previous_details = previous.get("details") if isinstance(previous.get("details"), dict) else {}
    previous_signature = previous_details.get("result_signature")
    current_signature = record.details.get("result_signature")
    if not isinstance(previous_signature, dict) or not isinstance(current_signature, dict):
        return None
    mismatches = []
    for key in ("columns", "row_count", "pinned_values"):
        if previous_signature.get(key) != current_signature.get(key):
            mismatches.append(key)
    if not mismatches:
        return None
    return {
        "previous_passed": True,
        "current_passed": record.passed,
        "previous_score": float(previous.get("score") or 0.0),
        "current_score": record.score,
        "signature_mismatches": mismatches,
        "previous_signature": previous_signature,
        "current_signature": current_signature,
    }


def _render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# AI App Harness",
        "",
        f"- Workspace: `{report['workspace']}`",
        f"- Dataset: `{report['dataset']}`",
        f"- Run: `{report['run_id']}`",
        f"- Status: `{report['status']}`",
        "",
        "## Summary",
        "",
        render_markdown_table(
            ["Total", "Passed", "Failed", "Errors", "Blocked", "Pass rate", "Threshold", "Avg latency"],
            [
                [
                    summary["total"],
                    summary["passed"],
                    summary["failed"],
                    summary["errors"],
                    summary["blocked"],
                    summary["pass_rate"],
                    summary["pass_threshold"],
                    summary["avg_latency_ms"],
                ]
            ],
        ),
        "",
        "## Reliability Coverage",
        "",
        render_markdown_table(
            ["Layer", "Cases"],
            [
                [name, count]
                for name, count in report.get("coverage", {})
                .get("reliability_checks", {})
                .items()
            ],
        ),
        "",
        "## Cases",
        "",
        render_markdown_table(
            ["ID", "Target", "Eval", "Status", "Score", "Latency", "Error"],
            [
                [
                    record["id"],
                    record["target"],
                    record["eval_type"],
                    record["status"],
                    record["score"],
                    record["latency_ms"],
                    record.get("error") or "",
                ]
                for record in report["records"]
            ],
        ),
        "",
    ]
    regressions = report.get("baseline", {}).get("regressions") or []
    if regressions:
        lines.extend(
            [
                "## Baseline Regressions",
                "",
                render_markdown_table(
                    ["ID", "Previous score", "Current score", "Current passed"],
                    [
                        [
                            item["id"],
                            item["previous_score"],
                            item["current_score"],
                            item["current_passed"],
                        ]
                        for item in regressions
                    ],
                ),
                "",
            ]
        )
    return "\n".join(lines)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _rel(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the dependency-free AI application harness.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    parser.add_argument("--config")
    parser.add_argument("--tags", default="", help="Comma-separated tag filter.")
    parser.add_argument("--allow-remote-ai", action="store_true")
    parser.add_argument("--baseline")
    args = parser.parse_args(argv)
    result = AIAppHarness(
        args.repo_root,
        args.workspace,
        dataset=args.dataset,
        config_path=args.config,
        tags=[tag.strip() for tag in args.tags.split(",") if tag.strip()],
        allow_remote_ai=args.allow_remote_ai,
        baseline_path=args.baseline,
    ).run()
    print(json.dumps(result.summary(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
