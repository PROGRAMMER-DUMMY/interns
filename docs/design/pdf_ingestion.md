# Design: Governed PDF / Document Ingestion (opendataloader-pdf)

Status: Design (not yet implemented)
Author: platform
Library: https://github.com/opendataloader-project/opendataloader-pdf (Apache-2.0)
Related: `core/onboarding/workspace/onboarding.py` (input inventory), `core/onboarding/data_model/image_parser.py`
(review-gated sidecar pattern), `core/onboarding/relationships/contracts.py` (diagram→join proof),
`core/onboarding/lexicon/builder.py`, `tools/methodology_parser.py` (stub this would back),
`docs/design/kpi_intent_contract.md`.

---

## 1. Problem / opportunity

Onboarding ingests CSV (datasets), XLSX (KPI registry via openpyxl), and PNG (data model via local
Tesseract OCR). **PDFs and documents have no structured path** — yet KPI spec sheets, data
dictionaries, methodology/contract docs, and ERD exports frequently arrive as PDF. `TOOLS.md` already
advertises a `tools/methodology_parser.py --doc <path> --out schema.json` for "a data dictionary or
contract document needs semantic schema extraction" — currently unbacked.

`opendataloader-pdf` extracts structured content from PDFs (reading order via XY-Cut++, tables,
heading hierarchy, lists, images, formulas) and emits **JSON with bounding boxes + semantic types**,
plus Markdown/HTML/text. It is **fully local/offline** ("documents never leave your environment, no
API calls") — which matches this platform's hard no-upload / PHI-safety rule.

## 2. Core principle

**Extracted content is evidence, not authority.** Mirror the data-model-image flow exactly: extract
→ write a **review-gated sidecar** marked *authoritative usage allowed: False* → downstream consumers
may *propose* candidates, but nothing auto-promotes without the existing proof/human gates. This
reuses the "advisory != enforced" discipline rather than trusting a parser.

## 3. Library facts that drive the design

- **Install / invoke**: `pip install -U opendataloader-pdf`; Python `opendataloader_pdf.convert(input_path=[...], output_dir=..., format="json,markdown")`; CLI `opendataloader-pdf <files...>`.
- **Runtime**: requires **Java 11+** (spawns a JVM per batch); Python 3.10+ bindings. No GPU.
- **PDF only** today (DOCX/XLSX/PPTX are roadmap via "Hancom Data Loader"). Does NOT replace the
  XLSX (openpyxl) or PNG (Tesseract) paths — it is **additive**.
- **Free mode** = deterministic (XY-Cut++), simple-border tables, fully local. **Hybrid mode** = an
  AI backend (borderless tables @0.928, formula LaTeX, image captions) — local but **non-deterministic**.

## 4. The governed tool (the seam)

`uv run scan-document --workspace <ws> --input <path.pdf> [--mode free|hybrid]`

- **Free mode is the default and the only local-safe mode.** Hybrid is gated behind an explicit
  decision artifact (same shape as remote-execution approval / `AUTORESEARCH_ALLOW_REMOTE_EXECUTION`)
  — never in the local-safe default path, to keep onboarding reproducible.
- **Java preflight**: detect Java 11+ at start; if missing/incompatible, **degrade gracefully** —
  emit a clear blocker and skip PDF ingestion; never crash onboarding (same pattern as the parity
  gate env-skipping Spark). NOTE: the box runs Java 24; "11+" is nominally satisfied but needs a
  one-time smoke test against the bundled engine.

## 5. Artifacts (mirror `data_model_images/`)

- `interns/generated/documents/<doc>.doc.json` — the opendataloader JSON (semantic types + bboxes +
  reading order), plus recorded `engine_version`, `mode`, `source_sha256` so the **same PDF → the same
  sidecar** (the determinism guarantee applied to workbooks via `worksheets[0]`).
- `interns/reports/documents/current.md` — review panel, default `authoritative usage allowed: False`.
- **PHI redaction**: all extracted text/JSON passes through the platform's redactor **before** it
  lands in `interns/` (clinical PDFs carry PHI; this is the platform's responsibility, not the
  parser's).

## 6. Content classifier → routing (propose-only)

A deterministic classifier reads the structured JSON (headings, tables, lists) and routes to
**candidate** targets, each tagged with provenance `{ source: document_pdf, page, bbox }`:

| Detected shape | Routed as (candidate, review-gated) | Existing consumer |
| --- | --- | --- |
| Table `KPI \| metric \| cuts`-like | KPI registry candidates | `kpi_registry.json` (proposed) |
| Definition table `term \| definition` | data-dictionary / lexicon candidates | `workspace_lexicon.json` |
| ERD boxes + FK text | data-model sidecar | `build-relationship-contracts` — **still requires profile RI proof** (reuse BUG-004/023 + the low-cardinality confidence cap) |
| Prose rules / SLAs | open-questions / semantic-contract candidates | `open_questions.md`, `semantic_contract.json` |
| anything else | raw evidence only | (no auto-route) |

One extractor + one classifier + governed routing serves all four PDF use cases (KPI specs,
dictionaries, ERD docs, methodology/contract docs) without four bespoke parsers. This also backs the
stubbed `tools/methodology_parser.py --doc`.

This classifier is the same "document type + action signal" idea from the intent-discovery skill;
the skill's *intent-facet* half lives in `docs/design/kpi_intent_contract.md`.

## 7. Provenance & gate fit

Every PDF-derived candidate enters as `source: document_pdf` evidence, so the existing
**gate-provenance** and **intent-coverage** harnesses treat it like any other agent-asserted evidence
(needs human/profile confirmation before it is executable). Diagram-derived joins extracted from a
PDF ERD are **still** subject to profile RI proof + the low-cardinality confidence cap — a PDF does
not make a join executable.

## 8. Determinism

Free mode (XY-Cut++) is deterministic. Record `mode` + `engine_version` + `source_sha256` in the
sidecar; same PDF + free mode → identical sidecar. Hybrid mode is non-deterministic and therefore
excluded from the default/reproducible path.

## 9. Phasing (each phase independently shippable + green-gated)

1. **Wrapper + sidecar + Java preflight** — `scan-document` (free mode), deterministic sidecar,
   graceful Java-absent degradation, PHI redaction on extracted content.
2. **Classifier + propose-only routing** — structured JSON → candidate artifacts with provenance;
   nothing auto-promotes.
3. **Onboarding wiring** — `input_inventory.json` learns `.pdf`; `onboard-workspace` runs
   `scan-document` per PDF (free mode); downstream extractors consume **sidecars, never raw PDF**.
4. **Optional hybrid mode** — behind an explicit approval decision (borderless tables / formulas);
   never in local-safe default.

## 10. Risks

- **Java 24 vs bundled engine** — verify in Phase 1; JVM startup adds latency on small docs (the
  60 pp/s figure won't apply to a one-page spec).
- **Free-mode table accuracy** — borderless/complex tables need hybrid (0.928). In free mode, route
  those as **low-confidence, review-gated** rather than silently dropping or trusting them.
- **Hybrid non-determinism** — kept out of the default path.
- **PHI** — redaction must run before persistence (tested).

## 11. Tests (phase-gated)

- Java-absent degradation: missing/incompatible JVM → clear blocker, onboarding continues.
- Deterministic sidecar: same PDF (free mode) → identical JSON hash.
- PHI redaction applied to extracted text/JSON before it is written under `interns/`.
- Classifier routing: each detected shape routes to the correct candidate target.
- Non-authoritative-by-default: a PDF-derived ERD join is still blocked until profile RI proof.

## 12. Non-goals

- Not replacing the XLSX (openpyxl) or PNG (Tesseract) ingestion paths — additive only.
- Not enabling hybrid AI mode by default.
- Not auto-promoting any PDF-derived candidate without the existing proof/human gates.
