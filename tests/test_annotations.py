"""Annotations in blueprint/links/: the facts they carry, every fact left out warned about, and what reads them."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from literature_links import discovery

from autoform_cli import debrief
from autoform_cli.__main__ import main
from autoform_cli.annotations import Drops, _annotations, _walk
from autoform_cli.graph import load_graph
from autoform_cli.review import ReviewError
from autoform_cli.runtime import load_runtime_graph
from autoform_cli.search import search_blueprint
from autoform_cli.skeleton import blueprint_hash
from autoform_cli.ui.render import _left_out, _review_disclosures
from autoform_cli.work import work_context

ARTICLE_ID = "af_f8d7b54996f51594f707d44f"
#: The project, from links/_defaults.json, is two folders up: links/, then blueprint/.
DEFAULTS = {"object": {"type": "lean", "uri": "../.."}}
MATHLIB = {
    "type": "lean",
    "uri": "https://github.com/leanprover-community/mathlib4",
    "version": "v4.33.0",
    "location": {"declaration": "Subgroup.card_subgroup_dvd_card", "module": "Mathlib.GroupTheory.Coset.Card"},
}
#: Another Mathlib declaration in MATHLIB's file.
SAME_FILE = {**MATHLIB, "location": {**MATHLIB["location"], "declaration": "Subgroup.index_mul_card"}}
REVIEWED = {"area": "Group theory", "review_approved": "0" * 64}
BESIDE = (
    "frontmatter beside an annotation is ignored, except coverage_list_limit, open_statements, published, tracks_lean;"
    " keep it until `autoform migrate links blueprint` shows the annotation holds it"
)


def _blueprint(tmp_path: Path, articles: dict[str, str | None]) -> Path:
    """A blueprint that check, render and audit accept, with an article per frontmatter given; None writes none."""

    blueprint = tmp_path / "blueprint"
    (blueprint / "roadmap").mkdir(parents=True)
    (blueprint / "coverage").mkdir()
    (blueprint / "coverage" / "README.md").write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n| Narrative | OUT | No target |\n",
        encoding="utf-8",
    )
    for stem, frontmatter in articles.items():
        page = blueprint / "roadmap" / f"{stem}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        head = "" if frontmatter is None else f"---\n{frontmatter}\n---\n\n"
        page.write_text(
            f"{head}# {stem}\n\nA precise statement.\n\n## Depends on\n\nNo prerequisites.\n", encoding="utf-8"
        )
    return blueprint


def _links(blueprint: Path, files: dict[str, list[object]], defaults: object = DEFAULTS) -> Path:
    """Write each file's annotations as JSON Lines below blueprint/links/; a string is written as the line itself."""

    folder = blueprint / "links"
    folder.mkdir(exist_ok=True)
    if defaults is not None:
        (folder / "_defaults.json").write_text(json.dumps(defaults), encoding="utf-8")
    for name, rows in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows), encoding="utf-8"
        )
    return folder


def _about(stem: str, **fields: object) -> dict[str, object]:
    """An annotation about roadmap/<stem>.md, written in a file directly in links/."""
    return {"subject": [{"type": "generic", "uri": f"../roadmap/{stem}.md"}], **fields}


def _own(name: str, role: str | None = None) -> dict[str, object]:
    """An end the defaults make a Lean declaration of the project."""
    return {"location": {"declaration": name}, **({} if role is None else {"role": role})}


