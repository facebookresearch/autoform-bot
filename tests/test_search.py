from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from autoform_cli import __main__ as cli, search as search_module
from autoform_cli.runtime import RuntimeProjectionError, load_runtime_graph
from autoform_cli.search import SEARCH_SCHEMA, SearchError, search_blueprint, statement_preview


_LEAN_SOURCE = "namespace Project\n\ntheorem separation : True := trivial\n\nend Project\n"


def _article(
    project: Path,
    relative: str,
    *,
    title: str,
    body: str = "A precise statement.",
    metadata: tuple[str, ...] = (),
    depends: tuple[str, ...] = (),
) -> Path:
    path = project / "blueprint/roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", *metadata, "---", "", f"# {title}", "", body]
    if depends:
        lines.extend(["", "## Depends on", "", *(f"- [dependency]({target})" for target in depends)])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    _article(project, "README.md", title="Book")
    _article(project, "convexity/README.md", title="Convex analysis")
    _article(
        project,
        "convexity/hyperplane.md",
        title="Separating hyperplane",
        body="Two disjoint convex sets are separated by an affine functional.",
        metadata=("declaration: theorem", "statement: formalized", "lean: Project.separation"),
    )
    _article(
        project,
        "convexity/support.md",
        title="Supporting functional",
        body="A closed convex set is the intersection of its supporting half-spaces.",
        metadata=("declaration: lemma",),
        depends=("hyperplane.md",),
    )
    return project


def _ids(project: Path, query: str, **options: object) -> list[str]:
    return [hit.node_id for hit in search_blueprint(project, query, **options).hits]


def _matched(project: Path, query: str) -> dict[str, tuple[str, ...]]:
    return {hit.node_id: hit.matched_fields for hit in search_blueprint(project, query).hits}


def test_finds_an_article_by_a_word_in_its_statement_title_or_lean_name(tmp_path: Path) -> None:
    project = _project(tmp_path)

    assert _matched(project, "affine") == {"convexity/hyperplane": ("statement_text",)}
    assert _matched(project, "separating") == {"convexity/hyperplane": ("title",)}
    assert _matched(project, "separation") == {"convexity/hyperplane": ("lean",)}


def test_every_term_must_occur_but_terms_may_sit_in_different_fields(tmp_path: Path) -> None:
    project = _project(tmp_path)

    assert _matched(project, "separating affine") == {"convexity/hyperplane": ("title", "statement_text")}
    assert _ids(project, "separating nowhere") == []
    assert search_blueprint(project, "Affine  affine AFFINE").terms == ("affine",)


def test_a_directory_or_namespace_is_the_weakest_match(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "measure/README.md", title="Integration")
    _article(project, "measure/law.md", title="Law", metadata=("declaration: def", "lean: Project.Measure.law"))
    _article(project, "convexity/prob.md", title="Probability", body="A probability measure has mass one.")

    assert _matched(project, "measure") == {
        "convexity/prob": ("statement_text",),
        "measure": ("node_id",),
        "measure/law": ("qualified_names",),
    }
    assert _ids(project, "measure") == ["measure", "convexity/prob", "measure/law"]
    assert _ids(project, "Project.Measure.law") == ["measure/law"]
    assert _ids(project, "measure/law") == ["measure/law"]


