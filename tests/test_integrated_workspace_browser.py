"""Actual book/map workspace journeys, with no graph-client suppression.

The public synthetic consumer has eighteen authored topics, two authored areas
and two explicit source tracks. Its complete statements are longer than graph
previews. Lean fixture declarations are simple zero identities; these tests
exercise publication and UI behavior, not kernel verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import html5lib
import pytest

from autoform_cli.mathjax import MATHJAX_BUNDLE, MATHJAX_VERSION
from autoform_cli.render import render_site
from autoform_cli.scaffold import scaffold_project
from tests.test_integrated_explorer_browser import _PinnedRuntime, _math_menu_evidence, _math_source, _routes, _served, playwright


_LIBRARY = "Mathematical library"
_ANCESTOR = "Geometric configurations, foundational notation, and a deliberately long authored ancestor label"
_DEEP_TITLE = "Detailed identities, their complete explanations, and the surrounding elementary statements"
_TARGET = "division-00/part-01/results/main"
_OTHER = "division-09/main"
_ISOLATED = "division-00/part-01/results/isolated"
_READY = "division-00/part-01/results/ready"
_BASE = "division-00/part-01/results/base"
_AREAS = ("Geometry", "Analysis", "Unstarted mathematics")
_TARGET_TITLE = "Complete zero identity with full mathematical notation"
_OTHER_TITLE = "Counterpart zero identity"
_SOURCE_TITLES = ("Classical source essays", "Contemporary source records")
_TARGET_TEX = (r"0=0", r"\RR\ni0", r"\frac{0}{2}=0", r"\overline{0}=0", r"\sum_{i=1}^{n}0=0")
_LAST_SENTENCE = "This final mathematical sentence is retained in the complete reading page."
_CLUSTER_SCOPE = "wiki-clusters"
_CLUSTER_LINKS = (("a", "i"), ("i", "c"), ("b", "f"), ("d", "h"))
_CLUSTER_FAMILIES = (("a", "i", "c"), ("b", "f"), ("d", "h"), ("e",), ("g",))
_CLUSTER_END = "This complete elementary identity exposition remains present after graph navigation."


@dataclass(frozen=True)
class _WorkspaceSite:
    url: str
    records: dict[str, dict]
    root_graph: dict
    wiki_layer: dict | None = None
    wiki_pairs: frozenset[tuple[str, str]] = frozenset()

    def route(self, node_id: str) -> str:
        return urljoin(self.url + "/dependencies/index.json", self.records[node_id]["route"])


@dataclass(frozen=True)
class _WikiClusterSite:
    site: _WorkspaceSite
    owners: dict[str, str]
    graph: dict


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _area_edges(graph: dict) -> list[dict]:
    areas = {node["id"]: node.get("area") for node in graph["nodes"]}
    return [
        {"source": areas[edge["source"]], "target": areas[edge["target"]],
         "statement": edge["statement"], "proof": edge["proof"]}
        for edge in graph["edges"]
        if areas.get(edge["source"]) and areas.get(edge["target"]) and areas[edge["source"]] != areas[edge["target"]]
    ]


@pytest.fixture(scope="module")
def workspace_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WorkspaceSite]:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("integrated-workspace") / "project"
    scaffold_project(project, title="Synthetic mathematical workspace")
    blueprint = project / "blueprint"
    _write(blueprint / "tex-macros.json", '{"RR":"\\\\mathbb{R}"}')
    _write(blueprint / "README.md", "# Synthetic mathematical workspace\n\n- [Mathematical library](roadmap/README.md)\n")
    topics = [f"division-{index:02d}" for index in range(15)] + ["scope:authored", "boundary:authored", "source-notes"]
    links = "\n".join(f"- [Topic {index + 1}](./{topic}/README.md)" for index, topic in enumerate(topics))
    _write(blueprint / "roadmap/README.md", (
        "# " + _LIBRARY + "\n\nAn authored collection of mathematical topics and complete reading pages.\n\n"
        + links + "\n- [Classical source essays](appendix-a/README.md)\n"
        "- [Contemporary source records](literature-b/README.md)\n"
        "- [A topic without declarations](unstarted/README.md)\n"
    ))
    for index, topic in enumerate(topics):
        title = _ANCESTOR if index == 0 else (
            f"Topic {index + 1}: detailed mathematical foundations, examples, identities, and their complete explanations"
        )
        if topic == "source-notes":
            title = "Quantitative geometry, despite the source-looking directory name"
        area = "Geometry" if index < 9 or topic == "source-notes" else "Analysis"
        child = "part-01/README.md" if index == 0 else "main.md"
        _write(blueprint / f"roadmap/{topic}/README.md", (
            f"---\nnavigation: module\narea: {area}\n---\n\n# {title}\n\n"
            "This topic's name and area come from its authored content, not its directory.\n\n"
            f"- [Complete statements]({child})\n" + ("" if index == 0 else "- [Base](base.md)\n")
        ))
        group = f"{topic}/part-01/results" if index == 0 else topic
        if index == 0:
            _write(blueprint / "roadmap/division-00/part-01/README.md", (
                "---\nnavigation: transparent\n---\n\n# A transparent organizational layer\n\n"
                "- [Detailed identities](results/README.md)\n"
            ))
            _write(blueprint / f"roadmap/{group}/README.md", (
                "---\nnavigation: module\n---\n\n# " + _DEEP_TITLE + "\n\n"
                "Every statement below retains its full mathematical source.\n\n"
                "- [Base](base.md)\n- [Complete identity](main.md)\n"
                "- [Ready identity](ready.md)\n- [Independent identity](isolated.md)\n"
            ))
        _write(blueprint / f"roadmap/{group}/base.md", (
            "---\ndeclaration: def\nstatement: formalized\nlean: ReaderFixture.base\n---\n\n"
            "# Base value\n\nThe base value is zero.\n\n## Depends on\n\nNo prerequisites.\n"
        ))
        claim = "statement: formalized\nproof: formalized\nlean: ReaderFixture.zero_identity\n" if index == 0 else (
            "statement: formalized\nproof: formalized\nlean: ReaderFixture.counterpart_identity\n" if index == 9 else ""
        )
        result_title = _TARGET_TITLE if index == 0 else _OTHER_TITLE if index == 9 else f"Identity in topic {index + 1}"
        if index == 0:
            paragraphs = "\n\n".join(
                f"Explanation {n + 1}. The zero identity is stated explicitly, with every notation and reading paragraph retained. "
                "These complete mathematical paragraphs are not a graph preview."
                for n in range(12)
            )
            body = f"The proposition ${_TARGET_TEX[0]}$ holds.\n\n" + paragraphs + "\n\n"
            body += "\n\n".join(f"Notation: ${tex}$." for tex in _TARGET_TEX[1:-1])
            body += "\n\n$$\n" + _TARGET_TEX[-1] + "\n$$\n\n" + _LAST_SENTENCE
            body += "\n\n[Read the counterpart identity](../../../division-09/main.md).\n"
            body += "\n\n[Read the independent identity](isolated.md).\n"
        else:
            body = "The zero identity $0=0$ holds.\n\n" + "Its complete explanation remains available. " * 12
            if index == 9:
                body += "\n\nThe proof uses the complete zero identity from the geometric topic."
        proof_dependencies = (
            "\n\n## Proof depends on\n\n- [Complete zero identity](../division-00/part-01/results/main.md)\n"
            if index == 9 else ""
        )
        _write(blueprint / f"roadmap/{group}/main.md", (
            "---\ndeclaration: theorem\n" + claim + "---\n\n# " + result_title + "\n\n" + body
            + "\n\n## Depends on\n\n- [Base](base.md)\n"
            + proof_dependencies
        ))
        if index == 0:
            _write(blueprint / f"roadmap/{group}/ready.md", (
                "---\ndeclaration: theorem\nstatement: formalized\nlean: ReaderFixture.ready_identity\n---\n\n"
                "# Ready zero identity\n\nThe equality $0=0$ is stated.\n\n## Depends on\n\n- [Base](base.md)\n"
            ))
            _write(blueprint / f"roadmap/{group}/isolated.md", (
                "---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Independent zero identity\n\n"
                "This independent statement retains $0=0$ and $\\frac{0}{2}=0$ in its full reading page.\n\n"
                "[Read the complete zero identity](main.md).\n\n"
                "## Depends on\n\nNo prerequisites.\n"
            ))
    for name, title in zip(("appendix-a", "literature-b"), _SOURCE_TITLES):
        _write(blueprint / f"roadmap/{name}/README.md", (
            f"---\nnavigation: source\n---\n\n# {title}\n\n"
            "This is an authored source track, not a guessed topic.\n\n- [Reading note](note.md)\n"
        ))
        _write(blueprint / f"roadmap/{name}/note.md", "# Reading note\n\nComplete source commentary is retained.\n")
    _write(blueprint / "roadmap/unstarted/README.md", (
        "---\nnavigation: module\narea: Unstarted mathematics\n---\n\n"
        "# A reading topic without declarations\n\nThis authored topic currently has zero mathematical targets.\n\n"
        "- [Reading note](note.md)\n"
    ))
    _write(blueprint / "roadmap/unstarted/note.md", "# Reading note\n\nA narrative is not a declaration to formalize.\n")
    _write(project / "ReaderFixture.lean", (
        "namespace ReaderFixture\ndef base : Nat := 0\n"
        "theorem zero_identity : (0 : Nat) = 0 := rfl\n"
        "theorem counterpart_identity : (0 : Nat) = 0 := zero_identity\n"
        "theorem ready_identity : (0 : Nat) = 0 := rfl\nend ReaderFixture\n"
    ))
    render_site(blueprint, project / "site-src", lean_root=project)
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    configuration["extra_javascript"] = [
        entry for entry in configuration["extra_javascript"]
        if getattr(entry, "path", str(entry)) != MATHJAX_BUNDLE
    ]
    build(configuration)
    index_path = project / "site/dependencies/index.json"
    index_bytes = index_path.read_bytes()
    index = json.loads(index_bytes)
    records = {record["id"]: record for record in index["nodes"]}
    wiki_layer = json.loads((index_path.parent / "wiki-links.json").read_bytes())
    assert wiki_layer["schema"] == "autoform-wiki-links/v1"
    assert wiki_layer["index_sha256"] == hashlib.sha256(index_bytes).hexdigest()
    assert wiki_layer["availability"] == "complete" and wiki_layer["unavailable_sources"] == []
    identifiers = [record["id"] for record in index["nodes"]]
    wiki_pairs = frozenset((identifiers[source], identifiers[target]) for source, target in wiki_layer["references"])
    assert wiki_pairs == {(_TARGET, _OTHER), (_TARGET, _ISOLATED), (_ISOLATED, _TARGET)}
    root_path = index_path.parent / unquote(urlsplit(records["roadmap"]["data_url"]).path)
    root_graph = json.loads(root_path.read_text(encoding="utf-8"))
    exported = os.environ.get("AUTOFORM_UI_SITE_EXPORT_DIR")
    if exported:
        destination = Path(exported)
        shutil.copytree(project / "site", destination, dirs_exist_ok=True)
        runtime = (destination / "javascripts/blueprint-dag.js").read_bytes()
        fingerprint = re.match(rb"/\* autoform-explorer-source-sha256:([0-9a-f]{64}) \*/", runtime)
        assert fingerprint is not None, "the exported site did not carry its exact compiled source fingerprint"
        provenance = {
            "schema": "autoform-ci-site-provenance/v1",
            "consumer": "rich synthetic mathematical workspace",
            "candidate_sha": os.environ.get("AUTOFORM_CI_SOURCE_SHA"),
            "runtime_bytes": len(runtime),
            "runtime_sha256": hashlib.sha256(runtime).hexdigest(),
            "runtime_source_sha256": fingerprint.group(1).decode("ascii"),
            "root_nodes": len(root_graph["nodes"]),
            "root_edges": len(root_graph["edges"]),
            "area_edges": _area_edges(root_graph),
            "kernel_checked": False,
        }
        (destination / "ci-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    with _served(project / "site") as url:
        yield _WorkspaceSite(url, records, root_graph, wiki_layer, wiki_pairs)


@pytest.fixture(scope="module")
def navigation_overview_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WorkspaceSite]:
    """An authored five-area overview with navigation, but no mathematical prose to hide."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("navigation-only-overview") / "project"
    scaffold_project(project, title="Authored mathematical map")
    blueprint = project / "blueprint"
    areas = ("Algebra", "Analysis", "Discrete mathematics", "Geometry", "Logic")
    topics = [(area, f"topic-{number}-{part}") for number, area in enumerate(areas) for part in range(3)]
    _write(blueprint / "README.md", "# Authored mathematical map\n\n- [Mathematical map](roadmap/README.md)\n")
    _write(blueprint / "roadmap/README.md", "# Mathematical map\n\n## Topics\n\n" + "\n".join(
        f"- [{area} topic {topic[-1]}]({topic}/README.md)" for area, topic in topics
    ) + "\n")
    for number, area in enumerate(areas):
        for part in range(3):
            topic = f"topic-{number}-{part}"
            _write(blueprint / f"roadmap/{topic}/README.md", (
                f"---\nnavigation: module\narea: {area}\n---\n\n# {area} topic {part}\n\n"
                "A complete mathematical statement follows.\n\n- [Proposed identity](result.md)\n"
            ))
            previous = f"topic-{(number - 1) % len(areas)}-0"
            # Different theorem leaves induce a cyclic area aggregation; the
            # authored theorem graph itself must remain an acyclic blueprint.
            prerequisites = f"- [Previous identity](../{previous}/result.md)" if part else "No prerequisites."
            _write(blueprint / f"roadmap/{topic}/result.md", (
                "---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Proposed identity\n\n"
                "The equality $0=0$ is an unverified fixture target, not a claim of a Lean proof.\n\n"
                f"## Depends on\n\n{prerequisites}\n"
            ))
    render_site(blueprint, project / "site-src")
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    configuration["extra_javascript"] = [
        entry for entry in configuration["extra_javascript"]
        if getattr(entry, "path", str(entry)) != MATHJAX_BUNDLE
    ]
    build(configuration)
    index = json.loads((project / "site/dependencies/index.json").read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    graph = json.loads((project / "site/dependencies.json").read_text(encoding="utf-8"))
    assert len(graph["nodes"]) == 15 and len(_area_edges(graph)) == 10
    with _served(project / "site") as url:
        yield _WorkspaceSite(url, records, graph)


@pytest.fixture(scope="module")
def wiki_cluster_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WikiClusterSite]:
    """Nine independent base→identity developments with four authored prose links.

    These are deliberately unfinished educational identities, not kernel
    certification. No cross-topic formal prerequisite is invented for layout.
    """
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("native-wiki-clusters") / "project"
    scaffold_project(project, title="Independent elementary concept links")
    blueprint = project / "blueprint"
    _write(blueprint / "README.md", "# Elementary comparisons\n\n- [Mathematics](roadmap/README.md)\n")
    _write(blueprint / "roadmap/README.md", f"# Elementary comparisons\n\n- [Identities]({_CLUSTER_SCOPE}/README.md)\n")
    _write(blueprint / f"roadmap/{_CLUSTER_SCOPE}/README.md", (
        "---\nnavigation: module\n---\n\n# Linked zero identities\n\n"
        "Each development defines its own zero constant and states its own identity. "
        "Ordinary exposition links compare independent developments; they are not proof prerequisites.\n\n"
        + "\n".join(f"- [Identity {letter.upper()}]({letter}/README.md)" for letter in "abcdefghi") + "\n"
    ))
    declarations = []
    for letter in "abcdefghi":
        topic = f"{_CLUSTER_SCOPE}/{letter}"
        _write(blueprint / f"roadmap/{topic}/README.md", (
            f"---\nnavigation: module\n---\n\n# Identity {letter.upper()}\n\n"
            "This development keeps its definition, statement and educational proof obligation in full.\n\n"
            "- [Zero constant](base.md)\n- [Zero identity](identity.md)\n"
        ))
        _write(blueprint / f"roadmap/{topic}/base.md", (
            f"---\ndeclaration: def\nstatement: formalized\nlean: ClusterFixture.base_{letter}\n---\n\n"
            f"# Zero constant {letter.upper()}\n\nThe constant $b_{letter}=0$ is defined to be zero.\n\n"
            "## Depends on\n\nNo prerequisites.\n"
        ))
        exposition = "\n\n".join(
            f"Explanation {number + 1}. The constant in development {letter.upper()} is zero. "
            "Its identity is stated explicitly and its proof remains an educational obligation. "
            "This complete explanation is retained independently of any bounded graph preview."
            for number in range(6)
        )
        comparisons = "\n\n".join(
            f"Compare the independent exposition [Identity {other.upper()}](../{other}/identity.md). "
            "This ordinary concept link asserts no additional formal prerequisite."
            for source, other in _CLUSTER_LINKS if source == letter
        )
        _write(blueprint / f"roadmap/{topic}/identity.md", (
            f"---\ndeclaration: theorem\nnot_ready: true\nlean: ClusterFixture.identity_{letter}\n---\n\n"
            f"# Zero identity {letter.upper()}\n\nThe proposed identity is $b_{letter}=0$.\n\n"
            + exposition + "\n\n" + _CLUSTER_END + "\n\n" + comparisons
            + "\n\n## Depends on\n\n- [Zero constant](base.md)\n"
        ))
        declarations += [f"def base_{letter} : Nat := 0", f"theorem identity_{letter} : base_{letter} = 0 := by sorry"]
    _write(project / "ClusterFixture.lean", "namespace ClusterFixture\n" + "\n".join(declarations) + "\nend ClusterFixture\n")
    rendered = render_site(blueprint, project / "site-src", lean_root=project)
    assert not rendered.unresolved and not rendered.unchecked
    config = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    config["extra_javascript"] = [entry for entry in config["extra_javascript"]
                                  if getattr(entry, "path", str(entry)) != MATHJAX_BUNDLE]
    build(config)
    index_path = project / "site/dependencies/index.json"
    index_bytes = index_path.read_bytes()
    index = json.loads(index_bytes)
    records = {node["id"]: node for node in index["nodes"]}
    identifiers = [node["id"] for node in index["nodes"]]
    layer = json.loads((index_path.parent / "wiki-links.json").read_bytes())
    assert layer["schema"] == "autoform-wiki-links/v1"
    assert layer["index_sha256"] == hashlib.sha256(index_bytes).hexdigest()
    assert layer["availability"] == "complete" and layer["unavailable_sources"] == []
    pairs = frozenset((identifiers[source], identifiers[target]) for source, target in layer["references"])
    assert pairs == {(f"{_CLUSTER_SCOPE}/{source}/identity", f"{_CLUSTER_SCOPE}/{target}/identity")
                     for source, target in _CLUSTER_LINKS}
    formal = {(identifiers[source], identifiers[target], statement, proof)
              for source, target, statement, proof in index["relations"]}
    assert formal == {(f"{_CLUSTER_SCOPE}/{letter}/base", f"{_CLUSTER_SCOPE}/{letter}/identity", 1, 0)
                      for letter in "abcdefghi"}
    graph_path = index_path.parent / unquote(urlsplit(records[_CLUSTER_SCOPE]["data_url"]).path)
    graph = json.loads(graph_path.read_bytes())
    assert len(graph["nodes"]) == 9 and graph["edges"] == []
    routes = {urljoin(index_path.as_uri(), record["route"]): node_id for node_id, record in records.items()}
    owners = {}
    for node in graph["nodes"]:
        original = routes[urljoin(graph_path.as_uri(), node["url"])]
        assert original in {f"{_CLUSTER_SCOPE}/{letter}" for letter in "abcdefghi"}
        letter = original.rsplit("/", 1)[-1]
        owners[letter] = node["id"]
        assert node["title"] == f"Identity {letter.upper()}" and node["member_count"] == 2
        assert dict(node["status_counts"]) == {"fully_proved": 1, "not_ready": 1}
        assert node["internal_dependency_count"] == 1
    assert set(owners) == set("abcdefghi")
    with _served(project / "site") as url:
        yield _WikiClusterSite(_WorkspaceSite(url, records, graph, layer, pairs), owners, graph)


