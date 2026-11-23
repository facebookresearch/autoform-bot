"""What the site's stylesheets draw, read as a browser reads them: computed styles in the pinned jsdom, and contrast.

jsdom cascades by source order alone, without specificity, applies no media query but "screen", and computes no
pseudo-element's style. A test here reads what one rule decides, or matches rules itself where jsdom does not; the
WebKit tests read the rest.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import re

from autoform_cli.ui import dag_viewer, site_files
from tests.dom_fixture import run_dom


def styled(markup: str, body: str, *, explorer: bool = False, args: Mapping[str, object] | None = None) -> object:
    """What ``body`` returns on a page holding ``markup`` under the site's stylesheet, followed by the explorer's
    when ``explorer``, as the published pages link them."""

    sheets = [site_files._stylesheet(), *([dag_viewer._style()] if explorer else [])]
    head = "".join(f"<style>{sheet}</style>" for sheet in sheets)
    return json.loads(run_dom(body, page=f"<!doctype html><html><head>{head}</head><body>{markup}</body></html>", args=args))


def contrast(one: str, other: str) -> float:
    """The WCAG contrast ratio of two colours, each ``#RRGGBB`` or a computed ``rgb(r, g, b)``."""

    lighter, darker = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _luminance(colour: str) -> float:
    if colour.startswith("#"):
        channels = [int(colour[index : index + 2], 16) for index in (1, 3, 5)]
    else:
        channels = [int(part) for part in re.fullmatch(r"rgba?\((\d+), (\d+), (\d+)(?:, [\d.]+)?\)", colour).groups()]
    red, green, blue = (
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in (c / 255 for c in channels)
    )
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue
