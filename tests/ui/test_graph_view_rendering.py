from __future__ import annotations

import functools
import html.parser
import json
import re
from pathlib import Path

import pytest

from autoform_cli import status
from autoform_cli.ui import dag_viewer, graph_pages, render, site_index
from autoform_cli.ui.graph_views import GraphView, ViewEdge, ViewNode
from tests.dom_fixture import run_dom

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


# Walks each script's syntax tree, so comments and spacing play no part. It returns the strings each script can put on
# an element, and each operation that writes a string into the page as HTML. A string passed to a call that only looks
# a class up or takes it off draws nothing, nor does one held in a variable that only such calls use.
_SCRIPT_WALK = r"""
const acorn = require("acorn");
const lookups = new Set(["closest", "contains", "getElementsByClassName", "matches", "querySelector",
  "querySelectorAll", "remove"]);
const parsers = new Set(["createContextualFragment", "insertAdjacentHTML", "setHTMLUnsafe", "write", "writeln"]);
const member = node => node && node.type === "MemberExpression" && !node.computed ? node.property.name : null;
const isFunction = node => /Function/.test(node.type);
const children = node => Object.entries(node).flatMap(([key, child]) => (Array.isArray(child) ? child : [child])
  .filter(item => item && typeof item.type === "string").map(item => [key, item]));
// The names a declaration or a parameter binds, however it destructures.
const bound = pattern => !pattern ? [] : pattern.type === "Identifier" ? [pattern.name]
  : pattern.type === "AssignmentPattern" ? bound(pattern.left)
  : pattern.type === "RestElement" ? bound(pattern.argument)
  : pattern.type === "ArrayPattern" ? pattern.elements.flatMap(bound)
  : pattern.type === "ObjectPattern" ? pattern.properties.flatMap(item => bound(item.value || item.argument)) : [];
const found = {};
for (const [name, source] of Object.entries(args.sources)) {
  const program = acorn.parse(source, {ecmaVersion: "latest"});
  // The names each function, or the script, declares: its parameters, functions and variables.
  const declared = new Map([[program, new Set()]]);
  const declare = (node, scope) => {
    if (isFunction(node)) {
      if (node.type === "FunctionDeclaration" && node.id) declared.get(scope).add(node.id.name);
      declared.set(node, new Set([...(node.type === "FunctionExpression" && node.id ? [node.id.name] : []),
        ...node.params.flatMap(bound)]));
    }
    if (node.type === "VariableDeclarator") bound(node.id).forEach(id => declared.get(scope).add(id));
    if (node.type === "CatchClause") bound(node.param).forEach(id => declared.get(scope).add(id));
    for (const [, child] of children(node)) declare(child, isFunction(node) ? node : scope);
  };
  declare(program, program);
  // Where a name is declared: the nearest enclosing function, or the script, that declares it; else the page.
  const scopes = [...declared.keys()];
  const binding = (id, chain) => scopes.indexOf(chain.findLast(scope => declared.get(scope).has(id))) + ":" + id;
  const strings = [], sinks = [], held = new Map(), uses = new Map();
  const add = (map, key, value) => (map.get(key) || map.set(key, []).get(key)).push(value);
  const visit = (node, lookup, parent, key, chain) => {
    if (isFunction(node)) chain = [...chain, node];
    // A string a variable is set to draws only where that variable is used.
    const holder = parent && parent.type === "VariableDeclarator" && key === "init" && parent.id.type === "Identifier"
      ? parent.id.name
      : parent && parent.type === "AssignmentExpression" && key === "right" && parent.left.type === "Identifier"
        ? parent.left.name : null;
    if (node.type === "Literal" && typeof node.value === "string" && !lookup) {
      if (holder) add(held, binding(holder, chain), node.value); else strings.push(node.value);
    }
    if (node.type === "TemplateElement" && !lookup) strings.push(node.value.cooked);
    const declaration = parent && ((parent.type === "MemberExpression" && key === "property" && !parent.computed)
      || (parent.type === "Property" && key === "key" && !parent.computed)
      || (parent.type === "VariableDeclarator" && key === "id")
      || (parent.type === "AssignmentExpression" && key === "left")
      || (isFunction(parent) && (key === "id" || key === "params")));
    if (node.type === "Identifier" && !declaration) add(uses, binding(node.name, chain), lookup);
    if (node.type === "AssignmentExpression" && ["innerHTML", "outerHTML"].includes(member(node.left))) {
      sinks.push(member(node.left));
    }
    if (node.type === "CallExpression" && parsers.has(member(node.callee))) sinks.push(member(node.callee));
    const looksUp = node.type === "CallExpression" && lookups.has(member(node.callee));
    for (const [childKey, child] of children(node)) {
      visit(child, lookup || (looksUp && childKey === "arguments"), node, childKey, chain);
    }
  };
  visit(program, false, null, null, [program]);
  for (const [holder, values] of held) if ((uses.get(holder) || []).some(lookup => !lookup)) strings.push(...values);
  found[name] = {strings, sinks};
}
return found;
"""


