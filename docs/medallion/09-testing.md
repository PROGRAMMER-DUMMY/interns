# 09 — Testing Strategy

## The pyramid

| Layer | Where | Speed | Purpose |
|---|---|---|---|
| **Unit** | `tests/medallion/*.py` | <1s each | Dataclass round-trips, YAML emit, SQL lint rules, hash determinism, tier router math, parser correctness |
| **Integration** | `tests/medallion/integration/*.py` | 5–60s each | End-to-end against a small fixture workspace; one per phase |
| **Live (opt-in)** | `tests/medallion/live/*.py` (skipped by default) | minutes | Real Databricks, real WebSearch, real LLM |

Every PR runs unit + integration. Live tests run nightly or on `[live-tests]` PR tag.

## Fixture workspace

`tests/fixtures/medallion-workspace/` — a deliberately tiny workspace that exercises every important path:

```
tests/fixtures/medallion-workspace/
├── docs/
│   └── data_model.md                # minimal — 2 entities, 1 relationship
├── datasets/
│   ├── hospital_a/patients.csv      # 10 rows; columns: PatientID, DOB, Gender, SSN
│   ├── hospital_b/patients.csv      # 10 rows; same shape; some overlapping PatientIDs
│   └── claims/claims.csv            # 20 rows; columns: ClaimID, PatientID, Amount, ServiceDate
└── interns/                         # populated by tests via the existing onboarding pipeline
```

Why these contents:
- **Multi-source** (`hospital_a` + `hospital_b`): exercises composite-key default.
- **PII columns** (`PatientID`, `DOB`, `SSN`): exercises PII hashing in P3.
- **A derived column** (`age_at_service = ServiceDate - DOB` lives in `claims` joined to `patients`): exercises derived-feature lift in P1.
- **Small** (~40 rows total): every test runs in <10s on DuckDB.

The fixture is checked into the repo. Onboarding artifacts are NOT checked in — every integration test runs onboarding first as a setup step. (Trade-off: slightly slower; ensures we test the actual onboarding → design integration.)

## Unit test categories

### Round-trip tests (P0)

Every dataclass has a round-trip test:

```python
def test_manifest_round_trip():
    m1 = _make_manifest()  # constructed in code
    d = m1.to_dict()
    m2 = Manifest.from_dict(d)
    assert m2.to_dict() == d
```

Add for: `Manifest`, `BronzeTable`, `SilverTable`, `GoldTable`, `Budget`, `KpiRegeneration`, `StarSchema`, `FactTable`, `DimensionTable`, `Relationship`, `SilverContract`, `TableContract`, `DerivedColumn`, `Assertion`, `TypeCast`, `Lineage`, `LineageNode`, `LineageEdge`.

### YAML emit + parse (P0)

```python
def test_manifest_yaml_round_trip():
    m1 = _make_manifest_with_empty_lists_and_nulls()
    yaml_text = manifest_to_yaml(m1)
    parsed = yaml.safe_load(yaml_text)
    m2 = Manifest.from_dict(parsed)
    assert m1.workspace == m2.workspace
    # critical: empty lists stay lists, not None
    assert isinstance(parsed["layers"]["bronze"][0]["pii_columns"], list)
```

### Hash determinism (P0)

```python
def test_compute_inputs_hash_deterministic(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"a":1}')
    h1 = compute_inputs_hash([p])
    h2 = compute_inputs_hash([p])
    assert h1 == h2

def test_compute_inputs_hash_changes_on_edit(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"a":1}')
    h1 = compute_inputs_hash([p])
    p.write_text('{"a":2}')
    h2 = compute_inputs_hash([p])
    assert h1 != h2

def test_compute_inputs_hash_missing_file_is_ok(tmp_path):
    # missing files are skipped silently — workspaces may not have all inputs
    h = compute_inputs_hash([tmp_path / "doesntexist.json"])
    assert h.startswith("sha256:")
```

### SQL lint (P1)

```python
def test_lint_catches_cartesian_join():
    sql = "SELECT * FROM a, b"
    findings = lint_duckdb_sql(sql, file_label="test.sql")
    assert any(f.rule == "no_cartesian_join" for f in findings)

def test_lint_passes_on_clean_sql():
    sql = "SELECT a.x FROM a JOIN b ON a.id = b.id"
    findings = lint_duckdb_sql(sql, file_label="test.sql")
    assert not [f for f in findings if f.severity == "error"]
```

