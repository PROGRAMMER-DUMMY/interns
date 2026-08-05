# Finish Cloud-First Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the built cloud-first spine (intake/blueprint/provisioning/hardening/playbook, green at 1691 tests) through the real-Databricks rcm acceptance replay and flip the repo's defaults to cloud-first.

**Architecture:** Four phases in strict order: (0) AWS/UC storage-credential prerequisite (human-side), (A) replay-blocking hardening — readiness must diagnose the dual-profile auth split, generated code must actually ship (`databricks sync`), generation must gate on `dbt parse`; (B) the rcm replay itself — the broken Antigravity transcript rerun through the new spine with human gates; (C) post-replay hardening from the CLI research; (D) the flip — AGENTS.md/CLAUDE.md inversion and legacy retirement.

**Tech Stack:** Python 3.11 (`.venv`), unittest (NEVER `uv run` for tests), dbt-core 1.11 + dbt-databricks, Databricks CLI v1.7 (Go), Databricks Python SDK, Airflow 3 / astronomer-cosmos / Astro CLI, Unity Catalog.

## Global Constraints

- Run tests with `.venv\Scripts\python.exe -m unittest <module>` — never `uv run` (resyncs pyspark 4.1.1, breaks Delta tests).
- ASCII markers `[ok]/[~]/[x]` only, no emojis, in any generated/panel/report text.
- Workspace-agnostic: no hardcoded workspace/domain names in `core/` (`rcm` may appear ONLY in Phase B operational commands, never in code).
- Secrets: never print tokens/hosts/profile values; credential *names* only. `--confirmed-by` on every human gate; agents never set `AUTORESEARCH_ALLOW_REMOTE_EXECUTION`.
- Additive-only remote mutations; destructive ops stay gated (spec Section 10).
- Green gate (`.venv\Scripts\python.exe -m core.dev.green_gate`) must stay at 0 failures after every coding task.
- Spec of record: `docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md`. CLI findings of record: `docs/reference/{dbt,databricks,airflow}_cli_reference.md`.
- Commits: stage only intended files; commit at each task's commit step once its tests pass. Branch: `fix/close-built-to-wired-gap` (or a new `feat/cloud-first-finish` if the operator prefers — ask once at execution start).

---

## Phase 0 — S3 access prerequisite (HUMAN + assistant; no repo code)

### Task 0.1: Resolve the dual-profile auth split on this machine

**Files:** none (operator machine state). Reference: `docs/reference/databricks_cli_reference.md` section "Auth mechanics".

- [ ] **Step 1: Enumerate profiles (redacted).** Run: `databricks auth profiles`. Expected: 2 profiles, one invalid, both resolving to the same host (counts only — do not paste values into chat).
- [ ] **Step 2 (HUMAN): Pick one.** Either delete/rename the stale profile block in `~/.databrickscfg`, or pin the good one for this repo: `setx DATABRICKS_CONFIG_PROFILE <profile-name>` (new shells) and set it in the current shell.
- [ ] **Step 3: Verify both seams agree.** Run: `databricks current-user me` (expect your user summary) AND `databricks bundle validate` in any temp dir with a minimal bundle — or simply re-run Step 1 and confirm exactly one profile matches the host. Expected: no "multiple profiles matched" from any command.

### Task 0.2 (HUMAN + assistant): Create the UC storage credential for `amzn-workspace-rcm`

**Files:** none (AWS + Databricks account state). The assistant generates JSON/commands; the human runs the AWS-side steps.

- [ ] **Step 1: Create the credential shell to obtain the External ID.** Run:

```powershell
databricks storage-credentials create --json '{
  "name": "rcm_s3_credential",
  "aws_iam_role": {"role_arn": "arn:aws:iam::<YOUR-AWS-ACCOUNT-ID>:role/databricks-uc-rcm-access"},
  "comment": "UC access to amzn-workspace-rcm for the rcm workspace",
  "skip_validation": true
}'
```

Note the returned `aws_iam_role.external_id` (this is the Databricks-generated External ID; safe to use in the trust policy, do not paste other fields into chat).