@pytest.fixture(scope="module")
def workspace_browser() -> Iterator[object]:
    required = os.environ.get("AUTOFORM_REQUIRE_BROWSER_TESTS") == "1"
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.webkit.launch()
        except playwright.Error as error:
            if required or "Executable doesn't exist" not in str(error):
                raise
            pytest.skip("WebKit is not installed")
        yield browser
        browser.close()


@pytest.fixture()
def workspace_page(workspace_browser: object) -> Iterator[object]:
    context = workspace_browser.new_context(viewport={"width": 1600, "height": 900}, reduced_motion="reduce")
    page = context.new_page()
    page.set_default_timeout(30_000)
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        yield page
    finally:
        context.close()
    assert errors == []


@pytest.fixture(scope="module")
def workspace_runtime() -> _PinnedRuntime:
    return _PinnedRuntime()


def _frames(page: object) -> None:
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def _browse_contents(page: object, action: str) -> None:
    """Choose the real statement or source inventory through Browse."""
    toggle = page.get_by_role("button", name="Browse statements or source texts", exact=True)
    popover = page.locator(".bp-dag-browse-popover")
    playwright.expect(popover).to_be_hidden()
    toggle.click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "true")
    playwright.expect(popover).to_be_visible()
    popover.get_by_role("button", name=action, exact=True).click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    playwright.expect(popover).to_be_hidden()


def _peek_reader(page: object, handle: object) -> None:
    """Tap the reader's handle down to its peek: a phone cycles half, full and peek, so two taps at most, then it must be closed."""
    for _ in range(2):
        if handle.get_attribute("aria-expanded") != "true":
            break
        handle.click()
        _frames(page)
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")


