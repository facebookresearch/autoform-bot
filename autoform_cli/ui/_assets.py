"""How render and mathjax read their packaged files in ``assets/``: when a build asks for one, so importing the
renderer, as check does, reads none.

A file holding a value only Python knows, such as the status colours in ``blueprint.css`` or the MathJax settings in
``mathjax.js``, holds a marker in its place: an uppercase name, such as ``SETTINGS_JSON``, found nowhere else in it.
"""

from __future__ import annotations

from pathlib import Path

from ..graph import PublicationError

_ASSET_DIR = Path(__file__).resolve().parent / "assets"


def _static_asset(name: str) -> str:
    try:
        return (_ASSET_DIR / name).read_text(encoding="utf-8")
    except OSError as error:
        raise PublicationError([f"packaged render asset is unavailable: {name}"]) from error


def _filled_asset(name: str, /, **values: str) -> str:
    """The packaged asset ``name`` with each marker in ``values`` replaced by its value. A marker the file holds other
    than once, counting copies a value filled in before it brought, stops the build, so none reaches the site."""

    text = _static_asset(name)
    for marker, value in values.items():
        if (count := text.count(marker)) != 1:
            raise PublicationError([f"packaged render asset {name} holds the marker {marker} {count} times, not once"])
        text = text.replace(marker, value)
    return text
