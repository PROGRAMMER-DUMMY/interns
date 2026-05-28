# Gemini CLI Agent Reference

This is a repo-local reference for agents operating Gemini CLI against this control-plane repo.
It summarizes the Gemini CLI behavior that matters for governed workspace and KPI workflows.

Source references captured from the Gemini CLI docs:

- Command reference: `https://geminicli.com/docs/reference/commands/`
- Configuration reference: `https://geminicli.com/docs/reference/configuration/`
- Memory import reference: `https://geminicli.com/docs/reference/memport/`
- Policy engine reference: `https://geminicli.com/docs/reference/policy-engine/`
- Tools reference: `https://geminicli.com/docs/reference/tools/`

## Operational Rules For This Repo

- For generated workflow panels, render the generated Markdown artifact as the human-facing answer.
- Do not replace governed panels with Gemini's generic `ask_user`, `Ask User`, or `Answer Questions` UI.
- Use JSON artifacts only for structured option ids, buttons, answer application, and automation.
- For RCM KPI blocker panels, prefer the repo-local custom commands:
  - `/commands reload`
  - `/rcm:panel`
  - `/rcm:answer option_a`
  - `/rcm:custom <accepted mapping, formula, or rule>`
- After changing `GEMINI.md`, `.gemini/settings.json`, `.gemini/commands/*.toml`, or `.gemini/agents/*`,
  run the relevant reload command in Gemini CLI:
  - `/memory refresh` for context files.
  - `/commands reload` for custom slash commands.
  - `/agents reload` for subagents.

## Slash Commands

Gemini CLI slash commands control the CLI itself. Important commands for this repo:

- `/commands list`: list discovered custom command `.toml` files.
- `/commands reload`: reload custom commands from user, project, MCP prompts, and extensions.
- `/memory list`: show loaded `GEMINI.md` context files.
- `/memory refresh`: reload hierarchical memory from configured context files.
- `/memory show`: inspect the full loaded instructional context.
- `/agents list`: list discovered subagents.
- `/agents reload`: reload subagents from `~/.gemini/agents` and `.gemini/agents`.
- `/tools desc`: inspect available tool descriptions.
- `/policies list`: inspect active policy rules by mode.
- `/plan`: switch to read-only planning mode.

Custom commands are loaded from `.toml` files in:

- user-level `~/.gemini/commands/`
- project-level `.gemini/commands/`
- extensions and MCP prompts

Project command namespaces come from directories. For example:

```text
.gemini/commands/rcm/panel.toml -> /rcm:panel
.gemini/commands/rcm/answer.toml -> /rcm:answer
.gemini/commands/rcm/custom.toml -> /rcm:custom
```

## Configuration

Gemini CLI configuration precedence, from lower to higher:

1. hardcoded defaults
2. system defaults
3. user settings: `~/.gemini/settings.json`
4. project settings: `.gemini/settings.json`
5. system override settings
6. environment variables
7. command-line flags

Project settings live at `.gemini/settings.json`. In this repo they should keep
`GEMINI.md`, `AGENTS.md`, `TOOLS.md`, and `.agents/tools.json` available as context.

Important settings for this repo:

- `context.fileName`: context files to load.
- `context.includeDirectoryTree`: whether to include project tree context.
- `context.fileFiltering.respectGitIgnore`: affects `@` file inclusion and file discovery.
- `model.summarizeToolOutput`: can cause large shell output to be summarized.
- `tools.truncateToolOutputThreshold`: controls shell output truncation.
- `tools.allowed`, `tools.confirmationRequired`, `tools.exclude`: tool access controls.
- `policyPaths`, `adminPolicyPaths`: policy file locations.
- `hooks.*`: lifecycle hooks, if enabled.

For governed panels, do not rely on large shell output rendering. Read and post the Markdown artifact
as the model answer. Tool output can be summarized or truncated by Gemini CLI.

## Tools

Gemini CLI tools are invoked automatically by the model or manually with prompt syntax:

- `@path`: reads files/directories through `read_many_files`.
- `!command`: executes a shell command through `run_shell_command`.

Important built-in tools:

- `run_shell_command`: executes shell commands and normally requires confirmation.
- `read_file`, `read_many_files`, `glob`, `grep_search`, `list_directory`: read/search tools.
- `replace`, `write_file`: edit tools.
- `ask_user`: interactive clarification dialog.
- `activate_skill`: loads Gemini skills.
- `enter_plan_mode`, `exit_plan_mode`: planning flow.
- `google_web_search`, `web_fetch`: web tools.

For this repo, `ask_user` is not appropriate for generated blocker, approval, KPI-generation,
data-model, duplicate-review, or pipeline-format panels. Render `current.md` instead.

Relevant tool argument keys for policy rules:

- `run_shell_command`: `command`, `description`, `dir_path`, `is_background`
- `read_file`: `file_path`, `start_line`, `end_line`
- `read_many_files`: `include`, `exclude`, `recursive`, `useDefaultExcludes`
- `write_file`: `file_path`, `content`
- `replace`: `file_path`, `old_string`, `new_string`, `instruction`, `allow_multiple`
- `ask_user`: `questions`

## Policy Engine

Policies are TOML rules that decide whether a tool call is allowed, denied, or asks the user.

Rule shape:

```toml
[[rule]]
toolName = "run_shell_command"
commandPrefix = "git"
decision = "ask_user"
priority = 100
```

Decisions:

- `allow`: execute without user interaction.
- `deny`: block the tool call.
- `ask_user`: prompt for approval; in non-interactive mode this is treated as deny.

Important policy facts:

- User policies live under `~/.gemini/policies/*.toml`.
- Admin policies live under OS-specific admin policy directories.
- Workspace/project policies under `.gemini/policies` are documented as currently non-functional.
- Use user or admin policies for enforceable controls.
- Higher priority wins.
- `commandPrefix` and `commandRegex` simplify shell command policy rules.

Recommended safety policy ideas for this repo:

```toml
[[rule]]
toolName = "ask_user"
argsPattern = "blocker_question_panel|Answer Questions|Ask User"
decision = "ask_user"
priority = 500
```

The policy engine can govern tool calls, but it cannot force a normal natural-language prompt
to route through a project custom command. Use explicit slash commands for deterministic routing.

## Memory And Context

Gemini CLI loads hierarchical memory from configured context filenames, usually `GEMINI.md`.
The active memory can be inspected with:

```text
/memory list
/memory show
/memory refresh
```

This repo's project context should include:

- `GEMINI.md`
- `AGENTS.md`
- `TOOLS.md`
- `.agents/tools.json`

Use `/memory refresh` after editing project instructions. Use `/commands reload` after editing
`.gemini/commands/*.toml`.

## For Example RCM Workflow Notes

For `workspaces/Healthcare-RCM-Data-Platform`, Hospital A isolation is stored in:

```text
workspaces/Healthcare-RCM-Data-Platform/workspace_settings.json
```

Expected content:

```json
{
  "dataset_allowlist": [
    "datasets/EMR/trendytech-hospital-a"
  ]
}
```

Workspace discovery can list Hospital B files because it only lists available files. Onboarding,
profiling, mapping, and executable planning must honor the allowlist and use Hospital A only.

Reliable Gemini operator sequence:

```text
/memory refresh
/commands reload
/rcm:panel
```

Then answer blockers with:

```text
/rcm:answer option_a
/rcm:custom <accepted mapping, formula, or business rule>
```