def test_an_article_s_annotations_carry_its_facts(tmp_path: Path) -> None:
    blueprint = _blueprint(
        tmp_path, {"README": None, "lagrange": None, "card": None, "unlinked": None, "inventory": None}
    )
    _links(
        blueprint,
        {
            "lagrange.jsonl": [
                _about(
                    "lagrange",
                    id=ARTICLE_ID,
                    kind="theorem",
                    status="formalized",
                    # Credit goes by role, never by position.
                    object=[
                        _own("Groups.card_dvd", "supports"),
                        _own("Groups.lagrange", "states"),
                        # A fragment selects nothing on a Lean end, whose location is the selection.
                        {"type": "lean", "uri": "../..#readme", "location": {"declaration": "Groups.card_dvd"}},
                    ],
                    extensions={"org.autoform": {"origin": "cited", "not_ready": False, "discussion": 42, **REVIEWED}},
                ),
                # Two Mathlib names in one file give that file, and a query or fragment selects nothing there either.
                _about(
                    "lagrange",
                    id=ARTICLE_ID,
                    object=[
                        {**MATHLIB, "uri": MATHLIB["uri"] + "?tab=readme"},
                        {**SAME_FILE, "uri": MATHLIB["uri"] + "#readme"},
                    ],
                    extensions={"org.autoform": {"mathlib": True}},
                ),
            ],
            "README.jsonl": [_about("README", extensions={"org.autoform": {"navigation": "collection"}})],
            "card.jsonl": [
                # A subject end that is no roadmap article leaves the annotation about the one it names.
                {
                    **_about("card", kind="noncomputable def", status="formalized", object=[_own("Groups.card")]),
                    "subject": [*_about("card")["subject"], {"type": "generic", "uri": "../README.md"}],
                },
                # Mathlib's repository names a Mathlib declaration, without the flag that says the result is there.
                _about("card", object=[{**MATHLIB, "uri": MATHLIB["uri"] + ".git/"}]),
            ],
            "unlinked.jsonl": [_about("unlinked", object=[])],
            # A module catalog's status covers every name it inventories.
            "inventory.jsonl": [
                _about(
                    "inventory",
                    status="formalized",
                    object=[_own("Groups.Card.one"), _own("Groups.Card.two")],
                    extensions={"org.autoform": {"catalog": "module"}},
                )
            ],
        },
    )
    graph = load_graph(blueprint)

    assert graph.warnings == ()
    lagrange, card, unlinked = (graph.nodes[node_id] for node_id in ("lagrange", "card", "unlinked"))
    assert (lagrange.lean, lagrange.declaration, lagrange.article_id) == (
        "Groups.lagrange, Groups.card_dvd",
        "theorem",
        ARTICLE_ID,
    )
    assert (lagrange.statement_formalized, lagrange.proof_formalized) == (True, True)
    assert (lagrange.origin, lagrange.not_ready, lagrange.discussion) == ("cited", False, "42")
    assert (lagrange.area, lagrange.review_approved, graph.nodes["roadmap"].navigation) == (
        REVIEWED["area"],
        REVIEWED["review_approved"],
        "collection",
    )
    assert (lagrange.mathlib, lagrange.mathlib_declaration, lagrange.mathlib_file) == (
        True,
        "Subgroup.card_subgroup_dvd_card, Subgroup.index_mul_card",
        "Mathlib/GroupTheory/Coset/Card.lean",
    )
    # A definition is complete once stated, so "formalized" asserts no proof.
    assert (card.lean, card.statement_formalized, card.proof_formalized) == ("Groups.card", True, False)
    assert (card.mathlib, card.mathlib_declaration) == (False, "Subgroup.card_subgroup_dvd_card")
    assert (unlinked.lean, unlinked.declaration, unlinked.statement_formalized) == (None, None, False)
    inventory = graph.nodes["inventory"]
    assert (inventory.lean, inventory.catalog, inventory.statement_formalized, inventory.proof_formalized) == (
        "Groups.Card.one, Groups.Card.two",
        "module",
        True,
        True,
    )


@pytest.mark.parametrize(
    ("kind", "status", "stated", "retracted", "proved"),
    [
        ("theorem", "formalized", True, False, True),
        ("theorem", "stated", True, False, False),
        ("def", "formalized", True, False, False),
        ("def", "stated", True, False, False),
        ("theorem", "retracted", False, True, False),
        ("theorem", "not_formalized", False, False, False),
        # An axiom is never proved (status.py).
        ("axiom", "formalized", True, False, False),
    ],
)
def test_status_reads_as_frontmatter_did(
    tmp_path: Path, kind: str, status: str, stated: bool, retracted: bool, proved: bool
) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    _links(blueprint, {"result.jsonl": [_about("result", kind=kind, status=status, object=[_own("Result.main")])]})
    graph = load_graph(blueprint)
    node = graph.nodes["result"]

    assert graph.warnings == ()
    assert (node.statement_formalized, node.statement_retracted, node.proof_formalized) == (stated, retracted, proved)


def test_frontmatter_beside_an_annotation_is_ignored_but_for_the_settings(tmp_path: Path) -> None:
    blueprint = _blueprint(
        tmp_path,
        # Beside an annotation, frontmatter that would refuse the vault is ignored too.
        {"README": "open_statements: allowed\ntracks_lean: false\ndeclaration: theorem", "result": "statement: bogus"},
    )
    _links(blueprint, {"index.jsonl": [_about("README", object=[]), _about("result", kind="lemma")]})
    graph = load_graph(blueprint)

    assert (graph.open_statements, graph.tracks_lean) == (True, False)
    assert (graph.nodes["roadmap"].declaration, graph.nodes["result"].declaration) == (None, "lemma")
    assert graph.warnings == (f"{BESIDE} (2): roadmap, result",)


