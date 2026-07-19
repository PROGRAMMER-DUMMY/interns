"""Generate stakeholder-facing presentation exports for a workspace."""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import json
import textwrap
import xml.sax.saxutils as xml_escape
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


PRESENTATION_VERSION = 1


@dataclass(frozen=True)
class PresentationExportResult:
    workspace: str
    output_dir: str
    generated_paths: list[str]
    manifest_path: str
    source_state: str
    warnings: list[str]

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class WorkspacePresentationExporter:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.output_dir = self.layout.reports_dir / "presentation"

    def export_data_model_diagram(self) -> PresentationExportResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        source = self._load_data_model_source()
        warnings = list(source.get("warnings", []))
        contract = source.get("payload", {})
        svg_path = self.output_dir / "data-model.svg"
        mermaid_path = self.output_dir / "data-model.mermaid.md"
        svg_path.write_text(_render_model_svg(contract), encoding="utf-8")
        mermaid_path.write_text(_render_mermaid_markdown(contract), encoding="utf-8")
        return self._write_manifest(
            export_type="data_model_diagram",
            generated_paths=[svg_path, mermaid_path],
            source_state=str(source.get("state", "missing")),
            sources=[str(source.get("path", ""))] if source.get("path") else [],
            warnings=warnings,
        )

    def export_kpi_registry_excel(self) -> PresentationExportResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        source = self._load_kpi_source()
        warnings = list(source.get("warnings", []))
        workbook_path = self.output_dir / "kpi_registry.xlsx"
        _write_kpi_workbook(workbook_path, source.get("payload", {}), source)
        return self._write_manifest(
            export_type="kpi_registry_excel",
            generated_paths=[workbook_path],
            source_state=str(source.get("state", "missing")),
            sources=[str(source.get("path", ""))] if source.get("path") else [],
            warnings=warnings,
        )

    def export_kpi_proof_packet(self) -> PresentationExportResult:
        # Imported lazily so the data-model / Excel exports do not depend on the
        # proof-packet builder (and its validator) being importable.
        from core.onboarding.kpi.proof_packet import KPIProofPacketBuilder

        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        md_path = self.output_dir / "kpi-proof-packet.md"
        json_path = self.output_dir / "kpi-proof-packet.json"
        try:
            packet = KPIProofPacketBuilder(self.repo_root, self.workspace).run()
        except Exception as exc:  # pragma: no cover - defensive
            return self._write_manifest(
                export_type="kpi_proof_packet",
                generated_paths=[],
                source_state="missing",
                sources=[],
                warnings=[f"KPI proof packet could not be generated: {exc}"],
            )
        md_path.write_text(
            (self.repo_root / packet.report_path).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        json_path.write_text(
            (self.repo_root / packet.report_json_path).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return self._write_manifest(
            export_type="kpi_proof_packet",
            generated_paths=[md_path, json_path],
            source_state="generated",
            sources=[packet.report_path, packet.report_json_path],
            warnings=[],
        )

    def export_workspace_presentation(self) -> PresentationExportResult:
        data_model = self.export_data_model_diagram()
        kpi = self.export_kpi_registry_excel()
        proof = self.export_kpi_proof_packet()
        generated = [
            *(self.repo_root / path for path in data_model.generated_paths),
            *(self.repo_root / path for path in kpi.generated_paths),
            *(self.repo_root / path for path in proof.generated_paths),
        ]
        warnings = [*data_model.warnings, *kpi.warnings, *proof.warnings]
        return self._write_manifest(
            export_type="workspace_presentation",
            generated_paths=generated,
            source_state=(
                f"data_model={data_model.source_state};"
                f"kpi={kpi.source_state};"
                f"kpi_proof_packet={proof.source_state}"
            ),
            sources=[],
            warnings=warnings,
        )

    def _load_data_model_source(self) -> dict[str, Any]:
        candidates = [
            ("finalized", self.layout.contracts_dir / "data_model_contract.json"),
            ("draft", self.layout.requirements_dir / "data_model_draft.json"),
            ("onboarded_domain_model", self.layout.contracts_dir / "domain_model.json"),
        ]
        for state, path in candidates:
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if state == "onboarded_domain_model":
                payload = _domain_model_to_contract(payload)
            return {"state": state, "path": _rel(path, self.repo_root), "payload": payload, "warnings": []}
        return {
            "state": "missing",
            "path": "",
            "payload": {"workspace": _rel(self.workspace, self.repo_root), "tables": [], "relationships": []},
            "warnings": ["No data model contract, draft, or domain_model.json was found."],
        }

    def _load_kpi_source(self) -> dict[str, Any]:
        candidates = [
            ("finalized", self.workspace / "docs" / "kpi_registry.generated.json"),
            ("draft", self.layout.requirements_dir / "kpi_registry_draft.json"),
            ("onboarded", self.layout.contracts_dir / "kpi_registry.json"),
        ]
        for state, path in candidates:
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                return {"state": state, "path": _rel(path, self.repo_root), "payload": payload, "warnings": []}
        return {
            "state": "missing",
            "path": "",
            "payload": {"workspace": _rel(self.workspace, self.repo_root), "kpis": []},
            "warnings": ["No finalized, draft, or onboarded KPI registry was found."],
        }

    def _write_manifest(
        self,
        *,
        export_type: str,
        generated_paths: list[Path],
        source_state: str,
        sources: list[str],
        warnings: list[str],
    ) -> PresentationExportResult:
        manifest_path = self.output_dir / "presentation_manifest.json"
        payload = {
            "artifact_type": "presentation_manifest.json",
            "version": PRESENTATION_VERSION,
            "generated_by": export_type,
            "workspace": _rel(self.workspace, self.repo_root),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_state": source_state,
            "source_paths": sources,
            "generated_paths": [_rel(path, self.repo_root) for path in generated_paths],
            "warnings": warnings,
            "next_steps": [
                "Review generated presentation artifacts under interns/reports/presentation/.",
                "Promote user-facing copies to docs/ only after explicit approval.",
            ],
        }
        manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return PresentationExportResult(
            workspace=_rel(self.workspace, self.repo_root),
            output_dir=_rel(self.output_dir, self.repo_root),
            generated_paths=payload["generated_paths"],
            manifest_path=_rel(manifest_path, self.repo_root),
            source_state=source_state,
            warnings=warnings,
        )

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _render_model_svg(contract: dict[str, Any]) -> str:
    tables = contract.get("tables", [])
    relationships = contract.get("relationships", [])
    box_width = 320
    row_height = 20
    gap_x = 90
    gap_y = 50
    positions: dict[str, tuple[int, int, int]] = {}
    for idx, table in enumerate(tables):
        col = idx % 2
        row = idx // 2
        columns = table.get("columns", [])[:10]
        height = 92 + len(columns) * row_height
        x = 40 + col * (box_width + gap_x)
        y = 40 + row * (height + gap_y)
        positions[str(table.get("name", ""))] = (x, y, height)
    rows = max(1, (len(tables) + 1) // 2)
    width = 40 + min(2, max(1, len(tables))) * box_width + max(0, min(2, len(tables)) - 1) * gap_x + 40
    height = max(320, 60 + rows * 310)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        ".bg{fill:#f8fafc}.table{fill:#ffffff;stroke:#334155;stroke-width:1.5}.head{fill:#0f766e}.title{fill:#fff;font:600 14px Arial}.meta{fill:#475569;font:11px Arial}.col{fill:#0f172a;font:12px Arial}.pk{fill:#b45309;font:700 12px Arial}.rel{stroke:#2563eb;stroke-width:1.4;fill:none;marker-end:url(#arrow)}",
        "</style>",
        '<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="9" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#2563eb"/></marker></defs>',
        f'<rect class="bg" x="0" y="0" width="{width}" height="{height}"/>',
        f'<text x="40" y="24" class="meta">Workspace: {_xml(contract.get("workspace", ""))}</text>',
    ]
    for relationship in relationships:
        left = str(relationship.get("from_table") or "")
        right = str(relationship.get("to_table") or "")
        if left not in positions or right not in positions:
            continue
        lx, ly, lh = positions[left]
        rx, ry, rh = positions[right]
        start_x = lx + box_width
        start_y = ly + min(lh - 25, 76)
        end_x = rx
        end_y = ry + min(rh - 25, 76)
        if rx < lx:
            start_x = lx
            end_x = rx + box_width
        mid_x = (start_x + end_x) // 2
        parts.append(f'<path class="rel" d="M{start_x},{start_y} C{mid_x},{start_y} {mid_x},{end_y} {end_x},{end_y}"/>')
        label = f"{relationship.get('from_column', '')} -> {relationship.get('to_column', '')}"
        parts.append(f'<text x="{min(start_x, end_x) + 12}" y="{min(start_y, end_y) - 6}" class="meta">{_xml(label)}</text>')
    for table in tables:
        name = str(table.get("name") or "table")
        x, y, table_height = positions[name]
        columns = table.get("columns", [])[:10]
        pk = set(table.get("primary_key") or [])
        parts.extend(
            [
                f'<rect class="table" x="{x}" y="{y}" width="{box_width}" height="{table_height}" rx="6"/>',
                f'<rect class="head" x="{x}" y="{y}" width="{box_width}" height="34" rx="6"/>',
                f'<text x="{x + 12}" y="{y + 22}" class="title">{_xml(name)}</text>',
                f'<text x="{x + 12}" y="{y + 52}" class="meta">pattern: {_xml(table.get("table_pattern", ""))}</text>',
                f'<text x="{x + 12}" y="{y + 68}" class="meta">grain: {_xml(_short(table.get("grain", {}).get("description", ""), 42))}</text>',
            ]
        )
        cursor_y = y + 92
        for column in columns:
            cname = str(column.get("name") or "")
            css = "pk" if cname in pk else "col"
            prefix = "PK " if cname in pk else ""
            text = f"{prefix}{cname}: {column.get('type', '')}"
            parts.append(f'<text x="{x + 16}" y="{cursor_y}" class="{css}">{_xml(_short(text, 42))}</text>')
            cursor_y += row_height
        if len(table.get("columns", [])) > len(columns):
            parts.append(f'<text x="{x + 16}" y="{cursor_y}" class="meta">+ {len(table.get("columns", [])) - len(columns)} more columns</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _render_mermaid_markdown(contract: dict[str, Any]) -> str:
    lines = ["# Data Model ERD", "", "```mermaid", "erDiagram"]
    for table in contract.get("tables", []):
        lines.append(f"  {table.get('name', '')} {{")
        for column in table.get("columns", []):
            lines.append(f"    {column.get('type', 'TEXT')} {column.get('name', '')}")
        lines.append("  }")
    for rel in contract.get("relationships", []):
        lines.append(
            f"  {rel.get('from_table')} }}o--|| {rel.get('to_table')} : "
            f"\"{rel.get('from_column')} to {rel.get('to_column')}\""
        )
    lines.extend(["```", ""])
    return "\n".join(lines)


def _write_kpi_workbook(path: Path, payload: dict[str, Any], source: dict[str, Any]) -> None:
    try:
        import xlsxwriter
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("xlsxwriter is required for KPI Excel export") from exc
    workbook = xlsxwriter.Workbook(str(path))
    header = workbook.add_format({"bold": True, "bg_color": "#0F766E", "font_color": "white", "border": 1})
    cell = workbook.add_format({"text_wrap": True, "valign": "top", "border": 1})
    note = workbook.add_format({"text_wrap": True, "valign": "top", "border": 1, "bg_color": "#F8FAFC"})
    _write_sheet(
        workbook,
        "KPI Registry",
        _kpi_rows(payload, source),
        header,
        cell,
        widths=[16, 34, 38, 26, 28, 18, 26, 18],
    )
    _write_sheet(
        workbook,
        "Readiness Proof",
        _proof_rows(payload),
        header,
        cell,
        widths=[18, 24, 56, 24],
    )
    _write_sheet(
        workbook,
        "Blockers",
        _blocker_rows(payload),
        header,
        cell,
        widths=[18, 26, 60],
    )
    _write_sheet(
        workbook,
        "Evidence Links",
        _evidence_rows(payload, source),
        header,
        cell,
        widths=[20, 24, 70],
    )
    _write_sheet(
        workbook,
        "Decisions",
        _decision_rows(payload),
        header,
        note,
        widths=[24, 24, 36, 54],
    )
    workbook.close()


def _write_sheet(workbook: Any, name: str, rows: list[dict[str, Any]], header_fmt: Any, cell_fmt: Any, widths: list[int]) -> None:
    sheet = workbook.add_worksheet(name[:31])
    if not rows:
        rows = [{"status": "empty"}]
    headers = list(rows[0].keys())
    for col, key in enumerate(headers):
        sheet.write(0, col, key, header_fmt)
        if col < len(widths):
            sheet.set_column(col, col, widths[col])
    for row_idx, row in enumerate(rows, start=1):
        for col, key in enumerate(headers):
            value = row.get(key, "")
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=True)
            sheet.write(row_idx, col, value, cell_fmt)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, len(rows), max(0, len(headers) - 1))


def _kpi_rows(payload: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for idx, kpi in enumerate(payload.get("kpis", []), start=1):
        rows.append(
            {
                "kpi_id": kpi.get("kpi_id") or f"kpi_{idx:03d}",
                "business_question": kpi.get("business_question") or kpi.get("name") or "",
                "description": kpi.get("description", ""),
                "metric": kpi.get("metric") or kpi.get("metric_definition") or "",
                "grain_or_cuts": kpi.get("cuts") or kpi.get("grain") or kpi.get("dimensions") or "",
                "status": kpi.get("status") or kpi.get("refinement_required") or "",
                "source_state": source.get("state", ""),
                "source_path": source.get("path", ""),
            }
        )
    return rows


def _proof_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for proof in payload.get("proofs", []) or payload.get("draft_proofs", []):
        rows.append(
            {
                "kpi_id": proof.get("kpi_id", ""),
                "proof_level": proof.get("proof_level", ""),
                "remaining_risk": proof.get("remaining_risk", []),
                "source_to_target_required": proof.get("source_to_target_required", ""),
            }
        )
    review = payload.get("competitive_review") or {}
    for idx, suggestion in enumerate(review.get("ranked_suggestions", []), start=1):
        rows.append(
            {
                "kpi_id": f"review_{idx}",
                "proof_level": review.get("mode", "competitive_review"),
                "remaining_risk": suggestion,
                "source_to_target_required": "",
            }
        )
    return rows


def _blocker_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for kpi in payload.get("kpis", []):
        refinement = str(kpi.get("refinement_required") or "")
        status = str(kpi.get("status") or "")
        if refinement or "review" in status or "block" in status:
            rows.append(
                {
                    "kpi_id": kpi.get("kpi_id", ""),
                    "blocker_type": status or "refinement_required",
                    "blocker": refinement or status,
                }
            )
    review = payload.get("competitive_review") or {}
    for item in review.get("missing_discussion_points", []):
        rows.append({"kpi_id": "workspace", "blocker_type": "missing_discussion_point", "blocker": item})
    return rows


def _evidence_rows(payload: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [{"artifact": "source_registry", "state": source.get("state", ""), "path": source.get("path", "")}]
    for proof in payload.get("proofs", []) or []:
        for candidate in proof.get("mapped_dataset_candidates", []) or []:
            rows.append({"artifact": proof.get("kpi_id", ""), "state": "candidate_dataset", "path": candidate})
    return rows


def _decision_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = payload.get("decisions") or []
    return [
        {
            "stage": decision.get("stage", ""),
            "accepted_option_id": decision.get("accepted_option_id", ""),
            "accepted_label": decision.get("accepted_label", ""),
            "note": decision.get("custom_note", ""),
        }
        for decision in decisions
    ]


def _domain_model_to_contract(domain_model: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace": domain_model.get("workspace", ""),
        "tables": [
            {
                "name": Path(str(dataset.get("path", ""))).stem,
                "source_dataset": dataset.get("path", ""),
                "description": f"Profiled dataset `{dataset.get('path', '')}`.",
                "table_pattern": "profiled_dataset",
                "primary_key": [],
                "grain": {"description": "needs review"},
                "columns": [
                    {"name": name, "type": str(dtype), "role": "column"}
                    for name, dtype in (dataset.get("schema") or {}).items()
                ],
            }
            for dataset in domain_model.get("datasets", [])
        ],
        "relationships": [],
    }


def _short(value: Any, width: int) -> str:
    text = str(value or "")
    return textwrap.shorten(text, width=width, placeholder="...")


def _xml(value: Any) -> str:
    return xml_escape.escape(str(value or ""))


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()



@anchored("export-data-model-diagram")
def export_data_model_diagram_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a workspace data model diagram as SVG.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = WorkspacePresentationExporter(args.repo_root, args.workspace).export_data_model_diagram()
    print(json.dumps(result.summary(), indent=2))
    return 0


@anchored("export-kpi-registry-excel")
def export_kpi_registry_excel_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a workspace KPI registry as XLSX.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = WorkspacePresentationExporter(args.repo_root, args.workspace).export_kpi_registry_excel()
    print(json.dumps(result.summary(), indent=2))
    return 0


@anchored("export-workspace-presentation")
def export_workspace_presentation_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export stakeholder presentation artifacts for a workspace.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = WorkspacePresentationExporter(args.repo_root, args.workspace).export_workspace_presentation()
    print(json.dumps(result.summary(), indent=2))
    return 0

