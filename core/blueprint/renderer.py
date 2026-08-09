"""Render the reviewable pipeline blueprint.

The blueprint is the ONE artifact a human reads before the platform builds
anything, so it is written to be disagreed with: the concrete pipeline as a
mermaid graph using the workspace's real table names, what will be provisioned,
which dbt model serves which KPI, the engine and compute choice with two
cost-annotated options, the data-quality plan, and -- next to every choice --
the rule that fired, its evidence, and the vendor URL its threshold came from.

Re-runs against a confirmed blueprint render a DIFF instead of a fresh proposal.
Re-reading an unchanged twelve-section document to find the one line that moved
is how a review gate degrades into a rubber stamp.

Nothing here executes anything. Every line is a proposal.
"""
from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.blueprint.decisions import (
    STATUS_BLOCKED,
    build_payload,
    load_table,
    load_workspace_inputs,
)
from core.onboarding.panel_contract import attach_stage_routing
from core.storage.workspace_layout import WorkspaceLayout

BLUEPRINT_VERSION = 1
BLUEPRINT_ARTIFACT = "solution_blueprint/current.json"
# STAGE_ROUTING key (core/onboarding/workspace/delegation.py) this artifact belongs to.
ROUTING_STAGE = "blueprint_review"

ADDITIVE_LINE = (
    "Everything listed here is ADDITIVE: catalogs, schemas, external locations, "
    "tables, models and DAGs are CREATED. Nothing existing is modified, dropped, "
    "overwritten, or re-granted by this blueprint."
)

# How a discovered table gets landed in bronze. Ingestion is platform
# infrastructure, not part of the engine decision (engine.yaml ENG-C2).
_INGESTION_STREAMING = "Auto Loader (streaming, checkpointed)"
_INGESTION_ZERO_COPY = "external table (zero copy, registered in place)"
_INGESTION_BATCH = "COPY INTO (batch, idempotent by file)"

# `freshness_sla` option id (core/intake/interview.py) -> Airflow schedule. The
# minute offset is hashed from the workspace name so a fleet of workspaces does
# not stampede the same 2am slot.
_SCHEDULES = {
    "sub_minute": "continuous (real-time mode) - not a cron schedule",
    "minutes": "*/15 * * * *",
    "hourly": "{minute} * * * *",
    "daily": "{minute} 4 * * *",
    "weekly_or_monthly": "{minute} 4 * * 1",
}


@dataclass
class BlueprintRenderResult:
    current_json_path: str
    current_markdown_path: str
    mode: str  # "fresh" | "diff"
    decided_count: int
    blocked_count: int
    missing_facts: list[str]
    ok: bool = True

    def summary(self) -> dict[str, Any]:
        return attach_stage_routing(
            {
                "current_json_path": self.current_json_path,
                "current_markdown_path": self.current_markdown_path,
                "mode": self.mode,
                "decided_count": self.decided_count,
                "blocked_count": self.blocked_count,
                "missing_facts": list(self.missing_facts),
                "ok": self.ok,
            },
            ROUTING_STAGE,
        )


def blueprint_dir(layout: WorkspaceLayout) -> Path:
    return layout.reports_dir / "solution_blueprint"


def _safe(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(value).lower())
    return cleaned.strip("_") or "unnamed"


def _node_id(prefix: str, value: str) -> str:
    return f"{prefix}_{_safe(value)}"


def _bytes_human(num: Any) -> str:
    if not isinstance(num, (int, float)):
        return "size not measured"
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _ingestion_method(table: dict[str, Any]) -> str:
    if table.get("is_streaming"):
        return _INGESTION_STREAMING
    if str(table.get("format") or "").lower() == "delta":
        return _INGESTION_ZERO_COPY
    return _INGESTION_BATCH


def _schedule(workspace: str, facts: dict[str, Any]) -> str:
    sla = (facts.get("latency_sla") or {}).get("value")
    if not sla:
        return "not scheduled: the freshness SLA question has not been answered"
    template = _SCHEDULES.get(str(sla))
    if template is None:
        return f"not scheduled: unrecognised freshness answer `{sla}`"
    minute = zlib.crc32(str(workspace).encode("utf-8")) % 60
    return template.format(minute=minute)