- [ ] **Step 2 (HUMAN, AWS console/CLI): Create the IAM role.** Trust policy (replace `<EXTERNAL-ID>` from Step 1):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::414351767826:root"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "<EXTERNAL-ID>"}}
  }]
}
```

(414351767826 is Databricks' AWS account for UC credential vending — verify it matches the value shown in your workspace's storage-credential UI before trusting this plan's copy.) Attached permissions policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
     "Resource": ["arn:aws:s3:::amzn-workspace-rcm", "arn:aws:s3:::amzn-workspace-rcm/*"]},
    {"Effect": "Allow",
     "Action": ["sts:AssumeRole"],
     "Resource": "arn:aws:iam::<YOUR-AWS-ACCOUNT-ID>:role/databricks-uc-rcm-access"}
  ]
}
```

(The self-assume statement is required by UC role validation. Read-only S3 actions only — the platform lands data INTO UC managed tables, it never writes back to the customer bucket.)

- [ ] **Step 3: Validate.** Run: `databricks storage-credentials validate --name rcm_s3_credential --url s3://amzn-workspace-rcm/`. Expected: all checks PASS (read/list). If AWS IAM propagation lags, retry after ~60s.
- [ ] **Step 4: Record.** The credential NAME (`rcm_s3_credential`) is the `credential_ref` used in Phase B's `declare-source`. Nothing else to store.

---

## Phase A — Replay-blocking hardening (coding; one subagent per task)

### Task A1: Readiness diagnoses dual profiles, warehouse state, and names the credential source

**Files:**
- Modify: `core/platform_readiness.py` (`_check_databricks` at :26, `ReadinessReport` at :125)
- Test: `tests/test_platform_readiness.py` (exists)

**Interfaces:**
- Produces: `_check_databricks(cfg)` return dict gains keys `auth_source: str` (one of `env:DATABRICKS_HOST`, `env:DATABRICKS_CONFIG_PROFILE:<name>`, `profile:<name>`, `none`), `profile_conflicts: list[str]` (host-redacted profile-name lists that share one host), `warehouse_state: str | None` (`RUNNING|STOPPED|STARTING|unknown`). Status stays `ready` when authenticated but `notes` MUST include `[~] N profiles resolve to the same host; 'databricks bundle' commands will fail with "multiple profiles matched" until one is removed or DATABRICKS_CONFIG_PROFILE pins one` when N>1.

- [ ] **Step 1: Write the failing tests** in `tests/test_platform_readiness.py`:

```python
class DualProfileDiagnosisTests(unittest.TestCase):
    def _cfg(self, text: str) -> str:
        d = tempfile.mkdtemp()
        p = os.path.join(d, ".databrickscfg")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_two_profiles_same_host_is_flagged_but_not_blocking(self):
        cfg_path = self._cfg(
            "[one]\nhost = https://dbc-x.cloud.databricks.com\ntoken = redacted\n\n"
            "[two]\nhost = https://dbc-x.cloud.databricks.com\ntoken = redacted\n"
        )
        conflicts = platform_readiness.find_profile_conflicts(cfg_path)
        self.assertEqual(conflicts, [["one", "two"]])

    def test_conflict_note_never_contains_the_host(self):
        cfg_path = self._cfg(
            "[a]\nhost = https://secret-host.cloud.databricks.com\ntoken = redacted\n\n"
            "[b]\nhost = https://secret-host.cloud.databricks.com\ntoken = redacted\n"
        )
        note = platform_readiness.profile_conflict_note(
            platform_readiness.find_profile_conflicts(cfg_path)
        )
        self.assertNotIn("secret-host", note)
        self.assertIn("multiple profiles matched", note)

    def test_auth_source_prefers_env_host(self):
        with mock.patch.dict(os.environ, {"DATABRICKS_HOST": "x", "DATABRICKS_TOKEN": "y"}):
            self.assertEqual(platform_readiness.detect_auth_source(), "env:DATABRICKS_HOST")
```

