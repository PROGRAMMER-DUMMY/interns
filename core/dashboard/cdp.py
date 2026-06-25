"""Minimal Chrome DevTools Protocol (CDP) driver over a headless Edge/Chrome.

The dashboard screener needs to *interact* with the live app -- switch sub-tabs,
read each chart's plotly trace data, and capture per-element crops -- which the
plain ``--screenshot`` flag cannot do. Rather than add a heavy Playwright /
Selenium dependency, this drives the already-present Edge/Chrome over CDP using
only ``websocket-client`` + ``requests`` (both already vendored).

Scope is deliberately small: launch, navigate, evaluate JS, click, and capture
(full page or a clip). Generic; no dashboard knowledge lives here.
"""
from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def browser_bin() -> Optional[str]:
    for cand in _EDGE_CANDIDATES + _CHROME_CANDIDATES:
        if Path(cand).is_file():
            return cand
    for name in ("msedge", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CDPError(RuntimeError):
    pass


class CDPBrowser:
    """A headless Edge/Chrome controlled over CDP. Use as a context manager."""

    def __init__(self, *, width: int = 1500, height: int = 2200, settle_ms: int = 3500):
        self.width = width
        self.height = height
        self.settle_ms = settle_ms
        self._proc: Optional[subprocess.Popen] = None
        self._ws = None
        self._id = 0
        self._user_dir: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self) -> "CDPBrowser":
        import requests  # local import: optional dependency surface

        binary = browser_bin()
        if not binary:
            raise CDPError("no headless browser found (Edge/Chrome)")
        port = _free_port()
        self._user_dir = tempfile.mkdtemp(prefix="cdp_")
        self._proc = subprocess.Popen(
            [binary, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             "--remote-allow-origins=*",
             f"--remote-debugging-port={port}", f"--user-data-dir={self._user_dir}",
             f"--window-size={self.width},{self.height}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ws_url = None
        for _ in range(100):  # up to ~10s for the debug endpoint
            try:
                targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=1).json()
                pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.1)
        if not ws_url:
            self.__exit__(None, None, None)
            raise CDPError("CDP debug endpoint never came up")
        import websocket  # websocket-client

        self._ws = websocket.create_connection(ws_url, max_size=None, timeout=30)
        self._cmd("Page.enable")
        self._cmd("Runtime.enable")
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass
        try:
            if self._proc is not None:
                self._proc.terminate()
                self._proc.wait(timeout=10)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        if self._user_dir:
            shutil.rmtree(self._user_dir, ignore_errors=True)

    # -- protocol -----------------------------------------------------------
    def _cmd(self, method: str, params: Optional[dict] = None, *, timeout: float = 30) -> dict:
        self._id += 1
        msg_id = self._id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self._ws.recv()
            if not raw:
                continue
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            # else: an event -> ignore
        raise CDPError(f"{method}: timed out")

    # -- high-level ---------------------------------------------------------
    def navigate(self, url: str) -> None:
        self._cmd("Page.navigate", {"url": url})
        # Wait for the document, then a fixed settle for Dash callbacks to paint.
        for _ in range(100):
            try:
                if self.evaluate("document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.1)
        time.sleep(self.settle_ms / 1000.0)

    def evaluate(self, expression: str) -> Any:
        res = self._cmd("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if res.get("exceptionDetails"):
            raise CDPError(f"evaluate: {res['exceptionDetails'].get('text')}")
        return res.get("result", {}).get("value")

    def wait(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def click_text(self, text: str, *, settle_ms: Optional[int] = None) -> bool:
        """Click the first tab/button-like element whose trimmed text equals
        ``text`` (used to switch dcc.Tabs sub-tabs). Returns True if clicked."""
        js_text = json.dumps(text)
        clicked = self.evaluate(
            "(function(){var sels=['.tab','[role=tab]','.dash-tab','.dcc-tab',"
            "'button','a','li'];for(var i=0;i<sels.length;i++){var els="
            "document.querySelectorAll(sels[i]);for(var j=0;j<els.length;j++){"
            f"if(els[j].innerText.trim()==={js_text}){{els[j].click();return true;}}"
            "}}return false;})()"
        )
        if clicked:
            self.wait(settle_ms if settle_ms is not None else self.settle_ms)
        return bool(clicked)

    def screenshot(self, out_png: Path, *, clip: Optional[dict] = None,
                   full_page: bool = True) -> bool:
        params: dict[str, Any] = {"format": "png", "captureBeyondViewport": True}
        if clip:
            params["clip"] = {**clip, "scale": clip.get("scale", 1)}
        elif full_page:
            size = self.evaluate(
                "({w: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth),"
                " h: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)})"
            ) or {}
            w = int(size.get("w") or self.width)
            h = int(size.get("h") or self.height)
            params["clip"] = {"x": 0, "y": 0, "width": w, "height": h, "scale": 1}
        data = self._cmd("Page.captureScreenshot", params).get("data")
        if not data:
            return False
        out_png.write_bytes(base64.b64decode(data))
        return True


__all__ = ["CDPBrowser", "CDPError", "browser_bin"]
