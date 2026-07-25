# Production Security Threat Model and Gap-Remediation Report

**Date:** 2026-06-14
**Scope:** Architecture- and deployment-level gaps that only bite in production.
**Prior art:** `docs/core_audit/SECURITY_RECHECK.md` (classic appsec audit, verdict: largely
sound). This document does NOT duplicate that report; reference it for Surface 1-5 findings.
**Data context:** The platform processes healthcare-RCM data (HIPAA-18 identifiers detected
by `core/governance/phi_gate.py`) and may also carry PCI-DSS cardholder identifiers (same
file, `PCI_IDENTIFIER_PATTERNS`). HIPAA and PCI DSS compliance requirements are therefore
in scope for the remediation designs below.

---

## Gap 1 — Dashboard Authn/Authz + Werkzeug Debug RCE

**Priority:** CRITICAL
**Effort:** M (code partially done; deployment configuration remains)
**Status:** Being hardened in code now — residual deployment requirements documented here.

### Threat

An attacker who can reach the dashboard HTTP port gains access to all KPI results, workspace
artifacts, PHI column names, and blocker panels without credentials. If
`AUTORESEARCH_DASHBOARD_DEBUG=1` is set in production the Werkzeug interactive debugger is
active — it exposes a PIN-protected but exploitable console that allows arbitrary Python
execution on the server process.

The dashboard chat path (`_dashboard_llm_chat`, `dashboard.py:1043-1112`) also invokes
real CLI agents (`claude`, `gemini`, `codex`) as subprocesses with the full repo as `cwd`.
An authenticated but malicious session user can craft questions to extract filesystem
contents or trigger unintended CLI operations.

### Current state (verified)

- `dashboard.py:4936-5005` — `_setup_auth()` attaches Basic Auth or Bearer-token checking
  via a Flask `before_request` hook. Constant-time comparison (`hmac.compare_digest`) is
  used correctly. However, activation is opt-in: if `AUTORESEARCH_DASHBOARD_AUTH_USER` /
  `AUTORESEARCH_DASHBOARD_AUTH_PASSWORD` / `AUTORESEARCH_DASHBOARD_AUTH_TOKEN` are not set,
  the gate is NOT installed and the dashboard is fully open.
- `dashboard.py:4915-4922` — `_resolve_debug()` gates Werkzeug debug on
  `AUTORESEARCH_DASHBOARD_DEBUG=1`. Default is off. A stderr warning is printed when debug
  is on (`dashboard.py:5030-5036`). This is correct.
- `dashboard.py:4925-4933` — `_resolve_host()` defaults to `127.0.0.1`. A non-loopback
  binding emits a stderr warning (`dashboard.py:5040-5046`). This is correct.
- There is no session management, no CSRF protection, no rate limiting, and no OIDC/SSO
  integration. The token/Basic-Auth mechanism is single-credential and stateless, which is
  adequate for single-user local use but not for multi-user production.
- No TLS is configured at the application layer; Dash/Flask runs plain HTTP.

### Production consequence

- PHI data breach: unauthenticated access to all workspace KPI results, profile summaries,
  and blocker panels that may contain field names, sample counts, or derived values from
  patient records.
- RCE via Werkzeug debugger if `AUTORESEARCH_DASHBOARD_DEBUG=1` is accidentally set in a
  network-reachable deployment (e.g. container with `0.0.0.0` binding).
- Compliance failure: HIPAA Security Rule requires access controls, audit controls, and
  transmission security for any system handling PHI.

### Remediation design

1. **Reverse proxy (immediate, deployment config):** Never bind Dash/Flask directly to a
   public interface. Place nginx or a cloud load balancer in front. The reverse proxy
   handles TLS termination (minimum TLS 1.2, recommend TLS 1.3), forces HTTPS redirects,
   and can enforce IP allowlisting for the admin surface. Example nginx snippet:
   ```
   server {
       listen 443 ssl;
       ssl_certificate     /etc/ssl/certs/dashboard.crt;
       ssl_certificate_key /etc/ssl/private/dashboard.key;
       ssl_protocols       TLSv1.2 TLSv1.3;
       location / { proxy_pass http://127.0.0.1:8050; }
   }
   ```
2. **SSO/OIDC for multi-user (infra roadmap):** The current Basic-Auth hook in `_setup_auth`
   is single-credential. For multi-user deployments replace it with an OIDC middleware such
   as `flask-oidc` or `authlib`. The `before_request` hook extension point at
   `dashboard.py:4979` is already the right wiring location. Keycloak, Okta, or
   Entra ID (Azure AD) are suitable providers; the choice is deployment-environment-driven.
