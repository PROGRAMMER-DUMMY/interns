# rcm Replay Findings (live Databricks)

Replay of the failed 2026-08-04 session against the real account, driving the new
cloud-first spine. Every entry below is observed output, not inference.

Account shape (measured 2026-08-06):
- Databricks Free Edition, AWS-hosted. Metastore `b8a65a08-...`, default catalog `workspace`.
- Compute: ONE serverless PRO SQL warehouse (`Serverless Starter Warehouse`), STOPPED at rest.
- Catalogs already present: `workspace`, `system`, `samples`, `dbt_dev`, `rcm`.
- Auth: two profiles for the SAME host -- `DEFAULT` (valid) and `dbc-a2362023-5116` (INVALID).

Real source estate at `s3://amzn-workspace-rcm/` (listed through Unity Catalog):
```
datasets/EMR/trendytech-hospital-a/  Readme, departments.csv, encounters.csv,
                                     patients.csv, providers.csv, transactions.csv
datasets/EMR/trendytech-hospital-b/  (same five tables)
datasets/claims/                     hospital1_claim_data.csv, hospital2_claim_data.csv
datasets/cptcodes/                   cptcodes.csv
docs/                                DataModel.png, Sample_KPI (1).xlsx
```
~10 MB across 13 files. Two hospitals, EMR + claims + CPT reference, plus a data-model
image and a KPI workbook.

---

## F1 [BLOCKER, fixing] Discovery demanded local AWS credentials the platform did not need

`core/intake/discovery.py::scan_s3` requires `boto3` + resolvable AWS credentials on the
operator's machine. On this machine boto3 is absent, so `discover-source` would return
`credential_or_tool_missing` and the pipeline could not start.

But the bucket is ALREADY a Unity Catalog external location (`healthcare_rcm_ext_loc` ->
`healthcare_rcm_cred`), so Databricks holds the credential. Verified working with no local
AWS credentials at all:

```
databricks storage-credentials validate --storage-credential-name healthcare_rcm_cred \
  --external-location-name healthcare_rcm_ext_loc
  -> READ PASS, LIST PASS, WRITE PASS, DELETE PASS, PATH_EXISTS PASS

SQL: LIST 's3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-a/'
  -> rows of [full_path, name, size_bytes, modification_time_ms]
```

Requiring a second credential set on the laptop is wrong for a cloud-first platform.
FIX IN FLIGHT: a UC-based scanner that lists through the warehouse when the location is a
registered external location, falling back to boto3, then to an honest refusal naming both
options. Same fix closes ADLS and GCS discovery (SQL `LIST` is URI-scheme agnostic), which
were previously `unsupported_yet` while their ingestion codegen already existed.

## F2 [CONSTRAINT, accept + record] File events unavailable on this bucket

The same validation reports one FAIL:

```
Provisioning has failed. Failed to provision file events resources during
s3.getBucketNotificationConfiguration ... not authorized to perform:
s3:GetBucketNotification (Status Code: 403)
```

READ/LIST/WRITE/DELETE all pass, so ingestion works -- but Auto Loader must run in
DIRECTORY LISTING mode, not file-notification mode. The generated Auto Loader jobs must not
set `cloudFiles.useNotifications=true` for this workspace, and the blueprint should say so
rather than letting a run discover it. At ~13 files this costs nothing; at high file counts
it would.

## F3 [ENVIRONMENT] Duplicate same-host auth profiles

`databricks auth profiles` shows `DEFAULT` (valid) and `dbc-a2362023-5116` (INVALID) for the
same host. Confirms the CLI reference's warning: every `bundle` command fails with
"multiple profiles matched" while SDK calls succeed. Phase A1 (readiness diagnosis) should
report this precisely; the operator fix is to delete the invalid profile block.

## F4 [ENVIRONMENT] Free Edition capability check -- better than assumed

Storage-credential creation IS permitted (probed with a throwaway credential pointed at a
nonexistent role, then deleted). Serverless SQL executes. So the account can run the full
replay; the earlier worry that Free Edition would block external storage was wrong.
Cold start applies: the sole warehouse is STOPPED at rest and the first statement pays it.

