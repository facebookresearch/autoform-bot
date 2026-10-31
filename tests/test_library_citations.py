from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.library.citations import cited_sources
from tests.library_fixture import declaration, make_library
from tests.search_fixture import _article

_THEOREM = "Nat.threeSquareRepresentationCount_eq_threeSquareRepresentationCount_div_four"
_MORTENSON = "DOI 10.1007/s00222-012-0383-5"
_GAUSS = "- [Gauss](https://arxiv.org/abs/2307.05244v2)"
_UNREADABLE = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="mode 0 still reads"
)


@pytest.mark.parametrize(
    ("text", "source"),
    [
        ("Source: arXiv:2307.05244 `main.tex` lines 191-199", "arXiv 2307.05244"),
        ("arXiv 2307.05244v3", "arXiv 2307.05244"),
        ("arXiv `2311.13047v1`", "arXiv 2311.13047"),
        ("arXiv:1407.1234v2", "arXiv 1407.1234"),
        ("_arXiv:2307.05244_", "arXiv 2307.05244"),
        ("| arXiv | 2307.05244 |", "arXiv 2307.05244"),
        ("https://arxiv.org/abs/2307.05244v2", "arXiv 2307.05244"),
        ("https://arxiv.org/abs/2307.05244/", "arXiv 2307.05244"),
        ("http://arxiv.org/pdf/2307.05244v1.pdf", "arXiv 2307.05244"),
        ("https://export.arxiv.org/e-print/2209.11629", "arXiv 2209.11629"),
        ("https://arxiv.org/html/2307.05244v1", "arXiv 2307.05244"),
        ("https://arxiv.org/src/2307.05244", "arXiv 2307.05244"),
        ("https://doi.org/10.48550/arXiv.2307.05244", "arXiv 2307.05244"),
        ("doi:10.48550/arXiv.2307.05244v1", "arXiv 2307.05244"),
        ("arXiv:math.AG/0210149v2", "arXiv math/0210149"),
        ("https://arxiv.org/abs/math/0210149", "arXiv math/0210149"),
        ("doi:10.1007/S00222-012-0383-5.", _MORTENSON),
        ("DOI: 10.1007/s00222-012-0383-5", _MORTENSON),
        ("**DOI:** 10.1007/s00222-012-0383-5", _MORTENSON),
        ("| DOI | 10.1007/s00222-012-0383-5 |", _MORTENSON),
        ("*doi:10.1007/s00222-012-0383-5*", _MORTENSON),
        ("https://doi.org/10.1007/s00222-012-0383-5?via=ihub", _MORTENSON),
        ("(see https://dx.doi.org/10.1007%2Fs00222-012-0383-5)", _MORTENSON),
        ("[doi:10.1007/s00222-012-0383-5](https://doi.org/10.1007/s00222-012-0383-5).", _MORTENSON),
        ("[paper](https://doi.org/10.1016/0001-8708(77)90004-3)", "DOI 10.1016/0001-8708(77)90004-3"),
        ("https://doi.org/10.1007/s00222-012-0383-5#citeas", _MORTENSON),
        ("DOI `10.1007/s00222-012-0383-5`", _MORTENSON),
        ("_DOI:_ 10.1007/s00222-012-0383-5", _MORTENSON),
        ("_DOI: 10.1007/s00222-012-0383-5_", _MORTENSON),
        ("|DOI|10.1007/s00222-012-0383-5|", _MORTENSON),
        ("**arXiv:** 2307.05244", "arXiv 2307.05244"),
        ("*arXiv:* 2307.05244", "arXiv 2307.05244"),
    ],
)
def test_a_paper_is_one_source_in_each_of_its_usual_spellings(text: str, source: str) -> None:
    assert cited_sources(text) == {source}


@pytest.mark.parametrize(
    "text", ["2307.05244", "an arXiv preprint", "arXiv:2307.052449", "10.1007/s00222-012-0383-5", "xarXiv:2307.05244"]
)
def test_a_number_without_its_arxiv_or_doi_label_cites_nothing(text: str) -> None:
    assert cited_sources(text) == frozenset()


