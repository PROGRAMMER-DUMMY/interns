"""Governed PDF / document ingestion sub-package.

Phase 1: scan_document() -- Java preflight, opendataloader-pdf wrapper,
         review-gated sidecar, PHI redaction, graceful degradation.
Phase 2: classify_document() -- propose-only routing of structured PDF JSON
         to candidate types (KPI, lexicon, data-model, open-question).
"""
