"""CLI entrypoint for context routing."""

from core.context.router import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