def _consumer(
    tmp_path: Path,
    *,
    sources: str = _GAUSS,
    docstring: str | None = "Source: arXiv:2307.05244 `main.tex` lines 191-199.",
    module_doc: str | None = None,
    articles: int = 1,
    status: str = "complete",
    proved: bool = False,
    stated: bool = False,
    declarations: tuple | None = None,
) -> Path:
    # The blueprint and the Lake project share one root, as in a real consumer.
    library = make_library(
        tmp_path,
        declarations=declarations or (declaration(_THEOREM, status=status, docstring=docstring),),
        module_docs={"Lib.Convex": module_doc} if module_doc else None,
    )
    _article(library.project, "README.md", title="Three squares")
    for number in range(articles):
        lean = ("declaration: theorem", "statement: formalized", f"lean: Project.step{number}")
        _article(
            library.project,
            f"step-{number}.md",
            title=f"Step {number}",
            body=f"A step of the proof.\n\n## Sources\n\n{sources}",
            metadata=(
                f"article_id: af_{number:024x}",
                *(lean if proved or stated else ()),
                *(("proof: formalized",) if proved else ()),
            ),
        )
        if proved or stated:
            with (library.project / "Project.lean").open("a", encoding="utf-8") as file:
                file.write(f"theorem Project.step{number} : True := trivial\n")
    return library.project


def _check(project: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    status = main(["check", str(project / "blueprint"), "--lean-root", str(project)])
    return status, [line for line in capsys.readouterr().out.splitlines() if line.startswith("warning:")]


def _warning(
    name: str = _THEOREM, source: str = "arXiv 2307.05244", reach: str = "unproved article step-0 reaches"
) -> str:
    if not reach.startswith("unproved article"):
        reach += ", including step-0"
    return (
        f"warning: library atlas: {name}'s docstring cites {source}, which {reach}; no implementation note, source "
        "page or project Lean file names it, so reuse it or record why it does not apply"
    )


def _stop(issue: str) -> str:
    """Name what check reports because render refuses to publish it."""
    return f"warning: {issue}; autoform render stops until this is fixed"


def _note(project: Path, text: str, number: int = 0) -> None:
    notes = project / "blueprint/.implementation-notes"
    notes.mkdir(exist_ok=True)
    (notes / f"af_{number:024x}.md").write_text(text, encoding="utf-8")


def test_check_warns_about_a_library_result_citing_the_paper_until_a_note_or_lean_file_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path)
    assert _check(project, capsys) == (0, [_warning()])

    _note(project, f"Reuse `{_THEOREM}` from ATLAS.\n")
    assert _check(project, capsys) == (0, [])

    (project / "blueprint/.implementation-notes" / f"af_{0:024x}.md").unlink()
    # After `open Nat` the project names it by its last component alone.
    (project / "Project.lean").write_text(
        "open Nat\n\nexample := threeSquareRepresentationCount_eq_threeSquareRepresentationCount_div_four\n",
        encoding="utf-8",
    )
    assert _check(project, capsys) == (0, [])


@pytest.mark.parametrize("text", ["Reuse {name}'s proof.", "Does {name}? apply", "Reuse {name}!"])
def test_a_note_names_a_declaration_in_prose(tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str) -> None:
    project = _consumer(tmp_path)
    _note(project, text.format(name=_THEOREM) + "\n")

    assert _check(project, capsys) == (0, [])


def test_a_note_must_write_the_full_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _consumer(tmp_path)
    _note(project, "Reuse threeSquareRepresentationCount_eq_threeSquareRepresentationCount_div_four.\n")

    assert _check(project, capsys) == (0, [_warning()])


def test_a_note_naming_a_longer_declaration_does_not_name_this_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path)
    _note(project, f"Reuse `{_THEOREM}_aux`, then finish.\n")

    assert _check(project, capsys) == (0, [_warning()])


@pytest.mark.parametrize("text", ["Reuse _{name}_.", "Reuse __{name}__.", "Does '{name}' apply?"])
def test_a_note_names_a_declaration_in_emphasis_or_quotes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    project = _consumer(tmp_path)
    _note(project, text.format(name=_THEOREM) + "\n")

    assert _check(project, capsys) == (0, [])


def test_a_note_naming_a_primed_declaration_does_not_name_this_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path)
    _note(project, f"Reuse `{_THEOREM}'`.\n")

    assert _check(project, capsys) == (0, [_warning()])


@pytest.mark.parametrize(
    "lean",
    [
        "open Nat in\nexample := «threeSquareRepresentationCount_eq_threeSquareRepresentationCount_div_four»",
        f"example := Nat.«{_THEOREM.removeprefix('Nat.')}»",
    ],
)
def test_a_lean_file_names_a_declaration_with_a_quoted_component(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lean: str
) -> None:
    project = _consumer(tmp_path)
    (project / "Project.lean").write_text(lean + "\n", encoding="utf-8")

    assert _check(project, capsys) == (0, [])


