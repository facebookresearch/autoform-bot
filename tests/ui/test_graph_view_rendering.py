from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from autoform_cli import status
from autoform_cli.ui import dag_viewer, graph_pages, render, site_index
from autoform_cli.ui.graph_views import GraphView, ViewEdge, ViewNode

_ASSETS = Path(dag_viewer.__file__).with_name("assets")


def test_full_view_payload_is_deterministic_and_carries_only_what_the_book_reads(tmp_path: Path) -> None:
    nodes = tuple(
        ViewNode(
            id=f"chapter/node-{index}",
            title="Unsafe <script>" if index == 0 else f"Node {index}",
            kind="node",
            members=(f"chapter/node-{index}",),
            status_counts=((("inventory_checked", 1),) if index == 0 else (("planned", 1),)),
            declaration=None if index == 0 else "theorem",
            catalog="module" if index == 0 else None,
            status_key="planned",
            summary="A reader-facing mathematical summary." if index == 0 else None,
            lean="Project.node" if index == 0 else None,
        )
        for index in range(12)
    )
    view = GraphView(
        kind="full",
        title="Full graph",
        nodes=nodes,
        edges=tuple(ViewEdge(source.id, target.id, statement_count=1) for source, target in zip(nodes, nodes[1:])),
    )
    outputs = [tmp_path / "one.json", tmp_path / "two.json"]
    for output in outputs:
        dag_viewer.write_payload(output, view)

    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    payload = json.loads(outputs[0].read_text(encoding="utf-8"))
    assert payload["schema"] == "autoform-dag-view/v2"
    # The book reads each field; nothing rides along for a reader that is gone.
    assert set(payload) == {"edges", "nodes", "palette", "present_statuses", "schema", "title", "view"}
    assert all(set(node) == {"area", "id", "kind", "lean", "status", "summary", "title"} for node in payload["nodes"])
    assert [node["id"] for node in payload["nodes"]] == [node.id for node in nodes]
    assert payload["view"] == {"book": None, "breadcrumbs": []}
    assert payload["present_statuses"] == ["planned", "inventory_checked"]
    assert payload["nodes"][0]["title"] == "Unsafe <script>"
    assert payload["nodes"][0]["status"] == "inventory_checked"
    assert payload["nodes"][0]["summary"] == "A reader-facing mathematical summary."
    assert payload["nodes"][0]["lean"] == "Project.node"
    assert payload["palette"]["inventory_checked"]["label"] == "inventory checked"
    assert all(
        set(entry) == {"label", "meaning", "light", "dark"}
        and set(entry["light"]) == set(entry["dark"]) == {"fill", "stroke"}
        for entry in payload["palette"].values()
    )
    # Nothing reads a payload's relations; both clients still require the list.
    assert payload["edges"] == []
    for inventory in ("inventory_checked", "inventory_pending"):
        assert "it records inspection, not a mathematical proof." in payload["palette"][inventory]["meaning"]


def test_an_overview_marks_what_the_book_lists(tmp_path: Path) -> None:
    wrappers = tuple(
        ViewNode(
            f"scope:wrapper-{index}", f"Wrapper {index}", "scope", ("a", "b"), (("planned", 2),),
            navigation="collection" if index % 2 else "transparent",
        )
        for index in range(2)
    )
    results = tuple(ViewNode(name, name, "node", (name,), (("planned", 1),)) for name in ("a", "b"))
    edges = (ViewEdge(wrappers[0].id, "a", statement_count=1), ViewEdge("a", "b", 1, 2))
    view = GraphView("project", "Overview", (*wrappers, *results), edges, overview=True)
    output = tmp_path / "overview.json"
    dag_viewer.write_payload(output, view)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert [node["id"] for node in payload["nodes"]] == [node.id for node in view.nodes]
    assert [node["map"] for node in payload["nodes"]] == [False, False, True, True]


