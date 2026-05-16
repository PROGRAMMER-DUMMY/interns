"""Per-workspace wiki layout: paths and folder conventions."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WikiLayout:
    """Resolves on-disk paths for a workspace's wiki.

    Entity notes live under ``<project_root>/wiki/{kpis,features,sources}/<id>.md``.
    The repo-level template ``wiki/_templates/note.md`` is shared across workspaces.
    """

    project_root: Path

    @property
    def wiki_root(self) -> Path:
        return self.project_root / "wiki"

    @property
    def features_dir(self) -> Path:
        return self.wiki_root / "features"

    @property
    def kpis_dir(self) -> Path:
        return self.wiki_root / "kpis"

    @property
    def sources_dir(self) -> Path:
        return self.wiki_root / "sources"

    def feature_note_path(self, feature_id: str) -> Path:
        return self.features_dir / f"{slugify(feature_id)}.md"

    def kpi_note_path(self, kpi_id: str) -> Path:
        return self.kpis_dir / f"{slugify(kpi_id)}.md"

    def source_note_path(self, source_id: str) -> Path:
        return self.sources_dir / f"{slugify(source_id)}.md"

    def ensure_dirs(self) -> None:
        for path in (self.features_dir, self.kpis_dir, self.sources_dir):
            path.mkdir(parents=True, exist_ok=True)


def slugify(value: str) -> str:
    """Filesystem-safe slug. Preserves case for readability; collapses non-word runs."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("_-")
    return cleaned or "unnamed"
