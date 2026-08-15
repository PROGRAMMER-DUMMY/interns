# CONTEXT-skills.md — `core/skills/`

> Created 2026-08-15 to close a real CONTEXT-MAP drift: the master tree has linked this
> file since the map was written, but it never existed on disk. Found by
> [`tests/test_context_map_drift.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/tests/test_context_map_drift.py)
> on its first run — which is the point of that linter.

## Executive Overview & Architectural Model

Repo skills are canonical in `skills/*/SKILL.md`. This package turns those canonical
bodies into **tool-agnostic adapters** so Claude Code, Gemini CLI, Codex, and any future
frontend read the same policy without anyone hand-copying it per tool.

That single-source rule is an AGENTS.md invariant ("Cross-Tool Skill Adapters"): duplicate
skill bodies drift silently, and a drifted skill is worse than a missing one because it
looks authoritative.

```text
skills/<name>/SKILL.md          (canonical, human-authored)
        │
        ▼  generate-skill-adapters
.agents/skills_index.json       (machine routing)
.agents/<tool>/SKILLS.md        (per-tool adapter: name, description, path, routing)
```

## File Details

### 1. [`__init__.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/skills/__init__.py)

- **Exact Purpose**: Package surface. Re-exports `SkillAdapterGenerator`, `SkillAdapterResult`, and `SkillDefinition` so callers import from `core.skills` rather than the module path.
- **Failure Modes & Edge Cases**: Re-export only; no logic. Adding an import here widens the public surface — prefer the module path for internal use.

### 2. [`adapter_generator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/skills/adapter_generator.py)

- **Exact Purpose**: Discovers `skills/*/SKILL.md`, parses each skill's frontmatter/description, and writes `.agents/skills_index.json` plus one `.agents/<tool>/SKILLS.md` adapter per configured tool. Backs the `generate-skill-adapters` CLI.
- **Key Functions / Classes**:
  - `SkillDefinition`: One parsed skill — name, description, canonical path, routing hints.
  - `SkillAdapterGenerator`: Discovery + rendering per tool.
  - `SkillAdapterResult`: What was written, for the CLI to report.
- **Inputs & Outputs**:
  - *Inputs*: `skills/*/SKILL.md`, the configured tool list.
  - *Outputs*: `.agents/skills_index.json`, `.agents/<tool>/SKILLS.md`.
- **Failure Modes & Edge Cases**:
  - Adapters stay **lightweight by default** (name, description, path, routing). `--embed-full` inlines whole skill bodies and is only for hosted environments that cannot read local `SKILL.md` files — using it routinely recreates the duplication this package exists to prevent.
  - A malformed or description-less `SKILL.md` degrades that one entry; it must not abort the whole generation.

## Invariants

- `skills/*/SKILL.md` is the only place a skill body is authored. Anything under `.agents/` is generated and safe to delete and regenerate.
- Adding a tool means adding it to the generator's tool list, never hand-writing a new `SKILLS.md`.
