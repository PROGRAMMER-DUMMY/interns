"""Phase 0/1 of the cloud-first spine: declare a source, measure it, then ask
only what measurement could not answer.

* [[core.intake.declaration]] -- the source declaration (connector type,
  location, credential *reference*) persisted in ``workspace_settings.json``.
* [[core.intake.discovery]] -- read-only per-connector scanners emitting
  ``interns/generated/intake/discovery.json``. Never fabricates a size.
* [[core.intake.interview]] -- the merged intake question set, its panel, and
  the answer store at ``interns/generated/intake/intake_answers.json``.
* [[core.intake.cli]] -- the four governed CLI entry points.
"""
