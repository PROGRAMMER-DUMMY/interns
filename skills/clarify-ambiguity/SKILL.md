---
name: clarify-ambiguity
description: >
  Use when a request is underspecified, ambiguous, assumption-heavy, or likely to produce a wrong,
  unsafe, costly, or irrelevant answer without clarification. Trigger when missing context materially
  affects correctness, safety, user intent, implementation choices, or recommendation quality.
  Do not trigger for clear requests or minor ambiguities that can be handled by stating a reasonable
  assumption.
---

# Clarify Ambiguity

Clarify only when a wrong assumption would materially change the answer or cause wasted work.
If ambiguity is low-risk and reversible, proceed with the most likely interpretation and state the
assumption. If ambiguity is high-impact, irreversible, costly, unsafe, or likely to waste work, ask
one targeted question before proceeding.

## Context First

Before asking, inspect available context:

- Code agents: read relevant files, config, tests, and errors first.
- Terminal agents: run safe read-only commands when useful.
- API agents: use supplied messages, tool outputs, metadata, and attachments.
- Chat-only agents: use conversation history.

Ask only for intent, preference, permissions, or unavailable facts.

## Setup-Level Ambiguity

If the unclear part is the active workflow, workspace, or file set, do not guess. Use the Step 0
workflow from `AGENTS.md`: ask what the user wants to do, scan likely files, present the likely
file set, and ask for confirmation before continuing.

## KPI/Data Mapping Ambiguity

For KPI/query optimization, inspect all available evidence before asking:

1. KPI registry.
2. Data model docs and diagrams.
3. Dataset schema/profile outputs.
4. Data dictionaries, metadata exports, contracts, or SLA files.
5. Catalog metadata if connected.

Ask the user only when the missing or ambiguous mapping materially affects correctness. If a
required dictionary/metadata/catalog file is missing, ask for that file or location directly and
record the request under `workspaces/<project>/interns/reports/open_questions.md`.

## Rules

1. Ask one question only.
2. Ask the highest-leverage question first.
3. Name concrete options when possible.
4. Keep it brief.
5. Do not ask for information you can inspect or infer with high confidence.
6. If the user says "just answer", give a multi-interpretation response.

Good pattern:

```text
Do you mean X or Y? I would assume X unless you meant otherwise.
```

Examples:

| Ambiguous request | Bad clarification | Good clarification |
|---|---|---|
| "Recommend a Python library for data" | "What do you want to do?" | "Analysis/visualization, ML, or databases?" |
| "Fix this code" | "What's wrong?" | "What behavior are you seeing vs. what you expect? Or should I inspect for general issues?" |
| "Use that project" | "Which one?" | "Do you mean the active workspace in `config/tasks.json`, or another `workspaces/<project>` folder?" |

## False Premises

Do not answer as if a questionable premise is true. Correct it, answer the nearest corrected
version, or ask one targeted question.
