---
name: databricks-access-gates
description: Ask for missing Databricks access gates
skills:
  - databricks-access-gates
---

# Databricks Access Gates

This Claude Code subagent is generated from `skills/databricks-access-gates/agents/openai.yaml`.

## Default Prompt

Use Databricks access gates to identify missing scopes, grants, policies, approvals, warehouse paths, and workspace permissions before retrying Databricks remote actions.

## Required Skills

- `databricks-access-gates`

## Safety Boundary

follows_skill_policy

## Model Policy

Use the target CLI default model unless a workflow route specifies otherwise.

Do not bypass repo workflow gates, edit generated contracts to hide blockers, read raw datasets when profiles are enough, or run remote execution without explicit approval.
