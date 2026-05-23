"""Tool-agnostic adapter generation for repo skills."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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

    def summary(self) -> dict[str, Any]:
        return {
            "index_path": self.index_path,
            "tool_paths": self.tool_paths,
            "skill_count": self.skill_count,
            "embedded_full": self.embedded_full,
        }


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    command: str
    use_when: list[str]
    outputs: list[str]
    safety: str
    recovery: str | None = None

    def as_index_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "name": self.name,
            "command": self.command,
            "use_when": self.use_when,
            "outputs": self.outputs,
            "safety": self.safety,
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
        tool_registry = self.load_tool_registry()
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
        index_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

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
                ),
                encoding="utf-8",
            )
            tool_paths.append(_rel(path, self.repo_root))

        return SkillAdapterResult(
            index_path=_rel(index_path, self.repo_root),
            tool_paths=tool_paths,
            skill_count=len(skills),
            embedded_full=self.embed_full,
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

    def load_tool_registry(self) -> ToolRegistry | None:
        if not self.tools_registry_path.exists():
            return None
        return _read_tool_registry(self.tools_registry_path, self.repo_root)


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


def _read_tool_registry(path: Path, repo_root: Path) -> ToolRegistry:
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
        tool = _read_tool_definition(raw_tool, index, path)
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


def _read_tool_definition(raw_tool: dict[str, Any], index: int, path: Path) -> ToolDefinition:
    name = _required_string_field(raw_tool, "name", index, path)
    command = _required_string_field(raw_tool, "command", index, path)
    use_when = _required_string_list(raw_tool, "use_when", index, path)
    outputs = _required_string_list(raw_tool, "outputs", index, path)
    safety = _required_string_field(raw_tool, "safety", index, path)
    recovery = _optional_string_field(raw_tool, "recovery", index, path)
    recovery = recovery or _optional_string_field(raw_tool, "recovery_guidance", index, path)
    return ToolDefinition(
        name=name,
        command=command,
        use_when=use_when,
        outputs=outputs,
        safety=safety,
        recovery=recovery,
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
        "",
    ]
    if tool_registry:
        lines.extend(_render_tool_registry(tool_registry))
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
            ]
        )
        if registered_tool.recovery:
            lines.append(f"  - Recovery: {registered_tool.recovery}")
    lines.append("")
    return lines


def _join_items(items: list[str]) -> str:
    return "; ".join(items)


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
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
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