def _focus_graph_node(page: object, target_id: str) -> object:
    """Reach a map control with real keys, without relying on grid coordinates."""
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    # A phone's taps cycle half, full and peek, so the map is clear once the reader is closed.
    if page.viewport_size["width"] <= 720:
        _peek_reader(page, handle)
    pending: list[tuple[str, ...]] = [()]
    visited: set[str] = set()
    while pending and len(visited) <= 32:
        path = pending.pop(0)
        page.get_by_role("button", name="Fit shown nodes", exact=True).click()
        _frames(page)
        start = page.locator('.bp-dag-node[tabindex="0"]').first
        before_focus = start.evaluate("""node => {
          const box=node.getBoundingClientRect(),layer=node.closest('.bp-dag-node-layer');
          return {id:node.dataset.autoformNodeId,box:{left:box.left,top:box.top,right:box.right,bottom:box.bottom},
            windowScroll:[scrollX,scrollY],layerScroll:layer&&[layer.scrollLeft,layer.scrollTop]};
        }""")
        start.focus()
        for key in path:
            page.keyboard.press(key)
            _frames(page)
        active = page.locator(".bp-dag-node:focus")
        node_id = active.get_attribute("data-autoform-node-id")
        if node_id == target_id:
            diagnostic = active.evaluate("""node => {
              function box(element) {if(!element)return null;const r=element.getBoundingClientRect();
                return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height};}
              const bounds=box(node),x=bounds.left+bounds.width/2,y=bounds.top+bounds.height/2,hit=document.elementFromPoint(x,y);
              const ancestors=[];
              for(let element=node.parentElement;element&&ancestors.length<8;element=element.parentElement) {
                const style=getComputedStyle(element);ancestors.push({tag:element.tagName,class:element.className,
                  box:box(element),scroll:[element.scrollLeft,element.scrollTop],overflow:[style.overflowX,style.overflowY],
                  clipPath:style.clipPath,transform:style.transform,position:style.position});
              }
              const pane=document.querySelector('.bp-reading-pane'),handle=pane&&pane.querySelector('.bp-dag-sheet-handle');
              return {hit:node.contains(hit),id:node.dataset.autoformNodeId,box:bounds,center:[x,y],
                hitElement:hit&&{tag:hit.tagName,id:hit.id,class:hit.className},ancestors,
                viewport:{width:innerWidth,height:innerHeight,scroll:[scrollX,scrollY],
                  visual:window.visualViewport&&{width:window.visualViewport.width,height:window.visualViewport.height,left:window.visualViewport.offsetLeft,top:window.visualViewport.offsetTop}},
                reading:{box:box(pane),expanded:handle&&handle.getAttribute('aria-expanded')}};
            }""")
            diagnostic.update(before_focus=before_focus, keys=list(path))
            assert diagnostic["hit"] is True, json.dumps(diagnostic, indent=2)
            return active
        if node_id in visited:
            continue
        visited.add(node_id)
        pending.extend((*path, key) for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"))
    pytest.fail(f"Authored graph node is not keyboard-reachable: {target_id}")


def _focus_graph_region(page: object, area: str) -> object:
    return _focus_graph_node(page, f"region:{area}")


def _observe_dependency_routes(page: object) -> None:
    """Record actual native canvas strokes while leaving every rendering call intact."""
    page.add_init_script("""(() => {
      const prototype=CanvasRenderingContext2D.prototype, paths=new WeakMap();
      window.__autoformCanvasRoutes=[];window.__autoformCanvasClosedFills=[];
      for(const method of ['beginPath','moveTo','lineTo','quadraticCurveTo','bezierCurveTo','closePath']) {
        const original=prototype[method];
        prototype[method]=function(...args) {
          if(method==='beginPath') paths.set(this,[]);
          else if(paths.has(this)) paths.get(this).push({method,args});
          return original.apply(this,args);
        };
      }
      const clear=prototype.clearRect, stroke=prototype.stroke, fill=prototype.fill;
      prototype.clearRect=function(...args) {
        if(this.canvas.classList.contains('bp-dag-canvas')) {window.__autoformCanvasRoutes=[];window.__autoformCanvasClosedFills=[];}
        return clear.apply(this,args);
      };
      prototype.stroke=function(...args) {
        const path=paths.get(this);
        if(this.canvas.classList.contains('bp-dag-canvas') && path && path.length &&
            !path.some(command=>command.method==='closePath')) {
          const dashes=this.getLineDash(),ratio=dashes.length>1?dashes[0]/dashes[1]:NaN;
          const kind=!dashes.length?'statement':Math.abs(ratio-.3)<1e-6?'wiki':Math.abs(ratio-1.5)<1e-6?'proof':'other';
          window.__autoformCanvasRoutes.push({commands:path.slice(),dashes:dashes.slice(),kind,dashed:kind==='proof'});
        }
        return stroke.apply(this,args);
      };
      prototype.fill=function(...args) {
        const path=paths.get(this);
        if(this.canvas.classList.contains('bp-dag-canvas')&&path&&path.some(command=>command.method==='closePath'))
          window.__autoformCanvasClosedFills.push(path.slice());
        return fill.apply(this,args);
      };
    })();""")


def _cluster_native_box(control: object) -> dict:
    return control.evaluate("""node => {
      const r=node.getBoundingClientRect(),canvas=node.closest('.bp-dag-stage').querySelector('canvas').getBoundingClientRect();
      const matrix=new DOMMatrix(getComputedStyle(node).transform),title=node.querySelector('.bp-dag-node-title'),meta=node.querySelector('.bp-dag-node-meta');
      return {id:node.dataset.autoformNodeId,x:r.x-canvas.x,y:r.y-canvas.y,w:r.width,h:r.height,scale:matrix.a,
        screen:[r.x,r.y,r.width,r.height],title:title.textContent,meta:meta.textContent,
        titlePx:parseFloat(getComputedStyle(title).fontSize)*matrix.a,metaPx:parseFloat(getComputedStyle(meta).fontSize)*matrix.a,
        hit:node.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))};
    }""")


def _cluster_segment_hits(a: list[float], b: list[float], box: dict) -> bool:
    """Intersect an observed native straight segment with a card's interior."""
    enter, leave = 0.0, 1.0
    for axis, low, extent in ((0, box["x"] + .25, box["w"] - .5), (1, box["y"] + .25, box["h"] - .5)):
        delta = b[axis] - a[axis]
        if abs(delta) < 1e-10:
            if not low < a[axis] < low + extent:
                return False
        else:
            limits = sorted(((low - a[axis]) / delta, (low + extent - a[axis]) / delta))
            enter, leave = max(enter, limits[0]), min(leave, limits[1])
        if enter >= leave - 1e-8:
            return False
    return leave > 1e-8 and enter < 1 - 1e-8


def _cluster_readable_owner(page: object, identifier: str) -> object:
    control = _focus_graph_node(page, identifier)
    observed = control.get_attribute("data-autoform-node-id")
    assert observed == identifier
    # The keyboard helper returns :focus. Retain the observed card's identity
    # before a native canvas pan transfers focus away from that card.
    control = page.locator(f'.bp-dag-node[data-autoform-node-id="{observed}"]')
    # Keyboard centering and its tabstop paint finish on separate frames.
    # Observe the actual usable control, not a fixed number of rAF callbacks.
    page.wait_for_function("""id => {
      const node=Array.from(document.querySelectorAll('.bp-dag-node')).find(node=>node.dataset.autoformNodeId===id);
      if(!node||document.activeElement!==node||node.tabIndex!==0)return false;
      const r=node.getBoundingClientRect(),s=node.closest('.bp-dag-stage').getBoundingClientRect();
      return r.left>=s.left+12&&r.right<=s.right-12&&r.top>=s.top+12&&r.bottom<=s.bottom-12&&
        node.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
    }""", arg=identifier, timeout=5000)
    box = _cluster_native_box(control)
    assert box["scale"] >= .9 - 1e-6 and box["titlePx"] >= 15 and box["metaPx"] >= 12, box
    assert box["hit"] is True
    return control


def _cluster_pan(page: object) -> None:
    point = page.locator(".bp-dag-stage").evaluate("""stage => {
      const r=stage.getBoundingClientRect(),canvas=stage.querySelector('canvas');
      for(const fx of [.08,.92,.5])for(const fy of [.75,.5,.25]) {
        const x=r.x+r.width*fx,y=r.y+r.height*fy;
        if(document.elementFromPoint(x,y)===canvas)return {x,y};
      } throw new Error('No native canvas background is available for panning');
    }""")
    page.mouse.move(point["x"], point["y"])
    page.mouse.down()
    page.mouse.move(point["x"] + 18, point["y"] + 12, steps=4)
    page.mouse.up()
    _frames(page)


def _cluster_capture(page: object, width: int, phase: str, evidence: dict | None = None) -> None:
    folder = os.environ.get("AUTOFORM_UI_SCREENSHOT_DIR")
    if folder:
        directory = Path(folder)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"wiki-clusters-{width}-{phase}.png"))
        if evidence is not None:
            (directory / f"wiki-clusters-{width}-{phase}.json").write_text(
                json.dumps(evidence, indent=2) + "\n", encoding="utf-8",
            )


def _open(
    page: object, site: _WorkspaceSite, runtime: _PinnedRuntime, node_id: str = "roadmap",
    *, wiki_route: Callable[[object], None] | None = None,
) -> None:
    _routes(page, site.url, runtime, graph_client=True)
    if wiki_route is not None:
        # Register after the normal catch-all, retaining the real server's
        # renderer-produced asset for a delayed-response or transport-fault test.
        page.route(site.url + "/dependencies/wiki-links.json", wiki_route)
    page.goto(site.route(node_id))
    page.locator(".bp-dag-integrated .bp-dag-stage").wait_for()
    playwright.expect(page.locator(".bp-reading-body > .bp-reading-source")).to_have_count(1)
    # The placeholder says what search matches; aria-busy clearing says its index has loaded.
    search = page.locator(".bp-dag-search")
    playwright.expect(search).to_have_attribute("placeholder", "Search titles and Lean names")
    playwright.expect(search).not_to_have_attribute("aria-busy", "true")
    page.evaluate("() => window.__workspaceDocument = document")
    _frames(page)


def _selected(page: object, node_id: str) -> object:
    control = page.locator(f'.bp-dag-node[data-autoform-node-id="{node_id}"][data-selected="true"]')
    playwright.expect(control).to_be_visible()
    return control


def _full_math(page: object, node_id: str = _TARGET, count: int = 5) -> object:
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-reading-body'))") is True
    card = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{node_id}"]')
    playwright.expect(card).to_have_count(1)
    playwright.expect(card.locator(".arithmatex")).to_have_count(count)
    playwright.expect(card.locator("mjx-container")).to_have_count(count)
    assert card.locator("mjx-merror, merror").count() == 0
    return card


def _search(page: object, query: str, node_id: str) -> None:
    search = page.locator(".bp-dag-search")
    search.fill(query)
    playwright.expect(page.locator(f'.bp-dag-result[data-result-id="{node_id}"]')).to_be_visible()
    search.press("Enter")


def _pixels(page: object) -> dict:
    return page.locator(".bp-dag-canvas").evaluate("""canvas => {
      const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      let painted = 0; for (let n = 3; n < pixels.length; n += 4) if (pixels[n]) painted++;
      return {width: canvas.width, height: canvas.height, painted};
    }""")


def _surface(page: object) -> dict:
    return page.evaluate("""() => {
      const selected = document.querySelector('.bp-dag-node[data-selected=true]');
      const matrix = selected && new DOMMatrix(getComputedStyle(selected).transform);
      return {camera: matrix && [matrix.a, matrix.d, matrix.e, matrix.f],
        statuses: Array.from(document.querySelectorAll('.bp-dag-filter-popover input[value]:checked')).map(input => input.value).sort(),
        reading: document.querySelector('.bp-reading-body').scrollTop,
        outline: document.querySelector('.bp-dag-navigation').scrollTop};
    }""")


def _assert_surface(actual: dict, expected: dict) -> None:
    assert actual["statuses"] == expected["statuses"]
    assert actual["reading"] == pytest.approx(expected["reading"], abs=1)
    assert actual["outline"] == pytest.approx(expected["outline"], abs=1)
    assert actual["camera"] == pytest.approx(expected["camera"], abs=.5)