3. **CSRF tokens:** Add `flask-wtf` or inline CSRF token validation to all state-mutating
   callbacks (build triggers, approval gates, chat submit). Dash callbacks that call
   `WorkspaceCommandService` are the highest priority.
4. **Rate limiting:** Add `flask-limiter` to the Bearer/Basic-Auth gate and the chat submit
   endpoint to prevent brute-force credential attacks.
5. **Debug gate enforcement:** Add a startup assertion that prevents
   `AUTORESEARCH_DASHBOARD_DEBUG=1` when `AUTORESEARCH_DASHBOARD_HOST` is not loopback.
   This is a one-line guard before `app.run` at `dashboard.py:5059`.
6. **Hardening checklist before any network-reachable deployment:**
   - `AUTORESEARCH_DASHBOARD_HOST` must be `127.0.0.1` or the reverse proxy's loopback.
   - `AUTORESEARCH_DASHBOARD_DEBUG` must NOT be set to `1`.
   - At least one of `AUTORESEARCH_DASHBOARD_AUTH_USER+PASSWORD` or
     `AUTORESEARCH_DASHBOARD_AUTH_TOKEN` must be set.
   - TLS must terminate at the reverse proxy.
   - Access logs from the reverse proxy must feed the centralized log store (Gap 7).

---

## Gap 2 — No Multi-Tenant Isolation / RBAC

**Priority:** CRITICAL / HIGH (scales with number of concurrent users / workspaces)
**Effort:** L
**Status:** INFRA ROADMAP — cannot be fixed by editing code alone.

### Threat

The platform currently operates as a single-trust-domain: any authenticated dashboard user
can read any workspace under `workspaces/`, inspect any KPI result, browse any artifact, and
trigger any build. If two business units share an instance (e.g., a cardiology KPI workspace
and a billing KPI workspace), a user from unit A can view PHI-adjacent data from unit B.
There is no workspace-level ownership concept, no role hierarchy, and no data-partition
enforcement.

The `IsolatedDuckDBBackend` (`core/execution/backend.py:205-256`) strips env vars before
spawning the subprocess and is documented as "Future versions will use Docker-based
isolation." It is currently unreachable from the factory (`build_execution_backend` at
`backend.py:527` never instantiates it), so the env-isolation benefit is never exercised.

### Current state (verified)

- `dashboard.py:316-324` — `_scope_roots()` partitions the artifact browser by
  `scope="workspace"` vs `scope="project"` plus an `advanced` toggle. This is a UI
  convention, not an authorization boundary; the underlying file paths are unconstrained
  within `ROOT`.
- `dashboard.py:423-450` — `_artifact_inventory()` iterates `rglob("*")` over all roots
  returned by `_scope_roots`. No per-user filter or ACL check.
- `_path_from_rel` (`dashboard.py:235-244`) checks that the resolved path is inside `ROOT`,
  preventing path traversal (good), but makes no per-user workspace claim.
- `core/governance/provenance.py` — distinguishes agent vs human confirmer but has no
  concept of which human, no role, no workspace claim.
- No `users.json`, RBAC config, or per-workspace ACL file exists anywhere in the repo.

### Production consequence

- Cross-tenant data exposure: user from workspace A reads KPI results for workspace B,
  including PHI-adjacent profile metadata.
- HIPAA minimum-necessary standard violation: all authenticated users see all workspaces.
- Lateral movement: a compromised session for a low-trust workspace gains visibility into
  a PHI workspace with no enforcement boundary.

### Remediation design

**Phase 1 — Workspace ownership manifest (low-code, months 1-2):**
Add `config/workspace_acl.json` (operator-managed):
```json
{
  "workspaces": {
    "healthcare-rcm": { "owners": ["alice"], "viewers": ["bob"] },
    "billing-ops":    { "owners": ["carol"], "viewers": [] }
  }
}
```
The `_setup_auth` hook in `dashboard.py:4936` is extended to resolve the authenticated
user identity and inject it into Flask's `g` context. `_scope_roots` and
`_artifact_inventory` receive the identity and filter against the ACL. This requires
identity beyond the current single-credential Basic-Auth model (see Gap 1 OIDC step).

**Phase 2 — Workspace-level data partitioning (months 2-4):**
Each workspace gets its own DuckDB file (`workspace.db`) under
`workspaces/<name>/interns/state/`. Access to that file is governed at the storage layer
via OS file permissions (Linux: `chown workspace-owner:workspace-group`, `chmod 640`).
The `ResourceManager` (`core/resource/manager.py`) and `DuckDBBackend._run_subprocess`
receive the workspace-specific file path; the subprocess drops to the OS identity of the
workspace owner via `setuid`/`setgid` before opening the file (Linux only; on Windows use
service accounts or Managed Identities).