## F5 [PLATFORM] Pre-existing artifacts from the failed session

Catalog `rcm` already exists (created during the 2026-08-04 session that crashed), as does
`dbt_dev`. The replay must be idempotent against this -- the additive provisioner should
report `[ok] existing` rather than failing, which is exactly what its existence checks are
for. This is an unplanned but useful test of the idempotency contract.

---

## Status

- Phase 0 (access): DONE -- credential and external location already existed and validate.
- Phase A: in progress; F1 promoted to the top of the queue because it blocks discovery.
- Phase B: blocked on F1.

## F6 [BLOCKER, fixed] Distinct entities collapsed into one table

`discover-source` returned FOUR tables for an estate that has twelve. The
directory grouping rule ("files under one folder are one table") is correct for a
part-file layout and catastrophically wrong for a folder holding one file per
entity: `departments/encounters/patients/providers/transactions` under one
hospital folder were merged into a single table named after the folder.

Merging unrelated schemas into one bronze table poisons everything downstream --
every grain, join and KPI would have been built on a table that never existed.

FIX: a group survives intact only when every member reduces to the same shard
template (digit runs collapsed). `part-00001`/`part-00002` and
`hospital1_claim_data`/`hospital2_claim_data` converge on one template and stay
grouped -- correctly, they ARE shards of one dataset. `patients`/`providers` do
not, so they split.

The fix then exposed a second layer: `patients` existed under two hospital
folders, so two tables shared a name and would have collided in bronze naming and
in the generated dbt models. Colliding names are now qualified by their parent
(`trendytech-hospital-a__patients`); unambiguous names stay short.

Result: 4 tables -> 12 uniquely-named tables, matching the real estate.

## F7 [DESIGN, in progress] The interview presumed a domain instead of deriving one

Driving the intake against a real estate showed the interview asks generic
questions and leaves the judgment to the agent -- so the agent starts inventing
domain assumptions (healthcare/claims restatement behaviour, PHI columns,
regulatory lineage). On a platform where "rcm is just one user out of others",
that is exactly backwards.

Owner's requirement: the platform already HAS the evidence to infer the domain
once it can see the data and the documentation, so it should derive the domain,
CONFIRM it with the user, and only then offer domain-accurate options -- and
every question should carry a recommended best-case option. Consumers stay a
blackbox until the user states them; lineage/reproducibility and who-to-notify
are grilling questions with options, never agent assumptions.

FIX IN FLIGHT: a `domain_confirm` question whose options are built from evidence
(discovered table/column names, document filenames), with the matched tokens
shown so the inference can be attacked; unmatched evidence yields "unknown" and
the full list rather than a guess. Domain vocabulary ships as DATA, never as
branches in code, and a genericity guard test keeps domain names out of
`core/intake` logic.

## F8-F11 [FIXED] Four defects found while driving the intake

- **F8 provenance bypass (governance).** `decision_source` normalised the WHOLE
  identity and did an exact set-membership test, so `agent (platform
  recommendation)` became `agentplatformrecommendation`, matched nothing, and was
  recorded as `source: human`. Any agent could bypass any human gate by appending
  one word to its name. Matching is now per WORD and fails closed.
- **F9 playback misattribution.** Platform-supplied answers were played back as
  "(you said)", so confirming the alignment gate meant agreeing to statements
  nobody made. Now tagged "(assumed by the platform -- correct it if wrong)".
- **F10 template answers.** A `suggested_answer` applied verbatim reached the
  playback as a real requirement (grain: "one row per <business entity>").
  Templates are refused at the door, and every shipped suggestion must contain a
  placeholder -- two did not, and were hidden defaults.
- **F11 bulk upload read as a stream.** Two files written 3s apart classified as
  `continuous`, routing `claims` to Auto Loader streaming instead of batch COPY
  INTO. A cadence now needs enough arrivals over a long enough span.

## F12 [BLOCKER, FIXED] Documents in the source were unreachable

