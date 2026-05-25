"""Tool-agnostic adapter generation for repo skills."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.paths import PROJECT_ROOT

import yaml


DEFAULT_TOOLS = ("generic", "claude", "gemini", "codex")


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    path: str
    body: str

    def as_index_item(self) -> dict[str, str]:
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
        }


@dataclass(frozen=True)
class SkillAdapterResult:
    index_path: str
    tool_paths: list[str]
    skill_count: int
    embedded_full: bool
    route_manifest_path: str | None = None
    subagents_index_path: str | None = None
    subagent_count: int = 0
    native_agent_paths: list[str] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "index_path": self.index_path,
            "tool_paths": self.tool_paths,
            "skill_count": self.skill_count,
            "embedded_full": self.embedded_full,
            "route_manifest_path": self.route_manifest_path,
            "subagents_index_path": self.subagents_index_path,
            "subagent_count": self.subagent_count,
            "native_agent_paths": self.native_agent_paths or [],
        }


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    command: str
    use_when: list[str]
    outputs: list[str]
    safety: str
    required_skills: list[str]
    recovery: str | None = None

    def as_index_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "name": self.name,
            "command": self.command,
            "use_when": self.use_when,
            "outputs": self.outputs,
            "safety": self.safety,
            "required_skills": self.required_skills,
        }
        if self.recovery:
            item["recovery"] = self.recovery
        return item


@dataclass(frozen=True)
class ToolRegistry:
    source_path: str
    tools: list[ToolDefinition]
    evidence_policy: dict[str, Any]

    def as_index_item(self) -> dict[str, Any]:
        return {
            "source": self.source_path,
            "evidence_policy": self.evidence_policy,
            "tools": [tool.as_index_item() for tool in self.tools],
        }


@dataclass(frozen=True)
class SubagentDefinition:
    name: str
    display_name: str
    description: str
    skills: list[str]
    default_prompt: str
    safety: str
    source_path: str
    targets: dict[str, dict[str, Any]]
    model_policy: dict[str, Any]

    def as_index_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "skills": self.skills,
            "default_prompt": self.default_prompt,
            "safety": self.safety,
            "source_path": self.source_path,
            "targets": self.targets,
        }
        if self.model_policy:
            item["model_policy"] = self.model_policy
        return item


class SkillAdapterGenerator:
    """Generate tool-neutral skill indexes from canonical SKILL.md files."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        skills_dir: str | Path = "skills",
        output_dir: str | Path = ".agents",
        tools_registry_path: str | Path = ".agents/tools.json",
        tools: list[str] | tuple[str, ...] = DEFAULT_TOOLS,
        embed_full: bool = False,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.skills_dir = (self.repo_root / skills_dir).resolve()
        self.output_dir = (self.repo_root / output_dir).resolve()
        self.tools_registry_path = (self.repo_root / tools_registry_path).resolve()
        self.tools = tuple(_safe_tool_name(tool) for tool in tools)
        self.embed_full = embed_full

    def run(self) -> SkillAdapterResult:
        skills = self.discover()
        if not skills:
            raise FileNotFoundError(f"No skills found under {self.skills_dir}")
        subagents = self.discover_subagents(skills)
        tool_registry = self.load_tool_registry(known_skills={skill.name for skill in skills})
        self.output_dir.mkdir(parents=True, exist_ok=True)
        index_path = self.output_dir / "skills_index.json"
        payload = {
            "version": 1,
            "source": _rel(self.skills_dir, self.repo_root),
            "routing": {
                "canonical_source": "skills/*/SKILL.md",
                "explicit_skill_mention_wins": True,
                "fallback": "match user request to skill descriptions and load the smallest relevant set",
                "embed_full": self.embed_full,
            },
            "skills": [
                {
                    **skill.as_index_item(),
                    **({"body": skill.body} if self.embed_full else {}),
                }
                for skill in skills
            ],
        }
        if tool_registry:
            payload["tool_registry"] = tool_registry.as_index_item()
        if subagents:
            payload["subagents"] = [subagent.as_index_item() for subagent in subagents]
        index_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        route_manifest_path: Path | None = None
        if tool_registry:
            route_manifest_path = self.output_dir / "skill_route_manifest.json"
            route_manifest_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "source": _rel(self.tools_registry_path, self.repo_root),
                        "canonical_skills": _rel(self.skills_dir, self.repo_root),
                        "policy": {
                            "tools_must_declare_required_skills": True,
                            "unknown_required_skills_are_invalid": True,
                            "agents_should_record_active_skills_in_trajectory": True,
                        },
                        "routes": [
                            {
                                "tool": tool.name,
                                "command": tool.command,
                                "required_skills": tool.required_skills,
                            }
                            for tool in tool_registry.tools
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

        subagents_index_path: Path | None = None
        if subagents:
            subagents_index_path = self.output_dir / "subagents_index.json"
            subagents_index_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "source": _rel(self.skills_dir, self.repo_root),
                        "routing": {
                            "canonical_source": "skills/*/agents/*.yaml",
                            "generated_adapters": [f".agents/{tool}/SKILLS.md" for tool in self.tools],
                            "default_sandbox": "read-only unless the subagent role explicitly needs writes",
                        },
                        "subagents": [subagent.as_index_item() for subagent in subagents],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        native_agent_paths = self.export_native_subagents(subagents)

        tool_paths = []
        for tool in self.tools:
            tool_dir = self.output_dir / tool
            tool_dir.mkdir(parents=True, exist_ok=True)
            path = tool_dir / "SKILLS.md"
            path.write_text(
                _render_adapter(
                    tool,
                    skills,
                    embed_full=self.embed_full,
                    tool_registry=tool_registry,
                    subagents=subagents,
                ),
                encoding="utf-8",
            )
            tool_paths.append(_rel(path, self.repo_root))

        return SkillAdapterResult(
            index_path=_rel(index_path, self.repo_root),
            tool_paths=tool_paths,
            skill_count=len(skills),
            embedded_full=self.embed_full,
            route_manifest_path=_rel(route_manifest_path, self.repo_root)
            if route_manifest_path
            else None,
            subagents_index_path=_rel(subagents_index_path, self.repo_root)
            if subagents_index_path
            else None,
            subagent_count=len(subagents),
            native_agent_paths=native_agent_paths,
        )

    def discover(self) -> list[SkillDefinition]:
        if not self.skills_dir.exists():
            raise FileNotFoundError(f"skills directory not found: {self.skills_dir}")
        skills = []
        for path in sorted(self.skills_dir.glob("*/SKILL.md")):
            skills.append(_read_skill(path, self.repo_root))
        names = [skill.name for skill in skills]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate skill names: {', '.join(duplicates)}")
        return skills

    def load_tool_registry(self, *, known_skills: set[str] | None = None) -> ToolRegistry | None:
        if not self.tools_registry_path.exists():
            return None
        return _read_tool_registry(self.tools_registry_path, self.repo_root, known_skills=known_skills or set())

    def discover_subagents(self, skills: list[SkillDefinition]) -> list[SubagentDefinition]:
        known_skills = {skill.name for skill in skills}
        subagents: list[SubagentDefinition] = []
        for skill in skills:
            agents_dir = self.repo_root / Path(skill.path).parent / "agents"
            if not agents_dir.exists():
                continue
            for path in sorted(agents_dir.glob("*.yaml")):
                subagents.extend(_read_subagent_sidecar(path, self.repo_root, skill, known_skills))
        names = [subagent.name for subagent in subagents]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate subagent names: {', '.join(duplicates)}")
        return subagents

    def export_native_subagents(self, subagents: list[SubagentDefinition]) -> list[str]:
        if not subagents:
            return []
        paths: list[str] = []
        for tool in ("claude", "gemini", "codex"):
            tool_subagents = [
                subagent for subagent in subagents if _subagent_enabled_for_tool(subagent, tool)
            ]
            if not tool_subagents:
                continue
            agent_dir = self.repo_root / f".{tool}" / "agents"
            agent_dir.mkdir(parents=True, exist_ok=True)
            for subagent in tool_subagents:
                if tool == "claude":
                    path = agent_dir / f"{subagent.name}.md"
                    text = _render_claude_agent(subagent)
                elif tool == "gemini":
                    path = agent_dir / f"{subagent.name}.md"
                    text = _render_gemini_agent(subagent)
                else:
                    path = agent_dir / f"{subagent.name}.toml"
                    text = _render_codex_agent(subagent)
                path.write_text(text, encoding="utf-8")
                paths.append(_rel(path, self.repo_root))
        return paths


def _read_skill(path: Path, repo_root: Path) -> SkillDefinition:
    text = path.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(text, path)
    name = metadata.get("name", "").strip()
    description = metadata.get("description", "").strip()
    if not name:
        raise ValueError(f"skill missing name: {path}")
    if not description:
        raise ValueError(f"skill missing description: {path}")
    return SkillDefinition(
        name=name,
        description=description,
        path=_rel(path, repo_root),
        body=body.strip(),
    )


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"skill missing YAML frontmatter: {path}")
    end_idx = None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise ValueError(f"skill frontmatter is not closed: {path}")
    metadata: dict[str, str] = {}
    current_key: str | None = None
    for line in lines[1:end_idx]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace() and current_key:
            metadata[current_key] = (metadata[current_key] + " " + line.strip()).strip()
            continue
        if ":" not in line:
            raise ValueError(f"unsupported frontmatter line in {path}: {line}")
        key, value = line.split(":", 1)
        current_key = key.strip()
        cleaned_value = value.strip().strip("\"'")
        metadata[current_key] = "" if cleaned_value in {">", "|", ">-", "|-"} else cleaned_value
    body = "\n".join(lines[end_idx + 1 :])
    return metadata, body


