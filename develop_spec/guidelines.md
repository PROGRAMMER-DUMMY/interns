# develop_spec/guidelines.md — how we write platform code

Development-mode rules for changing `core/`, `tools/`, `tests/`. These are the
load-bearing conventions; the operating rules in `AGENTS.md` and the token
discipline in `CLAUDE.md` still apply on top.

## 1. Generic, never workspace-specific

The platform must work for ANY workspace. Do not bake domain vocabulary
(healthcare/RCM/hospital/encounter/patient/payer/claim, or any single
workspace's column names) into logic. Derive behavior from the workspace's own
evidence (profiles, KPI rows, data dictionary, accepted decisions).

- Enforced by `tests/test_metric_derivation.py::GenericityGuardTest` — it greps
  the module source for forbidden domain terms. Keep examples in comments
  generic (orders/customers/shipments), not domain terms.
- Before marking work done, grep your diff for hardcoded domain words.
- Principle: **derive, don't curate.** A curated JSON list is no better than a
  curated Python list — prefer evidence-driven derivation.

## 2. Don't hand-edit generated artifacts

`interns/generated/**` (contracts like `kpi_registry.json`,
`kpi_feature_mapping.json`, panels, profiles) are machine outputs. Never edit
them by hand to make something pass — fix the generator and re-run. Hand-editing
contracts caused fabricated results in the past (BUG-014/015 residue).

## 3. No emojis — ASCII status markers only

Use `[ok]` / `[~]` / `[x]` / `[blocked]` in panels, reports, generated text, and
docs. No emojis anywhere in output.

## 4. Respect the human gates

Human-answered gates (relationship join approval, kpi-analyst review) must record
provenance: pass `--confirmed-by "<name>"` so a human "yes" is `source: human`,
not `source: agent`. Don't clear a human gate while recording it as agent.

## 5. Staging hygiene (from docs/repo_hygiene.md)

Stage with explicit paths only — never `git add -A`. Do NOT stage: `.env`,
secrets, `state/`, `core/agents/state/`, `workspaces/**/interns/`, raw datasets
(csv/parquet/pdf/xlsx), `config/lock.toml` (unless asked), scratch probe scripts.

## 6. Token discipline (see CLAUDE.md)

- Prefer the deterministic wrappers over issuing each step as a separate call.
- Never read large machine JSON whole (`**/session.json`, `*trajectory*.json`,
  workflow `current.json`, `kpi_feature_mapping.json`) — read the paired `.md`.
- Pass `--quiet` on workspace-flow subcommands.

## 7. Log every change

After any change: append a dated entry to `develop_spec/changelog.md` (what /
why / files / tests / verification) and update `develop_spec/follow_ups.md`.

## Definition of done (a change is done when)

- [ ] generic (genericity guard + a grep of the diff pass)
- [ ] tests added/extended and green on the venv interpreter
- [ ] `green-gate` clean
- [ ] no generated contract hand-edited
- [ ] `develop_spec/changelog.md` + `follow_ups.md` updated
- [ ] meets `develop_spec/path_to_production.md`
