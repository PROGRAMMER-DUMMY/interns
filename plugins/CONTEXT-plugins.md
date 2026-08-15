# Plugins Context: `plugins`

This document provides an exhaustive reference for all components in `plugins`.

---

## Executive Overview & Architectural Model

The `plugins` directory contains extension packages for external agents, MCP servers, and IDE tools. Currently, it houses `plugins/autoresearch-workflow`, which packages governed operator guidance and skills for external orchestrators (Codex, Slack, Teams, MCP wrappers).

---

## File Details

### 1. [`autoresearch-workflow/.codex-plugin/plugin.json`](file:///C:/Users/shubh/OneDrive/Desktop/interns/plugins/autoresearch-workflow/.codex-plugin/plugin.json)

- **Exact Purpose**: Codex plugin metadata manifest defining plugin display name, capabilities (`workspace-flow`, `session-snapshot`, `guardrail-validation`), skills path, and default prompts.

### 2. [`autoresearch-workflow/.mcp.json`](file:///C:/Users/shubh/OneDrive/Desktop/interns/plugins/autoresearch-workflow/.mcp.json)

- **Exact Purpose**: Model Context Protocol (MCP) server configuration file (currently empty server map `{}`).

### 3. [`autoresearch-workflow/scripts/README.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/plugins/autoresearch-workflow/scripts/README.md)

- **Exact Purpose**: Transport adapter guidelines instructing developers to invoke existing repo CLI commands (`workspace-flow`) rather than duplicating workflow logic in plugin scripts.

### 4. [`autoresearch-workflow/skills/workspace-flow-operator/SKILL.md`](file:///C:/Users/shubh/OneDrive/Desktop/interns/plugins/autoresearch-workflow/skills/workspace-flow-operator/SKILL.md)

- **Exact Purpose**: Governed skill specification mapping external chat/bot interactions to core repo commands (`uv run workspace-flow start`, `status`, `answer`, `results`, `session-snapshot`, `validate-workflow-guardrails`).

---

## 🧹 Code Hygiene & Integrity Audit

- 💀 **Dead Code**: None.
- 🔌 **Unwired Components**: None.
- 👯 **Logic & Code Duplication**: None. Emphasizes zero duplication by delegating directly to `workspace-flow` CLI commands.
- ⚠️ **Broken References & Mismatches**: None.