The workspace's KPIs sat in `docs/Sample_KPI (1).xlsx` INSIDE the source bucket
for the whole run. Discovery listed the file; nothing could read it. So the KPI
registry stayed empty, `join_complexity` was unmeasurable, the engine decision
blocked, and `confirm-blueprint` refused a plan it could never complete.

The bytes were reachable the entire time through the credential Unity Catalog
already holds. `core/intake/documents.py` + `fetch-source-documents` now read
them via `read_files(..., format => 'binaryFile')` + `base64(...)`, bounded, with
a short read REFUSED rather than stored (half a workbook parses as a whole one).
Verified live: 372,953 B diagram and 12,191 B workbook recovered byte-exact, no
local cloud credential. Onboarding then registered **18 real KPIs** from the
workbook.

## F13 [BLOCKER, FIXED] Circular dependency deadlocked the blueprint

With KPIs registered the engine decision STILL blocked, on `join_complexity`:

```
engine <- join_complexity <- feature mapping <- feature resolution
       <- profiles <- data landed in UC <- ingestion <- apply-provisioning
       <- confirmed blueprint <- engine
```

A genuine deadlock. The cause was narrower than the cycle suggests: rules are
first-match, and evaluation halted at `ENG-R5 (join_complexity == heavy)` even
though `ENG-R6 (consumer_class_count >= 2)` and `ENG-R8 (< 50GB)` both fired on
fully-known facts and BOTH yielded `sql_dbt_warehouse`. The unknown could not
change the outcome, yet it blocked.

FIX: an unknown blocks only when it could change the answer. If every later rule
that fires on known facts agrees, the decision is taken and records why it did not
block. The relaxation is deliberately narrow -- any disagreement, a second
unknown, or no later rule firing still blocks -- and a rule may declare
`hard_block: true` for a CAPABILITY refusal (VEL-R1's missing online-store
serving edge), which is never fallen back past. Five tests pin all five cases.

---

## Replay outcome

Ran end to end to the remote-execution boundary:

| Step | Result |
|---|---|
| declare-source | s3://amzn-workspace-rcm/datasets/, credential by reference |
| discover-source | 12 tables, 11.3 MB, measured through Unity Catalog |
| fetch-source-documents | 2 documents recovered byte-exact |
| onboard-workspace | 18 KPIs registered from the workbook |
| intake | 18 answers (4 human, 14 platform defaults, honestly attributed) |
| playback | confirmed |
| prepare-blueprint | 10 decisions, 0 blocked |
| confirm-blueprint | confirmed, `source: human` |
| plan-provisioning | catalog `rcm_dev`, 6 additive steps, 0 blocked |
| generate-ingestion | 12 jobs; no `useNotifications` (F2 honored); no destructive statements |

STOPPED AT: `apply-provisioning`, which requires
`AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1`. A human must set that in the executing
shell; an agent must never set it. Nothing has been created in the Databricks
account by this replay.

---

# Phase B3 continued (2026-08-09) -- gate crossed

The human set `AUTORESEARCH_ALLOW_REMOTE_EXECUTION=1` and `apply-provisioning`
ran for real. Two blockers surfaced, in sequence; both are platform defects that
reproduce on any account of this shape, not rcm quirks.

## F14 [BLOCKER, FIXED] External-location identity: name vs path

First live `apply-provisioning` failed on step 1 of 6:

```
InvalidParameterValue: Input path url 's3://amzn-workspace-rcm/datasets'
overlaps with an existing external location within 'CreateExternalLocation'
call. Conflicting location: healthcare_rcm_ext_loc.
```

`created: 0, existing: 0, blocked: 0, failed: 1` -- the run stopped on the first
failure and created nothing, which is the P1-b stop-on-first-failure fix
behaving correctly.