def test_a_lean_file_ends_a_name_where_lean_does(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Lean reads `Fooˣ` as `Foo` followed by the units notation, so the file names Lib.Convex.Foo.
    project = _consumer(tmp_path, declarations=_cited("Lib.Convex.Foo"))
    (project / "Project.lean").write_text("open Lib.Convex\n\nexample (u : Fooˣ) : True := trivial\n", encoding="utf-8")

    assert _check(project, capsys) == (0, [])


def test_a_source_page_records_a_declaration_that_does_not_apply_for_every_article(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, sources="- [Gauss, Theorem 2](../sources/gauss.md#theorem-2)", articles=2)
    page = project / "blueprint/sources/gauss.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Gauss\n\narXiv:2307.05244.\n\n## Theorem 2\n\nLines 191-199.\n", encoding="utf-8")
    assert _check(project, capsys) == (0, [_warning(reach="2 unproved articles reach")])

    # Unlike an implementation note, the page stays once an article is proved.
    page.write_text(page.read_text(encoding="utf-8") + f"\n`{_THEOREM}` states r(n) only for n > 0.\n")
    assert _check(project, capsys) == (0, [])


def test_a_record_on_a_source_page_outlives_the_proof_of_the_article_linking_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, articles=2)
    # step-0 is proved and links the page; step-1, still open, cites the paper by URL.
    _article(
        project,
        "step-0.md",
        title="Step 0",
        body="A step of the proof.\n\n## Sources\n\n- [Gauss, Theorem 2](../sources/gauss.md#theorem-2)",
        metadata=(
            f"article_id: af_{0:024x}",
            "declaration: theorem",
            "statement: formalized",
            "proof: formalized",
            "lean: Project.step0",
        ),
    )
    (project / "Project.lean").write_text("theorem Project.step0 : True := trivial\n", encoding="utf-8")
    page = project / "blueprint/sources/gauss.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Gauss\n\narXiv:2307.05244.\n", encoding="utf-8")
    assert _check(project, capsys) == (0, [_warning(reach="unproved article step-1 reaches")])

    page.write_text(page.read_text(encoding="utf-8") + f"\n`{_THEOREM}` states r(n) only for n > 0.\n")
    assert _check(project, capsys) == (0, [])


def test_a_page_outside_the_sources_folder_records_a_declaration_once_a_proved_article_links_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, articles=2)
    _article(
        project,
        "step-0.md",
        title="Step 0",
        body="A step of the proof.\n\n## Sources\n\n- [Reading notes](../notes/gauss.md)",
        metadata=(
            f"article_id: af_{0:024x}",
            "declaration: theorem",
            "statement: formalized",
            "proof: formalized",
            "lean: Project.step0",
        ),
    )
    (project / "Project.lean").write_text("theorem Project.step0 : True := trivial\n", encoding="utf-8")
    notes = project / "blueprint/notes/gauss.md"
    notes.parent.mkdir(parents=True)
    notes.write_text(f"`{_THEOREM}` states r(n) only for n > 0.\n", encoding="utf-8")

    assert _check(project, capsys) == (0, [])


def test_a_source_page_no_article_links_still_records_a_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path)  # the article cites the paper only by URL
    sources = project / "blueprint/sources"
    (sources / "gauss").mkdir(parents=True)
    # Hidden pages and folders, links and other files are not records.
    record = f"`{_THEOREM}` does not apply.\n"
    (sources / ".draft.md").write_text(record, encoding="utf-8")
    (sources / ".drafts").mkdir()
    (sources / ".drafts/gauss.md").write_text(record, encoding="utf-8")
    (sources / "gauss/notes.txt").write_text(record, encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "gauss.md").write_text(record, encoding="utf-8")
    links: list[str] = []
    try:
        (sources / "linked.md").symlink_to(elsewhere / "gauss.md")
        links.append("linked.md")
        (sources / "linked").symlink_to(elsewhere, target_is_directory=True)
        links.append("linked")
    except OSError:
        pass  # without symlinks, the other cases still run
    stops = [_stop(f"refusing symlink in blueprint publication: sources/{link}") for link in sorted(links)]
    assert _check(project, capsys) == (0, [_warning(), *stops])

    (sources / "gauss/theorem-2.md").write_text(f"`{_THEOREM}` states r(n) only for n > 0.\n", encoding="utf-8")
    assert _check(project, capsys) == (0, stops)


