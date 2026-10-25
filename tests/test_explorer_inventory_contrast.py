"""Neutral inventory cards must remain readable on the explorer's real surfaces."""

from __future__ import annotations

import json
import re
from pathlib import Path

from autoform_cli import dag_viewer, render, status
from autoform_cli.graph_views import GraphView, ViewNode


def _rgb(colour: str) -> tuple[float, ...]:
    value = colour.lstrip("#")
    if len(value) == 3:
        value = "".join(channel * 2 for channel in value)
    return tuple(int(value[index:index + 2], 16) / 255 for index in (0, 2, 4))


def _luminance(colour: str | tuple[float, ...]) -> float:
    channels = _rgb(colour) if isinstance(colour, str) else colour
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def _contrast(first: str | tuple[float, ...], second: str | tuple[float, ...]) -> float:
    low, high = sorted((_luminance(first), _luminance(second)))
    return (high + 0.05) / (low + 0.05)


def _light_map_colours() -> tuple[str, str]:
    """Resolve the shipped light map variable both with and without site CSS."""
    style = dag_viewer._STYLE
    viewer = re.search(r"\.bp-dag-viewer\s*\{([^}]+)\}", style, re.S)
    assert viewer is not None
    variable = re.search(r"--dag-map:\s*var\((--[\w-]+),\s*(#[0-9a-f]{3,6})\)", viewer.group(1), re.I)
    assert variable is not None
    root = re.search(r":root\s*\{([^}]+)\}", render._stylesheet(), re.S)
    assert root is not None
    published = re.search(re.escape(variable.group(1)) + r":\s*(#[0-9a-f]{3,6})\s*;", root.group(1), re.I)
    assert published is not None
    main = re.search(r"\.bp-dag-main\s*\{([^}]+)\}", style, re.S)
    node = re.search(r"\.bp-dag-node\s*\{([^}]+)\}", style, re.S)
    assert main is not None and "background: var(--dag-map)" in main.group(1)
    assert node is not None and "background: var(--dag-node-fill)" in node.group(1)
    assert "var(--dag-node-stroke)" in node.group(1)
    return published.group(1), variable.group(2)


def _pending() -> status.State:
    return next(state for state in status.INVENTORY_STATES if state.key == "inventory_pending")


def test_pending_inventory_card_is_visible_on_published_and_standalone_light_maps() -> None:
    pending = _pending()

    for background in _light_map_colours():
        assert _contrast(pending.stroke, background) >= 3
        assert _contrast(pending.fill, background) >= 1.5
    for other in (*status.INVENTORY_STATES, next(state for state in status.STATES if state.key == "planned")):
        if other.key != pending.key:
            assert _contrast(pending.fill, other.fill) >= 1.5


def test_inventory_ring_greys_remain_distinguishable_in_both_schemes() -> None:
    planned = next(state for state in status.STATES if state.key == "planned")
    greys = (planned, *status.INVENTORY_STATES)

    for index, first in enumerate(greys):
        for second in greys[index + 1:]:
            assert _contrast(first.stroke, second.stroke) >= 1.5
            assert _contrast(first.dark_stroke, second.dark_stroke) >= 1.5


def test_pending_inventory_label_and_actual_muted_metadata_are_readable() -> None:
    pending = _pending()
    mix = re.search(
        r"color-mix\(in srgb,\s*var\(--dag-node-text\)\s*([\d.]+)%,\s*var\(--dag-node-fill\)\)",
        dag_viewer._STYLE,
    )
    assert mix is not None
    fraction = float(mix.group(1)) / 100
    muted = tuple(text * fraction + fill * (1 - fraction) for text, fill in zip(_rgb(pending.text), _rgb(pending.fill)))

    assert _contrast(pending.text, pending.fill) >= 4.5
    assert _contrast(muted, pending.fill) >= 4.5


def test_emitted_inventory_palette_retains_neutral_semantics_and_dark_colours(tmp_path: Path) -> None:
    pending = _pending()
    node = ViewNode(
        "module", "Module inventory", "node", ("module",), ((pending.key, 1),),
        catalog="module", status_key="planned",
    )
    path = tmp_path / "inventory.json"
    dag_viewer.write_payload(path, GraphView(kind="full", title="Inventory", nodes=(node,), edges=()), links={})
    payload = json.loads(path.read_text(encoding="utf-8"))
    palette = payload["palette"][pending.key]

    assert payload["nodes"][0]["status"] == pending.key
    assert palette["neutral"] is True
    assert palette["light"] == {"fill": pending.fill, "stroke": pending.stroke, "text": pending.text}
    assert palette["dark"] == {"fill": pending.dark_fill, "stroke": pending.dark_stroke, "text": pending.dark_text}
    assert _contrast(pending.dark_text, pending.dark_fill) >= 4.5
    assert _contrast(pending.dark_stroke, pending.dark_fill) >= 3