Root cause: the platform models external-location identity as a NAME. Both the
plan-time guard (`plan.py` `existing.get(f"external_location:{loc_name}")`) and
the apply-time guard (`external_location_exists(name)`) ask "is there a location
called `rcm_dev_root`?" -- there was not, so it planned a create. Unity Catalog
models identity as a URL PREFIX and forbids two locations covering overlapping
paths in either direction. The existing `healthcare_rcm_ext_loc` covers
`s3://amzn-workspace-rcm/`, a strict parent of the planned
`s3://amzn-workspace-rcm/datasets/`.

This is the NORMAL enterprise case -- a platform team pre-registers the bucket,
then a workspace tries to register a sub-path of it. It is precisely the
scenario cloud-first targets.

The same defect existed in BOTH provisioning paths: `uc_intake.py` used the
identical `external_locations.get(name)` lookup, the identical `f"{catalog}_root"`
naming convention, and the identical name-based skip.

FIX: a pure `covering_external_location(url, locations)` in
`core/provisioning/apply.py`, plus `list_external_locations()` on both API
seams. A name miss now falls back to overlap detection; a covering location is
recorded `existing` and reused, because it IS the access path for that data and
no create could ever succeed. Overlap is bidirectional and compares on
path-segment boundaries, so `s3://bkt/pfx2/` does not match `s3://bkt/pfx/`.
Five tests pin it, including the exact reproduction.

Verified live:
```
[ok] existing external location healthcare_rcm_ext_loc already covers
     s3://amzn-workspace-rcm/datasets/; reusing it
```

## F15 [BLOCKER, FIXED] Rootless catalog create on a Default Storage metastore

With F14 fixed, provisioning advanced one step and failed on the catalog:

```
InvalidState: Metastore storage root URL does not exist. Default Storage is
enabled in your account. ... please provide a storage location for the catalog
(for example 'CREATE CATALOG myCatalog MANAGED LOCATION '<location-path>').
```

Measured: metastore `metastore_aws_us_east_2` has `storage_root: None`. Two
working precedents already existed in the same account -- `rcm` at
`s3://amzn-workspace-rcm/` (OPEN), and `dbt_dev`/`workspace` at
`s3://dbstorage-prod-.../uc/<metastore-id>` (ISOLATED, Databricks Default
Storage). So a catalog here MUST carry an explicit MANAGED LOCATION; the
platform had no concept of one (`create_catalog(name)` took only a name).

Where managed tables physically live is a data-residency decision, and undoing
it means dropping the catalog. So it is NOT derived from the source location:
it is an explicit operator input, `plan-provisioning --storage-root`, recorded
in `provision_plan.json` where it is reviewable before apply. Omitted, the
catalog inherits the metastore root exactly as before, so metastores that have
one are untouched.

Human decision for this replay: `s3://amzn-workspace-rcm/` -- the customer's own
bucket, matching the existing `rcm` catalog, under the credential that already
validates.

Also removed: a DUPLICATE `create_catalog` definition in
`uc_intake.SdkUnityCatalogApi`. Python keeps the later definition, so the
rootless one silently shadowed the fixed one.

Verified live -- `applied`, `created: 5, existing: 1, blocked: 0, failed: 0`:
```
[ok] existing external location healthcare_rcm_ext_loc already covers ...
[ok] created catalog rcm_dev at s3://amzn-workspace-rcm/
[ok] created schema rcm_dev.bronze | .silver | .gold
[ok] created volume rcm_dev.bronze._checkpoints
```
Confirmed independently in UC: three schemas present, catalog
`storage_root: s3://amzn-workspace-rcm/`, `isolation_mode: OPEN`, volume created.

## F16 [OPEN, not blocking] Idempotency op_id ignores plan content

All three `apply-provisioning` runs reported the SAME
`op_id: 6fc743247007ada8` and `previously_applied_at: 2026-08-09T04:52:09Z`,
even though `provision_plan.json` materially changed between runs (the catalog
step gained `storage_root`). The envelope therefore claimed "This exact call was
already applied" about a call whose plan was different.

No harm here: `run_workspace_command` re-executes on replay to refresh counters,
so the new plan did apply. But the fingerprint does not cover the artifact the
command consumes, so the replay LABEL is false, and any future path that trusts
the replay cache to short-circuit would silently skip a changed plan.