def test_a_sources_folder_that_is_a_link_is_not_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _consumer(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "gauss.md").write_text(f"`{_THEOREM}` does not apply.\n", encoding="utf-8")
    try:
        (project / "blueprint/sources").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert _check(project, capsys) == (0, [_warning(), _stop("refusing symlink in blueprint publication: sources")])


@_UNREADABLE
@pytest.mark.parametrize("beside", [True, False], ids=["blueprint-beside-the-lean-root", "blueprint-inside-it"])
def test_a_sources_folder_that_cannot_be_searched_never_raises(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], beside: bool
) -> None:
    project = _consumer(tmp_path)
    blueprint = (project / "blueprint").rename(tmp_path / "blueprint") if beside else project / "blueprint"
    folder = blueprint / "sources/gauss"
    folder.mkdir(parents=True)
    (folder / "theorem-2.md").write_text(f"`{_THEOREM}` does not apply.\n", encoding="utf-8")
    folder.chmod(0o644)  # listable, not searchable
    try:
        status = main(["check", str(blueprint), "--lean-root", str(project)])
    finally:
        folder.chmod(0o755)

    out = capsys.readouterr().out.splitlines()
    if beside:  # the folder hides its page, so the declaration is still reported
        assert (status, [line for line in out if line.startswith("warning:")]) == (
            0,
            [_warning(), _stop("blueprint could not be captured: permission denied: sources/gauss")],
        )
    else:  # check's own index build walks the blueprint too, and names the folder
        assert status == 1 and out[-1].startswith("error: Lean sources could not be indexed: permission denied")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_among_the_source_pages_is_not_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _consumer(tmp_path)
    sources = project / "blueprint/sources"
    sources.mkdir(parents=True)
    os.mkfifo(sources / "scratch.md")

    assert _check(project, capsys) == (
        0,
        [
            _warning(),
            _stop(
                "refusing non-regular blueprint publication input: sources/scratch.md: "
                "named pipe is not a regular file or directory"
            ),
        ],
    )


def test_only_a_linked_markdown_page_adds_to_what_an_article_cites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, sources="- [References](../sources/refs.bib)")
    bib = project / "blueprint/sources/refs.bib"
    bib.parent.mkdir(parents=True)
    bib.write_text("@article{gauss, url = {https://arxiv.org/abs/2307.05244}}\n", encoding="utf-8")

    assert _check(project, capsys) == (0, [])


def test_a_source_page_an_article_links_counts_with_what_it_cites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(
        tmp_path,
        sources="- [Mortenson, Theorem 2](../sources/mortenson.md#theorem-2)",
        docstring="After https://doi.org/10.1007/s00222-012-0383-5.",
    )
    page = project / "blueprint/sources/mortenson.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "# Mortenson\n\nPublished as [doi:10.1007/s00222-012-0383-5](https://doi.org/10.1007/s00222-012-0383-5).\n\n"
        "## Theorem 2\n\nLines 127-131.\n",
        encoding="utf-8",
    )

    assert _check(project, capsys) == (0, [_warning(source=_MORTENSON)])


_HELPERS = tuple(declaration(f"Lib.Convex.helper{letter}", docstring="A helper.") for letter in "ABCD")
_MODULE_WARNING = (
    "warning: library atlas: module Lib.Convex's docstring cites arXiv 2307.05244, which unproved article step-0 "
    "reaches; 4 of its declarations cite it only there, such as Lib.Convex.helperA, "
    "Lib.Convex.helperB and Lib.Convex.helperC, and no implementation note, source page or project Lean file "
    "names the module or one of its declarations citing it, so reuse them or record why they do not apply"
)


def _module_consumer(tmp_path: Path, declarations: tuple = _HELPERS) -> Path:
    return _consumer(tmp_path, declarations=declarations, module_doc="Gauss's formula, after arXiv:2307.05244.")


def test_a_module_docstring_citing_the_paper_warns_once_for_the_module(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _module_consumer(tmp_path)

    assert _check(project, capsys) == (0, [_MODULE_WARNING])

    (project / "Project.lean").write_text("import Lib.Convex\n", encoding="utf-8")
    assert _check(project, capsys) == (0, [])


@pytest.mark.parametrize(
    ("place", "text"),
    [
        ("note", "Reuse `Lib.Convex.helperB` for the bound."),
        ("note", "Nothing in `Lib.Convex` applies: it proves the bound only for odd n."),
        ("page", "`Lib.Convex` proves the bound only for odd n."),
        ("lean", "example := Lib.Convex.helperC"),
        ("lean", "example := Lib.Convex.«helperC»"),
        ("lean", "example := Lib.Convex.helperA.symm"),
    ],
)
def test_naming_the_module_or_one_of_its_declarations_in_full_silences_its_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], place: str, text: str
) -> None:
    project = _module_consumer(tmp_path)
    if place == "note":
        _note(project, text + "\n")
    elif place == "page":
        page = project / "blueprint/sources/gauss.md"
        page.parent.mkdir(parents=True)
        page.write_text(f"# Gauss\n\n{text}\n", encoding="utf-8")
    else:
        (project / "Project.lean").write_text(text + "\n", encoding="utf-8")

    assert _check(project, capsys) == (0, [])


