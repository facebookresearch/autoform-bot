"""Native large-scope journeys over an independent authored mathematical book.

The source declares elementary zero-identity targets with explicitly unfinished
proofs. This is a publication/viewer fixture, not a Lean-kernel verification.
Its 81 local items, eight authored areas, structural unassigned items, and
outside dependencies come from Markdown and the real publisher, never mocked
graph JSON or a replacement renderer.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import pytest

from autoform_cli.mathjax import MATHJAX_BUNDLE
from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_integrated_workspace_browser import (
    _PinnedRuntime,
    _WorkspaceSite,
    _fit_graph,
    _focus_graph_node,
    _frames,
    _math_source,
    _open,
    _peek_reader,
    _pixels,
    _search,
    _served,
    _write,
    playwright,
    workspace_browser as workspace_browser,
    workspace_page as workspace_page,
    workspace_runtime as workspace_runtime,
)

_SCOPE = "local"
_SCOPE_TITLE = "Workshop of elementary identities"
_AREAS = (
    "Affine structures", "Metric structures", "Incidence structures", "Convex structures",
    "Discrete structures", "Geometric transformations", "Projective structures", "Algebraic structures",
)
_TARGET = "local/identity-72"
_TARGET_LEAN = "ScopedFixture.zero_72"
_TEX = (r"0=0", r"n\in\mathbb{N}", r"\frac{0}{2}=0", r"\sum_{k=1}^{n}0=0")
_SCOPE_TEX = (r"0=0", r"\frac{0}{2}=0", r"\sum_{j=1}^{n}0=0")
_HEADING = "retained-identity-notes"
_END = "The complete statement ends with this retained mathematical explanation."
_SCOPE_END = "This final workshop exposition is retained before the complete identity notes."
_SOURCE_END = "The elementary identity source remains available in full."
_VIEWPORTS = [(1600, 900), (1000, 900), (390, 844)]


def _item(number: int) -> str:
    return f"local/topic-{number:02d}/identity" if number < 72 else f"local/identity-{number:02d}"


def _title(number: int) -> str:
    return f"Authored topic {number:02d}" if number < 72 else f"Independent identity {number:02d}"


def _model() -> list[tuple[str, str, str]]:
    # Twenty-six connected items and 55 independent ones form 56 weak
    # components, rather than an artificial single tall rank-zero column.
    # All dependencies point backwards in this authored topological order.
    order = [*range(25), 72]
    pairs = [(order[i], order[i - lag]) for lag in range(1, len(order)) for i in range(lag, len(order))]
    edges = [(_item(a), _item(b), "statement") for a, b in pairs[:60]]
    # A proof-only prerequisite is not also a statement prerequisite. The
    # publisher correctly retains the statement kind for an overlapping pair.
    edges += [(_item(a), _item(b), "proof") for a, b in pairs[60:116]]
    return edges + [
        (_item(0), "inputs/base-statement", "statement"),
        (_item(1), "inputs/base-proof", "proof"),
        ("applications/using-last", _item(80), "proof"),
    ]


def _link(source: str, target: str) -> str:
    return posixpath.relpath("roadmap/" + target + ".md", posixpath.dirname("roadmap/" + source + ".md"))


def _statement(number: int) -> str:
    return (
        f"The target identity ${_TEX[0]}$ is stated explicitly.\n\n"
        + "\n\n".join(
            f"Explanation {i + 1} for identity {number:02d}. Zero is the additive neutral element; "
            "the entire elementary explanation belongs to this mathematical page, not its bounded graph preview."
            for i in range(10)
        )
        + f"\n\nThe natural-number parameter satisfies ${_TEX[1]}$. The zero fraction is ${_TEX[2]}$.\n\n"
        + "$$\n" + _TEX[-1] + "\n$$\n\n" + _END + "\n"
    )


def _target_markdown(node_id: str, number: int, edges: list[tuple[str, str, str]]) -> str:
    sections = []
    for kind, heading in (("statement", "Depends on"), ("proof", "Proof depends on")):
        dependencies = [target for source, target, edge_kind in edges if source == node_id and edge_kind == kind]
        if dependencies or kind == "statement":
            links = "\n".join(f"- [Authored input]({_link(node_id, target)})" for target in dependencies)
            sections.append(f"\n## {heading}\n\n" + (links or "No prerequisites.") + "\n")
    source_link = posixpath.relpath("sources/identity-notes.md", posixpath.dirname("roadmap/" + node_id + ".md"))
    return (
        f"---\ndeclaration: theorem\nnot_ready: true\nlean: ScopedFixture.zero_{number:02d}\n---\n\n"
        f"# Independent identity {number:02d}\n\n" + _statement(number) + "\n" + "\n".join(sections)
        + f"\n## Sources\n\n- [Elementary identity source]({source_link}#elementary)\n"
    )


@dataclass(frozen=True)
class _ScopedConsumer:
    site: _WorkspaceSite
    graph: dict
    groups: dict[str, list[dict]]
    totals: dict[str, dict[str, int]]


@pytest.fixture(scope="module")
def scoped_consumer(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_ScopedConsumer]:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("native-scoped-overview") / "project"
    scaffold_project(project, title="Independent scoped mathematical consumer")
    blueprint, edges = project / "blueprint", _model()
    _write(blueprint / "README.md", "# Elementary workshop\n\n- [Mathematical workshop](roadmap/README.md)\n")
    _write(blueprint / "roadmap/README.md", (
        "# Elementary workshop\n\n"
        "- [Local workshop](local/README.md)\n- [Auxiliary inputs](inputs/README.md)\n"
        "- [Applications](applications/README.md)\n- [Unrelated library](remote/README.md)\n"
    ))
    links = [f"- [{_title(n)}](topic-{n:02d}/README.md)" for n in range(72)]
    links += [f"- [{_title(n)}](identity-{n:02d}.md)" for n in range(72, 81)]
    _write(blueprint / "roadmap/local/README.md", (
        f"---\nnavigation: module\n---\n\n# {_SCOPE_TITLE}\n\n"
        "Areas below are explicit authored metadata. The remaining identities have no assigned mathematical area.\n\n"
        "The source preserves each complete statement and its declared educational dependency relationships.\n\n"
        + "\n\n".join(
            f"Workshop exposition {n + 1}. The complete mathematical scope is a reading page in its own right. "
            "Its explanatory prose remains available even when the default graph uses derived area groups."
            for n in range(8)
        )
        + "\n\n" + _SCOPE_END
        + "\n\n## Retained identity notes\n\nScope identity statement. The elementary equations "
        + ", ".join(f"${tex}$" for tex in _SCOPE_TEX)
        + " are retained with their complete mathematical explanation.\n\n"
        "## Authored contents\n\n" + "\n".join(links) + "\n"
    ))
    for n in range(81):
        if n < 72:
            _write(blueprint / f"roadmap/local/topic-{n:02d}/README.md", (
                f"---\nnavigation: module\narea: {_AREAS[n // 9]}\n---\n\n# {_title(n)}\n\n"
                "The title and area are authored mathematical metadata.\n\n- [Complete identity](identity.md)\n"
            ))
        _write(blueprint / ("roadmap/" + _item(n) + ".md"), _target_markdown(_item(n), n, edges))
    _write(blueprint / "roadmap/inputs/README.md", (
        "---\nnavigation: module\narea: Auxiliary mathematics\n---\n\n# Auxiliary inputs\n\n"
        "- [Statement input](base-statement.md)\n- [Proof input](base-proof.md)\n"
    ))
    for name in ("base-statement", "base-proof"):
        _write(blueprint / f"roadmap/inputs/{name}.md", (
            "---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Auxiliary zero identity\n\n"
            "The proposed elementary input is $0=0$; no completed proof is asserted.\n\n## Depends on\n\nNo prerequisites.\n"
        ))
    _write(blueprint / "roadmap/applications/README.md", (
        "---\nnavigation: module\narea: Auxiliary applications\n---\n\n# Applications\n\n"
        "- [Application of the last identity](using-last.md)\n"
    ))
    _write(blueprint / "roadmap/applications/using-last.md", (
        "---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Application of a local identity\n\n"
        "The proposed application uses the last local zero identity.\n\n## Depends on\n\nNo prerequisites.\n\n"
        "## Proof depends on\n\n- [Last local identity](../local/identity-80.md)\n"
    ))
    _write(blueprint / "roadmap/remote/README.md", (
        "---\nnavigation: module\narea: Unrelated mathematical library\n---\n\n# Unrelated library\n\n"
        + "\n".join(f"- [Unrelated note {n}](note-{n:02d}.md)" for n in range(24)) + "\n"
    ))
    for n in range(24):
        body = f"# Unrelated note {n}\n\nNo local dependency is asserted.\n"
        if n == 0:
            body += (
                "\n[Read the complete workshop](../local/README.md).\n\n"
                f"[Read the retained identity notes](../local/README.md#{_HEADING}).\n"
            )
        _write(blueprint / f"roadmap/remote/note-{n:02d}.md", body)
    _write(blueprint / "sources/identity-notes.md", "# Elementary source\n\n## Elementary\n\n" + _SOURCE_END + "\n")
    _write(blueprint / "coverage/README.md", (
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n"
        "| Educational identities | DECOMPOSED | [Local workshop](../roadmap/local/README.md) |\n"
    ))
    _write(project / "ScopedFixture.lean", (
        "namespace ScopedFixture\n"
        + "\n".join(f"theorem zero_{n:02d} : (0 : Nat) = 0 := by sorry" for n in range(81))
        + "\nend ScopedFixture\n"
    ))
    rendered = render_site(blueprint, project / "site-src", lean_root=project)
    # The same exact-name gate as the CLI's --require-declarations, not a
    # nonexistent Python API flag or a claim of Lean-kernel verification.
    assert not rendered.unresolved and not rendered.unchecked
    config = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    config["extra_javascript"] = [entry for entry in config["extra_javascript"]
                                  if getattr(entry, "path", str(entry)) != MATHJAX_BUNDLE]
    build(config)
    index_path = project / "site/dependencies/index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    records = {node["id"]: node for node in index["nodes"]}
    graph_path = index_path.parent / unquote(urlsplit(records[_SCOPE]["data_url"]).path)
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    local = [node for node in graph["nodes"] if node["kind"] != "boundary"]
    assert len(local) == 81 and {node["title"] for node in local} == {_title(n) for n in range(81)}
    assert all(node["member_count"] == 1 for node in local)
    assert not any("Unrelated note" in node["title"] for node in graph["nodes"])
    groups: dict[str, list[dict]] = {}
    owners = {}
    for node in local:
        key = "area:" + node["area"] if node.get("area") else "unassigned"
        groups.setdefault(key, []).append(node)
        owners[node["id"]] = key
    assert set(groups) == {*("area:" + area for area in _AREAS), "unassigned"}
    assert all(len(members) == 9 for members in groups.values())
    # Identify represented records through the actual published permalinks,
    # rather than manufacture a scope:/boundary: namespace from fixture paths.
    routes = {}
    for identifier, record in records.items():
        route = urljoin(index_path.as_uri(), record["route"])
        assert route not in routes, (identifier, routes.get(route))
        routes[route] = identifier
    represented = {}
    for node in graph["nodes"]:
        route = urljoin(graph_path.as_uri(), node["url"])
        assert route in routes, (node["id"], node["url"])
        original = routes[route]
        assert original not in represented, original
        represented[original] = node["id"]

    def endpoint(identifier: str) -> str:
        seen = set()
        while identifier not in represented:
            assert identifier not in seen and identifier in records, identifier
            seen.add(identifier)
            parent = records[identifier]["parent"]
            identifier = index["nodes"][parent]["id"] if type(parent) is int else parent
        return represented[identifier]

    # The authoring tuples are dependent -> prerequisite; published flow is
    # prerequisite -> dependent, so upstream/downstream is checked explicitly.
    expected_edges = Counter((endpoint(target), endpoint(source), kind) for source, target, kind in edges)
    actual_edges = Counter()
    neighbors = {node["id"]: set() for node in local}
    for edge in graph["edges"]:
        for kind in ("statement", "proof"):
            assert type(edge[kind]) is int and edge[kind] >= 0
            actual_edges[(edge["source"], edge["target"], kind)] += edge[kind]
        if edge["source"] in neighbors and edge["target"] in neighbors:
            neighbors[edge["source"]].add(edge["target"])
            neighbors[edge["target"]].add(edge["source"])
    assert actual_edges == expected_edges
    unseen, components = set(neighbors), []
    while unseen:
        pending, size = [min(unseen)], 0
        while pending:
            node = pending.pop()
            if node in unseen:
                unseen.remove(node)
                size += 1
                pending.extend(neighbors[node] & unseen)
        components.append(size)
    assert sorted(components) == [1] * 55 + [26]
    assert len(components) == 56
    totals = {part: {"statement": 0, "proof": 0} for part in ("internal", "cross_group", "outside")}
    for edge in graph["edges"]:
        part = "outside" if edge["source"] not in owners or edge["target"] not in owners else (
            "internal" if owners[edge["source"]] == owners[edge["target"]] else "cross_group"
        )
        for kind in ("statement", "proof"):
            totals[part][kind] += edge[kind]
    assert totals["outside"] == {"statement": 1, "proof": 2}
    assert {kind: totals["internal"][kind] + totals["cross_group"][kind] for kind in ("statement", "proof")} == {
        "statement": 60, "proof": 56,
    }
    assert Counter(kind for _, _, kind in edges) == {"statement": 61, "proof": 58}
    with _served(project / "site") as url:
        yield _ScopedConsumer(_WorkspaceSite(url, records, graph), graph, groups, totals)


def _committed(page: object) -> None:
    # The actual router removes aria-busy when a transaction completes; it
    # never writes a synthetic "false" attribute on the published host.
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_attribute("data-dag-ready", "true")
    playwright.expect(page.locator(".bp-dag-viewer")).not_to_have_attribute("aria-busy", "true")
    _frames(page)


def _native_math(page: object) -> object:
    """Observe the viewer's own safe refresh; never request a test-only pass."""
    card = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{_TARGET}"]')
    playwright.expect(card).to_have_count(1)
    playwright.expect(card.locator(".arithmatex")).to_have_count(len(_TEX))
    playwright.expect(card.locator("mjx-container")).to_have_count(len(_TEX))
    assert card.locator("mjx-merror, merror").count() == 0
    return card


