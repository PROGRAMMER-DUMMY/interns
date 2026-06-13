from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.storage.workspace_layout import WorkspaceLayout


REQUIRED_OPTION_FIELDS = (
    "derived_column_name",
    "formula",
    "input_columns",
    "example",
    "evidence_sources",
    "derivation_reasoning",
    "evidence_state",
    "confidence",
    "needs_user_confirmation",
)
REQUIRED_INPUT_COLUMN_FIELDS = (
    "column",
    "role",
    "observed_values",
    "value_profile",
    "semantic_meaning_sources",
    "reason",
)
REQUIRED_EXAMPLE_FIELDS = ("input", "output")
REQUIRED_REASONING_FIELDS = ("why_this_formula", "why_not_ground_truth", "remaining_risk")


@dataclass(frozen=True)
class DerivedFeatureMarkdownResult:
    output_dir: str
    files: list[str]
    markdown_files: list[str]
    json_files: list[str]
    stale_files: list[str]
    option_count: int

    def summary(self) -> dict[str, Any]:
        return asdict(self)


class DerivedFeatureMarkdownConverter:
    def __init__(
        self,
        repo_root: str | Path,
        workspace: str | Path,
        *,
        mapping_path: str | Path | None = None,
        output_dir: str | Path | None = None,
        strict: bool = True,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.workspace = (self.repo_root / workspace).resolve()
        self.layout = WorkspaceLayout(project_root=self.workspace)
        self.mapping_path = (
            (self.repo_root / mapping_path).resolve()
            if mapping_path
            else self.layout.contracts_dir / "kpi_feature_mapping.json"
        )
        self.output_dir = (
            (self.repo_root / output_dir).resolve()
            if output_dir
            else self.layout.reports_dir / "derived_feature_reviews"
        )
        self.markdown_dir = self.output_dir / "md"
        self.json_dir = self.output_dir / "json"
        self.strict = strict

    def run(self) -> DerivedFeatureMarkdownResult:
        if not self.mapping_path.exists():
            raise FileNotFoundError(f"feature mapping not found: {self.mapping_path}")
        mapping = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.markdown_dir.mkdir(parents=True, exist_ok=True)
        self.json_dir.mkdir(parents=True, exist_ok=True)
        existing_review_files = set(self.markdown_dir.glob("*.md")) | set(self.json_dir.glob("*.json"))

        written: list[str] = []
        markdown_files: list[str] = []
        json_files: list[str] = []
        for kpi in mapping.get("kpis", []):
            kpi_id = str(kpi.get("kpi_id") or "unknown_kpi")
            for feature in kpi.get("features", []):
                options = feature.get("derived_feature_options") or []
                if not options:
                    continue
                renderable_options = []
                for option_idx, option in enumerate(options, start=1):
                    if not option.get("input_columns"):
                        continue
                    if self.strict:
                        _validate_option(option, kpi_id, option_idx)
                    renderable_options.append(option)
                if not renderable_options:
                    continue
                stem = _review_stem(kpi_id, renderable_options[0])
                markdown_path = self.markdown_dir / f"{stem}.md"
                json_path = self.json_dir / f"{stem}.json"
                markdown_path.write_text(
                    _render_review(kpi, feature, renderable_options),
                    encoding="utf-8",
                )
                json_path.write_text(
                    json.dumps(
                        _review_json(kpi, feature, renderable_options),
                        indent=2,
                        default=str,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                markdown_files.append(_rel(markdown_path, self.repo_root))
                json_files.append(_rel(json_path, self.repo_root))
                written.extend([_rel(markdown_path, self.repo_root), _rel(json_path, self.repo_root)])

        current_review_files = {
            self.repo_root / file
            for file in [*markdown_files, *json_files]
        }
        stale_files = []
        for stale_path in sorted(existing_review_files - current_review_files):
            _mark_stale_review(stale_path)
            stale_files.append(_rel(stale_path, self.repo_root))

        index_content = _render_index(markdown_files, json_files, stale_files)
        index_path = self.output_dir / "index.md"
        if index_content:
            index_path.write_text(index_content, encoding="utf-8")
            written.append(_rel(index_path, self.repo_root))
            markdown_files.append(_rel(index_path, self.repo_root))
        return DerivedFeatureMarkdownResult(
            output_dir=_rel(self.output_dir, self.repo_root),
            files=written,
            markdown_files=markdown_files,
            json_files=json_files,
            stale_files=stale_files,
            option_count=len(json_files),
        )


def _validate_option(option: dict[str, Any], kpi_id: str, option_idx: int) -> None:
    missing = [field for field in REQUIRED_OPTION_FIELDS if field not in option]
    if missing:
        raise ValueError(f"{kpi_id} option {option_idx} missing required fields: {missing}")
    if not isinstance(option.get("input_columns"), list):
        raise ValueError(f"{kpi_id} option {option_idx} requires input_columns list")
    for column_idx, column in enumerate(option["input_columns"], start=1):
        missing_column = [field for field in REQUIRED_INPUT_COLUMN_FIELDS if field not in column]
        if missing_column:
            raise ValueError(
                f"{kpi_id} option {option_idx} input column {column_idx} "
                f"missing required fields: {missing_column}"
            )
        if not isinstance(column.get("value_profile"), dict):
            raise ValueError(
                f"{kpi_id} option {option_idx} input column {column_idx} "
                "requires value_profile object"
            )
        if not isinstance(column.get("semantic_meaning_sources"), list):
            raise ValueError(
                f"{kpi_id} option {option_idx} input column {column_idx} "
                "requires semantic_meaning_sources list"
            )
    example = option.get("example")
    if not isinstance(example, dict):
        raise ValueError(f"{kpi_id} option {option_idx} requires example object")
    missing_example = [field for field in REQUIRED_EXAMPLE_FIELDS if field not in example]
    if missing_example:
        raise ValueError(
            f"{kpi_id} option {option_idx} example missing required fields: {missing_example}"
        )
    if not isinstance(option.get("evidence_sources"), list):
        raise ValueError(f"{kpi_id} option {option_idx} requires evidence_sources list")
    reasoning = option.get("derivation_reasoning")
    if not isinstance(reasoning, dict):
        raise ValueError(f"{kpi_id} option {option_idx} requires derivation_reasoning object")
    missing_reasoning = [field for field in REQUIRED_REASONING_FIELDS if field not in reasoning]
    if missing_reasoning:
        raise ValueError(
            f"{kpi_id} option {option_idx} derivation_reasoning missing required fields: "
            f"{missing_reasoning}"
        )


def _render_review(
    kpi: dict[str, Any],
    feature: dict[str, Any],
    options: list[dict[str, Any]],
) -> str:
    name = options[0]["derived_column_name"]
    lines = [
        f"# Derived Feature Review: {name}",
        "",
        "## KPI",
        "",
        f"- KPI ID: `{kpi.get('kpi_id', '')}`",
        f"- KPI name: {kpi.get('name') or '(not provided)'}",
        f"- Feature state: `{feature.get('state', '')}`",
        "",
        "## Decision Needed",
        "",
        f"Can we define `{name}` using one of the proposed options below?",
        "",
    ]
    for option_idx, option in enumerate(options, start=1):
        lines.extend(_render_option(option, option_idx, len(options)))
    return "\n".join(lines)


def _render_option(option: dict[str, Any], option_idx: int, option_count: int) -> list[str]:
    heading = "## Proposed Option" if option_count == 1 else f"## Proposed Option {option_idx}"
    lines = [
        heading,
        "",
        "### Formula",
        "",
        "```sql",
        str(option.get("formula", "")),
        "```",
        "",
    ]
    if option.get("business_meaning"):
        lines.extend(["### Business Meaning", "", str(option["business_meaning"]), ""])
    reasoning = option.get("derivation_reasoning") or {}
    if reasoning:
        lines.extend(["### Why This Was Proposed", ""])
        for label, value in reasoning.items():
            lines.append(f"- {_human_label(label)}: {value}")
        lines.append("")
    lines.extend(["### Columns Used", "", "| Column | Role | Observed Values | Value Profile | Meaning | Risk/Reason |", "|---|---|---|---|---|---|"])
    for column in option.get("input_columns", []):
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_table(str(column.get("column", ""))),
                    _escape_table(str(column.get("role", ""))),
                    _escape_table(_render_values(column.get("observed_values"))),
                    _escape_table(_render_value_profile(column.get("value_profile") or {})),
                    _escape_table(_render_meanings(column.get("semantic_meaning_sources") or [])),
                    _escape_table(str(column.get("reason") or "")),
                ]
            )
            + " |"
        )
    lines.extend(["", "### Example", ""])
    example = option.get("example") or {}
    lines.extend(["Input:", ""])
    for key, value in (example.get("input") or {}).items():
        lines.append(f"- `{key}` = `{value}`")
    lines.extend(["", "Output:", ""])
    for key, value in (example.get("output") or {}).items():
        lines.append(f"- `{key}` = `{value}`")
    if example.get("warning"):
        lines.extend(["", f"Note: {example['warning']}"])
    lines.extend(
        [
            "",
            "### Evidence Status",
            "",
            f"- Evidence state: `{option.get('evidence_state')}`",
            f"- Confidence: `{option.get('confidence', 'not_provided')}`",
            f"- Needs user confirmation: `{option.get('needs_user_confirmation')}`",
            "",
            "### Evidence Sources",
            "",
        ]
    )
    sources = option.get("evidence_sources") or []
    if not sources:
        lines.append("- No evidence sources were provided.")
    for source in sources:
        label = source.get("file") or source.get("dataset") or "(unknown source)"
        detail = source.get("evidence") or source.get("meaning") or source.get("evidence_type") or ""
        lines.append(f"- `{label}`: {detail}")
    lines.append("")
    return lines