def test_naming_a_declaration_whose_own_docstring_cites_silences_its_module_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main_result = declaration("Lib.Convex.mainTheorem", docstring="Theorem 2 of arXiv:2307.05244.")
    project = _module_consumer(tmp_path, declarations=(main_result, *_HELPERS))
    _note(project, "Reuse `Lib.Convex.mainTheorem`.\n")

    assert _check(project, capsys) == (0, [])


def test_naming_a_declaration_that_cites_another_source_leaves_its_module_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = declaration("Lib.Convex.other", docstring="After doi:10.1007/s00222-012-0383-5.")
    project = _module_consumer(tmp_path, declarations=(other, *_HELPERS))
    _article(
        project,
        "step-0.md",
        title="Step 0",
        body=f"A step of the proof.\n\n## Sources\n\n{_GAUSS}\n- [Mortenson](https://doi.org/10.1007/s00222-012-0383-5)",
        metadata=(f"article_id: af_{0:024x}",),
    )
    _note(project, "Lib.Convex.other does not apply.\n")

    assert _check(project, capsys) == (0, [_MODULE_WARNING])


@pytest.mark.parametrize(
    "lean",
    [
        "example (a b : Nat) (h : a = b) : b = a := by symm; exact h",
        "theorem flip (a b : Nat) (h : a = b) : b = a := h.symm",
        "-- symm is not the library's\n",
        "import Lib.Convex.Sub",
        "open Lib in\nexample := Convex.helperA",
    ],
)
def test_a_lean_file_naming_neither_the_module_nor_a_declaration_in_full_leaves_it_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lean: str
) -> None:
    helpers = (*_HELPERS[:3], declaration("Lib.Convex.Rel.symm", docstring="A helper."))
    project = _module_consumer(tmp_path, declarations=helpers)
    (project / "Project.lean").write_text(lean + "\n", encoding="utf-8")

    _status, warnings = _check(project, capsys)

    assert len(warnings) == 1 and "module Lib.Convex's docstring cites" in warnings[0]


def test_a_module_covering_one_declaration_names_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _module_consumer(tmp_path, declarations=_HELPERS[:1])

    assert _check(project, capsys) == (
        0,
        [
            "warning: library atlas: module Lib.Convex's docstring cites arXiv 2307.05244, which unproved article "
            "step-0 reaches; its declaration Lib.Convex.helperA cites it only there, and no implementation "
            "note, source page or project Lean file names the module or that declaration, so reuse it or record why "
            "it does not apply"
        ],
    )


@pytest.mark.parametrize(
    ("count", "listed"),
    [(2, "Lib.Convex.helperA and Lib.Convex.helperB"), (3, "Lib.Convex.helperA, Lib.Convex.helperB and Lib.Convex.helperC")],
)
def test_a_module_covering_up_to_three_declarations_lists_them_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], count: int, listed: str
) -> None:
    project = _module_consumer(tmp_path, declarations=_HELPERS[:count])

    _status, warnings = _check(project, capsys)

    assert len(warnings) == 1
    assert f"; its declarations {listed} cite it only there, and " in warnings[0]


def test_a_module_docstring_citing_a_paper_no_article_reaches_gives_no_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, docstring="A helper.", module_doc="Related work: arXiv:1702.01627.")

    assert _check(project, capsys) == (0, [])