**Phase 3 — Docker-based execution isolation (months 4-8, INFRA):**
Wire `IsolatedDuckDBBackend` into `build_execution_backend` behind a config flag
`execution = "isolated"` in `config/lock.toml`. Each experiment subprocess runs in its
own ephemeral container:
- Base image: slim Python with no network access.
- Mounts: `workspaces/<name>/` read-only dataset mount + `workspaces/<name>/interns/`
  read-write result mount. No other filesystem access.
- Network policy: `--network=none` (no egress from the worker container).
- Resource limits: `--memory 2g --cpus 1.0`.
- Runtime: Docker with `--security-opt=no-new-privileges` + a seccomp profile denying
  `ptrace`, `mount`, `pivot_root`, and socket syscalls the worker does not need.
  gVisor (`runsc`) is a stronger option if the deployment environment supports it.
This is the production completion of the in-code stub at `backend.py:205-256`.

**Phase 4 — Authz middleware (months 6-10, INFRA):**
For enterprise deployments, introduce Open Policy Agent (OPA) as a sidecar. The Flask
`before_request` hook sends workspace + user + action to the OPA `/v1/data/authz/allow`
endpoint and aborts on `false`. Policy files live in `config/opa_policies/` and are
version-controlled.

---

## Gap 3 — No Secrets Management + No Encryption at Rest

**Priority:** HIGH
**Effort:** M-L
**Status:** INFRA ROADMAP — cannot be fixed by editing source code.

### Threat

All secrets (Databricks PAT, GitHub PAT, Google API key, Anthropic API key) live in `.env`
or in the shell environment at runtime. The `.mcp.json` file (`root:.mcp.json:16-28`)
confirms that `DATABRICKS_TOKEN` and `GITHUB_PERSONAL_ACCESS_TOKEN` are passed via
environment variable substitution. The `ConnectBackend` (`core/execution/backend.py:377-380`)
explicitly copies the process environment and injects `DATABRICKS_TOKEN` into a child
process, making the token visible in `/proc/<pid>/environ` on Linux for the subprocess
lifetime.

DuckDB files (`workspace.db`, decision history), Delta tables written by `write_delta`
(`core/execution/databricks_client.py`), and audit chain JSONL files at
`workspaces/<name>/interns/state/audit_chain.jsonl` are stored in plaintext on the
local filesystem. If the host is compromised or the disk image is exfiltrated, all
KPI results and PHI-adjacent schema metadata are readable without any additional
credential.

### Current state (verified)

- `.env` is gitignored (`SECURITY_RECHECK.md: Surface 1`). Correct for source control.
  But `.env` on disk is plaintext; anyone with filesystem read access reads all secrets.
- `core/config.py` reads all secrets via `os.environ.get(...)`. No secret-manager SDK is
  wired. No token rotation or scoping is enforced by the platform.
- `SECURITY_RECHECK.md: Surface 1` confirms no committed secrets. The runtime exposure
  (env var in child process, `/proc/pid/environ`) is flagged as [NOT-PROD] in
  `docs/core_audit/execution.md`.
- No encrypted-volume configuration, no app-layer envelope encryption, and no key
  management infrastructure exists in the repo.

### Production consequence

- Token exfiltration: a host compromise or process-listing attack gives an attacker the
  Databricks PAT and GitHub PAT, enabling Unity Catalog reads, SQL warehouse queries, and
  repo write access.
- PHI data breach: unencrypted `workspace.db` / audit chain on disk is readable if the
  host's disk is imaged. For a HIPAA-covered entity, unencrypted PHI at rest is a
  reportable breach under 45 CFR 164.312(a)(2)(iv) (encryption and decryption).
- PCI DSS Requirement 3.5 requires encryption of stored cardholder data with strong
  cryptography.
- Secret sprawl: long-lived PATs in `.env` are never rotated; a token leaked months
  earlier may still be valid.

### Remediation design

**Secrets management (months 1-3):**
Replace `.env` secret storage with a dedicated secret manager. Recommended options in
priority order for this stack:
1. HashiCorp Vault (self-hosted, works on-prem): `hvac` Python client. Secrets read at
   startup via `vault.secrets.kv.v2.read_secret_version(path="autoresearch/prod")`.
   `core/config.py`'s `load()` function is the single injection point.
2. AWS Secrets Manager / Azure Key Vault / GCP Secret Manager (if cloud-hosted):
   respective SDK calls in `load()`.
3. Minimum viable: OS keyring (`keyring` Python library) for local developer machines;
   CI/CD uses the platform's native secret injection (GitHub Actions encrypted secrets,
   Databricks job secrets).

