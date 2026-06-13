"""Apply a human-confirmed KPI definition (metric / grain / filters).

Natural-language-question workspaces arrive with KPIs that carry a business
question but no measurable ``metric``/``cuts``. Evidence-based derivation
(``metric_derivation``) fills the unambiguous ones; the genuinely-ambiguous ones
(share %, top-N, derived durations, readmission windows) are surfaced at the
``kpi_definition_required`` blocker for a human to define. Until now there was no
way to APPLY that human answer back into the registry, so those KPIs could be
asked about forever but never complete.

This module closes that loop the same way the rest of the platform applies human
answers: the decision is persisted (with provenance), re-applied on every
onboard so it survives regeneration, and mirrored into the live contract for
immediate effect. It never fabricates a definition and never hand-edits a
generated contract outside this deterministic apply path.

Decision store: ``<ws>/interns/generated/decisions/kpi_definitions.json``, keyed
by normalized business question (stable across re-onboards, which re-derive the
``kpi_001`` ids).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout

STORE_VERSION = 1
_STORE_RELATIVE = ("decisions", "kpi_definitions.json")

# Columns accepted by the bulk file (--from-file). Anything else is an error.
BULK_COLUMNS: tuple[str, ...] = (
    "kpi_id",
    "business_question",
    "metric",
    "cuts",
    "confirmed_by",
    "note",
)


def kpi_definition_key(business_question: str) -> str:
    """Normalized identity for a KPI business question (matches the dedupe key)."""
    return re.sub(
        r"\s+", " ", re.sub(r"[^a-z0-9\s]+", " ", str(business_question or "").lower())
    ).strip()


def _store_path(layout: WorkspaceLayout) -> Path:
    return layout.generated_dir.joinpath(*_STORE_RELATIVE)


def load_kpi_definition_store(layout: WorkspaceLayout) -> dict[str, Any]:
    path = _store_path(layout)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    defs = data.get("definitions")
    return defs if isinstance(defs, dict) else {}


def apply_accepted_definitions_to_kpis(kpis: list[Any], store: dict[str, Any]) -> list[Any]:
    """Override empty metric/cuts on KpiDefinition rows from accepted decisions.

    A human-confirmed definition is authoritative: it fills the cell even over a
    derived guess. Operates on the onboarding ``KpiDefinition`` dataclass; unknown
    KPIs pass through untouched. No-op when the store is empty.
    """
    if not store:
        return list(kpis)
    from dataclasses import replace

    out = []
    for kpi in kpis:
        decision = store.get(kpi_definition_key(getattr(kpi, "name", "")))
        if not isinstance(decision, dict):
            out.append(kpi)
            continue
        metric = str(decision.get("metric") or "").strip()
        cuts = str(decision.get("cuts") or "").strip()
        changes: dict[str, Any] = {}
        if metric:
            changes["metric"] = metric
            if hasattr(kpi, "metric_provenance"):
                changes["metric_provenance"] = "user_confirmed"
        if cuts:
            changes["cuts"] = cuts
            if hasattr(kpi, "cuts_provenance"):
                changes["cuts_provenance"] = "user_confirmed"
        out.append(replace(kpi, **changes) if changes else kpi)
    return out


@dataclass(frozen=True)
class ApplyKpiDefinitionResult:
    workspace: str
    kpi_id: str
    business_question: str
    metric: str
    cuts: str
    store_path: str
    contract_patched: bool
    source: str = "human"

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "kpi_id": self.kpi_id,
            "business_question": self.business_question,
            "metric": self.metric,
            "cuts": self.cuts,
            "source": self.source,
            "store_path": self.store_path,
            "contract_patched": self.contract_patched,
            "next_step": (
                "Re-run the KPI pipeline (or resolve-kpi-features) to generate SQL "
                "and results for this KPI."
            ),
        }


def _resolve_business_question(
    registry: dict[str, Any], kpi_id: str
) -> tuple[str, int] | None:
    for idx, kpi in enumerate(registry.get("kpis") or [], start=1):
        if not isinstance(kpi, dict):
            continue
        if (kpi.get("kpi_id") or f"kpi_{idx:03d}") == kpi_id:
            return str(kpi.get("name") or kpi.get("business_question") or ""), idx - 1
    return None


def apply_kpi_definition(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    kpi_id: str = "",
    business_question: str = "",
    metric: str = "",
    cuts: str = "",
    confirmed_by: str = "",
    note: str = "",
    allow_agent_provenance: bool = False,
) -> ApplyKpiDefinitionResult:
    metric = str(metric or "").strip()
    cuts = str(cuts or "").strip()
    if not metric and not cuts:
        raise ValueError("provide at least one of --metric or --cuts")
    if not confirmed_by.strip() and not allow_agent_provenance:
        raise ValueError(
            "a human-confirmed KPI definition requires --confirmed-by "
            "(records provenance source: human)"
        )
    # Human-gate provenance rule (BUG-014): a named confirmer records source:
    # human; an empty confirmed_by is recorded as agent-asserted, never as human.
    source = "human" if confirmed_by.strip() else "agent"

    root = Path(repo_root).resolve()
    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    registry_path = layout.kpi_registry_path
    registry = _read_json(registry_path)

    resolved_index: int | None = None
    if kpi_id:
        found = _resolve_business_question(registry, kpi_id)
        if found is None:
            raise ValueError(f"kpi_id not found in registry: {kpi_id}")
        business_question, resolved_index = found
    if not business_question.strip():
        raise ValueError("provide --kpi-id or --business-question")

    key = kpi_definition_key(business_question)

    # 1) Persist the decision (durable; re-applied on every onboard).
    store_path = _store_path(layout)
    store_doc = _read_json(store_path)
    definitions = store_doc.get("definitions")
    if not isinstance(definitions, dict):
        definitions = {}
    definitions[key] = {
        "business_question": business_question,
        "metric": metric,
        "cuts": cuts,
        "source": source,
        "confirmed_by": confirmed_by.strip(),
        "note": note.strip(),
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "artifact_type": "kpi_definitions.json",
                "version": STORE_VERSION,
                "generated_by": "apply-kpi-definition",
                "workspace": _rel(workspace_path, root),
                "definitions": definitions,
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )

    # 2) Mirror into the live contract so the next pipeline run sees it without a
    #    full re-onboard. Match by kpi_id (if given) else by business question.
    contract_patched = False
    kpis = registry.get("kpis")
    if isinstance(kpis, list):
        for idx, kpi in enumerate(kpis):
            if not isinstance(kpi, dict):
                continue
            matches_id = resolved_index is not None and idx == resolved_index
            matches_q = kpi_definition_key(
                str(kpi.get("name") or kpi.get("business_question") or "")
            ) == key
            if matches_id or matches_q:
                if metric:
                    kpi["metric"] = metric
                    kpi["metric_provenance"] = "user_confirmed"
                if cuts:
                    kpi["cuts"] = cuts
                    kpi["cuts_provenance"] = "user_confirmed"
                kpi["definition_source"] = (
                    "human_confirmed" if source == "human" else "agent_asserted"
                )
                kpi["definition_confirmed_by"] = confirmed_by.strip()
                contract_patched = True
        if contract_patched:
            registry_path.write_text(
                json.dumps(registry, indent=2, default=str) + "\n", encoding="utf-8"
            )

    return ApplyKpiDefinitionResult(
        workspace=_rel(workspace_path, root),
        kpi_id=kpi_id or "",
        business_question=business_question,
        metric=metric,
        cuts=cuts,
        store_path=_rel(store_path, root),
        contract_patched=contract_patched,
        source=source,
    )


def _parse_bulk_rows(path: Path) -> tuple[list[tuple[int, dict[str, str]]], list[dict[str, Any]]]:
    """Parse a bulk definitions file into (row_number, row) pairs plus row errors.

    File-level problems (missing file, unsupported extension, malformed CSV
    header / JSON document, unknown CSV columns) raise ``ValueError``. Row-level
    shape problems (non-object JSON rows, unknown JSON keys, extra CSV cells)
    are returned as per-row errors so the caller can report them all at once.
    """
    if not path.exists():
        raise ValueError(f"bulk file not found: {path}")
    suffix = path.suffix.lower()
    rows: list[tuple[int, dict[str, str]]] = []
    errors: list[dict[str, Any]] = []

    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, restkey="__extra__")
            if not reader.fieldnames:
                raise ValueError("empty CSV bulk file: a header row is required")
            fieldnames = [str(name or "").strip() for name in reader.fieldnames]
            unknown = [name for name in fieldnames if name not in BULK_COLUMNS]
            if unknown:
                raise ValueError(
                    "unknown columns in bulk file: "
                    + ", ".join(sorted(unknown))
                    + f" (allowed: {', '.join(BULK_COLUMNS)})"
                )
            for number, raw in enumerate(reader, start=1):
                if raw.get("__extra__"):
                    errors.append(
                        {"row": number, "error": "more cells than header columns"}
                    )
                    continue
                row = {
                    str(key or "").strip(): str(value or "").strip()
                    for key, value in raw.items()
                    if key is not None and key != "__extra__"
                }
                if not any(row.values()):
                    continue  # skip fully blank lines
                rows.append((number, row))
        return rows, errors

    if suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"could not parse JSON bulk file: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError("JSON bulk file must be a list of definition objects")
        for number, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                errors.append({"row": number, "error": "row must be a JSON object"})
                continue
            unknown = [str(key) for key in item if str(key) not in BULK_COLUMNS]
            if unknown:
                errors.append(
                    {
                        "row": number,
                        "error": "unknown columns: "
                        + ", ".join(sorted(unknown))
                        + f" (allowed: {', '.join(BULK_COLUMNS)})",
                    }
                )
                continue
            rows.append(
                (number, {key: str(item.get(key) or "").strip() for key in BULK_COLUMNS})
            )
        return rows, errors

    raise ValueError(
        f"unsupported bulk file type: {path.name} (use .csv or .json)"
    )


def apply_kpi_definitions_from_file(
    repo_root: str | Path,
    workspace: str | Path,
    from_file: str | Path,
    *,
    default_confirmed_by: str = "",
    default_note: str = "",
) -> dict[str, Any]:
    """Bulk apply: validate ALL rows first, then apply each via the single path.

    All-or-nothing: if any row fails validation (parse shape, required fields,
    kpi_id resolution) nothing is applied and the report carries per-row errors.
    Each valid row is applied through :func:`apply_kpi_definition` -- the bulk
    mode never forks the apply logic. Rows without a ``confirmed_by`` (after the
    ``default_confirmed_by`` fallback) are recorded as agent-asserted per the
    human-gate provenance rule; named confirmers record ``source: human``.
    """
    root = Path(repo_root).resolve()
    path = Path(from_file)
    if not path.is_absolute():
        path = root / path
    rows, errors = _parse_bulk_rows(path)
    if not rows and not errors:
        raise ValueError(f"no data rows found in bulk file: {path}")

    workspace_path = (root / workspace).resolve()
    layout = WorkspaceLayout(project_root=workspace_path)
    registry = _read_json(layout.kpi_registry_path)

    # Pass 1: validate every row before applying anything (all-or-nothing).
    for number, row in rows:
        if not row.get("kpi_id") and not row.get("business_question"):
            errors.append(
                {"row": number, "error": "provide kpi_id or business_question"}
            )
            continue
        if not row.get("metric") and not row.get("cuts"):
            errors.append(
                {"row": number, "error": "provide at least one of metric or cuts"}
            )
            continue
        kpi_id = row.get("kpi_id", "")
        if kpi_id and _resolve_business_question(registry, kpi_id) is None:
            errors.append(
                {"row": number, "error": f"kpi_id not found in registry: {kpi_id}"}
            )

    report: dict[str, Any] = {
        "workspace": _rel(workspace_path, root),
        "from_file": str(path),
        "row_count": len(rows),
        "errors": sorted(errors, key=lambda item: item["row"]),
        "applied": [],
        "store_path": _rel(_store_path(layout), root),
    }
    if errors:
        return report

    # Pass 2: apply each row through the existing single-definition path.
    for number, row in rows:
        confirmed_by = row.get("confirmed_by") or default_confirmed_by.strip()
        result = apply_kpi_definition(
            root,
            workspace,
            kpi_id=row.get("kpi_id", ""),
            business_question=row.get("business_question", ""),
            metric=row.get("metric", ""),
            cuts=row.get("cuts", ""),
            confirmed_by=confirmed_by,
            note=row.get("note") or default_note,
            allow_agent_provenance=True,
        )
        report["applied"].append(
            {
                "row": number,
                "kpi_id": result.kpi_id or "-",
                "business_question": result.business_question,
                "source": result.source,
                "status": "patched" if result.contract_patched else "recorded",
            }
        )
    return report


def _print_bulk_report(report: dict[str, Any]) -> None:
    errors = report.get("errors") or []
    if errors:
        print(
            f"[x] bulk apply aborted: {len(errors)} invalid row(s); nothing applied"
        )
        for item in errors:
            print(f"  [x] row {item['row']}: {item['error']}")
        return
    applied = report.get("applied") or []
    print(
        f"[ok] applied {len(applied)} kpi definition(s) from {report.get('from_file', '')}"
    )
    header = ("kpi_id", "question", "source", "status")
    table_rows = [
        (
            str(item["kpi_id"]),
            _truncate(str(item["business_question"]), 48),
            str(item["source"]),
            str(item["status"]),
        )
        for item in applied
    ]
    widths = [
        max(len(header[col]), *(len(row[col]) for row in table_rows))
        for col in range(len(header))
    ]
    print(" | ".join(header[col].ljust(widths[col]) for col in range(len(header))))
    print("-+-".join("-" * widths[col] for col in range(len(header))))
    for row in table_rows:
        print(" | ".join(row[col].ljust(widths[col]) for col in range(len(header))))
    print(f"store: {report.get('store_path', '')}")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply a human-confirmed KPI definition (metric/grain) into the registry."
    )
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--kpi-id", default="", help="KPI id (e.g. kpi_004). Resolves the business question.")
    parser.add_argument("--business-question", default="", help="Business question (alternative to --kpi-id).")
    parser.add_argument("--metric", default="", help="Metric expression, e.g. 'count(*)' or 'avg(BASE_COST)'.")
    parser.add_argument("--cuts", default="", help="Grain/filters, comma-separated, e.g. 'DESCRIPTION' or 'PAYER_COVERAGE = 0'.")
    parser.add_argument("--confirmed-by", default="", help="Human reviewer name (records provenance source: human). With --from-file, acts as the default for rows without their own confirmed_by.")
    parser.add_argument("--note", default="", help="Optional rationale recorded with the decision. With --from-file, acts as the default for rows without their own note.")
    parser.add_argument("--from-file", default="", help="Bulk mode: .csv or .json file of definition rows (columns: kpi_id, business_question, metric, cuts, confirmed_by, note). All-or-nothing.")
    args = parser.parse_args(argv)
    if args.from_file:
        mixed = [
            flag
            for flag, value in (
                ("--kpi-id", args.kpi_id),
                ("--business-question", args.business_question),
                ("--metric", args.metric),
                ("--cuts", args.cuts),
            )
            if str(value or "").strip()
        ]
        if mixed:
            print(
                "[x] --from-file is mutually exclusive with: " + ", ".join(mixed)
            )
            return 2
        try:
            report = apply_kpi_definitions_from_file(
                args.repo_root,
                args.workspace,
                args.from_file,
                default_confirmed_by=args.confirmed_by,
                default_note=args.note,
            )
        except ValueError as exc:
            print(f"[x] {exc}")
            return 1
        _print_bulk_report(report)
        return 1 if report["errors"] else 0
    result = apply_kpi_definition(
        args.repo_root,
        args.workspace,
        kpi_id=args.kpi_id,
        business_question=args.business_question,
        metric=args.metric,
        cuts=args.cuts,
        confirmed_by=args.confirmed_by,
        note=args.note,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