def _decisions_by_name(payload: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [d for d in payload.get("decisions", []) if d.get("decision") == name]


def _primary(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    for decision in _decisions_by_name(payload, name):
        if decision.get("status") != STATUS_BLOCKED:
            return decision
    blocked = _decisions_by_name(payload, name)
    return blocked[0] if blocked else None


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def build_structure(
    *,
    workspace: str,
    catalog: str,
    schemas: dict[str, str],
    decisions: dict[str, Any],
    discovery: dict[str, Any],
    kpi_summary: dict[str, Any],
    source_declaration: dict[str, Any],
) -> dict[str, Any]:
    """The blueprint's data model. The markdown and the diff both read this."""
    tables = [t for t in (discovery.get("tables") or []) if isinstance(t, dict)]
    facts = decisions.get("facts", {})
    landed = [
        {
            "source_name": str(t.get("name") or ""),
            "source_path": str(t.get("path") or ""),
            "format": str(t.get("format") or "unknown"),
            "size_bytes": t.get("size_bytes"),
            "row_estimate": t.get("row_estimate"),
            "arrival_pattern": str(t.get("arrival_pattern") or "unknown"),
            "arrival_evidence": str(t.get("arrival_evidence") or ""),
            "ingestion": _ingestion_method(t),
            "bronze": f"{catalog}.{schemas['bronze']}.{_safe(t.get('name') or '')}",
            "silver": f"{catalog}.{schemas['silver']}.{_safe(t.get('name') or '')}",
        }
        for t in tables
    ]
    kpi_models = [
        {
            "kpi_id": str(k.get("kpi_id") or ""),
            "name": str(k.get("name") or ""),
            "dbt_model": f"gold_{_safe(k.get('kpi_id') or '')}",
            "target": f"{catalog}.{schemas['gold']}.gold_{_safe(k.get('kpi_id') or '')}",
            "source_tables": [str(t) for t in (k.get("source_tables") or [])],
        }
        for k in (kpi_summary.get("kpis") or [])
    ]
    location = str(source_declaration.get("location") or discovery.get("connector") or "")
    provisioning = [
        {"object": "catalog", "name": catalog, "credential_ref": ""},
        *[
            {"object": "schema", "name": f"{catalog}.{schema}", "credential_ref": ""}
            for schema in (schemas["bronze"], schemas["silver"], schemas["gold"])
        ],
    ]
    if location:
        provisioning.insert(
            0,
            {
                "object": "external location",
                "name": f"{catalog}_root -> {location}",
                "credential_ref": str(source_declaration.get("credential_ref") or "")
                or "NOT DECLARED - a storage credential reference is required",
            },
        )
    return {
        "workspace": workspace,
        "catalog": catalog,
        "schemas": dict(schemas),
        "source": {
            "connector": str(source_declaration.get("type") or discovery.get("connector") or ""),
            "location": location,
            "credential_ref": str(source_declaration.get("credential_ref") or ""),
            "scanned_at": str(discovery.get("scanned_at") or ""),
        },
        "tables": landed,
        "kpi_models": kpi_models,
        "provisioning": provisioning,
        "schedule": _schedule(workspace, facts),
        "dag_name": f"{_safe(Path(str(workspace)).name)}_pipeline",
    }


def render_mermaid(structure: dict[str, Any], decisions: dict[str, Any]) -> str:
    """A flowchart of the concrete pipeline, with the workspace's real names."""
    schemas = structure["schemas"]
    engine = _primary(decisions, "engine") or {}
    engine_label = engine.get("choice") or "engine NOT DECIDED"
    lines = ["```mermaid", "flowchart TD"]

    source_label = structure["source"]["location"] or structure["source"]["connector"] or "source"
    lines.append(f'    SRC["Source: {source_label}"]')

    if not structure["tables"]:
        lines.append('    SRC --> NONE["no tables discovered yet"]')
    for table in structure["tables"]:
        name = table["source_name"]
        src = _node_id("t", name)
        bronze = _node_id("b", name)
        silver = _node_id("s", name)
        lines.append(f'    {src}["{name} ({table["format"]}, {_bytes_human(table["size_bytes"])})"]')
        lines.append(f"    SRC --> {src}")
        lines.append(f'    {src} -->|"{table["ingestion"]}"| {bronze}["{schemas["bronze"]}.{_safe(name)}"]')
        lines.append(f'    {bronze} -->|"{engine_label}"| {silver}["{schemas["silver"]}.{_safe(name)}"]')

    if structure["kpi_models"]:
        # Only draw a silver->gold edge the registry actually asserts. Where a KPI
        # does not name its sources, everything routes through one silver-layer
        # node rather than inventing a fan-in that looks like measured lineage.
        unsourced = [m for m in structure["kpi_models"] if not m.get("source_tables")]
        if unsourced and structure["tables"]:
            lines.append(
                f'    SILVER["{schemas["silver"]} layer '
                f'({len(structure["tables"])} models) - per-KPI sources not declared"]'
            )
            for table in structure["tables"]:
                lines.append(f"    {_node_id('s', table['source_name'])} --> SILVER")
        for model in structure["kpi_models"]:
            gold = _node_id("g", model["kpi_id"])
            lines.append(f'    {gold}["{schemas["gold"]}.{model["dbt_model"]}"]')
            sources = model.get("source_tables") or []
            if sources:
                for source in sources:
                    lines.append(f"    {_node_id('s', source)} --> {gold}")
            elif structure["tables"]:
                lines.append(f"    SILVER --> {gold}")
            lines.append(f"    {gold} --> DAG")
    else:
        lines.append('    NOKPI["no KPI models: KPI registry is empty"] --> DAG')

    lines.append(f'    DAG["Airflow DAG {structure["dag_name"]} ({structure["schedule"]})"]')
    lines.append('    DAG --> DASH["Dashboard + KPI results"]')
    lines.append("```")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# markdown
# --------------------------------------------------------------------------


def _rule_callout(decision: dict[str, Any]) -> list[str]:
    if decision.get("status") == STATUS_BLOCKED:
        head = (
            f"> **[blocked] {decision['decision']} - rule that fired: "
            f"`{decision['rule_id']}` could not be evaluated.**"
        )
    else:
        head = (
            f"> **Rule that fired: `{decision['rule_id']}` -> `{decision['choice']}` "
            f"(source: {decision['source']})**"
        )
    lines = [head, ">"]
    lines += [f"> - {item}" for item in decision.get("evidence", [])]
    for note in decision.get("notes", []):
        lines.append(f"> - note: {note}")
    if decision.get("missing_facts"):
        lines.append(f"> - missing fact(s): `{'`, `'.join(decision['missing_facts'])}`")
    return lines + [""]


def _alternatives_table(decision: dict[str, Any]) -> list[str]:
    alts = decision.get("alternatives") or []
    if not alts:
        return []
    lines = ["| Option | Cost / trade-off |", "|---|---|"]
    chosen = decision.get("choice")
    if chosen:
        lines.append(f"| `{chosen}` (chosen) | selected by `{decision['rule_id']}` |")
    for alt in alts:
        lines.append(f"| `{alt['choice']}` | {alt['cost_note']} |")
    return lines + [""]


def render_markdown(structure: dict[str, Any], decisions: dict[str, Any]) -> str:
    schemas = structure["schemas"]
    blocked = [d for d in decisions.get("decisions", []) if d.get("status") == STATUS_BLOCKED]

    lines = [
        "# Solution blueprint",
        "",
        f"Workspace: `{structure['workspace']}`",
        f"Source: `{structure['source']['location'] or 'not declared'}` "
        f"(connector `{structure['source']['connector'] or 'not declared'}`)",
        f"Target: catalog `{structure['catalog']}` -> "
        f"`{schemas['bronze']}` / `{schemas['silver']}` / `{schemas['gold']}`",
        "",
        f"**NOTHING HAS BEEN CREATED YET.** {ADDITIVE_LINE}",
        "",
    ]
    if blocked:
        names = sorted({m for d in blocked for m in d.get("missing_facts", [])})
        lines += [
            f"[blocked] {len(blocked)} decision(s) cannot be made yet. Missing fact(s): "
            f"`{'`, `'.join(names)}`. Nothing was assumed in their place - measure it in "
            "discovery or answer it in the intake interview, then re-run.",
            "",
        ]

    lines += ["## Pipeline", "", render_mermaid(structure, decisions), ""]

    lines += ["## Provisioning (all additive)", "", "| Object | Name | Credential reference |", "|---|---|---|"]
    for item in structure["provisioning"]:
        lines.append(f"| {item['object']} | `{item['name']}` | {item['credential_ref'] or '-'} |")
    lines += [
        "",
        "Credential *references* only. No secret value is read, printed, or stored by this blueprint.",
        "",
    ]

    lines += ["## Ingestion per table", ""]
    if structure["tables"]:
        lines += [
            "| Source table | Format | Size | Arrival | Ingestion | Bronze | Silver |",
            "|---|---|---|---|---|---|---|",
        ]
        for table in structure["tables"]:
            lines.append(
                f"| `{table['source_name']}` | {table['format']} | {_bytes_human(table['size_bytes'])} "
                f"| {table.get('arrival_pattern', 'unknown')} | {table['ingestion']} "
                f"| `{table['bronze']}` | `{table['silver']}` |"
            )
        lines += ["", "Arrival pattern is measured from the source's own file times:", ""]
        for table in structure["tables"]:
            lines.append(
                f"- `{table['source_name']}`: {table.get('arrival_pattern', 'unknown')} - "
                f"{table.get('arrival_evidence') or 'not measured'}"
            )
    else:
        lines.append("_No tables discovered. Run the discovery scan first._")
    lines.append("")

    lines += ["## KPI -> model mapping", ""]
    if structure["kpi_models"]:
        lines += ["| KPI | Name | dbt model | Target |", "|---|---|---|---|"]
        for model in structure["kpi_models"]:
            lines.append(
                f"| `{model['kpi_id']}` | {model['name']} | `{model['dbt_model']}` | `{model['target']}` |"
            )
    else:
        lines.append("_No KPIs in the registry yet; the gold layer has nothing to serve._")
    lines.append("")

    lines += ["## Velocity lane", ""]
    velocity = _primary(decisions, "velocity_lane")
    if velocity is None:
        lines += ["_No rule was evaluated._", ""]
    else:
        lines += [
            f"Lane for every source in this workspace: `{velocity.get('choice') or 'NOT DECIDED'}`.",
            "",
        ]
        lines += _rule_callout(velocity)
        lines += _alternatives_table(velocity)

    for name, heading in (("engine", "Engine"), ("compute", "Compute")):
        decision = _primary(decisions, name)
        lines += [f"## {heading}", ""]
        if decision is None:
            lines += ["_No rule was evaluated._", ""]
            continue
        lines += _rule_callout(decision)
        lines += _alternatives_table(decision)

    lines += ["## Modeling", ""]
    modeling = _decisions_by_name(decisions, "modeling")
    if not modeling:
        lines += ["_No rule was evaluated._", ""]
    for decision in modeling:
        lines += _rule_callout(decision)
    defaults = load_table("modeling").get("defaults") or {}
    if defaults:
        lines += ["Physical defaults applied to whatever technique fired:", ""]
        lines += [f"- **{key}**: {value}" for key, value in defaults.items()]
        lines.append("")

    lines += ["## Data quality", ""]
    dq = _decisions_by_name(decisions, "dq")
    if not dq:
        lines += ["_No rule was evaluated._", ""]
    else:
        lines += ["| Layer | Checks | Severity | Generated as | Rule |", "|---|---|---|---|---|"]
        for decision in dq:
            notes = {
                part.split(": ", 1)[0]: part.split(": ", 1)[1]
                for part in decision.get("notes", [])
                if ": " in part
            }
            lines.append(
                f"| {notes.get('layer', '-')} | {notes.get('checks', '-')} "
                f"| {notes.get('severity', '-')} | {notes.get('generated_as', '-')} "
                f"| `{decision['rule_id']}` |"
            )
        lines.append("")
        tiers = load_table("dq").get("severity_tiers") or {}
        lines += [f"- **{tier}**: {meaning}" for tier, meaning in tiers.items()]
        lines.append("")

    lines += [
        "## Orchestration",
        "",
        f"- Airflow DAG `{structure['dag_name']}`: ingest -> dbt build (Cosmos) -> DQ publish "
        "(write-audit-publish swap) -> dashboard refresh.",
        f"- Schedule: `{structure['schedule']}` (minute offset hashed from the workspace name so "
        "workspaces do not stampede the same slot).",
        "- A failed gold check leaves the last-good published table serving; it does not overwrite it.",
        "",
        "## Confirm",
        "",
        "```",
        "confirm-blueprint --workspace <ws> --confirmed-by \"<your name>\"",
        "```",
        "",
        f"{ADDITIVE_LINE}",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# diff mode
# --------------------------------------------------------------------------


def _decision_key(decision: dict[str, Any]) -> str:
    return f"{decision.get('decision')}/{decision.get('rule_id')}"


def _side(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule_id": decision.get("rule_id"),
        "choice": decision.get("choice"),
        "status": decision.get("status"),
    }


def diff_payloads(confirmed: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """What changed since the confirmed blueprint."""
    old = {_decision_key(d): d for d in (confirmed.get("decisions") or {}).get("decisions", [])}
    new = {_decision_key(d): d for d in (current.get("decisions") or {}).get("decisions", [])}
    changed = [
        {
            "key": key,
            "from": _side(old[key]),
            "to": _side(new[key]),
        }
        for key in sorted(set(old) & set(new))
        if old[key].get("choice") != new[key].get("choice")
        or old[key].get("status") != new[key].get("status")
    ]

    # A different rule firing for the same decision family is a CHANGE, not a
    # removal plus an unrelated addition. "engine: ENG-R4 -> ENG-R3" is the
    # sentence a reviewer needs; two bullets in different sections is not.
    only_old = sorted(set(old) - set(new))
    only_new = sorted(set(new) - set(old))
    for family in sorted({old[k]["decision"] for k in only_old} & {new[k]["decision"] for k in only_new}):
        pairs = list(
            zip(
                [k for k in only_old if old[k]["decision"] == family],
                [k for k in only_new if new[k]["decision"] == family],
            )
        )
        for old_key, new_key in pairs:
            changed.append({"key": family, "from": _side(old[old_key]), "to": _side(new[new_key])})
            only_old.remove(old_key)
            only_new.remove(new_key)
    changed.sort(key=lambda c: c["key"])
    old_tables = {t["source_name"] for t in (confirmed.get("structure") or {}).get("tables", [])}
    new_tables = {t["source_name"] for t in (current.get("structure") or {}).get("tables", [])}
    old_kpis = {k["kpi_id"] for k in (confirmed.get("structure") or {}).get("kpi_models", [])}
    new_kpis = {k["kpi_id"] for k in (current.get("structure") or {}).get("kpi_models", [])}
    return {
        "decisions_added": [{"key": k, "choice": new[k].get("choice")} for k in only_new],
        "decisions_removed": [{"key": k, "choice": old[k].get("choice")} for k in only_old],
        "decisions_changed": changed,
        "changed_decisions": _changed_decisions(confirmed, current),
        "tables_added": sorted(new_tables - old_tables),
        "tables_removed": sorted(old_tables - new_tables),
        "kpis_added": sorted(new_kpis - old_kpis),
        "kpis_removed": sorted(old_kpis - new_kpis),
    }


def _changed_facts(confirmed: dict[str, Any], current: dict[str, Any]) -> dict[str, str]:
    """Fact name -> the intake question whose changed answer produced it.

    Facts carry their own origin (``intake_answers.<question_id>``), so the
    question that moved a decision is read off the record rather than guessed.
    """
    old = (confirmed.get("decisions") or {}).get("facts") or {}
    new = (current.get("decisions") or {}).get("facts") or {}
    changed: dict[str, str] = {}
    for name, entry in new.items():
        before = old.get(name)
        if not isinstance(before, dict) or before.get("value") == entry.get("value"):
            continue
        origin = str(entry.get("origin") or "")
        changed[name] = origin.split("intake_answers.", 1)[1] if "intake_answers." in origin else name
    return changed


def _changed_decisions(confirmed: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Per decision family: old choice, new choice, and which changed ANSWER drove
    it. "The star became feature tables because you changed `consumers`" is the
    sentence a re-reviewer needs; a decision id moving on its own is not."""
    changed_facts = _changed_facts(confirmed, current)
    out: list[dict[str, Any]] = []
    for family in sorted(
        {d.get("decision") for d in (current.get("decisions") or {}).get("decisions", [])}
        & {d.get("decision") for d in (confirmed.get("decisions") or {}).get("decisions", [])}
    ):
        before = _primary(confirmed.get("decisions") or {}, str(family))
        after = _primary(current.get("decisions") or {}, str(family))
        if before is None or after is None:
            continue
        if (before.get("choice"), before.get("rule_id")) == (after.get("choice"), after.get("rule_id")):
            continue
        evidence = [str(item) for item in (after.get("evidence") or [])]
        out.append(
            {
                "decision": family,
                "from_choice": before.get("choice"),
                "to_choice": after.get("choice"),
                "from_rule_id": before.get("rule_id"),
                "to_rule_id": after.get("rule_id"),
                "driven_by": sorted(
                    {
                        question
                        for fact, question in changed_facts.items()
                        if any(line.startswith(f"{fact} = ") for line in evidence)
                    }
                ),
            }
        )
    return out


def _is_empty_diff(diff: dict[str, Any]) -> bool:
    return not any(diff.values())


def render_diff_markdown(
    structure: dict[str, Any], diff: dict[str, Any], confirmation: dict[str, Any]
) -> str:
    lines = [
        "# Solution blueprint - changes since confirmation",
        "",
        f"Workspace: `{structure['workspace']}`",
        f"Confirmed by `{confirmation.get('confirmed_by', '')}` at "
        f"{confirmation.get('confirmed_at', '')} (source: {confirmation.get('source', '')})",
        "",
        f"**NOTHING HAS BEEN CREATED YET.** {ADDITIVE_LINE}",
        "",
        "## Changes",
        "",
    ]
    if _is_empty_diff(diff):
        lines += ["_No change since the confirmed blueprint._", ""]
        return "\n".join(lines)

    def block(title: str, key: str, fmt) -> None:
        items = diff.get(key) or []
        if not items:
            return
        lines.append(f"### {title}")
        lines.append("")
        lines.extend(f"- {fmt(item)}" for item in items)
        lines.append("")

    block("Added decisions", "decisions_added", lambda i: f"`{i['key']}` -> `{i['choice']}`")
    block("Removed decisions", "decisions_removed", lambda i: f"`{i['key']}` (was `{i['choice']}`)")
    block(
        "Changed decisions",
        "decisions_changed",
        lambda i: f"`{i['key']}`: `{i['from']['rule_id']}` -> `{i['from']['choice']}` "
        f"({i['from']['status']}) becomes `{i['to']['rule_id']}` -> `{i['to']['choice']}` "
        f"({i['to']['status']})",
    )
    block(
        "Changed decisions and the answers that drove them",
        "changed_decisions",
        lambda i: f"`{i['decision']}`: `{i['from_choice']}` -> `{i['to_choice']}` "
        f"(rule `{i['from_rule_id']}` -> `{i['to_rule_id']}`), driven by changed answer(s): "
        + (", ".join(f"`{q}`" for q in i["driven_by"]) or "none of the intake answers"),
    )
    block("Added tables", "tables_added", lambda i: f"`{i}`")
    block("Removed tables", "tables_removed", lambda i: f"`{i}`")
    block("Added KPIs", "kpis_added", lambda i: f"`{i}`")
    block("Removed KPIs", "kpis_removed", lambda i: f"`{i}`")

    lines += [
        "## Re-confirm",
        "",
        "The previous confirmation covered the previous plan. Re-confirm to accept these changes:",
        "",
        "```",
        "confirm-blueprint --workspace <ws> --confirmed-by \"<your name>\"",
        "```",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def _assert_playback_confirmed(layout: WorkspaceLayout, workspace: str | Path) -> None:
    """The alignment gate. A blueprint is a design built on what the platform
    THINKS it was told; the playback is where that reading is checked. Refusing
    here is cheaper than discovering the misread after provisioning.

    Re-answering a question after the blueprint was confirmed clears this gate
    again (see ``IntakeAnswerRecorder``): the previous confirmation covered the
    previous requirement.
    """
    from core.intake.interview import PLAYBACK_CONFIRM_QUESTION, load_intake_answers

    if PLAYBACK_CONFIRM_QUESTION in load_intake_answers(layout):
        return
    playback = _rel(layout.reports_dir / "intake_playback" / "current.md", layout.project_root)
    raise ValueError(
        "the understanding playback has not been confirmed. Read "
        f"`{playback}` (written by `prepare-intake-panel`), then run: "
        f"apply-intake-answer --workspace {workspace} "
        '--question playback_confirm --answer confirmed --answered-by "<your name>". '
        "Nothing has been rendered or created."
    )


def load_confirmed(layout: WorkspaceLayout) -> dict[str, Any]:
    path = blueprint_dir(layout) / "current.confirmed.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def prepare_blueprint(
    repo_root: str | Path,
    workspace: str | Path,
    *,
    catalog: str = "",
    bronze_schema: str = "bronze",
    silver_schema: str = "silver",
    gold_schema: str = "gold",
) -> BlueprintRenderResult:
    repo_root = Path(repo_root).resolve()
    layout = WorkspaceLayout(project_root=(repo_root / workspace).resolve())
    layout.ensure_runtime_dirs()

    _assert_playback_confirmed(layout, workspace)
    inputs = load_workspace_inputs(layout)
    decisions = build_payload(workspace=str(workspace), **inputs)

    # Persist the decision record next to the contracts it will drive.
    from core.blueprint.decisions import DECISIONS_ARTIFACT

    (layout.contracts_dir / DECISIONS_ARTIFACT).write_text(
        json.dumps(decisions, indent=2) + "\n", encoding="utf-8"
    )

    structure = build_structure(
        workspace=str(workspace),
        catalog=catalog or _safe(Path(str(workspace)).name),
        schemas={"bronze": bronze_schema, "silver": silver_schema, "gold": gold_schema},
        decisions=decisions,
        discovery=inputs["discovery"],
        kpi_summary=inputs["kpi_summary"],
        source_declaration=inputs["source_declaration"],
    )
    payload = {
        "artifact_type": BLUEPRINT_ARTIFACT,
        "version": BLUEPRINT_VERSION,
        "generated_by": "prepare-blueprint",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "draft",
        "structure": structure,
        "decisions": decisions,
        "additive_only": ADDITIVE_LINE,
    }
    attach_stage_routing(payload, ROUTING_STAGE)

    confirmed = load_confirmed(layout)
    if confirmed:
        diff = diff_payloads(confirmed, payload)
        payload["mode"] = "diff"
        payload["diff"] = diff
        payload["confirmed_at"] = (confirmed.get("confirmation") or {}).get("confirmed_at", "")
        markdown = render_diff_markdown(structure, diff, confirmed.get("confirmation") or {})
    else:
        payload["mode"] = "fresh"
        markdown = render_markdown(structure, decisions)

    out_dir = blueprint_dir(layout)
    out_dir.mkdir(parents=True, exist_ok=True)
    current_json = out_dir / "current.json"
    current_md = out_dir / "current.md"

    # Strangler overlap: a legacy producer writes this same path, so keep its
    # artifact instead of silently overwriting someone's approved plan.
    #
    # KEEP THIS. D1 retired `prepare-solution-blueprint` (it now redirects
    # here), and the plan called for deleting this block along with it -- but
    # `apply-blueprint-answer` is a SECOND entry into the same legacy producer
    # (`core/onboarding/blueprint.py::apply_main` -> `build_blueprint`, which
    # still stamps `generated_by: prepare-solution-blueprint`). Until that
    # command is retired too -- which needs an answer for what replaces
    # blueprint EDITS (exclude/include/as_volume/as_managed) in the new spine,
    # since `prepare-blueprint` has no equivalent -- this net is still load
    # bearing.
    if current_json.exists():
        try:
            prior = json.loads(current_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prior = {}
        if isinstance(prior, dict) and prior.get("generated_by") not in (None, "prepare-blueprint"):
            (out_dir / "current.legacy.json").write_text(
                json.dumps(prior, indent=2) + "\n", encoding="utf-8"
            )
            if current_md.exists():
                (out_dir / "current.legacy.md").write_text(
                    current_md.read_text(encoding="utf-8"), encoding="utf-8"
                )

    current_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    current_md.write_text(markdown, encoding="utf-8")

    all_decisions = decisions.get("decisions", [])
    blocked = [d for d in all_decisions if d.get("status") == STATUS_BLOCKED]
    return BlueprintRenderResult(
        current_json_path=_rel(current_json, repo_root),
        current_markdown_path=_rel(current_md, repo_root),
        mode=str(payload["mode"]),
        decided_count=len(all_decisions) - len(blocked),
        blocked_count=len(blocked),
        missing_facts=list(decisions.get("blocked_facts") or []),
        ok=not blocked,
    )


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "ADDITIVE_LINE",
    "BLUEPRINT_ARTIFACT",
    "BLUEPRINT_VERSION",
    "BlueprintRenderResult",
    "blueprint_dir",
    "build_structure",
    "diff_payloads",
    "load_confirmed",
    "prepare_blueprint",
    "render_diff_markdown",
    "render_markdown",
    "render_mermaid",
]
