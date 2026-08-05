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