- [ ] **Step 2: Run to verify failure.** `.venv\Scripts\python.exe -m unittest tests.test_platform_readiness` → FAIL (`find_profile_conflicts` undefined).
- [ ] **Step 3: Implement** in `core/platform_readiness.py`: module-level `find_profile_conflicts(cfg_path: str | None = None) -> list[list[str]]` (configparser over `~/.databrickscfg`, group section names by normalized `host`, return groups with len>1, sorted); `profile_conflict_note(conflicts) -> str` (names only, fixed wording above); `detect_auth_source() -> str` (precedence: `DATABRICKS_HOST` env -> `DATABRICKS_CONFIG_PROFILE` env -> single cfg profile -> `none`). Wire all three into `_check_databricks`'s payload + notes; add `warehouse_state` by listing warehouses via the existing SDK client when reachable (first warehouse's `state.value`, else `None` — a STOPPED warehouse adds note `[~] warehouse STOPPED: first query pays a cold start` and stays `ready`).
- [ ] **Step 4: Run tests.** Same command → PASS. Also run `.venv\Scripts\python.exe -m ruff check core/platform_readiness.py`.
- [ ] **Step 5: Commit.** `git add core/platform_readiness.py tests/test_platform_readiness.py && git commit -m "feat(readiness): diagnose dual-profile auth splits, warehouse state, auth source"`

### Task A2: `sync-workspace-code` — ship generated `ingestion/` and `dbt/` to the Databricks workspace

**Files:**
- Create: `core/provisioning/sync_code.py`
- Modify: `core/provisioning/cli.py` (add `sync_workspace_code_main`), `pyproject.toml` (`sync-workspace-code = "core.provisioning.cli:sync_workspace_code_main"`)
- Test: `tests/test_sync_workspace_code.py`

**Interfaces:**
- Consumes: confirmed blueprint marker `interns/reports/solution_blueprint/current.confirmed.json` (same refusal contract as `apply.py`); generated dirs `workspaces/<ws>/ingestion/` and `workspaces/<ws>/dbt/`.
- Produces: `build_sync_commands(workspace_root: Path, remote_root: str) -> list[list[str]]` returning `databricks sync <local> <remote> --full` argv lists (one per existing dir); `sync_workspace_code(repo_root, workspace, *, dry_run: bool, runner=subprocess.run) -> dict` with `{"ok": bool, "synced": [...], "skipped": [...], "remote_root": str}`; evidence log at `interns/generated/evidence/provisioning/sync_log.json`. Remote root default: `/Workspace/Shared/<workspace-basename>/` overridable with `--remote-root`.

- [ ] **Step 1: Write the failing tests** in `tests/test_sync_workspace_code.py`:

```python
class SyncCommandTests(unittest.TestCase):
    def test_uses_databricks_sync_never_import_dir(self):
        cmds = sync_code.build_sync_commands(Path("ws"), "/Workspace/Shared/ws")
        for argv in cmds:
            self.assertEqual(argv[0:2], ["databricks", "sync"])
            self.assertNotIn("import-dir", " ".join(argv))  # strips .py/.sql extensions

    def test_refuses_without_confirmed_blueprint(self):
        ws = _make_ws(confirmed=False)  # helper builds tmp workspace with ingestion/
        result = sync_code.sync_workspace_code(ws.parent.parent, str(ws), dry_run=False,
                                               runner=_recording_runner())
        self.assertFalse(result["ok"])
        self.assertIn("confirmed", result["reason"])

    def test_syncs_only_existing_dirs_and_logs(self):
        ws = _make_ws(confirmed=True, with_dbt=False)  # ingestion/ only
        rec = _recording_runner()
        result = sync_code.sync_workspace_code(ws.parent.parent, str(ws), dry_run=False, runner=rec)
        self.assertTrue(result["ok"])
        self.assertEqual(len(rec.calls), 1)
        self.assertIn("ingestion", " ".join(rec.calls[0]))
        self.assertTrue((ws / "interns/generated/evidence/provisioning/sync_log.json").exists())
```

(`_recording_runner` returns an object whose `__call__(argv, ...)` records argv and returns `returncode=0`; `_make_ws` writes `ingestion/a.py` and, when `confirmed=True`, a minimal `current.confirmed.json`.)

