"""Local-safe data-model image parsing scaffolds.

This module creates review-gated sidecars for schema/ERD/medallion images.
It does not trust image-derived relationships for executable generation. OCR
and multimodal providers can populate the same contract later, but the default
path is local-safe and non-executable.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import struct
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from core.onboarding.data_model.generation_workflow import IMAGE_MODEL_SUFFIXES, MODEL_NAME_TOKENS
from core.storage.workspace_layout import WorkspaceLayout


@dataclass(frozen=True)
class DataModelImageParseResult:
    workspace: str
    image_count: int
    generated_sidecars: list[str]
    report_paths: list[str]
    current_json_path: str
    current_markdown_path: str
    status: str
    next_step: str

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class DataModelImageParser:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def parse(
        self,
        *,
        allow_remote_vision: bool = False,
        confirm_sensitive_upload: bool = False,
        local_ocr: str = "auto",
        auto_install_ocr: bool = False,
    ) -> DataModelImageParseResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        images = self._image_files()
        sidecar_dir = self.layout.generated_dir / "data_model_images"
        report_dir = self.layout.reports_dir / "data_model_images"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)

        sidecars: list[Path] = []
        reports: list[Path] = []
        records: list[dict[str, Any]] = []
        for image in images:
            record = self._build_sidecar(
                image,
                allow_remote_vision=allow_remote_vision,
                confirm_sensitive_upload=confirm_sensitive_upload,
                local_ocr=local_ocr,
                auto_install_ocr=auto_install_ocr,
            )
            base = _safe_stem(image)
            sidecar_path = sidecar_dir / f"{base}.model.json"
            report_path = report_dir / f"{base}.model.md"
            sidecar_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            report_path.write_text(_render_sidecar_markdown(record), encoding="utf-8")
            sidecars.append(sidecar_path)
            reports.append(report_path)
            records.append(record)

        index = self._build_index(records, sidecars, reports)
        current_json = report_dir / "current.json"
        current_md = report_dir / "current.md"
        current_json.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        current_md.write_text(_render_index_markdown(index), encoding="utf-8")

        return DataModelImageParseResult(
            workspace=_rel(self.workspace, self.repo_root),
            image_count=len(images),
            generated_sidecars=[_rel(path, self.repo_root) for path in sidecars],
            report_paths=[_rel(path, self.repo_root) for path in reports],
            current_json_path=_rel(current_json, self.repo_root),
            current_markdown_path=_rel(current_md, self.repo_root),
            status="needs_user_review" if images else "no_images_found",
            next_step=(
                f"Review {_rel(current_md, self.repo_root)}; image-derived relationships remain "
                "non-executable until profile/catalog proof or explicit approval."
                if images
                else "No data-model image files were found under the workspace docs tree."
            ),
        )

    def _build_sidecar(
        self,
        image: Path,
        *,
        allow_remote_vision: bool,
        confirm_sensitive_upload: bool,
        local_ocr: str,
        auto_install_ocr: bool,
    ) -> dict[str, Any]:
        metadata = _image_metadata(image)
        ocr = _run_local_ocr(
            image,
            local_ocr=local_ocr,
            repo_root=self.repo_root,
            auto_install_ocr=auto_install_ocr,
        )
        parsed_schema = _parse_ocr_schema_text(str(ocr.get("text") or ""))
        profile_matching = _match_schema_to_profiles(
            parsed_schema,
            layout=self.layout,
            repo_root=self.repo_root,
        )
        parsed_schema["relationships"] = _apply_relationship_profile_matches(
            parsed_schema.get("relationships") or [],
            profile_matching,
        )
        remote_status = _remote_status(
            allow_remote_vision=allow_remote_vision,
            confirm_sensitive_upload=confirm_sensitive_upload,
        )
        return {
            "version": 1,
            "artifact_type": "data_model_image_sidecar",
            "generated_by": "parse-data-model-images",
            "generated_at": _now(),
            "workspace": _rel(self.workspace, self.repo_root),
            "source_image": _rel(image, self.repo_root),
            "image_metadata": metadata,
            "parsers": {
                "ocr_layout": ocr,
                "multimodal_vision": remote_status,
            },
            **parsed_schema,
            "layer_intent": {
                "state": "unknown",
                "allowed_values": ["source", "bronze", "silver", "gold_target", "unknown"],
            },
            "profile_matching": profile_matching,
            "confidence_by_element": {},
            "warnings": _warnings(ocr, remote_status),
            "approval_state": "needs_parser_or_manual_sidecar",
            "executable_usage_allowed": False,
            "promotion_policy": {
                "relationship_candidate": (
                    "Only relationships with matched source/target tables and columns from profiles "
                    "or catalog/schema evidence may become review-ready candidates."
                ),
                "executable_contract": (
                    "User approval records a user_confirmed decision, then normal relationship-contract "
                    "validation must pass before executable SQL/Polars/PySpark generation."
                ),
                "fuzzy_matches": (
                    "Approximate table/column matches are suggestions only until profile proof or "
                    "user confirmation exists."
                ),
            },
            "review_actions": [
                "approve_relationship_after_profile_match",
                "repair_table_or_column",
                "reject_stale_diagram",
                "provide_manual_sidecar",
            ],
        }

    def _build_index(
        self,
        records: list[dict[str, Any]],
        sidecars: list[Path],
        reports: list[Path],
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "artifact_type": "data_model_image_review_panel",
            "generated_by": "parse-data-model-images",
            "generated_at": _now(),
            "workspace": _rel(self.workspace, self.repo_root),
            "status": "needs_user_review" if records else "no_images_found",
            "image_count": len(records),
            "summary": {
                "remote_vision_called": False,
                "executable_usage_allowed": False,
                "relationship_candidates": 0,
                "needs_manual_review": len(records),
            },
            "image_sidecars": [
                {
                    "source_image": record["source_image"],
                    "sidecar_path": _rel(sidecar, self.repo_root),
                    "report_path": _rel(report, self.repo_root),
                    "approval_state": record["approval_state"],
                    "warnings": record["warnings"],
                }
                for record, sidecar, report in zip(records, sidecars, reports)
            ],
            "question": "Review detected data-model images before using any diagram evidence.",
            "recommended_answer": (
                "Keep image-derived elements review-gated until OCR/multimodal extraction, "
                "profile matching, and relationship-contract validation are available."
            ),
            "next_step": "Use non-image evidence for executable generation until a sidecar is reviewed and approved.",
        }

    def _image_files(self) -> list[Path]:
        docs_dir = self.workspace / "docs"
        if not docs_dir.exists():
            return []
        images: list[Path] = []
        for path in sorted(p for p in docs_dir.rglob("*") if p.is_file()):
            if path.suffix.lower() not in IMAGE_MODEL_SUFFIXES:
                continue
            name = path.name.lower()
            if any(token in name for token in MODEL_NAME_TOKENS):
                images.append(path)
        return images

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"Workspace not found: {_rel(self.workspace, self.repo_root)}")
        try:
            self.workspace.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError("Workspace must be inside the repository root") from exc


def _run_local_ocr(
    image: Path,
    *,
    local_ocr: str,
    repo_root: Path,
    auto_install_ocr: bool = False,
) -> dict[str, Any]:
    mode = local_ocr.lower()
    if mode not in {"auto", "off"}:
        raise ValueError("--local-ocr must be auto or off")
    if mode == "off":
        return {
            "state": "disabled",
            "text": "",
            "confidence": 0.0,
            "engine": "",
            "auto_install": {"requested": False, "attempted": False},
        }
    tesseract = _find_tesseract()
    if not tesseract:
        install_result = _attempt_install_tesseract() if auto_install_ocr else _install_not_requested()
        tesseract = _find_tesseract()
        if tesseract:
            ocr = _run_tesseract(image, tesseract=tesseract, repo_root=repo_root)
            ocr["auto_install"] = install_result
            return ocr
        return {
            "state": "provider_not_configured",
            "text": "",
            "confidence": 0.0,
            "engine": "tesseract",
            "message": "Install/configure local OCR to populate text extraction.",
            "auto_install": install_result,
        }
    ocr = _run_tesseract(image, tesseract=tesseract, repo_root=repo_root)
    ocr["auto_install"] = {"requested": auto_install_ocr, "attempted": False, "reason": "already_installed"}
    return ocr


def _run_tesseract(image: Path, *, tesseract: str, repo_root: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [tesseract, str(image), "stdout"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return {
            "state": "failed",
            "text": "",
            "confidence": 0.0,
            "engine": "tesseract",
            "message": str(exc),
        }
    text = (completed.stdout or "").strip()
    if completed.returncode != 0:
        return {
            "state": "failed",
            "text": "",
            "confidence": 0.0,
            "engine": "tesseract",
            "message": (completed.stderr or "tesseract failed").strip()[:500],
        }
    return {
        "state": "completed" if text else "completed_empty",
        "text": text,
        "confidence": 0.5 if text else 0.0,
        "engine": "tesseract",
        "source_image": _rel(image, repo_root),
    }


def _install_not_requested() -> dict[str, Any]:
    return {
        "requested": False,
        "attempted": False,
        "state": "not_requested",
        "message": "Pass --auto-install-ocr to attempt local Tesseract installation.",
    }


def _attempt_install_tesseract() -> dict[str, Any]:
    installer = _tesseract_install_command()
    if not installer:
        return {
            "requested": True,
            "attempted": False,
            "state": "no_supported_package_manager",
            "message": "No supported package manager found for automatic Tesseract installation.",
        }
    try:
        completed = subprocess.run(
            installer["command"],
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except Exception as exc:
        return {
            "requested": True,
            "attempted": True,
            "state": "failed",
            "manager": installer["manager"],
            "command": installer["display_command"],
            "message": str(exc),
        }
    return {
        "requested": True,
        "attempted": True,
        "state": "installed" if completed.returncode == 0 and _find_tesseract() else "failed",
        "manager": installer["manager"],
        "command": installer["display_command"],
        "returncode": completed.returncode,
        "message": _install_message(completed),
    }


def _find_tesseract() -> str | None:
    executable = shutil.which("tesseract")
    if executable:
        return executable
    if platform.system().lower() != "windows":
        return None
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _tesseract_install_command() -> dict[str, Any] | None:
    system = platform.system().lower()
    if system == "windows":
        winget = shutil.which("winget")
        if winget:
            command = [
                winget,
                "install",
                "--id",
                "UB-Mannheim.TesseractOCR",
                "-e",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ]
            return {
                "manager": "winget",
                "command": command,
                "display_command": "winget install --id UB-Mannheim.TesseractOCR -e",
            }
        choco = shutil.which("choco")
        if choco:
            return {
                "manager": "choco",
                "command": [choco, "install", "tesseract", "-y"],
                "display_command": "choco install tesseract -y",
            }
        return None
    if system == "darwin":
        brew = shutil.which("brew")
        if brew:
            return {
                "manager": "brew",
                "command": [brew, "install", "tesseract"],
                "display_command": "brew install tesseract",
            }
        return None
    if system == "linux":
        apt = shutil.which("apt-get")
        if apt:
            return {
                "manager": "apt-get",
                "command": [apt, "install", "-y", "tesseract-ocr"],
                "display_command": "apt-get install -y tesseract-ocr",
            }
        dnf = shutil.which("dnf")
        if dnf:
            return {
                "manager": "dnf",
                "command": [dnf, "install", "-y", "tesseract"],
                "display_command": "dnf install -y tesseract",
            }
        return None
    return None


def _install_message(completed: subprocess.CompletedProcess[str]) -> str:
    if completed.returncode == 0:
        return "Tesseract install command completed; PATH may need a new shell session if OCR is still unavailable."
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    return (stderr or stdout or "Tesseract install command failed.")[:500]


def _remote_status(*, allow_remote_vision: bool, confirm_sensitive_upload: bool) -> dict[str, Any]:
    if not allow_remote_vision:
        return {
            "state": "not_requested",
            "remote_call_made": False,
            "message": "Pass --allow-remote-vision plus sensitivity confirmation to enable later providers.",
        }
    if not confirm_sensitive_upload:
        return {
            "state": "blocked_missing_sensitive_upload_confirmation",
            "remote_call_made": False,
            "message": "Remote vision for customer/healthcare diagrams requires --confirm-sensitive-upload.",
        }
    return {
        "state": "provider_not_implemented",
        "remote_call_made": False,
        "message": "Remote multimodal provider interface is intentionally not implemented in this local-safe command.",
    }


def _warnings(ocr: dict[str, Any], remote: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if ocr.get("state") in {"provider_not_configured", "disabled", "failed", "completed_empty"}:
        warnings.append(f"local_ocr:{ocr.get('state')}")
    if remote.get("state") != "completed":
        warnings.append(f"multimodal_vision:{remote.get('state')}")
    warnings.append("image_derived_relationships_non_executable")
    return warnings


def _parse_ocr_schema_text(text: str) -> dict[str, Any]:
    lines = _clean_ocr_lines(text)
    if not lines:
        return {
            "detected_schema_types": [],
            "tables": [],
            "columns": [],
            "relationships": [],
            "profile_matching": {},
            "parse_notes": ["no_ocr_text_available"],
        }

    table_headers = _detect_table_headers(lines)
    if not table_headers:
        return {
            "detected_schema_types": [],
            "tables": [],
            "columns": [],
            "relationships": [],
            "profile_matching": {},
            "parse_notes": ["no_table_headers_detected"],
        }

    tables = [
        {
            "table_name": item["table_name"],
            "role": item["role"],
            "source": "ocr_layout",
            "state": "candidate_image_parsed",
            "confidence": item["confidence"],
        }
        for item in table_headers
    ]
    columns = _extract_columns(lines, table_headers)
    relationships = _infer_relationships(table_headers, columns)
    return {
        "detected_schema_types": _detected_schema_types(tables, relationships),
        "tables": tables,
        "columns": columns,
        "relationships": relationships,
        "parse_notes": [
            "deterministic_ocr_parser_v1",
            "relationships_are_candidates_only",
            "layout_columns_may_need_multimodal_repair",
        ],
    }


def _clean_ocr_lines(text: str) -> list[str]:
    replacements = {
        "\u2018": "",
        "\u2019": "",
        "\u201c": "",
        "\u201d": "",
        "\u00e2\u20ac\u02dc": "",
        "|": " ",
        "&": " ",
        "\t": " ",
    }
    cleaned = text
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)
    lines = []
    for raw in cleaned.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" -_:")
        if not line:
            continue
        if len(line) <= 1 and not line.isalnum():
            continue
        lines.append(line)
    return lines


def _detect_table_headers(lines: list[str]) -> list[dict[str, Any]]:
    headers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        for token in re.findall(r"\b(?:Dim|DIM|dim|Fact|FACT|fact)[._ -]?[A-Za-z0-9_]*\b", line):
            table_name = _normalize_table_name(token)
            if not table_name or table_name.lower() in seen:
                continue
            seen.add(table_name.lower())
            role = "fact" if table_name.lower().startswith("fact") else "dimension"
            headers.append(
                {
                    "table_name": table_name,
                    "role": role,
                    "line_index": index,
                    "confidence": 0.82 if "_" in table_name else 0.68,
                }
            )
    return headers


def _extract_columns(lines: list[str], headers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not headers:
        return []
    columns_by_table: dict[str, list[dict[str, Any]]] = {item["table_name"]: [] for item in headers}
    header_by_index = {int(item["line_index"]): item for item in headers}
    header_names = {item["table_name"].lower() for item in headers}
    current_table = headers[0]["table_name"]
    for index, line in enumerate(lines):
        if index in header_by_index:
            current_table = header_by_index[index]["table_name"]
            line = _remove_table_tokens(line, header_names)
        tokens = _column_tokens(line)
        for token in tokens:
            if _normalize_table_name(token).lower() in header_names:
                continue
            column = _column_record(current_table, token)
            if _has_column(columns_by_table[current_table], column["column_name"]):
                continue
            columns_by_table[current_table].append(column)
    return [column for values in columns_by_table.values() for column in values]


def _infer_relationships(
    headers: list[dict[str, Any]],
    columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    table_roles = {item["table_name"]: item["role"] for item in headers}
    by_table: dict[str, list[dict[str, Any]]] = {}
    for column in columns:
        by_table.setdefault(str(column["table_name"]), []).append(column)
    dimensions = [name for name, role in table_roles.items() if role == "dimension"]
    facts = [name for name, role in table_roles.items() if role == "fact"]
    relationships: list[dict[str, Any]] = []
    for fact in facts:
        for fact_column in by_table.get(fact, []):
            if fact_column.get("key_role") not in {"foreign_key", "identifier_candidate"}:
                continue
            target = _best_dimension_match(str(fact_column["column_name"]), dimensions, by_table)
            if not target:
                continue
            target_table, target_column, confidence = target
            relationships.append(
                {
                    "relationship_id": _relationship_id(
                        fact,
                        str(fact_column["column_name"]),
                        target_table,
                        target_column,
                    ),
                    "from_table": fact,
                    "from_column": fact_column["column_name"],
                    "to_table": target_table,
                    "to_column": target_column,
                    "cardinality": "many_to_one",
                    "join_type": "left",
                    "source": "ocr_layout",
                    "state": "candidate_image_parsed",
                    "confidence": confidence,
                    "executable_usage_allowed": False,
                    "promotion_required": "profile_or_catalog_match_or_user_confirmation",
                }
            )
    return _dedupe_relationships(relationships)


def _best_dimension_match(
    column_name: str,
    dimensions: list[str],
    by_table: dict[str, list[dict[str, Any]]],
) -> tuple[str, str, float] | None:
    col_norm = _norm(column_name)
    if not col_norm.endswith(("id", "code", "npi")) and col_norm != "npi":
        return None
    best: tuple[str, str, float] | None = None
    key_root = _semantic_key_root(col_norm)
    for dimension in dimensions:
        dim_norm = _norm(dimension)
        dim_suffix = dim_norm.removeprefix("dim")
        if key_root and _compatible_dimension_root(key_root, dim_suffix):
            return (
                dimension,
                _best_target_column_name(column_name, by_table.get(dimension, []), dimension),
                0.86,
            )
        for target_column in by_table.get(dimension, []):
            target_name = str(target_column["column_name"])
            target_norm = _norm(target_name)
            score = 0.0
            if col_norm == target_norm:
                score = 0.9
            elif dim_suffix and dim_suffix in col_norm:
                score = 0.74
            if score > 0 and (best is None or score > best[2]):
                best = (dimension, target_name, score)
    return best


def _best_target_column_name(
    source_column: str,
    target_columns: list[dict[str, Any]],
    dimension: str,
) -> str:
    source_norm = _norm(source_column)
    for target in target_columns:
        name = str(target.get("column_name") or "")
        if _norm(name) == source_norm:
            return name
    source_root = _semantic_key_root(source_norm)
    dim_suffix = _norm(dimension).removeprefix("dim")
    if source_root and source_root != dim_suffix and _compatible_dimension_root(source_root, dim_suffix):
        return _canonical_source_key(source_column)
    return _canonical_key_for_dimension(dimension, source_column)


def _semantic_key_root(column_norm: str) -> str:
    for suffix in ("id", "code"):
        if column_norm.endswith(suffix) and len(column_norm) > len(suffix):
            return column_norm[: -len(suffix)]
    if column_norm == "npi":
        return "npi"
    return ""


def _compatible_dimension_root(key_root: str, dim_suffix: str) -> bool:
    """Compare a foreign-key root against a dimension's name suffix.

    Two structural, workspace-agnostic rules:
    1. Equality — ``provider`` == ``provider``.
    2. Abbreviation — the shorter root is a first-character-anchored
       SUBSEQUENCE of the longer, and that shorter root is at least 4
       characters. This catches contractions that drop interior letters, so
       ``dept`` matches ``department`` (d-e-p-...-t) and ``prov`` matches
       ``provider`` — a prefix test alone would miss ``dept`` because the 4th
       letter of ``department`` is ``a``, not ``t``. Anchoring on the first
       letter plus the length guard keeps short tokens (``id``, ``sk``,
       ``npi``) and unrelated roots (``icd`` vs ``diagnosis``) off this path.

    Both rules are purely lexical. Workspace-specific SYNONYMS (e.g.
    diagnosis↔icd) deliberately do NOT live here — they used to sit in a
    hardcoded healthcare-leaning dict and should instead come from the
    workspace lexicon (`workspace_feature_definitions.json` or accepted
    aliases in `workspace_vocabulary.json`). Image-parsed relationships still
    require user review, so a missed synonym lands as a blocker panel — the
    correct behavior — not as silently-wrong inference.
    """
    if key_root == dim_suffix:
        return True
    shorter, longer = sorted((key_root, dim_suffix), key=len)
    if len(shorter) >= 4 and _is_anchored_subsequence(shorter, longer):
        return True
    return False


def _is_anchored_subsequence(short: str, long: str) -> bool:
    """True when ``short`` is a subsequence of ``long`` sharing the first char.

    Lets a contraction match its full word (``dept`` -> ``department``) without
    matching unrelated roots that merely share scattered letters.
    """
    if not short or not long or short[0] != long[0]:
        return False
    it = iter(long)
    return all(ch in it for ch in short)


def _canonical_key_for_dimension(dimension: str, fallback_column: str) -> str:
    suffix = _norm(dimension).removeprefix("dim")
    if not suffix:
        return fallback_column
    if suffix == "npi":
        return "NPI"
    return f"{suffix.title()}_ID"


def _canonical_source_key(column_name: str) -> str:
    normalized = _normalize_column_name(column_name)
    return re.sub(r"([A-Za-z]+)(ID|Code)$", r"\1_\2", normalized)


def _detected_schema_types(tables: list[dict[str, Any]], relationships: list[dict[str, Any]]) -> list[str]:
    has_fact = any(item.get("role") == "fact" for item in tables)
    dim_count = sum(1 for item in tables if item.get("role") == "dimension")
    if has_fact and dim_count >= 2:
        return ["star_schema"]
    if relationships:
        return ["relational_model"]
    return []


def _match_schema_to_profiles(
    parsed_schema: dict[str, Any],
    *,
    layout: WorkspaceLayout,
    repo_root: Path,
) -> dict[str, Any]:
    profiles = _load_profile_index(layout, repo_root)
    rule = (
        "Image elements must match workspace profiles, catalog/schema evidence, or explicit user "
        "approval before relationship-contract promotion."
    )
    if not profiles:
        return {
            "state": "profile_index_missing",
            "rule": rule,
            "profile_index_path": _rel(layout.profiles_dir / "profile_index.json", repo_root),
            "table_matches": [],
            "column_matches": [],
            "relationship_matches": [],
            "unmatched_tables": [table.get("table_name") for table in parsed_schema.get("tables") or []],
            "unmatched_columns": [column.get("column_name") for column in parsed_schema.get("columns") or []],
        }

    table_matches = [
        match
        for table in parsed_schema.get("tables") or []
        if (match := _best_table_profile_match(table, profiles))
    ]
    table_match_by_name = {match["table_name"]: match for match in table_matches}
    column_matches: list[dict[str, Any]] = []
    for column in parsed_schema.get("columns") or []:
        match = _best_column_profile_match(column, profiles, table_match_by_name)
        if match:
            column_matches.append(match)
    column_matches = _add_relationship_endpoint_profile_matches(
        column_matches,
        parsed_schema.get("relationships") or [],
        profiles,
        table_match_by_name,
    )
    relationship_matches = _relationship_profile_matches(
        parsed_schema.get("relationships") or [],
        column_matches,
    )
    matched_tables = {item["table_name"] for item in table_matches}
    matched_column_keys = {(item["table_name"], item["column_name"]) for item in column_matches}
    exact_relationships = sum(1 for item in relationship_matches if item.get("state") == "profile_matched")
    state = "matched" if exact_relationships else "partial_match" if column_matches or table_matches else "unmatched"
    return {
        "state": state,
        "rule": rule,
        "profile_index_path": _rel(layout.profiles_dir / "profile_index.json", repo_root),
        "profile_count": len(profiles),
        "table_matches": table_matches,
        "column_matches": column_matches,
        "relationship_matches": relationship_matches,
        "unmatched_tables": [
            table.get("table_name")
            for table in parsed_schema.get("tables") or []
            if table.get("table_name") not in matched_tables
        ],
        "unmatched_columns": [
            column.get("column_name")
            for column in parsed_schema.get("columns") or []
            if (column.get("table_name"), column.get("column_name")) not in matched_column_keys
        ],
    }


def _load_profile_index(layout: WorkspaceLayout, repo_root: Path) -> list[dict[str, Any]]:
    path = layout.profiles_dir / "profile_index.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    profiles: list[dict[str, Any]] = []
    for profile in data.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        dataset = _repo_path(str(profile.get("path") or profile.get("dataset") or ""), repo_root)
        schema = profile.get("schema") if isinstance(profile.get("schema"), dict) else {}
        columns = []
        if schema:
            columns = [{"name": name, "dtype": dtype} for name, dtype in schema.items()]
        elif isinstance(profile.get("columns"), list):
            columns = [
                column if isinstance(column, dict) else {"name": str(column)}
                for column in profile.get("columns") or []
            ]
        profiles.append(
            {
                "dataset": dataset,
                "dataset_stem": Path(dataset).stem if dataset else "",
                "columns": [
                    {
                        "name": str(column.get("name") or column.get("column") or ""),
                        "dtype": column.get("dtype") or column.get("type") or "",
                    }
                    for column in columns
                    if str(column.get("name") or column.get("column") or "")
                ],
                "profile_path": _rel(layout.profiles_dir / f"{Path(dataset).name}.profile.json", repo_root)
                if dataset
                else "",
            }
        )
    return profiles


def _best_table_profile_match(table: dict[str, Any], profiles: list[dict[str, Any]]) -> dict[str, Any] | None:
    table_name = str(table.get("table_name") or "")
    role_suffix = _table_business_root(table_name)
    best: dict[str, Any] | None = None
    for profile in profiles:
        dataset = str(profile.get("dataset") or "")
        dataset_stem = str(profile.get("dataset_stem") or "")
        dataset_terms = _dataset_terms(dataset_stem)
        score = _similarity(role_suffix, _norm(dataset_stem))
        if role_suffix in dataset_terms:
            score = max(score, 0.92)
        elif role_suffix and any(role_suffix in term or term in role_suffix for term in dataset_terms):
            score = max(score, 0.78)
        if score < 0.68:
            continue
        candidate = {
            "table_name": table_name,
            "dataset": dataset,
            "profile_path": profile.get("profile_path", ""),
            "match_type": "exact_or_alias" if score >= 0.9 else "fuzzy",
            "confidence": round(score, 3),
            "state": "profile_matched" if score >= 0.9 else "fuzzy_profile_suggestion",
        }
        if best is None or candidate["confidence"] > best["confidence"]:
            best = candidate
    return best


def _generic_fact_table_profile_match(
    table_name: str,
    column_name: str,
    profiles: list[dict[str, Any]],
    *,
    exclude_datasets: set[str] | None = None,
) -> dict[str, Any] | None:
    """Resolve a generic/unmatched diagram table (e.g. bare 'Fact') to the physical
    profile dataset that actually contains the normalized FK column.

    When a diagram uses an abstract fact-table name with no business suffix (e.g.
    'Fact' or 'Fact_Table'), _best_table_profile_match produces no match because
    the role_suffix is empty. This fallback searches all profiles for one that
    contains a column matching `column_name` after normalization.

    `exclude_datasets`: profiles already claimed by the other endpoint of the same
    relationship edge are excluded. This handles the case where the FK column appears
    in both the fact table (transactions) and the dimension table (patients) — the
    dimension's dataset is excluded so the fact table is chosen unambiguously.

    If no single match remains after exclusion the function returns None; never
    fabricates a mapping. Workspace-agnostic: no dataset or column names are hardcoded.
    """
    col_norm = _norm(column_name)
    if not col_norm:
        return None
    excluded = exclude_datasets or set()
    matching_profiles = []
    for profile in profiles:
        dataset = str(profile.get("dataset") or "")
        if dataset in excluded:
            continue
        for profile_column in profile.get("columns") or []:
            profile_column_name = str(profile_column.get("name") or "")
            if _norm(profile_column_name) == col_norm:
                matching_profiles.append(profile)
                break
    if len(matching_profiles) != 1:
        # Ambiguous (column appears in multiple non-excluded profiles) or absent -> no match.
        return None
    profile = matching_profiles[0]
    # Find the physical column name (preserving original casing).
    physical_col = next(
        (
            str(c.get("name") or "")
            for c in (profile.get("columns") or [])
            if _norm(str(c.get("name") or "")) == col_norm
        ),
        column_name,
    )
    return {
        "table_name": table_name,
        "dataset": str(profile.get("dataset") or ""),
        "profile_column": physical_col,
        "profile_path": profile.get("profile_path", ""),
        "match_type": "column_resolved_fact",
        "confidence": 0.85,
        "state": "profile_matched",
    }


def _best_column_profile_match(
    column: dict[str, Any],
    profiles: list[dict[str, Any]],
    table_match_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    table_name = str(column.get("table_name") or "")
    column_name = str(column.get("column_name") or "")
    table_match = table_match_by_name.get(table_name)
    candidate_profiles = profiles
    if table_match:
        candidate_profiles = [p for p in profiles if p.get("dataset") == table_match.get("dataset")] or profiles
    best: dict[str, Any] | None = None
    for profile in candidate_profiles:
        for profile_column in profile.get("columns") or []:
            profile_column_name = str(profile_column.get("name") or "")
            score = _column_similarity(column_name, profile_column_name)
            if score < 0.72:
                continue
            candidate = {
                "table_name": table_name,
                "column_name": column_name,
                "dataset": profile.get("dataset", ""),
                "profile_column": profile_column_name,
                "profile_path": profile.get("profile_path", ""),
                "match_type": "exact_or_alias" if score >= 0.92 else "fuzzy",
                "confidence": round(score, 3),
                "state": "profile_matched" if score >= 0.92 else "fuzzy_profile_suggestion",
            }
            if best is None or candidate["confidence"] > best["confidence"]:
                best = candidate
    return best


def _relationship_profile_matches(
    relationships: list[dict[str, Any]],
    column_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_key = {
        (str(match.get("table_name")), str(match.get("column_name"))): match
        for match in column_matches
    }
    matches: list[dict[str, Any]] = []
    for relationship in relationships:
        left = by_key.get((str(relationship.get("from_table")), str(relationship.get("from_column"))))
        right = by_key.get((str(relationship.get("to_table")), str(relationship.get("to_column"))))
        state = "profile_matched" if left and right else "partial_profile_match" if left or right else "unmatched"
        confidence = min(float(left.get("confidence", 0.0)) if left else 0.0, float(right.get("confidence", 0.0)) if right else 0.0)
        matches.append(
            {
                "relationship_id": relationship.get("relationship_id", ""),
                "state": state,
                "confidence": round(confidence, 3) if confidence else 0.0,
                "from_match": left or {},
                "to_match": right or {},
                "executable_usage_allowed": False,
            }
        )
    return matches


def _add_relationship_endpoint_profile_matches(
    column_matches: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    table_match_by_name: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    existing = {
        (str(match.get("table_name")), str(match.get("column_name")))
        for match in column_matches
    }
    # Build a running lookup of resolved column matches (seeded from columns already
    # matched in the main pass) so the generic-fact resolver can exclude datasets
    # claimed by the other endpoint of the same relationship.
    match_by_key: dict[tuple[str, str], dict[str, Any]] = {
        (str(m.get("table_name")), str(m.get("column_name"))): m
        for m in column_matches
    }
    updated = list(column_matches)

    # Process each relationship in two ordered passes per edge so that named-table
    # endpoints (dimension tables) are resolved first, enabling the generic-fact
    # resolver in pass 2 to exclude the dimension's dataset and unambiguously land
    # on the physical fact table. Workspace-agnostic: no dataset names hardcoded.
    for relationship in relationships:
        endpoint_pairs = [
            ("from_table", "from_column", "to_table", "to_column"),
            ("to_table", "to_column", "from_table", "from_column"),
        ]
        for pass_num in (1, 2):
            for table_key, column_key, other_table_key, other_column_key in endpoint_pairs:
                table_name = str(relationship.get(table_key) or "")
                column_name = str(relationship.get(column_key) or "")
                key = (table_name, column_name)
                if not table_name or not column_name or key in existing:
                    continue
                has_table_match = table_match_by_name.get(table_name) is not None

                # Pass 1: only resolve endpoints that have a direct table match
                # (named dim tables).  Pass 2: resolve remaining endpoints (generic
                # fact tables) now that the dim side is in match_by_key.
                if pass_num == 1 and not has_table_match:
                    continue
                if pass_num == 2 and has_table_match:
                    continue

                match = _best_column_profile_match(
                    {"table_name": table_name, "column_name": column_name},
                    profiles,
                    table_match_by_name,
                )
                if not match and not has_table_match:
                    # Generic/unmatched fact table (e.g. bare 'Fact' with no
                    # business suffix): resolve to the profile that actually holds
                    # this FK column, excluding the dataset already claimed by the
                    # other endpoint so the two sides land on distinct datasets.
                    other_table = str(relationship.get(other_table_key) or "")
                    other_col = str(relationship.get(other_column_key) or "")
                    other_match = match_by_key.get((other_table, other_col))
                    exclude: set[str] = set()
                    if other_match and other_match.get("dataset"):
                        exclude.add(str(other_match["dataset"]))
                    fact_match = _generic_fact_table_profile_match(
                        table_name, column_name, profiles, exclude_datasets=exclude
                    )
                    if fact_match:
                        match = {
                            "table_name": table_name,
                            "column_name": column_name,
                            "dataset": fact_match["dataset"],
                            "profile_column": fact_match["profile_column"],
                            "profile_path": fact_match.get("profile_path", ""),
                            "match_type": fact_match["match_type"],
                            "confidence": fact_match["confidence"],
                            "state": fact_match["state"],
                            "source": "relationship_endpoint_fact_resolved",
                        }
                if match:
                    match = {**match, "source": match.get("source", "relationship_endpoint")}
                    updated.append(match)
                    existing.add(key)
                    match_by_key[key] = match
    return updated


def _apply_relationship_profile_matches(
    relationships: list[dict[str, Any]],
    profile_matching: dict[str, Any],
) -> list[dict[str, Any]]:
    matches = {
        str(item.get("relationship_id")): item
        for item in profile_matching.get("relationship_matches") or []
    }
    updated: list[dict[str, Any]] = []
    for relationship in relationships:
        match = matches.get(str(relationship.get("relationship_id"))) or {}
        relationship_state = "candidate_image_profile_matched" if match.get("state") == "profile_matched" else relationship.get("state", "candidate_image_parsed")
        updated.append(
            {
                **relationship,
                "state": relationship_state,
                "profile_match_state": match.get("state", "unmatched"),
                "profile_match": match,
                "executable_usage_allowed": False,
            }
        )
    return updated


def _column_similarity(left: str, right: str) -> float:
    left_norm = _norm(left)
    right_norm = _norm(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    aliases = {
        "deptid": "departmentid",
        "departmentid": "deptid",
        "payorid": "payerid",
        "payerid": "payorid",
    }
    if aliases.get(left_norm) == right_norm:
        return 0.95
    return _similarity(left_norm, right_norm)


def _table_business_root(table_name: str) -> str:
    norm = _norm(table_name)
    for prefix in ("dim", "fact"):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
            break
    return norm


def _dataset_terms(dataset_stem: str) -> set[str]:
    terms = {_norm(dataset_stem)}
    for token in re.split(r"[^A-Za-z0-9]+", dataset_stem):
        clean = _norm(token)
        if len(clean) >= 2:
            terms.add(clean)
            if clean.endswith("s"):
                terms.add(clean[:-1])
    return terms


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _remove_table_tokens(line: str, header_names: set[str]) -> str:
    result = line
    for name in sorted(header_names, key=len, reverse=True):
        result = re.sub(re.escape(name), " ", result, flags=re.IGNORECASE)
        result = re.sub(re.escape(name.replace("_", " ")), " ", result, flags=re.IGNORECASE)
        result = re.sub(re.escape(name.replace("_", ".")), " ", result, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip()


def _column_tokens(line: str) -> list[str]:
    tokens: list[str] = []
    pending_key_role = ""
    for raw in re.split(r"\s+", line):
        token = raw.strip(" ,;:()[]{}")
        if not token:
            continue
        low = token.lower()
        if low in {"pk", "sk", "fk"}:
            pending_key_role = low
            continue
        if not _looks_like_column(token):
            pending_key_role = ""
            continue
        if pending_key_role:
            token = f"{pending_key_role.upper()}:{token}"
            pending_key_role = ""
        tokens.append(token)
    return tokens


def _column_record(table_name: str, token: str) -> dict[str, Any]:
    key_role = ""
    raw = token
    if ":" in token:
        prefix, token = token.split(":", 1)
        key_role = {"PK": "primary_key", "SK": "surrogate_key", "FK": "foreign_key"}.get(prefix.upper(), "")
    if not key_role and _norm(token).endswith(("id", "code")):
        key_role = "identifier_candidate"
    return {
        "table_name": table_name,
        "column_name": _normalize_column_name(token),
        "raw_text": raw,
        "key_role": key_role,
        "source": "ocr_layout",
        "state": "candidate_image_parsed",
        "confidence": 0.72 if key_role else 0.62,
    }


def _looks_like_column(token: str) -> bool:
    if re.fullmatch(r"\d+", token):
        return False
    if len(token) < 2:
        return False
    if token.lower() in {"and", "or", "the", "name"}:
        return False
    return bool(re.search(r"[A-Za-z]", token))


def _normalize_table_name(token: str) -> str:
    token = token.replace(".", "_").replace("-", "_").replace(" ", "_")
    token = re.sub(r"[^A-Za-z0-9_]+", "", token)
    if token.lower() == "fact":
        return "Fact"
    return token


def _normalize_column_name(token: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]+", "_", token).strip("_")
    return token


def _has_column(columns: list[dict[str, Any]], name: str) -> bool:
    normalized = _norm(name)
    return any(_norm(str(item.get("column_name") or "")) == normalized for item in columns)


def _dedupe_relationships(relationships: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for relationship in relationships:
        key = str(relationship.get("relationship_id") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(relationship)
    return deduped


def _relationship_id(left_table: str, left_col: str, right_table: str, right_col: str) -> str:
    return re.sub(
        r"[^a-z0-9_]+",
        "_",
        "__".join([left_table, left_col, right_table, right_col]).lower(),
    ).strip("_")


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _image_metadata(path: Path) -> dict[str, Any]:
    metadata = {
        "file_name": path.name,
        "suffix": path.suffix.lower(),
        "size_bytes": path.stat().st_size,
        "width_px": None,
        "height_px": None,
    }
    dimensions = _image_dimensions(path)
    if dimensions:
        metadata["width_px"], metadata["height_px"] = dimensions
    return metadata


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    suffix = path.suffix.lower()
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
        if suffix == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"):
            width, height = struct.unpack(">II", header[16:24])
            return int(width), int(height)
        if suffix in {".jpg", ".jpeg"}:
            return _jpeg_dimensions(path)
    except Exception:
        return None
    return None


def _jpeg_dimensions(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as handle:
        data = handle.read(2)
        if data != b"\xff\xd8":
            return None
        while True:
            marker_start = handle.read(1)
            if not marker_start:
                return None
            if marker_start != b"\xff":
                continue
            marker = handle.read(1)
            while marker == b"\xff":
                marker = handle.read(1)
            if marker in {b"\xc0", b"\xc1", b"\xc2", b"\xc3"}:
                handle.read(3)
                height, width = struct.unpack(">HH", handle.read(4))
                return int(width), int(height)
            length_bytes = handle.read(2)
            if len(length_bytes) != 2:
                return None
            length = struct.unpack(">H", length_bytes)[0]
            handle.seek(max(0, length - 2), 1)


def _render_sidecar_markdown(record: dict[str, Any]) -> str:
    metadata = record.get("image_metadata", {})
    lines = [
        f"# Data Model Image Review: {record.get('source_image', '')}",
        "",
        f"- Approval state: `{record.get('approval_state', '')}`",
        f"- Executable usage allowed: `{record.get('executable_usage_allowed', False)}`",
        f"- Size: `{metadata.get('size_bytes', 0)}` bytes",
        f"- Dimensions: `{metadata.get('width_px') or 'unknown'} x {metadata.get('height_px') or 'unknown'}`",
        "",
        "## Parser Status",
        "",
    ]
    parsers = record.get("parsers", {})
    for name, status in parsers.items():
        lines.append(f"- `{name}`: `{status.get('state', '')}`")
    lines.extend(
        [
            "",
            "## Governance",
            "",
            "- Image-derived relationships are non-executable by default.",
            "- Fuzzy table/column matches are suggestions until profile proof or user confirmation exists.",
            "- User approval must still pass relationship-contract validation before code generation.",
            "",
            "## Extracted Elements",
            "",
            f"- Tables: `{len(record.get('tables') or [])}`",
            f"- Columns: `{len(record.get('columns') or [])}`",
            f"- Relationships: `{len(record.get('relationships') or [])}`",
            "",
            "## Warnings",
            "",
        ]
    )
    for warning in record.get("warnings") or ["none"]:
        lines.append(f"- `{warning}`")
    return "\n".join(lines).rstrip() + "\n"


def _render_index_markdown(index: dict[str, Any]) -> str:
    lines = [
        "# Data Model Image Review Panel",
        "",
        f"- Workspace: `{index.get('workspace', '')}`",
        f"- Status: `{index.get('status', '')}`",
        f"- Image count: `{index.get('image_count', 0)}`",
        f"- Executable usage allowed: `{index.get('summary', {}).get('executable_usage_allowed', False)}`",
        "",
        "## Recommended Answer",
        "",
        str(index.get("recommended_answer", "")),
        "",
        "## Images",
        "",
    ]
    sidecars = index.get("image_sidecars") or []
    if not sidecars:
        lines.append("- No data-model images found.")
    for item in sidecars:
        lines.extend(
            [
                f"### `{item.get('source_image', '')}`",
                "",
                f"- Sidecar: `{item.get('sidecar_path', '')}`",
                f"- Report: `{item.get('report_path', '')}`",
                f"- Approval state: `{item.get('approval_state', '')}`",
                "",
            ]
        )
        warnings = item.get("warnings") or []
        if warnings:
            lines.append("Warnings:")
            for warning in warnings:
                lines.append(f"- `{warning}`")
            lines.append("")
    lines.extend(["## Next Step", "", str(index.get("next_step", "")), ""])
    return "\n".join(lines).rstrip() + "\n"


def _safe_stem(path: Path) -> str:
    stem = path.stem.lower()
    return "".join(char if char.isalnum() else "_" for char in stem).strip("_") or "image"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _repo_path(value: str, root: Path) -> str:
    if not value:
        return ""
    normalized = value.replace("\\", "/")
    marker = "workspaces/"
    if marker in normalized:
        return normalized[normalized.index(marker):]
    path = Path(value)
    if path.is_absolute():
        return _rel(path, root)
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create review-gated data-model image sidecars.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--local-ocr", choices=["auto", "off"], default="auto")
    parser.add_argument(
        "--auto-install-ocr",
        action="store_true",
        help="Attempt to install Tesseract OCR with a supported local package manager if missing.",
    )
    parser.add_argument("--allow-remote-vision", action="store_true")
    parser.add_argument("--confirm-sensitive-upload", action="store_true")
    args = parser.parse_args(argv)
    result = DataModelImageParser(Path(args.repo_root), args.workspace).parse(
        allow_remote_vision=args.allow_remote_vision,
        confirm_sensitive_upload=args.confirm_sensitive_upload,
        local_ocr=args.local_ocr,
        auto_install_ocr=args.auto_install_ocr,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
