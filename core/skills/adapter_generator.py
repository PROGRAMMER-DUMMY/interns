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


class SkillAdapterGenerator:
    """Generate tool-neutral skill indexes from canonical SKILL.md files."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        skills_dir: str | Path = "skills",
        output_dir: str | Path = ".agents",
        tools: list[str] | tuple[str, ...] = DEFAULT_TOOLS,
        embed_full: bool = False,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.skills_dir = (self.repo_root / skills_dir).resolve()
        self.output_dir = (self.repo_root / output_dir).resolve()
        self.tools = tuple(_safe_tool_name(tool) for tool in tools)
        self.embed_full = embed_full

    def run(self) -> SkillAdapterResult:
        skills = self.discover()
        if not skills:
            raise FileNotFoundError(f"No skills found under {self.skills_dir}")
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
        index_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        tool_paths = []
        for tool in self.tools:
            tool_dir = self.output_dir / tool
            tool_dir.mkdir(parents=True, exist_ok=True)
            path = tool_dir / "SKILLS.md"
            path.write_text(_render_adapter(tool, skills, embed_full=self.embed_full), encoding="utf-8")
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


def _render_adapter(tool: str, skills: list[SkillDefinition], *, embed_full: bool) -> str:
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
        "## Available Skills",
        "",
    ]
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