Fix direction (not yet applied): include the plan file in `fingerprint_paths`
for `apply-provisioning` so a changed plan yields a new `op_id`.

## F20 [FIXED in `913ddca`, introduced by C3] Ghost reconcile's only caller passes bare names

Task C3 correctly changed `reconcile_ghost_tables` to diff on dbt's
fully-qualified `relation_name` (commit `ac7bc49`). Its sole production caller
was not updated and now feeds it the wrong shape.

`core/orchestration/cosmos_dag.py::_run_ghost_reconcile` (L403-407) queries:

```sql
SELECT table_name FROM `<catalog>`.information_schema.tables
WHERE table_schema = '<schema>'
```

and passes those BARE names (`fct_x`) into `reconcile_ghost_tables`. On the new
qualified path the model set holds `cat.gold.fct_x`, so nothing matches and
EVERY live table is reported as an orphan -- the exact inversion of the bug C3
set out to fix.

It does not fire today: `reconcile_ghost_tables` falls back to the legacy alias
diff unless every manifest node carries `relation_name`, and this workspace has
no built dbt project yet. It fires the first time a real manifest exists.

C3's tests pass because they exercise the function in isolation with
already-qualified input -- a reminder that a unit-level green says nothing about
the seam.

FIX (APPLIED in `913ddca`, with a regression test): select catalog and schema too and build
`f"{catalog}.{schema}.{table_name}"` before the call. (The deferral note here was
written while Task C1 held `cosmos_dag.py`; the fix landed once C1 committed.)
The catalog that expression uses was itself wrong until F21 -- it read the
declared base rather than the provisioned catalog.

Noted while reading, out of scope for the fix above: that same query
interpolates `schema` directly into SQL text. It is settings-derived rather than
user input, so it is not currently exploitable, but it should be parameterized.
CLOSED: both `catalog` and `schema` now pass through
`core.sql_safety.assert_safe_identifier` before interpolation. "Not reachable
from a user" was a property of that function's callers, not of the function.

---

## F21 -- the declared catalog is a BASE, and four consumers read it as concrete

Found while reviewing Task C4, which split `profiles.yml` into `dev`/`prod`
targets on `<base>_dev`/`<base>_prod` catalogs. That change is correct, but it
made a latent contradiction unavoidable: the same generated project named two
catalogs in `profiles.yml` and a third, different one everywhere else.

Ground truth for `workspaces/rcm`:

| where | key | value |
|---|---|---|
| `workspace_settings.json` | `databricks_source.catalog` | `rcm` |
| `interns/generated/contracts/provision_plan.json` | `catalog_base` | `rcm` |
| same | `env` | `dev` |
| same | `catalog` | **`rcm_dev`** |

`core/provisioning/plan.py::env_catalog` applies the catalog-per-env rule, so
the catalog that the live `apply-provisioning` run actually created is
`rcm_dev`. **The catalog `rcm` does not exist.** Four consumers read the base as
if it were the concrete catalog:

1. `dbt_project_generator.main()` -- defaulted `--catalog` from
   `databricks_source.catalog`, so `sources.yml` emitted `database: rcm`,
   `dbt_project.yml` emitted `vars.catalog: rcm`, and `--enforce-contracts`
   ran `DESCRIBE TABLE \`rcm\`....`.
2. `cosmos_dag._run_ghost_reconcile` -- queried
   `\`rcm\`.information_schema.tables`. A nonexistent catalog yields an empty
   listing, which this function treats as "no live tables", which reports
   every project model as an orphan. Same conflation F20 fixed one layer up.
3. `dbt_state.state_remote_root` -- built `/Volumes/rcm/_state/dbt/...` from
   `vars.catalog`, while the provisioned volume root is `/Volumes/rcm_dev/`.
4. `profiles.yml` (post-C4) -- the ONLY site that was accidentally right, and
   only for `dev`.