- [ ] **Step 2: Run to verify failure.** `.venv\Scripts\python.exe -m unittest tests.test_sync_workspace_code` → FAIL (module missing).
- [ ] **Step 3: Implement** `core/provisioning/sync_code.py` per the Produces block: check confirmation exactly like `apply.py` does; injectable `runner` (default `subprocess.run`) so tests never touch the network; never echo profile/host values; write `sync_log.json` with per-dir outcome. Add `sync_workspace_code_main` to `core/provisioning/cli.py` with `@anchored("sync-workspace-code")`, `run_workspace_command` envelope, `--dry-run` defaulting ON without confirmation (mirroring `apply_provisioning_main`). Register in `pyproject.toml`.
- [ ] **Step 4: Run tests + gates.** Target suite PASS; then `.venv\Scripts\python.exe -m unittest tests.test_cost_ledger tests.regressions.test_tool_index_coverage` (will fail until you run `.venv\Scripts\python.exe -m core.dev.tool_index` to refresh the index — do that, rerun, PASS).
- [ ] **Step 5: Commit.** `git add core/provisioning/sync_code.py core/provisioning/cli.py pyproject.toml .agents/tools.json tests/test_sync_workspace_code.py && git commit -m "feat(provisioning): sync-workspace-code ships generated ingestion/ and dbt/ via databricks sync"`

### Task A3: Generation gates on `dbt parse` + empty-selection guard + flags block

**Files:**
- Modify: `core/onboarding/kpi/dbt_project_generator.py` (generator emit + `validate_generated_project`), `core/orchestration/dbt_verify.py` (`main`)
- Test: `tests/test_dbt_generator_hardening.py` (extend)

**Interfaces:**
- Produces: `run_dbt_parse(project_dir: Path, dbt_exe: str | None = None, runner=subprocess.run) -> tuple[bool, str]` in `dbt_project_generator.py` (returns ok + tail of output; `dbt_exe` default `.venv/Scripts/dbt.exe` if present else `"dbt"`); emitted `dbt_project.yml` gains a top-level `flags:` block containing `warn_error_options: {error: ["NoNodesForSelectionCriteria"]}` (a no-match selection must fail CI, not exit 0); `verify-dbt-project` calls `run_dbt_parse` and fails on parse failure.

- [ ] **Step 1: Write the failing tests** (extend `tests/test_dbt_generator_hardening.py`):

```python
class ParseGateTests(unittest.TestCase):
    def test_emitted_project_yml_promotes_empty_selection_to_error(self):
        text = _generate_min_project(self).joinpath("dbt_project.yml").read_text()
        self.assertIn("NoNodesForSelectionCriteria", text)

    def test_run_dbt_parse_reports_failure_output(self):
        calls = []
        def fake_run(argv, **kw):
            calls.append(argv)
            return types.SimpleNamespace(returncode=2, stdout="", stderr="Compilation Error in model x")
        ok, tail = dbt_project_generator.run_dbt_parse(Path("proj"), dbt_exe="dbt", runner=fake_run)
        self.assertFalse(ok)
        self.assertIn("Compilation Error", tail)
        self.assertEqual(calls[0][:2], ["dbt", "parse"])
```

(`_generate_min_project` is the existing helper pattern in this test file that generates a minimal project into `tempfile.mkdtemp()` — reuse it.)

- [ ] **Step 2: Run to verify failure.** `.venv\Scripts\python.exe -m unittest tests.test_dbt_generator_hardening` → FAIL.
- [ ] **Step 3: Implement.** Add the `flags:` block to the emitted `dbt_project.yml` template; implement `run_dbt_parse` (argv `[dbt_exe, "parse", "--project-dir", str(project_dir), "--profiles-dir", str(project_dir)]`, ok = returncode==0, tail = last 2000 chars of stdout+stderr); call it at the end of project generation (failure -> raise with the tail, files stay on disk for inspection, matching `validate_generated_project` behavior) and from `core/orchestration/dbt_verify.py:main` before its existing checks.
- [ ] **Step 4: Run tests.** Target suite + `tests.test_dbt_project_generator` + `tests.test_dbt_verify` → PASS. `ruff check` touched files.
- [ ] **Step 5: Commit.** `git commit -m "feat(dbt): gate generation and verify on dbt parse; empty selection is an error" -- core/onboarding/kpi/dbt_project_generator.py core/orchestration/dbt_verify.py tests/test_dbt_generator_hardening.py`

---

## Phase B — The rcm acceptance replay (operational; assistant drives, HUMAN at gates)

The acceptance test IS the broken Antigravity transcript, rerun through the new spine. Run each command; paste nothing secret; stop at each HUMAN gate. Workspace: `workspaces/rcm`. Any refusal/blocked panel here is a FINDING to fix in the platform, not something to work around by hand — that is the point of the replay.