### PII hash (P3)

```python
def test_pii_hash_deterministic(monkeypatch):
    monkeypatch.setenv("AUTORESEARCH_WORKSPACE_SALT__TEST_WS", "abc123")
    h1 = pii_hash_value("patient_42", workspace="test-ws")
    h2 = pii_hash_value("patient_42", workspace="test-ws")
    assert h1 == h2

def test_pii_hash_workspace_isolated(monkeypatch):
    monkeypatch.setenv("AUTORESEARCH_WORKSPACE_SALT__WS_A", "salt_a")
    monkeypatch.setenv("AUTORESEARCH_WORKSPACE_SALT__WS_B", "salt_b")
    a = pii_hash_value("patient_42", workspace="ws-a")
    b = pii_hash_value("patient_42", workspace="ws-b")
    assert a != b

def test_pii_hash_null_coalesces():
    pii_hash_value(None, workspace="test-ws")  # must not raise
```

### Tier router (P4)

```python
@pytest.mark.parametrize("n,expected_tiers", [
    (1, {"heavy": 1, "medium": 1, "light": 1}),  # same model in all
    (2, {"heavy": 1, "medium": 1, "light": 1}),  # top, top, bottom
    (3, {"heavy": 1, "medium": 1, "light": 1}),  # one per
    (6, {"heavy": 2, "medium": 2, "light": 2}),  # tertile split
])
def test_assign_tiers(n, expected_tiers):
    ranking = [f"m{i}" for i in range(n)]
    assignment = assign_tiers(ranking)
    for tier, count in expected_tiers.items():
        assert len(assignment.by_tier[tier]) == count

def test_pick_model_respects_minimum():
    assignment = TierAssignment(by_tier={"light": ["gemma"], "medium": [], "heavy": []})
    with pytest.raises(InsufficientModelCapability):
        pick_model("star_schema_design", assignment)  # min=medium, none available
```

### Lineage parser (P5)

```python
def test_passthrough_edge():
    sql = "INSERT INTO silver.patient SELECT patient_id FROM bronze.patient__a"
    edges = extract_edges_from_sql(sql, target_table="silver.patient")
    assert any(e.transform_type == "passthrough" and e.from_columns == ["patient_id"] for e in edges)

def test_computed_edge():
    sql = "INSERT INTO silver.x SELECT date_diff('year', dob, sd) AS age FROM bronze.y"
    edges = extract_edges_from_sql(sql, target_table="silver.x")
    e = [e for e in edges if "age" in e.to_columns][0]
    assert e.transform_type.startswith("computed")
    assert "dob" in e.from_columns and "sd" in e.from_columns
```

## Integration test categories

### P0 e2e (design pass)

```python
def test_design_medallion_e2e_seed(tmp_path):
    """Copy fixture, run onboarding, run design-medallion --cheap, verify artifacts."""
    workspace = _stage_fixture_workspace(tmp_path)
    subprocess.run(["uv", "run", "onboard-workspace", "--workspace", str(workspace)], check=True)
    result = design_medallion(workspace=workspace, repo_root=tmp_path, intern=None, cheap=True)
    assert result.bronze_files
    assert result.silver_files
    assert (workspace / "interns/generated/medallion/manifest.yaml").exists()
    # verify validator passes
    v = WorkspaceArtifactValidator(repo_root=tmp_path, workspace=workspace.relative_to(tmp_path))
    r = v.run()
    assert r.ok, r.errors
```

### P0 negative tests

```python
def test_design_fails_when_onboarding_missing(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(MedallionExit) as exc:
        design_medallion(workspace=workspace, repo_root=tmp_path, intern=None, cheap=True)
    assert exc.value.code == "RUN_ONBOARD_FIRST"

def test_validator_catches_tampered_hash(staged_workspace):
    manifest = staged_workspace / "interns/generated/medallion/manifest.yaml"
    text = manifest.read_text()
    manifest.write_text(text.replace(re.search(r"sha256:[a-f0-9]+", text).group(), "sha256:" + "0" * 64))
    v = WorkspaceArtifactValidator(repo_root=staged_workspace.parent, workspace=staged_workspace.relative_to(staged_workspace.parent))
    r = v.run()
    assert any("inputs_hash" in e for e in r.errors)
```

### P1 e2e (build pass)

