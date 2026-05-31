# Bug Logs — Index

Internal bug logs for the platform. Each is an append-only record: finding -> root cause ->
fix (with files + tests). Newest session reports carry per-bug Status (Open / Fixed / Won't-fix).

| File | What it covers |
|------|----------------|
| `BUG_SESSION_REPORT.md` | Current session: 10 bugs from the RCM local-test (feature dedup, denominators, join-key uniqueness, RI gate, age basis, quiet flags, runs/ snapshot, guard checks, gemini config, data-understanding gate). All Fixed. |
| `workspace_onboarding_bugs.md` | Onboarding ignoring root-level workspace inputs (empty discovered inputs despite valid files). |
| `data_engineering_control_plane_hardening.md` | Data-engineering control-plane hardening findings. |
| `gemini_workspace_flow_monitoring_bug.md` | Gemini workspace-flow session-monitoring bug. |

When adding a new bug log: drop the file here, add one row above. Use a dated
`BUG_SESSION_REPORT.md`-style file for a test session, or a topic-named file for a focused defect.
