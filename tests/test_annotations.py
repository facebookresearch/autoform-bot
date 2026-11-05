"""Annotations in blueprint/links/: the facts they carry, every fact left out warned about, and what reads them."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from literature_links import discovery

from autoform_cli.__main__ import main
from autoform_cli.annotations import Drops, _annotations, _walk
from autoform_cli.graph import load_graph
from autoform_cli.render import load_publication_source
from autoform_cli.runtime import load_runtime_graph
from autoform_cli.skeleton import blueprint_hash

ARTICLE_ID = "af_f8d7b54996f51594f707d44f"
#: The project, from links/_defaults.json, is two folders up: links/, then blueprint/.
DEFAULTS = {"object": {"type": "lean", "uri": "../.."}}
MATHLIB = {
    "type": "lean",
    "uri": "https://github.com/leanprover-community/mathlib4",
    "version": "v4.33.0",
    "location": {"declaration": "Subgroup.card_subgroup_dvd_card", "module": "Mathlib.GroupTheory.Coset.Card"},
}
BESIDE = (
    "frontmatter beside an annotation is ignored, except coverage_list_limit, open_statements, published, tracks_lean"
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
    blueprint = _blueprint(tmp_path, {"lagrange": None, "card": None, "unlinked": None})
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
                    object=[_own("Groups.card_dvd", "supports"), _own("Groups.lagrange", "states")],
                    extensions={"org.autoform": {"origin": "cited", "not_ready": False, "discussion": 42}},
                ),
                _about("lagrange", id=ARTICLE_ID, object=[MATHLIB], extensions={"org.autoform": {"mathlib": True}}),
            ],
            "card.jsonl": [_about("card", kind="noncomputable def", status="formalized", object=[_own("Groups.card")])],
            "unlinked.jsonl": [_about("unlinked", object=[])],
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
    assert (lagrange.mathlib, lagrange.mathlib_declaration, lagrange.mathlib_file) == (
        True,
        "Subgroup.card_subgroup_dvd_card",
        "Mathlib/GroupTheory/Coset/Card.lean",
    )
    # A definition is complete once stated, so "formalized" asserts no proof.
    assert (card.lean, card.statement_formalized, card.proof_formalized) == ("Groups.card", True, False)
    assert (unlinked.lean, unlinked.declaration, unlinked.statement_formalized) == (None, None, False)


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
    assert graph.warnings == (
        "articles with no annotation keep their frontmatter facts; move them into blueprint/links/ (1): old",
    )


def test_every_fact_left_out_is_warned_about_once_for_each_reason(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {stem: None for stem in "abcdefghijklmnop"})
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
                    "d", kind=5, extensions={"org.autoform": {"not_ready": "yes", "colour": "red", "published": False}}
                ),
                _about("e", kind="theorem"),
                _about("e", kind="lemma"),
                _about(
                    "f", object=[{"type": "lean", "uri": "https://example.com/other", "location": {"declaration": "F"}}]
                ),
                _about("g", extensions={"org.autoform": {"origin": "invented"}}),
                _about("h", kind="theorem", extensions={"org.autoform": {"catalog": "module"}}),
                _about("i", extensions={"org.autoform": 1}),
                _about("j", object=[{"location": {"module": "J.Module"}}]),
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
        "a status needs one Lean declaration with role states, or a single one with no role, so it is left out (2): b, m",
        "status values Autoform does not read are left out (1): c (partial)",
        "annotation values Autoform does not accept are left out (6): d (not_ready), d (kind), i (extensions), l (kind), "
        "l (object type) and 1 more",
        "unknown org.autoform keys are left out (1): d (colour)",
        "settings stay in README frontmatter, so in an annotation they are left out (1): d (published)",
        "an article's annotations give one key several values, so the first is kept (2): e (kind), p (location.module)",
        "Lean ends naming another project are left out, unless their annotation sets org.autoform mathlib (1): f",
        "catalog is left out beside a kind or a Mathlib link (1): h",
        "Lean ends that name no declaration are left out (1): j",
        "status retracted is left out beside org.autoform mathlib true (1): k",
    )
    assert (graph.nodes["b"].lean, graph.nodes["b"].statement_formalized) == ("B.one, B.two", False)
    assert (graph.nodes["e"].declaration, graph.nodes["h"].catalog, graph.nodes["l"].declaration) == (
        "theorem",
        None,
        None,
    )
    assert graph.nodes["n"].statement_formalized and not graph.nodes["m"].statement_formalized
    assert graph.nodes["o"].article_id == ARTICLE_ID


def test_a_warning_names_the_first_five_places(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    _links(blueprint, {"gone.jsonl": [_about(f"gone-{index}") for index in range(7)]})

    assert load_graph(blueprint).warnings == (
        "annotations about no roadmap article are left out (7): links/gone.jsonl:1, links/gone.jsonl:2, "
        "links/gone.jsonl:3, links/gone.jsonl:4, links/gone.jsonl:5 and 2 more",
    )


def test_an_id_another_article_uses_is_left_out_without_refusing(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"first": f"article_id: {ARTICLE_ID}", "second": None})
    _links(blueprint, {"second.jsonl": [_about("second", id=ARTICLE_ID)]})
    graph = load_graph(blueprint)

    assert (graph.nodes["first"].article_id, graph.nodes["second"].article_id) == (ARTICLE_ID, None)
    assert "article IDs another article already uses are left out (1): second (first)" in graph.warnings


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


@pytest.mark.parametrize("spelling", ["Links", "links-file"])
def test_a_folder_that_is_not_links_is_read_nowhere(tmp_path: Path, spelling: str, capsys) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    if spelling == "Links":
        _links(blueprint, {"result.jsonl": [_about("result", kind="lemma")]})
        (blueprint / "links").rename(blueprint / "Links")
        warning = "a folder spelled other than links is not read; rename it (in Git: git mv through a temporary name) (1): Links"
    else:
        (blueprint / "links").write_text("not a folder", encoding="utf-8")
        warning = "blueprint/links is not a folder, so no annotation is read (1): links"
    graph = load_graph(blueprint)

    assert (graph.nodes["result"].declaration, graph.warnings) == (None, (warning,))
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
    _links(blueprint, {"result.jsonl": [_about("result", kind="lemma", colour="red")]})

    assert main(["check", str(blueprint)]) == 0
    assert (
        "warning: literature-links: is not an allowed key here (with defaults from links/_defaults.json) (1): "
        "links/result.jsonl:1 $.colour"
    ) in capsys.readouterr().out.splitlines()
    graph = load_graph(blueprint)
    assert (graph.nodes["result"].declaration, graph.warnings) == ("lemma", ())


def test_render_reads_the_annotations_and_never_publishes_them(tmp_path: Path, capsys) -> None:
    blueprint = _blueprint(tmp_path, {"result": None, "drafts/README": "published: false", "drafts/idea": None})
    _links(
        blueprint,
        {
            "result.jsonl": [_about("result", kind="theorem")],
            "drafts.jsonl": [_about("drafts/idea", kind="lemma")],
        },
    )

    assert main(["render", str(blueprint), "--output", str(tmp_path / "site")]) == 0
    assert "links/" not in capsys.readouterr().out
    assert not (tmp_path / "site" / "links").exists()
    assert not list((tmp_path / "site").rglob("*.jsonl"))
    revision, graph = load_publication_source(blueprint)
    assert graph.nodes["result"].declaration == "theorem"
    assert "drafts/idea" not in graph.nodes


def test_revisions_cover_the_annotations(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": None})
    before = load_runtime_graph(blueprint).source_revision, blueprint_hash(load_graph(blueprint))
    folder = _links(blueprint, {"result.jsonl": [_about("result", kind="lemma")]})
    linked = load_runtime_graph(blueprint).source_revision, blueprint_hash(load_graph(blueprint))
    (folder / "result.jsonl").write_text(json.dumps(_about("result", kind="theorem")) + "\n", encoding="utf-8")
    edited = load_runtime_graph(blueprint).source_revision, blueprint_hash(load_graph(blueprint))

    assert len({before[0], linked[0], edited[0]}) == 3
    assert len({before[1], linked[1], edited[1]}) == 3


@pytest.mark.parametrize("assigned", [False, True], ids=["an-id-to-write", "none-to-write"])
def test_migrate_article_ids_writes_nothing_beside_the_folder(tmp_path: Path, assigned: bool, capsys) -> None:
    blueprint = _blueprint(tmp_path, {"result": f"article_id: {ARTICLE_ID}" if assigned else "declaration: theorem"})
    _links(blueprint, {})
    page = (blueprint / "roadmap" / "result.md").read_bytes()

    assert main(["migrate", "article-ids", str(blueprint), "--write"]) == (0 if assigned else 2)
    assert ("nothing written" in capsys.readouterr().err) is not assigned
    assert (blueprint / "roadmap" / "result.md").read_bytes() == page


def test_literature_links_is_imported_only_for_a_folder(tmp_path: Path) -> None:
    blueprint = _blueprint(tmp_path, {"result": "declaration: theorem"})
    probe = (
        "import sys\nfrom autoform_cli.graph import load_graph\n"
        f"load_graph({str(blueprint)!r})\nprint('literature_links' in sys.modules)\n"
    )

    assert subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout == "False\n"
    _links(blueprint, {})
    assert subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout == "True\n"