def test_a_module_catalog_asks_for_no_search(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A pending catalog is not proved, but it inventories existing Lean rather than awaiting a proof."""
    project = _consumer(tmp_path, articles=0)
    ledger = project / "blueprint/sources/ledger.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Ledger\n\nAfter https://arxiv.org/abs/2307.05244.\n", encoding="utf-8")
    _article(
        project,
        "catalog.md",
        title="Existing module",
        body="The module's declarations.\n\n## Sources\n\n- [Declaration ledger](../sources/ledger.md)",
        metadata=("catalog: module",),
    )

    assert _check(project, capsys) == (0, [])


def test_a_declaration_whose_docstring_and_module_docstring_both_cite_warns_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _check(_consumer(tmp_path, module_doc="After arXiv:2307.05244."), capsys) == (0, [_warning()])


def test_the_warning_counts_every_article_reaching_the_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, sources="- [Gauss](https://arxiv.org/pdf/2307.05244)", articles=3)

    assert _check(project, capsys) == (0, [_warning(reach="3 unproved articles reach")])


def test_a_declaration_citing_several_sources_names_the_most_reached_and_counts_every_article(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(
        tmp_path,
        articles=3,
        docstring="Source: arXiv:2307.05244, doi:10.1007/s00222-012-0383-5 and arXiv:1702.01627.",
    )
    # step-0 also cites the DOI, and step-1 the second paper too, so arXiv 2307.05244 is reached most.
    for number, extra in ((0, "https://doi.org/10.1007/s00222-012-0383-5"), (1, "https://arxiv.org/abs/1702.01627")):
        _article(
            project,
            f"step-{number}.md",
            title=f"Step {number}",
            body=f"A step of the proof.\n\n## Sources\n\n{_GAUSS}\n- [More]({extra})",
            metadata=(f"article_id: af_{number:024x}",),
        )

    assert _check(project, capsys) == (
        0,
        [
            _warning(
                source="arXiv 2307.05244 and 2 more sources the blueprint cites",
                reach="3 unproved articles reach",
            )
        ],
    )


@pytest.mark.parametrize(
    "changes",
    [{"status": "wanted"}, {"declarations": (declaration(_THEOREM, docstring="arXiv:2307.05244", auto_named=True),)}],
)
def test_a_wanted_or_automatically_named_declaration_is_not_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], changes: dict
) -> None:
    assert _check(_consumer(tmp_path, **changes), capsys) == (0, [])


def test_a_proved_article_no_longer_asks_for_a_search(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _check(_consumer(tmp_path, proved=True), capsys) == (0, [])


def test_the_warning_names_an_article_reaching_the_source_it_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, articles=3, docstring="After arXiv:2307.05244 and doi:10.1007/s00222-012-0383-5.")
    # step-0 cites only the DOI; step-1 and step-2 cite the paper, which is then the source named.
    _article(
        project,
        "step-0.md",
        title="Step 0",
        body="A step of the proof.\n\n## Sources\n\n- [Mortenson](https://doi.org/10.1007/s00222-012-0383-5)",
        metadata=(f"article_id: af_{0:024x}",),
    )

    assert _check(project, capsys) == (
        0,
        [
            "warning: library atlas: Nat.threeSquareRepresentationCount_eq_threeSquareRepresentationCount_div_four's "
            "docstring cites arXiv 2307.05244 and 1 more source the blueprint cites, which 3 unproved articles "
            "reach, including step-1; no implementation note, source page or project Lean file names it, so reuse "
            "it or record why it does not apply"
        ],
    )


def _cite_from_the_root(project: Path) -> None:
    _article(project, "README.md", title="Three squares", body=f"The book.\n\n## Sources\n\n{_GAUSS}")


def test_a_container_stops_asking_for_a_search_once_its_scope_is_proved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, proved=True)
    _cite_from_the_root(project)

    assert _check(project, capsys) == (0, [])


def test_a_container_stops_asking_once_every_nested_scope_is_proved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, articles=0)
    _cite_from_the_root(project)
    _article(project, "chapter/README.md", title="Chapter")
    _article(
        project,
        "chapter/step.md",
        title="Step",
        metadata=("declaration: theorem", "statement: formalized", "proof: formalized", "lean: Project.step"),
    )
    (project / "Project.lean").write_text("theorem Project.step : True := trivial\n", encoding="utf-8")

    assert _check(project, capsys) == (0, [])


def test_a_leaf_marked_not_ready_keeps_its_container_asking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, proved=True)
    _cite_from_the_root(project)
    _article(project, "later.md", title="Later", metadata=("not_ready: true",))

    status, warnings = _check(project, capsys)

    assert status == 0 and len(warnings) == 1
    assert "which unproved article " in warnings[0] and "including" not in warnings[0]


def test_a_catalog_marked_not_ready_leaves_its_proved_container_quiet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit leaves catalogs out of a container's targets, so one marked not ready keeps no container asking."""
    project = _consumer(tmp_path, proved=True)
    _cite_from_the_root(project)
    ledger = project / "blueprint/sources/ledger.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("# Ledger\n\nThe module's declarations.\n", encoding="utf-8")
    _article(
        project,
        "catalog.md",
        title="Existing module",
        body="The module's declarations.\n\n## Sources\n\n- [Declaration ledger](../sources/ledger.md)",
        metadata=("catalog: module", "not_ready: true"),
    )

    assert _check(project, capsys) == (0, [])


def test_the_warning_names_a_leaf_before_its_container(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _consumer(tmp_path, articles=2)
    _cite_from_the_root(project)

    # The root, `roadmap`, sorts before `step-0` but is no place to reuse a declaration.
    assert _check(project, capsys) == (0, [_warning(reach="3 unproved articles reach")])


def test_an_article_stated_in_lean_but_unproved_still_asks_for_a_search(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _check(_consumer(tmp_path, stated=True), capsys) == (0, [_warning()])


def _cited(*names: str) -> tuple:
    return tuple(declaration(name, docstring="arXiv:2307.05244") for name in names)


def test_declarations_named_in_different_notes_are_both_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, articles=2, declarations=_cited("A.foo", "B.bar"))
    _note(project, "Reuse `A.foo`.\n", 0)
    _note(project, "Reuse `B.bar`.\n", 1)

    assert _check(project, capsys) == (0, [])


def test_declarations_sharing_a_last_component_are_each_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path, declarations=_cited("A.foo", "B.foo"))

    assert _check(project, capsys) == (0, [_warning("A.foo"), _warning("B.foo")])


def test_a_hidden_file_among_the_notes_names_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _consumer(tmp_path)
    notes = project / "blueprint/.implementation-notes"
    notes.mkdir()
    (notes / f".af_{0:024x}.md.swp").write_text(f"{_THEOREM}\n", encoding="utf-8")

    assert _check(project, capsys) == (0, [_warning()])


def test_a_library_citing_another_paper_or_an_uncited_blueprint_gives_no_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _check(_consumer(tmp_path, docstring="Source: arXiv:1702.01627v2."), capsys) == (0, [])
    assert _check(_consumer(tmp_path / "other", sources="- [A talk](https://example.org/talk)"), capsys) == (0, [])


def _stale(project: Path) -> None:
    # An edit after indexing makes the index stale, so search would refuse it too.
    source = project / ".lake/packages/atlas/Lib/Classic.lean"
    source.write_text(source.read_text(encoding="utf-8") + "-- edited\n", encoding="utf-8")


def test_an_index_that_cannot_be_used_is_named_and_never_fails_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path)
    _stale(project)

    status, warnings = _check(project, capsys)

    assert status == 0
    assert warnings == [
        "warning: library atlas: its index cannot be used, so its declarations were not compared with the "
        "blueprint's sources: 1 source file differs from its index: Lib/Classic.lean"
    ]


