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

## Core Principle

Clarify only when a wrong assumption would materially change the answer or cause wasted work.
If ambiguity is low-risk and reversible, proceed with the most likely interpretation and state the
assumption. If ambiguity is high-impact, irreversible, costly, unsafe, or likely to waste work, ask
one targeted question before proceeding.

## Context First

Before asking, inspect any available context the environment provides:

- For code agents: read relevant files, config, tests, and errors first.
- For terminal agents: run safe read-only commands when useful.
- For API agents: use supplied messages, tool outputs, metadata, and attachments.
- For chat-only agents: use conversation history.

Ask only for intent, preference, permissions, or facts that are not reasonably available.

## When To Clarify

Clarify when any of these would materially affect the response:

- Missing referent: "it", "they", "this", "last time", "here".
- Missing scope: version, platform, timeframe, audience, budget, success criteria.
- Multiple valid interpretations: Python language vs snake, Java language vs island, bank account vs river bank.
- Hidden preferences: recommendation, recipe, architecture, UI style, risk tolerance, deployment target.
- Questionable premise: the question assumes an event, fact, failure, or causal relationship that may be false.
- High-stakes ambiguity: medical, legal, financial, security, data loss, production operations.

Do not clarify for minor ambiguity if the common interpretation is likely useful. Answer with a
brief assumption instead.

## Clarification Rules

1. Ask one question only.
2. Ask the highest-leverage question first.
3. Name the concrete options when possible.
4. Keep the question brief.
5. Do not ask for information you can inspect or infer with high confidence.
6. If the user explicitly says "just answer", use a multi-interpretation answer instead of blocking.

Good pattern:

```text
Do you mean X or Y? I would assume X unless you meant otherwise.
```

Bad pattern:

```text
What do you mean?
```

Examples:

| Ambiguous request | Bad clarification | Good clarification |
|---|---|---|
| "Recommend a Python library for data" | "What do you want to do?" | "Are you doing analysis/visualization (pandas, matplotlib), ML, or databases?" |
| "Tell me about bank accounts" | "Which kind?" | "Personal savings/checking, or business accounts - and any particular bank?" |
| "Fix this code" | "What's wrong?" | "What behavior are you seeing vs. what you expect? Or should I look for general issues?" |
| "What's a good recipe?" | "For what?" | "Any dietary preferences or ingredients you want to use?" |

## False Presuppositions

Do not answer as if a false or questionable premise is true.

Use one of these approaches:

- Correct the premise directly.
- Answer the nearest corrected version.
- Flag the assumption and ask one targeted question.

Example:

```text
There were no Winter Olympics in 2021. Did you mean the 2018 games in PyeongChang
or the 2022 games in Beijing?
```

## Multi-Interpretation Response

When you cannot ask or should not block, cover the likely interpretations:

```text
This could mean two things:
- If you mean X: ...
- If you mean Y: ...

I can go deeper once you confirm which one applies.
```

## Confidence

Append a confidence note only when it helps the user judge reliability, especially for:

- High-stakes answers.
- Fresh or time-sensitive facts.
- Research-backed answers.
- Assumption-heavy answers.
- Low or uncertain confidence.

Format:

```text
Confidence: Medium - I assumed X because Y was not specified.
```

Do not add confidence notes to routine, clear, low-risk responses.

## Quick Decision Table

| Situation | Action |
|---|---|
| Clear request | Answer directly |
| Minor ambiguity with obvious default | Answer and state assumption |
| Material ambiguity | Ask one targeted question |
| High-stakes ambiguity | Clarify before advising |
| False premise | Correct or ask |
| User says "just answer" | Give multi-interpretation response |

## Goal

Prevent confidently wrong answers without turning every interaction into an interview.