@functools.cache
def _scripts() -> dict[str, dict[str, list[str]]]:
    """What each script the explorer's page runs writes and parses: its source files and the book's boot frame."""
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(_ASSETS.glob("*.js")) if not path.name.endswith(".min.js")
    }
    sources["boot frame"] = re.fullmatch(r"<script>(.*)</script>", dag_viewer.BOOT_SCRIPT, re.S).group(1)
    return json.loads(run_dom(_SCRIPT_WALK, args={"sources": sources}))


def test_the_explorer_builds_its_dom_without_html_strings() -> None:
    """Titles, summaries and Lean names come from authored Markdown: the client sets text, never parsed HTML."""
    explorer = ("tex-plain.js", "blueprint-navigation.js", "blueprint-dag.js", "boot frame")
    assert {name: _scripts()[name]["sinks"] for name in explorer} == {name: [] for name in explorer}


def _split(text: str, separators: str) -> list[str]:
    """``text`` cut at each of ``separators`` that is outside brackets and parentheses."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        depth += (char in "([") - (char in ")]")
        if depth == 0 and char in separators:
            parts.append(text[start:index])
            start = index + 1
    return [*parts, text[start:]]


def _sheet(style: str) -> list[tuple[tuple[str, ...], str, dict[str, str]]]:
    """Each rule in source order: the at-rules around it, its selectors and its declarations, comments dropped."""
    text, rules, context, position = re.sub(r"/\*.*?\*/", "", style, flags=re.S), [], [], 0
    while (match := re.compile(r"[{};]").search(text, position)) is not None:
        prelude, position = text[position : match.start()].strip(), match.end()
        if match.group() == "}":
            context.pop()
        elif match.group() == "{" and prelude.startswith("@"):
            context.append(prelude)
        elif match.group() == "{":
            end = text.index("}", position)
            declarations = {}
            for declaration in text[position:end].split(";"):
                name, colon, value = declaration.partition(":")
                if colon:
                    declarations[name.strip()] = value.replace("!important", "").strip()
            rules.append((tuple(context), prelude, declarations))
            position = end + 1
    return rules


def _compound(compound: str) -> set[frozenset[str]]:
    """The simple selectors of one compound selector, once for each alternative of an :is() or :where() in it."""
    alternatives: set[frozenset[str]] = {frozenset()}
    for token in re.findall(r"::?[\w-]+(?:\((?:[^()]|\([^()]*\))*\))?|[.#][\w-]+|\[[^\]]*\]|[\w-]+|\*", compound):
        if re.match(r":(?:is|where)\(", token):
            inside = _split(token[token.index("(") + 1 : -1], ",")
            options = set().union(*(_compound(option.strip()) for option in inside))
            alternatives = {have | option for have in alternatives for option in options}
        else:
            alternatives = {have | {re.sub(r"[\s\"']", "", token)} for have in alternatives}
    return alternatives


def _subjects(selectors: str) -> set[frozenset[str]]:
    """What each selector in a list styles: the simple selectors of its last compound."""
    lasts = ([part for part in _split(one.strip(), " >+~") if part][-1] for one in _split(selectors, ","))
    return set().union(*(_compound(last) for last in lasts))


def _declared(subject: str, *properties: str) -> str | None:
    """The value the explorer's unconditional rules give ``properties`` on an element whose selector ends in exactly
    ``subject``, the later rule winning; None when no rule sets them."""
    wanted, value = _compound(subject), None
    for context, selectors, declarations in _sheet(dag_viewer._style()):
        found = [declarations[name] for name in properties if name in declarations]
        if found and not context and wanted & _subjects(selectors):
            value = found[-1]
    return value


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
    rings = [(selectors, declarations["outline"]) for context, selectors, declarations in _sheet(dag_viewer._style())
             if not context and ":focus-visible" in selectors and "outline" in declarations]
    assert rings
    for selectors, outline in rings:
        assert outline.split() == ["2px", "solid", "var(--dag-link)"], selectors


def test_touch_screens_keep_every_toolbar_control_and_44px_targets() -> None:
    """A coarse pointer hides no control, and every toolbar control reaches 44px."""
    coarse = [(selectors, declarations) for context, selectors, declarations in _sheet(dag_viewer._style())
              if any("pointer: coarse" in rule for rule in context)]
    assert coarse
    hiding = {"display": ("none",), "visibility": ("hidden", "collapse"), "pointer-events": ("none",)}
    for selectors, declarations in coarse:
        assert not [name for name, values in hiding.items() if declarations.get(name) in values], selectors
    # The rule has to select each control itself: a wrapper's height leaves the control as small as it was.
    tall = [selectors for selectors, declarations in coarse if declarations.get("min-height") == "44px"]
    targets = set().union(*(_subjects(selectors) for selectors in tall))
    for control in (".bp-dag-search", ".bp-dag-button", ".bp-dag-filter-summary", ".bp-dag-hidden-button"):
        assert any(control in subject for subject in targets), control


class _Classes(html.parser.HTMLParser):
    """Every class an element of an HTML string has."""

    def __init__(self, markup: str) -> None:
        super().__init__()
        self.found: set[str] = set()
        self.feed(markup)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.found.update(token for name, value in attrs if name == "class" and value for token in value.split())


def _written_classes() -> set[str]:
    """The explorer classes its scripts can put on an element, and those on the elements of the renderer's frame and
    host, rendered with every part they can show."""
    strings = [string for found in _scripts().values() for string in found["strings"]]
    frame = dag_viewer.render_boot(
        style_href="assets/blueprint-dag.css",
        trail=[("Home", "index.html"), ("Chapter", "chapter.html"), ("Result", None)],
        context=("Chapter", "chapter.html"),
        sources=True,
        statements=([("Home", "index.html")], [("result", "Result")]),
        handle="Result",
        heading="Result",
        indexes=[("contents", 1)],
    )
    host = dag_viewer.render_container("graph.json", fallback_links=[("A", "a.html")], fallback_total=2)
    written = {token for string in strings for token in string.split() if re.fullmatch(r"bp-dag-[\w-]*\w", token)}
    return written | _Classes(frame + host).found


_FRAME_RUN = r"""
// The book's frame takes the explorer's pane classes before it starts. Run its boot script, as the page does, on a
// frame whose stylesheet has loaded, once for each pane the reader can shut, and read the classes the frame ends with.
const taken = new Set();
Object.defineProperty(window.HTMLLinkElement.prototype, "sheet", {configurable: true, get: () => ({cssRules: [{}]})});
for (const panes of args.panes) {
  document.body.innerHTML = args.page;
  const script = document.querySelector(".bp-reading-source > script");
  Object.defineProperty(document, "currentScript", {configurable: true, get: () => script});
  window.localStorage.setItem("autoform:book-panes:v1", JSON.stringify(panes));
  window.eval(script.textContent);
  document.querySelector(".bp-book-frame").classList.forEach(name => taken.add(name));
}
return [...taken];
"""


def _frame_classes() -> set[str]:
    """The classes the book's frame takes before the explorer starts, for each pane the reader can shut."""
    page = (
        dag_viewer.render_boot(style_href="assets/blueprint-dag.css")
        + '<div class="bp-reading-source" data-autoform-node-id="chapter/result">'
        + dag_viewer.BOOT_SCRIPT + "<p>The reading.</p></div>"
    )
    panes = [{"topicsHidden": True, "readingHidden": True}, {"mapHidden": True}]
    return set(json.loads(run_dom(_FRAME_RUN, args={"page": page, "panes": panes})))


