"""Fresh workspace onboarding for KPI/query optimization tasks.

The onboarder treats ``workspaces/<project>`` as user input and writes every
generated artifact under ``workspaces/<project>/interns``.
"""
from __future__ import annotations
from core.governance.injection_guard import neutralize_text
from core.observability.cost_ledger import anchored

import argparse
import json
import re
import shutil
import zipfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from core.paths import PROJECT_ROOT
from core.paths import rel_to as _rel
from core.onboarding.kpi.text_parser import (
    KPI_CUTS_HEADERS,
    cell_at,
    clean_cell,
    extract_kpis_from_sql,
    first_existing,
    first_index,
    infer_metric_and_cuts,
    is_template_kpi_row,
)
from core.onboarding.kpi.kpi_confirmation_panel import (
    build_kpi_confirmation_panel,
    render_kpi_confirmation_markdown,
)
from core.onboarding.kpi.kpi_format_detector import KpiFormatDetection, detect_kpi_format
from core.onboarding.kpi.workbook_structure import read_workbook_grid
from core.onboarding.lexicon import (
    WorkspaceLexicon,
    build_workspace_lexicon,
)
from core.onboarding.workspace.incremental import (
    artifacts_exist,
    build_manifest_payload,
    diff_fingerprints,
    fingerprint_inputs,
    load_manifest,
    write_manifest,
)
from core.observability.events import time_command
from core.resource.manager import ResourceManager
from core.profiling.data_model_profiler import DataModelProfiler
from core.storage.external_data import is_external_path, load_external_data_policy
from core.storage.metadata_store import MetadataStore, build_metadata_store
from core.storage.workspace_layout import WorkspaceLayout
from core.storage.workspace_lock import WorkspaceLockTimeout, workspace_lock
from core.contracts.versioning import register_contract

register_contract("kpi_registry.json", current_version=1)

try:
    import polars as pl
except ImportError:  # pragma: no cover - optional at runtime
    pl = None


DATA_SUFFIXES = {".csv", ".parquet", ".pq", ".json", ".ndjson"}
REGISTRY_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".json", ".md", ".sql", ".txt", ".yaml", ".yml", ".toml"}
MODEL_SUFFIXES = {".csv", ".md", ".png", ".jpg", ".jpeg", ".svg", ".json", ".sql", ".txt", ".yaml", ".yml"}


@dataclass(frozen=True)
class WorkspaceInputs:
    workspace: str
    data_files: list[str] = field(default_factory=list)
    kpi_registries: list[str] = field(default_factory=list)
    data_models: list[str] = field(default_factory=list)
    # Fully-qualified "catalog.schema.table" strings -- populated generically
    # from workspace_settings.json's "databricks_source" declaration (see
    # _databricks_source_tables()), never hardcoded to any specific workspace.
    # Empty for every workspace that doesn't declare one; local-file discovery
    # above is completely unaffected either way.
    databricks_tables: list[str] = field(default_factory=list)
    # WorkspaceLayout.databricks_source_mode(): "local_files" (default),
    # "additive" (today's original silent merge), or "exclusive" (local
    # data_files discovery is skipped -- see discover_inputs()).
    databricks_source_mode: str = "local_files"


@dataclass(frozen=True)
class KpiDefinition:
    name: str
    description: str = ""
    cuts: str = ""
    metric: str = ""
    refinement_required: str = ""
    source: str = ""
    status: str = "needs_mapping"
    # Cell provenance: how the metric/cuts VALUES got there. "authored" means
    # the registry file carried the cell; gap-fill passes record themselves
    # ("lexicon_inferred", "derived_from_question") and accepted human
    # decisions record "user_confirmed". The resolver uses this to keep
    # machine-guessed metrics from silently reaching ready_for_sql.
    metric_provenance: str = "authored"
    cuts_provenance: str = "authored"


@dataclass(frozen=True)
class OnboardingResult:
    workspace: str
    interns_dir: str
    inputs: WorkspaceInputs
    kpi_count: int
    profile_count: int
    artifacts: dict[str, str]
    next_step: str
    next_command: str
    warnings: list[str] = field(default_factory=list)
    # Incremental-run accounting: mode is "full" | "incremental" | "skipped";
    # counts say how many datasets were re-profiled vs reused vs dropped.
    incremental: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "interns_dir": self.interns_dir,
            "inputs": asdict(self.inputs),
            "kpi_count": self.kpi_count,
            "profile_count": self.profile_count,
            "artifacts": self.artifacts,
            "next_step": self.next_step,
            "next_command": self.next_command,
            "warnings": self.warnings,
            "incremental": self.incremental,
        }


