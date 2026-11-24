"""What the WebKit tests share: a quiet local server for a built site, and the browser each module opens once."""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

#: CI sets this, so a missing browser fails the job instead of skipping every case.
REQUIRED = os.environ.get("AUTOFORM_REQUIRE_BROWSER_TESTS") == "1"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def served(directory: Path) -> Iterator[str]:
    """Serve ``directory`` on a free local port, as a static host serves a published site."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def webkit() -> Iterator[object]:
    """Launch WebKit, or skip when it is not installed; where CI requires the browser, fail instead.

    A module keeps one browser: Playwright's event loop runs while it is open, and asyncio.run() in later test modules
    refuses to start under it.
    """
    import playwright.sync_api as playwright

    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.webkit.launch()
        except playwright.Error as error:
            if REQUIRED or "Executable doesn't exist" not in str(error):
                raise
            pytest.skip("WebKit is not installed; run `playwright install webkit`")
        try:
            yield browser
        finally:
            browser.close()