Nothing in the test suite caught it: every test passes `--catalog main`
explicitly, so the defaulting path -- the one real workspaces take -- was never
exercised, and with an explicit catalog no base/concrete distinction exists.

FIX (applied): `resolve_catalog_and_base(layout, catalog="")` returns the pair,
reading `provision_plan.json` as the authority and falling back to the declared
value unchanged for workspaces that never provisioned (local/POC keep today's
behavior byte for byte). An explicit `--catalog` names that exact catalog and is
its own base -- the env suffix is never re-applied on top of an operator's
literal.

Inside the emitted project, nothing hardcodes a catalog any more: `sources.yml`
and the `publish_gold` macro use `{{ target.database }}`, so they follow
whichever target the run selects. dbt-databricks aliases the profile's `catalog`
key onto `database` (verified against the installed adapter's
`DatabricksCredentials._ALIASES`), so `target.database` IS the active catalog.
`profiles.yml` derives its two targets through the shared `env_catalog`, not a
second inline copy of the naming rule.

Verified against the real workspace, not a fixture:
`resolve_catalog_and_base(WorkspaceLayout('workspaces/rcm'))` returns
`('rcm_dev', 'rcm')`; profile targets `rcm_dev` / `rcm_prod`;
`vars.catalog` `rcm_dev`, which makes `dbt_state`'s volume path agree with the
provisioned `checkpoint_root` (`/Volumes/rcm_dev/bronze/_checkpoints`).

Left open deliberately: `generate-dbt-project --workspace workspaces/rcm` still
refuses, and correctly -- `No KPI is fully ready for SQL ... Resolve KPI feature
mappings first.` The catalog fix is upstream of that gate; the emission itself
is proven by tests that run the real generator end to end.

---

## F22 -- discovery dropped the file extension, so COPY INTO pointed at nothing

**Live failure.** `run-ingestion` against the real warehouse:

```
[PATH_NOT_FOUND] Path does not exist:
s3://amzn-workspace-rcm/datasets/EMR/trendytech-hospital-a/departments
SQLSTATE: 42K03
```

1 failed, 11 `not_attempted` (stop-on-first-failure held, as designed).

Root cause is in the F6 fix itself. `_split_distinct_entities`
(`core/intake/discovery.py`) splits a folder of distinct entities into one
table per file -- correct -- but keyed each new group by the file's **stem**:

```python
stem = Path(member[0]).stem
child = f"{key_path}/{stem}" if key_path else stem
```

That key becomes `DiscoveredTable.path`, and `generate-ingestion` writes it
verbatim into `COPY INTO ... FROM '<path>'`. So the manifest recorded
`.../trendytech-hospital-a/departments` while the real object is
`departments.csv`. Every one of the 12 jobs carried a location that does not
exist; the first one to run said so.

The un-split case was never wrong: a genuine part-file layout groups on the
parent directory, which is a real path.

FIX (applied): key on `Path(member[0]).name`. Table naming is untouched --
downstream takes `Path(key).stem` either way, so `trendytech-hospital-a__
departments` is still the bronze name. Both halves of the contract are now
pinned: a split entity keeps its `.csv`, a part-file directory still points at
the directory.

## F23 -- CSV ingestion had no header option, and would have landed garbage

Found while reading the emitted SQL for F22. The COPY INTO template hardcoded:

```sql
FORMAT_OPTIONS ('mergeSchema' = 'true')
```

COPY INTO defaults CSV `header` to **false**. Had F22's path been correct,
every CSV would have loaded its header line as a DATA row with columns named
`_c0, _c1, ...` -- a silently wrong bronze table, which is worse than the loud
failure F22 produced, because the nonsense only surfaces much later in feature
resolution.

FIX (applied): `FORMAT_OPTIONS` is now format-aware -- `'header' = 'true',
'inferSchema' = 'true'` for delimited text only. Self-describing formats
(parquet/avro/orc) carry their own column names and must not get these options.

Stated assumption: the first line of a CSV source is a header. Discovery runs
`content_read_policy: metadata_and_paths_only` and never opens a file, so this
cannot be derived from evidence today. Headerless CSV would need an intake
question; no such source exists in this estate.