Token scoping and rotation:
- Databricks: replace long-lived PATs with OAuth M2M tokens (client credentials flow)
  scoped to the minimum required Unity Catalog permissions. Rotate on a 90-day schedule
  via the Databricks account console or Vault dynamic secrets.
- GitHub: replace classic PATs with fine-grained PATs scoped to the single repo with
  `contents: read` and `pull_requests: write`. Disable the broad classic PAT.
- Anthropic / Google API keys: use short-lived API key rotation via the respective
  developer console; store in Vault or the cloud secret manager, not in `.env`.

**Encryption at rest (months 2-5, INFRA):**
Two complementary approaches:

Option A — Volume-level encryption (simpler, lower code impact):
- Enable LUKS (Linux Unified Key Setup) on the disk partition that holds `workspaces/`.
  Key stored in Vault; unlocked at boot via a Vault agent.
- For cloud deployments use encrypted EBS (AWS), encrypted Persistent Disk (GCP), or
  encrypted Managed Disk (Azure) with customer-managed keys (CMK) in the respective KMS.
- DuckDB files, Delta tables, and audit chain JSONL are automatically protected.

Option B — App-layer envelope encryption (stronger, more portable):
- Wrap sensitive workspace artifacts (DuckDB files, audit chain JSONL, decision history
  JSON) with AES-256-GCM encryption in `core/storage/workspace.py` and
  `core/governance/audit_chain.py` using a per-workspace data-encryption key (DEK) stored
  encrypted (wrapped by a key-encryption key, KEK) in Vault or the cloud KMS.
- The `write_delta` path in `core/execution/databricks_client.py` should write only to
  Unity Catalog tables that live in a Databricks workspace with Customer-Managed Keys
  (CMK) enabled — not to arbitrary external Delta paths.

**Minimum prod checklist for HIPAA compliance:**
- [ ] No secrets in `.env` on production hosts; all credentials from Vault or KMS.
- [ ] Databricks workspace has the Compliance Security Profile (BAA required by
  `phi_gate.py:databricks_phi_covered`).
- [ ] Disk encryption at rest enabled and key rotation documented.
- [ ] Secret access is audited (Vault audit log / KMS CloudTrail).

---

## Gap 4 — No Supply-Chain / SAST / Secret Scanning in CI

**Priority:** HIGH
**Effort:** S (workflow in progress)
**Status:** Being added now — `.github/workflows/security.yml` exists; document triage
cadence and policy.

### Threat

A malicious or vulnerable transitive dependency (dependency confusion, typosquatting, or
known CVE) reaches production without detection. A developer accidentally commits a secret
(API key in a test fixture, token in a config example) and it is merged before being caught.

### Current state (verified)

- `.github/workflows/security.yml` (verified, full read) defines three jobs:
  - `dep-audit` — `pip-audit` over the resolved dependency tree. `continue-on-error: true`
    (advisory, does not block merge). Runs on every push to main and every PR.
  - `sast` — `bandit -r core tools -ll -ii` (medium+ severity AND medium+ confidence).
    BLOCKING: non-zero exit fails the build.
  - `secret-scan` — `gitleaks/gitleaks-action@v2` with `fetch-depth: 0` (full history
    scan). BLOCKING: secrets found fail the build.
- `.github/dependabot.yml` exists (confirmed by directory listing). Dependabot config
  enables automated dependency update PRs.
- `ci.yml` has no security scanning; `security.yml` runs independently on the same
  triggers.

### Production consequence

- A known-CVE transitive dependency (e.g., a vulnerable `cryptography` or `requests`
  version) is not surfaced until manual audit.
- A committed secret reaches the `main` branch history and is not rotated; the secret
  remains in git history indefinitely even after the file is deleted.

### Remediation design (operational policy, not code change)

**Triage cadence:**
- `dep-audit` findings should be reviewed weekly (Monday triage meeting) by the team lead.
  A finding against a direct dependency must be resolved within 14 days of CVE publication
  (CVSS >= 7.0) or 30 days (CVSS < 7.0). A finding against a transitive dependency is
  reviewed case-by-case; if no upstream fix exists, document the exception with a
  remediation date in `docs/core_audit/DEP_EXCEPTIONS.md`.
- Consider changing `dep-audit` to `continue-on-error: false` (blocking) once the
  direct-dependency CVE backlog is cleared. This is the recommended end-state.

**Failing-build policy:**
- `sast` (bandit) and `secret-scan` (gitleaks) are already blocking. Do not add
  `continue-on-error: true` to these jobs under any circumstances.
- If bandit produces a false positive, suppress it with an inline `# nosec B<id>` comment
  with a justification comment on the same line. Never suppress entire files.