def test_an_article_without_an_annotation_keeps_its_frontmatter(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"old": "declaration: theorem", "new": None, "plain": None})
    assert load_graph(blueprint).warnings == ()

    _links(blueprint, {"new.jsonl": [_about("new", kind="lemma")]})
    graph = load_graph(blueprint)

    assert (graph.nodes["old"].declaration, graph.nodes["new"].declaration) == ("theorem", "lemma")
    # Hand-made annotations do not mark a moved project: check's deprecation covers its frontmatter facts.
    assert graph.warnings == ()

    (blueprint / "links/roadmap").mkdir()
    (blueprint / "links/roadmap/_defaults.json").write_text('{"object": {"type": "lean", "uri": "../../.."}}\n')
    assert load_graph(blueprint).warnings == (
        "articles with no annotation keep their frontmatter facts until 0.8.0, which stops reading them; "
        "move them into blueprint/links/ with `autoform migrate links blueprint --write` (1): old",
    )


def test_every_fact_left_out_is_warned_about_once_for_each_reason(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {stem: None for stem in "abcdefghijklmnopqrstuwx"})
    mathlib = {"org.autoform": {"mathlib": True}}
    _links(
        blueprint,
        {
            "bad.jsonl": [
                "not json",
                "[1]",
                _about("gone"),
                {"subject": [*_about("a")["subject"], *_about("b")["subject"]]},
                _about("a", status="formalized", object=[]),
                _about("b", status="formalized", object=[_own("B.one"), _own("B.two")]),
                _about("c", status="partial"),
                _about(
                    "d",
                    kind=5,
                    # Nothing a file writes breaks a line, here in a key's name.
                    extensions={
                        "org.autoform": {"not_ready": "yes", "colour\nwarning: forged": "red", "published": False}
                    },
                ),
                _about("e", kind="theorem"),
                _about("e", kind="lemma"),
                _about(
                    "f",
                    status="formalized",
                    object=[
                        {
                            "type": "lean",
                            "uri": "https://example.com/other",
                            "location": {"declaration": "F"},
                            "role": "states",
                        },
                        _own("F.helper"),
                    ],
                ),
                _about("g", extensions={"org.autoform": {"origin": "invented"}}),
                _about("h", kind="theorem", extensions={"org.autoform": {"catalog": "module"}}),
                _about("i", extensions={"org.autoform": 1}),
                _about(
                    "j",
                    status="formalized",
                    object=[{"location": {"module": "J.Module"}, "role": "states"}, _own("J.helper")],
                ),
                _about("k", status="retracted", object=[_own("K.main")]),
                _about("k", object=[MATHLIB], extensions=mathlib),
                # Nothing a file writes breaks a line, in a warning or on the site.
                _about("l", kind="theorem\n\nwarning: forged", object=[{"type": "Lean"}]),
                # One end with a role other than states takes no credit.
                _about("m", status="stated", object=[_own("M.main", "supports")]),
                # The same declaration twice is still one.
                _about("n", status="stated", object=[_own("N.main")]),
                _about("n", object=[_own("N.main")]),
                # A grouping id gives way to Autoform's.
                _about("o", id="lagrange"),
                _about("o", id=ARTICLE_ID),
                # Frontmatter holds one Mathlib file.
                _about(
                    "p",
                    object=[MATHLIB, {**MATHLIB, "location": {"declaration": "P.two", "module": "Mathlib.P"}}],
                    extensions=mathlib,
                ),
                # The flag says the result is in Mathlib; only an end naming Mathlib's repository names its declaration.
                _about(
                    "q", object=[{**MATHLIB, "uri": "https://github.com/ImperialCollegeLondon/FLT"}], extensions=mathlib
                ),
                # Stated by Mathlib, or by an end left out, the article credits none of the project's declarations.
                _about("r", status="formalized", object=[{**MATHLIB, "role": "states"}, _own("R.helper")]),
                _about("s", status="formalized", object=[_own("S.main\a", "states"), _own("S.helper")]),
                # Two declarations with role states are not one.
                _about("u", status="formalized", object=[_own("U.one", "states"), _own("U.two", "states")]),
                # Another project as a whole can state the article too.
                _about(
                    "w",
                    status="formalized",
                    object=[{"type": "lean", "uri": "https://example.com/other", "role": "states"}, _own("W.main")],
                ),
                # So can an end left out for its type.
                _about(
                    "x",
                    status="formalized",
                    object=[
                        {"type": "Lean", "location": {"declaration": "X.main"}, "role": "states"},
                        _own("X.helper"),
                    ],
                ),
                # The file is the first name's, which gives none.
                _about(
                    "t",
                    object=[
                        {**MATHLIB, "location": {"declaration": "T.one"}},
                        {**MATHLIB, "location": {"declaration": "T.two", "module": "Mathlib.T"}},
                    ],
                ),
            ]
        },
    )
    graph = load_graph(blueprint)

    assert graph.warnings == (
        "annotations that are not JSON are left out (1): links/bad.jsonl:1 (not JSON: Expecting value: line 1 column 1 "
        "(char 0))",
        "annotations that are not JSON objects are left out (1): links/bad.jsonl:2",
        "annotations about no roadmap article are left out (1): links/bad.jsonl:3",
        "annotations about several articles are left out (1): links/bad.jsonl:4",
        "a status needs a Lean declaration of this project, so it is left out (1): a",
        "a status needs one Lean declaration with role states, or a single one with no role, so it is left out "
        "(3): b, m, u",
        "status values Autoform does not read are left out (1): c (partial)",
        "annotation values Autoform does not accept are left out (8): d (not_ready), d (kind), i (extensions), "
        "l (kind), l (object type) and 3 more",
        "unknown org.autoform keys are left out (1): d (colour\\nwarning: forged)",
        "settings stay in README frontmatter, so in an annotation they are left out (1): d (published)",
        "an article's annotations give one key several values, so the first is kept (1): e (kind)",
        "Lean ends whose uri is neither this project nor https://github.com/leanprover-community/mathlib4 are left out "
        "(2): f (F), q (Subgroup.card_subgroup_dvd_card)",
        "a status is left out when an end with role states is not a declaration of this project (6): f, j, r, s, w "
        "and 1 more",
        "catalog is left out beside a kind or a Mathlib link (1): h",
        "Lean ends that name no declaration are left out (1): j",
        "status retracted is left out beside org.autoform mathlib true; to retract, also unset mathlib, "
        "mathlib_declaration and mathlib_file (1): k",
        "Mathlib modules other than the first name's are left out (2): p (Mathlib.P), t (Mathlib.T)",
    )
    assert (graph.nodes["b"].lean, graph.nodes["b"].statement_formalized) == ("B.one, B.two", False)
    assert (graph.nodes["e"].declaration, graph.nodes["h"].catalog, graph.nodes["l"].declaration) == (
        "theorem",
        None,
        None,
    )
    assert graph.nodes["n"].statement_formalized and not graph.nodes["m"].statement_formalized
    assert graph.nodes["o"].article_id == ARTICLE_ID
    assert (graph.nodes["q"].mathlib, graph.nodes["q"].mathlib_declaration) == (True, None)
    assert [(graph.nodes[stem].mathlib_declaration, graph.nodes[stem].mathlib_file) for stem in "pt"] == [
        ("Subgroup.card_subgroup_dvd_card, P.two", "Mathlib/GroupTheory/Coset/Card.lean"),
        ("T.one, T.two", None),
    ]
    assert [(graph.nodes[stem].lean, graph.nodes[stem].statement_formalized) for stem in "rs"] == [
        ("R.helper", False),
        ("S.helper", False),
    ]


