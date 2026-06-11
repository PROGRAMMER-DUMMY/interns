"""One front door for every validation/harness suite (Phase 1 consolidation).

Eight separate ``run-*-harness`` / ``validate-*`` entry points were eight
chances to pick the wrong one (and two of them differed only by a coin-flip
of a name). This dispatcher keeps each suite's module intact and routes:

    uv run harness <suite> [suite args...]
    uv run harness list

``validate-project-harness`` keeps its dedicated entry point (it is the
most-documented gate); everything else routes through here.
"""
from __future__ import annotations

import importlib
import sys

SUITES: dict[str, tuple[str, str, str]] = {
    # suite -> (module, attr, one-line purpose)
    "project": (
        "core.onboarding.harness.project_harness", "main",
        "cross-artifact project gate (same as validate-project-harness)",
    ),
    "workflow-guardrails": (
        "core.onboarding.harness.workflow_guard_harness", "main",
        "workflow guard reliability checks (stall/retry/unsupported-cmd/...)",
    ),
    "reliability": (
        "core.onboarding.harness.reliability_suite", "main",
        "reliability suite over recorded sessions",
    ),
    "data-quality": (
        "core.onboarding.data_quality", "run_main",
        "data-quality harness (duplicates/grain checks)",
    ),
    "layered-pipeline": (
        "core.onboarding.harness.layered_pipeline_harness", "main",
        "medallion layered pipeline harness",
    ),
    "pipeline-execution": (
        "core.onboarding.harness.pipeline_execution_harness", "main",
        "generated pipeline SQL execution harness",
    ),
    "kpi-execution": (
        "core.onboarding.kpi.execution_harness", "main",
        "KPI SQL execution harness (same as run-kpi-execution-harness)",
    ),
    "ai-app": (
        "core.onboarding.harness.ai_app_harness", "main",
        "AI app harness (LLM suite scenarios)",
    ),
    "ai-cli": (
        "core.onboarding.harness.ai_cli_harness", "main",
        "AI CLI harness (governed CLI scenarios)",
    ),
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"list", "--list", "-h", "--help"}:
        print("usage: harness <suite> [suite args...]\n\nsuites:")
        for name, (_, _, purpose) in sorted(SUITES.items()):
            print(f"  {name:<22} {purpose}")
        return 0
    suite = argv[0]
    if suite not in SUITES:
        print(f"harness: unknown suite `{suite}`. Run `harness list`.")
        return 2
    module_name, attr, _ = SUITES[suite]
    entry = getattr(importlib.import_module(module_name), attr)
    result = entry(argv[1:])
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
