"""Build bounded context packs from workspace artifacts.

The context layer is derived from canonical artifacts. It does not own KPI
decisions, engine lessons, profiles, or source selections; it indexes them into
small retrievable pages and writes manifests that explain what was loaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


DEFAULT_EXCERPT_CHARS = 1800


@dataclass(frozen=True)
class ContextBudget:
    name: str
    max_sections: int
    max_bytes: int
    max_estimated_tokens: int
    include_long_memory: bool = True

    @classmethod
    def named(
        cls,
        name: str,
        *,
        max_sections: int | None = None,
        max_bytes: int | None = None,
        max_estimated_tokens: int | None = None,
        include_long_memory: bool | None = None,
    ) -> "ContextBudget":
        presets = {
            "small": cls("small", max_sections=8, max_bytes=12_000, max_estimated_tokens=3_000, include_long_memory=False),
            "standard": cls(
                "standard",
                max_sections=24,
                max_bytes=48_000,
                max_estimated_tokens=12_000,
                include_long_memory=True,
            ),
            "deep": cls("deep", max_sections=80, max_bytes=160_000, max_estimated_tokens=40_000, include_long_memory=True),
        }
        if name not in presets:
            raise ValueError(f"unsupported context budget: {name}")
        base = presets[name]
        return cls(
            name=name,
            max_sections=max_sections or base.max_sections,
            max_bytes=max_bytes or base.max_bytes,
            max_estimated_tokens=max_estimated_tokens or base.max_estimated_tokens,
            include_long_memory=base.include_long_memory if include_long_memory is None else include_long_memory,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextSelection:
    index_path: str
    pages_path: str
    manifest_path: str
    wiki_path: str
    selected_sections: int
    skipped_sections: int
    budget: ContextBudget

    def summary(self) -> dict[str, Any]:
        data = asdict(self)
        data["budget"] = self.budget.to_dict()
        return data


class ContextRouter:
    """Indexes workspace context and creates task-specific bounded manifests."""

    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.context_dir = self.layout.generated_dir / "context"
        self.manifest_dir = self.context_dir / "manifests"
        self.report_dir = self.layout.reports_dir / "context"

    def build(
        self,
        *,
        task: str,
        budget: ContextBudget | None = None,
        refresh: str = "safe",
    ) -> ContextSelection:
        if refresh not in {"never", "safe", "force"}:
            raise ValueError("refresh must be one of: never, safe, force")
        budget = budget or ContextBudget.named("standard")
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        self.context_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)

        pages = self._pages()
        selected, skipped = self._select_pages(pages, task=task, budget=budget)
        index_path = self.context_dir / "context_index.json"
        pages_path = self.context_dir / "context_pages.jsonl"
        manifest_path = self.manifest_dir / f"{_safe_name(task)}_{budget.name}.json"
        wiki_path = self.report_dir / f"{_safe_name(task)}_{budget.name}.md"

        index = self._index_payload(pages)
        index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        with pages_path.open("w", encoding="utf-8") as handle:
            for page in pages:
                handle.write(json.dumps(page, sort_keys=True) + "\n")

        manifest = self._manifest_payload(
            task=task,
            budget=budget,
            selected=selected,
            skipped=skipped,
            index_path=index_path,
            pages_path=pages_path,
            wiki_path=wiki_path,
            refresh=refresh,
        )
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        wiki_path.write_text(_render_wiki(manifest), encoding="utf-8")
        return ContextSelection(
            index_path=_rel(index_path, self.repo_root),
            pages_path=_rel(pages_path, self.repo_root),
            manifest_path=_rel(manifest_path, self.repo_root),
            wiki_path=_rel(wiki_path, self.repo_root),
            selected_sections=len(selected),
            skipped_sections=len(skipped),
            budget=budget,
        )

    def _pages(self) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        artifact_specs = [
            ("contracts", self.layout.contracts_dir / "kpi_feature_mapping.json", "kpi_mapping", True),
            ("contracts", self.layout.contracts_dir / "domain_model.json", "domain_model", False),
            ("contracts", self.layout.contracts_dir / "relationship_contracts.json", "relationships", False),
            ("profiles", self.layout.profiles_dir / "profile_index.json", "profiles", True),
            ("evidence", self.layout.evidence_dir / "resource_preflight.json", "resource", False),
            ("requirements", self.layout.requirements_dir / "source_catalog_plan.json", "source_catalog", False),
            ("memory", self.layout.memory_dir / "engine_evolution.json", "engine_memory", False),
            ("memory", self.layout.memory_dir / "kpi_generation_workspace_memory.json", "kpi_memory", False),
            ("memory", self.layout.memory_dir / "lessons.json", "lessons", False),
            ("memory", self.layout.memory_dir / "genie_workspace_decisions.json", "genie_memory", False),
        ]
        for family, path, topic, required_for_planning in artifact_specs:
            pages.extend(
                self._json_artifact_pages(
                    path,
                    family=family,
                    topic=topic,
                    required_for_planning=required_for_planning,
                )
            )
        for path in sorted(self.layout.memory_dir.glob("*.md")):
            pages.extend(self._text_artifact_pages(path, family="memory", topic=path.stem))
        return pages

    def _json_artifact_pages(
        self,
        path: Path,
        *,
        family: str,
        topic: str,
        required_for_planning: bool,
    ) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return [
                self._page(
                    path,
                    section=f"{topic}:invalid_json",
                    family=family,
                    topic=topic,
                    title=f"{path.name} invalid JSON",
                    summary="Artifact exists but could not be parsed as JSON.",
                    priority=95 if required_for_planning else 30,
                    tags=["invalid_json", topic],
                    payload={"error": "json_decode_error"},
                )
            ]
        pages = [
            self._page(
                path,
                section=f"{topic}:summary",
                family=family,
                topic=topic,
                title=f"{path.name} summary",
                summary=_artifact_summary(topic, data),
                priority=100 if required_for_planning else _topic_priority(topic),
                tags=[topic, "summary"],
                payload=_compact_payload(data),
            )
        ]
        if topic == "profiles":
            for profile in data.get("profiles", []) if isinstance(data, dict) else []:
                dataset = str(profile.get("path") or profile.get("dataset") or "unknown")
                pages.append(
                    self._page(
                        path,
                        section=f"profiles:{_safe_name(dataset)}",
                        family=family,
                        topic=topic,
                        title=f"Profile {dataset}",
                        summary=_profile_summary(profile),
                        priority=86,
                        tags=["profile", "dataset", dataset],
                        payload=_compact_payload(profile),
                    )
                )
        elif topic == "kpi_mapping":
            for kpi in data.get("kpis", []) if isinstance(data, dict) else []:
                kpi_id = str(kpi.get("kpi_id") or kpi.get("id") or "kpi")
                pages.append(
                    self._page(
                        path,
                        section=f"kpi_mapping:{_safe_name(kpi_id)}",
                        family=family,
                        topic=topic,
                        title=f"KPI mapping {kpi_id}",
                        summary=_kpi_summary(kpi),
                        priority=94,
                        tags=["kpi", "mapping", kpi_id],
                        payload=_compact_payload(kpi),
                    )
                )
        elif topic == "relationships":
            for rel in data.get("relationships", []) if isinstance(data, dict) else []:
                rel_id = str(rel.get("relationship_id") or f"{rel.get('left_dataset', '')}:{rel.get('right_dataset', '')}")
                pages.append(
                    self._page(
                        path,
                        section=f"relationships:{_safe_name(rel_id)}",
                        family=family,
                        topic=topic,
                        title=f"Relationship {rel_id}",
                        summary=_relationship_summary(rel),
                        priority=84,
                        tags=["relationship", "join", str(rel.get("state", ""))],
                        payload=_compact_payload(rel),
                    )
                )
        elif topic == "engine_memory":
            for idx, lesson in enumerate(data.get("lessons", []) if isinstance(data, dict) else []):
                engine = str(lesson.get("engine") or "engine")
                family_name = str(lesson.get("workload_family") or idx)
                pages.append(
                    self._page(
                        path,
                        section=f"engine_memory:{_safe_name(engine)}:{_safe_name(family_name)}",
                        family=family,
                        topic=topic,
                        title=f"Engine lesson {engine} {family_name}",
                        summary=_engine_lesson_summary(lesson),
                        priority=78,
                        tags=["engine", "memory", engine, family_name],
                        payload=_compact_payload(lesson),
                    )
                )
        return pages

    def _text_artifact_pages(self, path: Path, *, family: str, topic: str) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks = _markdown_sections(text)
        if not chunks:
            chunks = [(topic, text)]
        pages = []
        for idx, (heading, body) in enumerate(chunks[:20]):
            section = f"{topic}:{_safe_name(heading or str(idx))}"
            pages.append(
                self._page(
                    path,
                    section=section,
                    family=family,
                    topic=topic,
                    title=heading or f"{path.name} section {idx + 1}",
                    summary=_truncate(" ".join(body.split()), 500),
                    priority=55,
                    tags=[topic, "long_memory", "markdown"],
                    payload={"excerpt": _truncate(body, DEFAULT_EXCERPT_CHARS)},
                )
            )
        return pages

    def _page(
        self,
        path: Path,
        *,
        section: str,
        family: str,
        topic: str,
        title: str,
        summary: str,
        priority: int,
        tags: list[str],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        artifact_rel = _rel(path, self.repo_root)
        excerpt = _truncate(json.dumps(payload, sort_keys=True, default=str), DEFAULT_EXCERPT_CHARS)
        page = {
            "page_id": f"{topic}.{_safe_name(section)}",
            "section": section,
            "family": family,
            "topic": topic,
            "title": title,
            "summary": summary,
            "artifact_path": artifact_rel,
            "artifact_sha256": _sha256(path),
            "artifact_mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "priority": priority,
            "tags": sorted({tag for tag in tags if tag}),
            "estimated_tokens": _estimate_tokens(summary) + _estimate_tokens(excerpt),
            "byte_size": len(summary.encode("utf-8")) + len(excerpt.encode("utf-8")),
            "excerpt": excerpt,
        }
        return page

    def _select_pages(
        self,
        pages: list[dict[str, Any]],
        *,
        task: str,
        budget: ContextBudget,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        wanted = _task_topics(task)
        ranked = []
        skipped = []
        for page in pages:
            if not budget.include_long_memory and "long_memory" in page.get("tags", []):
                skipped.append({**_skip_stub(page), "reason": "long_memory_excluded_by_budget"})
                continue
            score = int(page.get("priority") or 0)
            topic = str(page.get("topic") or "")
            family = str(page.get("family") or "")
            if topic in wanted["required_topics"]:
                score += 100
            if topic in wanted["optional_topics"] or family in wanted["optional_families"]:
                score += 35
            if topic not in wanted["required_topics"] and topic not in wanted["optional_topics"] and family not in wanted["optional_families"]:
                score -= 25
            ranked.append((score, str(page.get("page_id", "")), page))
        selected = []
        used_bytes = 0
        used_tokens = 0
        for score, _, page in sorted(ranked, key=lambda item: (-item[0], item[1])):
            projected_bytes = used_bytes + int(page.get("byte_size") or 0)
            projected_tokens = used_tokens + int(page.get("estimated_tokens") or 0)
            if len(selected) >= budget.max_sections:
                skipped.append({**_skip_stub(page), "reason": "max_sections"})
                continue
            if projected_bytes > budget.max_bytes:
                skipped.append({**_skip_stub(page), "reason": "max_bytes"})
                continue
            if projected_tokens > budget.max_estimated_tokens:
                skipped.append({**_skip_stub(page), "reason": "max_estimated_tokens"})
                continue
            selected.append(page)
            used_bytes = projected_bytes
            used_tokens = projected_tokens
        return selected, skipped

    def _index_payload(self, pages: list[dict[str, Any]]) -> dict[str, Any]:
        by_topic: dict[str, int] = {}
        by_family: dict[str, int] = {}
        for page in pages:
            by_topic[str(page.get("topic", ""))] = by_topic.get(str(page.get("topic", "")), 0) + 1
            by_family[str(page.get("family", ""))] = by_family.get(str(page.get("family", "")), 0) + 1
        return {
            "artifact_type": "context_index.json",
            "version": 1,
            "generated_by": "context-router",
            "workspace": self.workspace_rel,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "page_count": len(pages),
            "by_topic": dict(sorted(by_topic.items())),
            "by_family": dict(sorted(by_family.items())),
            "pages": [
                {
                    "page_id": page["page_id"],
                    "section": page["section"],
                    "topic": page["topic"],
                    "family": page["family"],
                    "artifact_path": page["artifact_path"],
                    "priority": page["priority"],
                    "tags": page["tags"],
                    "summary": page["summary"],
                }
                for page in pages
            ],
        }

    def _manifest_payload(
        self,
        *,
        task: str,
        budget: ContextBudget,
        selected: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        index_path: Path,
        pages_path: Path,
        wiki_path: Path,
        refresh: str,
    ) -> dict[str, Any]:
        return {
            "artifact_type": "context_manifest.json",
            "version": 1,
            "generated_by": "context-router",
            "workspace": self.workspace_rel,
            "task": task,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "refresh_policy": refresh,
            "budget": budget.to_dict(),
            "index_path": _rel(index_path, self.repo_root),
            "pages_path": _rel(pages_path, self.repo_root),
            "wiki_path": _rel(wiki_path, self.repo_root),
            "summary": {
                "selected_sections": len(selected),
                "skipped_sections": len(skipped),
                "selected_bytes": sum(int(page.get("byte_size") or 0) for page in selected),
                "selected_estimated_tokens": sum(int(page.get("estimated_tokens") or 0) for page in selected),
                "required_topics": sorted(_task_topics(task)["required_topics"]),
            },
            "selected_pages": [
                {
                    "page_id": page["page_id"],
                    "section": page["section"],
                    "topic": page["topic"],
                    "family": page["family"],
                    "artifact_path": page["artifact_path"],
                    "artifact_sha256": page["artifact_sha256"],
                    "title": page["title"],
                    "summary": page["summary"],
                    "tags": page["tags"],
                    "estimated_tokens": page["estimated_tokens"],
                    "byte_size": page["byte_size"],
                    "excerpt": page["excerpt"],
                }
                for page in selected
            ],
            "skipped_pages": skipped[:200],
            "usage_trace": {
                "mode": "bounded_retrieval",
                "canonical_ownership": "source artifacts remain authoritative; context pages are derived.",
            },
        }

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")


def _task_topics(task: str) -> dict[str, set[str]]:
    normalized = task.replace("_", "-").lower()
    if normalized in {"plan-source-to-target", "source-to-target-planner"}:
        return {
            "required_topics": {"kpi_mapping", "profiles"},
            "optional_topics": {"domain_model", "relationships", "engine_memory", "resource", "source_catalog"},
            "optional_families": {"memory"},
        }
    if normalized in {"generate-kpi-sql", "kpi-sql"}:
        return {
            "required_topics": {"kpi_mapping", "profiles", "relationships"},
            "optional_topics": {"domain_model", "engine_memory", "resource"},
            "optional_families": {"memory"},
        }
    return {
        "required_topics": set(),
        "optional_topics": {"kpi_mapping", "profiles", "domain_model", "relationships", "engine_memory", "resource"},
        "optional_families": {"memory", "contracts", "profiles"},
    }


def _artifact_summary(topic: str, data: Any) -> str:
    if not isinstance(data, dict):
        return f"{topic} artifact with non-object JSON root."
    if topic == "kpi_mapping":
        summary = data.get("summary") or {}
        return (
            f"KPI mapping with {len(data.get('kpis', []))} KPIs; "
            f"ready={summary.get('ready_kpi_count', 'unknown')}, "
            f"blocked={summary.get('blocked_kpi_count', 'unknown')}."
        )
    if topic == "profiles":
        return f"Profile index with {len(data.get('profiles', []))} profiled datasets."
    if topic == "relationships":
        return f"Relationship contracts with {len(data.get('relationships', []))} relationships."
    if topic == "engine_memory":
        return f"Engine evolution memory with {len(data.get('records', []))} records and {len(data.get('lessons', []))} lessons."
    if topic == "resource":
        decision = data.get("decision") or data.get("resource_decision") or {}
        return f"Resource preflight status={decision.get('status', data.get('status', 'unknown'))} mode={decision.get('mode', data.get('mode', 'unknown'))}."
    return f"{topic} artifact with keys: {', '.join(sorted(data.keys())[:12])}."


def _profile_summary(profile: dict[str, Any]) -> str:
    schema = profile.get("schema") or {}
    return (
        f"{profile.get('path', profile.get('dataset', 'dataset'))}: "
        f"rows={profile.get('row_count', 'unknown')}, columns={len(schema)}, "
        f"size_bytes={profile.get('size_bytes', 'unknown')}."
    )


def _kpi_summary(kpi: dict[str, Any]) -> str:
    features = kpi.get("features") or []
    unresolved = [str(f.get("feature", "")) for f in features if "ready" not in str(f.get("state", "")).lower()]
    return (
        f"{kpi.get('kpi_id', kpi.get('id', 'kpi'))}: {kpi.get('name', kpi.get('business_question', ''))}; "
        f"features={len(features)}, unresolved={len([f for f in unresolved if f])}."
    )


def _relationship_summary(rel: dict[str, Any]) -> str:
    return (
        f"{rel.get('left_dataset', '')}.{rel.get('left_column', '')} -> "
        f"{rel.get('right_dataset', '')}.{rel.get('right_column', '')}; state={rel.get('state', 'unknown')}."
    )


def _engine_lesson_summary(lesson: dict[str, Any]) -> str:
    return (
        f"engine={lesson.get('engine', 'unknown')} workload={lesson.get('workload_family', 'unknown')} "
        f"confidence={lesson.get('confidence', 'unknown')} score={lesson.get('score', 'unknown')}."
    )


def _compact_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"value": value}
    keep = {}
    for key, item in value.items():
        if key in {"records"} and isinstance(item, list):
            keep[key] = item[-5:]
        elif isinstance(item, list):
            keep[key] = item[:20]
        elif isinstance(item, dict):
            keep[key] = {k: v for idx, (k, v) in enumerate(item.items()) if idx < 40}
        else:
            keep[key] = item
    return keep


def _markdown_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            if current_lines:
                sections.append((current_heading, current_lines))
            current_heading = line.strip("# ").strip() or "section"
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_heading, current_lines))
    return [(heading, "\n".join(lines).strip()) for heading, lines in sections if "\n".join(lines).strip()]


def _render_wiki(manifest: dict[str, Any]) -> str:
    lines = [
        "# Context Pack",
        "",
        f"- Workspace: `{manifest.get('workspace', '')}`",
        f"- Task: `{manifest.get('task', '')}`",
        f"- Budget: `{manifest.get('budget', {}).get('name', '')}`",
        f"- Selected sections: `{manifest.get('summary', {}).get('selected_sections', 0)}`",
        f"- Skipped sections: `{manifest.get('summary', {}).get('skipped_sections', 0)}`",
        "",
        "## Page Index",
        "",
    ]
    for page in manifest.get("selected_pages", []):
        lines.extend(
            [
                f"### {page.get('page_id', '')}",
                "",
                f"- Artifact: `{page.get('artifact_path', '')}`",
                f"- Topic: `{page.get('topic', '')}`",
                f"- Tags: {', '.join(page.get('tags', []))}",
                "",
                page.get("summary", ""),
                "",
            ]
        )
    if manifest.get("skipped_pages"):
        lines.extend(["## Skipped", ""])
        for page in manifest["skipped_pages"][:25]:
            lines.append(f"- `{page.get('page_id', '')}`: {page.get('reason', '')}")
    return "\n".join(lines).rstrip() + "\n"


def _skip_stub(page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page_id": page.get("page_id", ""),
        "topic": page.get("topic", ""),
        "family": page.get("family", ""),
        "artifact_path": page.get("artifact_path", ""),
    }


def _topic_priority(topic: str) -> int:
    return {
        "domain_model": 82,
        "relationships": 82,
        "resource": 70,
        "engine_memory": 68,
        "source_catalog": 58,
        "kpi_memory": 52,
        "lessons": 50,
    }.get(topic, 40)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "...[truncated]"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    return safe.strip("_")[:120] or "item"


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a bounded context pack for a workspace task.")
    parser.add_argument("command", choices=["build"], help="Context router command.")
    parser.add_argument("--workspace", required=True, help="Workspace path relative to repo root.")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--task", required=True, help="Task name, for example plan-source-to-target.")
    parser.add_argument("--budget", choices=["small", "standard", "deep"], default="standard")
    parser.add_argument("--max-sections", type=int)
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--max-estimated-tokens", type=int)
    parser.add_argument("--include-long-memory", action="store_true")
    parser.add_argument("--exclude-long-memory", action="store_true")
    parser.add_argument("--refresh", choices=["never", "safe", "force"], default="safe")
    args = parser.parse_args(argv)
    include_long_memory = None
    if args.include_long_memory:
        include_long_memory = True
    if args.exclude_long_memory:
        include_long_memory = False
    budget = ContextBudget.named(
        args.budget,
        max_sections=args.max_sections,
        max_bytes=args.max_bytes,
        max_estimated_tokens=args.max_estimated_tokens,
        include_long_memory=include_long_memory,
    )
    result = ContextRouter(args.repo_root, args.workspace).build(
        task=args.task,
        budget=budget,
        refresh=args.refresh,
    )
    print(json.dumps(result.summary(), indent=2))
    return 0
