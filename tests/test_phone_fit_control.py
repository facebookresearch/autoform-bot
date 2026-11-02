"""Stable phone framing control labels, using the actual framing callbacks."""

from __future__ import annotations

import pytest

from tests.test_integrated_dag_layout import _NODE, _run
from tests.test_qa_scene_framing import _ENVIRONMENT


@pytest.mark.skipif(_NODE is None, reason="Node is needed for actual framing callback checks")
def test_actual_framing_labels_and_tooltips_track_fit_and_readable_crop_without_data_changes() -> None:
    data = {
        "nodes": [{"id": key, "kind": "node", "width": 196, "height": 100} for key in ("A", "B")],
        "edges": [{"source": "A", "target": "B", "statement": 1, "proof": 0}],
    }
    _run(_ENVIRONMENT + r"""
      const before=JSON.stringify(data);fit();
      assert.equal(dom$fit.textContent,'Fit');assert.equal(dom$fit['aria-label'],'Fit the map');
      assert.equal(dom$fit.title,dom$fit['aria-label']);
      dom$stage.clientHeight=100;fit();assert(ui$readableCrop);
      assert.equal(dom$fit.textContent,'Recentre');assert.equal(dom$fit['aria-label'],'Recentre the map');
      assert.equal(dom$fit.title,dom$fit['aria-label']);
      dom$stage.clientHeight=600;fit();assert.equal(ui$readableCrop,false);
      assert.equal(dom$fit.textContent,'Fit');assert.equal(dom$fit.title,'Fit the map');
      assert.equal(JSON.stringify(data),before);
    """, {"data": data, "width": 390, "height": 600, "direction": "lr", "atlas": False})
