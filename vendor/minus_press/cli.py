"""``minus-press`` CLI: export a generated MinusAnalyst project to a rich .pdf."""

from __future__ import annotations

import argparse
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="minus-press",
                                description="Rich PDF export of a MinusAnalyst dashboard.")
    p.add_argument("--root", default=".", help="Project root holding project.yaml.")
    p.add_argument("--out", default="MinusPress.pdf", help="Output .pdf path.")
    p.add_argument("--theme", default=None, help="Override theme (claude / dark).")
    args = p.parse_args(argv)

    from minus_press.press import build_pdf

    res = build_pdf(args.root, args.out, theme=args.theme)
    print(f"[ok] MinusPress -> {res['path']}")
    print(f"     {res['pages']} pages, {res['charts']} charts, {res['tables']} tables, "
          f"{res['kpis']} KPIs, theme {res['theme']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