def _native_scope_math(page: object) -> object:
    source = page.locator(".bp-reading-body > .bp-reading-source")
    paragraph = source.locator("p").filter(has_text="Scope identity statement").first
    playwright.expect(paragraph).to_have_count(1)
    playwright.expect(paragraph.locator(".arithmatex")).to_have_count(len(_SCOPE_TEX))
    playwright.expect(paragraph.locator("mjx-container")).to_have_count(len(_SCOPE_TEX))
    assert paragraph.locator("mjx-merror, merror").count() == 0
    assert paragraph.locator(".arithmatex").evaluate_all(
        "nodes => nodes.map(node => node.getAttribute('data-autoform-math-source').trim())"
    ) == [r"\(" + tex + r"\)" for tex in _SCOPE_TEX]
    assert len(source.text_content()) > 420
    playwright.expect(source).to_contain_text(_SCOPE_END)
    cards = source.locator(".bp-thmwrapper[data-autoform-node-id]")
    playwright.expect(cards).to_have_count(9)
    assert set(cards.evaluate_all("nodes => nodes.map(node => node.dataset.autoformNodeId)")) == {
        _item(number) for number in range(72, 81)
    }
    for number in range(72, 81):
        card = source.locator(f'.bp-thmwrapper[data-autoform-node-id="{_item(number)}"]')
        playwright.expect(card).to_have_count(1)
        playwright.expect(card).to_contain_text(_END)
        playwright.expect(card.locator(".arithmatex")).to_have_count(len(_TEX))
        playwright.expect(card.locator("mjx-container")).to_have_count(len(_TEX))
        assert card.locator("mjx-merror, merror").count() == 0
    return source