### Task B1: Declare + discover + intake

- [ ] `uv run declare-source --workspace workspaces/rcm --type s3 --location s3://amzn-workspace-rcm --credential-ref rcm_s3_credential --declared-by "<your-name>"`
- [ ] `uv run discover-source --workspace workspaces/rcm` — expected: `status: ok`, real table list with measured sizes (boto3 present; if it reports `credential_or_tool_missing`, `pip install boto3` into `.venv` and rerun). `working_set_estimate_bytes` non-null.
- [ ] `uv run prepare-intake-panel --workspace workspaces/rcm` then read `workspaces/rcm/interns/reports/intake_panel/current.md` and ask the HUMAN each pending question verbatim from the panel; apply each with `uv run apply-intake-answer --workspace workspaces/rcm --question <id> --answer <option> --answered-by "<your-name>"`.

### Task B2: Blueprint + HUMAN confirmation

- [ ] `uv run prepare-blueprint --workspace workspaces/rcm` — expected: 0 blocked decisions (a block names a missing fact; resolve it via discovery/intake, never by editing artifacts).
- [ ] Render `interns/reports/solution_blueprint/current.md` to the HUMAN verbatim (mermaid graph, provisioning table, engine/compute options, "everything additive" line).
- [ ] **HUMAN GATE:** `uv run confirm-blueprint --workspace workspaces/rcm --confirmed-by "<your-name>"` — only after the human says yes.

### Task B3: Provision + land + build