def test_each_inventory_has_a_neutral_colour_of_its_own(tmp_path: Path) -> None:
    """Checked and pending catalogs are drawn apart from each other and from every state.

    Both are neutral, but a ring holding planned targets and both kinds of
    inventory needs three greys, and borrowing a state's colour would read as
    target work.
    """
    statuses = ("planned", "inventory_pending", "inventory_checked")
    view = GraphView(
        kind="chapter",
        title="Library",
        nodes=tuple(
            ViewNode(
                id=f"library/{status}",
                title=status,
                kind="node",
                members=(f"library/{status}",),
                status_counts=((status, 1),),
                catalog=None if status == "planned" else "module",
                status_key="planned",
            )
            for status in statuses
        ),
        edges=(),
    )
    output = tmp_path / "library.json"
    dag_viewer.write_payload(output, view)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert [node["status"] for node in payload["nodes"]] == list(statuses)
    assert payload["present_statuses"] == ["planned", "inventory_checked", "inventory_pending"]
    assert payload["palette"]["inventory_pending"]["label"] == "inventory pending"
    for inventory in ("inventory_checked", "inventory_pending"):
        for scheme in ("light", "dark"):
            assert all(
                entry[scheme]["stroke"] != payload["palette"][inventory][scheme]["stroke"]
                for key, entry in payload["palette"].items()
                if key != inventory
            )


def test_a_catalog_carries_a_few_lean_names(tmp_path: Path) -> None:
    """A catalog lists its keyword-declared public names, which made its payload and inspector line unbounded."""
    names = [f"Mathlib.Algebra.Module.lemma_{index:03d}" for index in range(150)]
    nodes = (
        ViewNode(
            "catalog",
            "Catalog",
            "node",
            ("catalog",),
            (("inventory_checked", 1),),
            catalog="module",
            lean=", ".join(names),
        ),
        ViewNode(
            "leaf", "Leaf", "node", ("leaf",), (("planned", 1),), declaration="theorem", lean=" ".join(names[:20])
        ),
    )
    output = tmp_path / "inventory.json"
    dag_viewer.write_payload(output, GraphView(kind="chapter", title="Inventory", nodes=nodes, edges=()))

    catalog, leaf = json.loads(output.read_text(encoding="utf-8"))["nodes"]
    assert catalog["lean"] == ", ".join(names[:8])
    assert len(catalog["lean"].encode()) < 400
    # A declaration leaf names only its own declarations, so it keeps them all.
    assert leaf["lean"] == " ".join(names[:20])


def test_a_standalone_explorer_page_is_named_for_its_scope() -> None:
    results = GraphView("chapter", "Results dependency map", (ViewNode("a", "A", "node", ("a",), (("planned", 1),)),), ())
    topics = tuple(ViewNode(f"scope:{name}", name, "scope", (), (("planned", 1),), area="Algebra") for name in "xy")
    areas = GraphView("chapter", "Topics dependency map", topics, (ViewEdge("scope:x", "scope:y", statement_count=1),))

    assert graph_pages._dependency_heading(results) == "Results dependencies"
    assert graph_pages._dependency_heading(areas) == "Topics dependencies"
    assert graph_pages._dependency_heading(results, "Top level") == "Top level dependencies"


def test_an_explorer_host_escapes_what_it_writes_and_links_only_relative_fallbacks() -> None:
    container = dag_viewer.render_container(
        'unsafe" data-x="oops',
        script_href='scripts/viewer" onload="oops.js',
        fallback_links=(
            ("Unsafe <node>", 'roadmap/a" onclick="oops'),
            ("Unsafe protocol", "javascript:alert(1)"),
        ),
        fallback_total=5,
    )

    assert 'data-graph-src="unsafe%22%20data-x%3D%22oops"' in container
    assert 'src="scripts/viewer%22%20onload%3D%22oops.js"' in container
    assert "The dependency explorer needs JavaScript and its graph data." in container
    assert "Unsafe &lt;node&gt;" in container
    assert 'href="roadmap/a&quot; onclick=&quot;oops"' in container
    assert "javascript:alert" not in container
    assert "Showing 1 of 5 linked nodes" in container
    # The host carries its stylesheet, so the first paint is already laid out.
    assert "<style data-autoform-dag-style>" in container


def test_the_explorer_builds_its_dom_without_html_strings() -> None:
    """Titles, summaries and Lean names come from authored Markdown: the client sets text, never parsed HTML."""
    script = dag_viewer._script()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in script, sink