- If gitleaks detects a false positive (e.g., a test fixture with a fake key-shaped
  string), add a `.gitleaks.toml` allow-rule with the exact file path and a note
  explaining why the match is not a real secret. Review allow-rules in each quarterly
  security review.

**Additional hardening to add:**
- Pin the `gitleaks/gitleaks-action` to a specific commit SHA (not `@v2`) to prevent
  supply-chain substitution of the action itself.
- Add `pip-licenses` or `cyclonedx-bom` as an advisory step to generate an SBOM on each
  release tag. This supports HIPAA and PCI DSS supply-chain documentation requirements.
- Consider adding `safety` (PyUp) as a second CVE scanner alongside `pip-audit` for
  broader advisory database coverage.

---

## Gap 5 — No Real Execution Sandbox for Generated PySpark/Python + Profiler Subprocesses

**Priority:** HIGH / MED
**Effort:** L
**Status:** INFRA ROADMAP.

### Threat

The `DuckDBBackend._run_subprocess` (`core/execution/backend.py:144-177`) spawns the
`experiment_cmd` from `config/tasks.json` as a child process with:
- `env = os.environ.copy()` (full environment including all secrets)
- `cwd = ROOT` (full repo access)
- No filesystem restrictions, no network restrictions, no resource limits beyond a
  wall-clock timeout.

Generated PySpark scripts (`core/onboarding/kpi/pyspark_generator.py`) and generated
Python code run through the same path. If a generated script is malformed or if an
adversary can influence its content (via a hostile workspace document that evades
`injection_guard`), the subprocess runs with the full privileges of the dashboard process.

The profiler tool (`tools/optimizer_finder.py:270-271`, flagged [~] in `SECURITY_RECHECK.md`)
already embeds file paths in a Python `-c` string; if combined with a crafted workspace
path, the subprocess execution surface widens further.

The `IsolatedDuckDBBackend` (`backend.py:205-256`) strips the environment and sets
`AUTORESEARCH_ISOLATED_WORKER=1` but: (a) it is not wired into the factory; (b) it imposes
no filesystem isolation; (c) it imposes no network isolation; (d) it has no resource limits
(no `ulimit`, no cgroup, no container boundary).

### Current state (verified)

- `backend.py:149` — `env = os.environ.copy()` confirms full env inheritance in
  `DuckDBBackend`.
- `backend.py:217-224` — `IsolatedDuckDBBackend` strips env down to PATH, SYSTEMROOT,
  PYTHONPATH, and `AUTORESEARCH_*` vars. This is an improvement in env isolation only.
- `backend.py:205` docstring: "Future versions will use Docker-based isolation."
- No cgroup, seccomp, `setrlimit`, or container configuration found anywhere in `core/`.
- No network-egress policy for worker processes.

### Production consequence

- A malicious or buggy generated script can exfiltrate data, make network calls to
  attacker infrastructure, read other workspace data, or delete files — all within the
  same OS user and filesystem scope as the dashboard.
- For HIPAA: a worker that can read `workspaces/*/datasets/` across workspace boundaries
  violates minimum-necessary and access-control requirements.

### Remediation design

**Step 1 — Wire IsolatedDuckDBBackend (short-term, code change):**
Add `execution = "isolated"` as a valid value in `config/lock.toml` and wire it in
`build_execution_backend` (`backend.py:527`). This immediately reduces env leakage. It
does not provide filesystem or network isolation but reduces the attack surface of
`DuckDB+ConnectBackend` token injection (`SECURITY_RECHECK.md` residual risk, Surface 1).

**Step 2 — seccomp profile + ulimit (medium-term, deployment config):**
When running on Linux, wrap the subprocess invocation in `prlimit` (or Python
`resource.setrlimit`) before `execvp`:
- `RLIMIT_NOFILE`: limit open file descriptors to ~64.
- `RLIMIT_CPU`: match the `time_budget` in seconds.
- `RLIMIT_AS`: set to 2 GB address space.
Apply a seccomp filter (via `seccomp` Python binding or a wrapper script using
`unshare --net`) that denies: `execve` to interpreters outside the venv, `socket` calls
with `AF_INET`/`AF_INET6` (deny network egress), `mount`, `ptrace`.

**Step 3 — Container isolation (long-term, INFRA — see also Gap 2 Phase 3):**
Move to full OCI container isolation per the design in Gap 2. This provides:
- Filesystem: only the workspace dataset directory mounted read-only + the workspace
  interns/ directory mounted read-write. No access to `core/`, `tools/`, `.env`, or other
  workspace directories.
- Network: `--network=none` (no egress). Generated PySpark that needs a Databricks
  endpoint will be rewritten to use a controlled proxy endpoint, not a raw socket.
