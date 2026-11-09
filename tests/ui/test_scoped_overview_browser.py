"""Native large-scope journeys over an independent authored mathematical book.

The source declares elementary zero-identity targets with explicitly unfinished
proofs. This is a publication/viewer fixture, not a Lean-kernel verification.
Its 81 local items, eight authored areas, structural unassigned items, and
outside dependencies come from Markdown and the real publisher, never mocked
graph JSON or a replacement renderer.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import pytest

from autoform_cli.scaffold import scaffold_project
from autoform_cli.ui.render import render_site
from tests.ui.test_integrated_publication import _decoded_relations
from tests.ui.test_integrated_workspace_browser import (
    _PinnedRuntime,
    _WorkspaceSite,
    _frames,
    _math_source,
    _open,
    _search,
    _selected,
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
    assert not rendered.unresolved
    config = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    build(config)
    index_path = project / "site/dependencies/index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    records = {node["id"]: node for node in index["nodes"]}
    graph_path = index_path.parent / unquote(urlsplit(records[_SCOPE]["data_url"]).path)
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    local = [node for node in graph["nodes"] if node["kind"] != "boundary"]
    assert len(local) == 81 and {node["title"] for node in local} == {_title(n) for n in range(81)}
    assert not any("Unrelated note" in node["title"] for node in graph["nodes"])
    groups: dict[str, list[dict]] = {}
    owners = {}
    for node in local:
        key = "area:" + node["area"] if node.get("area") else "unassigned"
        groups.setdefault(key, []).append(node)
        owners[node["id"]] = key
    assert set(groups) == {*("area:" + area for area in _AREAS), "unassigned"}
    assert all(len(members) == 9 for members in groups.values())
    # Identify each card's record as the book does (blueprint-dag.js recordId): by its
    # id, or for a scope or boundary card that is no record itself, the id after its prefix.
    represented = {}
    for node in graph["nodes"]:
        original = node["id"] if node["id"] in records or node["kind"] not in {"scope", "boundary"} else (
            node["id"].split(":", 1)[1]
        )
        assert original in records, node["id"]
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

    # The search index publishes every relation; each is put on the cards holding its records, as the book
    # identifies them.
    shown = [(endpoint(source), endpoint(target), {"statement": statement, "proof": proof})
             for source, target, statement, proof in _decoded_relations(index)]
    # The authoring tuples are dependent -> prerequisite; published flow is
    # prerequisite -> dependent, so upstream/downstream is checked explicitly.
    expected_edges = Counter((endpoint(target), endpoint(source), kind) for source, target, kind in edges)
    actual_edges = Counter()
    neighbors = {node["id"]: set() for node in local}
    for source, target, counts in shown:
        for kind in ("statement", "proof"):
            actual_edges[(source, target, kind)] += counts[kind]
        if source in neighbors and target in neighbors:
            neighbors[source].add(target)
            neighbors[target].add(source)
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
    for source, target, counts in shown:
        part = "outside" if source not in owners or target not in owners else (
            "internal" if owners[source] == owners[target] else "cross_group"
        )
        for kind in ("statement", "proof"):
            totals[part][kind] += counts[kind]
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


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS)
def test_exact_lean_search_keeps_complete_math_source_and_scope_back(
    workspace_page: object, scoped_consumer: _ScopedConsumer, workspace_runtime: _PinnedRuntime, width: int, height: int,
) -> None:
    page, consumer = workspace_page, scoped_consumer
    page.set_viewport_size({"width": width, "height": height})
    documents = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, consumer.site, workspace_runtime, _SCOPE)
    _committed(page)
    # The workshop's page has prose of its own, so a desktop reads it beside the map and a phone opens it on the map.
    handle, opened = page.locator(".bp-reading-pane > .bp-dag-sheet-handle"), "true" if width > 720 else "false"
    playwright.expect(handle).to_have_attribute("aria-expanded", opened)
    _search(page, _TARGET_LEAN, _TARGET)
    page.wait_for_url(consumer.site.route(_TARGET))
    _committed(page)
    _selected(page, _TARGET)
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
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(_SCOPE_TITLE)
    playwright.expect(handle).to_have_attribute("aria-expanded", opened)
    playwright.expect(page.locator(".bp-dag-overview-only")).to_have_count(0)
    _selected(page, _SCOPE)
    assert documents == [consumer.site.route(_SCOPE)]
    assert page.evaluate("() => document === __workspaceDocument") is True


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS)
def test_native_scope_reading_links_open_full_math_and_back_restores_the_scope(
    workspace_page: object, scoped_consumer: _ScopedConsumer, workspace_runtime: _PinnedRuntime, width: int, height: int,
) -> None:
    page, consumer = workspace_page, scoped_consumer
    page.set_viewport_size({"width": width, "height": height})
    documents = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, consumer.site, workspace_runtime, _SCOPE)
    _committed(page)
    # A desktop reads the workshop's own prose beside the map; a phone opens it on the map.
    handle, opened = page.locator(".bp-reading-pane > .bp-dag-sheet-handle"), "true" if width > 720 else "false"
    playwright.expect(handle).to_have_attribute("aria-expanded", opened)
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
        playwright.expect(handle).to_have_attribute("aria-expanded", opened)
        playwright.expect(page.locator(".bp-dag-overview-only")).to_have_count(0)
        _selected(page, _SCOPE)
    assert documents == [consumer.site.route(_SCOPE)]
    assert page.evaluate("() => document === __workspaceDocument") is True