def _rules(style: str) -> list[tuple[str, str]]:
    """Each rule's selector and declarations, at-rules unwrapped."""
    flat = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
    return [(selector.strip(), body) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", flat)]


def test_explorer_styles_keep_one_family_one_type_scale_and_px_sizes() -> None:
    """The rail and drawer headings were set in Georgia, among twenty rem sizes and unloaded weights. Material's root
    font is 20px, 22px from 1600px, so a rem rail was 407px wide at 1600 by 900."""
    style = dag_viewer._style()
    assert "serif" not in style.replace("sans-serif", "")
    assert set(re.findall(r"font-size: (\d+)px(?: !important)?;", style)) <= {"11", "12", "13", "14", "16", "17", "18", "22"}
    assert set(re.findall(r"font-weight: ([^;]+);", style)) <= {"400", "600", "700"}
    assert not re.findall(r"\d+(?:\.\d+)?rem\b", style)
    assert "text-transform: uppercase" not in style


def test_focus_rings_are_one_solid_link_coloured_line() -> None:
    """Focus rings were about 2:1 against the page, and the rail showed focus as hover."""
    rules = _rules(dag_viewer._style())
    rings = [(selector, body) for selector, body in rules if ":focus-visible" in selector and "outline:" in body]
    assert rings
    for selector, body in rings:
        assert re.search(r"outline: 2px solid var\(--dag-link\)", body), selector
    # The statement being read is marked in ink, not in the blue that marks focus and links.
    current = [body for selector, body in rules if selector == ".bp-reading-current"]
    assert current and all("--dag-link" not in body and "outline" not in body for body in current)


def test_touch_screens_keep_every_toolbar_control_and_44px_targets() -> None:
    """A coarse pointer hides no control, and every toolbar control reaches 44px."""
    coarse = dag_viewer._style().split("@media (pointer: coarse) {\n", 1)[1].split("\n}", 1)[0]
    for hiding in ("display: none", "visibility: hidden", "pointer-events: none"):
        assert hiding not in coarse, hiding
    targets = [selector for selector, body in _rules(coarse) if "min-height: 44px" in body]
    for control in ("bp-dag-search", "bp-dag-button", "bp-dag-filter-summary", "bp-dag-hidden-button"):
        assert any(control in selector for selector in targets), control


def _drawn_classes() -> str:
    """Every source that can put a class on the explorer's page: its scripts and the Python that writes its HTML."""
    root = Path(dag_viewer.__file__).parents[1]
    scripts = [path for path in _ASSETS.glob("*.js") if path.name != "blueprint-explorer.min.js"]
    return "\n".join(path.read_text(encoding="utf-8") for path in (*scripts, *root.rglob("*.py")))


def test_every_explorer_class_the_stylesheet_styles_is_drawn_somewhere() -> None:
    """The removed canvas and inspector left 26 classes styled that nothing drew any more.

    A class is drawn where a script or the renderer names it, or builds it as ``"bp-dag-" + name + "-<state>"``.
    """
    classes = {token for selector, _body in _rules(dag_viewer._style()) for token in re.findall(r"\.(bp-dag-[\w-]+)", selector)}
    drawn = _drawn_classes()
    built = set(re.findall(r'"bp-dag-" \+ \w+ \+ "(-[\w-]+)"', drawn))
    undrawn = sorted(
        name for name in classes
        if not re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", drawn)
        and not any(name.endswith(suffix) for suffix in built)
    )
    assert len(classes) > 50 and built
    assert undrawn == []


def _contrast(first: str, second: str) -> float:
    def luminance(colour: str) -> float:
        channels = [int(colour.lstrip("#")[index : index + 2], 16) / 255 for index in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _mix(colour: str, base: str, share: float) -> str:
    pairs = ((int(colour[index:index + 2], 16), int(base[index:index + 2], 16)) for index in (1, 3, 5))
    return "#" + "".join(f"{round(a * share + b * (1 - share)):02X}" for a, b in pairs)


def _tokens(dark: bool) -> dict[str, str]:
    """The book's colour tokens for one scheme, as its stylesheet sets them."""
    selector = "[data-md-color-scheme=slate]" if dark else ":root"
    rule = re.search(re.escape(selector) + r" \{([^}]+)\}", render._stylesheet())
    assert rule is not None, selector
    return {name: value.strip().upper() for name, value in re.findall(r"(--[\w-]+):\s*([^;]+);", rule.group(1))}


def _explorer(dark: bool) -> dict[str, str]:
    """The explorer's surfaces and link for one scheme, resolved from both stylesheets."""
    tokens, style = _tokens(dark), dag_viewer._style()
    panel = re.search(r"--dag-panel: color-mix\(in srgb, var\(--dag-bg\) (\d+)%, (#[\da-fA-F]{6}) \d+%\)", style)
    assert panel is not None
    page = tokens["--bp-surface"]
    surfaces = {"link": tokens["--bp-link"], "page": page, "panel": _mix(page, panel.group(2).upper(), int(panel.group(1)) / 100)}
    if dark:
        dark_map = re.search(r"\[data-md-color-scheme=slate\] \.bp-dag-viewer \{[^}]*--dag-map: (#[\da-fA-F]{6});", style)
        assert dark_map is not None
        surfaces["map"] = dark_map.group(1).upper()
    else:
        assert re.search(r"--dag-map: var\(--bp-sunken\b", style)
        surfaces["map"] = tokens["--bp-sunken"]
    return surfaces


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_links_read_at_small_size_on_every_explorer_surface(dark: bool) -> None:
    """The dark link was #2D88FF, 4.44:1 on the panel and 4.18:1 on the outline's current row."""
    surfaces, style = _explorer(dark), dag_viewer._style()
    link = surfaces["link"]
    # The page, the map and the panel, and each tint of the link the current rows take over the page.
    tints = {int(share) for share in re.findall(r"color-mix\(in srgb, var\(--dag-link\) (\d+)%, var\(--dag-bg\)\)", style)}
    assert tints
    behind = [surfaces["page"], surfaces["map"], surfaces["panel"], *(_mix(link, surfaces["page"], share / 100) for share in tints)]
    assert min(_contrast(link, surface) for surface in behind) >= 4.5, (link, behind)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_muted_text_reads_on_the_search_field_and_the_maps_notice(dark: bool) -> None:
    """The placeholder was translucent, and a map that failed to load left the stage empty."""
    style, tokens, surfaces = dag_viewer._style(), _tokens(dark), _explorer(dark)

    def declarations(pattern: str) -> dict[str, str]:
        rule = re.search(pattern, style, re.M)
        assert rule is not None, pattern
        return dict(re.findall(r"([\w-]+):\s*([^;]+);", rule.group(1)))

    placeholder = declarations(r"^\.bp-dag-search::placeholder \{([^}]+)\}")
    assert placeholder["color"] == "var(--dag-muted)" and placeholder["opacity"] == "1"
    assert declarations(r"^\.bp-dag-search \{([^}]+)\}")["background"] == "var(--dag-panel)"
    notice = declarations(r"^\.bp-dag-viewer\.bp-dag-viewer \.bp-dag-map-unavailable \{([^}]+)\}")
    assert (notice["position"], notice["top"], notice["left"]) == ("absolute", "50%", "50%")
    assert (notice["color"], notice["background"]) == ("var(--dag-muted)", "var(--dag-bg)")
    assert "--dag-muted: var(--bp-muted" in style
    assert min(_contrast(tokens["--bp-muted"], surface) for surface in (surfaces["panel"], surfaces["page"])) >= 4.5


def test_dark_explorer_cards_and_errors_read_against_the_page_toned_map() -> None:
    """Outlined cards were filled with the dark map's own colour, and planned's stroke had 2.1:1 against it."""
    surfaces, style = _explorer(True), dag_viewer._style()
    assert all(state.dark_fill.upper() != surfaces["map"] for state in (*status.STATES, *status.INVENTORY_STATES))
    planned = next(state for state in status.STATES if state.key == "planned")
    assert min(_contrast(planned.dark_stroke, surfaces["map"]), _contrast(planned.dark_stroke, surfaces["page"])) >= 3
    error = re.search(r"\[data-md-color-scheme=slate\] \.bp-dag-viewer \{[^}]*--dag-error: (#[\da-fA-F]{6});", style)
    assert error is not None and _contrast(error.group(1), surfaces["page"]) >= 4.5


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_status_strokes_outline_cards_on_both_maps(dark: bool) -> None:
    """A tinted card is barely darker than the map, so its stroke alone has to draw the card's edge."""
    surfaces = _explorer(dark)
    # Planned and the inventory greys are quiet on purpose; the ring tests keep those three apart.
    for state in status.STATES:
        if state.key != "planned":
            stroke, fill = (state.dark_stroke, state.dark_fill) if dark else (state.stroke, state.fill)
            assert min(_contrast(stroke, surface) for surface in (fill, surfaces["map"], surfaces["page"])) >= 3, state.key


def test_status_ring_greys_stay_apart_in_both_schemes() -> None:
    """Planned's dark stroke rose to #737578, 1.39:1 from inventory pending's #8A8D91, so their arcs ran together."""
    planned = next(state for state in status.STATES if state.key == "planned")
    greys = (planned, *status.INVENTORY_STATES)
    # A status bar runs planned, checked and pending inventory, so with one of them absent the other two meet.
    # 1.5:1 keeps neighbouring segments a visible step apart; the old dark pair was 1.39:1.
    for first, second in ((0, 1), (1, 2), (0, 2)):
        for stroke in ("stroke", "dark_stroke"):
            ratio = _contrast(getattr(greys[first], stroke), getattr(greys[second], stroke))
            assert ratio >= 1.5, (greys[first].key, greys[second].key, stroke, ratio)
    dark_map = _explorer(True)["map"]
    for state in greys:
        assert min(_contrast(state.dark_stroke, state.dark_fill), _contrast(state.dark_stroke, dark_map)) >= 3, state.key


def _book_marks(dark: bool) -> dict[str, str]:
    """The fill of each state's dependency dot and swatch, as the book's stylesheet emits it."""
    marks = re.findall(
        r"(?m)^(\[data-md-color-scheme=slate\] )?[^{\n]*\.bp-swatch-(\w+) \{ background: (#[0-9A-Fa-f]{6});",
        render._stylesheet(),
    )
    return {key: colour for slate, key, colour in marks if bool(slate) == dark}


def test_each_finished_status_has_its_own_small_mark_in_both_schemes() -> None:
    """Fully proved, proved and defined shared stroke #22773A, so their dots and swatches were one colour.

    In the book's dependency rows and the explorer's lists the mark is the only status signal. Light marks
    darken towards mathlib and dark ones brighten towards it, so more finished always reads stronger.
    """
    finished = ("mathlib", "fully_proved", "proved", "defined")
    assert [state.key for state in status.STATES[:4]] == list(finished)
    for dark in (False, True):
        # Search results, statement lists and the filter set marks on the map, the page and the panel.
        surfaces = _explorer(dark)
        behind = (surfaces["map"], surfaces["page"], surfaces["panel"])
        book = _book_marks(dark)
        marks = [book[key] for key in finished]
        for (first, second), (one, other) in zip(zip(finished, finished[1:]), zip(marks, marks[1:])):
            ratio = _contrast(one, other)
            assert ratio >= 1.5, (first, second, dark, ratio)
            assert (_contrast(one, "#000000") > _contrast(other, "#000000")) is dark, (first, second, dark)
        for key, mark in zip(finished, marks):
            assert min(_contrast(mark, surface) for surface in behind) >= 3, (key, dark, mark)


def test_filled_marks_that_are_not_green_part_from_every_green_by_hue() -> None:
    """The four greens span the lightness that keeps 3:1 on the map, so filled amber and grey marks match one.

    Light defined against not ready is 1.06:1 and against inventory pending 1.02:1; dark fully proved against
    not ready is 1.10:1. That is accepted (status.py says so), but hue must keep them apart.
    """

    def lab(colour: str) -> tuple[float, float, float]:
        channels = [int(colour[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        xyz = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,
               (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
        x, y, z = (t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116 for t in xyz)
        return 116 * y - 16, 500 * (x - y), 200 * (y - z)

    finished = ("mathlib", "fully_proved", "proved", "defined")
    filled = [state.key for state in (*status.STATES, *status.INVENTORY_STATES)
              if state.fill != "#FFFFFF" and state.key not in finished]
    assert {"not_ready", "inventory_pending"} <= set(filled)
    for dark in (False, True):
        book = _book_marks(dark)
        for key in filled:
            for green in finished:
                distance = sum((a - b) ** 2 for a, b in zip(lab(book[key]), lab(book[green]))) ** .5
                assert distance >= 40, (key, green, dark, round(distance))


def test_explorer_assets_and_ordinary_payload_stay_within_budgets(tmp_path: Path) -> None:
    nodes = tuple(
        ViewNode(
            id=f"chapter/node-{index}",
            title=f"Node {index}",
            kind="node",
            members=(f"chapter/node-{index}",),
            status_counts=(("planned", 1),),
            status_key="planned",
            lean="Project.NodeZero" if index == 0 else None,
            summary="A spectral gap statement." if index == 0 else None,
        )
        for index in range(500)
    )
    view = GraphView(
        kind="full",
        title="Budget fixture",
        nodes=nodes,
        edges=tuple(ViewEdge(nodes[index].id, nodes[index + 1].id, statement_count=1) for index in range(499)),
    )
    output = tmp_path / "budget.json"
    dag_viewer.write_payload(output, view)
    search = tmp_path / "search.json"
    site_index.write_search_index(
        search,
        view,
        context_links={node.id: f"chapters/chapter.html#node={node.id}" for node in nodes},
    )

    # The reader, hierarchy rail and bounded layout share one prebuilt
    # response; consumers still need no application build or runtime CDN.
    assert len(dag_viewer.viewer_script().encode()) < 145_000
    assert output.stat().st_size < 300_000
    assert search.stat().st_size < 100_000
    search_payload = json.loads(search.read_text(encoding="utf-8"))
    assert search_payload["schema"] == "autoform-dag-search/v1"
    assert "layout" not in search_payload["nodes"][0]
    assert search_payload["nodes"][0]["lean"] == "Project.NodeZero"
    assert search_payload["nodes"][0]["summary"] == "A spectral gap statement."
    assert "lean" not in search_payload["nodes"][1]
    assert "summary" not in search_payload["nodes"][1]


def test_viewer_script_links_are_relative_to_the_rendered_site_root(tmp_path: Path) -> None:
    assert graph_pages._viewer_script_link(tmp_path / "dependencies.md", tmp_path) == "javascripts/blueprint-dag.js"
    assert graph_pages._viewer_script_link(tmp_path / "dependencies/full.md", tmp_path) == "../javascripts/blueprint-dag.js"
    assert (
        graph_pages._viewer_script_link(tmp_path / "dependencies/chapters/large.md", tmp_path)
        == "../../javascripts/blueprint-dag.js"
    )


def test_payload_rejects_duplicate_nodes_and_unknown_edges(tmp_path: Path) -> None:
    node = ViewNode("a", "A", "node", ("a",), (("planned", 1),))
    duplicate = GraphView(kind="full", title="Duplicate", nodes=(node, node), edges=())
    unknown = GraphView(
        kind="full",
        title="Unknown",
        nodes=(node,),
        edges=(ViewEdge("missing", "a", statement_count=1),),
    )

    with pytest.raises(ValueError, match="duplicate node ids"):
        dag_viewer.write_payload(tmp_path / "duplicate.json", duplicate)
    with pytest.raises(ValueError, match="unknown node"):
        dag_viewer.write_payload(tmp_path / "unknown.json", unknown)


def test_the_fallback_offers_the_search_index_as_the_complete_graph_data() -> None:
    with_index = dag_viewer.render_container("chapter.json", search_href="../index.json")
    assert '<a href="../index.json">Download the complete graph data</a>' in with_index
    assert '<a href="chapter.json">Download the complete graph data</a>' in dag_viewer.render_container("chapter.json")


def test_an_explorer_page_encodes_a_literal_percent_in_its_payload_name(tmp_path: Path) -> None:
    """render_container keeps % for hrefs that arrive encoded, so the page encodes the payload's name."""
    page = tmp_path / "100%25 results.md"
    node = ViewNode("a", "A", "node", ("a",), (("planned", 1),))

    graph_pages._write_page(
        page,
        view=GraphView(kind="chapter", title="Results", nodes=(node,), edges=()),
        statuses={},
        links={},
        heading="Results",
        lead="One result.",
        site_root=tmp_path,
        search_href="index.json",
        breadcrumbs=(),
    )

    assert (tmp_path / "100%25 results.json").is_file()
    assert 'data-graph-src="100%2525%20results.json"' in page.read_text(encoding="utf-8")


def test_interactive_graph_urls_encode_authored_path_delimiters() -> None:
    container = dag_viewer.render_container(
        "hash#chapter.json",
        script_href="../javascripts/blueprint-dag.js?cache#asset",
    )

    assert 'data-graph-src="hash%23chapter.json"' in container
    assert 'src="../javascripts/blueprint-dag.js%3Fcache%23asset"' in container