- Runtime: gVisor (`runsc`) for defense-in-depth against container escapes.

**Profiler path hardening (code, near-term):**
In `tools/optimizer_finder.py:270-271`, replace path embedding in the `-c` string with
an argument passed via a temp file or environment variable (as recommended in
`SECURITY_RECHECK.md`). This removes the syntax-break / code-injection risk in the
developer tool before the container sandbox is in place.

---

## Gap 6 — Audit-Log Non-Repudiation

**Priority:** MED
**Effort:** S (hash chain is done; residual is design)
**Status:** Being fixed in code now (SHA-256 hash chain implemented). Residual documented here.

### Threat

An attacker who gains write access to the `workspaces/<name>/interns/state/audit_chain.jsonl`
file can replace the entire chain and recompute all SHA-256 hashes (since the hash key is
public knowledge — it is the file content itself). The `verify_chain` function will report
`ok=True` on a fully-forged chain. This means the chain detects accidental corruption and
naive single-line tampering, but does NOT prevent a determined attacker from fabricating
the entire audit history.

### Current state (verified)

- `core/governance/audit_chain.py` — SHA-256 chain with genesis constant, seq
  contiguity, prev-hash linkage, and content-integrity checks. File is JSONL; `verify_chain`
  walks all entries. The module's own docstring at `audit_chain.py:29-35` accurately states
  this limitation: "not non-repudiation against an attacker who can read the file and
  recompute hashes."
- No HMAC secret, no asymmetric signing, no off-box log shipping is implemented.
- The chain file lives on the same filesystem as the rest of the workspace; no separate
  access control for the chain file vs the workspace data.

### Production consequence

- A malicious insider or an attacker who achieves RW file access can erase all evidence
  of a PHI access event by rewriting the chain. For HIPAA (45 CFR 164.312(b)) audit
  controls must ensure the integrity of records; a chain that can be silently forged does
  not meet this bar.

### Residual remediation design

**Option A — HMAC-with-managed-secret (simplest upgrade, months 1-2):**
Replace the bare SHA-256 entry hash with `HMAC-SHA256(managed_secret, prev_hash +
canonical_json(record))`. The `managed_secret` is a 256-bit key stored in the secret
manager (Gap 3). An attacker who can write the file but not read the HMAC key cannot
forge a valid chain. Requires adding `hmac` (stdlib) and one `SecretManager.get()` call
in `audit_chain.py`.

**Option B — Asymmetric signing (stronger, months 2-4):**
Each append signs the entry with an Ed25519 private key held in the secret manager (or
a hardware HSM). `verify_chain` verifies against the corresponding public key (which can
be stored in the repo). An attacker who rewrites the file would need the HSM key to
produce valid signatures. Use `cryptography.hazmat.primitives.asymmetric.ed25519`.

**Option C — Off-box log shipping (defense-in-depth, months 1-3):**
Forward each `append_audit_record` call to a centralized SIEM (Splunk, Elastic, or a
cloud logging service such as AWS CloudWatch Logs or Azure Monitor). The SIEM log is
append-only by policy (no delete API); the local JSONL chain is a hot copy. Divergence
between the two is a tamper signal. This is complementary to Option A/B, not a
replacement.

**Recommended approach:** A + C. Option A costs ~30 lines of code change; Option C costs
a logging SDK call per `append_audit_record` invocation. Together they provide both
on-box HMAC integrity and off-box immutability.

---

## Gap 7 — PII/PHI in Application Logs

**Priority:** MED
**Effort:** S (filter implemented; coverage and retention policy remain)
**Status:** Being fixed in code now. Residual documented here.

### Threat

Application log records that capture exception details, subprocess output, or HTTP
request parameters may contain PII/PHI values (patient names, MRNs, email addresses)
from workspace data that passes through the pipeline. Once in a log file, this data is
subject to different retention and access controls than the primary workspace, creating
a PHI shadow copy that may be overlooked in breach inventory.

### Current state (verified)

- `core/observability/log_redaction.py` — `RedactionFilter` class attaches to Python's
  logging framework. Covers: GitHub tokens, Databricks tokens, OpenAI `sk-` tokens, AWS
  AKIA keys, Bearer headers, generic `api_key=`/`token=`/`secret=` assignments, email
  addresses, US SSNs, and long digit runs (12+ digits). Redaction marker: `[REDACTED:<kind>]`.
  The module is idempotent when `install_log_redaction()` is called multiple times.
- `core/observability/__init__.py` — exports `install_log_redaction`.
- Known design limitation documented in `log_redaction.py:33-36`: "if a caller creates
  a new root handler that captures records before they propagate to root, that handler
  would not be covered unless `install_log_redaction()` is called again for that specific
  logger."

