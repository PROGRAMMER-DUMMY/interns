"""``minus-deck`` CLI: export a generated MinusAnalyst project to an interactive .pptx."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="minus-deck",
                                description="Interactive PPTX export of a MinusAnalyst dashboard.")
    p.add_argument("--root", default=".", help="Project root holding project.yaml.")
    p.add_argument("--out", default="MinusDeck.pptx", help="Output .pptx path.")
    p.add_argument("--theme", default=None, help="Override theme (claude / dark).")
    p.add_argument("--charts", choices=["native", "image"], default="native",
                   help="native = editable Excel-backed charts (default); "
                        "image = pixel-perfect dashboard images.")
    args = p.parse_args(argv)

    from minus_deck.deck import build_deck

    res = build_deck(args.root, args.out, theme=args.theme, charts=args.charts)
    print(f"[ok] MinusDeck -> {res['path']}")
    print(f"     {res['slides']} slides ({res['detail_slides']} detail), "
          f"{res['pages']} pages, theme {res['theme']}")
    print(f"     charts: {res['native_charts']} native (editable), "
          f"{res['image_charts']} image")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
