"""Governed source catalog planning and ingestion.

Reusable source templates live under ``config/source_catalogs``. A workspace
chooses approved sources in ``docs/source_selection.json`` and this module
turns that selection into either a dry-run plan or local workspace inputs.
"""
from __future__ import annotations
from core.observability.cost_ledger import anchored

import argparse
import asyncio
import csv
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import polars as pl

from core.paths import rel_to as _rel
from core.resource.manager import ResourceManager
from core.storage.external_data import (
    is_external_path,
    is_within_allowed_roots,
    load_external_data_policy,
)
from core.storage.workspace_layout import WorkspaceLayout


SOURCE_TYPES = {"api", "local", "databricks_uc"}
DEFAULT_SELECTION = "docs/source_selection.json"


@dataclass(frozen=True)
class ApiFetchPolicy:
    max_pages: int = 1
    max_rows: int = 1000
    page_size: int = 5000
    max_bytes: int = 50_000_000
    qps: float = 1.0
    attempts: int = 4
    timeout_seconds: float = 30.0
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 30.0

    @classmethod
    def from_action(cls, action: dict[str, Any]) -> "ApiFetchPolicy":
        policy = action.get("fetch_policy") or {}
        return cls(
            max_pages=max(1, int(action.get("max_pages") or policy.get("max_pages") or 1)),
            max_rows=max(0, int(action.get("max_rows") or policy.get("max_rows") or 1000)),
            page_size=max(1, int(action.get("page_size") or policy.get("page_size") or 5000)),
            max_bytes=max(1, int(policy.get("max_bytes") or action.get("max_bytes") or 50_000_000)),
            qps=max(0.1, float(policy.get("qps") or action.get("qps") or 1.0)),
            attempts=max(1, int(policy.get("attempts") or action.get("attempts") or 4)),
            timeout_seconds=max(1.0, float(policy.get("timeout_seconds") or 30.0)),
            backoff_initial_seconds=max(0.1, float(policy.get("backoff_initial_seconds") or 1.0)),
            backoff_max_seconds=max(1.0, float(policy.get("backoff_max_seconds") or 30.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogResult:
    workspace: str
    selection_path: str
    apply: bool
    source_count: int
    actions: list[dict[str, Any]]
    artifacts: dict[str, str]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HostRateLimiter:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request_at: dict[str, float] = {}

    async def wait(self, url: str, qps: float) -> None:
        host = urllib.parse.urlparse(url).netloc or "default"
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last_request_at = self._last_request_at.get(host, 0.0)
            wait_for = (1.0 / max(qps, 0.1)) - (time.monotonic() - last_request_at)
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request_at[host] = time.monotonic()


class SourceCatalogManager:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        selection_path: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(self.workspace)
        self.selection_path = self._resolve_selection_path(selection_path)
        self._validate_workspace()

    def plan(self) -> CatalogResult:
        selection = self._load_selection()
        templates = self._load_templates()
        actions, warnings = self._build_actions(selection, templates)
        artifacts = self._write_plan(selection, actions, warnings, applied=False)
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(self.selection_path, self.repo_root),
            apply=False,
            source_count=len(selection.get("sources", [])),
            actions=actions,
            artifacts=artifacts,
            warnings=warnings,
        )

    def ingest(self) -> CatalogResult:
        selection = self._load_selection()
        templates = self._load_templates()
        actions, warnings = self._build_actions(selection, templates)
        applied_actions = self._apply_actions(actions, warnings)
        artifacts = self._write_plan(selection, applied_actions, warnings, applied=True)
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(self.selection_path, self.repo_root),
            apply=True,
            source_count=len(selection.get("sources", [])),
            actions=applied_actions,
            artifacts=artifacts,
            warnings=warnings,
        )

    def apply_stage(self, source_type: str, source_id: str | None = None) -> CatalogResult:
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"unsupported source type: {source_type}")
        selection = self._load_selection()
        templates = self._load_templates()
        actions, warnings = self._build_actions(selection, templates)
        stage_actions = [
            action
            for action in actions
            if action["source_type"] == source_type
            and (source_id is None or action["source_id"] == source_id)
        ]
        if source_id and not stage_actions:
            raise ValueError(f"source not found for {source_type}: {source_id}")
        applied_actions = self._apply_actions(stage_actions, warnings)
        artifacts = self._write_plan(selection, applied_actions, warnings, applied=True)
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(self.selection_path, self.repo_root),
            apply=True,
            source_count=len(stage_actions),
            actions=applied_actions,
            artifacts=artifacts,
            warnings=warnings,
        )

    def preflight(self, source_id: str | None = None) -> CatalogResult:
        selection = self._load_selection()
        templates = self._load_templates()
        actions, warnings = self._build_actions(selection, templates)
        selected = [
            action
            for action in actions
            if source_id is None or action.get("source_id") == source_id
        ]
        checked: list[dict[str, Any]] = []
        resource_manager = ResourceManager(self.workspace, repo_root=self.repo_root)
        resource_artifacts = resource_manager.write_report(
            estimated_bytes=_estimated_source_bytes(selected),
            workload="source_catalog",
        )
        resource_decision = resource_manager.decide(
            estimated_bytes=_estimated_source_bytes(selected),
            workload="source_catalog",
        )
        for action in selected:
            item = dict(action)
            checks = _preflight_action(self.workspace, self.repo_root, item, resource_manager=resource_manager)
            checks.append(
                {
                    "name": "resource_budget",
                    "status": "error" if resource_decision.blockers else "warning" if resource_decision.warnings else "ok",
                    "detail": resource_decision.mode,
                }
            )
            item["preflight_status"] = "error" if any(c["status"] == "error" for c in checks) else "ok"
            item["preflight_checks"] = checks
            item["resource_decision"] = resource_decision.to_dict()
            checked.append(item)
        artifacts = self._write_evidence_artifact(
            "source_catalog_preflight.json",
            "source-catalog preflight",
            checked,
            warnings,
        )
        artifacts.update(resource_artifacts)
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(self.selection_path, self.repo_root),
            apply=False,
            source_count=len(checked),
            actions=checked,
            artifacts=artifacts,
            warnings=warnings,
        )

    def discover_docs(self, source_id: str | None = None) -> CatalogResult:
        actions, warnings = self._read_latest_or_planned_actions()
        selected = [
            action
            for action in actions
            if action.get("source_type") == "api"
            and (source_id is None or action.get("source_id") == source_id)
        ]
        discovered: list[dict[str, Any]] = []
        for action in selected:
            item = dict(action)
            path = Path(str(action.get("materialized_path") or ""))
            docs = _discover_doc_links(path) if path.exists() else []
            item["discovered_docs"] = docs
            item["discovery_status"] = "ok" if docs else "none"
            discovered.append(item)
        artifacts = self._write_evidence_artifact(
            "source_catalog_doc_discovery.json",
            "source-catalog discover-docs",
            discovered,
            warnings,
        )
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(self.selection_path, self.repo_root),
            apply=False,
            source_count=len(discovered),
            actions=discovered,
            artifacts=artifacts,
            warnings=warnings,
        )

    def index_catalog(self, source_id: str, *, max_entries: int = 1000) -> CatalogResult:
        actions, warnings = self._read_latest_or_planned_actions()
        action = _find_action(actions, source_id)
        path = Path(str(action.get("materialized_path") or ""))
        if not path.exists():
            raise FileNotFoundError(f"materialized catalog not found for source {source_id}: {path}")
        resource_manager = ResourceManager(self.workspace, repo_root=self.repo_root)
        resource_decision = resource_manager.check_disk_budget(estimated_bytes=path.stat().st_size, workload="catalog_index")
        if resource_decision.blockers:
            raise RuntimeError(f"catalog_index_resource_blocked:{resource_decision.blockers}")
        entries = _index_catalog_file(
            path,
            source_id=source_id,
            max_entries=max_entries,
            max_single_json_bytes=resource_manager.budget.max_single_json_bytes,
        )
        index_dir = self.layout.requirements_dir / "source_catalog"
        report_dir = self.layout.reports_dir / "source_catalog"
        index_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        index_path = index_dir / f"{_safe_name(source_id)}_index.jsonl"
        summary_path = index_dir / f"{_safe_name(source_id)}_index_summary.json"
        report_path = report_dir / f"{_safe_name(source_id)}_index.md"
        with index_path.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        summary = {
            "artifact_type": "source_catalog_index_summary.json",
            "version": 1,
            "source_id": source_id,
            "entry_count": len(entries),
            "truncated": len(entries) >= max_entries,
            "index_path": _rel(index_path, self.repo_root),
            "generated_at": _now(),
        }
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        report_path.write_text(_render_index_report(summary, entries), encoding="utf-8")
        item = dict(action)
        item.update(
            {
                "catalog_index_status": "indexed",
                "entry_count": len(entries),
                "index_path": _rel(index_path, self.repo_root),
                "index_summary_path": _rel(summary_path, self.repo_root),
                "index_report_path": _rel(report_path, self.repo_root),
            }
        )
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(self.selection_path, self.repo_root),
            apply=False,
            source_count=1,
            actions=[item],
            artifacts={
                "index": _rel(index_path, self.repo_root),
                "summary": _rel(summary_path, self.repo_root),
                "report": _rel(report_path, self.repo_root),
            },
            warnings=warnings,
        )

    def match_catalog(
        self,
        source_id: str,
        *,
        keywords: list[str] | None = None,
        min_score: int = 2,
    ) -> CatalogResult:
        index_path = self.layout.requirements_dir / "source_catalog" / f"{_safe_name(source_id)}_index.jsonl"
        if not index_path.exists():
            raise FileNotFoundError(f"catalog index not found: {index_path}. Run index-catalog first.")
        workspace_terms = _workspace_match_terms(self.workspace)
        all_terms = sorted(set(workspace_terms + [kw for kw in (keywords or []) if kw]))
        matches = _match_index(index_path, all_terms, min_score=min_score)
        out_dir = self.layout.requirements_dir / "source_catalog"
        report_dir = self.layout.reports_dir / "source_catalog"
        out_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        match_path = out_dir / f"{_safe_name(source_id)}_matches.json"
        report_path = report_dir / f"{_safe_name(source_id)}_matches.md"
        payload = {
            "artifact_type": "source_catalog_matches.json",
            "version": 1,
            "source_id": source_id,
            "terms": all_terms,
            "min_score": min_score,
            "match_count": len(matches),
            "matches": matches,
            "generated_at": _now(),
        }
        match_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        report_path.write_text(_render_match_report(payload), encoding="utf-8")
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(self.selection_path, self.repo_root),
            apply=False,
            source_count=len(matches),
            actions=matches,
            artifacts={"matches": _rel(match_path, self.repo_root), "report": _rel(report_path, self.repo_root)},
            warnings=[],
        )

    def draft_selection(self, source_id: str, *, min_score: int = 2) -> CatalogResult:
        match_path = self.layout.requirements_dir / "source_catalog" / f"{_safe_name(source_id)}_matches.json"
        if not match_path.exists():
            raise FileNotFoundError(f"catalog matches not found: {match_path}. Run match-catalog first.")
        payload = json.loads(match_path.read_text(encoding="utf-8"))
        sources = _draft_sources_from_matches(payload.get("matches") or [], min_score=min_score)
        draft = {
            "artifact_type": "source_selection_draft.json",
            "version": 1,
            "generated_by": "source-catalog draft-selection",
            "source_catalog_id": source_id,
            "approval_required": True,
            "sources": sources,
        }
        draft_path = self.workspace / "docs" / "source_selection.generated.json"
        report_path = self.layout.reports_dir / "source_catalog" / f"{_safe_name(source_id)}_draft_selection.md"
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(json.dumps(draft, indent=2), encoding="utf-8")
        report_path.write_text(_render_draft_report(draft), encoding="utf-8")
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(self.selection_path, self.repo_root),
            apply=False,
            source_count=len(sources),
            actions=sources,
            artifacts={"draft": _rel(draft_path, self.repo_root), "report": _rel(report_path, self.repo_root)},
            warnings=[],
        )

    def finalize_selection(
        self, source_id: str | None = None, *, approve_final_preview: bool = False
    ) -> CatalogResult:
        if not approve_final_preview:
            raise ValueError("finalize-selection requires --approve-final-preview")
        draft_path = self.workspace / "docs" / "source_selection.generated.json"
        if not draft_path.exists():
            raise FileNotFoundError(f"source selection draft not found: {draft_path}")
        draft = json.loads(draft_path.read_text(encoding="utf-8"))
        draft_id = str(draft.get("source_catalog_id") or "").strip()
        # source_id is optional: infer it from the draft's source_catalog_id, then
        # the workspace folder name. Only enforce a match when the caller passes an
        # explicit id AND the draft also records one and they differ.
        if source_id and draft_id and draft_id != source_id:
            raise ValueError(
                f"draft source_catalog_id mismatch: expected {source_id}, found {draft_id}"
            )
        source_id = source_id or draft_id or self.workspace.name
        sources = draft.get("sources")
        if not isinstance(sources, list):
            raise ValueError("draft selection must contain a sources list")
        final = {
            "artifact_type": "source_selection.json",
            "version": 1,
            "generated_by": "source-catalog finalize-selection",
            "source_catalog_id": source_id,
            "finalized_at": _now(),
            "sources": sources,
        }
        final_path = self.workspace / DEFAULT_SELECTION
        final_path.parent.mkdir(parents=True, exist_ok=True)
        previous_path = final_path.with_suffix(final_path.suffix + f".backup.{int(time.time())}")
        if final_path.exists():
            shutil.copy2(final_path, previous_path)
        final_path.write_text(json.dumps(final, indent=2), encoding="utf-8")
        report_path = self.layout.reports_dir / "source_catalog" / f"{_safe_name(source_id)}_finalized_selection.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_finalized_report(final, previous_path if previous_path.exists() else None), encoding="utf-8")
        artifacts = {"selection": _rel(final_path, self.repo_root), "report": _rel(report_path, self.repo_root)}
        if previous_path.exists():
            artifacts["backup"] = _rel(previous_path, self.repo_root)
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(final_path, self.repo_root),
            apply=True,
            source_count=len(sources),
            actions=sources,
            artifacts=artifacts,
            warnings=[],
        )

    def process(self, source_id: str | None = None) -> CatalogResult:
        actions, warnings = self._read_latest_actions()
        selected = [
            action
            for action in actions
            if source_id is None or action.get("source_id") == source_id
        ]
        processed: list[dict[str, Any]] = []
        for action in selected:
            item = dict(action)
            path = Path(str(action.get("materialized_path") or ""))
            if path.exists():
                item["processing_status"] = "classified"
                item["content_class"] = _classify_path(path)
                if item["content_class"] == "dataset":
                    item.update(_process_dataset(path, self.layout, item.get("source_id", "source"), self.repo_root))
                item["next_step"] = (
                    "onboard-workspace"
                    if item["content_class"] in {"dataset", "document"}
                    else "manual_review"
                )
            else:
                item["processing_status"] = "skipped"
                item["processing_reason"] = "no_materialized_path"
            processed.append(item)
        artifacts = self._write_processing(processed, warnings)
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(self.selection_path, self.repo_root),
            apply=True,
            source_count=len(processed),
            actions=processed,
            artifacts=artifacts,
            warnings=warnings,
        )

    def validate(self, *, strict: bool = False) -> CatalogResult:
        actions, warnings = self._read_latest_actions()
        validated: list[dict[str, Any]] = []
        for action in actions:
            item = dict(action)
            status = action.get("status")
            materialized = Path(str(action.get("materialized_path") or ""))
            provenance = Path(str(action.get("provenance_path") or ""))
            if status in {"fetched", "partial_failed", "copied"}:
                if not materialized.exists():
                    item["validation_status"] = "error"
                    item["validation_error"] = "materialized_path_missing"
                elif not provenance.exists():
                    item["validation_status"] = "error"
                    item["validation_error"] = "provenance_path_missing"
                elif strict and status == "partial_failed":
                    item["validation_status"] = "error"
                    item["validation_error"] = "partial_fetch_not_allowed_in_strict_mode"
                elif strict and int(action.get("failure_count") or 0) > 0:
                    item["validation_status"] = "error"
                    item["validation_error"] = "fetch_failures_not_allowed_in_strict_mode"
                else:
                    item["validation_status"] = "ok"
            elif status in {"registered", "metadata_exported", "planned_only", "blocked"}:
                item["validation_status"] = "ok"
            else:
                item["validation_status"] = "warning"
                item["validation_warning"] = f"unrecognized_or_failed_status:{status}"
            validated.append(item)
        artifacts = self._write_validation(validated, warnings, strict=strict)
        return CatalogResult(
            workspace=_rel(self.workspace, self.repo_root),
            selection_path=_rel(self.selection_path, self.repo_root),
            apply=False,
            source_count=len(validated),
            actions=validated,
            artifacts=artifacts,
            warnings=warnings,
        )

    def _apply_actions(
        self,
        actions: list[dict[str, Any]],
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        api_results = self._apply_api_actions(actions, warnings)
        applied_actions: list[dict[str, Any]] = []
        for idx, action in enumerate(actions):
            applied = dict(action)
            try:
                if idx in api_results:
                    applied.update(api_results[idx])
                elif action["source_type"] == "local":
                    applied.update(self._apply_local_source(action))
                elif action["source_type"] == "databricks_uc":
                    applied.update(self._apply_databricks_source(action))
            except Exception as exc:
                applied["status"] = "failed"
                applied["error"] = f"{type(exc).__name__}:{exc}"
                warnings.append(f"{action['source_id']}:apply_failed:{type(exc).__name__}:{exc}")
            applied_actions.append(applied)
        return applied_actions

    def _apply_api_actions(
        self,
        actions: list[dict[str, Any]],
        warnings: list[str],
    ) -> dict[int, dict[str, Any]]:
        indexed = [
            (
                idx,
                {
                    **action,
                    "workspace_path": str(self.workspace),
                    "repo_root": str(self.repo_root),
                },
            )
            for idx, action in enumerate(actions)
            if action.get("source_type") == "api"
        ]
        if not indexed:
            return {}
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_apply_api_actions_async(self.repo_root, self.layout, indexed, warnings))
        raise RuntimeError("source catalog API scheduler cannot run inside an active event loop")

    def _build_actions(
        self,
        selection: dict[str, Any],
        templates: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        actions: list[dict[str, Any]] = []
        warnings: list[str] = []
        for raw in selection.get("sources", []):
            if not isinstance(raw, dict):
                warnings.append("invalid_source_entry:not_object")
                continue
            source = _merge_template(raw, templates)
            source_id = str(source.get("id") or source.get("source_id") or "").strip()
            source_type = str(source.get("type") or "").strip()
            if not source_id:
                warnings.append("invalid_source_entry:missing_id")
                continue
            if source_type not in SOURCE_TYPES:
                warnings.append(f"{source_id}:unsupported_source_type:{source_type}")
                continue
            approval = str(source.get("approval") or "needs_approval")
            if approval != "approved":
                actions.append(_base_action(source, status="blocked", reason="source_not_approved"))
                continue
            if source_type == "api":
                actions.append(self._plan_api_source(source))
            elif source_type == "local":
                actions.append(self._plan_local_source(source))
            elif source_type == "databricks_uc":
                actions.append(self._plan_databricks_source(source))
        return actions, warnings

    def _plan_api_source(self, source: dict[str, Any]) -> dict[str, Any]:
        action = _base_action(source, status="planned", reason="api_fetch_requires_apply")
        target_kind = str(source.get("target_kind") or "dataset")
        target_dir = "docs" if target_kind == "doc" else "datasets"
        target_name = _safe_name(str(source.get("target_name") or source["id"]))
        extension = str(source.get("extension") or ("json" if target_kind != "doc" else "bin")).lstrip(".")
        action.update(
            {
                "method": str(source.get("method") or "GET").upper(),
                "url": source.get("url", ""),
                "target_path": _rel(self.workspace / target_dir / target_name / f"data.{extension}", self.repo_root),
                "max_rows": source.get("max_rows", 1000),
                "max_pages": source.get("max_pages", 1),
                "page_size": source.get("page_size", 5000),
                "max_bytes": source.get("max_bytes", 50_000_000),
                "fetch_policy": source.get("fetch_policy", {}),
                "auth": _redacted_auth_policy(source.get("auth", {})),
                "pagination": source.get("pagination", {}),
                "expected_columns": source.get("expected_columns", []),
                "schema": source.get("schema", {}),
                "response_mode": source.get("response_mode", ""),
            }
        )
        return action

    def _plan_local_source(self, source: dict[str, Any]) -> dict[str, Any]:
        action = _base_action(source, status="planned", reason="local_source_requires_apply")
        source_path = Path(str(source.get("path") or "")).expanduser()
        if not source_path.is_absolute():
            source_path = (self.workspace / source_path).resolve()
        target_kind = str(source.get("target_kind") or "dataset")
        target_root = self.workspace / ("docs" if target_kind == "doc" else "datasets")
        target_name = _safe_name(str(source.get("target_name") or source_path.stem or source["id"]))
        action.update(
            {
                "path": str(source_path),
                "mode": str(source.get("mode") or "copy"),
                "target_path": _rel(target_root / target_name / source_path.name, self.repo_root),
            }
        )
        return action

    def _plan_databricks_source(self, source: dict[str, Any]) -> dict[str, Any]:
        action = _base_action(source, status="planned", reason="databricks_uc_metadata_requires_remote_approval")
        catalog = str(source.get("catalog") or "")
        schema = str(source.get("schema") or "")
        table = str(source.get("table") or "")
        action.update(
            {
                "catalog": catalog,
                "schema": schema,
                "table": table,
                "fqn": ".".join(part for part in (catalog, schema, table) if part),
                "remote_metadata": "planned_only",
                "approval_env": "AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1",
            }
        )
        return action

    def _apply_api_source(self, action: dict[str, Any]) -> dict[str, Any]:
        if not action.get("url"):
            return {"status": "blocked", "reason": "missing_api_url"}
        return _run_api_runtime(self.repo_root, self.layout, action)

    def _apply_local_source(self, action: dict[str, Any]) -> dict[str, Any]:
        source_path = Path(action["path"]).resolve()
        # T8: refuse any source path outside the repo or a configured external
        # root, even after approval — otherwise an approved selection could copy
        # or register-by-allowlist an arbitrary host file (e.g. C:/Windows, /etc,
        # another user's home) into a governed workspace. Ref: ob-sources.md.
        policy = load_external_data_policy(self.repo_root)
        if not is_within_allowed_roots(source_path, self.repo_root, policy):
            return {
                "status": "blocked",
                "reason": "local_source_outside_allowed_roots",
                "path": str(source_path),
            }
        if not source_path.exists():
            return {"status": "blocked", "reason": "local_source_missing"}
        mode = action.get("mode", "copy")
        if mode == "register":
            self._register_external_allowlist(source_path, action["source_id"])
            return {"status": "registered", "registered_path": str(source_path)}
        if source_path.is_dir():
            return {"status": "blocked", "reason": "directory_copy_not_supported_use_register"}
        target_path = self.repo_root / action["target_path"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        return _provenance(target_path, status="copied", source_url=str(source_path))

    def _apply_databricks_source(self, action: dict[str, Any]) -> dict[str, Any]:
        if os.environ.get("AUTORESEARCH_ALLOW_REMOTE_EXECUTION") != "1":
            return {"status": "planned_only", "reason": "remote_execution_not_approved"}
        try:
            from databricks.sdk import WorkspaceClient
        except ImportError:
            return {"status": "blocked", "reason": "databricks_sdk_not_installed"}
        client = WorkspaceClient()
        fqn = action.get("fqn") or ""
        try:
            table = client.tables.get(fqn)
        except Exception as exc:
            return {"status": "blocked", "reason": f"uc_table_metadata_failed:{type(exc).__name__}:{exc}"}
        metadata = {
            "status": "metadata_exported",
            "fqn": fqn,
            "name": getattr(table, "name", ""),
            "catalog_name": getattr(table, "catalog_name", ""),
            "schema_name": getattr(table, "schema_name", ""),
            "table_type": str(getattr(table, "table_type", "")),
            "data_source_format": str(getattr(table, "data_source_format", "")),
            "columns": [
                {"name": getattr(col, "name", ""), "type": str(getattr(col, "type_text", ""))}
                for col in (getattr(table, "columns", None) or [])
            ],
        }
        out = self.layout.requirements_dir / "source_catalog" / f"{_safe_name(action['source_id'])}_uc_metadata.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return {"status": "metadata_exported", "metadata_path": _rel(out, self.repo_root)}

    def _register_external_allowlist(self, path: Path, source_id: str) -> None:
        # P4d (T6): take the workspace lock around the settings read-modify-write
        # and re-read UNDER the lock, so concurrent local-stage of multiple
        # sources can't lose allowlist entries (last-writer-wins). Atomic write.
        # Ref: core-audit ob-sources.md, storage.md.
        from core.storage.atomic_io import write_text_atomic
        from core.storage.workspace_lock import workspace_lock

        self.layout.state_dir.mkdir(parents=True, exist_ok=True)
        with workspace_lock(self.layout.project_root):
            settings = self.layout.load_settings()
            entries = settings.setdefault("dataset_allowlist", [])
            normalized = str(path)
            if not any(str(item.get("path")) == normalized for item in entries if isinstance(item, dict)):
                entries.append(
                    {
                        "type": "external_absolute" if path.is_absolute() else "workspace_relative",
                        "path": normalized,
                        "reason": f"source_catalog:{source_id}",
                    }
                )
            write_text_atomic(self.layout.workspace_settings, json.dumps(settings, indent=2))

    def _write_plan(
        self,
        selection: dict[str, Any],
        actions: list[dict[str, Any]],
        warnings: list[str],
        *,
        applied: bool,
    ) -> dict[str, str]:
        self.layout.ensure_runtime_dirs()
        payload = {
            "artifact_type": "source_catalog_plan.json",
            "version": 1,
            "generated_by": "source-catalog",
            "workspace": _rel(self.workspace, self.repo_root),
            "selection_path": _rel(self.selection_path, self.repo_root),
            "applied": applied,
            "generated_at": _now(),
            "source_count": len(selection.get("sources", [])),
            "actions": actions,
            "warnings": warnings,
        }
        plan_path = self.layout.requirements_dir / "source_catalog_plan.json"
        report_path = self.layout.reports_dir / "source_catalog_plan.md"
        plan_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        report_path.write_text(_render_report(payload), encoding="utf-8")
        return {"plan": _rel(plan_path, self.repo_root), "report": _rel(report_path, self.repo_root)}

    def _write_processing(
        self,
        actions: list[dict[str, Any]],
        warnings: list[str],
    ) -> dict[str, str]:
        self.layout.ensure_runtime_dirs()
        payload = {
            "artifact_type": "source_catalog_processing.json",
            "version": 1,
            "generated_by": "source-catalog process",
            "workspace": _rel(self.workspace, self.repo_root),
            "generated_at": _now(),
            "actions": actions,
            "warnings": warnings,
        }
        path = self.layout.evidence_dir / "source_catalog_processing.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {"processing": _rel(path, self.repo_root)}

    def _write_validation(
        self,
        actions: list[dict[str, Any]],
        warnings: list[str],
        *,
        strict: bool = False,
    ) -> dict[str, str]:
        self.layout.ensure_runtime_dirs()
        errors = [a for a in actions if a.get("validation_status") == "error"]
        payload = {
            "artifact_type": "source_catalog_validation.json",
            "version": 1,
            "generated_by": "source-catalog validate",
            "workspace": _rel(self.workspace, self.repo_root),
            "generated_at": _now(),
            "status": "error" if errors else "ok",
            "strict": strict,
            "actions": actions,
            "warnings": warnings,
        }
        path = self.layout.evidence_dir / "source_catalog_validation.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {"validation": _rel(path, self.repo_root)}

    def _write_evidence_artifact(
        self,
        filename: str,
        generated_by: str,
        actions: list[dict[str, Any]],
        warnings: list[str],
    ) -> dict[str, str]:
        self.layout.ensure_runtime_dirs()
        payload = {
            "artifact_type": filename,
            "version": 1,
            "generated_by": generated_by,
            "workspace": _rel(self.workspace, self.repo_root),
            "generated_at": _now(),
            "actions": actions,
            "warnings": warnings,
        }
        path = self.layout.evidence_dir / filename
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {Path(filename).stem: _rel(path, self.repo_root)}

    def _read_latest_actions(self) -> tuple[list[dict[str, Any]], list[str]]:
        plan_path = self.layout.requirements_dir / "source_catalog_plan.json"
        if not plan_path.exists():
            raise FileNotFoundError(
                f"source catalog plan not found: {plan_path}. Run source-catalog plan first."
            )
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        return list(payload.get("actions") or []), list(payload.get("warnings") or [])

    def _read_latest_or_planned_actions(self) -> tuple[list[dict[str, Any]], list[str]]:
        plan_path = self.layout.requirements_dir / "source_catalog_plan.json"
        if plan_path.exists():
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
            return list(payload.get("actions") or []), list(payload.get("warnings") or [])
        selection = self._load_selection()
        return self._build_actions(selection, self._load_templates())

    def _load_selection(self) -> dict[str, Any]:
        if not self.selection_path.exists():
            raise FileNotFoundError(f"source selection not found: {self.selection_path}")
        data = json.loads(self.selection_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
            raise ValueError("source selection must be a JSON object with a sources list")
        return data

    def _load_templates(self) -> dict[str, dict[str, Any]]:
        template_dir = self.repo_root / "config" / "source_catalogs"
        templates: dict[str, dict[str, Any]] = {}
        if not template_dir.exists():
            return templates
        for path in sorted(template_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for template in payload.get("templates", []):
                if isinstance(template, dict) and template.get("id"):
                    templates[str(template["id"])] = template
        return templates

    def _resolve_selection_path(self, selection_path: str | Path | None) -> Path:
        if selection_path is None:
            return self.workspace / DEFAULT_SELECTION
        raw = Path(selection_path).expanduser()
        if raw.is_absolute():
            return raw.resolve()
        repo_relative = (self.repo_root / raw).resolve()
        if repo_relative.is_relative_to(self.workspace):
            return repo_relative
        return (self.workspace / raw).resolve()

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        policy = load_external_data_policy(self.repo_root)
        if is_external_path(self.workspace, self.repo_root, policy):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")
        if not self.selection_path.is_relative_to(self.workspace):
            raise ValueError("source selection must live inside the workspace")


def _merge_template(source: dict[str, Any], templates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    template_id = source.get("template")
    if not template_id:
        return dict(source)
    merged = dict(templates.get(str(template_id), {}))
    merged.update(source)
    return merged


def _base_action(source: dict[str, Any], *, status: str, reason: str) -> dict[str, Any]:
    return {
        "source_id": str(source.get("id") or source.get("source_id") or ""),
        "source_type": str(source.get("type") or ""),
        "status": status,
        "reason": reason,
        "target_kind": str(source.get("target_kind") or "dataset"),
        "processing_hint": source.get("processing_hint", {}),
        "provenance_required": True,
    }


def _run_api_runtime(repo_root: Path, layout: WorkspaceLayout, action: dict[str, Any]) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_api_runtime_async(repo_root, layout, action))
    raise RuntimeError("source catalog API runtime cannot run inside an active event loop")


async def _apply_api_actions_async(
    repo_root: Path,
    layout: WorkspaceLayout,
    indexed_actions: list[tuple[int, dict[str, Any]]],
    warnings: list[str],
) -> dict[int, dict[str, Any]]:
    limiter = HostRateLimiter()
    concurrency = _api_scheduler_concurrency([action for _, action in indexed_actions])
    semaphore = asyncio.Semaphore(concurrency)

    async def apply_one(idx: int, action: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not action.get("url"):
            return idx, {"status": "blocked", "reason": "missing_api_url"}
        async with semaphore:
            try:
                result = await _run_api_runtime_async(repo_root, layout, action, rate_limiter=limiter)
            except Exception as exc:
                warnings.append(f"{action['source_id']}:apply_failed:{type(exc).__name__}:{exc}")
                result = {"status": "failed", "error": f"{type(exc).__name__}:{exc}"}
            result["scheduler"] = {
                "mode": "concurrent_api",
                "max_concurrent": concurrency,
                "host_rate_limited": True,
            }
            return idx, result

    completed = await asyncio.gather(*(apply_one(idx, action) for idx, action in indexed_actions))
    return dict(completed)


def _api_scheduler_concurrency(actions: list[dict[str, Any]]) -> int:
    configured: list[int] = []
    for action in actions:
        policy = action.get("fetch_policy") or {}
        value = policy.get("concurrency") or action.get("concurrency")
        if value is not None:
            configured.append(max(1, int(value)))
    if configured:
        return min(16, max(1, min(configured)))
    env_value = os.environ.get("AUTORESEARCH_SOURCE_CATALOG_API_CONCURRENCY")
    if env_value:
        return min(16, max(1, int(env_value)))
    workspace_value = actions[0].get("workspace_path") if actions else None
    repo_value = actions[0].get("repo_root") if actions else None
    if workspace_value and repo_value:
        try:
            decision = ResourceManager(Path(str(workspace_value)), repo_root=Path(str(repo_value))).decide(
                workload="api_ingestion"
            )
            return min(decision.recommended_api_concurrency, max(1, len(actions)))
        except Exception:
            pass
    return min(4, max(1, len(actions)))


async def _run_api_runtime_async(
    repo_root: Path,
    layout: WorkspaceLayout,
    action: dict[str, Any],
    *,
    rate_limiter: HostRateLimiter | None = None,
) -> dict[str, Any]:
    policy = ApiFetchPolicy.from_action(action)
    resource_decision = ResourceManager(layout.project_root, repo_root=repo_root).check_disk_budget(
        estimated_bytes=policy.max_bytes,
        workload="api_ingestion",
    )
    if resource_decision.blockers:
        return {
            "status": "blocked",
            "reason": "resource_budget_blocked",
            "resource_decision": resource_decision.to_dict(),
        }
    target_path = repo_root / action["target_path"]
    target_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = _checkpoint_path(layout, action["source_id"])
    quarantine_dir = layout.evidence_dir / "source_catalog" / "quarantine" / _safe_name(action["source_id"])
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    response_mode = str(action.get("response_mode") or "").lower()
    target_suffix = target_path.suffix.lower()
    if response_mode == "file" or action.get("target_kind") == "doc" or target_suffix not in {".csv", ".json", ".jsonl", ".ndjson"}:
        return await _run_streaming_file_download(
            action,
            target_path,
            checkpoint_path,
            quarantine_dir,
            policy,
            repo_root,
            rate_limiter=rate_limiter,
        )

    checkpoint = _read_checkpoint(checkpoint_path)
    start_page = int(checkpoint.get("next_page") or checkpoint.get("pages_succeeded") or 0)
    rows: list[dict[str, Any]] = _read_existing_rows(target_path) if start_page else []
    bytes_read = 0
    pages_attempted = start_page
    pages_succeeded = start_page
    failures: list[dict[str, Any]] = []
    pagination = action.get("pagination") or {}
    headers = _headers_from_auth(action.get("auth") or {})
    timeout = aiohttp.ClientTimeout(total=policy.timeout_seconds)
    connector = aiohttp.TCPConnector(limit_per_host=1, ttl_dns_cache=300)
    last_request_at = 0.0

    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        for page in range(start_page, policy.max_pages):
            pages_attempted += 1
            url = _page_url(action["url"], pagination, page, policy.page_size)
            if rate_limiter is not None:
                await rate_limiter.wait(url, policy.qps)
            else:
                wait_for = (1.0 / policy.qps) - (time.monotonic() - last_request_at)
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                last_request_at = time.monotonic()
            try:
                payload_bytes = await _fetch_bytes_with_retry(session, url, policy)
            except Exception as exc:
                failure = _quarantine_failure(quarantine_dir, page, url, exc)
                failures.append(failure)
                _write_checkpoint(checkpoint_path, action, policy, pages_succeeded, bytes_read, failures, next_page=page + 1)
                continue

            bytes_read += len(payload_bytes)
            if bytes_read > policy.max_bytes:
                failure = _quarantine_failure(
                    quarantine_dir,
                    page,
                    url,
                    RuntimeError(f"max_bytes_exceeded:{bytes_read}>{policy.max_bytes}"),
                )
                failures.append(failure)
                break

            pages_succeeded += 1
            try:
                parsed = json.loads(payload_bytes.decode("utf-8"))
            except UnicodeDecodeError:
                parsed = None
            except json.JSONDecodeError:
                parsed = None

            page_rows = _rows_from_payload(parsed, pagination) if parsed is not None else []
            if page_rows:
                schema_error = _validate_expected_columns(action, page_rows)
                if schema_error:
                    failure = _quarantine_failure(quarantine_dir, page, url, schema_error)
                    failures.append(failure)
                    break
                rows.extend(page_rows)
                if policy.max_rows and len(rows) >= policy.max_rows:
                    rows = rows[: policy.max_rows]
                    break
                if len(page_rows) < policy.page_size:
                    break
            else:
                await _write_streamed_bytes(target_path, payload_bytes)
                _write_checkpoint(checkpoint_path, action, policy, pages_succeeded, bytes_read, failures, next_page=page + 1)
                result = _provenance(target_path, status="fetched", source_url=action["url"])
                result.update(
                    {
                        "fetch_policy": policy.to_dict(),
                        "checkpoint_path": _rel(checkpoint_path, repo_root),
                        "pages_attempted": pages_attempted,
                        "pages_succeeded": pages_succeeded,
                        "bytes_read": bytes_read,
                        "failure_count": len(failures),
                    }
                )
                return result

            _write_checkpoint(checkpoint_path, action, policy, pages_succeeded, bytes_read, failures, next_page=page + 1)

    if rows:
        _write_rows(target_path, rows)
        result = _provenance(
            target_path,
            status="partial_failed" if failures else "fetched",
            source_url=action["url"],
        )
    else:
        result = {"status": "failed", "reason": "no_rows_or_file_materialized"}
    result.update(
        {
            "fetch_policy": policy.to_dict(),
            "checkpoint_path": _rel(checkpoint_path, repo_root),
            "pages_attempted": pages_attempted,
            "pages_succeeded": pages_succeeded,
            "rows_written": len(rows),
            "bytes_read": bytes_read,
            "failure_count": len(failures),
            "quarantine_dir": _rel(quarantine_dir, repo_root) if failures else "",
        }
    )
    return result


async def _run_streaming_file_download(
    action: dict[str, Any],
    target_path: Path,
    checkpoint_path: Path,
    quarantine_dir: Path,
    policy: ApiFetchPolicy,
    repo_root: Path,
    *,
    rate_limiter: HostRateLimiter | None = None,
) -> dict[str, Any]:
    headers = _headers_from_auth(action.get("auth") or {})
    timeout = aiohttp.ClientTimeout(total=policy.timeout_seconds)
    connector = aiohttp.TCPConnector(limit_per_host=1, ttl_dns_cache=300)
    failures: list[dict[str, Any]] = []
    async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
        try:
            if rate_limiter is not None:
                await rate_limiter.wait(action["url"], policy.qps)
            bytes_read = await _stream_to_path_with_retry(session, action["url"], target_path, policy)
        except Exception as exc:
            failures.append(_quarantine_failure(quarantine_dir, 0, action["url"], exc))
            _write_checkpoint(checkpoint_path, action, policy, 0, 0, failures, next_page=1)
            return {
                "status": "failed",
                "reason": "streaming_download_failed",
                "checkpoint_path": _rel(checkpoint_path, repo_root),
                "failure_count": len(failures),
                "quarantine_dir": _rel(quarantine_dir, repo_root),
            }
    _write_checkpoint(checkpoint_path, action, policy, 1, bytes_read, failures, next_page=1)
    result = _provenance(target_path, status="fetched", source_url=action["url"])
    result.update(
        {
            "fetch_policy": policy.to_dict(),
            "checkpoint_path": _rel(checkpoint_path, repo_root),
            "pages_attempted": 1,
            "pages_succeeded": 1,
            "bytes_read": bytes_read,
            "failure_count": 0,
        }
    )
    return result


def _page_url(url: str, pagination: dict[str, Any], page: int, page_size: int) -> str:
    if not pagination:
        return url
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    style = pagination.get("style", "offset")
    if style == "offset":
        query[str(pagination.get("size_param") or "size")] = str(page_size)
        query[str(pagination.get("offset_param") or "offset")] = str(page * page_size)
    elif style == "page":
        query[str(pagination.get("page_param") or "page")] = str(page + 1)
        query[str(pagination.get("size_param") or "size")] = str(page_size)
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def assert_url_egress_allowed(url: str) -> None:
    """Raise RuntimeError unless ``url`` is a safe public HTTP(S) egress target.

    SSRF guard for approved `api` sources (T8): blocks non-http(s) schemes and any
    host that resolves to loopback / private (RFC1918) / link-local
    (169.254.0.0/16, incl. the cloud metadata IP 169.254.169.254) / reserved /
    multicast / unspecified addresses. A human may opt out for a trusted internal
    host by setting AUTORESEARCH_ALLOW_PRIVATE_EGRESS=1. Ref: ob-sources.md.
    """
    if os.environ.get("AUTORESEARCH_ALLOW_PRIVATE_EGRESS") == "1":
        return
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(f"ssrf_blocked_scheme:{parsed.scheme or '<none>'}")
    host = parsed.hostname
    if not host:
        raise RuntimeError("ssrf_blocked_no_host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        raise RuntimeError(f"ssrf_unresolvable_host:{host}:{exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise RuntimeError(f"ssrf_blocked_private_ip:{host}->{ip}")


async def _fetch_bytes_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    policy: ApiFetchPolicy,
) -> bytes:
    assert_url_egress_allowed(url)
    last_exc: Exception | None = None
    for attempt in range(policy.attempts):
        try:
            async with session.get(url) as response:
                if response.status in {429, 500, 502, 503, 504}:
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                    await asyncio.sleep(
                        retry_after
                        if retry_after is not None
                        else min(
                            policy.backoff_initial_seconds * (2**attempt),
                            policy.backoff_max_seconds,
                        )
                    )
                    last_exc = RuntimeError(f"http_retryable_status:{response.status}")
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    total += len(chunk)
                    if total > policy.max_bytes:
                        raise RuntimeError(f"max_bytes_exceeded:{total}>{policy.max_bytes}")
                    chunks.append(chunk)
                return b"".join(chunks)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            last_exc = exc
            if attempt < policy.attempts - 1:
                await asyncio.sleep(
                    min(policy.backoff_initial_seconds * (2**attempt), policy.backoff_max_seconds)
                )
    raise RuntimeError(f"api_request_failed:{last_exc}")


async def _stream_to_path_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    path: Path,
    policy: ApiFetchPolicy,
) -> int:
    assert_url_egress_allowed(url)
    last_exc: Exception | None = None
    for attempt in range(policy.attempts):
        part_path = path.with_suffix(path.suffix + ".part")
        try:
            async with session.get(url) as response:
                if response.status in {429, 500, 502, 503, 504}:
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                    await asyncio.sleep(
                        retry_after
                        if retry_after is not None
                        else min(policy.backoff_initial_seconds * (2**attempt), policy.backoff_max_seconds)
                    )
                    last_exc = RuntimeError(f"http_retryable_status:{response.status}")
                    continue
                response.raise_for_status()
                total = 0
                with part_path.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        total += len(chunk)
                        if total > policy.max_bytes:
                            raise RuntimeError(f"max_bytes_exceeded:{total}>{policy.max_bytes}")
                        handle.write(chunk)
                part_path.replace(path)
                return total
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            last_exc = exc
            if part_path.exists():
                part_path.unlink()
            if attempt < policy.attempts - 1:
                await asyncio.sleep(min(policy.backoff_initial_seconds * (2**attempt), policy.backoff_max_seconds))
    raise RuntimeError(f"stream_download_failed:{last_exc}")


def _rows_from_payload(payload: Any, pagination: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows_key = pagination.get("rows_key") or "data"
        rows = payload.get(rows_key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _validate_expected_columns(action: dict[str, Any], rows: list[dict[str, Any]]) -> Exception | None:
    schema = action.get("schema") or {}
    expected = action.get("expected_columns") or schema.get("required_columns") or []
    if not expected:
        return None
    observed = set()
    for row in rows:
        observed.update(row.keys())
    missing = [col for col in expected if col not in observed]
    if missing:
        return RuntimeError(f"schema_missing_columns:{missing}")
    return None


def _read_existing_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8", newline="") as handle:
                return list(csv.DictReader(handle))
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, csv.Error):
        # Narrowed from bare Exception: a corrupt checkpoint resume now degrades
        # to "restart from empty" only on a genuine read/parse error, not on an
        # unrelated bug masquerading as an empty resume. Ref: P6 (sources).
        return []
    return []


async def _write_streamed_bytes(path: Path, payload: bytes) -> None:
    part_path = path.with_suffix(path.suffix + ".part")
    part_path.write_bytes(payload)
    part_path.replace(path)


def _headers_from_auth(auth: dict[str, Any]) -> dict[str, str]:
    headers = {"User-Agent": "autoresearch-source-catalog/1.0"}
    header_env = auth.get("header_env")
    header_name = auth.get("header_name", "Authorization")
    if header_env and os.environ.get(str(header_env)):
        value = os.environ[str(header_env)]
        prefix = str(auth.get("header_prefix") or "").strip()
        headers[str(header_name)] = f"{prefix} {value}".strip() if prefix else value
    return headers


def _redacted_auth_policy(auth: Any) -> dict[str, Any]:
    if not isinstance(auth, dict):
        return {}
    redacted: dict[str, Any] = {}
    for key in ("type", "header_name", "header_env", "header_prefix"):
        if key in auth:
            redacted[key] = auth[key]
    if "header_env" in redacted:
        redacted["secret_value"] = "<redacted>"
    return redacted


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _checkpoint_path(layout: WorkspaceLayout, source_id: str) -> Path:
    return layout.state_dir / "source_catalog" / "checkpoints" / f"{_safe_name(source_id)}.json"


def _read_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_checkpoint(
    path: Path,
    action: dict[str, Any],
    policy: ApiFetchPolicy,
    pages_succeeded: int,
    bytes_read: int,
    failures: list[dict[str, Any]],
    *,
    next_page: int | None = None,
) -> None:
    payload = {
        "artifact_type": "source_catalog_checkpoint.json",
        "version": 1,
        "source_id": action["source_id"],
        "updated_at": _now(),
        "fetch_policy": policy.to_dict(),
        "pages_succeeded": pages_succeeded,
        "next_page": pages_succeeded if next_page is None else next_page,
        "bytes_read": bytes_read,
        "failure_count": len(failures),
        "failures": failures,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _quarantine_failure(
    quarantine_dir: Path,
    page: int,
    url: str,
    exc: Exception,
) -> dict[str, Any]:
    payload = {
        "page": page,
        "url": url,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "created_at": _now(),
    }
    path = quarantine_dir / f"page_{page:05d}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["path"] = str(path)
    return payload


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part_path = path.with_suffix(path.suffix + ".part")
    if path.suffix.lower() == ".csv":
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        with part_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
        part_path.replace(path)
        return
    part_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    part_path.replace(path)


def _classify_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".json", ".jsonl", ".ndjson", ".parquet", ".pq"}:
        return "dataset"
    if suffix in {".pdf", ".md", ".txt", ".html", ".docx", ".xlsx", ".xls", ".png", ".jpg", ".jpeg"}:
        return "document"
    return "unknown"


def _process_dataset(path: Path, layout: WorkspaceLayout, source_id: str, repo_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        if path.suffix.lower() == ".csv":
            lf = pl.scan_csv(path, infer_schema_length=1000, ignore_errors=True)
        elif path.suffix.lower() in {".json", ".jsonl", ".ndjson"}:
            lf = pl.scan_ndjson(path)
        elif path.suffix.lower() in {".parquet", ".pq"}:
            lf = pl.scan_parquet(path)
        else:
            return {"dataset_processing_status": "skipped", "dataset_processing_reason": "unsupported_format"}
        schema = lf.collect_schema()
        row_count = lf.select(pl.len()).collect().item()
        nulls = lf.select([pl.col(col).null_count().alias(col) for col in schema.names()]).collect().to_dicts()[0]
        profile = {
            "source_id": source_id,
            "path": str(path),
            "row_count": row_count,
            "schema": {name: str(dtype) for name, dtype in schema.items()},
            "null_counts": nulls,
            "generated_at": _now(),
        }
        profile_dir = layout.evidence_dir / "source_catalog" / "profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_path = profile_dir / f"{_safe_name(source_id)}.profile.json"
        drift_path = profile_dir / f"{_safe_name(source_id)}.drift.json"
        previous = json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
        profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        drift = _build_drift_report(previous, profile)
        drift_path.write_text(json.dumps(drift, indent=2), encoding="utf-8")
        result.update(
            {
                "dataset_processing_status": "profiled",
                "profile_path": _rel(profile_path, repo_root),
                "drift_path": _rel(drift_path, repo_root),
                "row_count": row_count,
            }
        )
        if path.suffix.lower() != ".parquet":
            parquet_path = layout.evidence_dir / "source_catalog" / "staged" / f"{_safe_name(source_id)}.parquet"
            parquet_path.parent.mkdir(parents=True, exist_ok=True)
            lf.collect(engine="streaming").write_parquet(parquet_path)
            result["staged_parquet_path"] = _rel(parquet_path, repo_root)
    except Exception as exc:
        result.update(
            {
                "dataset_processing_status": "failed",
                "dataset_processing_error": f"{type(exc).__name__}:{exc}",
            }
        )
    return result


def _build_drift_report(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    if not previous:
        return {"status": "baseline_created", "alerts": []}
    alerts: list[str] = []
    old_schema = set((previous.get("schema") or {}).keys())
    new_schema = set((current.get("schema") or {}).keys())
    missing = sorted(old_schema - new_schema)
    added = sorted(new_schema - old_schema)
    if missing:
        alerts.append(f"missing_columns:{missing}")
    if added:
        alerts.append(f"added_columns:{added}")
    old_rows = int(previous.get("row_count") or 0)
    new_rows = int(current.get("row_count") or 0)
    if old_rows and abs(new_rows - old_rows) / old_rows > 0.5:
        alerts.append(f"row_count_changed:{old_rows}->{new_rows}")
    old_nulls = previous.get("null_counts") or {}
    new_nulls = current.get("null_counts") or {}
    for col, old_count in old_nulls.items():
        if col in new_nulls and old_rows and new_rows:
            old_rate = float(old_count or 0) / old_rows
            new_rate = float(new_nulls[col] or 0) / new_rows
            if new_rate > old_rate * 2 and new_rate > 0.05:
                alerts.append(f"null_spike:{col}:{old_rate:.3f}->{new_rate:.3f}")
    return {"status": "warning" if alerts else "healthy", "alerts": alerts}


def _estimated_source_bytes(actions: list[dict[str, Any]]) -> int:
    total = 0
    for action in actions:
        if action.get("source_type") == "api":
            policy = ApiFetchPolicy.from_action(action)
            total += policy.max_bytes
        elif action.get("source_type") == "local":
            path = Path(str(action.get("path") or ""))
            if path.exists() and path.is_file():
                total += path.stat().st_size
    return total


def _preflight_action(
    workspace: Path,
    repo_root: Path,
    action: dict[str, Any],
    *,
    resource_manager: ResourceManager | None = None,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    target = repo_root / str(action.get("target_path") or "")
    if action.get("target_path") and target.resolve().is_relative_to(workspace.resolve()):
        checks.append({"name": "target_path", "status": "ok", "detail": _rel(target, repo_root)})
    elif action.get("source_type") != "databricks_uc":
        checks.append({"name": "target_path", "status": "error", "detail": "target outside workspace"})
    if action.get("source_type") == "api":
        checks.append({"name": "url", "status": "ok" if action.get("url") else "error", "detail": "configured" if action.get("url") else "missing"})
        policy = ApiFetchPolicy.from_action(action)
        checks.append({"name": "rate_limit", "status": "ok", "detail": f"qps={policy.qps}"})
        if resource_manager is not None:
            disk_decision = resource_manager.check_disk_budget(
                estimated_bytes=policy.max_bytes,
                workload="api_ingestion",
            )
            checks.append(
                {
                    "name": "api_disk_budget",
                    "status": "error" if disk_decision.blockers else "ok",
                    "detail": disk_decision.mode,
                }
            )
        auth = action.get("auth") or {}
        env_name = auth.get("header_env")
        if env_name:
            checks.append({"name": "auth_env", "status": "ok" if os.environ.get(str(env_name)) else "error", "detail": f"{env_name}=<redacted>" if os.environ.get(str(env_name)) else f"{env_name}=<missing>"})
    if action.get("source_type") == "local":
        path = Path(str(action.get("path") or ""))
        checks.append({"name": "local_path", "status": "ok" if path.exists() else "error", "detail": str(path)})
    if action.get("source_type") == "databricks_uc":
        approved = os.environ.get("AUTORESEARCH_ALLOW_REMOTE_EXECUTION") == "1"
        checks.append({"name": "remote_approval", "status": "ok" if approved else "warning", "detail": "approved" if approved else "planned_only"})
    return checks


def _discover_doc_links(path: Path) -> list[dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    docs: list[dict[str, str]] = []
    for item in _walk_json(data):
        if isinstance(item, str) and item.lower().split("?")[0].endswith((".pdf", ".docx", ".html", ".txt", ".md")):
            docs.append({"url": item, "kind": Path(urllib.parse.urlparse(item).path).suffix.lower().lstrip(".")})
    return docs


def _index_catalog_file(
    path: Path,
    *,
    source_id: str,
    max_entries: int,
    max_single_json_bytes: int = 20_000_000,
) -> list[dict[str, Any]]:
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return _collect_catalog_entries(_iter_jsonl_objects(path), source_id=source_id, max_entries=max_entries)
    if path.suffix.lower() == ".json":
        stream_offset = _catalog_array_offset(path)
        if stream_offset is not None:
            return _collect_catalog_entries(
                _iter_json_array_objects(path, stream_offset),
                source_id=source_id,
                max_entries=max_entries,
            )
        if path.stat().st_size > max_single_json_bytes:
            raise ValueError(
                "large JSON catalog has no streamable top-level or known-key array; "
                "convert it to JSONL/NDJSON or provide a catalog with dataset/items/results/data/tables array near the top"
            )
    data = json.loads(path.read_text(encoding="utf-8"))
    candidates = _catalog_entry_candidates(data)
    return _collect_catalog_entries(iter(candidates), source_id=source_id, max_entries=max_entries)


def _collect_catalog_entries(
    candidates: Any,
    *,
    source_id: str,
    max_entries: int,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for candidate in candidates:
        if len(entries) >= max_entries:
            break
        if isinstance(candidate, dict):
            entries.append(_normalize_catalog_entry(candidate, source_id, len(entries)))
    return entries


def _iter_jsonl_objects(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _catalog_array_offset(path: Path) -> int | None:
    sample_limit = 2_000_000
    with path.open("rb") as handle:
        sample = handle.read(sample_limit)
    stripped = sample.lstrip()
    if stripped.startswith(b"["):
        return len(sample) - len(stripped)
    for key in (b"dataset", b"datasets", b"resources", b"items", b"results", b"data", b"tables"):
        marker = b'"' + key + b'"'
        pos = sample.find(marker)
        if pos == -1:
            continue
        bracket = sample.find(b"[", pos + len(marker))
        colon = sample.find(b":", pos + len(marker))
        if bracket != -1 and colon != -1 and colon < bracket:
            return bracket
    return None


def _iter_json_array_objects(path: Path, array_offset: int):
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(array_offset + 1)
        buffer = ""
        eof = False
        while True:
            while not eof and len(buffer) < 1024 * 1024:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    eof = True
                    break
                buffer += chunk
            buffer = buffer.lstrip()
            if not buffer:
                if eof:
                    return
                continue
            if buffer.startswith("]"):
                return
            if buffer.startswith(","):
                buffer = buffer[1:].lstrip()
                continue
            try:
                obj, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                if eof:
                    raise
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    eof = True
                buffer += chunk
                continue
            yield obj
            buffer = buffer[end:]


def _catalog_entry_candidates(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("dataset", "datasets", "resources", "items", "results", "data", "tables"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [value for value in data.values() if isinstance(value, dict)]
    return []


def _normalize_catalog_entry(entry: dict[str, Any], source_id: str, idx: int) -> dict[str, Any]:
    title = _first_text(entry, "title", "name", "label", "table", "table_name")
    description = _first_text(entry, "description", "notes", "summary", "comment")
    entry_id = _first_text(entry, "identifier", "id", "uuid", "name") or f"{source_id}_{idx}"
    urls = _extract_urls(entry)
    doc_urls = [url for url in urls if _url_kind(url) == "doc"]
    api_urls = [url for url in urls if _url_kind(url) == "api"]
    file_urls = [url for url in urls if _url_kind(url) == "file"]
    tags = _extract_tags(entry)
    publisher = _first_text(entry, "publisher", "provider", "owner", "organization")
    modified = _first_text(entry, "modified", "modified_at", "updated", "updated_at")
    match_text = " ".join([entry_id, title, description, publisher, " ".join(tags)])
    return {
        "source_catalog_id": source_id,
        "entry_id": str(entry_id),
        "title": title,
        "description": description[:500],
        "tags": tags,
        "publisher": publisher,
        "modified_at": modified,
        "dataset_urls": file_urls,
        "doc_urls": doc_urls,
        "api_urls": api_urls,
        "file_urls": file_urls,
        "schema_urls": [url for url in urls if "schema" in url.lower() or "dictionary" in url.lower()],
        "raw_entry_pointer": f"{source_id}[{idx}]",
        "match_keys": sorted(_tokens(match_text)),
    }


def _first_text(entry: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for nested in ("name", "title", "label"):
                if isinstance(value.get(nested), str):
                    return value[nested].strip()
    return ""


def _extract_urls(value: Any) -> list[str]:
    urls: list[str] = []
    for item in _walk_json(value):
        if isinstance(item, str) and item.lower().startswith(("http://", "https://")):
            urls.append(item)
    return sorted(dict.fromkeys(urls))


def _extract_tags(entry: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for key in ("tags", "keywords", "theme", "themes", "category", "categories"):
        value = entry.get(key)
        if isinstance(value, str):
            tags.extend(_tokens(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    tags.extend(_tokens(item))
                elif isinstance(item, dict):
                    tags.extend(_tokens(_first_text(item, "name", "title", "label")))
    return sorted(set(tags))


def _url_kind(url: str) -> str:
    clean = url.lower().split("?", 1)[0]
    if clean.endswith((".pdf", ".docx", ".html", ".txt", ".md", ".xlsx", ".xls")):
        return "doc"
    if clean.endswith((".csv", ".json", ".jsonl", ".ndjson", ".parquet", ".zip")):
        return "file"
    return "api"


def _workspace_match_terms(workspace: Path) -> list[str]:
    terms: list[str] = []
    for folder in ("datasets", "docs"):
        root = workspace / folder
        if root.exists():
            for path in root.rglob("*"):
                if path == root or "/interns/" in path.as_posix():
                    continue
                terms.extend(_tokens(path.stem if path.is_file() else path.name))
    for path in (workspace / "docs").glob("*") if (workspace / "docs").exists() else []:
        terms.extend(_tokens(path.name))
    return sorted(set(t for t in terms if len(t) > 2))


def _match_index(index_path: Path, terms: list[str], *, min_score: int) -> list[dict[str, Any]]:
    term_set = set(terms)
    matches: list[dict[str, Any]] = []
    with index_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            entry = json.loads(line)
            keys = set(entry.get("match_keys") or [])
            overlap = sorted(term_set.intersection(keys))
            score = len(overlap)
            if score >= min_score:
                item = dict(entry)
                item["match_score"] = score
                item["match_reason"] = f"matched_terms:{overlap}"
                matches.append(item)
    matches.sort(key=lambda item: (-int(item["match_score"]), item.get("title", "")))
    return matches


def _draft_sources_from_matches(matches: list[dict[str, Any]], *, min_score: int) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for match in matches:
        if int(match.get("match_score") or 0) < min_score:
            continue
        base_id = _safe_name(str(match.get("entry_id") or match.get("title") or "catalog_entry"))
        for idx, url in enumerate(match.get("api_urls") or []):
            sources.append(
                {
                    "id": f"{base_id}_api_{idx + 1}",
                    "type": "api",
                    "approval": "needs_approval",
                    "url": url,
                    "target_kind": "dataset",
                    "target_name": base_id,
                    "extension": "json",
                    "response_mode": "rows",
                    "match_reason": match.get("match_reason", ""),
                }
            )
        for idx, url in enumerate(match.get("file_urls") or []):
            sources.append(
                {
                    "id": f"{base_id}_file_{idx + 1}",
                    "type": "api",
                    "approval": "needs_approval",
                    "url": url,
                    "target_kind": "dataset",
                    "target_name": base_id,
                    "extension": Path(urllib.parse.urlparse(url).path).suffix.lstrip(".") or "bin",
                    "response_mode": "file",
                    "match_reason": match.get("match_reason", ""),
                }
            )
        for idx, url in enumerate(match.get("doc_urls") or []):
            sources.append(
                {
                    "id": f"{base_id}_doc_{idx + 1}",
                    "type": "api",
                    "approval": "needs_approval",
                    "url": url,
                    "target_kind": "doc",
                    "target_name": base_id,
                    "extension": Path(urllib.parse.urlparse(url).path).suffix.lstrip(".") or "bin",
                    "response_mode": "file",
                    "match_reason": match.get("match_reason", ""),
                }
            )
    return sources


def _walk_json(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)
    else:
        yield value


def _render_index_report(summary: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    lines = [
        "# Source Catalog Index",
        "",
        f"- Source: `{summary['source_id']}`",
        f"- Entries: `{summary['entry_count']}`",
        f"- Truncated: `{summary['truncated']}`",
        f"- Index: `{summary['index_path']}`",
        "",
        "## Preview",
        "",
    ]
    for entry in entries[:20]:
        lines.append(f"- `{entry['entry_id']}` {entry.get('title', '')}")
    return "\n".join(lines) + "\n"


def _render_match_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Source Catalog Matches",
        "",
        f"- Source: `{payload['source_id']}`",
        f"- Match count: `{payload['match_count']}`",
        f"- Terms: `{', '.join(payload.get('terms') or [])}`",
        "",
    ]
    for match in payload.get("matches", [])[:50]:
        lines.extend(
            [
                f"## {match.get('title') or match.get('entry_id')}",
                "",
                f"- Entry: `{match.get('entry_id')}`",
                f"- Score: `{match.get('match_score')}`",
                f"- Reason: `{match.get('match_reason')}`",
                f"- API URLs: `{len(match.get('api_urls') or [])}`",
                f"- File URLs: `{len(match.get('file_urls') or [])}`",
                f"- Doc URLs: `{len(match.get('doc_urls') or [])}`",
                "",
            ]
        )
    return "\n".join(lines)


def _render_draft_report(draft: dict[str, Any]) -> str:
    lines = [
        "# Source Selection Draft",
        "",
        f"- Source catalog: `{draft['source_catalog_id']}`",
        f"- Sources: `{len(draft.get('sources') or [])}`",
        "- Approval required: `true`",
        "",
    ]
    for source in draft.get("sources", [])[:100]:
        lines.extend(
            [
                f"## {source['id']}",
                "",
                f"- Type: `{source['type']}`",
                f"- Target kind: `{source.get('target_kind')}`",
                f"- Approval: `{source.get('approval')}`",
                f"- URL: `{source.get('url')}`",
                f"- Match: `{source.get('match_reason', '')}`",
                "",
            ]
        )
    return "\n".join(lines)


def _render_finalized_report(final: dict[str, Any], previous_path: Path | None) -> str:
    lines = [
        "# Source Selection Finalized",
        "",
        f"- Source catalog: `{final['source_catalog_id']}`",
        f"- Sources: `{len(final.get('sources') or [])}`",
        f"- Finalized at: `{final['finalized_at']}`",
    ]
    if previous_path is not None:
        lines.append(f"- Previous selection backup: `{previous_path}`")
    lines.append("")
    lines.append("All finalized sources still keep their own approval values; sources drafted as `needs_approval` remain gated.")
    lines.append("")
    return "\n".join(lines)


def _provenance(path: Path, *, status: str, source_url: str) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    prov_path = path.with_suffix(path.suffix + ".provenance.json")
    prov = {
        "status": status,
        "path": str(path),
        "source": source_url,
        "sha256": digest,
        "bytes": path.stat().st_size,
        "fetched_at": _now(),
    }
    prov_path.write_text(json.dumps(prov, indent=2), encoding="utf-8")
    return {
        "status": status,
        "materialized_path": str(path),
        "provenance_path": str(prov_path),
        "sha256": digest,
    }


def _render_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Source Catalog Plan",
        "",
        f"- Workspace: `{payload['workspace']}`",
        f"- Selection: `{payload['selection_path']}`",
        f"- Applied: `{payload['applied']}`",
        f"- Sources: `{payload['source_count']}`",
        "",
        "## Actions",
        "",
    ]
    for action in payload["actions"]:
        lines.extend(
            [
                f"### {action['source_id']}",
                "",
                f"- Type: `{action['source_type']}`",
                f"- Status: `{action['status']}`",
                f"- Reason: `{action.get('reason', '')}`",
                f"- Target: `{action.get('target_path') or action.get('fqn') or action.get('path') or ''}`",
                "",
            ]
        )
    if payload["warnings"]:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- `{warning}`" for warning in payload["warnings"])
        lines.append("")
    return "\n".join(lines)


def _safe_name(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")
    return cleaned or "source"


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", value.lower()) if token}


def _find_action(actions: list[dict[str, Any]], source_id: str) -> dict[str, Any]:
    for action in actions:
        if action.get("source_id") == source_id:
            return action
    raise ValueError(f"source not found: {source_id}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()



@anchored("prepare-source-catalog")
def plan_main(argv: list[str] | None = None) -> int:
    parser = _parser("Prepare a governed source catalog plan.")
    args = parser.parse_args(argv)
    result = SourceCatalogManager(args.repo_root, args.workspace, selection_path=args.selection).plan()
    _print_result(result, args.json)
    return 0


@anchored("ingest-source-catalog")
def ingest_main(argv: list[str] | None = None) -> int:
    parser = _parser("Apply an approved source catalog selection.")
    args = parser.parse_args(argv)
    result = SourceCatalogManager(args.repo_root, args.workspace, selection_path=args.selection).ingest()
    _print_result(result, args.json)
    return 0


@anchored("source-catalog")
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Governed source catalog control surface.")
    sub = parser.add_subparsers(dest="command", required=True)

    _add_common(sub.add_parser("plan", help="Write a dry-run source catalog plan."))
    _add_common(sub.add_parser("preflight", help="Check source configuration before ingestion."), include_source=True)
    _add_common(sub.add_parser("api-fetch", help="Fetch approved API sources."), include_source=True)
    _add_common(sub.add_parser("local-stage", help="Copy or register approved local sources."), include_source=True)
    _add_common(sub.add_parser("uc-inspect", help="Inspect approved Databricks UC sources."), include_source=True)
    _add_common(sub.add_parser("discover-docs", help="Discover document links from fetched catalog payloads."), include_source=True)
    index_parser = sub.add_parser("index-catalog", help="Build a compact index from a fetched catalog payload.")
    _add_common(index_parser, include_source=True)
    index_parser.add_argument("--max-entries", type=int, default=1000)
    match_parser = sub.add_parser("match-catalog", help="Match a compact catalog index to workspace signals.")
    _add_common(match_parser, include_source=True)
    match_parser.add_argument("--keyword", action="append", default=[])
    match_parser.add_argument("--min-score", type=int, default=2)
    draft_parser = sub.add_parser("draft-selection", help="Draft source_selection.generated.json from catalog matches.")
    _add_common(draft_parser, include_source=True)
    draft_parser.add_argument("--min-score", type=int, default=2)
    finalize_parser = sub.add_parser("finalize-selection", help="Promote a generated source selection after approval.")
    _add_common(finalize_parser, include_source=True)
    finalize_parser.add_argument("--approve-final-preview", action="store_true")
    _add_common(sub.add_parser("process", help="Classify materialized source outputs."), include_source=True)
    validate_parser = sub.add_parser("validate", help="Validate source catalog artifacts and provenance.")
    _add_common(validate_parser)
    validate_parser.add_argument("--strict", action="store_true", help="Fail partial fetches and warnings.")
    _add_common(sub.add_parser("run", help="Run plan, API, local, UC, process, and validate stages."))

    args = parser.parse_args(argv)
    manager = SourceCatalogManager(args.repo_root, args.workspace, selection_path=args.selection)
    if args.command == "plan":
        result = manager.plan()
    elif args.command == "preflight":
        result = manager.preflight(source_id=args.source)
    elif args.command == "api-fetch":
        result = manager.apply_stage("api", source_id=args.source)
    elif args.command == "local-stage":
        result = manager.apply_stage("local", source_id=args.source)
    elif args.command == "uc-inspect":
        result = manager.apply_stage("databricks_uc", source_id=args.source)
    elif args.command == "discover-docs":
        result = manager.discover_docs(source_id=args.source)
    elif args.command == "index-catalog":
        if not args.source:
            raise ValueError("--source is required for index-catalog")
        result = manager.index_catalog(args.source, max_entries=args.max_entries)
    elif args.command == "match-catalog":
        if not args.source:
            raise ValueError("--source is required for match-catalog")
        result = manager.match_catalog(args.source, keywords=args.keyword, min_score=args.min_score)
    elif args.command == "draft-selection":
        if not args.source:
            raise ValueError("--source is required for draft-selection")
        result = manager.draft_selection(args.source, min_score=args.min_score)
    elif args.command == "finalize-selection":
        if not args.source:
            raise ValueError("--source is required for finalize-selection")
        result = manager.finalize_selection(args.source, approve_final_preview=args.approve_final_preview)
    elif args.command == "process":
        result = manager.process(source_id=args.source)
    elif args.command == "validate":
        result = manager.validate(strict=args.strict)
    elif args.command == "run":
        manager.plan()
        manager.preflight()
        manager.ingest()
        manager.discover_docs()
        manager.process()
        result = manager.validate()
    else:  # pragma: no cover - argparse prevents this
        raise ValueError(f"unsupported command: {args.command}")
    _print_result(result, args.json)
    return 0


def _parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    _add_common(parser)
    return parser


def _add_common(parser: argparse.ArgumentParser, *, include_source: bool = False) -> None:
    parser.add_argument("--workspace", required=True, help="Workspace path, for example workspaces/demo")
    parser.add_argument("--selection", help="Selection JSON path inside the workspace.")
    parser.add_argument("--repo-root", default=".", help="Repository root.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    if include_source:
        parser.add_argument("--source", help="Optional source id to process for this stage.")


def _print_result(result: CatalogResult, as_json: bool) -> None:
    payload = result.to_dict()
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    mode = "APPLIED" if result.apply else "DRY_RUN"
    print(f"{mode}: source catalog for {result.workspace}")
    print(f"Sources: {result.source_count}")
    print(f"Plan: {result.artifacts.get('plan', '')}")
    print(f"Report: {result.artifacts.get('report', '')}")
    blocked = [a for a in result.actions if a.get("status") in {"blocked", "failed"}]
    if blocked:
        print(f"Blocked/failed actions: {len(blocked)}")
    if result.warnings:
        print(f"Warnings: {len(result.warnings)}")