### Residual risks

1. **Third-party library logs:** `aiohttp`, `databricks-sdk`, `pyspark`, and `urllib3`
   log at the WARNING/ERROR level and may include URL query parameters, response bodies,
   or exception messages that contain PHI values. The `RedactionFilter` is attached to
   the application logger; if a library uses its own `logging.getLogger(__name__)` and
   that logger has a direct handler (e.g., `StreamHandler` added by the library), the
   filter does not apply. Mitigation: call `install_log_redaction(logging.root)` so the
   root logger's filter fires before any propagated record reaches any handler; additionally
   enumerate all third-party logger names and install the filter on each.
2. **Log retention and rotation:** No log retention policy exists. Log files at
   `workspaces/<name>/interns/state/*.log` and `backend:duckdb` logs at
   `log_path` (a caller-supplied path) accumulate indefinitely. For HIPAA, PHI in log
   files must be covered by the same minimum-necessary and retention policies as primary
   records. Log files containing PHI cannot be retained beyond the minimum needed.
3. **Log file access control:** The log files live in `workspaces/*/interns/state/`, which
   is readable by any user with access to the workspace directory. No separate ACL for
   log files exists.

### Residual remediation design

- **Root-logger filter:** Change `install_log_redaction()` calls to target `logging.root`
  and iterate `logging.Logger.manager.loggerDict` on startup to cover already-initialized
  third-party loggers.
- **Log retention policy:** Add a log rotation config (`logging.handlers.RotatingFileHandler`
  or `TimedRotatingFileHandler`) with `maxBytes=50MB`, `backupCount=7`, and automatic
  compression. Implement a cron / GitHub Actions scheduled job to delete logs older than
  90 days (or the HIPAA/PCI minimum retention period, whichever is shorter).
- **Centralized log store:** Ship logs to the SIEM specified in Gap 6 Option C. The SIEM
  enforces access controls and provides the audit trail for log-access events themselves.

---

## Gap 8 — AI-Security Coverage Completeness (Injection Guard)

**Priority:** MED
**Effort:** S (audit + targeted code fix)
**Status:** Substantially closed (2026-07-25, "Security S3" of the lingering-issues plan). Item 3
(dashboard chat context) was fixed earlier the same session ("Security S6" equivalent). Item 1
(image OCR) was traced and confirmed safe, not patched. Item 2 (external intake) remains open —
not yet audited.

### Threat

A hostile workspace (a data dictionary or dataset containing instruction-like text) can
inject prompt-manipulation payloads into an LLM call if the text passes through an
untrusted-to-LLM path that does NOT call `injection_guard.neutralize_text` or
`injection_guard.neutralize_rows`.

### Current state (verified 2026-07-25, supersedes the original findings below)

- `core/governance/injection_guard.py` gained a third primitive, `neutralize_json(value)` —
  recursively neutralizes every string leaf in a nested dict/list, for JSON structures
  serialized into an artifact (applied before `json.dumps`, not to the dumped text, to avoid
  a match spanning JSON syntax and corrupting structure).
- `core/onboarding/kpi/blocker_question_panel.py` was credited with coverage in the original
  version of this gap, but that credit was misleading: only one narrow function
  (`_execute_option_preview`) was actually guarded. The panel's own declared
  `primary_artifact` (`current.md`) and `current_full.md` rendered raw KPI business-question
  prose, raw sample/observed values, a human-typed wiki "why" note, blocked-KPI prose
  excerpts, and the full CLI-agent evidence pack (including PDF/DOCX-extracted data-dictionary
  excerpts) completely unguarded. All now fixed — see the render functions
  `_render_markdown_compact`, `_render_markdown`, `_render_sample_evidence`.
- `core/onboarding/kpi/kpi_confirmation_panel.py`'s `render_kpi_confirmation_markdown`
  ("Real row, read back" section) had the same gap — raw workbook cell values rendered
  truncated but unneutralized. Fixed.
- Fixed at the SOURCE, not just the reader: `core/onboarding/workspace/onboarding.py`'s
  `_extract_data_model_documents` now neutralizes raw PDF/DOCX-extracted text before writing
  it to `interns/generated/data_dictionary/*.txt`, so every current and future consumer of
  that file is protected, not only the one reader this audit originally traced.
- Item 3 (dashboard chat context, `_chat_artifact_context`) was fixed earlier in the same
  session as an unrelated wiring-decision pass — `neutralize_text` now wraps each
  `label`/`interpreter` value.
- Item 1 (`image_parser.py` OCR text) was traced end-to-end and confirmed to never reach an
  LLM call (it feeds only deterministic regex/profile matching) — documented as an exemption
  in the module rather than patched with a no-op neutralize call.
