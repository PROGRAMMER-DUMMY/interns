"""MCP (Model Context Protocol) server for MinusAnalyst.

Exposes the governed semantic model + dashboards over the standard MCP protocol
so any MCP client (Claude, etc.) can inspect and query the analytics project.
"""

from __future__ import annotations

from minus.mcp.server import build_server

__all__ = ["build_server"]
