"""Build a workspace evidence graph from generated artifacts."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.onboarding.harness.trajectory_recorder import load_trajectory
from core.presentation.console_tables import render_markdown_table
from core.storage.workspace_layout import WorkspaceLayout


GRAPH_VERSION = 1


@dataclass(frozen=True)
class EvidenceGraphResult:
    workspace: str
    ok: bool
    graph_path: str
    current_markdown_path: str
    node_count: int
    edge_count: int
    introduced_term_count: int

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "workspace_evidence_graph_result",
            "version": GRAPH_VERSION,
            "generated_by": "build-workspace-evidence-graph",
            **asdict(self),
        }


@dataclass(frozen=True)
class EvidenceGraphHealthResult:
    workspace: str
    ok: bool
    status: str
    graph_path: str
    node_count: int
    edge_count: int
    warning_count: int
    warnings: list[str]

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "workspace_evidence_graph_health_result",
            "version": GRAPH_VERSION,
            "generated_by": "build-workspace-evidence-graph",
            **asdict(self),
        }


@dataclass(frozen=True)
class EvidenceGraphQueryResult:
    workspace: str
    ok: bool
    query_type: str
    query_value: str
    graph_path: str
    result_count: int
    results: list[dict[str, Any]]

    def summary(self) -> dict[str, Any]:
        return {
            "artifact_type": "workspace_evidence_graph_query_result",
            "version": GRAPH_VERSION,
            "generated_by": "query-workspace-evidence-graph",
            **asdict(self),
        }


class WorkspaceEvidenceGraphBuilder:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []

    def build(self) -> EvidenceGraphResult:
        self._validate_workspace()
        self.layout.ensure_runtime_dirs()
        self._add_workspace()
        self._add_profiles()
        self._add_kpis_and_mappings()
        self._add_workspace_definitions()
        self._add_relationships()
        self._add_source_to_target_plan()
        self._add_sql_files()
        self._add_reports()
        self._add_trajectory()
        self._add_harness_findings()
        graph = {
            "artifact_type": "workspace_evidence_graph/graph.json",
            "version": GRAPH_VERSION,
            "generated_by": "build-workspace-evidence-graph",
            "workspace": self.workspace_rel,
            "generated_at": _now(),
            "summary": {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "introduced_term_count": len(self._introduced_terms()),
            },
            "nodes": sorted(self.nodes.values(), key=lambda item: item["id"]),
            "edges": sorted(self.edges, key=lambda item: (item["source"], item["type"], item["target"])),
            "queries": {
                "introduced_terms": self._introduced_terms(),
                "feature_impact": self._feature_impact(),
                "column_impact": self._column_impact(),
            },
        }
        graph_dir = self.layout.generated_dir / "evidence_graph"
        report_dir = self.layout.reports_dir / "evidence_graph"
        graph_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        graph_path = graph_dir / "graph.json"
        md_path = report_dir / "current.md"
        graph_path.write_text(json.dumps(graph, indent=2, default=str) + "\n", encoding="utf-8")
        md_path.write_text(_render_markdown(graph), encoding="utf-8")
        return EvidenceGraphResult(
            workspace=self.workspace_rel,
            ok=True,
            graph_path=_rel(graph_path, self.repo_root),
            current_markdown_path=_rel(md_path, self.repo_root),
            node_count=len(self.nodes),
            edge_count=len(self.edges),
            introduced_term_count=len(graph["queries"]["introduced_terms"]),
        )

    def health(self) -> EvidenceGraphHealthResult:
        result = self.build()
        graph = _load_json(self.repo_root / result.graph_path)
        warnings = []
        kinds = _node_kind_counts(graph)
        if result.node_count <= 1:
            warnings.append("Evidence graph has only the workspace node; generated artifacts are probably missing.")
        if not kinds.get("kpi") and (self.layout.contracts_dir / "kpi_registry.json").exists():
            warnings.append("KPI registry exists but no KPI nodes were built.")
        if not kinds.get("column") and (self.layout.profiles_dir / "profile_index.json").exists():
            warnings.append("Profile index exists but no column nodes were built.")
        if not graph.get("queries", {}).get("introduced_terms") and (self.layout.contracts_dir / "kpi_registry.json").exists():
            warnings.append("KPI registry exists but no introduced terms were detected.")
        return EvidenceGraphHealthResult(
            workspace=self.workspace_rel,
            ok=True,
            status="passed" if not warnings else "warning",
            graph_path=result.graph_path,
            node_count=result.node_count,
            edge_count=result.edge_count,
            warning_count=len(warnings),
            warnings=warnings,
        )

    def _validate_workspace(self) -> None:
        if not self.workspace.exists():
            raise FileNotFoundError(f"workspace not found: {self.workspace_rel}")
        if self.workspace == self.repo_root or not self.workspace.is_relative_to(self.repo_root):
            raise ValueError(f"workspace must be inside repo root: {self.workspace}")

    def _add_workspace(self) -> None:
        self._node("workspace", self.workspace_rel, "workspace", {"path": self.workspace_rel})

    def _add_profiles(self) -> None:
        profile_index_path = self.layout.profiles_dir / "profile_index.json"
        profile_index = _load_json(profile_index_path)
        if not profile_index:
            return
        profile_node = self._artifact_node(profile_index_path, "profile_index")
        self._edge(self._workspace_id(), "has_artifact", profile_node)
        for profile in profile_index.get("profiles") or []:
            if not isinstance(profile, dict):
                continue
            dataset = str(profile.get("path") or profile.get("dataset") or "")
            if not dataset:
                continue
            dataset_node = self._node("dataset", dataset, "dataset", {"path": dataset})
            self._edge(self._workspace_id(), "has_dataset", dataset_node)
            self._edge(dataset_node, "profiled_by", profile_node)
            columns = profile.get("columns") or []
            if isinstance(profile.get("schema"), dict):
                columns = [{"name": name, "dtype": dtype} for name, dtype in profile["schema"].items()]
            for column in columns:
                if not isinstance(column, dict):
                    continue
                name = str(column.get("name") or "")
                if not name:
                    continue
                column_node = self._node(
                    "column",
                    f"{dataset}.{name}",
                    "column",
                    {"dataset": dataset, "column": name, "dtype": column.get("dtype") or column.get("type")},
                )
                self._edge(dataset_node, "has_column", column_node)
                self._edge(column_node, "profiled_by", profile_node)

    def _add_kpis_and_mappings(self) -> None:
        registry_path = self.layout.contracts_dir / "kpi_registry.json"
        mapping_path = self.layout.contracts_dir / "kpi_feature_mapping.json"
        registry = _load_json(registry_path)
        mapping = _load_json(mapping_path)
        if registry:
            artifact = self._artifact_node(registry_path, "kpi_registry")
            self._edge(self._workspace_id(), "has_artifact", artifact)
            for index, kpi in enumerate(registry.get("kpis") or [], start=1):
                if not isinstance(kpi, dict):
                    continue
                kpi_id = str(kpi.get("kpi_id") or f"kpi_{index:03d}")
                kpi_node = self._node(
                    "kpi",
                    kpi_id,
                    "kpi",
                    {
                        "kpi_id": kpi_id,
                        "name": kpi.get("name"),
                        "metric": kpi.get("metric"),
                        "cuts": kpi.get("cuts"),
                        "source": kpi.get("source"),
                    },
                )
                self._edge(artifact, "defines", kpi_node)
                for term in _split_terms(str(kpi.get("cuts") or "")):
                    term_node = self._node("term", term, "term", {"term": term, "source": "kpi_registry.cuts"})
                    self._edge(kpi_node, "mentions_term", term_node)
                    self._edge(artifact, "introduced_term", term_node)
                metric = str(kpi.get("metric") or "").strip()
                if metric:
                    term_node = self._node("term", metric, "term", {"term": metric, "source": "kpi_registry.metric"})
                    self._edge(kpi_node, "mentions_term", term_node)
                    self._edge(artifact, "introduced_term", term_node)
        if mapping:
            artifact = self._artifact_node(mapping_path, "kpi_feature_mapping")
            self._edge(self._workspace_id(), "has_artifact", artifact)
            for kpi in mapping.get("kpis") or []:
                if not isinstance(kpi, dict):
                    continue
                kpi_id = str(kpi.get("kpi_id") or "")
                if not kpi_id:
                    continue
                kpi_node = self._node("kpi", kpi_id, "kpi", {"kpi_id": kpi_id})
                self._edge(artifact, "maps_kpi", kpi_node)
                for feature in kpi.get("features") or []:
                    if isinstance(feature, dict):
                        self._add_feature_mapping(kpi_node, feature, artifact)

    def _add_feature_mapping(self, kpi_node: str, feature: dict[str, Any], artifact_node: str) -> None:
        name = str(feature.get("feature") or feature.get("name") or "")
        if not name:
            return
        feature_node = self._node(
            "feature",
            name,
            "feature",
            {
                "feature": name,
                "state": feature.get("state"),
                "evidence_state": feature.get("evidence_state"),
                "confidence": feature.get("confidence"),
            },
        )
        self._edge(kpi_node, "requires_feature", feature_node)
        self._edge(artifact_node, "introduced_feature", feature_node)
        for key in ("accepted_mapping", "candidate", "mapping", "column"):
            value = feature.get(key)
            if isinstance(value, dict):
                self._link_feature_to_column(feature_node, value, artifact_node)
            elif isinstance(value, str) and value:
                column_node = self._node("column", value, "column", {"column": value})
                self._edge(feature_node, "maps_to", column_node)
                self._edge(artifact_node, "supports_mapping", column_node)
        for option in feature.get("options") or feature.get("candidates") or []:
            if isinstance(option, dict):
                self._link_feature_to_column(feature_node, option, artifact_node, edge_type="candidate_for")

    def _link_feature_to_column(
        self,
        feature_node: str,
        mapping: dict[str, Any],
        artifact_node: str,
        *,
        edge_type: str = "maps_to",
    ) -> None:
        dataset = str(mapping.get("dataset") or mapping.get("path") or "")
        column = str(mapping.get("column") or mapping.get("field") or "")
        if not column:
            return
        identity = f"{dataset}.{column}" if dataset else column
        column_node = self._node("column", identity, "column", {"dataset": dataset, "column": column})
        self._edge(feature_node, edge_type, column_node)
        self._edge(artifact_node, "supports_mapping", column_node)

    def _add_workspace_definitions(self) -> None:
        path = self.layout.contracts_dir / "workspace_feature_definitions.json"
        payload = _load_json(path)
        if not payload:
            return
        artifact = self._artifact_node(path, "workspace_feature_definitions")
        self._edge(self._workspace_id(), "has_artifact", artifact)
        definitions = payload.get("definitions") or payload.get("features") or {}
        if isinstance(definitions, dict):
            iterable = definitions.items()
        else:
            iterable = ((str(item.get("feature") or item.get("name") or ""), item) for item in definitions if isinstance(item, dict))
        for name, definition in iterable:
            if not name:
                continue
            feature_node = self._node("feature", str(name), "feature", {"feature": str(name)})
            self._edge(artifact, "defines_feature", feature_node)
            if isinstance(definition, dict):
                self._link_feature_to_column(feature_node, definition, artifact)

    def _add_relationships(self) -> None:
        path = self.layout.contracts_dir / "relationship_contracts.json"
        payload = _load_json(path)
        if not payload:
            return
        artifact = self._artifact_node(path, "relationship_contracts")
        self._edge(self._workspace_id(), "has_artifact", artifact)
        for index, relationship in enumerate(payload.get("relationships") or [], start=1):
            if not isinstance(relationship, dict):
                continue
            rel_node = self._node(
                "relationship",
                str(relationship.get("relationship_id") or f"relationship_{index}"),
                "relationship",
                relationship,
            )
            self._edge(artifact, "defines_relationship", rel_node)

    def _add_source_to_target_plan(self) -> None:
        path = self.layout.contracts_dir / "source_to_target_plan.json"
        payload = _load_json(path)
        if not payload:
            return
        artifact = self._artifact_node(path, "source_to_target_plan")
        self._edge(self._workspace_id(), "has_artifact", artifact)
        for kpi in payload.get("kpis") or []:
            if not isinstance(kpi, dict):
                continue
            kpi_id = str(kpi.get("kpi_id") or "")
            if kpi_id:
                kpi_node = self._node("kpi", kpi_id, "kpi", {"kpi_id": kpi_id})
                self._edge(artifact, "plans_for", kpi_node)

    def _add_sql_files(self) -> None:
        if not self.layout.solutions_dir.exists():
            return
        for sql_file in sorted(self.layout.solutions_dir.glob("kpi_*.sql")):
            artifact = self._artifact_node(sql_file, "sql")
            kpi_id = sql_file.stem
            kpi_node = self._node("kpi", kpi_id, "kpi", {"kpi_id": kpi_id})
            self._edge(kpi_node, "has_sql", artifact)
            sql_text = sql_file.read_text(encoding="utf-8", errors="ignore")
            for quoted in re.findall(r'"([^"]+)"', sql_text):
                if quoted.lower().startswith("kpi_"):
                    continue
                term_node = self._node("term", quoted, "term", {"term": quoted, "source": "sql_identifier"})
                self._edge(artifact, "references_identifier", term_node)

    def _add_reports(self) -> None:
        report_paths = [
            self.layout.reports_dir / "blocker_question_panel" / "current.json",
            self.layout.reports_dir / "workflow_guard_harness" / "current.json",
            self.layout.reports_dir / "ai_app_harness" / "current.json",
            self.layout.reports_dir / "ai_cli_harness" / "current.json",
        ]
        for path in report_paths:
            payload = _load_json(path)
            if not payload:
                continue
            artifact = self._artifact_node(path, path.parent.name)
            self._edge(self._workspace_id(), "has_report", artifact)
            feature = str(payload.get("feature") or "")
            if feature:
                feature_node = self._node("feature", feature, "feature", {"feature": feature})
                self._edge(artifact, "asks_about" if "blocker_question_panel" in path.as_posix() else "references_feature", feature_node)

    def _add_trajectory(self) -> None:
        path = self.layout.state_dir / "trajectory.jsonl"
        records = load_trajectory(path)
        if not records:
            return
        artifact = self._artifact_node(path, "trajectory")
        self._edge(self._workspace_id(), "has_artifact", artifact)
        for index, record in enumerate(records, start=1):
            event_node = self._node(
                "trajectory_event",
                f"{index}:{record.get('event_type', 'event')}:{record.get('timestamp', '')}",
                "trajectory_event",
                {
                    "event_type": record.get("event_type"),
                    "status": record.get("status"),
                    "summary": record.get("summary"),
                    "timestamp": record.get("timestamp"),
                    "artifact": record.get("artifact"),
                    "validation": record.get("validation"),
                },
            )
            self._edge(artifact, "contains_event", event_node)
            if record.get("artifact"):
                self._edge(event_node, "wrote_or_used", self._artifact_node(self.repo_root / str(record["artifact"]), "artifact"))

    def _add_harness_findings(self) -> None:
        for path in [
            self.layout.reports_dir / "workflow_guard_harness" / "current.json",
            self.layout.reports_dir / "ai_app_harness" / "current.json",
            self.layout.reports_dir / "ai_cli_harness" / "current.json",
            self.layout.reports_dir / "project_harness.md",
        ]:
            if not path.exists():
                continue
            artifact = self._artifact_node(path, "harness")
            if path.suffix == ".json":
                payload = _load_json(path)
                for finding in payload.get("findings") or []:
                    if isinstance(finding, dict):
                        finding_node = self._node(
                            "harness_finding",
                            f"{path.as_posix()}:{finding.get('code')}:{finding.get('message')}",
                            "harness_finding",
                            finding,
                        )
                        self._edge(artifact, "reported_finding", finding_node)

    def _artifact_node(self, path: Path, kind: str) -> str:
        rel = _rel(path, self.repo_root)
        return self._node("artifact", rel, "artifact", {"path": rel, "kind": kind, "exists": path.exists()})

    def _workspace_id(self) -> str:
        return _node_id("workspace", self.workspace_rel)

    def _node(self, kind: str, identity: str, label: str, properties: dict[str, Any]) -> str:
        node_id = _node_id(kind, identity)
        current = self.nodes.get(node_id, {})
        merged = {**(current.get("properties") or {}), **{key: value for key, value in properties.items() if value not in (None, "")}}
        self.nodes[node_id] = {
            "id": node_id,
            "kind": kind,
            "label": label,
            "identity": identity,
            "properties": merged,
        }
        return node_id

    def _edge(self, source: str, edge_type: str, target: str) -> None:
        edge = {"source": source, "type": edge_type, "target": target}
        if edge not in self.edges:
            self.edges.append(edge)

    def _introduced_terms(self) -> list[dict[str, Any]]:
        rows = []
        term_nodes = {node["id"]: node for node in self.nodes.values() if node["kind"] in {"term", "feature"}}
        incoming: dict[str, list[dict[str, Any]]] = {}
        outgoing: dict[str, list[dict[str, Any]]] = {}
        for edge in self.edges:
            incoming.setdefault(edge["target"], []).append(edge)
            outgoing.setdefault(edge["source"], []).append(edge)
        for node_id, node in term_nodes.items():
            introducers = [edge["source"] for edge in incoming.get(node_id, []) if edge["type"].startswith("introduced")]
            users = [edge["source"] for edge in incoming.get(node_id, []) if edge["type"] in {"mentions_term", "requires_feature", "asks_about"}]
            rows.append(
                {
                    "term": node["identity"],
                    "kind": node["kind"],
                    "introduced_by": introducers,
                    "used_by": users,
                    "outgoing_edge_count": len(outgoing.get(node_id, [])),
                }
            )
        return sorted(rows, key=lambda item: (item["kind"], item["term"].lower()))

    def _feature_impact(self) -> dict[str, Any]:
        impact: dict[str, Any] = {}
        for node in self.nodes.values():
            if node["kind"] != "feature":
                continue
            incoming = [edge for edge in self.edges if edge["target"] == node["id"]]
            outgoing = [edge for edge in self.edges if edge["source"] == node["id"]]
            impact[node["identity"]] = {
                "incoming": incoming,
                "outgoing": outgoing,
            }
        return impact

    def _column_impact(self) -> dict[str, Any]:
        impact: dict[str, Any] = {}
        for node in self.nodes.values():
            if node["kind"] != "column":
                continue
            incoming = [edge for edge in self.edges if edge["target"] == node["id"]]
            impact[node["identity"]] = {"incoming": incoming}
        return impact


def _render_markdown(graph: dict[str, Any]) -> str:
    summary = graph.get("summary") or {}
    lines = [
        "# Workspace Evidence Graph",
        "",
        f"- Workspace: `{graph.get('workspace')}`",
        f"- Nodes: `{summary.get('node_count', 0)}`",
        f"- Edges: `{summary.get('edge_count', 0)}`",
        f"- Introduced terms: `{summary.get('introduced_term_count', 0)}`",
        "",
        "## Introduced Terms",
        "",
    ]
    terms = graph.get("queries", {}).get("introduced_terms") or []
    if terms:
        rows = [
            [
                item.get("term", ""),
                item.get("kind", ""),
                len(item.get("introduced_by") or []),
                len(item.get("used_by") or []),
            ]
            for item in terms[:50]
        ]
        lines.append(render_markdown_table(["Term", "Kind", "Introducers", "Users"], rows))
    else:
        lines.append("_No terms found._")
    lines.extend(["", "## Node Counts", ""])
    counts: dict[str, int] = {}
    for node in graph.get("nodes") or []:
        counts[str(node.get("kind") or "")] = counts.get(str(node.get("kind") or ""), 0) + 1
    lines.append(render_markdown_table(["Kind", "Count"], sorted(counts.items())))
    return "\n".join(lines).rstrip() + "\n"


class WorkspaceEvidenceGraphQuery:
    def __init__(self, repo_root: str | Path, workspace: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.workspace_rel = _rel(self.workspace, self.repo_root)
        self.layout = WorkspaceLayout(project_root=self.workspace)

    def query(
        self,
        *,
        term: str = "",
        impact_feature: str = "",
        impact_column: str = "",
    ) -> EvidenceGraphQueryResult:
        graph_path = self.layout.generated_dir / "evidence_graph" / "graph.json"
        if not graph_path.exists():
            WorkspaceEvidenceGraphBuilder(self.repo_root, self.workspace_rel).build()
        graph = _load_json(graph_path)
        if term:
            query_type = "term"
            query_value = term
            results = _query_term(graph, term)
        elif impact_feature:
            query_type = "impact_feature"
            query_value = impact_feature
            results = _query_impact(graph, "feature", impact_feature)
        elif impact_column:
            query_type = "impact_column"
            query_value = impact_column
            results = _query_impact(graph, "column", impact_column)
        else:
            raise ValueError("one of term, impact_feature, or impact_column is required")
        return EvidenceGraphQueryResult(
            workspace=self.workspace_rel,
            ok=True,
            query_type=query_type,
            query_value=query_value,
            graph_path=_rel(graph_path, self.repo_root),
            result_count=len(results),
            results=results,
        )


def _query_term(graph: dict[str, Any], term: str) -> list[dict[str, Any]]:
    needle = _key(term)
    results = []
    nodes = {node["id"]: node for node in graph.get("nodes") or []}
    for node in nodes.values():
        identity = str(node.get("identity") or "")
        if needle not in _key(identity):
            continue
        incoming = [edge for edge in graph.get("edges") or [] if edge.get("target") == node["id"]]
        outgoing = [edge for edge in graph.get("edges") or [] if edge.get("source") == node["id"]]
        results.append(
            {
                "node": node,
                "introduced_by": [_edge_with_node(edge, nodes, "source") for edge in incoming if str(edge.get("type", "")).startswith("introduced")],
                "used_by": [_edge_with_node(edge, nodes, "source") for edge in incoming if edge.get("type") in {"mentions_term", "requires_feature", "asks_about"}],
                "outgoing": [_edge_with_node(edge, nodes, "target") for edge in outgoing],
            }
        )
    return results


def _query_impact(graph: dict[str, Any], kind: str, value: str) -> list[dict[str, Any]]:
    needle = _key(value)
    nodes = {node["id"]: node for node in graph.get("nodes") or []}
    results = []
    for node in nodes.values():
        if node.get("kind") != kind or needle not in _key(node.get("identity")):
            continue
        incoming = [edge for edge in graph.get("edges") or [] if edge.get("target") == node["id"]]
        outgoing = [edge for edge in graph.get("edges") or [] if edge.get("source") == node["id"]]
        related_kpis = []
        for edge in incoming + outgoing:
            for endpoint in ("source", "target"):
                related = nodes.get(str(edge.get(endpoint)))
                if related and related.get("kind") == "kpi" and related not in related_kpis:
                    related_kpis.append(related)
        results.append(
            {
                "node": node,
                "related_kpis": related_kpis,
                "incoming": [_edge_with_node(edge, nodes, "source") for edge in incoming],
                "outgoing": [_edge_with_node(edge, nodes, "target") for edge in outgoing],
            }
        )
    return results


def _edge_with_node(edge: dict[str, Any], nodes: dict[str, dict[str, Any]], endpoint: str) -> dict[str, Any]:
    return {
        "edge_type": edge.get("type"),
        "node": nodes.get(str(edge.get(endpoint)), {"id": edge.get(endpoint)}),
    }


def _node_kind_counts(graph: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in graph.get("nodes") or []:
        kind = str(node.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _split_terms(value: str) -> list[str]:
    terms = []
    for part in re.split(r"[,;]", value):
        cleaned = part.strip()
        if not cleaned:
            continue
        if "=" in cleaned:
            terms.append(cleaned.split("=", 1)[0].strip())
            terms.append(cleaned.split("=", 1)[1].strip())
        elif ">" in cleaned:
            terms.append(cleaned.split(">", 1)[0].strip())
        else:
            terms.append(cleaned)
    return [term for term in terms if term]


def _node_id(kind: str, identity: str) -> str:
    return f"{kind}:{_key(identity)}"


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_.:/\\-]+", "_", str(value or "").strip().lower()).strip("_")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a workspace evidence graph from generated artifacts.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = WorkspaceEvidenceGraphBuilder(args.repo_root, args.workspace).build()
    print(json.dumps(result.summary(), indent=2, default=str))
    return 0


def query_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query a workspace evidence graph.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-root", default=".")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--term")
    group.add_argument("--impact-feature")
    group.add_argument("--impact-column")
    args = parser.parse_args(argv)
    result = WorkspaceEvidenceGraphQuery(args.repo_root, args.workspace).query(
        term=args.term or "",
        impact_feature=args.impact_feature or "",
        impact_column=args.impact_column or "",
    )
    print(json.dumps(result.summary(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