def test_a_warning_names_the_first_five_places(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    _links(blueprint, {"gone.jsonl": [_about(f"gone-{index}") for index in range(7)]})

    assert load_graph(blueprint).warnings == (
        "annotations about no roadmap article are left out (7): links/gone.jsonl:1, links/gone.jsonl:2, "
        "links/gone.jsonl:3, links/gone.jsonl:4, links/gone.jsonl:5 and 2 more",
    )


def test_an_annotation_about_part_of_an_article_is_left_out(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    # A fragment or a query names a part of the page, and a backslash separates folders on Windows alone.
    uris = [
        "../roadmap/result.md#proof",
        "../roadmap/result.md?view=1",
        "..\\roadmap\\result.md",
        "//[x/roadmap/result.md",
    ]
    rows = [{"subject": [{"type": "generic", "uri": uri}], "kind": "lemma"} for uri in uris]
    whole = _about("result")["subject"]
    # So does a location, even an empty one.
    rows.insert(2, {"subject": [{**whole[0], "location": {}}], "kind": "lemma"})
    # Beside the whole article, part of another still makes two, and part of the same one changes nothing.
    other, same = ({"type": "generic", "uri": f"../roadmap/{stem}.md#proof"} for stem in ("other", "result"))
    _links(
        blueprint,
        {
            "parts.jsonl": [
                *rows,
                {"subject": [*whole, other], "kind": "lemma"},
                {"subject": [*whole, same], "kind": "theorem"},
            ]
        },
    )
    graph = load_graph(blueprint)

    assert graph.nodes["result"].declaration == "theorem"
    assert graph.warnings == (
        "annotations about part of an article are left out (3): links/parts.jsonl:1, links/parts.jsonl:2, "
        "links/parts.jsonl:3",
        "annotations about no roadmap article are left out (2): links/parts.jsonl:4, links/parts.jsonl:5",
        "annotations about several articles are left out (1): links/parts.jsonl:6",
    )


def test_warnings_name_places_in_one_order_on_every_disk(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    folder = _links(blueprint, {}, defaults=None)
    for name in "bca":  # made out of order, as a disk may list them
        (folder / name).mkdir()
        (folder / name / "_defaults.json").write_text("[]", encoding="utf-8")

    assert load_graph(blueprint).warnings == (
        "defaults files that do not hold one JSON object are left out (3): links/a/_defaults.json, "
        "links/b/_defaults.json, links/c/_defaults.json",
    )


@pytest.mark.parametrize("annotated", ["first", "second"])
def test_an_id_another_article_uses_is_left_out_without_refusing(tmp_path: Path, annotated: str) -> None:
    other = "second" if annotated == "first" else "first"
    blueprint = _blueprint(tmp_path, {annotated: None, other: f"article_id: {ARTICLE_ID}"})
    _links(blueprint, {f"{annotated}.jsonl": [_about(annotated, id=ARTICLE_ID)]})
    graph = load_graph(blueprint)

    # The first article loaded keeps the ID, whichever of the two carries it in an annotation.
    assert (graph.nodes["first"].article_id, graph.nodes["second"].article_id) == (ARTICLE_ID, None)
    assert "article IDs another article already uses are left out (1): second (first)" in graph.warnings


def test_a_value_frontmatter_would_refuse_is_left_out_saying_what_the_key_accepts(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    _links(blueprint, {"result.jsonl": [_about("result", extensions={"org.autoform": {"origin": "invented"}})]})

    assert load_graph(blueprint).warnings == (
        "annotation values Autoform does not accept are left out (1): result ('origin' accepts cited, bridged, or "
        "background)",
    )


def test_an_annotation_nested_too_deeply_is_left_out(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    deep = json.loads("[" * 600 + "]" * 600)
    _links(blueprint, {"result.jsonl": [_about("result", kind="lemma", extensions={"x": deep})]})
    graph = load_graph(blueprint)

    assert graph.nodes["result"].declaration is None
    assert graph.warnings == ("annotations nested too deeply to read are left out (1): links/result.jsonl:1",)


def test_a_symbolic_link_in_the_folder_is_not_followed(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text(json.dumps(_about("result", kind="lemma")) + "\n", encoding="utf-8")
    folder = _links(blueprint, {})
    try:
        (folder / "result.jsonl").symlink_to(elsewhere)
    except OSError:
        pytest.skip("symlinks are unavailable")
    graph = load_graph(blueprint)

    assert graph.nodes["result"].declaration is None
    assert graph.warnings == ("symbolic links in blueprint/links/ are not followed (1): links/result.jsonl",)


def test_annotations_are_read_as_literature_links_reads_them(tmp_path: Path) -> None:
    """Defaults in nested folders merge as discovery merges them, in a JSON file too, without its validation."""

    folder = tmp_path / "links"
    defaults = {**DEFAULTS, "extensions": {"org.autoform": {"origin": "cited"}}}
    files = {
        "a.jsonl": [_about("a", kind="theorem", object=[_own("A.main", "states"), MATHLIB])],
        "nested/_defaults.json": {"object": {"version": "v1"}, "status": "stated"},
        # A null removes what the defaults give; an end with its own uri takes nothing from them.
        "nested/b.jsonl": [
            {
                "subject": [{"type": "generic", "uri": "../../roadmap/nested/b.md"}],
                "object": [_own("B.main"), {"type": "lean", "uri": "../../..", "version": "own"}],
                "extensions": {"org.autoform": {"origin": None}},
            }
        ],
        "nested/c.json": {"subject": [{"type": "generic", "uri": "../../roadmap/nested/c.md"}], "status": None},
    }
    _links(tmp_path, {name: [rows] if name.endswith(".json") else rows for name, rows in files.items()}, defaults)
    (folder / "nested" / "b.jsonl").write_bytes((folder / "nested" / "b.jsonl").read_bytes().replace(b"\n", b"\r\n"))
    drops = Drops()
    ours = [row[0] for row in _annotations(folder, *_walk(folder, drops), drops)]

    assert ours == [record.annotation for record in discovery.collect([folder], root=folder).records]
    assert len(ours) == 3 and drops.warnings() == []


def test_a_defaults_file_that_cannot_be_used_leaves_out_the_annotations_below_it(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"top": None, "nested/README": None, "nested/below": None})
    folder = _links(
        blueprint,
        {
            "top.jsonl": [_about("top", kind="lemma")],
            "nested/below.jsonl": [
                {"subject": [{"type": "generic", "uri": "../../roadmap/nested/below.md"}], "kind": "lemma"}
            ],
        },
    )
    (folder / "nested" / "_defaults.json").write_text("[]", encoding="utf-8")
    graph = load_graph(blueprint)

    assert (graph.nodes["top"].declaration, graph.nodes["nested/below"].declaration) == ("lemma", None)
    assert graph.warnings == (
        "defaults files that do not hold one JSON object are left out (1): links/nested/_defaults.json",
        "annotation files below a defaults file that cannot be used are left out (1): links/nested/below.jsonl",
    )
    # Nested deeper than literature-links merges, it is left out the same way, never refusing the vault.
    deep = {"object": {"version": "v1"}, "extensions": {"x": json.loads("[" * 600 + "]" * 600)}}
    (folder / "nested" / "_defaults.json").write_text(json.dumps(deep), encoding="utf-8")
    assert load_graph(blueprint).warnings == (
        "annotation files below a defaults file that cannot be used are left out (1): links/nested/below.jsonl",
    )


@pytest.mark.parametrize("spelling", ["Links", "links-file", "links-symlink"])
def test_a_folder_that_is_not_links_is_read_nowhere(tmp_path: Path, spelling: str, capsys) -> None:
    blueprint = _blueprint(tmp_path, {"result": None, "old": "declaration: theorem"})
    revision = load_runtime_graph(blueprint).source_revision
    if spelling == "Links":
        _links(blueprint, {"result.jsonl": [_about("result", kind="lemma")]})
        (blueprint / "links").rename(blueprint / "Links")
        warning = (
            "a folder spelled other than links is not read; rename it (in Git: git mv through a temporary name) "
            "(1): Links"
        )
    else:
        if spelling == "links-file":
            (blueprint / "links").write_text("not a folder", encoding="utf-8")
        else:
            (tmp_path / "elsewhere").mkdir()
            elsewhere = _links(tmp_path / "elsewhere", {"result.jsonl": [_about("result", kind="lemma")]})
            try:
                (blueprint / "links").symlink_to(elsewhere, target_is_directory=True)
            except OSError:
                pytest.skip("symlinks are unavailable")
        warning = "blueprint/links is not a folder, so no annotation is read (1): links"
    graph = load_graph(blueprint)

    # Nothing is read, so nothing else changes: no article is told to move its facts, and no revision moves.
    assert (graph.nodes["result"].declaration, graph.warnings) == (None, (warning,))
    assert (graph.links_sha256, load_runtime_graph(blueprint).source_revision) == (None, revision)
    if spelling == "Links":
        assert main(["render", str(blueprint), "--output", str(tmp_path / "site")]) == 0
        assert not [
            path for path in (tmp_path / "site").rglob("*") if path.name.casefold() in {"links", "result.jsonl"}
        ]


def test_a_file_that_is_not_utf_8_is_left_out(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    (_links(blueprint, {}) / "result.jsonl").write_bytes(b"\xff\n")

    assert load_graph(blueprint).warnings == (
        "annotation files that are not UTF-8 are left out (1): links/result.jsonl",
    )


def test_check_adds_literature_links_validation_and_keeps_its_exit_status(tmp_path: Path, capsys) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    _links(blueprint, {"result.jsonl": [_about("result", kind="lemma", colour="red"), "not json"]})
    left_out = "annotations that are not JSON are left out (1): links/result.jsonl:2"

    assert main(["check", str(blueprint)]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert (
        "warning: literature-links: is not an allowed key here (with defaults from links/_defaults.json) (1): "
        "links/result.jsonl:1 $.colour"
    ) in printed
    # What loading already warns about, check does not say again.
    assert [line for line in printed if "result.jsonl:2" in line] == [
        line for line in printed if line.startswith(f"warning: {left_out}")
    ]
    graph = load_graph(blueprint)
    assert graph.nodes["result"].declaration == "lemma"
    assert [warning.split(" (not JSON")[0] for warning in graph.warnings] == [left_out]


@pytest.mark.parametrize("unreadable", [True, False], ids=["unreadable", "defaults-alone"])
def test_check_says_nothing_again_that_loading_warns_about(tmp_path: Path, unreadable: bool, capsys) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    folder = _links(blueprint, {})
    if unreadable:
        below = [_about("result")]
        _links(
            blueprint, {"shape.jsonl": ["[1]"], **{f"{name}/below.jsonl": below for name in ("bad", "odd", "linked")}}
        )
        (folder / "bytes.jsonl").write_bytes(b"\xff\n")
        (folder / "bad" / "_defaults.json").write_text("[]", encoding="utf-8")
        (folder / "odd" / "_defaults.json").mkdir()
        try:
            (folder / "linked" / "_defaults.json").symlink_to(folder / "_defaults.json")
        except OSError:
            pass  # the other folders still show it
    loaded = load_graph(blueprint).warnings

    assert main(["check", str(blueprint)]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert bool(loaded) is unreadable and all(f"warning: {warning}" in printed for warning in loaded)
    # Nor does check report a folder of defaults alone, which holds no annotation yet.
    assert not [line for line in printed if line.startswith("warning: literature-links:")]


def test_review_refuses_annotations_changed_after_its_snapshot(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    _links(blueprint, {"result.jsonl": [_about("result", kind="lemma")]})
    earlier = dataclasses.replace(load_graph(blueprint), links_sha256="0" * 64)

    with pytest.raises(ReviewError) as refused:
        _review_disclosures(blueprint, earlier, None, {}, {}, ())
    assert [(finding.code, finding.reason.split(" while")[0]) for finding in refused.value.findings] == [
        ("review-snapshot-changed", "blueprint/links/ changed")
    ]


def test_render_reads_the_annotations_and_never_publishes_them(tmp_path: Path, capsys) -> None:
    blueprint = _blueprint(tmp_path, {"result": None, "drafts/README": "published: false", "drafts/idea": None})
    both = [*_about("result")["subject"], *_about("drafts/idea")["subject"]]
    folder = _links(
        blueprint,
        {
            "result.jsonl": [_about("result", kind="theorem")],
            # About two articles, one unpublished: left out by render's copy too, which lacks that one.
            "drafts.jsonl": [_about("drafts/idea", kind="lemma"), {"subject": both, "kind": "definition"}],
        },
    )
    (folder / "notes.md").write_text("# Notes\n", encoding="utf-8")
    page = blueprint / "roadmap" / "result.md"
    text = page.read_text(encoding="utf-8").replace("statement.", "statement. See [the notes](../links/notes.md).")
    page.write_text(text, encoding="utf-8")

    assert main(["render", str(blueprint), "--output", str(tmp_path / "site")]) == 0
    assert "links/" not in capsys.readouterr().out
    assert not (tmp_path / "site" / "links").exists()
    assert not list((tmp_path / "site").rglob("*.jsonl"))
    # A link into the folder keeps only its text, as a link to any file the site leaves out does.
    published = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "site").rglob("*.md"))
    assert "the notes" in published and "notes.md" not in published
    # Nor can a review cite a passage from there.
    assert _left_out(PurePosixPath("links/notes.md"), ())
    # The kind comes from the annotation, so the site counts the article as a result.
    index = json.loads((tmp_path / "site" / "dependencies" / "index.json").read_text(encoding="utf-8"))
    records = {record["id"]: record for record in index["nodes"]}
    assert records["result"]["result"] is True and "drafts/idea" not in records


def test_revisions_cover_the_annotations(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    before = load_runtime_graph(blueprint).source_revision, blueprint_hash(load_graph(blueprint))
    folder = _links(blueprint, {"result.jsonl": [_about("result", kind="lemma")]})
    linked = load_runtime_graph(blueprint).source_revision, blueprint_hash(load_graph(blueprint))
    (folder / "result.jsonl").write_text(json.dumps(_about("result", kind="theorem")) + "\n", encoding="utf-8")
    edited = load_runtime_graph(blueprint).source_revision, blueprint_hash(load_graph(blueprint))
    # Hidden files are read nowhere, so they change nothing.
    (folder / ".DS_Store").write_bytes(b"\0")
    (folder / ".draft.jsonl").write_text(json.dumps(_about("result", kind="lemma")) + "\n", encoding="utf-8")
    hidden = load_runtime_graph(blueprint).source_revision, load_graph(blueprint).nodes["result"].declaration
    (folder / "result.jsonl").rename(folder / "renamed.jsonl")
    renamed = load_runtime_graph(blueprint).source_revision, blueprint_hash(load_graph(blueprint))
    # A defaults file that cannot be used changes what loads below it, though no byte read changes.
    (folder / "nested" / "_defaults.json").mkdir(parents=True)
    unusable = load_runtime_graph(blueprint).source_revision, blueprint_hash(load_graph(blueprint))

    # Without the folder, the revision is the one main gives.
    assert before[0] == "0571905d3806a4ac230097caca7aa5b49f3d4753f53172594ac959aca323dd99"
    assert hidden == (edited[0], "theorem")
    assert len({before[0], linked[0], edited[0], renamed[0], unusable[0]}) == 5
    assert len({before[1], linked[1], edited[1], renamed[1], unusable[1]}) == 5


def test_the_article_revision_covers_its_annotation_and_no_other_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The project sits in a folder of its own, so the debrief store below is outside it.
    blueprint = _blueprint(
        tmp_path / "project",
        {"result": None, "other": None, "plain": "declaration: lemma", "bare": "declaration: lemma"},
    )

    def revisions(result: str, other: str) -> dict[str, str | None]:
        rows = [_about("result", kind=result, id=ARTICLE_ID), _about("other", kind=other), _about("bare")]
        _links(blueprint, {"both.jsonl": rows})
        return {node_id: node.article_revision for node_id, node in load_graph(blueprint).nodes.items()}

    before = revisions("lemma", "lemma")
    sibling = revisions("lemma", "theorem")
    edited = revisions("theorem", "theorem")

    digest = hashlib.sha256((blueprint / "roadmap" / "result.md").read_bytes()).hexdigest()
    # Keys in sorted order, which is not the order loading reads them in.
    facts = json.dumps({"article_id": ARTICLE_ID, "declaration": "lemma"}, separators=(",", ":"))
    assert before["result"] == hashlib.sha256(f"autoform-article/v1\0{digest}{facts}".encode()).hexdigest()
    # A line about another article in the same file leaves the revision alone; an edit to its own moves it.
    assert sibling["result"] == before["result"] and sibling["other"] != before["other"]
    assert edited["result"] != sibling["result"] and edited["other"] == sibling["other"]
    # An article no annotation counts for keeps the hash of its bytes, the value without the folder.
    plain = hashlib.sha256((blueprint / "roadmap" / "plain.md").read_bytes()).hexdigest()
    assert before["plain"] == edited["plain"] == plain
    # An annotation with no facts still counts, so the frontmatter beside it is not what the revision covers.
    bare = hashlib.sha256((blueprint / "roadmap" / "bare.md").read_bytes()).hexdigest()
    assert before["bare"] == hashlib.sha256(f"autoform-article/v1\0{bare}{{}}".encode()).hexdigest()
    # Each command that reports the revision reports this one.
    monkeypatch.delenv(debrief.DEBRIEF_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    entry, _ = debrief.record(blueprint, "result", '{"nothing_to_report": true}', phase="statement", worker_id="w1")
    projected = json.loads(load_runtime_graph(blueprint).to_json())["nodes"]
    assert {
        load_runtime_graph(blueprint).get("result").article_revision,
        next(node["article_revision"] for node in projected if node["id"] == "result"),
        work_context(blueprint, "result")[1].article_revision,
        {hit.node_id: hit.article_revision for hit in search_blueprint(blueprint, "result").hits}["result"],
        entry.article_revision,
    } == {edited["result"]}


def test_literature_links_is_imported_only_for_a_folder(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": "declaration: theorem"})
    probe = (
        "import sys\nfrom autoform_cli.graph import load_graph\n"
        f"load_graph({str(blueprint)!r})\nprint('literature_links' in sys.modules)\n"
    )

    assert subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout == "False\n"
    _links(blueprint, {})
    assert subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout == "True\n"
