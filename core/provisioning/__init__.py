"""Additive-only Unity Catalog provisioning + ingestion code generation.

Slice 3 of the cloud-first restructure (see
``docs/superpowers/specs/2026-08-05-cloud-first-restructure-design.md``):

* :mod:`core.provisioning.plan` turns a source declaration + discovery scan into
  an ordered, additive-only ``provision_plan.json``.
* :mod:`core.provisioning.apply` executes that plan through the Unity Catalog
  seam already built for UC intake, idempotently, and only after the ONE human
  blueprint confirmation exists.
* :mod:`core.provisioning.ingestion` emits (never runs) Databricks-native
  ingestion code per discovered table.

Additive-only is a hard invariant: no module here ever plans, emits, or executes
a statement that removes or rewrites an existing object.
"""