def _index(project: Path) -> Path:
    return project / ".lake/packages/atlas/autoform-library-index.jsonl"


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("cut", "its index is not usable: index does not end with a newline; its last record may be cut short"),
        ("schema", "its index is not usable: unknown index schema autoform-library-index/v999"),
        ("header", "its index is not usable: index has no header on its first line"),
    ],
)
def test_an_index_that_cannot_be_parsed_is_named_and_never_fails_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], damage: str, reason: str
) -> None:
    project = _consumer(tmp_path)
    index = _index(project)
    if damage == "cut":  # an interrupted write
        index.write_bytes(index.read_bytes().rstrip(b"\n"))
    elif damage == "header":
        index.write_bytes(b"{}\n")
    else:  # a library indexed by a newer generator
        header, rest = index.read_text(encoding="utf-8").split("\n", 1)
        index.write_text(json.dumps({**json.loads(header), "schema": "autoform-library-index/v999"}) + "\n" + rest)

    assert _check(project, capsys) == (
        0,
        [
            "warning: library atlas: its index cannot be used, so its declarations were not compared with the "
            f"blueprint's sources: {reason}"
        ],
    )


@pytest.mark.parametrize("sources", ["- [A talk](https://example.org/talk)", _GAUSS])
def test_a_stale_index_is_not_verified_when_it_cites_nothing_the_blueprint_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], sources: str
) -> None:
    project = _consumer(tmp_path, sources=sources, docstring="Source: arXiv:1702.01627.")
    _stale(project)

    assert _check(project, capsys) == (0, [])


def test_a_lean_root_without_a_lake_manifest_compares_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path)
    (project / "lake-manifest.json").unlink()

    assert _check(project, capsys) == (0, [])