- [ ] `uv run plan-provisioning --workspace workspaces/rcm --catalog rcm --env dev` then show the plan; expected: create catalog `rcm_dev`, schemas, external location on `s3://amzn-workspace-rcm` with `rcm_s3_credential`, `_checkpoints` volume; `blocked_count: 0`.
- [ ] HUMAN sets `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` in the executing shell (agents never set it), then: `uv run apply-provisioning --workspace workspaces/rcm` — expected: every step `[ok] created` or `[ok] existing`; verify in UC: `databricks schemas list rcm_dev`.
- [ ] `uv run generate-ingestion --workspace workspaces/rcm` then `uv run sync-workspace-code --workspace workspaces/rcm` (Task A2's command) — expected: ingestion files + dbt project in `/Workspace/Shared/rcm/`.
- [ ] Run the landing (per `ingestion/jobs_manifest.json`: COPY INTO via warehouse for batch files, Auto Loader job for streaming) and then `uv run generate-dbt-project --workspace workspaces/rcm` + `dbt build` against `rcm_dev` (exact commands come from the manifest and the generated project README — follow them, don't improvise).
- [ ] Airflow leg: `astro dev start` in the generated project, `airflow dags list-import-errors` clean, `airflow dags test <rcm-dag-id> <today>` green, `airflow tasks render` shows substituted backfill params.

### Task B4: Acceptance verdict

- [ ] All true: S3 declared source accepted end-to-end; catalog `rcm_dev` exists with bronze/silver/gold; row counts in bronze match discovery estimates within reason; dbt build green; KPI results packet rendered from real data; dashboard reads the dbt marts; zero hand-edited artifacts; every gate crossed with human provenance.
- [ ] Write findings (every friction point, refusal, or manual step) to `docs/plans/rcm_replay_findings.md` — these prioritize Phase C. **HUMAN reviews findings before Phase D.**

---

## Phase C — Post-replay hardening (coding; scope may be re-ranked by B4 findings)

### Task C1: Publish dbt state after runs (unlocks slim CI, `retry`, `clone`)

**Files:**
- Create: `core/orchestration/dbt_state.py`
- Modify: `core/orchestration/cosmos_dag.py` (publish task), `core/orchestration/airflow_dag.py` (wire after `dbt_build`), `pyproject.toml` (`publish-dbt-state = "core.orchestration.dbt_state:main"`)
- Test: `tests/test_dbt_state_publish.py`

**Interfaces:**
- Produces: `publish_state(project_dir: Path, workspace: str, *, runner=subprocess.run) -> dict` — copies `target/manifest.json` + `target/run_results.json` to `/Volumes/<catalog>/_state/dbt/<workspace>/<utc-timestamp>/` AND `.../latest/` via `databricks fs cp`; `state_download_command(workspace) -> list[str]` for CI (`--state` input). DAG task `publish_dbt_state` emitted downstream of `publish_gold`.

- [ ] **Step 1: Failing tests** — mirror Task A2's recording-runner pattern: assert two `databricks fs cp` calls per artifact (timestamped + latest), assert no call when `target/` missing (returns `{"ok": False, "reason": "no target/ artifacts"}`), assert DAG wiring includes `publish_dbt_state` after `publish_gold` (extend `tests/test_orchestration_hardening.py`'s existing tail-map test style).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Target + `tests.test_orchestration_hardening` PASS; refresh tool index; cost-ledger/tool-index suites PASS. **Step 5:** Commit `feat(dbt): publish manifest/run_results state to a UC volume after runs`.

### Task C2: Airflow operability — backfill pool + `is_paused` health

**Files:**
- Create: `core/orchestration/airflow_health.py`
- Modify: `core/orchestration/airflow_dag.py` (backfill path gets `pool="backfill"`; emit `setup_pools` bootstrap snippet `airflow pools set backfill 2 "bounded replay capacity"` in the generated DAG file header comment), `pyproject.toml` (`check-airflow-health = "core.orchestration.airflow_health:main"`)
- Test: `tests/test_airflow_health.py` + extend `tests/test_orchestration_hardening.py`

**Interfaces:**
- Produces: `check_airflow_health(base_url: str, token: str, dag_ids: list[str], http=urllib.request.urlopen) -> dict` hitting Airflow 3 REST (`/api/v2/dags/{id}` for `is_paused`, `/api/v2/monitor/health` for scheduler heartbeat) with injectable `http`; result `{"ok": bool, "paused_dags": [...], "scheduler": "healthy|unhealthy|unreachable"}`. A paused generated DAG => `ok: False` (a paused pipeline is silently dead — spec Section 8 operability).

- [ ] **Step 1: Failing tests** with a fake `http` returning canned JSON (paused dag -> `ok False`; healthy -> `ok True`; connection error -> `scheduler: unreachable`, `ok False`). DAG test: backfill task carries `pool="backfill"`.
- [ ] **Steps 2-4:** FAIL → implement → PASS + tool index refresh. **Step 5:** Commit `feat(airflow): backfill pool + is_paused/scheduler health check`.

### Task C3: Ghost-table reconcile uses `manifest.json` `relation_name`

**Files:**
- Modify: `core/onboarding/kpi/dbt_project_generator.py` (`reconcile_ghost_tables`)
- Test: `tests/test_dbt_generator_hardening.py` (extend)

- [ ] **Step 1: Failing test:** feed a manifest fixture whose node has `relation_name: "`cat`.`gold`.`fct_x`"` and a warehouse listing `["cat.gold.fct_x", "cat.gold.fct_orphan"]`; assert the report names exactly `cat.gold.fct_orphan` (fully qualified), contains no `DROP`, and bare-alias collisions across schemas are NOT reported as matches.
- [ ] **Steps 2-4:** FAIL → change the diff key from alias to normalized `relation_name` (strip backticks, casefold) with fallback to the current alias diff when `relation_name` absent (pre-1.0 manifests) → PASS. **Step 5:** Commit `fix(dbt): reconcile ghosts on fully-qualified relation_name, not bare alias`.

### Task C4: Generated dbt project polish — env_var credentials, version pin, catalog-per-env targets

**Files:**
- Modify: `core/onboarding/kpi/dbt_project_generator.py` (profiles + project emit)
- Test: `tests/test_dbt_generator_hardening.py` (extend)

- [ ] **Step 1: Failing tests:** emitted `profiles.yml` contains `env_var('DBT_DATABRICKS_TOKEN'` and NO literal token/host values; contains two targets `dev` and `prod` whose `catalog:` values are literals `<base>_dev` / `<base>_prod` (Jinja `{{ target.name }}` does not work in profiles.yml — regression-pin that no `{{ target` appears); emitted `dbt_project.yml` contains `require-dbt-version: [">=1.11.0", "<2.0.0"]`.
- [ ] **Steps 2-4:** FAIL → implement → PASS (also rerun `tests.test_dbt_project_generator`). **Step 5:** Commit `feat(dbt): env_var credentials, dbt version pin, catalog-per-env targets`.
- [ ] **Step 6: Spelling purge.** `Grep "dags backfill" docs/ core/ skills/` → replace any hit with `backfill create` semantics (per `docs/reference/airflow_cli_reference.md`). Commit if any hits.

---

## Phase D — The flip (only after B4 verdict is accepted by the HUMAN)

### Task D1: Retire the legacy blueprint producer via deprecation redirect

**Files:**
- Modify: `core/onboarding/blueprint.py` (`prepare_main` becomes a redirect), `pyproject.toml` (no entry change — same command name, new behavior)
- Test: `tests/test_cli_deprecation_redirect.py` (extend — this suite already pins the `resolve-kpi-features` precedent; follow its pattern exactly)

- [ ] **Step 1: Failing test:** invoking `prepare-solution-blueprint`'s `prepare_main` prints a `[~] deprecated: use prepare-blueprint` note and delegates to `core.blueprint.cli.prepare_blueprint_main`, returning its exit code.
- [ ] **Steps 2-4:** FAIL → implement redirect (keep `apply-blueprint-answer` functional for its OTHER panels if it serves any; if it only served the legacy blueprint, redirect it to `confirm-blueprint` the same way) → PASS. Remove the `current.legacy.*` copy logic from `core/blueprint/renderer.py` (no longer reachable) and its test. **Step 5:** Commit `refactor(blueprint): legacy prepare-solution-blueprint redirects to prepare-blueprint`.

### Task D2: Invert AGENTS.md + CLAUDE.md to cloud-first

**Files:**
- Modify: `AGENTS.md` (sections "Local-Native vs. Cloud-Native", "Step 0", "Platform readiness check", the mode table), `CLAUDE.md` (mirror), `.agents/claude/SKILLS.md` regenerated via `uv run generate-skill-adapters`

- [ ] **Step 1: Rewrite AGENTS.md's mode section:** cloud-first is the spine — Step 0 for a workspace that names a source is `declare-source -> discover-source -> prepare-intake-panel -> prepare-blueprint -> confirm-blueprint` (one confirmation); `--local`/`local_files` documented as the dev/POC mode with the OLD ceremony text moved under it; safety model text updated to "additive ops free once blueprint-confirmed; destructive gated; AUTORESEARCH_ALLOW_REMOTE_EXECUTION is the kill-switch whose setting remains human-only". Update the Stage index rows (Workspace selection + Cloud-native rows gain the five new commands; Solution blueprint row points at `prepare-blueprint`).
- [ ] **Step 2:** Mirror the same inversion in `CLAUDE.md`'s startup instructions (selection-only checklist keeps its mutation hard-stop verbatim).
- [ ] **Step 3:** `uv run generate-skill-adapters`; `.venv\Scripts\python.exe -m unittest tests.test_agent_skill_routing` → PASS (update `delegation.STAGE_ROUTING` if the coverage test names the new commands as unrouted).
- [ ] **Step 4: Commit** `docs(flip): AGENTS.md/CLAUDE.md invert to cloud-first; local becomes dev mode`.

### Task D3: Final gate + wrap

- [ ] `.venv\Scripts\python.exe -m core.dev.green_gate --sweep` → 0 NEW failures.
- [ ] Refresh tool index + `tests.regressions.test_tool_index_coverage` PASS.
- [ ] Update memory file `project_cloud_first_restructure.md` (flip DONE, replay verdict, findings doc path).
- [ ] **HUMAN GATE:** review `git status --short` + `git diff --cached --stat`; commit any remainder; decide push/PR.

---

## Self-review notes

- Spec coverage: Phase 0/B cover spec Section 12 (acceptance); A1-A3 + C1-C4 cover the seven CLI-research gaps (readiness=4, sync=2, parse+selection=1, state=3, pool/is_paused=5, reconcile=6, profiles/env=7); D covers spec Sections 9 (legacy retirement), 11 (flip). Engine-machinery retirement of N-way parity (spec Section 9) is deliberately NOT in this plan — it is a deletion with wide blast radius and should be its own plan after the flip proves the routed model in production.
- The Databricks AWS root-account id in Task 0.2 must be verified against the workspace UI before use (noted inline).
- Phase B command flags assume Phase A lands first (`sync-workspace-code` exists, readiness diagnoses profiles). Executing B before A will fail at B3.