def test_every_explorer_class_the_stylesheet_styles_is_drawn_somewhere() -> None:
    """The removed canvas and inspector left 22 classes styled that nothing drew any more.

    A class is drawn where a script writes it, rather than looking it up or taking it off, where an element of the
    renderer's HTML has it, or where the book's frame takes it: the frame builds its pane classes from the panes'
    names, so its run shows which.
    """
    styled = {name for _context, selectors, _declarations in _sheet(dag_viewer._style())
              for name in re.findall(r"\.(bp-dag-[\w-]+)", selectors)}
    written, frame = _written_classes(), _frame_classes()
    # Saved with Contents and the reader shut, the frame shuts both.
    assert len(styled) > 50 and {"bp-dag-topics-hidden", "bp-dag-reading-hidden"} <= frame
    assert sorted(styled - written - frame) == []


def _contrast(first: str, second: str) -> float:
    def luminance(colour: str) -> float:
        channels = [int(colour.lstrip("#")[index : index + 2], 16) / 255 for index in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _tokens(dark: bool) -> dict[str, str]:
    """The custom properties an explorer element inherits in one scheme: the book's root tokens, its dark scheme's on
    the body, then the explorer's own on its host, each rule's declarations replacing the earlier ones."""
    slate = "[data-md-color-scheme=slate]"
    scopes = [":root", slate, ".bp-dag-viewer", slate + " .bp-dag-viewer"] if dark else [":root", ".bp-dag-viewer"]
    sheets, tokens = [*_sheet(render._stylesheet()), *_sheet(dag_viewer._style())], {}
    for scope in scopes:
        for context, selectors, declarations in sheets:
            if not context and scope in {" ".join(one.replace('"', "").split()) for one in _split(selectors, ",")}:
                tokens.update((name, value) for name, value in declarations.items() if name.startswith("--"))
    return tokens


def _rgba(value: str, tokens: dict[str, str]) -> tuple[float, float, float, float]:
    """A CSS colour as the browser computes it, as sRGB channels and an alpha: hex, rgb(), transparent, var() with
    its fallback, and color-mix() in sRGB, which mixes premultiplied and scales alpha when the shares fall short."""
    value = value.strip()
    if value in ("transparent", "none"):
        return (0.0, 0.0, 0.0, 0.0)
    if re.fullmatch(r"#[\da-fA-F]{3,8}", value):
        digits = value[1:] if len(value) > 5 else "".join(char * 2 for char in value[1:])
        return (*(float(int(digits[index : index + 2], 16)) for index in (0, 2, 4)), int(digits[6:8] or "ff", 16) / 255)
    call = re.fullmatch(r"([\w-]+)\((.*)\)", value, re.S)
    assert call is not None, value
    name, arguments = call.group(1), _split(call.group(2), ",")
    if name == "var":
        token = arguments[0].strip()
        return _rgba(tokens[token] if token in tokens else ",".join(arguments[1:]), tokens)
    if name in ("rgb", "rgba"):
        numbers = re.split(r"[\s,/]+", call.group(2).strip())
        alpha = float(numbers[3].rstrip("%")) / (100 if numbers[3].endswith("%") else 1) if len(numbers) > 3 else 1.0
        return (*(float(number) for number in numbers[:3]), alpha)
    assert name == "color-mix" and arguments[0].split() == ["in", "srgb"], value
    parts = [re.fullmatch(r"(.*?)(?:\s+([\d.]+)%)?", argument.strip(), re.S) for argument in arguments[1:]]
    colours = [_rgba(part.group(1), tokens) for part in parts]
    first, second = (None if part.group(2) is None else float(part.group(2)) for part in parts)
    first = 100 - second if first is None and second is not None else 50 if first is None else first
    second = 100 - first if second is None else second
    weights = (first / (first + second), second / (first + second))
    alpha = sum(weight * colour[3] for weight, colour in zip(weights, colours))
    if alpha == 0:
        return (0.0, 0.0, 0.0, 0.0)
    channels = (sum(weight * colour[3] * colour[channel] for weight, colour in zip(weights, colours)) / alpha
                for channel in range(3))
    return (*channels, alpha * min(first + second, 100) / 100)


def _screen(*layers: str, dark: bool) -> str:
    """The 8-bit colour the screen shows where each of ``layers``, bottom first, is painted over the explorer's page. A
    layer is a colour, or a selector subject whose background the explorer's rules set."""
    tokens, page = _tokens(dark), _declared(".bp-dag-viewer", "background", "background-color")
    assert page is not None and _rgba(page, tokens)[3] == 1
    shown = _rgba(page, tokens)[:3]
    for layer in layers:
        paint = _declared(layer, "background", "background-color") if layer.startswith(".") else layer
        assert paint is not None, layer
        *colour, alpha = _rgba(paint, tokens)
        shown = tuple(alpha * top + (1 - alpha) * under for top, under in zip(colour, shown))
    return "#" + "".join(f"{round(channel):02X}" for channel in shown)


def _explorer(dark: bool) -> dict[str, str]:
    """The explorer's link and its page, panel and map, in one scheme, as screen colours."""
    return {"link": _screen("var(--dag-link)", dark=dark), "page": _screen(dark=dark),
            "panel": _screen("var(--dag-panel)", dark=dark), "map": _screen(".bp-dag-main", dark=dark)}


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_links_read_at_small_size_on_every_explorer_surface(dark: bool) -> None:
    """The dark link was #2D88FF, 4.44:1 on the panel and 4.18:1 on the outline's current row."""
    surfaces = _explorer(dark)
    link = surfaces["link"]
    behind = {"page": surfaces["page"], "map": surfaces["map"], "panel": surfaces["panel"]}
    # Wherever a rule sets text in the link's colour, on the background the rules give that element.
    for context, selectors, declarations in _sheet(dag_viewer._style()):
        colour = declarations.get("color", "")
        if context or not re.match(r"(?:var|color-mix|rgba?)\(|#", colour) or _screen(colour, dark=dark) != link:
            continue
        for subject in _subjects(selectors):
            paint = _declared("".join(sorted(subject)), "background", "background-color")
            behind[" ".join(sorted(subject))] = _screen(paint, dark=dark) if paint else surfaces["page"]
    assert len(behind) > 4
    assert min(_contrast(link, surface) for surface in behind.values()) >= 4.5, (link, behind)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_muted_text_reads_on_the_search_field_and_the_maps_notice(dark: bool) -> None:
    """The placeholder was translucent, and a map that failed to load left the stage empty."""
    placeholder = _declared(".bp-dag-search::placeholder", "color")
    opacity = float(_declared(".bp-dag-search::placeholder", "opacity") or 1)
    field = _screen(".bp-dag-search", dark=dark)
    assert placeholder is not None and opacity == 1
    assert _contrast(_screen(".bp-dag-search", placeholder, dark=dark), field) >= 4.5
    notice = _declared(".bp-dag-map-unavailable", "color")
    assert notice is not None
    stage = _screen(".bp-dag-main", ".bp-dag-map-unavailable", dark=dark)
    assert _contrast(_screen(".bp-dag-main", ".bp-dag-map-unavailable", notice, dark=dark), stage) >= 4.5


def test_dark_explorer_cards_and_errors_read_against_the_page_toned_map() -> None:
    """Outlined cards were filled with the dark map's own colour, and planned's stroke had 2.1:1 against it."""
    surfaces = _explorer(True)
    assert all(state.dark_fill.upper() != surfaces["map"] for state in (*status.STATES, *status.INVENTORY_STATES))
    planned = next(state for state in status.STATES if state.key == "planned")
    assert min(_contrast(planned.dark_stroke, surfaces["map"]), _contrast(planned.dark_stroke, surfaces["page"])) >= 3
    assert _contrast(_screen("var(--dag-error)", dark=True), surfaces["page"]) >= 4.5


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


# Where the explorer draws a status mark: the element under it, over the one that element is drawn on. A result or a
# statement in the reader is tinted, so its mark sits on the tint over the panel or the reader.
_MARK_HOSTS = {
    "map": (".bp-dag-main",),
    "statement list": (".bp-dag-inventory",),
    "hovered statement": (".bp-dag-inventory", ".bp-dag-inventory-row:hover"),
    "focused statement": (".bp-dag-inventory", ".bp-dag-inventory-row:focus-visible"),
    "selected statement": (".bp-dag-inventory", ".bp-dag-inventory-row[aria-current=true]"),
    "search results": (".bp-dag-search-results",),
    "hovered result": (".bp-dag-search-results", ".bp-dag-result:hover"),
    "focused result": (".bp-dag-search-results", ".bp-dag-result:focus-visible"),
    "filter": (".bp-dag-filter-popover",),
    "hovered filter": (".bp-dag-filter-popover", ".bp-dag-check:hover"),
    "reader": (".bp-dag-inspector",),
    "statement being read": (".bp-dag-inspector", ".bp-reading-current"),
}
# Dark defined is one ladder step below proved, so on the three tints it falls just short of 3:1. These are its ratios
# there today: they may rise, never fall, and every other mark keeps 3:1 on every surface.
_MARK_FLOORS = {
    ("defined", "selected statement"): 2.93,
    ("defined", "hovered result"): 2.98,
    ("defined", "focused result"): 2.98,
    ("defined", "statement being read"): 2.91,
}


def test_each_finished_status_has_its_own_small_mark_in_both_schemes() -> None:
    """Fully proved, proved and defined shared stroke #22773A, so their dots and swatches were one colour.

    In the book's dependency rows and the explorer's lists the mark is the only status signal. Light marks
    darken towards mathlib and dark ones brighten towards it, so more finished always reads stronger.
    """
    finished = ("mathlib", "fully_proved", "proved", "defined")
    assert [state.key for state in status.STATES[:4]] == list(finished)
    for dark in (False, True):
        book = _book_marks(dark)
        marks = [book[key] for key in finished]
        for (first, second), (one, other) in zip(zip(finished, finished[1:]), zip(marks, marks[1:])):
            ratio = _contrast(one, other)
            assert ratio >= 1.5, (first, second, dark, ratio)
            assert (_contrast(one, "#000000") > _contrast(other, "#000000")) is dark, (first, second, dark)
        for host, layers in _MARK_HOSTS.items():
            surface = _screen(*layers, dark=dark)
            for key, mark in zip(finished, marks):
                floor = _MARK_FLOORS.get((key, host), 3) if dark else 3
                assert _contrast(mark, surface) >= floor, (key, host, dark, mark, surface)


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
    # A chain runs through the first 400; the last 100, past the list's first page, have no relation at all.
    view = GraphView(
        kind="full",
        title="Budget fixture",
        nodes=nodes,
        edges=tuple(ViewEdge(nodes[index].id, nodes[index + 1].id, statement_count=1) for index in range(399)),
    )
    output = tmp_path / "budget.json"
    dag_viewer.write_payload(output, view)
    # The list shows every statement, related or not, in the view's order.
    listed = json.loads(output.read_text(encoding="utf-8"))["nodes"]
    assert [node["id"] for node in listed] == [node.id for node in nodes]
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