def test_lake_files_that_cannot_be_used_are_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _consumer(tmp_path)
    manifest = project / "lake-manifest.json"
    manifest.write_text(json.dumps({**json.loads(manifest.read_text(encoding="utf-8")), "lakeDir": "_lake"}))

    assert _check(project, capsys) == (
        0,
        [
            "warning: the project's Lake files cannot be used, so no shared library was compared with the blueprint's "
            "sources: lake-manifest.json sets lakeDir to '_lake'; only .lake is supported"
        ],
    )


def test_a_lean_root_written_with_a_tilde_is_read_like_any_other(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _consumer(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["check", str(project / "blueprint"), "--lean-root", "~/project"]) == 0
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("warning:")] == [_warning()]

    manifest = project / "lake-manifest.json"
    manifest.write_text(json.dumps({**json.loads(manifest.read_text(encoding="utf-8")), "lakeDir": "_lake"}))
    assert main(["check", str(project / "blueprint"), "--lean-root", "~/project"]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("warning: the project's Lake files cannot be used")


def test_a_lake_manifest_that_is_a_dangling_link_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _consumer(tmp_path)
    manifest = project / "lake-manifest.json"
    manifest.unlink()
    try:
        manifest.symlink_to(tmp_path / "moved/lake-manifest.json")
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert _check(project, capsys) == (
        0,
        [
            "warning: the project's Lake files cannot be used, so no shared library was compared with the blueprint's "
            "sources: lake-manifest.json is not a Lake manifest Autoform reads"
        ],
    )


@pytest.mark.parametrize("root", ["~autoform-no-such-user/project", "{project}\x00"])
def test_a_lean_root_that_cannot_be_resolved_keeps_its_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], root: str
) -> None:
    project = _consumer(tmp_path)

    assert main(["check", str(project / "blueprint"), "--lean-root", root.format(project=project)]) == 1
    assert capsys.readouterr().out.splitlines()[-1].startswith("error: Lean sources could not be indexed")


def test_a_failing_lean_name_keeps_the_check_failing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _consumer(tmp_path)
    _article(project, "bad.md", title="Bad", metadata=("declaration: theorem", "lean: Project.missing"))

    assert main(["check", str(project / "blueprint"), "--lean-root", str(project)]) == 1
    assert "error: bad: declaration not found in" in capsys.readouterr().out


@_UNREADABLE
def test_lean_files_that_cannot_be_read_leave_the_index_build_to_report_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path)
    lean = project / "Project.lean"
    lean.write_text("theorem Project.other : True := trivial\n", encoding="utf-8")
    lean.chmod(0)
    try:
        status = main(["check", str(project / "blueprint"), "--lean-root", str(project)])
    finally:
        lean.chmod(0o644)

    assert status == 1
    assert "error: Lean sources could not be indexed" in capsys.readouterr().out


@_UNREADABLE
def test_a_source_page_that_cannot_be_read_cites_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _consumer(tmp_path, sources="- [Gauss](../sources/gauss.md)")
    page = project / "blueprint/sources/gauss.md"
    page.parent.mkdir(parents=True)
    page.write_text("arXiv:2307.05244\n", encoding="utf-8")
    page.chmod(0)
    try:
        assert _check(project, capsys) == (
            0,
            [_stop("blueprint could not be captured: permission denied: sources/gauss.md")],
        )
    finally:
        page.chmod(0o644)


@_UNREADABLE
def test_an_index_that_cannot_be_read_is_named_and_never_fails_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path)
    _index(project).chmod(0)
    try:
        status, warnings = _check(project, capsys)
    finally:
        _index(project).chmod(0o644)

    assert (status, warnings) == (
        0,
        [
            "warning: library atlas: its index cannot be used, so its declarations were not compared with the "
            "blueprint's sources: its index cannot be read"
        ],
    )


def test_a_declaration_name_cannot_forge_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _consumer(tmp_path, declarations=_cited("Nat.foo\x1b[2K\rerror: forged"))

    assert main(["check", str(project / "blueprint"), "--lean-root", str(project)]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\r" not in out
    assert "Nat.foo\\x1b[2K\\rerror: forged's docstring cites" in out


def test_a_symlinked_lean_root_is_compared_like_its_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _consumer(tmp_path)
    link = tmp_path / "link"
    try:
        link.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    assert main(["check", str(project / "blueprint"), "--lean-root", str(link)]) == 0
    assert [line for line in capsys.readouterr().out.splitlines() if line.startswith("warning:")] == [_warning()]