def test_unpublished_text_is_not_searched(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(
        project,
        "convexity/quiet.md",
        title="Quiet",
        metadata=("declaration: theorem", "origin: background"),
        body=(
            "Shown text.\n\n<!-- commentword -->\n\n```lean\nfencedword\n```\n\n"
            "    indentedword\n\n<span hidden>hiddenword</span>\n\n### headingword"
        ),
    )

    assert _ids(project, "shown") == ["convexity/quiet"]
    for word in ("commentword", "fencedword", "indentedword", "hiddenword", "headingword", "background", "declaration"):
        assert _ids(project, word) == [], word


def test_matching_ignores_case_width_emphasis_and_line_wraps(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(
        project,
        "convexity/spelled.md",
        title="Spelled",
        body="A non-**ambiguous** label over ℝ is\nﬁnite, by Straße.",
        metadata=("declaration: def",),
    )

    for query in ("NON-AMBIGUOUS", "non-ambiguous label", "is finite", "over r", "Ｓpelled", "STRASSE"):
        assert _ids(project, query) == ["convexity/spelled"], query


def test_matching_ignores_accents_typographic_punctuation_and_invisible_characters(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(
        project,
        "convexity/names.md",
        title="Hahn–Banach",
        body="Hölder and Poincaré use Zorn’s lemma on semi­continuous non​negative maps.",
    )

    for query in ("hahn-banach", "holder", "poincare", "zorn's", "semicontinuous", "nonnegative", "HÖLDER"):
        assert _ids(project, query) == ["convexity/names"], query
    with pytest.raises(SearchError, match="no terms"):
        search_blueprint(project, "​ ­")


def test_punctuation_around_a_query_word_does_not_hide_an_article(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(
        project,
        "convexity/names.md",
        title="Names",
        body="The map x_1 is non-ambiguous, and Hahn\u2013Banach holds.",
        metadata=("declaration: theorem", "lean: Project.named"),
    )

    for query in ("Hahn\u2013Banach,", '"non-ambiguous"', "(non-ambiguous)", "\u201cnon-ambiguous\u201d.", "x_1,", "Project.named."):
        assert _ids(project, query) == ["convexity/names"], query
    # Punctuation alone is still searched as written.
    assert search_blueprint(project, "affine ...").terms == ("affine", "...")


def test_a_title_is_matched_as_the_page_shows_it(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/README.md", title="Convex *analysis* at caf&eacute;")
    _article(
        project,
        "convexity/map.md",
        title="Non-*ambiguous* map [linked](http://urlword.example)",
    )
    _article(project, "convexity/numbered.md", title="1. Introduction")

    assert _matched(project, "non-ambiguous") == {"convexity/map": ("title",)}
    assert _matched(project, "caf\u00e9")["convexity/map"] == ("ancestors",)
    assert _ids(project, "eacute") == _ids(project, "urlword") == []
    # A title the renderer would read as a list item or drop is matched as written.
    assert _ids(project, "1. introduction") == ["convexity/numbered"]


def test_filler_words_and_word_endings_do_not_hide_an_article(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/recovery.md", title="Recovery", body="It recovers the fully supervised solution.")

    for query in ("recovered solution", "recovering of the solutions", "the solution is recovered by it"):
        assert _ids(project, query) == ["convexity/recovery"], query
    assert search_blueprint(project, "The recovered solutions of bounded classes").terms == (
        "recover",
        "solution",
        "bound",
        "class",
    )
    # An ending is cut only from a plain word that stays four letters long.
    assert search_blueprint(project, "sets Nat.succ_pos ring_hom_ext less continuous basis").terms == (
        "sets",
        "nat.succ_pos",
        "ring_hom_ext",
        "less",
        "continuous",
        "basis",
    )
    # A query of nothing but filler words is searched as written.
    assert search_blueprint(project, "of the").terms == ("of", "the")


def test_query_words_are_cut_to_a_stem_their_other_forms_contain(tmp_path: Path) -> None:
    project = _project(tmp_path)

    def terms(query: str) -> tuple[str, ...]:
        return search_blueprint(project, query).terms

    assert terms("topologies properties satisfied") == ("topolog", "propert", "satisf")
    assert terms("embedding controlled splitting passing") == ("embed", "control", "split", "pass")
    assert terms("ideals compactness rings ring") == ("ideal", "compactness", "ring")
    assert terms("recovered, (solutions). f(x) y,") == ("recover", "solution", "f(x)", "y")
    assert terms("$of$ $x$") == ("x",)


def test_only_words_that_carry_no_meaning_are_dropped(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fillers = (
        "a an and are as at be by can do does for from has have if in is it its let of on or such that the then "
        "there these this to we when where which whose with"
    )

    assert search_blueprint(project, f"{fillers} not every all any some no exists x").terms == (
        "not",
        "every",
        "all",
        "any",
        "some",
        "no",
        "exist",
        "x",
    )


def test_in_one_field_the_word_as_typed_comes_before_one_found_through_its_stem(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/basis.md", title="Basis theorem", body="Polynomial rings over Noetherian rings.")
    _article(project, "convexity/string.md", title="A string diagram")
    _article(project, "convexity/ring.md", title="Ring")
    _article(project, "convexity/zrings.md", title="Zariski rings")

    # The field decides first: a title reached through the stem is above a
    # statement holding the word as typed.
    assert _ids(project, "rings") == ["convexity/zrings", "convexity/ring", "convexity/string", "convexity/basis"]


def test_a_full_lean_name_finds_the_article_that_owns_it_before_those_that_cite_it(tmp_path: Path) -> None:
    project = _project(tmp_path)
    for index in range(3):
        _article(project, f"convexity/cite{index}.md", title=f"Corollary {index}", body="By `Project.Convex.sep` it holds.")
    _article(project, "convexity/owner.md", title="Owner", metadata=("declaration: theorem", "lean: Project.Convex.sep"))

    for query in ("Project.Convex.sep", "Convex.sep", "_root_.Project.Convex.sep", "Project.Convex.\u00absep\u00bb"):
        assert _matched(project, query)["convexity/owner"] == ("lean",), query
        assert _ids(project, query)[0] == "convexity/owner", query
    # A namespace, or part of a component, names no declaration.
    assert _matched(project, "Project.Convex")["convexity/owner"] == ("qualified_names",)
    assert _matched(project, "onvex.sep")["convexity/owner"] == ("qualified_names",)


@pytest.mark.parametrize("name", ["Project.sep'", "Project.sep''", "Project.get?", "Project.get!"])
def test_a_full_lean_name_ending_in_a_prime_or_a_mark_finds_its_owner_first(tmp_path: Path, name: str) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/cite.md", title="Corollary", body=f"By `{name}` it holds.")
    _article(project, "convexity/owner.md", title="Owner", metadata=("declaration: theorem", f"lean: {name}"))

    assert _matched(project, name)["convexity/owner"] == ("lean",)
    assert _ids(project, name)[0] == "convexity/owner"


def test_a_full_mathlib_name_finds_the_article_that_owns_it(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(
        project,
        "convexity/hahn.md",
        title="Extension",
        metadata=(
            "declaration: theorem",
            "mathlib: true",
            "mathlib_declaration: Real.exists_extension_norm_eq",
            "mathlib_file: Mathlib/Analysis/HahnBanach.lean",
        ),
    )

    assert _matched(project, "Real.exists_extension_norm_eq")["convexity/hahn"] == ("lean",)


def test_a_lean_name_written_with_quoting_marks_is_found_by_the_name_without_them(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(
        project, "convexity/owner.md", title="Owner", metadata=("declaration: theorem", "lean: Project.Convex.\u00absep\u00bb")
    )

    assert _matched(project, "Project.Convex.sep")["convexity/owner"] == ("lean",)


def test_a_chapter_title_does_not_bury_the_article_titled_for_the_result(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "sep/README.md", title="Separating hyperplanes")
    for index in range(25):
        _article(project, f"sep/unrelated{index:02}.md", title=f"Unrelated {index}", body="Nothing relevant.")
    _article(project, "convexity/theorem.md", title="Hyperplane separation theorem")

    assert _ids(project, "hyperplanes")[:3] == ["sep", "convexity/hyperplane", "convexity/theorem"]


@pytest.mark.parametrize(
    ("query", "terms"),
    [
        ("`Nat.succ`", ("nat.succ",)),
        ("**weak**", ("weak",)),
        ("_weak_ *topologies*", ("weak", "topolog")),
        ("weak topology", ("weak", "topolog")),
        ("inequality", ("inequalit",)),
        ("boundary", ("boundar",)),
        ("(see f(x))", ("see", "f(x)")),
        ("(f(x))", ("f(x)",)),
        ("f(x),", ("f(x)",)),
        ("((weak))", ("weak",)),
        ("(**weak**)", ("weak",)),
        ("$L^2$,", ("l^2",)),
        ("($L^2$).", ("l^2",)),
        ("cones", ("cone",)),
        ("faces edges", ("face", "edge")),
    ],
)
def test_markup_brackets_and_number_around_a_query_word_do_not_hide_an_article(
    tmp_path: Path, query: str, terms: tuple[str, ...]
) -> None:
    project = _project(tmp_path)
    _article(
        project,
        "convexity/weak.md",
        title="Weak topologies",
        body="See the family of inequalities on boundaries, which uses `Nat.succ` and f(x).\n\n"
        "A face of the cone is an edge in $L^2$.",
    )

    result = search_blueprint(project, query)

    assert result.terms == terms
    assert [hit.node_id for hit in result.hits] == ["convexity/weak"]


def test_a_term_keeps_what_is_not_wrapping_it(tmp_path: Path) -> None:
    project = _project(tmp_path)

    def terms(query: str) -> tuple[str, ...]:
        return search_blueprint(project, query).terms

    # A star, an underscore or a bracket inside or on one side belongs to the name.
    assert terms("C*-algebra C* foo_ _private (a)(b) f(x") == ("c*-algebra", "c*", "foo_", "_private", "(a)(b)", "f(x")
    # A relation is not sentence punctuation.
    assert terms("x != y := z") == ("x", "!=", "y", ":=", "z")
    # A short word, or one ending in a vowel and y, keeps its y.
    assert terms("many every display family") == ("many", "every", "display", "famil")
    # A plural of three letters is kept, and so is a square bracket.
    assert terms("sets axes [weak] [[link]]") == ("sets", "axes", "[weak]", "[[link]]")
    # A prime is read as a closing quote.
    assert terms("foo'") == ("foo",)


def test_folding_keeps_a_negated_relation_apart_from_the_relation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/equal.md", title="Equal", body="Here $x = y$ and a \u2208 B.")
    _article(project, "convexity/unequal.md", title="Unequal", body="Here $x \u2260 y$ and a \u2209 B.")

    assert _ids(project, "\u2260") == ["convexity/unequal"]
    assert _ids(project, "\u2209") == ["convexity/unequal"]
    assert _ids(project, "\u2208") == ["convexity/equal"]


def test_a_formula_pasted_with_its_dollar_signs_matches_on_its_content(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/formula.md", title="Formula", body="Let $S : Y \\to \\mathrm{Prop}$ be given.")

    assert _ids(project, "$\\mathrm{Prop}$") == ["convexity/formula"]
    assert search_blueprint(project, "$S : Y$ $$").terms == ("s", ":", "y")


def test_hits_sort_by_best_field_then_worst_placed_term_then_dependents_then_id(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _article(project, "README.md", title="Book")
    _article(project, "c/README.md", title="Chapter")
    _article(project, "c/both.md", title="Compact operator")
    _article(project, "c/a-split.md", title="Operator", body="A compact map.")
    _article(project, "c/used.md", title="Spectrum", body="Of a compact operator.")
    _article(project, "c/alone.md", title="Resolvent", body="Of a compact operator.")
    _article(project, "c/again.md", title="Adjoint", body="Of a compact operator.")
    _article(project, "c/uses.md", title="Consumer", body="Uses it.", depends=("used.md",))

    assert _ids(project, "compact operator") == ["c/both", "c/a-split", "c/used", "c/again", "c/alone"]


def test_fields_rank_in_the_documented_order(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _article(project, "README.md", title="Book")
    _article(project, "zeta/README.md", title="Holds the marker word")
    _article(project, "zeta/f-ancestor.md", title="Under")
    _article(project, "zeta/deep/README.md", title="Deep")
    _article(project, "zeta/deep/h-nested.md", title="Nested")
    _article(project, "zeta/d-statement.md", title="Statement", body="The marker.")
    _article(project, "zeta/marker.md", title="Path")
    _article(project, "zeta/b-lean.md", title="Lean", metadata=("declaration: def", "lean: Project.marker"))
    _article(
        project,
        "zeta/c-mathlib.md",
        title="Library",
        metadata=("declaration: theorem", "mathlib: true", "mathlib_declaration: Mathlib.marker"),
    )
    _article(project, "zeta/a-title.md", title="Marker")
    _article(project, "zeta/g-full.md", title="Full", metadata=("declaration: def", "lean: Marker.other"))

    assert _ids(project, "marker") == [
        "zeta",
        "zeta/a-title",
        "zeta/b-lean",
        "zeta/c-mathlib",
        "zeta/marker",
        "zeta/d-statement",
        "zeta/deep",
        "zeta/deep/h-nested",
        "zeta/f-ancestor",
        "zeta/g-full",
    ]
    # The roadmap's own title is above every article, so it singles none out.
    assert _ids(project, "book") == ["roadmap"]


def test_a_statement_match_ranks_above_a_containing_title(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _article(project, "README.md", title="Book")
    _article(project, "zeta/README.md", title="Holds the marker word")
    _article(project, "zeta/a-under.md", title="Under")
    _article(project, "zeta/z-statement.md", title="Statement", body="The marker.")

    assert _ids(project, "marker") == ["zeta", "zeta/z-statement", "zeta/a-under"]


def test_a_chapter_title_is_searched_when_the_roadmap_has_no_root_article(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _article(project, "ch/README.md", title="Quagga chapter")
    _article(project, "ch/leaf.md", title="Leaf")
    _article(project, "ch/sub/README.md", title="Okapi section")
    _article(project, "ch/sub/deep.md", title="Deep")

    assert _ids(project, "quagga") == ["ch", "ch/leaf", "ch/sub", "ch/sub/deep"]
    assert _ids(project, "okapi") == ["ch/sub", "ch/sub/deep"]


def test_a_hit_keeps_the_authored_statement_and_previews_the_published_one(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(
        project,
        "convexity/gauge.md",
        title="Gauge",
        body="A **sublinear** map $p$.",
        metadata=("declaration: definition",),
    )

    hit = search_blueprint(project, "sublinear").hits[0]

    assert hit.statement_text == "A **sublinear** map $p$."
    assert statement_preview(hit) == "A sublinear map \\(p\\)."
    assert search_blueprint(project, "convex", states=["proved", "can_prove", "proved"]).states == (
        "can_prove",
        "proved",
    )


def test_filters_and_limit_narrow_the_hits_but_not_the_count(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/hull.md", title="Convex hull", metadata=("declaration: noncomputable DEF",))

    assert _ids(project, "convex") == [
        "convexity",
        "convexity/hull",
        "convexity/hyperplane",
        "convexity/support",
    ]
    assert _ids(project, "convex", declarations=["Theorem"]) == ["convexity/hyperplane"]
    assert _ids(project, "convex", declarations=["theorem", "lemma"]) == [
        "convexity/hyperplane",
        "convexity/support",
    ]
    assert _ids(project, "convex", declarations=["def"]) == ["convexity/hull"]
    assert _ids(project, "convex", states=["can_prove"]) == ["convexity/hyperplane"]
    limited = search_blueprint(project, "convex", limit=1)
    assert [hit.node_id for hit in limited.hits] == ["convexity"]
    assert limited.total_matches == 4
    with pytest.raises(SearchError, match="positive integer"):
        search_blueprint(project, "convex", limit=0)
    with pytest.raises(SearchError, match="declaration kind is empty"):
        search_blueprint(project, "convex", declarations=[" "])


def test_a_declaration_kind_no_article_uses_is_refused_with_the_kinds_in_use(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/hull.md", title="Convex hull", metadata=("declaration: noncomputable DEF",))

    # An empty answer would read as "this result is new" when the kind was only misspelled.
    with pytest.raises(SearchError) as refused:
        search_blueprint(project, "convex", declarations=["theorem", "Definition", "thm"])

    assert str(refused.value) == (
        "no article declares kind definition, thm; this blueprint uses: lemma, noncomputable def, theorem"
    )
    # A kind in use that the query does not reach is an ordinary empty result.
    assert _ids(project, "nowhere", declarations=["def"]) == []


def test_a_hit_names_at_most_ten_dependents_and_counts_them_all(tmp_path: Path) -> None:
    project = _project(tmp_path)
    for index in range(12):
        _article(project, f"convexity/use{index:02d}.md", title=f"Use {index}", depends=("hyperplane.md",))

    hit = search_blueprint(project, "separating").hits[0]

    assert hit.used_by_count == 13
    assert hit.used_by == ("convexity/support", *(f"convexity/use{index:02d}" for index in range(9)))


def test_a_title_shared_with_another_article_is_marked(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/again.md", title="separating  HYPERPLANE")

    assert [hit.shared_title for hit in search_blueprint(project, "separating").hits] == [True, True]
    assert [hit.shared_title for hit in search_blueprint(project, "supporting").hits] == [False]


def test_a_project_and_its_blueprint_directory_give_the_same_result(tmp_path: Path) -> None:
    project = _project(tmp_path)

    assert search_blueprint(project / "blueprint", "convex").to_json() == search_blueprint(project, "convex").to_json()


def test_a_symlinked_roadmap_entry_is_refused(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("---\n---\n\n# Outside\n\nconvex\n", encoding="utf-8")
    (project / "blueprint/roadmap/convexity/linked.md").symlink_to(outside)

    with pytest.raises(RuntimeProjectionError):
        search_blueprint(project, "convex")
    assert cli.main(["search", str(project), "convex"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "symbolic link" in captured.err


def test_an_article_edited_while_the_blueprint_is_searched_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    build = search_module.build_runtime_graph

    def edit_after_building(*args: object, **kwargs: object):
        runtime = build(*args, **kwargs)
        (project / "blueprint/roadmap/convexity/support.md").write_text("# Rewritten\n", encoding="utf-8")
        return runtime

    monkeypatch.setattr(search_module, "build_runtime_graph", edit_after_building)

    with pytest.raises(SearchError, match="convexity/support: the article changed while"):
        search_blueprint(project, "convex")


def test_a_query_that_is_not_text_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)

    for query in ("", "   ", "$"):
        with pytest.raises(SearchError, match="no terms"):
            search_blueprint(project, query)
    with pytest.raises(SearchError, match="not valid Unicode"):
        search_blueprint(project, "convex\udcff")


def test_search_writes_nothing_and_starts_no_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    (project / "Project.lean").write_text(_LEAN_SOURCE, encoding="utf-8")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("search must not start a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    before = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    assert search_blueprint(project, "convex", lean_root=project).total_matches == 3

    assert {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()} == before


def test_search_adds_one_read_and_one_render_per_article(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The graph and the runtime projection read each article as well; this
    # counts what search adds to them.
    project = _project(tmp_path)
    reads: list[str] = []
    renders: list[str] = []
    read, prose = search_module._article, search_module.visible_prose
    monkeypatch.setattr(search_module, "_article", lambda path, node: reads.append(node.id) or read(path, node))
    monkeypatch.setattr(search_module, "visible_prose", lambda value: renders.append(value) or prose(value))

    assert search_blueprint(project, "convex", limit=1).total_matches == 3

    assert sorted(reads) == sorted(node.id for node in load_runtime_graph(project).nodes)
    assert len(renders) == len(reads)


def test_a_statement_the_renderer_gives_up_on_is_still_found_by_its_title(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/deep.md", title="Deeply nested", body="- " * 500 + "x")

    assert _ids(project, "nested") == ["convexity/deep"]
    assert _ids(project, "affine") == ["convexity/hyperplane"]


def test_the_bundled_example_answers_a_reuse_question(repo_root: Path) -> None:
    project = repo_root / "skills/setup/assets/cabannes-thesis-project"

    hit = search_blueprint(project, "NonAmbiguous at most one eligible", lean_root=project).hits[0]

    assert hit.node_id == "infimum-loss/definitions/non-ambiguity"
    assert hit.state == "fully_proved"
    assert [target.as_dict() for target in hit.lean_targets] == [
        {"declaration": "CabannesThesis.NonAmbiguous", "line": 11, "source_file": "src/CabannesThesis/Basic.lean"}
    ]
    assert hit.used_by_count == 2


def test_search_cli_emits_stable_json(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    command = ["search", str(project), "Separating  AFFINE", "--json", "--state", "can_prove", "--limit", "5"]

    assert cli.main(command) == 0
    first = capsys.readouterr().out
    assert cli.main(command) == 0
    assert capsys.readouterr().out == first

    assert str(tmp_path) not in first
    document = json.loads(first)
    assert first == json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    hit = document.pop("hits")[0]
    assert document == {
        "filters": {"declaration": [], "state": ["can_prove"]},
        "limit": 5,
        "open_statements": False,
        "query": "Separating  AFFINE",
        "schema": SEARCH_SCHEMA,
        "source_revision": load_runtime_graph(project).source_revision,
        "terms": ["separat", "affine"],
        "total_matches": 1,
    }
    assert len(hit.pop("article_revision")) == 64
    assert hit == {
        "article_id": None,
        "article_path": "blueprint/roadmap/convexity/hyperplane.md",
        "declaration": "theorem",
        "lean_targets": [{"declaration": "Project.separation", "line": None, "source_file": None}],
        "matched_fields": ["title", "statement_text"],
        "mathlib": False,
        "mathlib_declarations": [],
        "mathlib_file": None,
        "node_id": "convexity/hyperplane",
        "shared_title": False,
        "source_targets": [],
        "state": "can_prove",
        "statement_text": "Two disjoint convex sets are separated by an affine functional.",
        "title": "Separating hyperplane",
        "used_by": ["convexity/support"],
        "used_by_count": 1,
    }


def test_search_cli_reports_identity_mathlib_sources_and_dependents(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    _article(project, "README.md", title="Book", metadata=("open_statements: allowed",))
    _article(
        project,
        "convexity/hahn.md",
        title="Extension",
        body="Extends a functional.\n\n## Sources\n\n- [paper](https://example.invalid/paper)",
        metadata=(
            "article_id: af_000000000000000000000001",
            "declaration: theorem",
            "mathlib: true",
            "mathlib_declaration: exists_extension_norm_eq",
            "mathlib_file: Mathlib/Analysis/HahnBanach.lean",
        ),
    )
    _article(project, "convexity/twin.md", title="Extension")
    _article(
        project,
        "convexity/user.md",
        title="Consumer",
        body="Uses it.\n\n## Proof depends on\n\n- [dependency](hahn.md)",
    )

    assert cli.main(["search", str(project), "exists_extension_norm_eq", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    hit = document["hits"][0]
    assert document["open_statements"] is True
    assert hit["article_id"] == "af_000000000000000000000001"
    assert (hit["mathlib"], hit["mathlib_declarations"], hit["mathlib_file"]) == (
        True,
        ["exists_extension_norm_eq"],
        "Mathlib/Analysis/HahnBanach.lean",
    )
    assert hit["source_targets"] == ["https://example.invalid/paper"]
    assert (hit["used_by"], hit["used_by_count"], hit["shared_title"]) == (["convexity/user"], 1, True)
    assert hit["article_revision"] != document["source_revision"]

    assert cli.main(["search", str(project), "exists_extension_norm_eq"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "Extension (convexity/hahn) [af_000000000000000000000001]" in lines
    assert "  theorem, mathlib, used by 1, title shared with another article" in lines
    assert "  Mathlib: exists_extension_norm_eq" in lines


def test_search_cli_human_output_escapes_project_text(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    (project / "Project.lean").write_text(_LEAN_SOURCE, encoding="utf-8")
    path = project / "blueprint/roadmap/convexity/hyperplane.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("# Separating hyperplane", "# Separating hyperplane\x1b[2J").replace("affine", "affine\x07")
    path.write_text(text.replace("declaration: theorem", "declaration: theo\x1brem"), encoding="utf-8")

    assert cli.main(["search", str(project), "separating", "--lean-root", str(project)]) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output and "\x07" not in output
    lines = output.splitlines()
    assert "Separating hyperplane\\x1b[2J (convexity/hyperplane)" in lines
    assert "  theo\\x1brem, can_prove, used by 1" in lines
    assert "  Lean: Project.separation (Project.lean:3)" in lines
    assert "  Statement: Two disjoint convex sets are separated by an affine\\x07 functional." in lines
    assert "  Matched: title" in lines
    assert lines[-1] == "1 of 1 matching article(s) shown."

    _article(
        project,
        "convexity/forged.md",
        title="Forged",
        metadata=("lean: Project.forged\x1b[2J", "mathlib: true", "mathlib_declaration: Mathlib.forged\x07"),
    )
    assert cli.main(["search", str(project), "forged"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "  Lean: Project.forged\\x1b[2J" in lines
    assert "  Mathlib: Mathlib.forged\\x07" in lines

    assert cli.main(["search", str(project), "nowhere"]) == 0
    assert capsys.readouterr().out == "No matching articles.\n"
    assert cli.main(["search", str(project), "nowhere", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["hits"] == []


def test_search_cli_cuts_a_long_statement_and_counts_what_the_limit_hides(tmp_path: Path, capsys) -> None:
    project = _project(tmp_path)
    _article(project, "convexity/long.md", title="Longwinded", body="convex " * 100 + "\n\nSecond paragraph.")

    assert cli.main(["search", str(project), "convex", "--declaration", "lemma", "--limit", "1"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "Supporting functional (convexity/support)"

    assert cli.main(["search", str(project), "convex", "--limit", "1"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "1 of 4 matching article(s) shown."

    assert cli.main(["search", str(project), "longwinded"]) == 0
    statement = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("  Statement: "))
    assert len(statement) <= len("  Statement: ") + 200
    assert statement.endswith("...")


def test_search_cli_reports_errors_on_stderr_with_exit_2(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)

    assert cli.main(["search", str(project), "  "]) == 2
    assert capsys.readouterr().err == "error: search query has no terms\n"

    assert cli.main(["search", str(project), "convex", "--lean-root", str(tmp_path / "missing")]) == 2
    assert capsys.readouterr().err == "error: Lean root does not exist or is not a directory\n"

    assert cli.main(["search", str(tmp_path / "missing"), "convex"]) == 2
    assert capsys.readouterr().err == "error: project or blueprint directory does not exist\n"

    assert cli.main(["search", str(project), "convex", "--limit", "0"]) == 2
    assert capsys.readouterr().err == "error: search limit must be a positive integer\n"

    for arguments in (["--state", "finished"], ["--limit", "many"]):
        with pytest.raises(SystemExit) as refused:
            cli.main(["search", str(project), "convex", *arguments])
        assert refused.value.code == 2
    capsys.readouterr()

    _article(project, "convexity/bad\x1b.md", title="Bad", metadata=("article_id: not-an-id",))
    assert cli.main(["search", str(project), "convex"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "\x1b" not in captured.err and "malformed article_id" in captured.err
    (project / "blueprint/roadmap/convexity/bad\x1b.md").unlink()

    def unreadable(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied", "/private/secret/blueprint")

    monkeypatch.setattr(cli, "search_blueprint", unreadable)
    assert cli.main(["search", str(project), "convex"]) == 2
    error = capsys.readouterr().err
    assert error.startswith("error: ") and "/private/secret" not in error


def test_search_cli_does_not_report_output_errors_as_unreadable_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)

    class ClosedPipe(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    with pytest.raises(BrokenPipeError):
        cli.main(["search", str(project), "convex"])