def _read_tool_registry(path: Path, repo_root: Path, *, known_skills: set[str]) -> ToolRegistry:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"tool registry is invalid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"tool registry must be a JSON object: {path}")
    raw_tools = payload.get("tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        raise ValueError(f"tool registry must define a non-empty tools list: {path}")

    tools: list[ToolDefinition] = []
    seen_names: set[str] = set()
    for index, raw_tool in enumerate(raw_tools):
        if not isinstance(raw_tool, dict):
            raise ValueError(f"tool registry entry {index} must be an object: {path}")
        tool = _read_tool_definition(raw_tool, index, path, known_skills=known_skills)
        if tool.name in seen_names:
            raise ValueError(f"tool registry has duplicate tool name: {tool.name}")
        seen_names.add(tool.name)
        tools.append(tool)

    evidence_policy = payload.get("evidence_policy", {})
    if evidence_policy and not isinstance(evidence_policy, dict):
        raise ValueError(f"tool registry evidence_policy must be an object: {path}")
    return ToolRegistry(
        source_path=_rel(path, repo_root),
        tools=tools,
        evidence_policy=evidence_policy,
    )


def _read_tool_definition(
    raw_tool: dict[str, Any],
    index: int,
    path: Path,
    *,
    known_skills: set[str],
) -> ToolDefinition:
    name = _required_string_field(raw_tool, "name", index, path)
    command = _required_string_field(raw_tool, "command", index, path)
    use_when = _required_string_list(raw_tool, "use_when", index, path)
    outputs = _required_string_list(raw_tool, "outputs", index, path)
    safety = _required_string_field(raw_tool, "safety", index, path)
    required_skills = _required_string_list(raw_tool, "required_skills", index, path)
    missing_skills = sorted(set(required_skills) - known_skills)
    if missing_skills:
        raise ValueError(
            f"tool registry entry {index} references unknown required skill(s): "
            f"{', '.join(missing_skills)}: {path}"
        )
    recovery = _optional_string_field(raw_tool, "recovery", index, path)
    recovery = recovery or _optional_string_field(raw_tool, "recovery_guidance", index, path)
    return ToolDefinition(
        name=name,
        command=command,
        use_when=use_when,
        outputs=outputs,
        safety=safety,
        required_skills=required_skills,
        recovery=recovery,
    )


def _read_subagent_sidecar(
    path: Path,
    repo_root: Path,
    owning_skill: SkillDefinition,
    known_skills: set[str],
) -> list[SubagentDefinition]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"subagent sidecar is invalid YAML: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"subagent sidecar must be a YAML object: {path}")

    raw_subagents = payload.get("subagents")
    if raw_subagents is None and isinstance(payload.get("interface"), dict):
        interface = payload["interface"]
        raw_subagents = [
            {
                "name": owning_skill.name,
                "display_name": interface.get("display_name", owning_skill.name),
                "description": interface.get(
                    "short_description",
                    f"Subagent for the {owning_skill.name} skill.",
                ),
                "skills": [owning_skill.name],
                "default_prompt": interface.get(
                    "default_prompt",
                    f"Load `{owning_skill.name}` and follow its operating policy.",
                ),
                "safety": interface.get("safety", "follows_skill_policy"),
                "targets": interface.get("targets", {}),
            }
        ]
    if not isinstance(raw_subagents, list):
        raise ValueError(f"subagent sidecar must define `subagents` list: {path}")

    subagents: list[SubagentDefinition] = []
    for index, raw_subagent in enumerate(raw_subagents):
        if not isinstance(raw_subagent, dict):
            raise ValueError(f"subagent entry {index} must be an object: {path}")
        subagent = _read_subagent_definition(raw_subagent, index, path, repo_root)
        missing_skills = sorted(set(subagent.skills) - known_skills)
        if missing_skills:
            raise ValueError(
                f"subagent {subagent.name} references unknown skill(s): "
                f"{', '.join(missing_skills)}: {path}"
            )
        subagents.append(subagent)
    return subagents


def _read_subagent_definition(
    raw_subagent: dict[str, Any],
    index: int,
    path: Path,
    repo_root: Path,
) -> SubagentDefinition:
    name = _required_string_field(raw_subagent, "name", index, path)
    display_name = _optional_string_field(raw_subagent, "display_name", index, path) or name
    description = _required_string_field(raw_subagent, "description", index, path)
    skills = _required_string_list(raw_subagent, "skills", index, path)
    default_prompt = _required_string_field(raw_subagent, "default_prompt", index, path)
    safety = _required_string_field(raw_subagent, "safety", index, path)
    targets = raw_subagent.get("targets", {})
    if targets and not isinstance(targets, dict):
        raise ValueError(f"subagent entry {index} has invalid targets: {path}")
    model_policy = raw_subagent.get("model_policy", {})
    if model_policy and not isinstance(model_policy, dict):
        raise ValueError(f"subagent entry {index} has invalid model_policy: {path}")
    cleaned_targets: dict[str, dict[str, Any]] = {}
    for target_name, target_config in targets.items():
        if not isinstance(target_name, str) or not target_name.strip():
            raise ValueError(f"subagent entry {index} has invalid target name: {path}")
        if target_config is None:
            target_config = {}
        if not isinstance(target_config, dict):
            raise ValueError(f"subagent entry {index} target {target_name} must be an object: {path}")
        cleaned_targets[target_name.strip()] = target_config
    return SubagentDefinition(
        name=name,
        display_name=display_name,
        description=description,
        skills=skills,
        default_prompt=default_prompt,
        safety=safety,
        source_path=_rel(path, repo_root),
        targets=cleaned_targets,
        model_policy=model_policy,
    )


def _required_string_field(raw_tool: dict[str, Any], field: str, index: int, path: Path) -> str:
    value = raw_tool.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"tool registry entry {index} missing non-empty {field}: {path}")
    return value.strip()


