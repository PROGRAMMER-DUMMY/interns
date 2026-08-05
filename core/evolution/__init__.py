"""Schema evolution as a governed event.

Discovery measures the source; this package remembers what it measured last
time. A re-discovery that removes a column, drops a table or changes a type is
not a silent config change -- it is snapshotted, diffed, and (when it needs a
human) raised as a panel with the same envelope as every other blocker panel.

* [[core.evolution.snapshot]] -- rotate ``discovery.json`` into a history dir.
* [[core.evolution.drift]]    -- diff two snapshots into a ``DriftReport``.
* [[core.evolution.panel]]    -- render the panel, record the answer, emit the
  ``schema_exclusions.json`` contract the dbt generator honors.
* [[core.evolution.cli]]      -- ``prepare-drift-panel`` / ``apply-drift-answer``.
"""