```python
def test_build_e2e_duckdb(staged_designed_workspace):
    """design done; now run build; verify Bronze + Silver tables populate; assertions pass."""
    result = build_medallion(workspace=staged_designed_workspace, repo_root=..., cfg=cfg, only_layer=None)
    assert result.run_state["per_table_status"]["silver.patient"]["status"] == "ok"
    assert not result.run_state["degraded_run"]

def test_silver_assertion_failure_routes(staged_designed_workspace):
    """Inject a null into a not-null column; verify SILVER_ASSERTION_FAILED."""
    _inject_null_into_silver_input(staged_designed_workspace)
    result = build_medallion(...)
    assert "SILVER_ASSERTION_FAILED" in result.run_state["retry_history"][0]["stage_code"]
```

### P3 e2e (PII)

```python
def test_silver_hashes_pii(staged_designed_workspace, monkeypatch):
    monkeypatch.setenv("AUTORESEARCH_WORKSPACE_SALT__TEST_FIXTURE", "salt_a")
    build_medallion(...)
    con = duckdb.connect(staged_designed_workspace / "interns/state/medallion/local.duckdb")
    rows = con.execute("SELECT patient_id FROM silver.patient LIMIT 1").fetchall()
    # hashed values are 64-char hex
    assert all(len(r[0]) == 64 for r in rows)
```

### P4 e2e (Gemma)

```python
@pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="needs Gemma access")
def test_gemma_4_design_e2e(staged_workspace):
    """Pin Gemma 4; run full design; verify valid output."""
    result = subprocess.run([
        "uv", "run", "design-medallion",
        "--workspace", str(staged_workspace),
        "--engine", "gemini-api",
        "--model", "gemma-4",
        "--json",
    ], capture_output=True, text=True)
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["unconfirmed_decision_count"] >= 0  # i.e., it produced a valid star schema
```

### P5 e2e (lineage trace)

```python
def test_lineage_trace_to_bronze(staged_designed_workspace):
    result = subprocess.run([
        "uv", "run", "medallion-lineage", "trace",
        "--workspace", str(staged_designed_workspace),
        "--column", "gold.fact_claim.claim_amount",
        "--json",
    ], capture_output=True, text=True)
    data = json.loads(result.stdout)
    assert any(s[0].startswith("bronze.") for s in data["sources"])
```

## What to mock vs. run

| Component | Mock or run? | Why |
|---|---|---|
| `LLMEngine.generate()` | Mock in most tests | Real LLM is non-deterministic + slow |
| Real LLM | Run only in live tests | Verifies the actual prompt format works |
| DuckDB | Run | Fast; deterministic |
| Spark | Mock the connector; run only in P2 integration | Slow startup |
| Databricks API | Mock by default; run live in `tests/medallion/live/` | Cost + auth |
| WebSearch | Mock in P4 unit tests; run live in `tests/medallion/live/test_classifier_live.py` | Network + non-determinism |
| MLflow | Use file-based tracker (`mlflow.set_tracking_uri("file:./mlruns")`) | No external service |
| Filesystem | Real (tmp_path) | Fast + deterministic |

## Test data discipline

- **No real PII in fixtures.** Patient names are clearly synthetic (`Patient_001`, `Patient_002`, etc.). Dates are random within 1900–2025. SSNs are clearly fake (`000-00-NNNN`).
- **Fixtures are reproducible.** The data files are checked in; they don't depend on a randomization step.
- **Fixtures are minimal.** A test should fail in a way that points at the bug, not at fixture complexity. If you can't tell from the fixture which rows triggered the failure, the fixture is too big.

## CI integration

`tests/medallion/` runs in standard CI. Live tests skip unless `RUN_LIVE_TESTS=1` is set. The CI config (when it exists) should:

1. Cache the uv environment.
2. Run `uv run pytest tests/medallion/` (unit + integration).
3. Surface test counts + timings.
4. On schedule (nightly), also run `RUN_LIVE_TESTS=1 uv run pytest tests/medallion/live/`.

## Coverage targets

| Module | Target |
|---|---|
| `core/medallion/*.py` | ≥ 90% line coverage |
| `interns/medallion_architect.py` | ≥ 80% (LLM path is mocked) |
| `core/orchestration/governor.py` (medallion routing) | 100% |
| `core/onboarding/workspace_artifact_validator.py` (medallion checks) | 100% |

Coverage is a guide, not a goal. A poorly-targeted 95% misses more bugs than a thoughtful 80%.
