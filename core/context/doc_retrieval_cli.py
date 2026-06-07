"""CLI for bounded internal-doc retrieval.

    retrieve-docs --query "snowflake star schema join" [--top-k N] [--quiet]

``--quiet`` prints one line per hit (``path (score) — title``); the default
also prints each bounded excerpt. Read-only over ``docs/``.
"""
from __future__ import annotations

import argparse

from core.context.doc_retrieval import (
    DEFAULT_MAX_CHARS,
    DEFAULT_TOP_K,
    retrieve_docs,
)
from core.paths import PROJECT_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retrieve the most relevant INTERNAL doc sections for a query.",
    )
    parser.add_argument("--query", required=True, help="Topic/ambiguity text to retrieve docs for.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="Max docs to return.")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="Excerpt cap per hit.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(PROJECT_ROOT),
        help="Repository root. Defaults to the detected project root.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="One line per hit (path (score) — title); omit excerpts.",
    )
    args = parser.parse_args(argv)

    hits = retrieve_docs(
        args.query,
        args.repo_root,
        top_k=args.top_k,
        max_chars=args.max_chars,
    )
    if not hits:
        print(f"No internal docs matched: {args.query!r}")
        return 0

    for hit in hits:
        print(f"{hit['path']} ({hit['score']}) - {hit['title']}")
        if not args.quiet:
            print()
            print(hit["excerpt"])
            print()
            print("-" * 72)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
