"""Neutral inventory colours must stay distinguishable and readable in both schemes."""

from __future__ import annotations

import json
from pathlib import Path

from autoform_cli import dag_viewer, status
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


def _pending() -> status.State:
    return next(state for state in status.INVENTORY_STATES if state.key == "inventory_pending")


def test_inventory_ring_greys_remain_distinguishable_in_both_schemes() -> None:
    planned = next(state for state in status.STATES if state.key == "planned")
    greys = (planned, *status.INVENTORY_STATES)

    for index, first in enumerate(greys):
        for second in greys[index + 1:]:
            assert _contrast(first.stroke, second.stroke) >= 1.5
            assert _contrast(first.dark_stroke, second.dark_stroke) >= 1.5


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