class WorkspaceOnboarder:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        exact_profile: bool = False,
        sample_rows: int = 100_000,
        metadata_store: MetadataStore | None = None,
        force: bool = False,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.exact_profile = exact_profile
        self.sample_rows = sample_rows
        # force=True preserves the legacy behavior: clear everything and
        # re-profile every dataset regardless of the incremental manifest.
        self.force = force
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.profiler = DataModelProfiler()
        self.metadata_store = metadata_store or build_metadata_store(self.layout, repo_root=self.repo_root)
        # Set by discover_inputs() when databricks_source_mode()=="exclusive"
        # and local dataset files are physically present despite that mode.
        self._exclusive_mode_stale_local_files: list[str] = []

    def run(self) -> OnboardingResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        with workspace_lock(self.workspace):
            return self._run_locked()

    def _extract_data_model_documents(
        self,
        data_models: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Run methodology_parser.parse_document() on PDF/DOCX data-model
        files and write their extracted text under
        ``interns/generated/data_dictionary/<safe_name>.txt``. Returns a list
        of ``{path, text_path, char_count}`` entries plus any warnings.

        Failures are non-fatal: a missing pdfplumber/python-docx dependency
        produces a warning, never an error. The extracted text is downstream
        evidence for the CLI-agent proposal panel and (in future) for
        targeted lexicon harvesting.
        """
        try:
            from tools.methodology_parser import parse_document
        except Exception as exc:
            return [], [f"methodology_parser_import_failed:{type(exc).__name__}:{exc}"]

        supported_suffixes = {".pdf", ".docx"}
        extracted: list[dict[str, Any]] = []
        warnings: list[str] = []
        dictionary_dir = self.layout.generated_dir / "data_dictionary"
        for rel in data_models:
            path = self.repo_root / rel
            if not path.exists() or not path.is_file():
                continue
            if path.suffix.lower() not in supported_suffixes:
                continue
            try:
                text = parse_document(str(path))
            except ImportError as exc:
                warnings.append(
                    f"data_model_document_skipped:{rel}:missing_dependency:{exc}"
                )
                continue
            except Exception as exc:
                warnings.append(
                    f"data_model_document_extraction_failed:{rel}:{type(exc).__name__}:{exc}"
                )
                continue
            if not text or not text.strip():
                continue
            dictionary_dir.mkdir(parents=True, exist_ok=True)
            safe_stem = _safe_stem(path, self.workspace)
            text_path = dictionary_dir / f"{safe_stem}.txt"
            # Neutralized at the SOURCE, not just at whatever currently reads
            # this file (_cli_agent_evidence_pack): this text is raw PDF/DOCX
            # content from the workspace's own uploaded documents, and every
            # current and future consumer of interns/generated/data_dictionary/
            # *.txt should get the protection, not just today's one reader.
            text_path.write_text(neutralize_text(text), encoding="utf-8")
            extracted.append(
                {
                    "path": _rel(path, self.repo_root),
                    "text_path": _rel(text_path, self.repo_root),
                    "char_count": len(text),
                }
            )
        return extracted, warnings

    def _parse_data_model_images(self) -> tuple[dict[str, str], list[str]]:
        """Invoke DataModelImageParser for any data-model image under the workspace
        docs tree.  Writes review-gated sidecars under
        ``interns/generated/data_model_images/``.

        LOCAL-SAFE ONLY: remote vision and sensitive-upload flags are always off.
        If Tesseract / local OCR is unavailable, degrades gracefully with a [~]
        warning so onboarding continues without hard-failing.

        Returns ``(artifacts_dict, warnings_list)`` where the artifacts dict
        contains ``"diagram_sidecar_dir"`` and ``"diagram_current_json"`` when at
        least one image was processed; it is empty when no images are found or when
        the parser cannot be imported.
        """
        try:
            from core.onboarding.data_model.image_parser import DataModelImageParser
        except Exception as exc:  # pragma: no cover - import guard
            return {}, [f"data_model_image_parser_import_failed:{type(exc).__name__}:{exc}"]

        try:
            parse_result = DataModelImageParser(self.repo_root, self.workspace).parse(
                allow_remote_vision=False,
                confirm_sensitive_upload=False,
                local_ocr="auto",
                auto_install_ocr=False,
            )
        except Exception as exc:
            return {}, [f"data_model_image_parse_failed:{type(exc).__name__}:{exc}"]

        warnings: list[str] = []
        artifacts: dict[str, str] = {}

        if parse_result.image_count == 0:
            return artifacts, warnings

        # Surface OCR availability through warnings rather than errors.
        for sidecar_path in parse_result.generated_sidecars:
            try:
                import json as _json
                sidecar = _json.loads((self.repo_root / sidecar_path).read_text(encoding="utf-8"))
                ocr_state = (sidecar.get("parsers") or {}).get("ocr_layout") or {}
                if ocr_state.get("state") in {"provider_not_configured", "failed"}:
                    warnings.append(
                        f"[~] data_model_image_ocr_unavailable:{sidecar_path}:"
                        "Tesseract not found locally; diagram parsed without OCR text. "
                        "Install Tesseract or pass --auto-install-ocr for full extraction."
                    )
            except Exception:
                pass

        artifacts["diagram_sidecar_dir"] = str(
            self.layout.generated_dir / "data_model_images"
        )
        artifacts["diagram_current_json"] = parse_result.current_json_path
        artifacts["diagram_current_md"] = parse_result.current_markdown_path
        return artifacts, warnings

    def _scan_documents(self) -> tuple[dict[str, str], list[str]]:
        """Run the local-safe opendataloader PDF extractor (free mode) over any
        PDF under the workspace, writing review-gated sidecars under
        ``interns/generated/documents/`` and propose-only candidate records.

        LOCAL-SAFE ONLY: free mode (deterministic, no AI backend, no upload).
        Degrades gracefully with a [~] warning when opendataloader-pdf or Java is
        unavailable, so onboarding continues without hard-failing. Candidates are
        review-gated and are NEVER auto-promoted into any contract.
        """
        warnings: list[str] = []
        artifacts: dict[str, str] = {}
        try:
            from core.onboarding.documents.classifier import classify_document
            from core.onboarding.documents.document_loader import scan_document
        except Exception as exc:  # pragma: no cover - import guard
            return {}, [f"document_scan_import_failed:{type(exc).__name__}:{exc}"]

        interns_dir = self.layout.interns_dir.resolve()
        pdfs = sorted(
            p
            for p in self.workspace.rglob("*.pdf")
            if interns_dir not in p.resolve().parents
        )
        if not pdfs:
            return artifacts, warnings

        workspace_rel = _rel(self.workspace, self.repo_root)
        all_candidates: list[dict[str, Any]] = []
        document_types: dict[str, dict[str, Any]] = {}
        scanned = 0
        for pdf in pdfs:
            try:
                result = scan_document(self.repo_root, workspace_rel, str(pdf), mode="free")
            except Exception as exc:
                warnings.append(
                    f"[~] document_scan_failed:{_rel(pdf, self.repo_root)}:"
                    f"{type(exc).__name__}:{exc}"
                )
                continue
            if result.status == "blocker":
                warnings.append(
                    f"[~] document_scan_unavailable:{_rel(pdf, self.repo_root)}:"
                    f"{getattr(result, 'blocker_reason', '') or 'extractor unavailable'} "
                    "(install opendataloader-pdf + Java 11+ for PDF ingestion)."
                )
                continue
            if result.status != "ok":
                continue
            scanned += 1
            pdf_rel = _rel(pdf, self.repo_root)
            try:
                from core.onboarding.documents.classifier import (
                    CANDIDATE_RAW_EVIDENCE,
                    detect_document_type,
                )

                sidecar = json.loads(
                    (self.repo_root / result.sidecar_path).read_text(encoding="utf-8")
                )
                extracted = sidecar.get("extracted_content") or {}
                doc_type_info = detect_document_type(extracted)
                document_types[pdf_rel] = doc_type_info
                cands = classify_document(extracted)
                for cand in cands:
                    cand["source_document"] = pdf_rel
                    all_candidates.append(cand)
                # "No actionable candidates" signal (the skill's `None` tier): a
                # document that yielded nothing routable is reference-only, not a
                # silent no-op. Raw-evidence-only counts as no actionable candidate.
                routable = [
                    c for c in cands
                    if c.get("candidate_type") != CANDIDATE_RAW_EVIDENCE
                ]
                if not routable:
                    warnings.append(
                        f"[~] document_scanned_no_candidates:{pdf_rel}: "
                        f"document_type={doc_type_info.get('document_type')} "
                        "(reference-only; no KPI/lexicon/relationship/rule signals found)."
                    )
            except Exception as exc:
                warnings.append(
                    f"[~] document_classify_failed:{pdf_rel}:"
                    f"{type(exc).__name__}:{exc}"
                )

        if scanned == 0:
            return artifacts, warnings

        documents_dir = self.layout.generated_dir / "documents"
        candidates_path = self._write_json(
            documents_dir / "candidates.json",
            {
                "artifact_type": "document_candidates.json",
                "version": 1,
                "generated_by": "onboard-workspace",
                "workspace": workspace_rel,
                "authoritative_usage_allowed": False,
                "note": (
                    "Propose-only candidates extracted from PDFs; review-gated, "
                    "never auto-promoted into any contract."
                ),
                "scanned_pdf_count": scanned,
                "candidate_count": len(all_candidates),
                "document_types": document_types,
                "candidates": all_candidates,
            },
        )
        artifacts["document_sidecar_dir"] = str(documents_dir)
        artifacts["document_candidates"] = str(candidates_path)
        return artifacts, warnings

    def _accepted_document_kpis(
        self, existing: list[KpiDefinition]
    ) -> tuple[list[KpiDefinition], list[str]]:
        """Merge human-confirmed PDF KPI candidates (the durable accepted-candidates
        store) into the KPI set as additional proposals.

        GOVERNED: only candidates a human confirmed via `apply-document-candidate`
        are consumed (never raw-extracted text). Each becomes a KpiDefinition with
        `status=needs_mapping` and `source=document_pdf:<file>`, so it flows through
        the normal feature-resolution / proof gates like any other KPI. New names
        only — never overrides an authored workbook KPI.
        """
        warnings: list[str] = []
        try:
            from core.onboarding.documents.candidate_apply import merge_accepted_candidates
        except Exception:  # pragma: no cover - import guard
            return [], warnings
        try:
            merged = merge_accepted_candidates(self.layout)
        except Exception as exc:
            return [], [f"[~] accepted_document_kpi_merge_failed:{type(exc).__name__}:{exc}"]

        accepted = merged.get("kpi_registry_candidates") or []
        if not accepted:
            return [], warnings

        existing_names = {(k.name or "").strip().lower() for k in existing}
        out: list[KpiDefinition] = []
        for entry in accepted:
            content = entry.get("content") or {}
            headers = [str(h) for h in (content.get("headers") or [])]
            roles = {h: _document_kpi_header_role(h) for h in headers}
            for row in content.get("sample_rows") or []:
                if not isinstance(row, dict):
                    continue
                name = metric = cuts = ""
                for header, value in row.items():
                    role = roles.get(str(header)) or _document_kpi_header_role(str(header))
                    text = str(value).strip()
                    if role == "name" and not name:
                        name = text
                    elif role == "metric" and not metric:
                        metric = text
                    elif role == "cuts" and not cuts:
                        cuts = text
                if not name or name.lower() in existing_names:
                    continue
                existing_names.add(name.lower())
                out.append(
                    KpiDefinition(
                        name=name,
                        metric=metric,
                        cuts=cuts,
                        source=f"document_pdf:{entry.get('source_document', '')}",
                        status="needs_mapping",
                    )
                )
        if out:
            warnings.append(
                f"[~] document_kpis_merged:{len(out)} KPI(s) from human-confirmed PDF "
                "candidate(s) added to the registry as proposals (status=needs_mapping)."
            )
        return out, warnings

    def _accepted_document_open_questions(self) -> tuple[list[str], list[str]]:
        """Merge human-confirmed PDF open-question candidates into open_questions.md.

        GOVERNED: only candidates a human accepted via `apply-document-candidate`
        are consumed (never raw-extracted text). Each accepted
        `open_question_candidate` (prose rule / SLA / policy text) becomes an
        appended open question carrying its source-document + page provenance, so
        the rule is surfaced for stakeholder clarification rather than silently
        dropped. Cheap append only -- never mutates a contract.
        """
        warnings: list[str] = []
        try:
            from core.onboarding.documents.candidate_apply import merge_accepted_candidates
        except Exception:  # pragma: no cover - import guard
            return [], warnings
        try:
            merged = merge_accepted_candidates(self.layout)
        except Exception as exc:
            return [], [
                f"[~] accepted_document_open_question_merge_failed:"
                f"{type(exc).__name__}:{exc}"
            ]

        accepted = merged.get("open_question_candidates") or []
        if not accepted:
            return [], warnings

        questions: list[str] = []
        seen: set[str] = set()
        for entry in accepted:
            content = entry.get("content") or {}
            snippet = str(content.get("text_snippet") or "").strip()
            if not snippet:
                continue
            key = snippet.lower()
            if key in seen:
                continue
            seen.add(key)
            src = str(entry.get("source_document") or "").strip()
            page = entry.get("page")
            if src:
                loc = f" (source: {src}" + (f", p.{page}" if page is not None else "") + ")"
            else:
                loc = ""
            questions.append(f"{snippet}{loc}")
        if questions:
            warnings.append(
                f"[~] document_open_questions_merged:{len(questions)} business-rule/SLA "
                "prose item(s) from human-confirmed PDF candidate(s) appended to open_questions.md."
            )
        return questions, warnings

    def _accepted_document_relationship_notes(self) -> tuple[list[str], list[str]]:
        """Surface human-confirmed PDF data-model candidates as NON-EXECUTABLE notes.

        GOVERNED + NON-EXECUTABLE: a `data_model_candidate` (proposed ERD / FK /
        relationship extracted from a document) is NEVER auto-promoted into
        `relationship_contracts.json` and is NEVER executable from document
        evidence alone (BUG-004/023 discipline -- profile RI proof is still
        required via `build-relationship-contracts`). This consumption only
        SURFACES the proposal as an open-question note so a reviewer can prove it
        against profiles before any join is executed.
        """
        warnings: list[str] = []
        try:
            from core.onboarding.documents.candidate_apply import merge_accepted_candidates
        except Exception:  # pragma: no cover - import guard
            return [], warnings
        try:
            merged = merge_accepted_candidates(self.layout)
        except Exception as exc:
            return [], [
                f"[~] accepted_document_relationship_merge_failed:"
                f"{type(exc).__name__}:{exc}"
            ]

        accepted = merged.get("data_model_candidates") or []
        if not accepted:
            return [], warnings

        notes: list[str] = []
        seen: set[str] = set()
        for entry in accepted:
            content = entry.get("content") or {}
            snippet = str(content.get("text_snippet") or "").strip()
            if not snippet:
                headers = content.get("headers") or []
                signals = content.get("detected_signals") or []
                if headers or signals:
                    snippet = f"relationship table headers={headers} signals={signals}"
            if not snippet:
                continue
            key = snippet.lower()
            if key in seen:
                continue
            seen.add(key)
            src = str(entry.get("source_document") or "").strip()
            page = entry.get("page")
            if src:
                loc = f" (source: {src}" + (f", p.{page}" if page is not None else "") + ")"
            else:
                loc = ""
            notes.append(
                f"[NON-EXECUTABLE] proposed relationship from document: {snippet}{loc} "
                "-- requires profile RI proof via build-relationship-contracts before any join."
            )
        if notes:
            warnings.append(
                f"[~] document_relationships_surfaced:{len(notes)} NON-EXECUTABLE relationship "
                "proposal(s) from human-confirmed PDF candidate(s) surfaced in open_questions.md "
                "(profile RI proof still required)."
            )
        return notes, warnings

    def _run_locked(self) -> OnboardingResult:
        inputs = self.discover_inputs()

        # Incremental planning: fingerprint every input (datasets, KPI
        # registries, data-model files, durable decision stores) and compare
        # against the manifest from the previous run. Unchanged datasets keep
        # their profile artifacts; aggregate contracts rebuild when ANY input
        # changed; a fully unchanged workspace replays the recorded result
        # without touching a single artifact. --force restores the legacy
        # clear-everything behavior.
        manifest = None if self.force else load_manifest(self.layout.state_dir)
        previous_inputs = (manifest or {}).get("inputs") or {}
        current_inputs = {
            "data_files": fingerprint_inputs(
                self.repo_root, inputs.data_files, previous_inputs.get("data_files")
            ),
            "registry_files": fingerprint_inputs(
                self.repo_root, inputs.kpi_registries, previous_inputs.get("registry_files")
            ),
            "model_files": fingerprint_inputs(
                self.repo_root, inputs.data_models, previous_inputs.get("model_files")
            ),
            "decision_files": fingerprint_inputs(
                self.repo_root, self._decision_input_files(), previous_inputs.get("decision_files")
            ),
        }
        data_changes = diff_fingerprints(
            previous_inputs.get("data_files"), current_inputs["data_files"]
        )
        non_data_unchanged = all(
            diff_fingerprints(previous_inputs.get(cat), current_inputs[cat]).nothing_changed
            for cat in ("registry_files", "model_files", "decision_files")
        )
        if (
            manifest is not None
            and data_changes.nothing_changed
            and non_data_unchanged
            and artifacts_exist(self.repo_root, manifest)
        ):
            return self._replay_unchanged_result(manifest, inputs, data_changes)

        reusable_profiles: dict[str, dict[str, Any]] = {}
        removed_warnings: list[str] = []
        if manifest is None:
            run_mode = "full"
            self._clear_onboarding_artifacts()
        else:
            run_mode = "incremental"
            reusable_profiles = self._reusable_profile_summaries(
                manifest, data_changes.unchanged
            )
            self._clear_onboarding_artifacts(
                keep_profile_paths={
                    str(summary.get("profile_path") or "")
                    for summary in reusable_profiles.values()
                },
                clear_metadata_store=False,
            )
            removed_warnings = self._drop_removed_dataset_artifacts(
                manifest, data_changes.removed
            )

        estimated_profile_bytes = _total_existing_bytes(inputs.data_files, self.repo_root)
        resource_manager = ResourceManager(self.workspace, repo_root=self.repo_root)
        resource_artifacts = resource_manager.write_report(
            estimated_bytes=estimated_profile_bytes,
            workload="profile",
        )
        profiling_settings = resource_manager.profiling_settings(
            requested_sample_rows=self.sample_rows,
            requested_exact=self.exact_profile,
            estimated_bytes=estimated_profile_bytes,
        )
        kpis, kpi_warnings = self.load_kpis(inputs.kpi_registries)
        # Consume human-confirmed PDF KPI candidates (closes the PDF->registry loop).
        document_kpis, document_kpi_warnings = self._accepted_document_kpis(kpis)
        if document_kpis:
            kpis = list(kpis) + document_kpis
        kpi_warnings = kpi_warnings + document_kpi_warnings
        # Consume human-confirmed PDF prose-rule/SLA candidates into open_questions.md.
        document_open_questions, document_oq_warnings = self._accepted_document_open_questions()
        kpi_warnings = kpi_warnings + document_oq_warnings
        # Surface human-confirmed PDF data-model candidates as NON-EXECUTABLE notes.
        document_dm_notes, document_dm_warnings = self._accepted_document_relationship_notes()
        kpi_warnings = kpi_warnings + document_dm_warnings
        # Per-dataset skip/redo: reuse the recorded summary (and the on-disk
        # *.profile.json) for content-unchanged datasets; profile only the
        # changed/new ones. Output order stays inputs.data_files order (sorted)
        # so artifacts are byte-identical regardless of which subset re-ran.
        profiles: list[dict[str, Any]] = []
        profile_warnings: list[str] = list(removed_warnings)
        profile_map: dict[str, str] = {}
        profiled_count = 0
        for data_file in inputs.data_files:
            reused = reusable_profiles.get(data_file)
            if reused is not None:
                profiles.append(reused)
                profile_map[data_file] = str(reused.get("profile_path") or "")
                continue
            file_profiles, file_warnings = self.profile_inputs(
                [data_file],
                sample_rows=profiling_settings.sample_rows,
                exact_profile=profiling_settings.exact_profile,
                expensive_checks=profiling_settings.expensive_checks,
                resource_mode=profiling_settings.mode,
            )
            profile_warnings.extend(file_warnings)
            if file_profiles:
                profiled_count += 1
                profiles.extend(file_profiles)
                profile_map[data_file] = str(file_profiles[0].get("profile_path") or "")
        # Databricks-sourced tables (workspace_settings.json "databricks_source"),
        # generic and additive -- empty for every workspace that hasn't declared
        # one, so this changes nothing for workspaces profiling local files.
        if inputs.databricks_tables:
            db_profiles, db_warnings = self.profile_databricks_tables(inputs.databricks_tables)
            profile_warnings.extend(db_warnings)
            if db_profiles:
                profiled_count += len(db_profiles)
                profiles.extend(db_profiles)
        reused_count = len(reusable_profiles)
        # Extract dictionary text from PDF/DOCX data-model files. The text is
        # downstream evidence for the CLI-agent proposal panel; failures
        # (missing pdfplumber/docx) are warnings, not errors.
        data_dictionary_documents, dictionary_warnings = self._extract_data_model_documents(
            inputs.data_models
        )
        if data_dictionary_documents:
            self._write_json(
                self.layout.generated_dir / "data_dictionary" / "index.json",
                {
                    "artifact_type": "data_dictionary_index.json",
                    "version": 1,
                    "generated_by": "onboard-workspace",
                    "workspace": _rel(self.workspace, self.repo_root),
                    "documents": data_dictionary_documents,
                },
            )

        # Two-phase write so the workspace lexicon can be derived from this
        # workspace's own evidence rather than a curated dictionary.
        #   1. write input_inventory, profile_index, and a draft kpi_registry
        #      containing only the authored cells from the registry files;
        #   2. build the workspace lexicon (reads profile_index + kpi_registry +
        #      any previously accepted feature definitions/mappings);
        #   3. fill empty metric/cuts cells from the lexicon;
        #   4. rewrite kpi_registry with the filled values, then write the
        #      remaining contracts (semantic, baseline_sql, open_questions,
        #      etc.) using those filled KPIs.
        input_inventory_path = self._write_json(
            self.layout.requirements_dir / "input_inventory.json",
            asdict(inputs),
        )
        profile_index = self._write_json(
            self.layout.profiles_dir / "profile_index.json",
            {
                "artifact_type": "profile_index.json",
                "version": 1,
                "generated_by": "onboard-workspace",
                "workspace": _rel(self.workspace, self.repo_root),
                "profiles": profiles,
                "resource_profile_settings": profiling_settings.to_dict(),
            },
        )
        # Parse data-model images (e.g. DataModel.png) into review-gated sidecars.
        # Must run AFTER profile_index.json is persisted to disk: the parser's
        # profile matcher reads profile_index.json to resolve diagram FK endpoints
        # (Fact/Dim_*) to real dataset/column names. Running before the write left
        # the matcher in `profile_index_missing` state, so no edges resolved.
        # LOCAL-SAFE ONLY (no remote vision / upload); degrades with a [~] warning
        # if Tesseract is absent.
        diagram_artifacts, diagram_warnings = self._parse_data_model_images()
        # Local-safe PDF document ingestion (opendataloader free mode). No-op when
        # no PDFs are present; degrades gracefully if the extractor/Java is absent.
        document_artifacts, document_warnings = self._scan_documents()
        kpi_registry_payload = {
            "artifact_type": "kpi_registry.json",
            "version": 1,
            "generated_by": "onboard-workspace",
            "workspace": _rel(self.workspace, self.repo_root),
            "source_registries": inputs.kpi_registries,
            "kpis": [asdict(kpi) for kpi in kpis],
        }
        self._write_json(
            self.layout.contracts_dir / "kpi_registry.json",
            kpi_registry_payload,
        )

        # Human-confirmed KPI definitions are authoritative and applied FIRST, so
        # they override derived/lexicon guesses and survive every re-onboard.
        kpis = self._apply_accepted_kpi_definitions(kpis)
        lexicon = build_workspace_lexicon(self.layout, self.repo_root)
        kpis = self._fill_kpi_gaps_with_lexicon(kpis, lexicon)
        # Evidence-based derivation for cells the lexicon could not fill. This
        # recovers natural-language-question workspaces (empty metric/cuts) by
        # deriving a measurable metric/grain from the workspace's own profiled
        # columns, exactly as the KPI-generation interview does. Idempotent and
        # workspace-agnostic; low-confidence/ambiguous cases stay empty so the
        # definition-blocker gate still asks rather than fabricating.
        kpis = self._fill_kpi_gaps_with_derivation(kpis, profiles)
        kpi_registry_payload["kpis"] = [asdict(kpi) for kpi in kpis]

        artifacts = {
            "input_inventory": input_inventory_path,
            "kpi_registry": self._write_json(
                self.layout.contracts_dir / "kpi_registry.json",
                kpi_registry_payload,
            ),
            "workspace_lexicon": str(
                self.layout.contracts_dir / "workspace_lexicon.json"
            ),
            "domain_model": self._write_json(
                self.layout.contracts_dir / "domain_model.json",
                self._build_domain_model(inputs, profiles),
            ),
            "semantic_contract": self._write_json(
                self.layout.contracts_dir / "semantic_contract.json",
                self._build_semantic_contract(kpis, inputs),
            ),
            "open_questions": self._write_open_questions(
                kpis,
                kpi_warnings + profile_warnings,
                document_open_questions,
                document_dm_notes,
            ),
            "stakeholder_interview": self._write_stakeholder_interview(inputs, kpis),
            "baseline_sql": self._write_baseline_sql(kpis),
            "experiment": self._write_experiment_script(),
            "evaluator": self._write_evaluator_script(),
            "onboarding_report": self._write_report(inputs, kpis, profiles),
            "profile_index": profile_index,
            **resource_artifacts,
            **diagram_artifacts,
            **document_artifacts,
        }
        artifacts["generated_file_readability"] = self._write_generated_file_readability()

        # Exclusive-mode guards: the single most important check in this
        # phase. A workspace declaring databricks_exclusive with zero UC
        # tables actually discovered must NOT proceed as if profiling
        # succeeded -- an empty profile_index.json would otherwise silently
        # point KPI resolution at a workspace with no data at all. This is a
        # hard warning (not an exception) so onboarding still completes and
        # writes artifacts a human/agent can inspect to diagnose the
        # connection, matching every other degrade-with-a-warning path here.
        exclusive_mode_warnings: list[str] = []
        exclusive_mode_zero_tables = (
            inputs.databricks_source_mode == "exclusive" and not inputs.databricks_tables
        )
        if exclusive_mode_zero_tables:
            exclusive_mode_warnings.append(
                "[x] exclusive_databricks_mode_zero_tables_discovered: "
                "databricks_source.mode is 'exclusive' but zero Unity Catalog "
                "tables were discovered -- profile_index.json is empty. Fix the "
                "Databricks connection (catalog/schema/credentials) before "
                "resolving KPI features; local datasets/ is intentionally not "
                "scanned in this mode."
            )
        if self._exclusive_mode_stale_local_files:
            exclusive_mode_warnings.append(
                "[~] exclusive_databricks_mode_stale_local_files_present: "
                f"{len(self._exclusive_mode_stale_local_files)} local dataset file(s) "
                "exist on disk but are not scanned in databricks_exclusive mode "
                "(e.g. " + self._exclusive_mode_stale_local_files[0] + "). Remove them "
                "or switch mode via apply-data-source-answer if this is unintended."
            )

        if exclusive_mode_zero_tables:
            next_step = (
                "Fix the Databricks connection for this exclusive-mode workspace "
                "(catalog/schema/credentials) -- zero UC tables were discovered, "
                "so KPI feature resolution has no data to work against."
            )
            next_command = (
                f"uv run apply-data-source-answer --workspace {inputs.workspace} "
                "--answer databricks_exclusive --catalog <catalog> --schema <schema> "
                '--confirmed-by "<name>"'
            )
        else:
            next_step = _onboarding_next_step(inputs, kpis, profiles)
            next_command = _onboarding_next_command(inputs, kpis, profiles)

        result = OnboardingResult(
            workspace=str(self.workspace),
            interns_dir=str(self.layout.interns_dir),
            inputs=inputs,
            kpi_count=len(kpis),
            profile_count=len(profiles),
            artifacts=artifacts,
            next_step=next_step,
            next_command=next_command,
            warnings=kpi_warnings
            + profile_warnings
            + dictionary_warnings
            + diagram_warnings
            + document_warnings
            + exclusive_mode_warnings,
            incremental={
                "mode": run_mode,
                "datasets_total": len(inputs.data_files),
                "datasets_profiled": profiled_count,
                "datasets_reused": reused_count,
                "datasets_removed": len(data_changes.removed),
            },
        )
        write_manifest(
            self.layout.state_dir,
            build_manifest_payload(
                workspace=_rel(self.workspace, self.repo_root),
                inputs=current_inputs,
                profiles=profile_map,
                result_summary=result.summary(),
            ),
        )
        return result

    def discover_inputs(self) -> WorkspaceInputs:
        source_mode = self.layout.databricks_source_mode()
        classified = self._classified_workspace_inputs()
        local_data_files: list[Path] = [
            path
            for path, roles in classified
            if "dataset_evidence" in roles
            and "kpi_input" not in roles
            and "data_model_input" not in roles
            and path.suffix.lower() in DATA_SUFFIXES
            and self.layout.is_dataset_allowed(path)
        ]
        local_data_files.extend(self._external_data_files())
        # "exclusive" mode: the workspace's data lives on Databricks only --
        # local datasets/ is not "your data" for this workspace, so it is
        # never scanned. kpi_registries/data_models are business-definition
        # inputs (what the KPIs mean), not data itself, so they are always
        # discovered regardless of source mode. A local file physically
        # present despite exclusive mode is surfaced as a non-blocking
        # warning by _run_locked() (self._exclusive_mode_stale_local_files),
        # not silently dropped -- a stale/misconfigured workspace should be
        # visible, not invisible.
        if source_mode == "exclusive":
            self._exclusive_mode_stale_local_files = sorted(
                {_rel(path, self.repo_root) for path in local_data_files}
            )
            data_files: list[Path] = []
        else:
            self._exclusive_mode_stale_local_files = []
            data_files = local_data_files

        kpi_registries = [
            path
            for path, roles in classified
            if "kpi_input" in roles
            and path.suffix.lower() in REGISTRY_SUFFIXES
            and not self._is_platform_governance_note(path)
        ]
        data_models = [
            path
            for path, roles in classified
            if "data_model_input" in roles
            and path.suffix.lower() in MODEL_SUFFIXES
            and not self._is_platform_governance_note(path)
        ]

        return WorkspaceInputs(
            workspace=_rel(self.workspace, self.repo_root),
            data_files=[_rel(path, self.repo_root) for path in sorted(set(data_files))],
            kpi_registries=[_rel(path, self.repo_root) for path in sorted(set(kpi_registries))],
            data_models=[_rel(path, self.repo_root) for path in sorted(set(data_models))],
            databricks_tables=self._databricks_source_tables(),
            databricks_source_mode=source_mode,
        )

    def _databricks_source_tables(self) -> list[str]:
        """Fully-qualified tables to profile via the SQL warehouse instead of
        local files, per workspace_settings.json's "databricks_source":
        ``{"catalog": "...", "schema": "..."}``.

        Fully generic: no workspace name, catalog name, or table name is ever
        hardcoded here. A workspace that doesn't declare this key gets an
        empty list and local-file discovery above is entirely unaffected --
        this is additive, not a replacement for any existing workspace.
        """
        source = (self.layout.load_settings() or {}).get("databricks_source")
        if not isinstance(source, dict):
            return []
        catalog = str(source.get("catalog") or "").strip()
        schema = str(source.get("schema") or "").strip()
        if not catalog or not schema:
            return []
        try:
            from core.config import resolve_databricks_config
            from core.execution.databricks_client import DatabricksClient

            # Per-enterprise credential/catalog resolution (dbt+Airflow plan
            # section 9): this workspace's declared enterprise_id (or its
            # catalog as a fallback) picks its own config override when one
            # exists, else the global config exactly as before.
            db_cfg = resolve_databricks_config(self.layout.enterprise_id())
            client = DatabricksClient(db_cfg)
            if not client.is_configured():
                return []
            _, rows = client.execute_query(f"SHOW TABLES IN `{catalog}`.`{schema}`")
            # SHOW TABLES columns: database, tableName, isTemporary (order/
            # names are stable Spark SQL output, not Databricks-specific).
            return sorted(f"{catalog}.{schema}.{row[1]}" for row in rows)
        except Exception:
            # Table discovery must never break onboarding for a workspace
            # that declared a source but has no reachable warehouse right
            # now -- surfaces as an empty list, same as not declaring one.
            return []

    def _is_platform_governance_note(self, path: Path) -> bool:
        """Whether a file is a platform-generated governance note, not input.

        The workspace ``wiki/`` tree is written by the platform itself (feature
        decisions, KPI notes). Re-ingesting those records as KPI/data-model
        INPUTS is circular: a wiki feature note resurfaced as a phantom KPI on
        re-onboarding. Generic location rule -- no filename vocabulary.
        """
        try:
            relative = path.resolve().relative_to(self.workspace)
        except ValueError:
            return False
        return bool(relative.parts) and relative.parts[0] == "wiki"

    def _classified_workspace_inputs(self) -> list[tuple[Path, set[str]]]:
        from tools.list_workspace_files import list_workspace_files

        listing = list_workspace_files(self.repo_root, _rel(self.workspace, self.repo_root))
        classified: list[tuple[Path, set[str]]] = []
        for item in listing.classifications:
            raw_path = item.get("path")
            if not isinstance(raw_path, str):
                continue
            path = (self.repo_root / raw_path).resolve()
            if not path.exists() or not path.is_file():
                continue
            roles = {str(role) for role in item.get("roles") or []}
            if roles:
                classified.append((path, roles))
        return classified

    def load_kpis(self, registry_paths: list[str]) -> tuple[list[KpiDefinition], list[str]]:
        kpis: list[KpiDefinition] = []
        warnings: list[str] = []
        detections: list[tuple[KpiFormatDetection, list[dict[str, Any]]]] = []
        for registry in registry_paths:
            path = self.repo_root / registry
            rel = _rel(path, self.repo_root)
            try:
                if path.suffix.lower() in {".xlsx", ".xlsm"}:
                    detection, file_kpis = _read_excel_kpis_with_detection(path)
                    kpis.extend(file_kpis)
                    if detection is not None:
                        detections.append((detection, _sample_rows_for(path)))
                elif path.suffix.lower() == ".csv" and pl:
                    frame = pl.read_csv(path)
                    detection, file_kpis = _read_frame_with_detection(frame, rel)
                    kpis.extend(file_kpis)
                    if detection is not None:
                        detections.append((detection, list(frame.head(2).iter_rows(named=True))))
                elif path.suffix.lower() == ".json":
                    kpis.extend(_read_json_kpis(path, self.repo_root))
                elif path.suffix.lower() == ".md":
                    kpis.extend(_read_markdown_kpis(path, self.repo_root))
                elif path.suffix.lower() == ".sql":
                    kpis.extend(_read_sql_comment_kpis(path, self.repo_root))
                else:
                    warnings.append(f"unsupported_registry_format:{rel}")
            except Exception as exc:
                warnings.append(
                    f"kpi_registry_read_failed:{rel}:{type(exc).__name__}:{exc}"
                )
        self._write_kpi_format_confirmation(detections)
        kpis, dedupe_warnings = _dedupe_kpis_by_name(kpis)
        return kpis, warnings + dedupe_warnings

    def _write_kpi_format_confirmation(
        self,
        detections: list[tuple[KpiFormatDetection, list[dict[str, Any]]]],
    ) -> str:
        """Write the KPI-file format confirmation card (the detected column->role
        mapping, a real-row read-back, and any nesting/low-confidence flags) so the
        user can verify the interpretation before it is trusted. One panel per
        detected tabular KPI file; nothing here commits a mapping."""
        if not detections:
            return ""
        panels = [
            build_kpi_confirmation_panel(detection, samples)
            for detection, samples in detections
        ]
        out_dir = self.layout.reports_dir / "kpi_format"
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "artifact_type": "kpi_format_confirmation",
            "needs_user_confirmation": any(
                p["summary"]["needs_user_confirmation"] for p in panels
            ),
            "panels": panels,
        }
        (out_dir / "current.json").write_text(
            json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
        )
        markdown = "\n\n---\n\n".join(render_kpi_confirmation_markdown(p) for p in panels)
        (out_dir / "current.md").write_text(markdown + "\n", encoding="utf-8")
        return _rel(out_dir / "current.json", self.repo_root)

    def _fill_kpi_gaps_with_lexicon(
        self,
        kpis: list[KpiDefinition],
        lexicon: WorkspaceLexicon | None,
    ) -> list[KpiDefinition]:
        """Apply lexicon inference only to KPI rows whose cells were left empty.

        Authored cells are preserved exactly. When the lexicon proposes a metric
        for a KPI whose ``metric`` cell is empty, fill it. Same for ``cuts``.
        An empty or absent lexicon produces no changes; sparse workspaces
        correctly leave KPIs in ``needs_mapping`` rather than confidently
        inferring from nothing.
        """
        if lexicon is None or lexicon.is_empty():
            return list(kpis)
        filled: list[KpiDefinition] = []
        for kpi in kpis:
            metric = kpi.metric
            cuts = kpi.cuts
            if metric and cuts:
                filled.append(kpi)
                continue
            inferred_metric, inferred_cuts = lexicon.infer_metric_and_cuts(
                kpi.name, kpi.description
            )
            changes: dict[str, Any] = {}
            if not metric and inferred_metric:
                changes["metric"] = inferred_metric
                changes["metric_provenance"] = "lexicon_inferred"
            if not cuts and inferred_cuts:
                changes["cuts"] = inferred_cuts
                changes["cuts_provenance"] = "lexicon_inferred"
            filled.append(replace(kpi, **changes) if changes else kpi)
        return filled

    def _apply_accepted_kpi_definitions(
        self, kpis: list[KpiDefinition]
    ) -> list[KpiDefinition]:
        """Apply human-confirmed KPI definitions (metric/grain) from the decision
        store. Authoritative: overrides empty/derived cells and survives
        re-onboarding. No-op when no definitions have been accepted."""
        try:
            from core.onboarding.kpi.kpi_definition import (
                apply_accepted_definitions_to_kpis,
                load_kpi_definition_store,
            )
        except Exception:  # pragma: no cover - defensive import guard
            return list(kpis)
        store = load_kpi_definition_store(self.layout)
        if not store:
            return list(kpis)
        return apply_accepted_definitions_to_kpis(kpis, store)

    def _load_column_glosses(self) -> list[dict[str, str]]:
        """Discover column descriptions from the workspace's data-model evidence.

        Returns ``[{"column": <field>, "description": <gloss>}]`` aggregated from
        any data-dictionary-style file the workspace carries (a ``*dictionary*``
        CSV with field/description columns, or the onboarding-parsed
        ``data_dictionary/index.json``). Generic and convention-based: it keys on
        the presence of field/description structure, never a specific filename or
        domain term. Returns ``[]`` when no such evidence exists, leaving
        derivation profile-only. Failures are swallowed (advisory evidence).
        """
        import csv as _csv

        entries: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def _add(field: str, description: str) -> None:
            field = str(field or "").strip()
            description = str(description or "").strip()
            if not field or not description:
                return
            key = (field.lower(), description.lower())
            if key in seen:
                return
            seen.add(key)
            entries.append({"column": field, "description": description})

        try:
            for path in sorted(self.workspace.rglob("*dictionary*.csv")):
                if not path.is_file() or "/interns/" in path.as_posix():
                    continue
                try:
                    with path.open("r", encoding="utf-8-sig", newline="") as handle:
                        reader = _csv.DictReader(handle)
                        names = {str(n).strip().lower(): n for n in (reader.fieldnames or [])}
                        field_key = next((names[k] for k in ("field", "column", "name") if k in names), None)
                        desc_key = next((names[k] for k in ("description", "definition", "meaning") if k in names), None)
                        if not field_key or not desc_key:
                            continue
                        for row in reader:
                            _add(row.get(field_key, ""), row.get(desc_key, ""))
                except OSError:
                    continue
        except OSError:
            pass

        index_path = self.layout.generated_dir / "data_dictionary" / "index.json"
        if index_path.exists():
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
                for doc in payload.get("documents") or []:
                    for entry in (doc.get("entries") if isinstance(doc, dict) else None) or []:
                        if isinstance(entry, dict):
                            _add(
                                entry.get("field") or entry.get("column") or "",
                                entry.get("description") or entry.get("definition") or "",
                            )
            except (json.JSONDecodeError, OSError):
                pass
        return entries

    def _fill_kpi_gaps_with_derivation(
        self,
        kpis: list[KpiDefinition],
        profiles: list[dict[str, Any]],
    ) -> list[KpiDefinition]:
        """Derive a measurable metric/grain for KPIs still missing one.

        Mirrors the KPI-generation interview's derivation (same module, same
        confidence threshold) so a workspace onboarded directly off a
        natural-language-question registry reaches the same populated state as
        one taken through the interview. Only empty cells are touched; a cell is
        filled only when the derived facet clears the confidence threshold, so
        ambiguous metrics (share %, top-N) stay empty for the blocker gate to
        ask. Generic: column choices come from this workspace's profiles.
        """
        try:
            from core.onboarding.kpi.metric_derivation import (
                HIGH_CONFIDENCE_THRESHOLD,
                columns_from_profile_index,
                derive_metric_and_cuts,
            )
        except Exception:  # pragma: no cover - defensive import guard
            return list(kpis)
        columns = columns_from_profile_index({"profiles": profiles})
        if not columns:
            return list(kpis)
        # Ground the measure choice in the data model's own column descriptions,
        # not column-name similarity alone (AGENTS.md "Data Model Driven
        # Generation Rule"). Glosses are discovered from the workspace's
        # data-dictionary evidence; absent dictionaries leave derivation
        # profile-only. Generic: no specific file or column is assumed.
        dictionary_entries = self._load_column_glosses()
        filled: list[KpiDefinition] = []
        for kpi in kpis:
            metric = kpi.metric
            cuts = kpi.cuts
            question = str(kpi.name or "").strip()
            if (metric and cuts) or not question:
                filled.append(kpi)
                continue
            try:
                derivation = derive_metric_and_cuts(
                    question, columns, dictionary_entries=dictionary_entries
                )
            except Exception:  # pragma: no cover - derivation must never break onboarding
                filled.append(kpi)
                continue
            changes: dict[str, Any] = {}
            if not str(metric).strip() and (
                derivation["metric"]["confidence"] >= HIGH_CONFIDENCE_THRESHOLD
            ):
                changes["metric"] = derivation["metric"]["value"]
                changes["metric_provenance"] = "derived_from_question"
            if not str(cuts).strip() and (
                derivation["cuts"]["confidence"] >= HIGH_CONFIDENCE_THRESHOLD
            ):
                changes["cuts"] = derivation["cuts"]["value"]
                changes["cuts_provenance"] = "derived_from_question"
            filled.append(replace(kpi, **changes) if changes else kpi)
        return filled

    def profile_inputs(
        self,
        data_files: list[str],
        *,
        sample_rows: int | None = None,
        exact_profile: bool | None = None,
        expensive_checks: bool = True,
        resource_mode: str = "local_standard",
    ) -> tuple[list[dict[str, Any]], list[str]]:
        profiles: list[dict[str, Any]] = []
        warnings: list[str] = []
        effective_sample_rows = self.sample_rows if sample_rows is None else sample_rows
        effective_exact = self.exact_profile if exact_profile is None else exact_profile
        for file in data_files:
            path = self.repo_root / file
            try:
                profile = self.profiler.profile_path(
                    path,
                    sample_rows=effective_sample_rows,
                    exact=effective_exact,
                )
                profile_path = self.layout.profiles_dir / f"{_safe_stem(path, self.workspace)}.profile.json"
                profile_path.write_text(profile.to_json(), encoding="utf-8")
                summary = profile.summary()
                summary["profile_path"] = _rel(profile_path, self.repo_root)
                summary["resource_mode"] = resource_mode
                summary["resource_sample_rows"] = effective_sample_rows
                summary["resource_exact_profile"] = effective_exact
                summary["resource_expensive_checks"] = expensive_checks
                if not expensive_checks:
                    summary.setdefault("warnings", []).append("resource_expensive_checks_skipped")
                profiles.append(summary)
                self._store_metadata(
                    "profiles",
                    _safe_stem(path, self.workspace),
                    summary,
                )
            except Exception as exc:
                warnings.append(f"profile_failed:{_rel(path, self.repo_root)}:{type(exc).__name__}:{exc}")
        return profiles, warnings

    def profile_databricks_tables(
        self, table_fqns: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Profile fully-qualified "catalog.schema.table" strings via the SQL
        warehouse (core.profiling.databricks_table_profiler.profile_uc_table),
        the same DatasetProfile shape profile_inputs() produces for local
        files -- so downstream consumers of profile_index.json don't need to
        know or care which profiler produced a given entry. No incremental
        fingerprint/reuse caching here yet (unlike the local-file loop) --
        a reasonable future optimization, not required for correctness now.
        """
        profiles: list[dict[str, Any]] = []
        warnings: list[str] = []
        if not table_fqns:
            return profiles, warnings
        from core.config import resolve_databricks_config
        from core.execution.databricks_client import DatabricksClient
        from core.profiling.databricks_table_profiler import profile_uc_table

        # Per-enterprise credential/catalog resolution (dbt+Airflow plan
        # section 9) -- same seam as _databricks_source_tables() above.
        db_cfg = resolve_databricks_config(self.layout.enterprise_id())
        client = DatabricksClient(db_cfg)
        for fqn in table_fqns:
            try:
                catalog, schema, table = fqn.split(".", 2)
            except ValueError:
                warnings.append(f"malformed_databricks_table_fqn:{fqn}")
                continue
            try:
                profile = profile_uc_table(client, catalog, schema, table)
                stem = f"{catalog}__{schema}__{table}"
                self.layout.profiles_dir.mkdir(parents=True, exist_ok=True)
                profile_path = self.layout.profiles_dir / f"{stem}.profile.json"
                profile_path.write_text(profile.to_json(), encoding="utf-8")
                summary = profile.summary()
                summary["profile_path"] = _rel(profile_path, self.repo_root)
                summary["resource_mode"] = "databricks_sql_warehouse"
                profiles.append(summary)
                self._store_metadata("profiles", stem, summary)
            except Exception as exc:
                warnings.append(f"databricks_profile_failed:{fqn}:{type(exc).__name__}:{exc}")
        return profiles, warnings

    def _build_domain_model(
        self,
        inputs: WorkspaceInputs,
        profiles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "artifact_type": "domain_model.json",
            "version": 1,
            "generated_by": "onboard-workspace",
            "workspace": inputs.workspace,
            "data_models": inputs.data_models,
            "datasets": [
                {
                    "path": profile["path"],
                    "format": profile["format"],
                    "row_count": profile["row_count"],
                    "schema": profile["schema"],
                    "profile_path": profile.get("profile_path"),
                }
                for profile in profiles
            ],
            "status": "generated_from_workspace_inputs",
        }

    def _sensitive_columns_section(self) -> dict[str, Any]:
        """The semantic contract's ``columns`` map + data-policy summary.

        ``columns.<name>.is_sensitive`` is what the SQL generator consults to
        mask sensitive output columns. Sources, in order: the user-authored
        workspace ``data_policy.json`` (declared sensitive + allowlist), then
        built-in HIPAA/PCI identifier detection over the profiled schema.
        """
        from core.governance.data_policy import (
            is_allowlisted,
            load_workspace_data_policy,
            policy_category_for_column,
        )
        from core.governance.phi_gate import identifier_category, pci_identifier_category

        policy = load_workspace_data_policy(self.workspace)
        columns: dict[str, dict[str, Any]] = {}
        try:
            profile_index = json.loads(
                (self.layout.profiles_dir / "profile_index.json").read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            profile_index = {}
        allowlisted: set[str] = set()
        for profile in profile_index.get("profiles") or []:
            if not isinstance(profile, dict):
                continue
            # Table identity disambiguates a bare ``name`` column (PHI only on a
            # person entity); see phi_gate.identifier_category.
            table = profile.get("path") or profile.get("dataset")
            schema = profile.get("schema")
            names = (
                list(schema.keys())
                if isinstance(schema, dict)
                else [c.get("name") for c in schema or [] if isinstance(c, dict)]
            )
            for name in names:
                if not isinstance(name, str):
                    continue
                if name in allowlisted:
                    continue
                if is_allowlisted(policy, name):
                    columns[name] = {
                        "is_sensitive": False,
                        "source": "workspace_data_policy_allowlist",
                    }
                    allowlisted.add(name)
                    continue
                # A column already marked sensitive on another table stays
                # sensitive — never downgrade it from a later non-person table.
                if columns.get(name, {}).get("is_sensitive"):
                    continue
                category = (
                    policy_category_for_column(policy, name)
                    or identifier_category(name, table=table)
                    or pci_identifier_category(name)
                )
                if category:
                    columns[name] = {"is_sensitive": True, "category": category}
        section: dict[str, Any] = {"columns": columns}
        if policy is not None:
            section["data_policy"] = policy.summary()
        return section

    def _build_semantic_contract(
        self,
        kpis: list[KpiDefinition],
        inputs: WorkspaceInputs,
    ) -> dict[str, Any]:
        return {
            **self._sensitive_columns_section(),
            "workspace": inputs.workspace,
            "kpi_count": len(kpis),
            "term_resolution_order": [
                "kpi_registry",
                "data_model_docs_or_diagrams",
                "dataset_schema_profile_evidence",
                "data_dictionary_or_metadata_files",
                "catalog_metadata_if_connected",
                "stakeholder_or_user_clarification",
            ],
            "rules": [
                {
                    "id": f"kpi_{idx:03d}",
                    "name": kpi.name,
                    "metric": kpi.metric,
                    "grain_or_cuts": kpi.cuts,
                    "status": kpi.status,
                    "refinement_required": kpi.refinement_required,
                }
                for idx, kpi in enumerate(kpis, start=1)
            ],
            "guardrails": [
                "preserve_kpi_semantics_before_runtime_optimization",
                "record_assumptions_for_ambiguous_or_missing_fields",
                "mark_unmapped_kpis_as_needs_review",
                "ask_for_missing_dictionary_metadata_catalog_contract_or_sla_files_when_required",
            ],
        }

    def _write_open_questions(
        self,
        kpis: list[KpiDefinition],
        warnings: list[str],
        document_questions: list[str] | None = None,
        data_model_notes: list[str] | None = None,
    ) -> str:
        lines = [
            "# Open Questions",
            "",
            "These questions were generated during workspace onboarding.",
            "",
            "Before asking the user, resolve KPI terms from the KPI registry, data model,",
            "dataset profiles, data dictionaries or metadata files, catalog metadata, then",
            "stakeholder clarification.",
            "",
        ]
        for idx, kpi in enumerate(kpis, start=1):
            if kpi.refinement_required:
                lines.append(f"{idx}. **{kpi.name}**: {kpi.refinement_required}")
        if document_questions:
            lines.extend([
                "",
                "## From confirmed source documents (PDF)",
                "",
                "Human-confirmed business rules / SLAs / policy prose extracted from",
                "uploaded documents. Resolve or fold into KPI definitions as appropriate.",
                "",
            ])
            lines.extend(f"- {q}" for q in document_questions)
        if data_model_notes:
            lines.extend([
                "",
                "## Proposed relationships from documents (NON-EXECUTABLE)",
                "",
                "Human-confirmed ERD/FK proposals from uploaded documents. These are",
                "NOT executable: profile referential-integrity proof is still required",
                "(run build-relationship-contracts) before any join is generated.",
                "",
            ])
            lines.extend(f"- {n}" for n in data_model_notes)
        if warnings:
            lines.extend(["", "## Warnings", ""])
            lines.extend(f"- {warning}" for warning in warnings)
        path = self.layout.reports_dir / "open_questions.md"
        return self._write_text(path, "\n".join(lines).rstrip() + "\n")

    def _write_stakeholder_interview(
        self,
        inputs: WorkspaceInputs,
        kpis: list[KpiDefinition],
    ) -> str:
        from pathlib import Path as _Path
        lines = [
            "# Stakeholder Interview Summary",
            "",
            f"- **Workspace:** `{inputs.workspace}`",
            f"- **KPI registries:** {len(inputs.kpi_registries)}",
            f"- **Data model files:** {len(inputs.data_models)}",
            f"- **Data files:** {len(inputs.data_files)}",
            "",
        ]
        if inputs.data_files:
            lines += ["## Source Files", ""]
            for f in inputs.data_files:
                lines.append(f"- `{_Path(f).name}`")
            lines.append("")
        if kpis:
            lines += ["## KPI Definitions", ""]
            for i, kpi in enumerate(kpis, 1):
                lines.append(f"### {i}. {kpi.name}")
                if kpi.description:
                    lines.append(f"- **Description:** {kpi.description}")
                lines.append(f"- **Metric:** `{kpi.metric}`")
                lines.append(f"- **Cuts:** {kpi.cuts}")
                if kpi.refinement_required:
                    lines.append(f"- **Needs clarification:** {kpi.refinement_required}")
                lines.append("")
        path = self.layout.requirements_dir / "stakeholder_interview.md"
        return self._write_text(path, "\n".join(lines).rstrip() + "\n")

    def _write_baseline_sql(self, kpis: list[KpiDefinition]) -> str:
        values = []
        for idx, kpi in enumerate(kpis, start=1):
            values.append(
                "("
                f"{idx}, "
                f"'{_sql_escape(kpi.name)}', "
                f"'{_sql_escape(kpi.metric)}', "
                f"'{_sql_escape(kpi.cuts)}', "
                f"'{_sql_escape(kpi.status)}'"
                ")"
            )
        if not values:
            values.append("(0, 'No KPI registry found', '', '', 'needs_review')")
        sql = "\n".join([
            "-- Generated baseline KPI manifest.",
            "-- Replace manifest-only rows with executable KPI logic as mappings are approved.",
            "CREATE OR REPLACE TABLE kpi_baseline_manifest AS",
            "SELECT * FROM (VALUES",
            ",\n".join(f"  {value}" for value in values),
            ") AS t(kpi_id, kpi_name, metric_expression, grain_or_cuts, status);",
            "",
        ])
        return self._write_text(self.layout.solutions_dir / "kpi_metrics.sql", sql)

    def _write_experiment_script(self) -> str:
        content = '''from __future__ import annotations

import json
import time
from pathlib import Path

import duckdb

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
INTERNS_ROOT = WORKSPACE_ROOT / "interns"
DB_PATH = INTERNS_ROOT / "state" / "analytics.duckdb"
SQL_FILE = INTERNS_ROOT / "generated" / "solutions" / "kpi_metrics.sql"
RESULT_PATH = INTERNS_ROOT / "runs" / "baseline_result.json"



def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(DB_PATH))
    started = time.perf_counter()
    success = False
    error = ""
    try:
        conn.execute(SQL_FILE.read_text(encoding="utf-8"))
        success = True
    except Exception as exc:
        error = str(exc)
    elapsed = time.perf_counter() - started
    conn.execute("DROP TABLE IF EXISTS sql_execution_time")
    conn.execute(
        "CREATE TABLE sql_execution_time AS SELECT ? AS execution_time_seconds, ? AS success, ? AS error",
        [elapsed, success, error],
    )
    conn.close()
    RESULT_PATH.write_text(
        json.dumps(
            {"execution_time_seconds": elapsed, "success": success, "error": error},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"execution_time_seconds: {elapsed:.4f}")
    print(f"success: {success}")
    if error:
        print(f"error: {error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
'''
        return self._write_text(self.layout.evaluation_dir / "experiment.py", content)

    def _write_evaluator_script(self) -> str:
        content = '''from __future__ import annotations

from pathlib import Path

import duckdb

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = WORKSPACE_ROOT / "interns" / "state" / "analytics.duckdb"


def main() -> None:
    conn = duckdb.connect(str(DB_PATH))
    row = conn.execute("SELECT execution_time_seconds, success FROM sql_execution_time").fetchone()
    manifest_count = conn.execute("SELECT count(*) FROM kpi_baseline_manifest").fetchone()[0]
    ready_count = conn.execute(
        "SELECT count(*) FROM kpi_baseline_manifest WHERE status = 'ready'"
    ).fetchone()[0]
    conn.close()
    execution_time, success = (float(row[0]), bool(row[1])) if row else (100.0, False)
    matching_score = (
        round((ready_count / manifest_count) * 100.0, 4)
        if success and manifest_count > 0
        else 0.0
    )
    time_score = max(0.0, 10.0 - execution_time) / 10.0 * 5.0 if matching_score == 100.0 else 0.0
    primary_metric = round((matching_score / 100.0) * 5.0 + time_score, 4)
    print("---")
    print(f"primary_metric: {primary_metric}")
    print(f"execution_time_seconds: {execution_time:.4f}")
    print(f"matching_score: {matching_score}")
    print(f"kpi_count: {manifest_count}")
    print(f"ready_kpi_count: {ready_count}")
    print("---")


if __name__ == "__main__":
    main()
'''
        return self._write_text(self.layout.evaluation_dir / "evaluator.py", content)

    def _write_report(
        self,
        inputs: WorkspaceInputs,
        kpis: list[KpiDefinition],
        profiles: list[dict[str, Any]],
    ) -> str:
        from pathlib import Path as _Path
        lines = [
            "# Workspace Onboarding Report",
            "",
            f"- **Workspace:** `{inputs.workspace}`",
            f"- **KPIs:** {len(kpis)}",
            f"- **Datasets profiled:** {len(profiles)}",
            "",
        ]

        if profiles:
            lines += ["## Datasets", ""]
            header = "| Dataset | Rows | Columns |"
            sep    = "|---------|------|---------|"
            lines += [header, sep]
            for p in profiles:
                name = _Path(p.get("path", "")).name
                rows = p.get("row_count", "?")
                cols = ", ".join(p.get("schema", {}).keys())
                lines.append(f"| `{name}` | {rows} | {cols} |")
            lines.append("")
            warnings = [w for p in profiles for w in (p.get("warnings") or [])]
            if warnings:
                lines += ["### Data Quality Warnings", ""]
                for w in warnings:
                    lines.append(f"- {w}")
                lines.append("")

        if kpis:
            lines += ["## KPIs", ""]
            header = "| # | Name | Metric | Cuts |"
            sep    = "|---|------|--------|------|"
            lines += [header, sep]
            for i, kpi in enumerate(kpis, 1):
                lines.append(f"| {i} | {kpi.name} | `{kpi.metric}` | {kpi.cuts} |")
            lines.append("")
            unresolved = [kpi for kpi in kpis if kpi.refinement_required]
            if unresolved:
                lines += ["### KPIs Needing Clarification", ""]
                for kpi in unresolved:
                    lines.append(f"- **{kpi.name}:** {kpi.refinement_required}")
                lines.append("")

        if not kpis and profiles:
            lines += [
                "## Next Steps", "",
                "1. Run `uv run build-source-family-contracts --workspace " + inputs.workspace + "`.",
                "2. Review `interns/reports/source_family_contracts.md`.",
                "",
            ]
        else:
            lines += [
                "## Next Steps", "",
                "1. Run `uv run prepare-kpi-blocker-panel --workspace " + inputs.workspace + " --domain <domain>`.",
                "2. If `blocked_kpi_count > 0`, review `interns/reports/blocker_question_panel/current.md`.",
                "3. Run `uv run generate-kpi-sql --workspace " + inputs.workspace + " --kpi-id <id>` per ready KPI.",
                "",
            ]
        return self._write_text(self.layout.reports_dir / "onboarding_report.md", "\n".join(lines))

    def _write_generated_file_readability(self) -> str:
        workspace = _rel(self.workspace, self.repo_root)
        lines = [
            "# Generated File Readability Map",
            "",
            f"This report classifies files for `{workspace}` by whether they are meant for human",
            "review, machine/tool use, or runtime/cache storage. Paths are relative to the project root.",
            "",
            "## Human-Readable Files",
            "",
            "Human-readable files are mainly Markdown reports, SQL, Python scripts, CSV dictionaries,",
            "and source docs.",
            "",
            "| Path | Readable by human? | What it is |",
            "|---|---:|---|",
            f"| `{workspace}/docs/*.md` | Yes | Workspace source documentation, if present |",
            f"| `{workspace}/wiki/features/*.md` | Yes | Feature notes, if present |",
            f"| `{workspace}/interns/generated/solutions/kpi_metrics.sql` | Yes | Baseline KPI SQL manifest/metadata |",
            f"| `{workspace}/interns/generated/solutions/kpi_*.sql` | Yes | Generated KPI queries after SQL generation |",
            f"| `{workspace}/interns/reports/onboarding_report.md` | Yes | Onboarding summary |",
            f"| `{workspace}/interns/reports/open_questions.md` | Yes | Questions/blockers |",
            f"| `{workspace}/interns/reports/relationship_contracts.md` | Yes | Join/relationship proof, when generated |",
            f"| `{workspace}/interns/reports/source_to_target_plan.md` | Yes | KPI-to-source logic plan, when generated |",
            f"| `{workspace}/interns/reports/source_family_contracts.md` | Yes | Source families, schema versions, and drift review for external raw files |",
            f"| `{workspace}/interns/reports/blocker_question_panel/current.md` | Yes | Current blocker question, when generated |",
            f"| `{workspace}/interns/reports/bugs/current.md` | Yes | Bug report, when generated |",
            f"| `{workspace}/interns/reports/context/*.md` | Yes | Routed context summaries, when generated |",
            f"| `{workspace}/interns/reports/data_model_generation/*.md` | Yes | Generated data-model review docs, when generated |",
            f"| `{workspace}/interns/reports/derived_feature_reviews/**/*.md` | Yes | Derived feature review docs, when generated |",
            f"| `{workspace}/interns/reports/kpi_generation/current.md` | Yes | KPI generation/review panel, when generated |",
            f"| `{workspace}/interns/generated/memory/*.md` | Yes | Accepted decisions/history, when generated |",
            f"| `{workspace}/interns/evaluation/evaluator.py` | Mostly | Evaluation code |",
            f"| `{workspace}/interns/evaluation/experiment.py` | Mostly | Experiment runner code |",
            "",
            "## Machine-Readable But Inspectable",
            "",
            "These files are mostly for tools and agents, but a reviewer can inspect them when they",
            "need exact structured evidence.",
            "",
            "| Path | Human-readable? | What it is |",
            "|---|---:|---|",
            f"| `{workspace}/interns/generated/contracts/*.json` | Partly | Core contracts for tools/agents |",
            f"| `{workspace}/interns/generated/profiles/*.profile.json` | Partly | Dataset profiles/statistics |",
            f"| `{workspace}/interns/generated/profiles/profile_index.json` | Partly | Index of profile files and profiled datasets |",
            f"| `{workspace}/interns/generated/requirements/*.json` | Partly | Generated requirement/session state |",
            f"| `{workspace}/interns/generated/context/*.json`, `.jsonl` | Partly | Bounded context index/pages |",
            f"| `{workspace}/interns/reports/*/current.json` | Partly | UI/panel backing data |",
            f"| `{workspace}/interns/generated/evidence/*.json` | Partly | Evidence/debug reports |",
            "",
            "## Runtime And Cache Files",
            "",
            "These files are not normal manual review targets. They support local execution,",
            "metadata storage, or repeatable workflow state.",
            "",
            "| Path | Readable? | What it is |",
            "|---|---:|---|",
            f"| `{workspace}/interns/state/*.duckdb` | No | Local DuckDB databases |",
            f"| `{workspace}/interns/state/*.db` | No | Local SQLite databases |",
            f"| `{workspace}/interns/state/delta_metadata/**/*.parquet` | No | Metadata table data |",
            f"| `{workspace}/interns/state/delta_metadata/**/_delta_log/*.json` | Low | Delta transaction log |",
            f"| `{workspace}/interns/state/metadata_store/**/*.json` | Low | JSON fallback metadata cache |",
            "",
            "## Normal Review Starting Point",
            "",
            "For normal KPI review, start with these files:",
            "",
            "```text",
            f"{workspace}/interns/reports/source_to_target_plan.md",
            f"{workspace}/interns/reports/relationship_contracts.md",
            f"{workspace}/interns/generated/solutions/kpi_001.sql",
            f"{workspace}/interns/reports/open_questions.md",
            "```",
            "",
            "Use the Markdown reports first, then inspect JSON contracts only when you need exact",
            "machine-readable evidence.",
        ]
        return self._write_text(
            self.layout.reports_dir / "generated_file_readability.md",
            "\n".join(lines) + "\n",
        )

    def _write_json(self, path: Path, payload: dict[str, Any]) -> str:
        collection = _metadata_collection_for_path(path)
        if collection:
            self._store_metadata(collection, path.stem, payload)
        return self._write_text(path, json.dumps(payload, indent=2, default=str) + "\n")

    def _write_text(self, path: Path, content: str) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return _rel(path, self.repo_root)

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        policy = load_external_data_policy(self.repo_root)
        if is_external_path(self.workspace, self.repo_root, policy):
            raise ValueError(
                "workspace must be a repo workspace, not an external data root: "
                f"{self.workspace}. Use workspaces/<project> as the workspace and configure "
                "external data through dataset_allowlist."
            )
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _external_data_files(self) -> list[Path]:
        data_files: list[Path] = []
        for allowed in self.layout.external_dataset_allowlist_paths():
            if allowed.is_file() and allowed.suffix.lower() in DATA_SUFFIXES:
                data_files.append(allowed)
            elif allowed.is_dir():
                data_files.extend(
                    path
                    for path in allowed.rglob("*")
                    if path.is_file()
                    and path.suffix.lower() in DATA_SUFFIXES
                    and self.layout.is_dataset_allowed(path)
                )
        return sorted(set(data_files))

    def _decision_input_files(self) -> list[str]:
        """Durable human-decision stores that feed onboarding outputs.

        These survive re-onboarding and are consumed by the KPI gap-fill,
        lexicon build, and document-candidate merge. A change in any of them
        must invalidate the nothing-changed fast path so aggregate contracts
        rebuild with the new decisions. Workspace-agnostic: fixed layout
        locations only, no domain vocabulary.
        """
        candidates = [
            self.layout.generated_dir / "decisions" / "kpi_definitions.json",
            self.layout.contracts_dir / "workspace_feature_definitions.json",
            self.layout.generated_dir / "documents" / "accepted_candidates.json",
            self.layout.kpi_feature_mapping_path,
        ]
        return [
            _rel(path, self.repo_root)
            for path in candidates
            if path.exists() and path.is_file()
        ]

    def _replay_unchanged_result(
        self,
        manifest: dict[str, Any],
        inputs: WorkspaceInputs,
        data_changes: Any,
    ) -> OnboardingResult:
        """Nothing-changed fast path: replay the recorded result verbatim.

        No artifact is rewritten (byte-identical guarantee) and no dataset is
        re-read beyond the fingerprint stat/hash check.
        """
        stored = manifest.get("result") or {}
        warnings = [str(w) for w in (stored.get("warnings") or [])]
        warnings.append(
            f"[~] incremental_skip: {len(data_changes.unchanged)} dataset(s) unchanged; "
            "all onboarding artifacts reused byte-identical. Pass --force to rebuild."
        )
        return OnboardingResult(
            workspace=str(self.workspace),
            interns_dir=str(self.layout.interns_dir),
            inputs=inputs,
            kpi_count=int(stored.get("kpi_count") or 0),
            profile_count=int(stored.get("profile_count") or 0),
            artifacts={
                str(key): str(value)
                for key, value in (stored.get("artifacts") or {}).items()
            },
            next_step=str(stored.get("next_step") or ""),
            next_command=str(stored.get("next_command") or ""),
            warnings=warnings,
            incremental={
                "mode": "skipped",
                "datasets_total": len(inputs.data_files),
                "datasets_profiled": 0,
                "datasets_reused": len(data_changes.unchanged),
                "datasets_removed": 0,
            },
        )

    def _reusable_profile_summaries(
        self,
        manifest: dict[str, Any],
        unchanged: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Map unchanged dataset rel-path -> its recorded profile summary.

        A dataset is reusable only when the manifest recorded a profile path
        for it, the previous profile_index.json still carries its summary, and
        the *.profile.json artifact is still on disk. Anything else re-profiles.
        """
        if not unchanged:
            return {}
        recorded_profiles = manifest.get("profiles") or {}
        index_path = self.layout.profiles_dir / "profile_index.json"
        try:
            index_payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        summaries_by_profile_path: dict[str, dict[str, Any]] = {}
        for summary in index_payload.get("profiles") or []:
            if isinstance(summary, dict) and summary.get("profile_path"):
                summaries_by_profile_path[str(summary["profile_path"])] = summary
        reusable: dict[str, dict[str, Any]] = {}
        for rel in unchanged:
            profile_rel = str(recorded_profiles.get(rel) or "")
            summary = summaries_by_profile_path.get(profile_rel)
            if (
                profile_rel
                and summary is not None
                and (self.repo_root / profile_rel).exists()
            ):
                reusable[rel] = summary
        return reusable

    def _drop_removed_dataset_artifacts(
        self,
        manifest: dict[str, Any],
        removed: list[str],
    ) -> list[str]:
        """Delete profile artifacts for datasets that disappeared and flag
        downstream contracts as potentially stale."""
        warnings: list[str] = []
        recorded_profiles = manifest.get("profiles") or {}
        for rel in removed:
            profile_rel = str(recorded_profiles.get(rel) or "")
            if profile_rel:
                profile_path = self.repo_root / profile_rel
                if profile_path.exists():
                    profile_path.unlink()
            warnings.append(
                f"[~] dataset_removed:{rel}: profile artifact dropped; downstream "
                "contracts built from it (relationships, source-to-target, medallion "
                "plans) may be stale -- re-run their builders."
            )
        return warnings

    def _clear_onboarding_artifacts(
        self,
        *,
        keep_profile_paths: set[str] | None = None,
        clear_metadata_store: bool = True,
    ) -> None:
        keep = keep_profile_paths or set()
        for path in self.layout.profiles_dir.glob("*.profile.json"):
            if _rel(path, self.repo_root) in keep:
                continue
            path.unlink()
        for path in [
            self.layout.profiles_dir / "profile_index.json",
            self.layout.requirements_dir / "input_inventory.json",
            self.layout.requirements_dir / "stakeholder_interview.md",
            self.layout.contracts_dir / "kpi_registry.json",
            self.layout.contracts_dir / "domain_model.json",
            self.layout.contracts_dir / "semantic_contract.json",
            self.layout.contracts_dir / "workspace_lexicon.json",
            self.layout.solutions_dir / "kpi_metrics.sql",
            self.layout.evaluation_dir / "experiment.py",
            self.layout.evaluation_dir / "evaluator.py",
            self.layout.reports_dir / "open_questions.md",
            self.layout.reports_dir / "onboarding_report.md",
            self.layout.reports_dir / "generated_file_readability.md",
        ]:
            if path.exists():
                path.unlink()
        # Incremental runs keep the metadata store: reused profiles are not
        # re-upserted, so wiping it would orphan their entries. Full runs wipe
        # and rebuild as before.
        if clear_metadata_store:
            metadata_root = self.layout.state_dir / "metadata_store"
            if metadata_root.exists():
                for path in metadata_root.rglob("*.json"):
                    path.unlink()
            delta_root = self.layout.state_dir / "delta_metadata"
            if delta_root.exists():
                shutil.rmtree(delta_root)
        # Clear extracted data-dictionary text from prior runs so a fresh
        # onboarding re-extracts from whatever PDFs/DOCX are present today.
        dictionary_root = self.layout.generated_dir / "data_dictionary"
        if dictionary_root.exists():
            shutil.rmtree(dictionary_root)
        # Clear parsed data-model image sidecars so a re-run re-parses fresh.
        sidecar_root = self.layout.generated_dir / "data_model_images"
        if sidecar_root.exists():
            shutil.rmtree(sidecar_root)
        sidecar_report_root = self.layout.reports_dir / "data_model_images"
        if sidecar_report_root.exists():
            shutil.rmtree(sidecar_report_root)

    def _store_metadata(
        self,
        collection: str,
        document_id: str,
        payload: dict[str, Any],
    ) -> None:
        result = self.metadata_store.upsert(
            collection,
            document_id,
            payload,
            workspace=str(self.workspace),
        )
        if result.warning:
            warning_path = self.layout.reports_dir / "metadata_store_warnings.log"
            warning_path.parent.mkdir(parents=True, exist_ok=True)
            with warning_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{collection}/{document_id}: {result.warning}\n")


def find_root_artifact_violations(workspace: str | Path) -> list[str]:
    root = Path(workspace)
    forbidden_suffixes = {".duckdb", ".db", ".sqlite", ".log"}
    forbidden_names = {
        "kpi_metrics.sql",
        "analytics.duckdb",
        "evaluator.py",
        "experiment.py",
        "run_kpi_solution.py",
    }
    violations = []
    for path in root.iterdir() if root.exists() else []:
        if path.name == "interns":
            continue
        if path.is_file() and (path.name in forbidden_names or path.suffix.lower() in forbidden_suffixes):
            violations.append(str(path))
    return violations


def _sample_rows_for(path: Path, limit: int = 2) -> list[dict[str, Any]]:
    """First few data rows of an .xlsx, for the confirmation read-back."""
    try:
        return read_workbook_grid(path).rows[:limit]
    except Exception:
        return []


def _read_excel_kpis(path: Path) -> list[KpiDefinition]:
    return _read_excel_kpis_with_detection(path)[1]


def _read_excel_kpis_with_detection(
    path: Path,
) -> tuple[KpiFormatDetection | None, list[KpiDefinition]]:
    """Read an .xlsx KPI file, returning the format detection (with merged-cell
    nesting signals) alongside the extracted KPIs. Falls back to the XML reader
    when the structural reader is unavailable."""
    try:
        grid = read_workbook_grid(path)
        detection = detect_kpi_format(
            grid.columns, grid.rows, source=str(path), merged_spans=grid.merged_spans,
        )
        return detection, _extract_tabular_kpis(grid.columns, grid.rows, detection, source=str(path))
    except Exception:
        pass
    if pl:
        try:
            frame = pl.read_excel(path)
            return _read_frame_with_detection(frame, str(path))
        except Exception:
            pass
    return None, _read_xlsx_xml_kpis(path)


def _read_frame_with_detection(
    frame: Any, source: str,
) -> tuple[KpiFormatDetection | None, list[KpiDefinition]]:
    columns = list(frame.columns)
    rows = list(frame.iter_rows(named=True))
    detection = detect_kpi_format(columns, rows, source=source)
    return detection, _extract_tabular_kpis(columns, rows, detection, source=source)


def _read_tabular_kpis(frame: Any, source: str) -> list[KpiDefinition]:
    return _read_frame_with_detection(frame, source)[1]


def _extract_tabular_kpis(
    columns: list[str],
    rows: list[dict[str, Any]],
    detection: KpiFormatDetection,
    *,
    source: str,
) -> list[KpiDefinition]:
    """Extract KPI rows using the detector's column->role mapping, with the legacy
    header-synonym matcher as a zero-regression fallback for any role the detector
    did not place. The nested-row inheritance (blank key rows merging into the
    parent) is preserved exactly."""
    lowered = {col.lower().strip(): col for col in columns}
    name_col = detection.role_header("business_question") or _first_existing(
        lowered, ["key business question", "kpi", "kpi name", "metric", "name"]
    )
    desc_col = detection.role_header("description") or _first_existing(lowered, ["description", "definition"])
    cuts_col = detection.role_header("cuts") or _first_existing(lowered, KPI_CUTS_HEADERS)
    metric_col = detection.role_header("metric") or _first_existing(lowered, ["metric", "formula", "expression"])
    refine_col = detection.role_header("refinement") or _first_existing(
        lowered,
        ["data model refinement required", "refinement required", "open questions"],
    )
    if not name_col:
        return []
    if not cuts_col or not metric_col:
        detected_cuts_col, detected_metric_col = _detect_kpi_registry_detail_columns(rows, columns, name_col)
        cuts_col = cuts_col or detected_cuts_col
        metric_col = metric_col or detected_metric_col

    kpis = []
    current: dict[str, str] | None = None

    def flush_current() -> None:
        nonlocal current
        if not current:
            return
        # Authored cells only: inference is applied later by
        # _fill_kpi_gaps_with_lexicon once a workspace lexicon has been built.
        kpis.append(
            KpiDefinition(
                name=current["name"],
                description=current["description"],
                cuts=current["cuts"],
                metric=current["metric"],
                refinement_required=current["refinement_required"],
                source=source,
            )
        )
        current = None

    for row in rows:
        name = _clean_cell(row.get(name_col))
        if not name or _is_template_kpi_row(name):
            if current and not name:
                current["cuts"] = _merge_kpi_cuts(
                    current["cuts"],
                    _clean_cell(row.get(cuts_col)) if cuts_col else "",
                )
                if not current["metric"] and metric_col:
                    current["metric"] = _clean_cell(row.get(metric_col))
            continue
        metric = _clean_cell(row.get(metric_col)) if metric_col else ""
        cuts = _clean_cell(row.get(cuts_col)) if cuts_col else ""
        flush_current()
        current = {
            "name": name,
            "description": _clean_cell(row.get(desc_col)) if desc_col else "",
            "cuts": cuts,
            "metric": metric,
            "refinement_required": _clean_cell(row.get(refine_col)) if refine_col else "",
        }
    flush_current()
    return kpis


def _read_xlsx_xml_kpis(path: Path) -> list[KpiDefinition]:
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as zf:
        _sheet_xml = zf.read("xl/worksheets/sheet1.xml")
    # The .xlsx is untrusted (customer-uploaded). A worksheet sheet never
    # legitimately carries a DTD or entity declaration, so reject any before
    # parsing -- this neutralizes XXE / billion-laughs entity expansion. The
    # ET.fromstring below is therefore safe on this pre-screened input.
    if b"<!DOCTYPE" in _sheet_xml or b"<!ENTITY" in _sheet_xml:
        raise ValueError(
            "xlsx worksheet XML contains a DTD/entity declaration; refusing to "
            "parse (possible XXE)."
        )
    root = ET.fromstring(_sheet_xml)  # nosec B314 - DTD/ENTITY rejected above
    rows = []
    for row in root.findall("main:sheetData/main:row", ns):
        values = []
        for cell in row.findall("main:c", ns):
            text = "".join(node.text or "" for node in cell.findall(".//main:t", ns)).strip()
            values.append(text)
        rows.append(values)
    if not rows:
        return []
    headers = [value.lower().strip() for value in rows[0]]
    index = {header: idx for idx, header in enumerate(headers)}
    name_idx = _first_index(index, ["key business question", "kpi", "kpi name", "metric", "name"])
    if name_idx is None:
        return []
    desc_idx = _first_index(index, ["description", "definition"])
    cuts_idx = _first_index(index, KPI_CUTS_HEADERS)
    metric_idx = _first_index(index, ["metric", "formula", "expression"])
    refine_idx = _first_index(index, ["data model refinement required", "refinement required"])
    kpis = []
    for row in rows[1:]:
        name = _cell_at(row, name_idx)
        if not name or _is_template_kpi_row(name):
            continue
        # Authored cells only: inference is applied later by
        # _fill_kpi_gaps_with_lexicon once a workspace lexicon has been built.
        kpis.append(
            KpiDefinition(
                name=name,
                description=_cell_at(row, desc_idx),
                cuts=_cell_at(row, cuts_idx),
                metric=_cell_at(row, metric_idx),
                refinement_required=_cell_at(row, refine_idx),
                source=str(path),
            )
        )
    return kpis


def _kpi_name_key(name: str) -> str:
    """Normalized identity for a KPI business question.

    Lowercased, punctuation-stripped, whitespace-collapsed so the SAME question
    text coming from two registries (e.g. a finalized
    ``kpi_registry.generated.json`` plus the raw ``*.sql`` it was generated from)
    collapses to one key. Generic: it keys on the question text only, never on a
    source path or domain vocabulary.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]+", " ", str(name or "").lower())).strip()


def _kpi_richness(kpi: KpiDefinition) -> int:
    """How completely a KPI is specified. Used to pick the survivor when the
    same business question appears in multiple registries: the entry that
    already carries a metric/cuts/refinement wins over a bare duplicate."""
    score = 0
    if str(kpi.metric or "").strip():
        score += 2
    if str(kpi.cuts or "").strip():
        score += 2
    if str(kpi.refinement_required or "").strip():
        score += 1
    if str(kpi.description or "").strip():
        score += 1
    return score


def _dedupe_kpis_by_name(kpis: list[KpiDefinition]) -> tuple[list[KpiDefinition], list[str]]:
    """Collapse KPIs that share a normalized business question across registries.

    Onboarding can discover both a finalized generation registry and the raw
    source file it was generated from; ingesting both double-counts every KPI.
    Dedupe by normalized question text, keeping the richest entry (and the first
    of equal-richness ones, preserving spec order). Workspace-agnostic — it never
    inspects which file a KPI came from, only the question text and completeness.
    """
    survivors: dict[str, int] = {}
    ordered: list[KpiDefinition] = []
    collapsed = 0
    for kpi in kpis:
        key = _kpi_name_key(kpi.name)
        if not key:
            ordered.append(kpi)
            continue
        if key not in survivors:
            survivors[key] = len(ordered)
            ordered.append(kpi)
            continue
        existing_index = survivors[key]
        if _kpi_richness(kpi) > _kpi_richness(ordered[existing_index]):
            ordered[existing_index] = kpi
        collapsed += 1
    warnings: list[str] = []
    if collapsed:
        warnings.append(
            f"[~] kpi_dedupe:{collapsed} duplicate KPI(s) collapsed by business question "
            "(same question present in more than one registry)."
        )
    return ordered, warnings


def _read_json_kpis(path: Path, repo_root: Path) -> list[KpiDefinition]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("kpis", data if isinstance(data, list) else [])
    kpis = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = _clean_cell(item.get("name") or item.get("kpi") or item.get("question") or item.get("kpi_name") or item.get("business_question"))
        if name:
            description = _clean_cell(item.get("description") or item.get("definition"))
            cuts = _clean_cell(item.get("cuts") or item.get("grain") or item.get("dimensions"))
            metric = _clean_cell(item.get("metric") or item.get("formula"))
            # Authored cells only: inference is applied later by
            # _fill_kpi_gaps_with_lexicon once a workspace lexicon has been built.
            kpis.append(
                KpiDefinition(
                    name=name,
                    description=description,
                    cuts=cuts,
                    metric=metric,
                    refinement_required=_clean_cell(item.get("refinement_required")),
                    source=_rel(path, repo_root),
                )
            )
    return kpis


def _read_markdown_kpis(path: Path, repo_root: Path) -> list[KpiDefinition]:
    """Read KPI definitions from a markdown document.

    Two authoring shapes are supported, both evidence-preserving:

    1. Table rows (``| name | detail | ... |``) — unchanged behavior.
    2. Prose sections: a heading whose text mentions a KPI starts a section;
       every prose line until the next same-or-higher-level heading (or a
       thematic break ``---``) is the KPI's authored body and is preserved as
       the ``description``. Stakeholder sentences ARE the KPI definition in
       natural-language registries; dropping them starves feature extraction
       and produces blocked-with-no-question dead ends (hostile-workspace
       finding F2). No heading layout beyond "heading + body" is assumed.
    """
    text = path.read_text(encoding="utf-8")
    source = _rel(path, repo_root)
    kpis: list[KpiDefinition] = []
    heading_re = re.compile(r"^(#{1,6})\s+(.*)$")
    rule_re = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
    current_name: str | None = None
    current_level = 0
    current_body: list[str] = []

    def table_row_kpi(stripped: str) -> KpiDefinition | None:
        if not stripped.startswith("|") or "kpi" in stripped.lower():
            return None
        if stripped.startswith("|---") or stripped.startswith("| :"):
            return None
        cells = [cell.strip(" *`") for cell in stripped.strip("|").split("|")]
        if not cells or not cells[0]:
            return None
        return KpiDefinition(
            name=cells[0], description=" | ".join(cells[1:]), source=source
        )

    def flush() -> None:
        nonlocal current_name, current_body
        if current_name is not None:
            table_rows = [line for line in current_body if line.startswith("|")]
            prose_lines = [
                line for line in current_body if line and not line.startswith("|")
            ]
            if table_rows and not prose_lines:
                # A "## KPIs" container heading over a table: the table rows are
                # the KPIs, not the heading itself.
                for row in table_rows:
                    parsed = table_row_kpi(row)
                    if parsed is not None:
                        kpis.append(parsed)
            else:
                description = "\n".join(current_body).strip()
                kpis.append(
                    KpiDefinition(
                        name=current_name, description=description, source=source
                    )
                )
        current_name = None
        current_body = []

    for line in text.splitlines():
        stripped = line.strip()
        heading = heading_re.match(stripped)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if "kpi" in title.lower():
                flush()
                current_name = title
                current_level = level
            elif current_name is not None and level <= current_level:
                # A non-KPI heading at the same or higher level closes the
                # current KPI section.
                flush()
            elif current_name is not None:
                # A deeper non-KPI subheading stays part of the prose body.
                current_body.append(title)
            continue
        if current_name is not None:
            if rule_re.match(stripped):
                # A thematic break ends the section (shared document trailer).
                flush()
                continue
            current_body.append(stripped)
            continue
        parsed = table_row_kpi(stripped)
        if parsed is not None:
            kpis.append(parsed)
    flush()
    return kpis


def _read_sql_comment_kpis(path: Path, repo_root: Path) -> list[KpiDefinition]:
    return [
        KpiDefinition(
            name=_clean_cell(item.get("name")),
            description=_clean_cell(item.get("description")),
            cuts=_clean_cell(item.get("cuts")),
            metric=_clean_cell(item.get("metric")),
            source=_rel(path, repo_root),
        )
        for item in extract_kpis_from_sql(path.read_text(encoding="utf-8"), _rel(path, repo_root))
        if _clean_cell(item.get("name"))
    ]


def _first_existing(lowered: dict[str, str], candidates: list[str]) -> str | None:
    return first_existing(lowered, candidates)


def _detect_kpi_registry_detail_columns(
    rows: list[dict[str, Any]],
    columns: list[str],
    name_col: str,
) -> tuple[str | None, str | None]:
    """Detect spreadsheets where the cuts/metric labels appear in a subheader row."""
    for row in rows[:5]:
        if _clean_cell(row.get(name_col)):
            continue
        cuts_col = None
        metric_col = None
        for col in columns:
            value = _clean_cell(row.get(col)).lower()
            normalized = re.sub(r"[^a-z0-9]+", " ", value).strip()
            if normalized in {"cuts with drg consolidated", "cuts", "dimensions", "grain"}:
                cuts_col = col
            elif normalized in {"metric", "formula", "expression"}:
                metric_col = col
        if cuts_col or metric_col:
            return cuts_col, metric_col
    return None, None


def _merge_kpi_cuts(primary: str, extra: str) -> str:
    values: list[str] = []
    for source in (primary, extra):
        for part in re.split(r"[,;\n]+", source or ""):
            clean = _clean_cell(part)
            if clean and clean.lower() not in {value.lower() for value in values}:
                values.append(clean)
    return ", ".join(values)


def _source_truth_constraints(inferred_cuts: str) -> str:
    constraints = []
    for part in re.split(r"[,;\n]+", inferred_cuts or ""):
        clean = _clean_cell(part)
        if any(token in clean for token in ("=", ">", "<")) or "top 10" in clean.lower():
            constraints.append(clean)
    return ", ".join(constraints)


_DOCUMENT_KPI_NAME_HEADERS = frozenset({"kpi", "name", "indicator", "kpiname", "metricname"})
_DOCUMENT_KPI_METRIC_HEADERS = frozenset({"metric", "measure", "formula", "calculation", "aggregation"})
_DOCUMENT_KPI_CUTS_HEADERS = frozenset(
    {"cut", "cuts", "dimension", "dimensions", "grain", "breakout", "breakdown", "by", "groupby"}
)


def _document_kpi_header_role(header: str) -> str | None:
    """Map a PDF KPI-table column header to a KpiDefinition field role.

    Returns "name" | "metric" | "cuts" | None. Workspace-agnostic: matches on the
    normalized header word only (no domain vocabulary)."""
    norm = re.sub(r"[^a-z0-9]+", "", str(header).lower())
    if norm in _DOCUMENT_KPI_NAME_HEADERS:
        return "name"
    if norm in _DOCUMENT_KPI_METRIC_HEADERS:
        return "metric"
    if norm in _DOCUMENT_KPI_CUTS_HEADERS:
        return "cuts"
    return None


def _is_template_kpi_row(name: str) -> bool:
    return is_template_kpi_row(name)


def _infer_metric_and_cuts(name: str, description: str = "") -> tuple[str, str]:
    return infer_metric_and_cuts(name, description)


def _first_index(index: dict[str, int], candidates: list[str]) -> int | None:
    return first_index(index, candidates)


def _cell_at(row: list[str], idx: int | None) -> str:
    return cell_at(row, idx)


def _clean_cell(value: Any) -> str:
    return clean_cell(value)


def _total_existing_bytes(paths: list[str], repo_root: Path) -> int:
    total = 0
    for raw in paths:
        path = repo_root / raw
        if path.exists() and path.is_file():
            total += path.stat().st_size
    return total


def _onboarding_next_step(
    inputs: WorkspaceInputs,
    kpis: list[KpiDefinition],
    profiles: list[dict[str, Any]],
) -> str:
    if not kpis and profiles:
        return (
            "Build source-family/schema-drift contracts before KPI generation, route selection, "
            "or medallion planning."
        )
    return "Resolve KPI feature mappings or prepare the KPI blocker panel before SQL generation."


def _onboarding_next_command(
    inputs: WorkspaceInputs,
    kpis: list[KpiDefinition],
    profiles: list[dict[str, Any]],
) -> str:
    if not kpis and profiles:
        return f"uv run build-source-family-contracts --workspace {inputs.workspace}"
    return f"uv run prepare-kpi-blocker-panel --workspace {inputs.workspace} --domain <domain>"


def _safe_stem(path: Path, root: Path) -> str:
    try:
        rel = str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        rel = str(path).replace("\\", "/")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", rel).strip("_")


def _metadata_collection_for_path(path: Path) -> str | None:
    name = path.name
    parent = path.parent.name
    if parent == "contracts":
        return "contracts"
    if parent == "requirements":
        return "requirements"
    if parent == "profiles":
        return "profiles"
    if name == "bootstrap_manifest.json":
        return "bootstrap"
    if name == "bootstrap_status.json":
        return "bootstrap"
    return None


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


# THIS is the live `onboard-workspace` entry point. This module defines `def main`
# three times (also ~L1413, ~L1462); Python binds the LAST, so this one wins and the
# earlier two are dead. This file has trapped two audits on shadowed/duplicate mains
# (a grep for `^def main` also once matched two mains inside string templates) — if
# you touch the entry point, edit THIS main, and keep the @anchored decorator here.
@anchored("onboard-workspace")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Onboard a workspace into interns/ artifacts.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Repository root. Defaults to detected project root.",
    )
    parser.add_argument("--exact-profile", action="store_true", help="Run exact scans for profile bounds.")
    parser.add_argument("--sample-rows", type=int, default=100_000, help="Sample rows for profiling.")
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Ignore the incremental onboarding manifest and re-profile every "
            "dataset (full clear + rebuild, the legacy behavior)."
        ),
    )
    args = parser.parse_args(argv)

    onboarder = WorkspaceOnboarder(
        args.repo_root,
        args.workspace,
        exact_profile=args.exact_profile,
        sample_rows=args.sample_rows,
        force=args.force,
    )
    workspace_path = (Path(args.repo_root) / args.workspace).resolve()
    try:
        with time_command(workspace_path, "onboard-workspace") as event_details:
            result = onboarder.run()
            event_details["kpi_count"] = result.kpi_count
            event_details["profile_count"] = result.profile_count
            event_details["warnings"] = len(result.warnings)
    except WorkspaceLockTimeout as exc:
        print(json.dumps({"error": "workspace_lock_timeout", "detail": str(exc)}, indent=2))
        return 2
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