def _group(page: object, key: str) -> object:
    return page.locator(f'.bp-dag-node[data-overview-group="{key}"]')


def _geometry(control: object) -> dict:
    return control.evaluate("""node => {
      const r=node.getBoundingClientRect(),s=node.closest('.bp-dag-stage').getBoundingClientRect();
      const m=new DOMMatrix(getComputedStyle(node).transform),title=node.querySelector('.bp-dag-node-title'),meta=node.querySelector('.bp-dag-node-meta');
      return {x:r.x,y:r.y,width:r.width,height:r.height,scale:m.a,camera:[m.a,m.d,m.e,m.f],
        stage:{x:s.x,y:s.y,width:s.width,height:s.height},hit:node.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)),
        title:title.textContent,meta:meta.textContent,titlePx:parseFloat(getComputedStyle(title).fontSize)*m.a,
        metaPx:parseFloat(getComputedStyle(meta).fontSize)*m.a};
    }""")


def _focus_group(page: object, key: str) -> object:
    # The existing native map keys can reach virtualized off-screen groups.
    # Do not expose the renderer or invent a view-group id from its serial.
    pending: list[tuple[str, ...]] = [()]
    seen: set[str] = set()
    _fit_graph(page)
    start_id = page.locator(".bp-dag-node").evaluate_all("""nodes => {
      for(const node of nodes) {
        const r=node.getBoundingClientRect(),s=node.closest('.bp-dag-stage').getBoundingClientRect();
        if(r.width>0&&r.height>0&&r.left>=s.left&&r.right<=s.right&&r.top>=s.top&&r.bottom<=s.bottom&&
           node.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)))return node.dataset.autoformNodeId;
      } return null;
    }""")
    assert start_id is not None, "A fitted large scope must contain a real usable starting card"
    while pending and len(seen) <= 32:
        path = pending.pop(0)
        _fit_graph(page)
        start = page.locator(f'.bp-dag-node[data-autoform-node-id="{start_id}"]')
        playwright.expect(start).to_be_visible()
        start.focus()
        for direction in path:
            page.keyboard.press(direction)
            _frames(page)
        active = page.locator(".bp-dag-node:focus")
        if active.get_attribute("data-overview-group") == key:
            assert _geometry(active)["hit"] is True
            # Keep this observed control's identity; a background pan clears
            # focus, so returning the dynamic :focus selector would lose it.
            identifier = active.get_attribute("data-autoform-node-id")
            return page.locator(f'.bp-dag-node[data-autoform-node-id="{identifier}"]')
        identifier = active.get_attribute("data-autoform-node-id")
        if identifier not in seen:
            seen.add(identifier)
            pending.extend((*path, direction) for direction in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"))
    pytest.fail(f"Real scoped group is not keyboard-reachable: {key}")


def _pan(page: object) -> None:
    point = page.locator(".bp-dag-stage").evaluate("""stage => {
      const r=stage.getBoundingClientRect(),canvas=stage.querySelector('canvas');
      for(const fx of [.08,.92,.5])for(const fy of [.75,.5,.25]) {
        const x=r.x+r.width*fx,y=r.y+r.height*fy;if(document.elementFromPoint(x,y)===canvas)return {x,y};
      } throw new Error('The real map must have a reachable native canvas background');
    }""")
    page.mouse.move(point["x"], point["y"])
    page.mouse.down()
    page.mouse.move(point["x"] + 18, point["y"] + 12, steps=5)
    page.mouse.up()
    _frames(page)


def _settled_reading_scroll(reading: object) -> float:
    """Observe native scrolling until its actual position is stable, without setting it."""
    return reading.evaluate("""node => new Promise((resolve, reject) => {
      let previous = node.scrollTop, stableSince = performance.now(), frame = 0;
      const timeout = setTimeout(() => {
        cancelAnimationFrame(frame);
        reject(new Error('Native reader scroll did not stabilize within five seconds'));
      }, 5000);
      function sample(now) {
        const actual = node.scrollTop;
        if (actual !== previous) { previous = actual; stableSince = now; }
        if (now - stableSince >= 200) { clearTimeout(timeout); resolve(actual); return; }
        frame = requestAnimationFrame(sample);
      }
      frame = requestAnimationFrame(sample);
    })""")


def _zoom_group(page: object, key: str) -> None:
    control = _focus_group(page, key)
    r = control.bounding_box()
    page.mouse.move(r["x"] + r["width"] / 2, r["y"] + r["height"] / 2)
    page.keyboard.down("Control")
    try:
        for _ in range(8):
            page.mouse.wheel(0, -180)
            _frames(page)
            if _group(page, key).count() == 0:
                break
    finally:
        page.keyboard.up("Control")
    playwright.expect(_group(page, key)).to_have_count(0)


def _capture(page: object, consumer: _ScopedConsumer, width: int, phase: str) -> None:
    folder = os.environ.get("AUTOFORM_UI_SCREENSHOT_DIR")
    if folder:
        directory = Path(folder)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"scope-overview-{width}-{phase}.png"))
        snapshot = {"phase": phase, "typed_totals": consumer.totals,
                    "visible_groups": page.locator(".bp-dag-node[data-overview-group]").count(), "pixels": _pixels(page)}
        snapshot["controls"] = page.locator(".bp-dag-node").evaluate_all("""nodes => nodes.map(node => {
          const r=node.getBoundingClientRect(),m=new DOMMatrix(getComputedStyle(node).transform);
          return {id:node.dataset.autoformNodeId,group:node.dataset.overviewGroup,title:node.querySelector('.bp-dag-node-title').textContent,
            meta:node.querySelector('.bp-dag-node-meta').textContent,scale:m.a,x:r.x,y:r.y,width:r.width,height:r.height};
        })""")
        (directory / f"scope-overview-{width}-{phase}.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS)
def test_large_scope_keeps_readable_truthful_groups_native_drill_zoom_and_back(
    workspace_page: object, scoped_consumer: _ScopedConsumer, workspace_runtime: _PinnedRuntime, width: int, height: int,
) -> None:
    page, consumer = workspace_page, scoped_consumer
    page.set_viewport_size({"width": width, "height": height})
    documents = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, consumer.site, workspace_runtime, _SCOPE)
    _committed(page)
    playwright.expect(page.locator(".bp-dag-scope-overview")).to_have_count(1)
    handle = page.locator(".bp-reading-pane > .bp-dag-sheet-handle")
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    _capture(page, consumer, width, "initial")
    assert 1 <= page.locator(".bp-dag-node[data-overview-group]").count() <= 9
    assert page.locator(".bp-dag-node").count() < 40 and _pixels(page)["painted"] > 0
    playwright.expect(page.locator('.bp-dag-node[data-autoform-node-id*="remote"]')).to_have_count(0)
    for key in consumer.groups:
        control = _focus_group(page, key)
        box = _geometry(control)
        assert box["title"] == ("Other topics" if key == "unassigned" else key[5:])
        # An area card counts its fully proved targets; every fixture target is not ready.
        assert box["meta"] == "9 topics · 0 of 9 fully proved"
        assert box["scale"] >= .9 - 1e-6 and box["width"] >= 180 and box["height"] >= 90
        assert box["titlePx"] >= 15 and box["metaPx"] >= 12
        assert box["hit"] is True
        assert box["y"] >= box["stage"]["y"] + 36
        assert box["y"] + box["height"] <= box["stage"]["y"] + box["stage"]["height"] - 32
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    _native_math(page)
    reading = page.locator(".bp-reading-body")
    reading.hover()
    page.mouse.wheel(0, 360)
    reading_scroll = _settled_reading_scroll(reading)
    assert reading_scroll > 0
    # A phone's taps cycle half, full and the peek, so closing can take two.
    _peek_reader(page, handle)
    key = "area:" + _AREAS[0]
    control = _focus_group(page, key)
    _pan(page)
    control.focus()
    saved = _geometry(control)
    assert saved["hit"] is True
    source = page.locator(".bp-reading-body > .bp-reading-source")
    source_title = source.locator("h1").text_content()
    control.click()
    playwright.expect(_group(page, key)).to_have_count(0)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    for node in (consumer.groups[key][0], consumer.groups[key][-1]):
        exposed = _focus_graph_node(page, node["id"])
        assert exposed.get_attribute("data-overview-group") is None and _geometry(exposed)["hit"] is True
    assert page.url == consumer.site.route(_SCOPE)
    page.go_back()
    _committed(page)
    restored = _group(page, key)
    playwright.expect(restored).to_be_focused()
    assert _geometry(restored)["camera"] == pytest.approx(saved["camera"], abs=.5)
    assert source.locator("h1").text_content() == source_title
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    # A collapsed wide reader is display:none; inspect retention only after
    # the user natively reopens the actual scrollable mathematical source.
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    playwright.expect(page.locator(".bp-reading-pane > .bp-dag-inspector-inner")).to_be_visible()
    assert _settled_reading_scroll(reading) == pytest.approx(reading_scroll, abs=1)
    # A phone's taps go half, full, then peek, so closing may take two.
    _peek_reader(page, handle)
    _zoom_group(page, key)
    focused = _focus_graph_node(page, consumer.groups[key][0]["id"])
    playwright.expect(page.locator(".bp-dag-breadcrumb")).to_contain_text(_AREAS[0])
    playwright.expect(page.locator(".bp-dag-breadcrumb").get_by_role("link", name=_SCOPE_TITLE, exact=True)).to_have_count(1)
    expanded_camera = _geometry(focused)["camera"]
    parent = page.locator(".bp-dag-breadcrumb").get_by_role("link", name=_SCOPE_TITLE, exact=True)
    playwright.expect(parent).to_have_attribute("href", consumer.site.route(_SCOPE))
    parent.click()
    _committed(page)
    assert page.url == consumer.site.route(_SCOPE)
    playwright.expect(page.locator(".bp-dag-scope-overview")).to_have_count(1)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    _focus_group(page, key)
    page.go_back()
    _committed(page)
    playwright.expect(_group(page, key)).to_have_count(0)
    playwright.expect(page.locator(".bp-dag-breadcrumb")).to_contain_text(_AREAS[0])
    focused = page.locator(f'.bp-dag-node[data-autoform-node-id="{consumer.groups[key][0]["id"]}"]')
    assert _geometry(focused)["camera"] == pytest.approx(expanded_camera, abs=.5)
    parent = page.locator(".bp-dag-breadcrumb").get_by_role("link", name=_SCOPE_TITLE, exact=True)
    parent.focus()
    parent.press("Enter")
    _committed(page)
    assert page.url == consumer.site.route(_SCOPE), "The group parent is this local scope, not the whole library"
    playwright.expect(page.locator(".bp-dag-scope-overview")).to_have_count(1)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    # The trail is rebuilt under the crumb, so focus goes to the group just left rather than to the page.
    playwright.expect(_group(page, key)).to_be_focused()
    _focus_group(page, key)
    playwright.expect(page.locator('.bp-dag-node[data-autoform-node-id*="remote"]')).to_have_count(0)
    page.go_back()
    _committed(page)
    playwright.expect(_group(page, key)).to_have_count(0)
    playwright.expect(page.locator(".bp-dag-breadcrumb")).to_contain_text(_AREAS[0])
    focused = page.locator(f'.bp-dag-node[data-autoform-node-id="{consumer.groups[key][0]["id"]}"]')
    assert _geometry(focused)["camera"] == pytest.approx(expanded_camera, abs=.5)
    bounds = focused.bounding_box()
    page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
    page.keyboard.down("Control")
    try:
        for _ in range(12):
            page.mouse.wheel(0, 220)
            _frames(page)
            if _group(page, key).count():
                break
    finally:
        page.keyboard.up("Control")
    _focus_group(page, key)
    _frames(page)
    assert page.url == consumer.site.route(_SCOPE)
    playwright.expect(_group(page, key)).to_have_count(1)
    current = page.locator(".bp-dag-breadcrumb li").last
    playwright.expect(current).to_have_text(_SCOPE_TITLE)
    playwright.expect(current).to_have_attribute("aria-current", "page")
    playwright.expect(page.locator(".bp-dag-breadcrumb").get_by_role("link", name=_SCOPE_TITLE, exact=True)).to_have_count(0)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(page.locator(".bp-dag-scope-overview")).to_have_count(1)
    playwright.expect(page.locator('.bp-dag-node[data-autoform-node-id*="remote"]')).to_have_count(0)
    _fit_graph(page)
    assert _geometry(page.locator(".bp-dag-node[data-overview-group]").first)["scale"] >= .9 - 1e-6
    _capture(page, consumer, width, "fit")
    assert documents == [consumer.site.route(_SCOPE)]
    assert page.evaluate("() => document === __workspaceDocument") is True


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS)
def test_exact_lean_search_precedes_groups_and_keeps_complete_math_source_and_scope_back(
    workspace_page: object, scoped_consumer: _ScopedConsumer, workspace_runtime: _PinnedRuntime, width: int, height: int,
) -> None:
    page, consumer = workspace_page, scoped_consumer
    page.set_viewport_size({"width": width, "height": height})
    documents = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, consumer.site, workspace_runtime, _SCOPE)
    _committed(page)
    key = "area:" + _AREAS[0]
    control = _focus_group(page, key)
    camera = _geometry(control)["camera"]
    _search(page, _TARGET_LEAN, _TARGET)
    page.wait_for_url(consumer.site.route(_TARGET))
    _committed(page)
    playwright.expect(page.locator(".bp-dag-scope-overview")).to_have_count(0)
    playwright.expect(page.locator(".bp-dag-node[data-overview-group]")).to_have_count(0)
    playwright.expect(page.locator(f'.bp-dag-node[data-autoform-node-id="{_TARGET}"]')).to_have_attribute("data-selected", "true")
    handle = page.locator(".bp-reading-pane > .bp-dag-sheet-handle")
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    card = _native_math(page)
    playwright.expect(card).to_contain_text(_END)
    playwright.expect(card).to_contain_text(_TARGET_LEAN)
    assert len(card.text_content()) > 420
    source_link = card.get_by_role("link", name="Elementary identity source", exact=True)
    playwright.expect(source_link).to_have_count(1)
    source_url = urlsplit(urljoin(page.url, source_link.get_attribute("href")))
    assert source_url.path.endswith("/sources/identity-notes.html") and source_url.fragment == "elementary"
    if width <= 720:
        # A phone reader opens at half under the map; read the formula's source at full, as a reader would, so MathJax's menu
        # opens with room rather than at the screen's bottom edge.
        handle.click()
        playwright.expect(page.locator(".bp-dag-viewer")).to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    formula = card.locator("mjx-container").last
    formula.scroll_into_view_if_needed()
    assert "".join(_math_source(page, formula).split()) == "".join(_TEX[-1].split())
    page.go_back()
    _committed(page)
    playwright.expect(page.locator(".bp-dag-scope-overview")).to_have_count(1)
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(_SCOPE_TITLE)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    assert _geometry(_group(page, key))["camera"] == pytest.approx(camera, abs=.5)
    assert documents == [consumer.site.route(_SCOPE)]
    assert page.evaluate("() => document === __workspaceDocument") is True


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS)
def test_native_scope_reading_links_open_full_math_and_restore_closed_group_history(
    workspace_page: object, scoped_consumer: _ScopedConsumer, workspace_runtime: _PinnedRuntime, width: int, height: int,
) -> None:
    page, consumer = workspace_page, scoped_consumer
    page.set_viewport_size({"width": width, "height": height})
    documents = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, consumer.site, workspace_runtime, _SCOPE)
    _committed(page)
    handle = page.locator(".bp-reading-pane > .bp-dag-sheet-handle")
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    key = "area:" + _AREAS[0]
    control = _focus_group(page, key)
    camera = _geometry(control)["camera"]
    for label, anchor in (("Read the complete workshop", None), ("Read the retained identity notes", _HEADING)):
        _search(page, "remote/note-00", "remote/note-00")
        page.wait_for_url(consumer.site.route("remote/note-00"))
        playwright.expect(page.locator(".bp-reading-body h1")).to_have_text("Unrelated note 0")
        _committed(page)
        link = page.locator(".bp-reading-body").get_by_role("link", name=label, exact=True)
        link.focus()
        link.press("Enter")
        destination = consumer.site.route(_SCOPE) + ("#" + anchor if anchor else "")
        page.wait_for_url(destination)
        playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(_SCOPE_TITLE)
        _committed(page)
        playwright.expect(handle).to_have_attribute("aria-expanded", "true")
        inner = page.locator(".bp-reading-pane > .bp-dag-inspector-inner")
        assert inner.get_attribute("inert") is None and inner.get_attribute("aria-hidden") is None
        source = _native_scope_math(page)
        if anchor:
            heading = source.locator("#" + anchor)
            playwright.expect(heading).to_be_visible()
            bounds, body = heading.bounding_box(), page.locator(".bp-reading-body").bounding_box()
            assert body["y"] - 1 <= bounds["y"] and bounds["y"] + bounds["height"] <= body["y"] + body["height"] + 1
            assert page.locator(".bp-reading-body").evaluate("node => node.scrollTop") > 0
        else:
            playwright.expect(source.locator("h1")).to_be_focused()
        page.go_back()
        page.wait_for_url(consumer.site.route("remote/note-00"))
        playwright.expect(page.locator(".bp-reading-body h1")).to_have_text("Unrelated note 0")
        _committed(page)
        page.go_back()
        page.wait_for_url(consumer.site.route(_SCOPE))
        playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(_SCOPE_TITLE)
        _committed(page)
        playwright.expect(handle).to_have_attribute("aria-expanded", "false")
        playwright.expect(page.locator(".bp-dag-scope-overview")).to_have_count(1)
        assert _geometry(_group(page, key))["camera"] == pytest.approx(camera, abs=.5)
    assert documents == [consumer.site.route(_SCOPE)]
    assert page.evaluate("() => document === __workspaceDocument") is True
