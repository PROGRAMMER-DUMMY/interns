# Repo Hygiene Notes

This repository should be staged with explicit paths only. Avoid `git add -A`
because workspace inputs, generated outputs, local state, and human-owned config
can coexist with product code.

Do not stage:

- `.env`, credential files, private keys, tokens, or environment dumps.
- `state/`, `core/agents/state/`, logs, SQLite/DuckDB databases, or caches.
- `workspaces/**/interns/` or generated workspace reports/state.
- Raw workspace datasets or source artifacts such as CSV, parquet, PDF, and XLSX files.
- `config/lock.toml` unless the user explicitly requests it.
- Scratch probes such as root-level one-off KPI output scripts.

Current hygiene audit notes:

- `get_kpi_001_output.py` and `get_kpi_001_output_v2.py` are scratch probes and should stay untracked.
- `config/lock.toml` is human-owned and should not be staged by default.
- Existing tracked workspace raw/source files need a separate explicit cleanup decision before untracking.
- A deleted workspace doc should be reviewed before any commit includes that deletion.
