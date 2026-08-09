---
name: context-map-sync
description: >
  Keep every CONTEXT-<folder>.md true to the code beside it. Use whenever a file under a mapped
  directory is created, deleted, renamed, or has a signature/flag/constant change -- and ALWAYS
  before staging a commit that touches core/, tools/, tests/, config/, docs/, skills/ or vendor/.
  CONTEXT-MAP.md rule 3 makes this atomic with the code change, not a follow-up.
---

# Context Map Sync

`CONTEXT-MAP.md` is not documentation-for-its-own-sake. Agents read these files *instead of* the
code to decide what to call. A stale entry does not merely mislead a human — it produces wrong
code, and the wrongness surfaces at runtime against a real account.

That is not hypothetical. `core/execution/CONTEXT-execution.md` documented a class named
`DatabricksExecutionClient`; the real class is `DatabricksClient` and it takes a resolved config.
Trusting the doc shipped a runner that could never work, and it failed live against a Databricks
account mid-replay (F19). One stale line, one dead command.

## The rule

**A code change and its CONTEXT update are one change.** Not a follow-up, not a cleanup pass, not
"I'll do it before the commit" — the same edit session, before `git add`.

## When this fires

Any of these, in a directory that has a `CONTEXT-<folder>.md`:

- a file is **added**, **deleted**, or **renamed**
- a public function/class/constant is **added, removed, or renamed**
- a **signature** changes — parameters, defaults, return type
- a **CLI flag** is added or removed
- a **failure mode** changes (new refusal, new raise, changed exit code)
- a documented **line-number anchor** moves materially

Adding a private helper used once inside its own module does not fire this.

## Procedure

1. **Map the touched files to their CONTEXT owners.** One per directory, named for the directory:
   `core/provisioning/*.py` -> `core/provisioning/CONTEXT-provisioning.md`. Look up the path in
   `CONTEXT-MAP.md` if unsure.

2. **Read the current entry before editing it.** Entries drift independently of your change; you
   will frequently find pre-existing errors sitting next to the line you came to update. Fix what
   you can verify from code you have actually read. Do not "correct" things you are guessing at.

3. **Update the entry to match disk reality**, in the file's established shape:
   `Exact Purpose` / `Key Functions / Classes` / `Inputs & Outputs` / `Failure Modes & Edge Cases`.

4. **New file? Add a numbered section.** Keep sections alphabetical if the file already is.
   **Deleted file? Remove its section**, and any cross-reference pointing at it.

5. **New directory? Update `CONTEXT-MAP.md`'s tree** as well as writing the new
   `CONTEXT-<folder>.md`.

6. **Links use the repo convention** — GitHub-style markdown with a `file://` URI and forward
   slashes:
   `[`apply.py`](file:///C:/Users/shubh/OneDrive/Desktop/interns/core/provisioning/apply.py)`

7. **Stage the CONTEXT file in the same commit** as the code.

## Verification

There is no coverage checker. `tools/context_status.py` sounds like one and is not — it estimates
LLM chat-context SIZE, has no `main()`, and prints nothing when run. Do not rely on it here.

**Import every symbol you just documented.** This is the check that would have caught F19, and it
costs one line:

```powershell
.venv\Scripts\python.exe -c "from core.provisioning.ingestion_run import run_ingestion_jobs, sql_statements; print('[ok]')"
```

**Confirm the directory you touched still has an owner:**

```powershell
Get-ChildItem -LiteralPath core/provisioning -Filter "CONTEXT-*.md"
```

A directory holding source files with no `CONTEXT-<folder>.md` is a coverage gap — add one and add
it to the tree in `CONTEXT-MAP.md`.

> A real coverage/drift verifier — walk every mapped directory, import each documented symbol,
> resolve each `file://` link — does not exist yet and is worth building. Until it does, the import
> line above is the honest substitute.

## Anti-patterns

- **Documenting intent instead of reality.** If the function is named `build_provision_plan`, the
  entry says `build_provision_plan` — not the tidier name you wish it had.
- **Copying a signature from another CONTEXT file.** That is how drift propagates.
- **Line anchors you did not verify.** A wrong `#L30-L110` is worse than no anchor, because it
  looks precise. Prefer a bare file link over a fabricated range.
- **Batching it up "at the end".** The end is where it gets dropped; four commits went out without
  their CONTEXT updates that way in one session.
- **Leaving a known-wrong neighbouring line** because it was not your change. If you read the code
  and can see the entry is false, fix it — that is the cheapest moment it will ever be fixed.