def _review_json(
    kpi: dict[str, Any],
    feature: dict[str, Any],
    options: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "kpi_id": kpi.get("kpi_id"),
        "kpi_name": kpi.get("name"),
        "kpi_source": kpi.get("source"),
        "feature": feature.get("feature"),
        "feature_state": feature.get("state"),
        "derived_feature_options": options,
    }


def _render_index(markdown_files: list[str], json_files: list[str], stale_files: list[str] | None = None) -> str:
    if not markdown_files and not json_files:
        return ""  # nothing to show — caller skips writing the file
    lines = ["# Derived Feature Review Index", ""]
    if stale_files:
        lines.extend(
            [
                "> Some older review files were marked stale because the current feature mapping no longer "
                "contains those derived options. Do not use stale files as selectable blocker options.",
                "",
            ]
        )
    json_by_stem = {Path(file).stem: Path(file).name for file in json_files}
    for file in markdown_files:
        name = Path(file).name
        markdown_link = "md/" + name
        json_name = json_by_stem.get(Path(file).stem)
        if json_name:
            lines.append(f"- [{name}](./{markdown_link}) | [JSON](./json/{json_name})")
        else:
            lines.append(f"- [{name}](./{markdown_link})")
    return "\n".join(lines) + "\n"


def _mark_stale_review(path: Path) -> None:
    message = (
        "This generated derived-feature review is stale. The current "
        "kpi_feature_mapping.json no longer contains this derived option, so agents must not offer "
        "it as a selectable blocker choice."
    )
    if path.suffix.lower() == ".json":
        path.write_text(
            json.dumps(
                {
                    "stale": True,
                    "reason": message,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return
    path.write_text(f"# Stale Derived Feature Review\n\n{message}\n", encoding="utf-8")


def _review_stem(kpi_id: str, option: dict[str, Any]) -> str:
    feature = _slug(str(option.get("derived_column_name") or "derived_feature"))
    return f"{_slug(kpi_id)}_{feature}"


def _render_values(values: Any) -> str:
    if not values:
        return "(none profiled)"
    if isinstance(values, list):
        return ", ".join(str(value) for value in values[:8])
    return str(values)


def _render_value_profile(profile: dict[str, Any]) -> str:
    parts = []
    for key in ("sample_min", "sample_max", "exact_min", "exact_max", "null_count", "profile_source"):
        value = profile.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    return "; ".join(parts) if parts else "(no value profile)"


def _render_meanings(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return "(not provided)"
    meanings = []
    for source in sources[:3]:
        if source.get("meaning"):
            meanings.append(str(source["meaning"]))
        elif source.get("evidence"):
            meanings.append(str(source["evidence"]))
    return "; ".join(meanings) if meanings else "(not provided)"


def _human_label(value: str) -> str:
    return value.replace("_", " ").capitalize()


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "item"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str] | None = None) -> int | None:
    parser = argparse.ArgumentParser(
        description="Convert strict derived-feature JSON options into stakeholder Markdown."
    )
    parser.add_argument("--workspace", required=True, help="Workspace path, for example workspaces/demo")
    parser.add_argument("--repo-root", default=".", help="Repository root. Defaults to current directory.")
    parser.add_argument("--mapping", help="Optional path to kpi_feature_mapping.json.")
    parser.add_argument("--out", help="Optional output directory.")
    parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Allow missing fields. Default is strict validation.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    args = parser.parse_args(argv)

    from core.onboarding.cli_deprecation import (
        announce_deprecated_cli_redirect,
        is_internal_cli_call,
        warn_soft_deprecated_cli,
    )

    stage_only = bool(args.mapping or args.out or args.no_strict)
    if stage_only or is_internal_cli_call():
        warn_soft_deprecated_cli(
            "derived-feature-markdown",
            prefer="prepare-kpi-blocker-panel",
            reason="the wrapper regenerates derived-feature markdown as part of the panel build",
        )
    else:
        announce_deprecated_cli_redirect(
            "derived-feature-markdown",
            prefer="prepare-kpi-blocker-panel",
            reason="the wrapper regenerates derived-feature markdown as part of the panel build",
        )
        from core.onboarding.kpi.blocker_cli import prepare_main

        return prepare_main(
            ["--workspace", args.workspace, "--repo-root", args.repo_root]
        )

    result = DerivedFeatureMarkdownConverter(
        args.repo_root,
        args.workspace,
        mapping_path=args.mapping,
        output_dir=args.out,
        strict=not args.no_strict,
    ).run()
    if args.json:
        print(json.dumps(result.summary(), indent=2))
        return None
    print(f"Wrote {result.option_count} derived feature review file(s) to {result.output_dir}")
    for file in result.files:
        print(f"- {file}")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
