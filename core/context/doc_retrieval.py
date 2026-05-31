"""Bounded retrieval over the platform's INTERNAL docs (``docs/**/*.md``).

An ambiguous model often does not know which internal doc explains a topic.
Rather than guess, it can call :func:`retrieve_docs` with the topic/ambiguity
string and pull the single most relevant *bounded* section into context.

Design mirrors :mod:`core.context.router` (manifest of derived pages + small
excerpts, never a full-file dump) and reuses the heading-bounded section
extractor approach from :mod:`tools.skill_excerpt` (``_find_section`` /
``_section_index``) so excerpts are tight markdown slices.

Pure, deterministic, workspace-agnostic, read-only over ``docs/``. No business
words hardcoded; keywords are derived from each doc's own headings plus the
``index.md`` "what it is" descriptions. Missing docs/indexes return ``[]``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

DEFAULT_TOP_K = 3
DEFAULT_MAX_CHARS = 1800

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# Common english/markdown stopwords + table noise so scoring keys on signal.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
        "is", "are", "be", "by", "as", "at", "it", "its", "this", "that",
        "what", "how", "use", "used", "uses", "using", "from", "into", "per",
        "not", "no", "see", "own", "every", "which", "when", "where", "who",
        "file", "files", "index", "doc", "docs", "md", "etc", "via", "vs",
    }
)


# --------------------------------------------------------------------------- #
# Tokenisation / heading helpers (heading logic adapted from skill_excerpt).
# --------------------------------------------------------------------------- #
def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(text).lower()) if t]


def _keywords(text: str) -> set[str]:
    return {t for t in _tokens(text) if len(t) > 2 and t not in _STOPWORDS}


def _section_index(body: str) -> list[tuple[int, int, str]]:
    """Return ``(level, line_index, heading_text)`` for every markdown heading."""
    out: list[tuple[int, int, str]] = []
    for idx, line in enumerate(body.split("\n")):
        match = _HEADING_RE.match(line)
        if match:
            out.append((len(match.group(1)), idx, match.group(2).strip()))
    return out


def _first_meaningful_line(text: str) -> str:
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        return line.lstrip("# ").strip()
    return ""


def _section_for_query(body: str, query_tokens: list[str], *, max_chars: int) -> str:
    """Return the most query-relevant heading-bounded section, capped.

    Picks the section whose heading + body share the most query keywords; ties
    break toward the earlier section (stable). Falls back to the document head
    when there is no overlap or no headings.
    """
    lines = body.split("\n")
    sections = _section_index(body)
    if not sections:
        return _truncate(body.strip(), max_chars)

    qset = set(query_tokens)

    def span(i: int) -> tuple[int, int]:
        start = sections[i][1]
        end = len(lines)
        for j in range(i + 1, len(sections)):
            if sections[j][0] <= sections[i][0]:
                end = sections[j][1]
                break
        return start, end

    best_i: int | None = None
    best_score = 0
    for i, (_, _, heading) in enumerate(sections):
        start, end = span(i)
        chunk = "\n".join(lines[start:end])
        # Heading matches weigh double so the title carries the section.
        score = 2 * len(qset & _keywords(heading)) + len(qset & _keywords(chunk))
        if score > best_score:
            best_score = score
            best_i = i

    if best_i is None:
        # No overlap: return the document's opening section as a stable default.
        start, end = span(0)
        return _truncate("\n".join(lines[start:end]).strip(), max_chars)
    start, end = span(best_i)
    return _truncate("\n".join(lines[start:end]).strip(), max_chars)


_TRUNCATION_SUFFIX = "\n...[truncated]"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNCATION_SUFFIX))
    return (text[:keep].rstrip() + _TRUNCATION_SUFFIX)[:limit]


# --------------------------------------------------------------------------- #
# Index-derived keyword enrichment.
# --------------------------------------------------------------------------- #
def _index_keywords_by_file(index_path: Path) -> dict[str, set[str]]:
    """Parse an ``index.md`` and map each referenced doc filename -> keywords.

    Handles both the markdown table form (``| file | what it is | ... |``) used
    by ``reference/index.md`` etc., and the bullet form (``- `path` — what``)
    used by ``docs/README.md``. Keys are bare filenames (e.g. ``foo.md``) so a
    doc can be matched regardless of how the index spells its relative path.
    """
    out: dict[str, set[str]] = {}
    try:
        text = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.split("\n"):
        line = raw.strip()
        # Filenames referenced anywhere on the line (backtick-quoted or bare).
        names = re.findall(r"`?([\w./ -]+?\.md)`?", line)
        if not names:
            continue
        # Description text = the line minus the code-spanned path tokens.
        desc = re.sub(r"`[^`]*`", " ", line)
        desc = re.sub(r"[|*#>-]", " ", desc)
        kws = _keywords(desc)
        for name in names:
            base = Path(name.strip()).name
            if not base or base == "index.md":
                continue
            out.setdefault(base, set()).update(kws)
    return out


def _all_index_keywords(docs_root: Path) -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    for index_path in sorted(docs_root.rglob("index.md")):
        for base, kws in _index_keywords_by_file(index_path).items():
            merged.setdefault(base, set()).update(kws)
    # docs/README.md acts as the top-level index too.
    readme = docs_root / "README.md"
    if readme.exists():
        for base, kws in _index_keywords_by_file(readme).items():
            merged.setdefault(base, set()).update(kws)
    return merged


# --------------------------------------------------------------------------- #
# Public API.
# --------------------------------------------------------------------------- #
def _docs_root(repo_root: str | Path) -> Path:
    return Path(repo_root).resolve() / "docs"


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def build_doc_manifest(repo_root: str | Path) -> list[dict[str, Any]]:
    """Scan ``docs/**/*.md`` and derive a deterministic manifest.

    Each entry: ``{path, title, summary, keywords}`` where ``keywords`` fuses
    the doc's section headers with index-derived "what it is" terms. Returns
    ``[]`` when ``docs/`` is missing. Sorted by path for stability.
    """
    docs_root = _docs_root(repo_root)
    if not docs_root.is_dir():
        return []

    index_kws = _all_index_keywords(docs_root)
    manifest: list[dict[str, Any]] = []
    for doc_path in sorted(docs_root.rglob("*.md")):
        if doc_path.name == "index.md":
            continue
        try:
            text = doc_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        headings = [h for _, _, h in _section_index(text)]
        title = headings[0] if headings else (_first_meaningful_line(text) or doc_path.stem)
        summary = _first_meaningful_line(text) or title

        keywords: set[str] = set()
        for heading in headings:
            keywords |= _keywords(heading)
        keywords |= _keywords(title)
        # Filename stem terms (e.g. "schema_types_identification_guide").
        keywords |= _keywords(doc_path.stem.replace("_", " ").replace("-", " "))
        keywords |= index_kws.get(doc_path.name, set())

        manifest.append(
            {
                "path": _rel(doc_path, repo_root),
                "title": title,
                "summary": summary,
                "keywords": sorted(keywords),
            }
        )
    return manifest


def retrieve_docs(
    query: str,
    repo_root: str | Path,
    *,
    top_k: int = DEFAULT_TOP_K,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[dict[str, Any]]:
    """Return up to ``top_k`` docs most relevant to ``query`` with bounded excerpts.

    Each hit: ``{path, title, score, excerpt}``. Scoring is keyword/title
    overlap between the query and each doc's derived keywords/title; ties break
    by path so results are deterministic. The ``excerpt`` is the most relevant
    heading-bounded section, capped at ``max_chars``. Empty/no-match queries and
    a missing ``docs/`` tree both return ``[]``.
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    qset = set(query_tokens)

    manifest = build_doc_manifest(repo_root)
    if not manifest:
        return []

    repo = Path(repo_root).resolve()
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for entry in manifest:
        kw = set(entry["keywords"])
        title_kw = _keywords(entry["title"])
        # Keyword overlap is the base; title hits weigh extra (1.5x via +1 each).
        score = len(qset & kw) + len(qset & title_kw)
        if score <= 0:
            continue
        scored.append((score, entry["path"], entry))

    if not scored:
        return []

    scored.sort(key=lambda item: (-item[0], item[1]))

    hits: list[dict[str, Any]] = []
    for score, path, entry in scored[: max(0, top_k)]:
        doc_path = repo / path
        try:
            body = doc_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        excerpt = _section_for_query(body, query_tokens, max_chars=max_chars)
        hits.append(
            {
                "path": path,
                "title": entry["title"],
                "score": score,
                "excerpt": excerpt,
            }
        )
    return hits


__all__ = ["build_doc_manifest", "retrieve_docs", "DEFAULT_TOP_K", "DEFAULT_MAX_CHARS"]