def _optional_string_field(raw_tool: dict[str, Any], field: str, index: int, path: Path) -> str | None:
    value = raw_tool.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"tool registry entry {index} has invalid {field}: {path}")
    return value.strip()


def _required_string_list(raw_tool: dict[str, Any], field: str, index: int, path: Path) -> list[str]:
    value = raw_tool.get(field)
    if not isinstance(value, list) or not value:
        raise ValueError(f"tool registry entry {index} missing non-empty {field}: {path}")
    cleaned = []
    for item_index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"tool registry entry {index} has invalid {field}[{item_index}]: {path}"
            )
        cleaned.append(item.strip())
    return cleaned


def _render_adapter(
    tool: str,
    skills: list[SkillDefinition],
    *,
    embed_full: bool,
    tool_registry: ToolRegistry | None = None,
    subagents: list[SubagentDefinition] | None = None,
) -> str:
    title = tool.title() if tool != "generic" else "Generic"
    lines = [
        f"# {title} Skill Adapter",
        "",
        "This file is generated from canonical repo skills. Do not hand-edit it.",
        "",
        "## Routing Rules",
        "",
        "- Treat `skills/*/SKILL.md` as the source of truth.",
        "- If the user explicitly names `$skill-name` or `skill-name`, load that skill.",
        "- Otherwise match the request to skill descriptions and load the smallest relevant skill set.",
        "- If multiple skills match, order them by dependency and keep context minimal.",
        "- If local file access is available, open the listed `SKILL.md` before applying a skill.",
        "- If local file access is unavailable, use embedded bodies only when this adapter was generated with full embedding.",
        "- Hard stop: before choosing any project workflow route or next command, read `.agents/tools.json` or this adapter in the active session; if that did not happen, stop, reread it, and restart route selection.",
        "- Hard stop: for external/profiled workspaces with no KPIs, do not run KPI feature resolution before `build-source-family-contracts`; source-family/schema-drift planning comes first.",
        "",
    ]
    if tool_registry:
        lines.extend(_render_tool_registry(tool_registry))
    enabled_subagents = [
        subagent
        for subagent in (subagents or [])
        if _subagent_enabled_for_tool(subagent, tool)
    ]
    if enabled_subagents:
        lines.extend(_render_subagents(tool, enabled_subagents))
    lines.extend(["## Available Skills", ""])
    for skill in skills:
        lines.extend(
            [
                f"### {skill.name}",
                "",
                f"- Path: `{skill.path}`",
                f"- Description: {skill.description}",
                "",
            ]
        )
        if embed_full:
            lines.extend(
                [
                    "<skill-body>",
                    "",
                    skill.body,
                    "",
                    "</skill-body>",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _render_tool_registry(tool_registry: ToolRegistry) -> list[str]:
    lines = [
        "## Project Tool Registry",
        "",
        f"- Source: `{tool_registry.source_path}`",
        "- Before using project tools, honor each registered command's `safety` guidance.",
    ]
    if tool_registry.evidence_policy:
        secret_rule = tool_registry.evidence_policy.get("secret_display_rule")
        raw_dataset_rule = tool_registry.evidence_policy.get("raw_dataset_rule")
        if secret_rule:
            lines.append(f"- Secret display safety: {secret_rule}")
        if raw_dataset_rule:
            lines.append(f"- Dataset access safety: {raw_dataset_rule}")
    lines.extend(["", "### Registered Tools", ""])
    for registered_tool in tool_registry.tools:
        lines.extend(
            [
                f"- `{registered_tool.name}`",
                f"  - Command: `{registered_tool.command}`",
                f"  - Use when: {_join_items(registered_tool.use_when)}",
                f"  - Outputs: {_join_items(registered_tool.outputs)}",
                f"  - Safety: {registered_tool.safety}",
                f"  - Required skills: {_join_items(registered_tool.required_skills)}",
            ]
        )
        if registered_tool.recovery:
            lines.append(f"  - Recovery: {registered_tool.recovery}")
    lines.append("")
    return lines


def _render_subagents(tool: str, subagents: list[SubagentDefinition]) -> list[str]:
    lines = [
        "## Available Subagents",
        "",
        "These subagents are generated from `skills/*/agents/*.yaml`; do not hand-edit adapter output.",
        "Use the narrowest role that fits the task and keep write access limited to implementer-style roles.",
        "",
    ]
    for subagent in subagents:
        target_config = subagent.targets.get(tool, {})
        sandbox = target_config.get("sandbox_mode") or target_config.get("permission") or "role_defined"
        model = target_config.get("model", "default")
        lines.extend(
            [
                f"### {subagent.name}",
                "",
                f"- Display name: {subagent.display_name}",
                f"- Description: {subagent.description}",
                f"- Skills: {_join_items(subagent.skills)}",
                f"- Safety: {subagent.safety}",
                f"- Source: `{subagent.source_path}`",
                f"- Target model: `{model}`",
                f"- Target sandbox/permission: `{sandbox}`",
                f"- Model policy: {_format_model_policy(subagent.model_policy)}",
                f"- Default prompt: {subagent.default_prompt}",
                "",
            ]
        )
    return lines


def _subagent_enabled_for_tool(subagent: SubagentDefinition, tool: str) -> bool:
    if not subagent.targets:
        return True
    target_config = subagent.targets.get(tool)
    if target_config is None:
        return False
    enabled = target_config.get("enabled", True)
    return enabled is not False


def _render_claude_agent(subagent: SubagentDefinition) -> str:
    target_config = subagent.targets.get("claude", {})
    frontmatter: dict[str, Any] = {
        "name": subagent.name,
        "description": subagent.description,
        "skills": subagent.skills,
    }
    model = target_config.get("model")
    if model:
        frontmatter["model"] = model
    permission = target_config.get("permissionMode") or target_config.get("permission")
    if permission and permission not in {"read-only", "workspace-write"}:
        frontmatter["permissionMode"] = permission
    if permission == "read-only":
        frontmatter["tools"] = "Read, Glob, Grep"
    return _render_markdown_agent(frontmatter, subagent, "Claude Code")


def _render_gemini_agent(subagent: SubagentDefinition) -> str:
    target_config = subagent.targets.get("gemini", {})
    frontmatter: dict[str, Any] = {
        "name": subagent.name,
        "description": subagent.description,
        "kind": "local",
    }
    model = target_config.get("model")
    if model:
        frontmatter["model"] = model
    max_turns = target_config.get("max_turns") or target_config.get("maxTurns")
    if max_turns:
        frontmatter["max_turns"] = max_turns
    tools = target_config.get("tools")
    if tools:
        frontmatter["tools"] = tools
    elif _target_is_read_only(subagent, "gemini"):
        frontmatter["tools"] = ["read_file", "grep_search"]
    return _render_markdown_agent(frontmatter, subagent, "Gemini CLI")


def _render_codex_agent(subagent: SubagentDefinition) -> str:
    target_config = subagent.targets.get("codex", {})
    sandbox_mode = target_config.get("sandbox_mode") or target_config.get("permission")
    codex_name = _codex_agent_name(subagent.name)
    lines = [
        "# Generated from skills/*/agents/*.yaml. Do not hand-edit.",
        f"# Display name: {subagent.display_name}",
        f"# Safety: {subagent.safety}",
        f"# Source: {subagent.source_path}",
        "# Skills: " + ", ".join(subagent.skills),
        f'name = "{_toml_escape(codex_name)}"',
        f'description = "{_toml_escape(subagent.description)}"',
    ]
    nickname_candidates = [subagent.display_name, subagent.name]
    if codex_name != subagent.name:
        nickname_candidates.append(codex_name)
    lines.append(
        "nickname_candidates = ["
        + ", ".join(f'"{_toml_escape(value)}"' for value in _unique_items(nickname_candidates))
        + "]"
    )
    model = target_config.get("model")
    if model:
        lines.append(f'model = "{_toml_escape(str(model))}"')
    reasoning = target_config.get("model_reasoning_effort") or target_config.get("reasoning_effort")
    if reasoning:
        lines.append(f'model_reasoning_effort = "{_toml_escape(str(reasoning))}"')
    if sandbox_mode:
        lines.append(f'sandbox_mode = "{_toml_escape(str(sandbox_mode))}"')
    lines.extend(
        [
            "",
            'developer_instructions = """',
            _triple_quote_escape(_agent_body(subagent, "Codex CLI")),
            '"""',
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _render_markdown_agent(
    frontmatter: dict[str, Any],
    subagent: SubagentDefinition,
    target_name: str,
) -> str:
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", "", _agent_body(subagent, target_name)])
    return "\n".join(lines).rstrip() + "\n"


def _agent_body(subagent: SubagentDefinition, target_name: str) -> str:
    return "\n".join(
        [
            f"# {subagent.display_name}",
            "",
            f"This {target_name} subagent is generated from `{subagent.source_path}`.",
            "",
            "## Default Prompt",
            "",
            subagent.default_prompt,
            "",
            "## Required Skills",
            "",
            *[f"- `{skill}`" for skill in subagent.skills],
            "",
            "## Safety Boundary",
            "",
            subagent.safety,
            "",
            "## Model Policy",
            "",
            _format_model_policy(subagent.model_policy),
            "",
            "Do not bypass repo workflow gates, edit generated contracts to hide blockers, read raw datasets when profiles are enough, or run remote execution without explicit approval.",
        ]
    )


def _target_is_read_only(subagent: SubagentDefinition, tool: str) -> bool:
    target_config = subagent.targets.get(tool, {})
    value = str(target_config.get("sandbox_mode") or target_config.get("permission") or "")
    return value.lower() == "read-only" or subagent.safety.startswith("read_only")


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _triple_quote_escape(value: str) -> str:
    return value.replace('"""', '\\"\\"\\"')


def _codex_agent_name(name: str) -> str:
    return "_".join(part for part in name.replace("-", "_").split("_") if part)


def _unique_items(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _join_items(items: list[str]) -> str:
    return "; ".join(items)


def _format_model_policy(model_policy: dict[str, Any]) -> str:
    if not model_policy:
        return "Use the target CLI default model unless a workflow route specifies otherwise."
    return json.dumps(model_policy, sort_keys=True)


def _safe_tool_name(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    if not cleaned:
        raise ValueError("tool names cannot be empty")
    return cleaned


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate tool-agnostic skill adapters.")
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Repository root. Defaults to detected project root.",
    )
    parser.add_argument("--skills-dir", default="skills", help="Directory containing */SKILL.md files.")
    parser.add_argument("--output-dir", default=".agents", help="Output directory for generated adapters.")
    parser.add_argument(
        "--tool",
        action="append",
        dest="tools",
        help="Tool adapter name. Repeatable. Defaults to generic, claude, gemini, codex.",
    )
    parser.add_argument(
        "--embed-full",
        action="store_true",
        help="Embed full SKILL.md bodies for tools that cannot read local files.",
    )
    args = parser.parse_args(argv)
    result = SkillAdapterGenerator(
        args.repo_root,
        skills_dir=args.skills_dir,
        output_dir=args.output_dir,
        tools=args.tools or DEFAULT_TOOLS,
        embed_full=args.embed_full,
    ).run()
    print(json.dumps(result.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