- **Item 2 remains genuinely open**: `core/onboarding/sources/external_intake_workflow.py`'s
  external source metadata has NOT been re-audited as part of this pass. A separate,
  lower-confidence finding (not yet confirmed as a real sink) also flagged
  `core/onboarding/sources/catalog.py::_normalize_catalog_entry` for a similar reason — worth
  a follow-up pass specifically on the external-source-intake path.

### Production consequence

A hostile workspace owner (or a data supplier who controls dataset file naming, KPI workbook
prose, or data dictionary content) can inject instructions into the LLM prompt via the
now-fixed paths above. The remaining open path (external source intake) has the same
consequence and has not yet been verified either way.

### Open audit items to verify

1. `core/onboarding/sources/external_intake_workflow.py` — external metadata fields
   (source description, column descriptions from external catalog). Trace to any panel
   or LLM-facing surface.
2. `core/onboarding/sources/catalog.py::_normalize_catalog_entry` — embeds raw external-catalog
   `description`/`title` into human-review reports; confirm whether those reports are ever
   re-read into an LLM/agent prompt (not confirmed either way as of 2026-07-25).

---

## Prioritized Remediation Roadmap

| Gap | Priority   | Effort | Status                   | Owner Area        |
|-----|------------|--------|--------------------------|-------------------|
| 1   | CRITICAL   | M      | being-fixed-in-code      | Deployment / Ops  |
| 2   | CRITICAL/H | L      | infra-roadmap            | Platform / Infra  |
| 3   | HIGH       | M-L    | infra-roadmap            | Infra / SecEng    |
| 4   | HIGH       | S      | being-fixed-in-code      | CI / DevOps       |
| 5   | HIGH/MED   | L      | infra-roadmap            | Platform / Infra  |
| 6   | MED        | S      | being-fixed-in-code      | Core / SecEng     |
| 7   | MED        | S      | being-fixed-in-code      | Observability     |
| 8   | MED        | S      | substantially closed (2026-07-25) | Core / AI-Sec |

**Immediate actions (before any network-reachable deployment):**
- Gap 1: Enforce reverse proxy + TLS + auth env vars in the deployment runbook.
- Gap 3: Confirm no `.env` file on the production host; move to Vault or cloud secret
  manager before first external user accesses the dashboard.
- Gap 8: the blocker-panel/dashboard/data-dictionary paths are fixed; the one remaining
  open item is auditing `external_intake_workflow.py`.

**Not a security control — do not conflate with the above:** `core/onboarding/harness/
project_harness.py` and `core/onboarding/harness/workflow_guard_harness.py` (the "meta
harness" run before a workspace is called complete) check data-quality, evidence
completeness, and workflow reliability only. Confirmed empirically (2026-07-25): neither
has any awareness of injection, secrets, or destructive/unauthorized actions. A green
harness result says nothing about any of the 8 gaps in this document. The actual security-
relevant gates are `core/onboarding/databricks/deploy_gates.py` (G1-G5, production deploy
authorization) and the fixes tracked in this file.

---

## Executive Summary

This report covers eight production-readiness security gaps in the Autoresearch KPI
platform. Four gaps are actively being addressed in code (dashboard auth hardening,
CI security scanning, audit hash-chain, and log redaction); the code-level controls are
sound in design but require operational deployment configuration to be effective, and each
carries documented residual risks. Three gaps are infrastructure roadmap items that require
architectural investment beyond source-code changes: multi-tenant isolation and RBAC (the
platform currently operates as a single-trust domain with no per-workspace access controls),
secrets management and encryption at rest (all credentials live in process environment
variables; no disk encryption or key management infrastructure exists), and containerized
execution sandboxing for generated code (the `IsolatedDuckDBBackend` stub exists but is
unwired, and the worker subprocess inherits the full process environment and filesystem
scope). The eighth gap (AI-security / injection-guard coverage) is substantially closed as
of 2026-07-25 -- the blocker-panel, KPI-confirmation-panel, data-dictionary-extraction, and
dashboard-chat paths are all now neutralized at their actual render/write points (the
original finding undercounted the gap: `blocker_question_panel.py` was credited with
coverage it did not actually have beyond one narrow preview function). OCR text from
image_parser was traced and confirmed to never reach an LLM call. One item remains open:
external catalog/source-intake metadata has not yet been re-audited. Given the healthcare-RCM data context (HIPAA-18
identifiers, PCI cardholder fields, both detected and enforced by `phi_gate.py`), Gaps 1,
2, and 3 carry direct regulatory consequence and should be the first target of the
production hardening sprint.