def test_wide_workspace_has_three_nonoverlapping_panes_complete_math_and_one_h1(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    card = _full_math(page)
    assert len(card.inner_text()) > 420
    assert _LAST_SENTENCE in card.inner_text()
    playwright.expect(page.get_by_role("heading", level=1)).to_have_count(1)
    playwright.expect(page.locator(".bp-explorer-page > .bp-reading-source")).to_have_count(0)
    nav = page.locator(".bp-dag-navigation").bounding_box()
    graph = page.locator(".bp-dag-stage").bounding_box()
    reading = page.locator(".bp-reading-pane").bounding_box()
    assert nav and graph and reading
    assert nav["x"] + nav["width"] <= graph["x"] + 1
    assert graph["x"] + graph["width"] <= reading["x"] + 1
    assert page.locator(".bp-dag-stage").evaluate("""stage => {
      const rect = stage.getBoundingClientRect(), hit = document.elementFromPoint(rect.x + rect.width/2, rect.y + rect.height/2);
      return stage.contains(hit);
    }""") is True
    viewer = page.locator(".bp-dag-viewer").bounding_box()
    assert viewer and viewer["x"] == pytest.approx(0, abs=1) and viewer["y"] == pytest.approx(0, abs=1)
    assert viewer["width"] == pytest.approx(1600, abs=1) and viewer["height"] == pytest.approx(900, abs=1)
    assert _pixels(page)["painted"] > 0
    assert _math_source(page, card.locator("mjx-container").last) == _TARGET_TEX[-1]


@pytest.mark.parametrize("width", [1600, 1000, 390])
def test_empty_authored_overview_opens_on_the_map_and_restores_its_reading_and_camera(
    workspace_page: object, navigation_overview_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int,
) -> None:
    page, site = workspace_page, navigation_overview_site
    page.set_viewport_size({"width": width, "height": 900 if width > 720 else 844})
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime)
    page.locator(".bp-dag-overview-only").wait_for()
    # The same retained control is visible on the collapsible overview, but a
    # wide theorem reader intentionally hides it. Inspect that state by DOM,
    # while keeping native click/visibility assertions on the overview itself.
    handle = page.locator(".bp-reading-pane > .bp-dag-sheet-handle")
    inner = page.locator(".bp-reading-pane .bp-dag-inspector-inner")
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    playwright.expect(inner).to_have_attribute("inert", "")
    playwright.expect(inner).to_have_attribute("aria-hidden", "true")
    assert page.locator(".bp-dag-stage").evaluate("""stage => {
      const box=stage.getBoundingClientRect(); return stage.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2));
    }""") is True
    controls = page.locator('.bp-dag-node[data-autoform-node-id^="region:"]')
    if width == 1600:
        playwright.expect(controls).to_have_count(5)
        assert controls.evaluate_all("""nodes => nodes.every(node => {
          const box=node.getBoundingClientRect(),stage=node.closest('.bp-dag-stage').getBoundingClientRect();
          return box.left>=stage.left+12&&box.right<=stage.right-12&&box.top>=stage.top+12&&box.bottom<=stage.bottom-12;
        })""") is True, "The initial five-area desktop overview should not need Fit or panning"
    source = page.locator(".bp-reading-body > .bp-reading-source")
    root_text = source.text_content()
    assert all(f"{area} topic" in root_text for area in ("Algebra", "Analysis", "Discrete mathematics", "Geometry", "Logic"))
    assert _pixels(page)["painted"] > 0
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    assert inner.get_attribute("inert") is None and inner.get_attribute("aria-hidden") is None
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text("Mathematical map")
    playwright.expect(handle).to_be_visible()
    if width <= 720:
        # A phone reader opens at half; the next tap covers the map and the one after returns to the peek, as the title says.
        playwright.expect(handle).to_have_attribute("title", "Expand reading")
        handle.click()
        playwright.expect(page.locator(".bp-dag-viewer")).to_have_class(re.compile(r"\bbp-dag-sheet-full\b"))
    playwright.expect(handle).to_have_attribute("title", "Close reading")
    handle.click()
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    _frames(page)
    camera = controls.first.evaluate("node => getComputedStyle(node).transform")
    target = "topic-0-0/result"
    _search(page, target, target)
    page.wait_for_url(site.route(target))
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    _full_math(page, target, 1)
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route("roadmap"))
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    _frames(page)
    assert controls.first.evaluate("node => getComputedStyle(node).transform") == camera
    assert source.text_content() == root_text
    assert page.evaluate("() => document === __workspaceDocument") is True
    assert len(documents) == 1


