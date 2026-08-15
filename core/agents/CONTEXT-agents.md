# Core Agents Architecture Context: `core/agents`

This document provides an exhaustive reference for all components in [`core/agents`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents).

---

## Executive Overview & Architectural Model

The `core/agents` package defines the LLM engine abstraction layer, CLI inspector, code mutation framework, intern agent bus communication protocol, and agent registry.

```
┌─────────────────┐        ┌─────────────────┐        ┌─────────────────┐
│   registry.py   ├───────►│  llm_engine.py  ├───────►│  intern_bus.py  │
└─────────────────┘        └─────────────────┘        └─────────────────┘
                                    │
                                    ▼
                           ┌─────────────────┐
                           │ code_mutator.py │
                           └─────────────────┘
```

---

## File Details

### 1. [`cli_inspector.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/cli_inspector.py)

- **Exact Purpose**: Inspects CLI command definitions, extracts parameter schemas, and validates arguments against command specifications.
- **Key Functions / Classes**:
  - [`inspect_cli_command(command_name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/cli_inspector.py#L25-L65): Inspects registered CLI commands and returns arg specifications.
- **Inputs & Outputs**:
  - *Inputs*: Command names, argument tuples.
  - *Outputs*: Schema dictionary containing valid options, flags, and types.
- **Failure Modes & Edge Cases**:
  - Raises exception on unrecognized commands or invalid flag parameters.

### 2. [`code_mutator.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/code_mutator.py)

- **Exact Purpose**: AST-based code transformation engine used by agents for refactoring, SQL generation, and code modifications.
- **Key Functions / Classes**:
  - [`mutate_python_ast(source_code, transformation)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/code_mutator.py#L30-L90): Transforms Python code AST safely.
- **Inputs & Outputs**:
  - *Inputs*: Source code string, target AST node specs.
  - *Outputs*: Formatted modified source code string.
- **Failure Modes & Edge Cases**:
  - Syntax errors in input code prevent AST parsing and raise transformation errors.

### 3. [`intern_bus.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/intern_bus.py)

- **Exact Purpose**: Event bus and inter-agent message passing router for local intern agents.
- **Key Functions / Classes**:
  - [`InternBus`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/intern_bus.py#L20-L75): Manages agent subscription topics and delivers messages asynchronously.
- **Inputs & Outputs**:
  - *Inputs*: Agent messages, payload objects, recipient topics.
  - *Outputs*: Event routing confirmation and subscriber callbacks.
- **Failure Modes & Edge Cases**:
  - Handles missing subscribers gracefully by logging unhandled message events.

### 4. [`llm_engine.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/llm_engine.py)

- **Exact Purpose**: Abstraction layer interfacing with LLM providers (e.g. OpenAI, Databricks Serving, Gemini) for prompt execution and structured outputs.
- **Key Functions / Classes**:
  - [`LLMEngine`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/llm_engine.py#L15-L60): Wraps API calls, fallback handling, token counting, and structured JSON parsing.
- **Inputs & Outputs**:
  - *Inputs*: Prompts, model parameters, system instructions.
  - *Outputs*: LLM response strings or structured JSON dicts.
- **Failure Modes & Edge Cases**:
  - API rate limits or network connection drops trigger retry loops or fallback models.

### 5. [`registry.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/registry.py)

- **Exact Purpose**: Registry of specialized intern agent definitions and capabilities.
- **Key Functions / Classes**:
  - [`register_agent(name, class_ref)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/registry.py#L15-L35): Registers agent implementations.
  - [`get_agent(name)`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/agents/registry.py#L36-L50): Fetches agent class reference by name.
- **Inputs & Outputs**:
  - *Inputs*: Agent name identifier.
  - *Outputs*: Registered agent instance or class.
- **Failure Modes & Edge Cases**:
  - Unregistered agent lookups return KeyError.