## F24 -- a failed run was recorded as an applied op

The same live run returned `status: idempotent_replay`, `previously_applied_at:
07:10:23Z` -- for a run that executed 0 of 12 jobs and failed.

`core/onboarding/workspace/cli_runner.py` called `record_op` on the sole
condition that `fn()` RETURNED. But the cloud-first commands report refusals
and failures as a structured payload (`ok: False`) rather than by raising --
that is the refusal ladder's entire design. So every structured failure was
stamped as applied, and each honest retry afterwards came back as a replay
telling the operator to pass `--allow-replay` to redo work that never happened.

Not as severe as it first reads: the replay path re-runs `fn()` under the lock
to refresh counts, so the run did re-execute and the reported numbers are
current. The damage is the false "already applied" record and the ceremony it
imposes on every retry.

FIX (applied): an explicit `ok: False` suppresses the record. Payloads with no
`ok` key are unaffected, so no currently-recording command stops recording.

Observation, not fixed: nothing fingerprints `discovery.json` into the
blueprint confirmation, so re-running `discover-source` after a discovery bug
does NOT re-open the human gate. That is convenient here and wrong in general
-- a corrected discovery can change what a human confirmed.

---

## F25 -- nothing profiled the landed tables, so KPI resolution stayed blind

Found while working out the execution order after F22-F24, not by a failing
command: `run-ingestion` would have succeeded and Task 1.3 would still have
been blocked.

`workspaces/rcm` has **no local `datasets/`** and
`interns/generated/profiles/profile_index.json` reads `{"profiles": []}`.
Discovery is deliberately metadata-only (`content_read_policy:
metadata_and_paths_only`) and records `columns: null` for all 12 tables. So the
only possible source of column-level evidence is the UC profiler --
`WorkspaceOnboarder.profile_databricks_tables` ->
`core.profiling.databricks_table_profiler.profile_uc_table`, which exists and
is correctly wired. What feeds it was wrong:

```python
catalog = str(source.get("catalog") or "").strip()   # "rcm"      <- the BASE
schema  = str(source.get("schema")  or "").strip()   # "default"
_, rows = client.execute_query(f"SHOW TABLES IN `{catalog}`.`{schema}`")
except Exception:
    return []                                        # silent
```

`databricks_source.catalog`/`.schema` describe the RAW SOURCE the operator
declared at intake. Ingestion lands in the PROVISIONED catalog's bronze schema
-- `rcm_dev.bronze`. Two independent errors in one query:

1. `rcm` instead of `rcm_dev` -- F21's fifth consumer, which that fix missed.
2. `default` instead of `bronze` -- the declared source schema is not where the
   medallion lands.

Then `except Exception: return []` made an unreachable warehouse, a missing
catalog and a genuinely empty schema all read identically as "zero tables". The
exclusive-mode zero-tables warning did fire, but with no reason attached it
pointed at "fix the connection" when the connection was fine and the query was
aimed at the wrong place.

Net effect: 131 features `blocked_missing_evidence`, 7 `candidate_pattern`,
0 resolved -- and landing the data would not have moved a single one.

FIX (applied): `_profiling_source_pair()` prefers `provision_plan.json`'s
`catalog` + `bronze_schema`; a workspace that never provisioned (one pointing at
pre-existing UC tables) keeps its declared pair byte-for-byte. The discovery
failure is recorded in `_databricks_discovery_error` and printed by the
zero-tables warning.

Verified against the real workspace: `_profiling_source_pair` for
`workspaces/rcm` returns `('rcm_dev', 'bronze')`, where the 12 COPY INTO jobs
target. Before the fix it returned `('rcm', 'default')`.

**Order lesson.** Three of the last five findings (F21, F25, and F20's catalog)
are the same mistake in different modules: a NAME that means one thing at
declaration time and another after provisioning, read as if it never changed.
Any future consumer of `databricks_source.catalog` should be assumed wrong
until checked against `provision_plan.json`.
