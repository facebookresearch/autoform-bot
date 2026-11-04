"""Run a JavaScript test body against a real page: the pinned jsdom under Node.

``run_dom`` hands ``tests/dom_fixture.js`` the page and the body, the text of an
async function of ``window``, ``document``, ``args``, ``errors``, ``require`` and
``load`` (which runs a production file whole in the page), and returns what
the body printed, followed by what it returned as JSON. The page's own errors
fail the run unless the body takes them out of ``errors``.

Run ``npm ci --ignore-scripts`` first. Without Node.js or that jsdom a test
skips, unless ``AUTOFORM_REQUIRE_NODE_TESTS`` is ``1``, as in CI's test job,
which fails it instead.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
_ROOT = Path(__file__).resolve().parents[1]
_ENTRY = Path(__file__).with_name("dom_fixture.js")


def check_jsdom(*, required: bool = False) -> None:
    """Skip, or fail when ``required`` or CI asks, unless Node and the jsdom package.json pins are installed."""

    unavailable = pytest.fail if required or os.environ.get("AUTOFORM_REQUIRE_NODE_TESTS") == "1" else pytest.skip
    if NODE is None:
        unavailable("Node.js is not on PATH")
    pinned = json.loads((_ROOT / "package.json").read_text(encoding="utf-8"))["devDependencies"]["jsdom"]
    installed = _ROOT / "node_modules/jsdom/package.json"
    if not installed.is_file():
        unavailable(f"jsdom {pinned} is not installed; run npm ci --ignore-scripts")
    # An install that differs from the lock is broken, not missing.
    if json.loads(installed.read_text(encoding="utf-8")).get("version") != pinned:
        pytest.fail(f"node_modules/jsdom is not the pinned {pinned}; run npm ci --ignore-scripts")


def run_dom(
    body: str,
    *,
    page: str = "<html><head></head><body></body></html>",
    url: str = "https://blueprint.example.invalid/",
    args: Mapping[str, object] | None = None,
    timeout: float = 60,
) -> str:
    """Run ``body`` on ``page`` at ``url`` and return its output."""

    check_jsdom()
    job = {"page": page, "url": url, "args": dict(args or {}), "body": body}
    done = subprocess.run(
        [NODE, str(_ENTRY)], input=json.dumps(job), capture_output=True, text=True, timeout=timeout, check=False
    )
    # The first error stderr reports is the one to read.
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[:6000]
    return done.stdout