@pytest.mark.parametrize("width", [1280, 1600, 390])
def test_far_right_genuine_math_menu_and_hover_submenu_stay_inside_the_workspace(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": width, "height": 900 if width > 720 else 844})
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    formula = _full_math(page).locator("mjx-container").last
    playwright.expect(formula).to_have_attribute("display", "true")
    assert page.evaluate("() => MathJax.version") == MATHJAX_VERSION
    formula.scroll_into_view_if_needed()
    _frames(page)
    box = formula.bounding_box()
    assert box and box["width"] > 4

    def scrolls() -> dict:
        return page.evaluate("""() => {
          const reader = document.querySelector('.bp-reading-body');
          return {window:[scrollX,scrollY],body:[document.body.scrollLeft,document.body.scrollTop],
            document:[document.documentElement.scrollLeft,document.documentElement.scrollTop],
            reader:[reader.scrollLeft,reader.scrollTop]};
        }""")

    before = scrolls()
    # Native contextmenu on the existing complete formula block, at its
    # rightmost interior point. No moved DOM, synthetic event or forced click.
    formula.click(button="right", position={"x": box["width"] - 2, "y": box["height"] / 2})
    try:
        _math_menu_evidence(page, f"workspace-{width}-root")
        playwright.expect(page.locator(".CtxtMenu_MenuFrame")).to_have_count(1)
        menus = page.get_by_role("menu")
        root = menus.first.bounding_box()
        assert root and root["x"] >= 0 and root["y"] >= 0
        # Phone display math keeps its natural centered width. Prove this
        # actual right-click would overflow using the real menu's width and
        # the pinned library's 5px anchor offset, not a fixed page inset.
        assert box["x"] + box["width"] - 2 - 5 + root["width"] > width
        assert root["x"] + root["width"] <= width
        assert root["y"] + root["height"] <= page.viewport_size["height"]
        show = page.get_by_role("menuitem", name=re.compile(r"^Show Math As(?:\s*\u25ba)?$"))
        show.hover()
        command = page.get_by_role("menuitem", name="TeX Commands", exact=True)
        playwright.expect(command).to_be_visible()
        assert menus.count() >= 2
        _math_menu_evidence(page, f"workspace-{width}-submenu")
        for index in range(menus.count()):
            bounds = menus.nth(index).bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["y"] >= 0
            assert bounds["x"] + bounds["width"] <= width
            assert bounds["y"] + bounds["height"] <= page.viewport_size["height"]
        assert show.evaluate("""item => {
          const box = item.getBoundingClientRect();
          return item.contains(document.elementFromPoint(box.x+box.width/2,box.y+box.height/2));
        }""") is True
        command.click()
        source = page.locator(".CtxtMenu_Info pre")
        playwright.expect(source).to_be_visible()
        assert source.inner_text() == _TARGET_TEX[-1]
        _math_menu_evidence(page, f"workspace-{width}-source")
        page.get_by_role("button", name="Close Dialog Box", exact=True).click()
    except Exception:
        _math_menu_evidence(page, f"workspace-{width}-failed")
        raise
    _frames(page)
    assert scrolls() == before
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.url == site.route(_TARGET)
    assert page.evaluate("() => document === __workspaceDocument") is True


@pytest.mark.parametrize("query", [_TARGET_TITLE, "ReaderFixture.zero_identity"])
def test_global_title_and_lean_search_select_and_read_in_one_action_without_document_navigation(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, query: str,
) -> None:
    page, site = workspace_page, workspace_site
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime)
    _search(page, query, _TARGET)
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    playwright.expect(page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{_TARGET}"]')).to_contain_text(_TARGET_TITLE)
    _full_math(page)
    assert documents == [site.route("roadmap")]
    assert page.evaluate("() => document === __workspaceDocument") is True
    assert page.locator(".bp-dag-viewer").count() == 1


def test_native_anchor_boot_after_delayed_index_selects_and_centers_the_real_theorem(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _routes(page, site.url, workspace_runtime, graph_client=True)
    held: list[object] = []
    page.route(site.url + "/dependencies/index.json", lambda route: held.append(route))
    page.goto(site.route(_TARGET), wait_until="domcontentloaded")
    playwright.expect(page.locator(".bp-explorer-page > .bp-reading-source")).to_be_visible()
    assert page.evaluate("() => AutoformMath.refresh(document)") is True
    assert held
    for route in held:
        route.continue_()
    node = _selected(page, _TARGET)
    _full_math(page)
    _frames(page)
    box, stage = node.bounding_box(), page.locator(".bp-dag-stage").bounding_box()
    assert box and stage
    assert abs(box["x"] + box["width"] / 2 - stage["x"] - stage["width"] / 2) < stage["width"] * .15
    assert abs(box["y"] + box["height"] / 2 - stage["y"] - stage["height"] / 2) < stage["height"] * .15


def test_back_and_forward_restore_filter_camera_reading_and_outline_scroll(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    page.locator(".bp-dag-filter-summary").click()
    status = site.records[_READY]["status"]
    assert status != site.records[_TARGET]["status"]
    page.locator(f'.bp-dag-filter-popover input[value="{status}"]').uncheck()
    page.keyboard.press("Escape")
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert stage
    page.mouse.move(stage["x"] + stage["width"] / 2, stage["y"] + stage["height"] / 2)
    page.mouse.wheel(57, 39)
    page.locator(".bp-reading-body").evaluate("reader => reader.scrollTop = 260")
    page.locator(".bp-dag-navigation").evaluate("outline => outline.scrollTop = 320")
    page.locator(".bp-dag-search").fill(_OTHER_TITLE)
    playwright.expect(page.locator(f'.bp-dag-result[data-result-id="{_OTHER}"]')).to_be_visible()
    _frames(page)
    first = _surface(page)
    assert first["reading"] > 0 and first["outline"] > 0
    page.locator(f'.bp-dag-result[data-result-id="{_OTHER}"]').click()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _frames(page)
    second = _surface(page)
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    _frames(page)
    _assert_surface(_surface(page), first)
    page.evaluate("() => history.forward()")
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _frames(page)
    _assert_surface(_surface(page), second)
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_focused_library_link_survives_1000_to_1280_resize_and_canvas_repaints(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 1000, "height": 900})
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    link = page.locator(".bp-dag-book")
    link.focus()
    link.evaluate("element => window.__focusedWorkspaceBook = element")
    before = _pixels(page)
    assert before["painted"] > 0
    page.set_viewport_size({"width": 1280, "height": 900})
    _frames(page)
    assert page.evaluate("() => document.querySelector('.bp-dag-book') === __focusedWorkspaceBook && document.activeElement === __focusedWorkspaceBook") is True
    after = _pixels(page)
    assert (after["width"], after["height"]) != (before["width"], before["height"])
    assert after["painted"] > 0, "the resized canvas lost its painted graph"


def test_closed_phone_reader_resized_to_desktop_is_visible_and_interactive(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": 390, "height": 844})
    _open(page, site, workspace_runtime, _TARGET)
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    playwright.expect(handle).to_have_attribute("aria-expanded", "true")
    # Half, then full, then the peek.
    handle.click()
    handle.click()
    inner = page.locator(".bp-reading-pane .bp-dag-inspector-inner")
    playwright.expect(inner).to_have_attribute("inert", "")
    playwright.expect(inner).to_have_attribute("aria-hidden", "true")
    page.set_viewport_size({"width": 1600, "height": 900})
    _frames(page)
    playwright.expect(page.locator(".bp-reading-pane")).to_be_visible()
    assert inner.get_attribute("inert") is None and inner.get_attribute("aria-hidden") is None
    assert page.locator(".bp-reading-body").evaluate("""reader => {
      const box = reader.getBoundingClientRect(); return reader.contains(document.elementFromPoint(box.x+box.width/2, box.y+box.height/2));
    }""") is True
    _full_math(page)


def test_hiding_selected_status_does_not_hide_or_inert_the_desktop_reader(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    page.locator(".bp-dag-filter-summary").click()
    status = site.records[_TARGET]["status"]
    page.locator(f'.bp-dag-filter-popover input[value="{status}"]').uncheck()
    playwright.expect(page.locator('.bp-dag-node[data-selected="true"]')).to_have_count(0)
    inner = page.locator(".bp-reading-pane .bp-dag-inspector-inner")
    assert inner.get_attribute("inert") is None and inner.get_attribute("aria-hidden") is None
    playwright.expect(page.locator(".bp-reading-pane")).to_be_visible()
    _full_math(page)


@pytest.mark.parametrize("width", [1600, 1000, 390])
def test_long_ancestor_labels_and_full_reading_heading_stay_within_their_panes(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": width, "height": 900 if width > 720 else 844})
    _open(page, site, workspace_runtime, _TARGET)
    heading = page.locator(".bp-reading-body h1")
    playwright.expect(heading).to_have_text(_DEEP_TITLE)
    assert heading.evaluate("heading => heading.scrollWidth <= heading.clientWidth + 1") is True
    assert page.locator(".bp-dag-head").evaluate("head => head.scrollWidth <= head.clientWidth + 1") is True
    # The four-deep trail reads first / … / parent / current; the "…" names the ancestor it hides.
    crumbs = page.locator(".bp-dag-breadcrumb li")
    playwright.expect(crumbs).to_have_count(4)
    playwright.expect(crumbs.nth(0)).to_have_text(_LIBRARY)
    playwright.expect(crumbs.nth(1)).to_have_attribute("title", _ANCESTOR)
    playwright.expect(crumbs.nth(1).locator(".bp-dag-sr-only")).to_have_text(_ANCESTOR)
    playwright.expect(crumbs.nth(3)).to_have_text(_TARGET_TITLE)
    playwright.expect(crumbs.nth(3)).to_have_attribute("aria-current", "page")
    if width <= 720:
        # A phone keeps the first crumb, the parent and the current page; the "…" gives way.
        playwright.expect(crumbs.nth(1)).to_be_hidden()
        playwright.expect(crumbs.nth(2)).to_be_visible()
    else:
        playwright.expect(crumbs.nth(1)).to_be_visible()
        playwright.expect(crumbs.nth(2)).to_have_text(_DEEP_TITLE)
        # The trail has the row to itself, rather than a third of it.
        trail_box, head_box = page.locator(".bp-dag-breadcrumb").bounding_box(), page.locator(".bp-dag-head").bounding_box()
        assert trail_box is not None and head_box is not None and trail_box["width"] >= head_box["width"] / 2
    if width == 1000:
        # A tablet gives Topics and search the first row and filter, Browse and zoom the second, rather than three rows.
        boxes = page.locator(".bp-dag-toolbar").evaluate(
            "bar => [...bar.children].filter(child => child.getClientRects().length)"
            ".map(child => { const box = child.getBoundingClientRect(); return [box.top, box.bottom]; })"
        )
        first, second = boxes[:2], boxes[2:]
        assert max(top for top, _ in first) < min(bottom for _, bottom in first), boxes
        assert max(top for top, _ in second) < min(bottom for _, bottom in second), boxes
        assert min(top for top, _ in second) >= max(bottom for _, bottom in first), boxes
    if width <= 1100:
        page.get_by_role("button", name="Toggle topic outline", exact=True).click()
    outline = page.locator(".bp-dag-navigation")
    playwright.expect(outline).to_be_visible()
    ancestor = outline.locator('[data-navigation-node-id="division-00"]')
    playwright.expect(ancestor).to_have_text(_ANCESTOR)
    assert ancestor.get_attribute("title") == _ANCESTOR
    assert ancestor.evaluate("link => link.scrollWidth <= link.clientWidth + 1") is True
    assert "A transparent organizational layer" not in outline.inner_text()


def test_authored_sources_results_and_areas_have_distinct_real_views(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime)
    # The map offers no way back to itself, and Browse marks only the list on screen, so at most one control reads as pressed.
    pressed = page.get_by_role("group", name="Dependency explorer controls", exact=True).locator('[aria-pressed="true"]')
    playwright.expect(pressed).to_have_count(0)
    playwright.expect(page.get_by_role("button", name="Back to map", exact=True)).to_be_hidden()
    titles = {_focus_graph_region(page, area).locator(".bp-dag-node-title").inner_text() for area in _AREAS}
    assert titles == set(_AREAS)
    assert page.locator('.bp-dag-node[data-autoform-node-id="scope:appendix-a"]').count() == 0
    _browse_contents(page, "Source texts")
    playwright.expect(page.locator(".bp-dag-inventory-head h2")).to_have_text("Source tracks")
    playwright.expect(pressed).to_have_count(1)
    playwright.expect(pressed).to_have_text("Source texts")
    titles = page.locator(".bp-dag-inventory-title").all_text_contents()
    assert sorted(titles) == sorted(_SOURCE_TITLES)
    assert site.records["source-notes"]["title"] not in titles
    _browse_contents(page, "All statements")
    playwright.expect(page.locator(".bp-dag-inventory-head h2")).to_have_text("Results")
    playwright.expect(pressed).to_have_count(1)
    playwright.expect(pressed).to_have_text("All statements")
    assert _TARGET_TITLE in page.locator(".bp-dag-inventory-title").all_text_contents()
    assert not set(_SOURCE_TITLES).intersection(page.locator(".bp-dag-inventory-title").all_text_contents())
    assert "Reading note" not in page.locator(".bp-dag-inventory-title").all_text_contents()
    page.get_by_role("button", name="Back to map", exact=True).click()
    playwright.expect(pressed).to_have_count(0)
    _focus_graph_region(page, "Geometry").click()
    notes = _focus_graph_node(page, "scope:source-notes")
    playwright.expect(notes).to_be_focused()
    playwright.expect(pressed).to_have_count(0)
    playwright.expect(notes.locator(".bp-dag-node-title")).to_have_text(site.records["source-notes"]["title"])
    assert page.url == site.route("roadmap")


def test_zero_edge_native_article_keeps_solo_focal_node_closed_topic_links_and_complete_math(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _ISOLATED)
    node = _selected(page, _ISOLATED)
    assert "no dependency edges" in node.get_attribute("aria-label")
    playwright.expect(page.locator(".bp-dag-node")).to_have_count(1)
    details = page.locator(".bp-reading-related-topics")
    playwright.expect(details).to_have_count(1)
    assert details.get_attribute("open") is None
    owner = site.records[_ISOLATED]["scope"]
    topic_peers = [record["id"] for record in site.records.values()
                   if record["id"] != _ISOLATED and record.get("scope") == owner
                   and (record.get("result") is True or record.get("catalog") == "module")
                   and record.get("navigation") not in {"collection", "transparent", "source"}]
    assert _TARGET in topic_peers[:5], "The real published peer preview must contain the connected target"
    playwright.expect(details.locator("summary")).to_have_text(f"More in {site.records[owner]['title']}")
    card = _full_math(page, _ISOLATED, 2)
    assert "independent statement" in card.inner_text()
    assert page.url == site.route(_ISOLATED)
    assert page.locator(".bp-dag-empty").is_hidden()
    details.locator("summary").click()
    peer = details.get_by_role("link", name=_TARGET_TITLE, exact=True)
    playwright.expect(peer).to_have_attribute("href", site.route(_TARGET))
    assert peer.get_attribute("aria-label") is None
    playwright.expect(peer).to_be_visible()
    peer.click()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    _full_math(page)
    assert page.evaluate("() => document === __workspaceDocument") is True
    page.go_back()
    page.wait_for_url(site.route(_ISOLATED))
    _selected(page, _ISOLATED)
    playwright.expect(page.locator(".bp-dag-node")).to_have_count(1)
    _full_math(page, _ISOLATED, 2)
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_real_result_flag_drives_document_title_leaf_breadcrumb_and_result_count(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    assert site.records[_TARGET]["result"] is True
    _open(page, site, workspace_runtime)
    _search(page, _TARGET_TITLE, _TARGET)
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    assert page.title() == site.records[_TARGET]["title"]
    playwright.expect(page.locator(".bp-dag-breadcrumb li").last).to_have_text(_TARGET_TITLE)
    _browse_contents(page, "Statements in this topic")
    enabled = set(page.locator(".bp-dag-filter-popover input[value]:checked").evaluate_all("inputs => inputs.map(input => input.value)"))
    expected = sum(record.get("result") is True and record.get("scope") == site.records[_TARGET]["scope"]
                   and record.get("status") in enabled
                   for record in site.records.values())
    assert expected > 0
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text(f"{expected} results")


@pytest.mark.parametrize("node_id", ["scope:authored/main", "boundary:authored/main"])
def test_authored_prefix_like_ids_activate_the_exact_published_result(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, node_id: str,
) -> None:
    page, site = workspace_page, workspace_site
    assert site.records[node_id]["result"] is True
    _open(page, site, workspace_runtime)
    _search(page, site.records[node_id]["title"], node_id)
    page.wait_for_url(site.route(node_id))
    _selected(page, node_id)
    playwright.expect(page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{node_id}"]')).to_have_count(1)
    assert page.title() == site.records[node_id]["title"]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_zero_count_authored_region_is_visible_and_does_not_invent_one_item(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    authored = [node for node in site.root_graph["nodes"] if node.get("area") == "Unstarted mathematics"]
    assert authored and all(node["member_count"] == 0 and node["status_counts"] == [] for node in authored)
    _open(page, site, workspace_runtime)
    region = _focus_graph_region(page, "Unstarted mathematics")
    assert ", 0 articles" in region.get_attribute("aria-label")
    assert ", 1 article" not in region.get_attribute("aria-label")
    _browse_contents(page, "All statements")
    page.get_by_role("button", name="Back to map", exact=True).click()
    _focus_graph_region(page, "Unstarted mathematics")


def test_authored_cross_topic_proof_dependency_has_a_real_captured_area_edge(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    assert {"source": "Geometry", "target": "Analysis", "statement": 0, "proof": 1} in _area_edges(site.root_graph)
    _open(page, site, workspace_runtime)
    for area in ("Geometry", "Analysis"):
        playwright.expect(_focus_graph_region(page, area)).to_be_focused()
    assert _pixels(page)["painted"] > 0


def test_isolated_topic_link_opens_connected_result_and_its_real_edge(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _observe_dependency_routes(page)
    _open(page, site, workspace_runtime, _ISOLATED)
    _selected(page, _ISOLATED)
    playwright.expect(page.locator(".bp-dag-node")).to_have_count(1)
    _full_math(page, _ISOLATED, 2)
    # The solo result and its connected neighborhood have no irrelevant Edges
    # checkbox. Exercise that real filter on the authored mixed scope instead.
    owner = site.records[_ISOLATED]["scope"]
    parent = page.locator(".bp-dag-breadcrumb").get_by_role("link", name=site.records[owner]["title"], exact=True)
    playwright.expect(parent).to_have_attribute("href", site.route(owner))
    assert parent.evaluate("""link => {
      const own = link.getBoundingClientRect(), crumb = link.closest('li').getBoundingClientRect();
      const hit = document.elementFromPoint(own.x + own.width / 2, own.y + own.height / 2);
      return own.width > 0 && own.left >= crumb.left - 1 && own.right <= crumb.right + 1 &&
        (hit === link || link.contains(hit));
    }"""), "A truncated breadcrumb must retain its own unobscured pointer target"
    parent.click()
    page.wait_for_url(site.route(owner))
    playwright.expect(page.locator(".bp-dag-viewer")).to_have_attribute("data-dag-ready", "true")
    playwright.expect(page.locator(".bp-dag-viewer")).not_to_have_attribute("aria-busy", "true")
    _frames(page)
    isolate_control = _focus_graph_node(page, _ISOLATED)
    playwright.expect(isolate_control).to_be_visible()
    assert _cluster_native_box(isolate_control)["hit"] is True
    status = page.get_by_role("button", name=re.compile(r"^Filter: "))
    status.click()
    playwright.expect(status).to_have_attribute("aria-expanded", "true")
    popover = page.locator(".bp-dag-filter-popover")
    playwright.expect(popover).to_be_visible()
    isolated = popover.get_by_role("checkbox", name="Show unlinked", exact=True)
    playwright.expect(isolated).to_be_visible()
    isolated.uncheck()
    playwright.expect(isolated).not_to_be_checked()
    page.keyboard.press("Escape")
    playwright.expect(status).to_have_attribute("aria-expanded", "false")
    playwright.expect(popover).to_be_hidden()
    _frames(page)
    playwright.expect(page.locator(f'.bp-dag-node[data-autoform-node-id="{_ISOLATED}"]')).to_have_count(0)
    for identifier in (_BASE, _TARGET):
        control = _focus_graph_node(page, identifier)
        playwright.expect(control).to_be_visible()
        assert "no dependency edges" not in control.get_attribute("aria-label")
    page.go_back()
    page.wait_for_url(site.route(_ISOLATED))
    _selected(page, _ISOLATED)
    playwright.expect(page.locator(".bp-dag-node")).to_have_count(1)
    _full_math(page, _ISOLATED, 2)
    details = page.locator(".bp-reading-related-topics")
    assert details.get_attribute("open") is None
    details.locator("summary").click()
    peer = details.get_by_role("link", name=_TARGET_TITLE, exact=True)
    playwright.expect(peer).to_have_attribute("href", site.route(_TARGET))
    peer.click()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    _focus_graph_node(page, _BASE)
    base = page.locator(f'.bp-dag-node[data-autoform-node-id="{_BASE}"]')
    main = page.locator(f'.bp-dag-node[data-autoform-node-id="{_TARGET}"]')
    playwright.expect(base).to_be_visible()
    playwright.expect(main).to_be_visible()
    assert "no dependency edges" not in base.get_attribute("aria-label")
    assert "no dependency edges" not in main.get_attribute("aria-label")
    _frames(page)
    playwright.expect(base).to_be_visible()
    playwright.expect(main).to_be_visible()
    _selected(page, _TARGET)
    assert page.evaluate("""([source, target]) => {
      const a = document.querySelector(source).getBoundingClientRect(), b = document.querySelector(target).getBoundingClientRect();
      const canvas = document.querySelector('.bp-dag-canvas'), bounds = canvas.getBoundingClientRect();
      const at=(point,box)=>point[0]>=box.left-bounds.left-1 && point[0]<=box.right-bounds.left+1 &&
        point[1]>=box.top-bounds.top-1 && point[1]<=box.bottom-bounds.top+1;
      const route=window.__autoformCanvasRoutes.find(route=>route.kind==='statement' &&
        at(route.commands[0].args,a) && at(route.commands.at(-1).args,b));
      if(!route) return false;
      const samples=[];let previous=route.commands[0].args;
      for(const command of route.commands.slice(1)) {
        const args=command.args, end=args.slice(-2);
        for(let step=1;step<20;step++) {
          const t=step/20;
          if(command.method==='quadraticCurveTo') samples.push([(1-t)**2*previous[0]+2*t*(1-t)*args[0]+t*t*end[0],
            (1-t)**2*previous[1]+2*t*(1-t)*args[1]+t*t*end[1]]);
          else samples.push([previous[0]+t*(end[0]-previous[0]),previous[1]+t*(end[1]-previous[1])]);
        }
        previous=end;
      }
      return samples.some(point=> {
        if(at(point,a)||at(point,b)) return false;
        const x=Math.round(point[0]*canvas.width/bounds.width),y=Math.round(point[1]*canvas.height/bounds.height);
        if(x<3||y<3||x+3>=canvas.width||y+3>=canvas.height) return false;
        return Array.from(canvas.getContext('2d').getImageData(x-3,y-3,7,7).data).some((value,index)=>index%4===3&&value>0);
      });
    }""", [f'.bp-dag-node[data-autoform-node-id="{_BASE}"]', f'.bp-dag-node[data-autoform-node-id="{_TARGET}"]']) is True
    page.go_back()
    page.wait_for_url(site.route(_ISOLATED))
    _selected(page, _ISOLATED)
    playwright.expect(page.locator(".bp-dag-node")).to_have_count(1)
    _full_math(page, _ISOLATED, 2)


def test_a_drag_from_a_map_card_pans_and_keeps_the_reading_and_a_tap_opens_it(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _open(page, site, workspace_runtime, _TARGET)
    _selected(page, _TARGET)
    card = _focus_graph_node(page, _BASE)
    url = page.url
    before = card.bounding_box()
    stage = page.locator(".bp-dag-stage").bounding_box()
    assert before and stage
    x, y = before["x"] + before["width"] / 2, before["y"] + before["height"] / 2
    # Drag towards the middle of the map, so the card stays in view for the tap.
    dx = 40 if x < stage["x"] + stage["width"] / 2 else -40
    dy = 30 if y < stage["y"] + stage["height"] / 2 else -30
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=5)
    page.mouse.up()
    _frames(page)
    after = card.bounding_box()
    assert after
    assert abs(after["x"] - before["x"] - dx) < 1 and abs(after["y"] - before["y"] - dy) < 1
    _selected(page, _TARGET)
    assert page.url == url

    page.mouse.move(x + dx, y + dy)
    page.mouse.down()
    page.mouse.move(x + dx + 3, y + dy + 4, steps=2)
    page.mouse.up()
    page.wait_for_url(site.route(_BASE))
    _selected(page, _BASE)


def test_native_reader_and_library_links_remain_soft_navigation(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime, _TARGET)
    page.locator(".bp-reading-body").get_by_role("link", name="Read the counterpart identity", exact=True).click()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    page.locator(".bp-dag-book").click()
    page.wait_for_url(site.route("roadmap"))
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(_LIBRARY)
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_verified_concept_links_have_native_direction_navigation_back_and_known_empty_coverage(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    assert site.wiki_layer and site.wiki_layer["availability"] == "complete"
    assert site.wiki_pairs == {(_TARGET, _OTHER), (_TARGET, _ISOLATED), (_ISOLATED, _TARGET)}
    wiki_url = site.url + "/dependencies/wiki-links.json"
    held: list[object] = []
    documents: list[str] = []
    requests: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    page.on("request", lambda request: requests.append(request.url) if request.url == wiki_url else None)
    with page.expect_request(wiki_url) as requested:
        _open(page, site, workspace_runtime, _TARGET, wiki_route=lambda route: held.append(route))
    assert requested.value.url == wiki_url and len(held) == 1
    _selected(page, _TARGET)
    card = _full_math(page)
    playwright.expect(card).to_contain_text(_LAST_SENTENCE)
    disclosure = page.locator(".bp-reading-wiki-links")
    # Until the real response is released, do not invent zero counts or links.
    playwright.expect(disclosure).to_have_count(0)
    with page.expect_response(wiki_url) as delivered:
        held[0].continue_()
    assert delivered.value.status == 200 and delivered.value.json() == site.wiki_layer
    playwright.expect(disclosure).to_have_count(1)
    assert disclosure.get_attribute("open") is None
    assert disclosure.locator("summary").evaluate("""summary =>
      getComputedStyle(summary).display === 'list-item' &&
      getComputedStyle(summary, '::-webkit-details-marker').display !== 'none' &&
      getComputedStyle(summary, '::marker').display !== 'none'
    """), "A compact native disclosure must retain its visible expansion marker"
    disclosure.locator("summary").click()
    parts = disclosure.locator(".bp-reading-wiki-related")
    playwright.expect(parts).to_have_count(2)
    outgoing, incoming = parts.nth(0), parts.nth(1)
    playwright.expect(outgoing.locator("strong")).to_have_text("Links to other concepts · 2")
    playwright.expect(incoming.locator("strong")).to_have_text("Linked from · 1")
    playwright.expect(outgoing.get_by_role("link")).to_have_count(2)
    assert {link.get_attribute("href") for link in outgoing.get_by_role("link").all()} == {
        site.route(_OTHER), site.route(_ISOLATED),
    }
    playwright.expect(incoming.get_by_role("link")).to_have_count(1)
    playwright.expect(incoming.get_by_role("link", name=site.records[_ISOLATED]["title"], exact=True)).to_have_attribute(
        "href", site.route(_ISOLATED),
    )
    playwright.expect(disclosure.locator(".bp-reading-wiki-notice")).to_have_count(0)
    outgoing.get_by_role("link", name=site.records[_OTHER]["title"], exact=True).click()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _full_math(page, _OTHER, 1)
    assert disclosure.get_attribute("open") is None
    disclosure.locator("summary").click()
    parts = disclosure.locator(".bp-reading-wiki-related")
    playwright.expect(parts.nth(0).locator("strong")).to_have_text("Links to other concepts · 0")
    playwright.expect(parts.nth(0).get_by_role("link")).to_have_count(0)
    playwright.expect(parts.nth(1).locator("strong")).to_have_text("Linked from · 1")
    playwright.expect(parts.nth(1).get_by_role("link", name=_TARGET_TITLE, exact=True)).to_have_attribute(
        "href", site.route(_TARGET),
    )
    page.go_back()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    playwright.expect(_full_math(page)).to_contain_text(_LAST_SENTENCE)
    # This real definition has formal dependents, but no authored concept links.
    # The already-verified layer makes its empty disclosure genuinely known.
    assert not any(_BASE in pair for pair in site.wiki_pairs)
    base = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{_BASE}"]')
    permalink = base.get_by_role("link", name="#", exact=True)
    playwright.expect(permalink).to_have_attribute("href", site.route(_BASE))
    permalink.click()
    page.wait_for_url(site.route(_BASE))
    _selected(page, _BASE)
    playwright.expect(_full_math(page, _BASE, 0)).to_contain_text("The base value is zero.")
    playwright.expect(disclosure).to_have_count(0)
    playwright.expect(page.locator(".bp-reading-wiki-notice")).to_have_count(0)
    page.go_back()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    _full_math(page)
    assert requests == [wiki_url]
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_failed_optional_concept_transport_stays_unknown_without_breaking_math_or_navigation(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    assert site.wiki_layer and site.wiki_pairs
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    # Fail only the optional HTTP request; do not substitute graph JSON or a
    # fabricated sidecar, change the actual reader, or bypass digest validation.
    _open(page, site, workspace_runtime, _TARGET, wiki_route=lambda route: route.abort())
    _selected(page, _TARGET)
    card = _full_math(page)
    playwright.expect(card).to_contain_text(_LAST_SENTENCE)
    disclosure = page.locator(".bp-reading-wiki-links")
    playwright.expect(disclosure).to_have_count(1)
    assert disclosure.get_attribute("open") is None
    disclosure.locator("summary").click()
    playwright.expect(disclosure.locator(".bp-reading-wiki-notice")).to_have_text(
        "Concept links are unavailable for this publication.",
    )
    parts = disclosure.locator(".bp-reading-wiki-related")
    playwright.expect(parts.nth(0).locator("strong")).to_have_text("Links to other concepts · unavailable")
    playwright.expect(parts.nth(1).locator("strong")).to_have_text("Linked from · unavailable")
    playwright.expect(disclosure.get_by_role("link")).to_have_count(0)
    playwright.expect(page.locator(".bp-dag-error")).to_be_hidden()
    card.get_by_role("link", name="Read the counterpart identity", exact=True).click()
    page.wait_for_url(site.route(_OTHER))
    _selected(page, _OTHER)
    _full_math(page, _OTHER, 1)
    page.go_back()
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    playwright.expect(_full_math(page)).to_contain_text(_LAST_SENTENCE)
    assert documents == [site.route(_TARGET).split("#", 1)[0]]
    assert page.evaluate("() => document === __workspaceDocument") is True


@pytest.mark.parametrize(("width", "height"), [(1600, 900), (1000, 900), (390, 844)])
def test_authored_concept_families_pack_real_owner_cards_and_keep_native_focus_math_and_back(
    workspace_page: object, wiki_cluster_site: _WikiClusterSite, workspace_runtime: _PinnedRuntime,
    width: int, height: int,
) -> None:
    page, consumer = workspace_page, wiki_cluster_site
    site, owners = consumer.site, consumer.owners
    page.set_viewport_size({"width": width, "height": height})
    _observe_dependency_routes(page)
    held: list[object] = []
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    wiki_url = site.url + "/dependencies/wiki-links.json"
    with page.expect_request(wiki_url):
        _open(page, site, workspace_runtime, _CLUSTER_SCOPE, wiki_route=lambda route: held.append(route))
    assert len(held) == 1
    anchor = _cluster_readable_owner(page, owners["c"])
    _cluster_pan(page)
    anchor.focus()
    _frames(page)
    before = _cluster_native_box(anchor)
    assert before["hit"] is True
    assert page.evaluate("() => window.__autoformCanvasRoutes.length") == 0
    with page.expect_response(wiki_url) as delivered:
        held[0].continue_()
    assert delivered.value.status == 200 and delivered.value.json() == site.wiki_layer
    page.wait_for_function("() => window.__autoformCanvasRoutes.filter(route=>route.kind==='wiki').length===4", timeout=5000)
    playwright.expect(anchor).to_be_focused()
    after = _cluster_native_box(anchor)
    assert after["screen"] == pytest.approx(before["screen"], abs=.5)
    assert after["hit"] is True and page.url == site.route(_CLUSTER_SCOPE)
    _cluster_capture(page, width, "arrival", {"before": before, "after": after, "source_pairs": sorted(site.wiki_pairs)})

    # A deliberate native zoom-out is a geometry measurement, not a claimed
    # readable all-fit default. Every owner is explored at the .9 floor below.
    for _ in range(12):
        if page.locator(".bp-dag-node").count() == 9:
            break
        page.get_by_role("button", name="Zoom out", exact=True).click()
        _frames(page)
    playwright.expect(page.locator(".bp-dag-node")).to_have_count(9)
    boxes = {letter: _cluster_native_box(page.locator(f'.bp-dag-node[data-autoform-node-id="{identifier}"]'))
             for letter, identifier in owners.items()}
    scale = boxes["a"]["scale"]
    assert all(box["scale"] == pytest.approx(scale, abs=1e-6) for box in boxes.values())
    for index, a in enumerate(boxes.values()):
        for b in list(boxes.values())[index + 1:]:
            assert a["x"] + a["w"] <= b["x"] + .25 or b["x"] + b["w"] <= a["x"] + .25 or (
                a["y"] + a["h"] <= b["y"] + .25 or b["y"] + b["h"] <= a["y"] + .25
            ), (a, b)
    envelopes = []
    for family in _CLUSTER_FAMILIES:
        members = [boxes[letter] for letter in family]
        envelopes.append({"members": family, "left": min(box["x"] for box in members), "top": min(box["y"] for box in members),
                          "right": max(box["x"] + box["w"] for box in members), "bottom": max(box["y"] + box["h"] for box in members)})
    for index, a in enumerate(envelopes):
        for b in envelopes[index + 1:]:
            gap = max(a["left"] - b["right"], b["left"] - a["right"], a["top"] - b["bottom"], b["top"] - a["bottom"])
            assert gap >= 96 * scale - .25, (a, b, scale)
    routes = page.evaluate("() => window.__autoformCanvasRoutes")
    assert len(routes) == 4 and {route["kind"] for route in routes} == {"wiki"}
    assert page.evaluate("""() => window.__autoformCanvasClosedFills.filter(path =>
      path.map(command=>command.method).join(',')==='moveTo,lineTo,lineTo,closePath').length""") == 0, (
        "Concept associations must have no arrowheads; rounded family halos are not arrows"
    )
    endpoints = set()
    for route in routes:
        assert route["dashes"][0] / route["dashes"][1] == pytest.approx(.3, abs=1e-6)
        points = [command["args"][-2:] for command in route["commands"] if command["method"] in {"moveTo", "lineTo"}]
        assert len(points) >= 2 and len(points) == len(route["commands"])

        def endpoint(point):
            found = [letter for letter, box in boxes.items()
                     if box["x"] - 1 <= point[0] <= box["x"] + box["w"] + 1
                     and box["y"] - 1 <= point[1] <= box["y"] + box["h"] + 1]
            assert len(found) == 1, (point, boxes)
            return found[0]

        pair = frozenset((endpoint(points[0]), endpoint(points[-1])))
        endpoints.add(pair)
        for a, b in zip(points, points[1:]):
            assert all(not _cluster_segment_hits(a, b, box) for box in boxes.values()), (route, a, b)
    assert endpoints == {frozenset(pair) for pair in _CLUSTER_LINKS}
    for letter, identifier in owners.items():
        control = _cluster_readable_owner(page, identifier)
        box = _cluster_native_box(control)
        # A topic card names its fully proved share in the redesign, not a bare item count.
        assert box["title"] == f"Identity {letter.upper()}" and box["meta"] == "0 of 2 fully proved"
        _cluster_capture(page, width, f"readable-{letter}")

    control = _cluster_readable_owner(page, owners["c"])
    saved = _cluster_native_box(control)
    control.click()
    topic = f"{_CLUSTER_SCOPE}/c"
    target, base = topic + "/identity", topic + "/base"
    page.wait_for_url(site.route(topic))
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text("Identity C")
    target_card = page.locator(f'.bp-reading-body .bp-thmwrapper[data-autoform-node-id="{target}"]')
    playwright.expect(target_card).to_have_count(1)
    permalink = target_card.get_by_role("link", name="#", exact=True)
    playwright.expect(permalink).to_have_attribute("href", site.route(target))
    permalink.click()
    page.wait_for_url(site.route(target))
    _selected(page, target)
    card = _full_math(page, target, 1)
    playwright.expect(card).to_contain_text(_CLUSTER_END)
    playwright.expect(card).to_contain_text("ClusterFixture.identity_c")
    assert len(card.inner_text()) > 420 and site.records[target]["status"] == "not_ready"
    _focus_graph_node(page, base)
    _frames(page)
    formal = page.evaluate("""([source,target]) => {
      const canvas=document.querySelector('.bp-dag-canvas').getBoundingClientRect();
      const boxes=[source,target].map(id=>Array.from(document.querySelectorAll('.bp-dag-node')).find(node=>node.dataset.autoformNodeId===id).getBoundingClientRect());
      const at=(point,box)=>point[0]>=box.left-canvas.left-1&&point[0]<=box.right-canvas.left+1&&
        point[1]>=box.top-canvas.top-1&&point[1]<=box.bottom-canvas.top+1;
      return window.__autoformCanvasRoutes.filter(route=>route.kind!=='wiki'&&
        at(route.commands[0].args,boxes[0])&&at(route.commands.at(-1).args.slice(-2),boxes[1]));
    }""", [base, target])
    assert len(formal) == 1 and formal[0]["kind"] == "statement"
    assert page.evaluate("""() => window.__autoformCanvasClosedFills.filter(path=>
      path.map(command=>command.method).join(',')==='moveTo,lineTo,lineTo,closePath').length""") >= 1
    assert _pixels(page)["painted"] > 0
    page.go_back()
    page.wait_for_url(site.route(topic))
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text("Identity C")
    page.go_back()
    page.wait_for_url(site.route(_CLUSTER_SCOPE))
    page.wait_for_function("() => window.__autoformCanvasRoutes.filter(route=>route.kind==='wiki').length===4", timeout=5000)
    restored = page.locator(f'.bp-dag-node[data-autoform-node-id="{owners["c"]}"]')
    playwright.expect(restored).to_be_focused()
    assert _cluster_native_box(restored)["screen"] == pytest.approx(saved["screen"], abs=.5)
    _cluster_capture(page, width, "restored")
    assert documents == [site.route(_CLUSTER_SCOPE)]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_graph_first_browse_is_contextual_keyboard_closable_and_returns_to_the_same_scope_map(
    workspace_page: object, navigation_overview_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, navigation_overview_site
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime)
    # The redesign's handle names the page it opens, so its accessible name ends ", reading".
    handle = page.get_by_role("button", name=re.compile(r", reading$"))
    playwright.expect(handle).to_have_attribute("aria-expanded", "false")
    stage = page.locator(".bp-dag-stage")
    playwright.expect(stage).to_be_visible()
    toggle = page.get_by_role("button", name="Browse statements or source texts", exact=True)
    popover = page.locator(".bp-dag-browse-popover")
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    playwright.expect(popover).to_be_hidden()
    toggle.focus()
    toggle.press("Enter")
    playwright.expect(toggle).to_have_attribute("aria-expanded", "true")
    playwright.expect(popover).to_be_visible()
    root_action = popover.get_by_role("button", name="All statements", exact=True)
    playwright.expect(root_action).to_be_visible()
    playwright.expect(popover.get_by_role("button", name="Source texts", exact=True)).to_be_visible()
    playwright.expect(popover.get_by_role("button", name="Statements in this topic", exact=True)).to_have_count(0)
    root_action.focus()
    root_action.press("Escape")
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    playwright.expect(popover).to_be_hidden()
    playwright.expect(toggle).to_be_focused()
    toggle.click()
    playwright.expect(popover).to_be_visible()
    page.locator(".bp-dag-breadcrumb li").last.click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    playwright.expect(popover).to_be_hidden()
    assert page.url == site.route("roadmap")
    scope = next(record for record in site.records.values()
                 if record.get("scope") == record["id"] and record.get("area") == "Geometry")
    _search(page, scope["title"], scope["id"])
    page.wait_for_url(site.route(scope["id"]))
    playwright.expect(page.locator(".bp-reading-body h1")).to_have_text(scope["title"])
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "true")
    playwright.expect(popover).to_be_visible()
    playwright.expect(popover.get_by_role("button", name="All statements", exact=True)).to_have_count(0)
    popover.get_by_role("button", name="Statements in this topic", exact=True).click()
    playwright.expect(toggle).to_have_attribute("aria-expanded", "false")
    playwright.expect(popover).to_be_hidden()
    playwright.expect(stage).to_be_hidden()
    expected = {record["id"] for record in site.records.values()
                if record.get("result") is True and record.get("scope") == scope["id"]}
    assert len(expected) == 1
    rows = page.locator(".bp-dag-inventory-row")
    playwright.expect(rows).to_have_count(1)
    assert set(rows.evaluate_all("nodes => nodes.map(node => node.dataset.inventoryNodeId)")) == expected
    playwright.expect(page.locator(".bp-dag-inventory-summary")).to_have_text("1 result")
    assert page.url == site.route(scope["id"])
    page.get_by_role("button", name="Back to map", exact=True).click()
    playwright.expect(stage).to_be_visible()
    playwright.expect(page.locator(".bp-dag-inventory")).to_be_hidden()
    _focus_graph_node(page, next(iter(expected)))
    playwright.expect(page.locator(".bp-dag-breadcrumb li").last).to_have_text(scope["title"])
    assert page.url == site.route(scope["id"]), "Returning to the map must not widen this topic to the library"
    assert documents == [site.route("roadmap")]
    assert page.evaluate("() => document === __workspaceDocument") is True


def test_delayed_math_readiness_cannot_restore_an_older_native_section_after_back(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, workspace_site
    _routes(page, site.url, workspace_runtime, graph_client=True)
    held: list[object] = []
    page.route(MATHJAX_BUNDLE, lambda route: held.append(route))
    page.goto(site.route(_TARGET), wait_until="domcontentloaded")
    _selected(page, _TARGET)
    page.evaluate("() => window.__workspaceDocument = document")
    body = page.locator(".bp-reading-body")
    body.get_by_role("link", name="Read the independent identity", exact=True).click()
    page.wait_for_url(site.route(_ISOLATED), wait_until="domcontentloaded")
    _selected(page, _ISOLATED)
    body.get_by_role("link", name="Read the complete zero identity", exact=True).click()
    page.wait_for_url(site.route(_TARGET), wait_until="domcontentloaded")
    _selected(page, _TARGET)
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route(_ISOLATED), wait_until="domcontentloaded")
    _selected(page, _ISOLATED)
    assert held
    for route in held:
        workspace_runtime.serve(route)
    card = _full_math(page, _ISOLATED, 2)
    _frames(page)
    assert page.url == site.route(_ISOLATED)
    current = body.locator(".bp-reading-current")
    playwright.expect(current).to_have_count(1)
    playwright.expect(current).to_have_attribute("data-autoform-node-id", _ISOLATED)
    assert body.evaluate("reader => reader.contains(document.activeElement)") is True
    assert _math_source(page, card.locator("mjx-container").last) == r"\frac{0}{2}=0"
    assert page.evaluate("() => document === __workspaceDocument") is True


@pytest.mark.parametrize(("width", "height"), [(1600, 900), (1000, 900), (390, 844)])
def test_rich_workspace_region_and_full_reader_screenshots(
    workspace_page: object, workspace_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime, width: int, height: int,
) -> None:
    page, site = workspace_page, workspace_site
    page.set_viewport_size({"width": width, "height": height})
    _open(page, site, workspace_runtime)
    for area in _AREAS:
        playwright.expect(_focus_graph_region(page, area)).to_be_focused()
    page.get_by_role("button", name="Fit shown nodes", exact=True).click()
    _frames(page)
    assert _pixels(page)["painted"] > 0
    if width <= 720:
        _peek_reader(page, page.get_by_role("button", name=re.compile(r", reading$")))
    page.evaluate("() => document.fonts.ready")
    output = os.environ.get("AUTOFORM_UI_SCREENSHOT_DIR")
    directory = Path(output) if output else None
    if directory:
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"regions-{width}x{height}.png"))
    _search(page, _TARGET_TITLE, _TARGET)
    page.wait_for_url(site.route(_TARGET))
    _selected(page, _TARGET)
    _full_math(page)
    _frames(page)
    playwright.expect(page.get_by_role("heading", level=1)).to_have_count(1)
    if directory:
        page.screenshot(path=str(directory / f"reader-{width}x{height}.png"))
        report = {"viewport": [width, height], "route": page.url, "nodes": len(site.records), "areas": list(_AREAS),
                  "root_nodes": len(site.root_graph["nodes"]), "root_edges": len(site.root_graph["edges"]),
                  "area_edges": _area_edges(site.root_graph),
                  "mathjax": page.evaluate("() => MathJax.version"), "complete_math": 5, "canvas": _pixels(page)}
        (directory / f"workspace-{width}x{height}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert page.evaluate("() => document === __workspaceDocument") is True


_DECLUTTER_END = "The complete mathematical exposition and its native section anchor are retained."


@pytest.fixture(scope="module")
def reader_declutter_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_WorkspaceSite]:
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    project = tmp_path_factory.mktemp("reader-declutter") / "project"
    scaffold_project(project, title="Independent mathematical reader")
    blueprint = project / "blueprint"
    _write(blueprint / "README.md", "# Independent mathematical reader\n\n- [Mathematics](roadmap/README.md)\n")
    exposition = "\n\n".join(
        f"Paragraph {number + 1}. This is complete mathematical exposition, not a graph summary or a navigation index."
        for number in range(8)
    )
    _write(blueprint / "roadmap/README.md", (
        "# Independent mathematical reader\n\nThe scalar equality $0=0$ remains readable.\n\n"
        "## Topic catalogue\n\n- [First mathematical topic](alpha/README.md)\n"
        "- [Second mathematical topic](beta/README.md)\n\n"
        "## Full exposition\n\n" + exposition + "\n\nThe equality $\\frac{0}{2}=0$ is retained.\n\n"
        "## Sources\n\n- [Second mathematical topic](beta/README.md)\n\n"
        "## Bibliography\n\n- [First mathematical topic](alpha/README.md)\n\n"
        "## Commentary links\n\n- [First mathematical topic](alpha/README.md)\n"
        "- This mathematical commentary is not a navigation-only link.\n\n"
        "[Read retained exposition](#retained-section).\n\n"
        "## Retained section\n\n" + _DECLUTTER_END + "\n"
    ))
    _write(blueprint / "roadmap/alpha/README.md", (
        "---\nnavigation: module\n---\n\n# First mathematical topic\n\n"
        "The complete reading page includes its authored theorem.\n\n"
        "## Reading contents\n\nRead the [Complete identity](identity.md).\n"
    ))
    _write(blueprint / "roadmap/alpha/identity.md", (
        "---\ndeclaration: theorem\nnot_ready: true\n---\n\n# Complete identity\n\n"
        "The complete proposed statement is $0=0$. This is a planned fixture target, not a verified Lean proof.\n\n"
        + exposition + "\n\n## Depends on\n\nNo prerequisites.\n"
    ))
    _write(blueprint / "roadmap/beta/README.md", (
        "---\nnavigation: module\n---\n\n# Second mathematical topic\n\n"
        "A complete reading note is retained. Read the [topic note](note.md).\n"
    ))
    _write(blueprint / "roadmap/beta/note.md", (
        "# Topic note\n\nA narrative reading note, not a formalization target or a claimed Lean proof.\n"
    ))
    render_site(blueprint, project / "site-src", lean_root=project)
    configuration = load_config(config_file=str(project / "mkdocs.yml"), strict=True)
    configuration["extra_javascript"] = [entry for entry in configuration["extra_javascript"]
                                           if getattr(entry, "path", str(entry)) != MATHJAX_BUNDLE]
    build(configuration)
    index_path = project / "site/dependencies/index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    for node_id in ("alpha", "beta"):
        assert records[node_id]["kind"] == "scope" and records[node_id]["scope"] == node_id
        assert type(records[node_id]["parent"]) is int
        assert index["nodes"][records[node_id]["parent"]]["id"] == "roadmap"
    published_root = index_path.parent / unquote(urlsplit(records["roadmap"]["route"]).path)
    tree = html5lib.parse(published_root.read_text(encoding="utf-8"), treebuilder="etree", namespaceHTMLElements=False)
    reading = next(element for element in tree.iter() if "bp-reading-source" in element.get("class", "").split())
    assert not any("bp-as-typed" in element.get("class", "").split() for element in reading.iter())
    assert {"topic-catalogue", "sources", "bibliography", "commentary-links", "retained-section"} <= {
        element.get("id") for element in reading.iter() if element.tag == "h2"
    }
    assert sum("arithmatex" in element.get("class", "").split() for element in reading.iter()) == 2
    root_path = index_path.parent / unquote(urlsplit(records["roadmap"]["data_url"]).path)
    root_graph = json.loads(root_path.read_text(encoding="utf-8"))
    with _served(project / "site") as url:
        yield _WorkspaceSite(url, records, root_graph)


def test_enhanced_reader_hides_only_duplicate_scope_index_and_keeps_full_ssr_sources_and_math(
    workspace_page: object, workspace_browser: object, reader_declutter_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, reader_declutter_site
    _open(page, site, workspace_runtime)
    reader = page.locator(".bp-reading-body > .bp-reading-source")
    playwright.expect(reader.locator("#topic-catalogue")).to_have_count(1)  # Check existence before hidden accepts absence.
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_have_count(1)
    playwright.expect(reader.locator("#topic-catalogue")).to_be_hidden()
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_be_hidden()
    for node_id in ("alpha", "beta"):
        playwright.expect(page.locator(f'.bp-dag-navigation [data-navigation-node-id="{node_id}"]')).to_be_visible()
    playwright.expect(reader.locator("#sources")).to_be_visible()
    playwright.expect(reader.locator("#sources + ul")).to_be_visible()
    playwright.expect(reader.locator("#bibliography")).to_be_visible()
    playwright.expect(reader.locator("#bibliography + ul")).to_be_visible()
    playwright.expect(reader.locator("#commentary-links + ul")).to_be_visible()
    assert _DECLUTTER_END in reader.inner_text() and len(reader.inner_text()) > 420
    assert page.evaluate("() => AutoformMath.refresh(document.querySelector('.bp-reading-body'))") is True
    playwright.expect(reader.locator(".arithmatex")).to_have_count(2)
    playwright.expect(reader.locator("mjx-container")).to_have_count(2)
    assert reader.locator("mjx-merror,merror").count() == 0
    playwright.expect(reader.locator("h1")).to_have_count(1)
    context = workspace_browser.new_context(viewport={"width": 1600, "height": 900}, java_script_enabled=False)
    try:
        native = context.new_page()
        _routes(native, site.url, workspace_runtime, graph_client=True)
        native.goto(site.route("roadmap"))
        source = native.locator(".bp-reading-source")
        playwright.expect(source.locator("#topic-catalogue")).to_be_visible()
        playwright.expect(source.locator("#topic-catalogue + ul a")).to_have_count(2)
        playwright.expect(source.locator("#topic-catalogue + ul")).to_be_visible()
        assert _DECLUTTER_END in source.inner_text()
        playwright.expect(source.locator(".arithmatex")).to_have_count(2)
        playwright.expect(source.locator("mjx-container")).to_have_count(0)
    finally:
        context.close()


def test_decluttered_reader_keeps_native_math_anchors_leaf_navigation_and_back_projection(
    workspace_page: object, reader_declutter_site: _WorkspaceSite, workspace_runtime: _PinnedRuntime,
) -> None:
    page, site = workspace_page, reader_declutter_site
    documents: list[str] = []
    page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
    _open(page, site, workspace_runtime)
    reader = page.locator(".bp-reading-body")
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_have_count(1)
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_be_hidden()
    reader.get_by_role("link", name="Read retained exposition", exact=True).click()
    page.wait_for_url(site.route("roadmap") + "#retained-section")
    playwright.expect(reader.locator("#retained-section")).to_be_visible()
    assert _DECLUTTER_END in reader.inner_text()
    page.locator('.bp-dag-navigation [data-navigation-node-id="alpha"]').click()
    page.wait_for_url(site.route("alpha"))
    playwright.expect(reader.get_by_role("link", name="Complete identity", exact=True)).to_be_visible()
    reader.get_by_role("link", name="Complete identity", exact=True).click()
    page.wait_for_url(site.route("alpha/identity"))
    _selected(page, "alpha/identity")
    card = _full_math(page, "alpha/identity", 1)
    assert "complete proposed statement" in card.inner_text() and len(card.inner_text()) > 420
    assert card.get_attribute("id")
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route("alpha"))
    playwright.expect(reader.locator("h1")).to_have_text(site.records["alpha"]["title"])
    page.evaluate("() => history.back()")
    page.wait_for_url(site.route("roadmap") + "#retained-section")
    # popstate changes the URL before the bounded asynchronous reader commit.
    # Missing catalogue nodes in the outgoing chapter must not satisfy hidden.
    playwright.expect(reader.locator("#retained-section")).to_be_visible()
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_have_count(1)
    playwright.expect(reader.locator("#topic-catalogue + ul")).to_be_hidden()
    assert _DECLUTTER_END in reader.inner_text()
    assert documents == [site.route("roadmap")]
    assert page.evaluate("() => document === __workspaceDocument") is True
