"""`move`: an article renamed with every link and annotation that names it by its path, read back the same, or
nothing moved."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from autoform_cli import annotation_writer as writer
from autoform_cli import move
from autoform_cli.__main__ import main
from autoform_cli.annotations import read_links
from autoform_cli.claims import CLAIM_REF_PREFIX, author_claim_key
from autoform_cli.graph import GraphValidationError, load_graph
from autoform_cli.links_set import parse_arguments, set_facts
from autoform_cli.move import MoveError, apply_move, plan_move

RENAME = "rename it (in Git: git mv through a temporary name)"
CANNOT_NAME = "a path holding '#', '?', ':', '|' or '\\'"
APPROVED = "---\nreview_approved: sha256:" + "0" * 64 + "\n---\n\n"
DRIFTS = "its review approval drifts, since this rewrites a link in its title or statement or moves the source passage"
VAULT = {
    "roadmap/README.md": "# Roadmap\n\n- [Chapter](chapter/README.md)\n",
    "roadmap/chapter/README.md": "# Chapter\n\nIntroduction.\n\n- [Lemma](lemma.md)\n- [Result](result.md)\n",
    "roadmap/chapter/lemma.md": "# Lemma\n\nA lemma.\n",
    "roadmap/chapter/result.md": (
        "# Result\n\nA result, by [the lemma](lemma.md#statement).\n\n## Depends on\n\n- [Lemma](lemma.md)\n\n"
        "## Sources\n\n- [Notes](../../sources/notes.md)\n"
    ),
    "sources/notes.md": (
        "# Notes\n\nSee [the result](../roadmap/chapter/result.md) and [the lemma][l].\n\n"
        "[l]: ../roadmap/chapter/lemma.md\n"
    ),
}
OTHER = {"roadmap/other/README.md": "# Other\n\nMore.\n"}


def _vault(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    blueprint = tmp_path / "blueprint"
    for name, text in {**VAULT, **(extra or {})}.items():
        path = blueprint / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return blueprint


def _bytes(blueprint: Path) -> dict[str, bytes]:
    files = (path for path in blueprint.rglob("*") if path.is_file())
    return {path.relative_to(blueprint).as_posix(): path.read_bytes() for path in files}


def _plan(blueprint: Path, old: str, new: str, **options: str) -> move.MovePlan:
    return plan_move(blueprint, str(blueprint / old), str(blueprint / new), **options)


def _move(blueprint: Path, old: str, new: str, **options: str) -> move.MovePlan:
    plan = _plan(blueprint, old, new, **options)
    apply_move(plan)
    return plan


def _refused(blueprint: Path, old: str, new: str, **options: str) -> list[str]:
    with pytest.raises(MoveError) as refused:
        _plan(blueprint, old, new, **options)
    return list(refused.value.issues)


def _undone(plan: move.MovePlan) -> list[str]:
    with pytest.raises(MoveError) as undone:
        apply_move(plan)
    return list(undone.value.issues)


def _migrated(blueprint: Path, *articles: str) -> None:
    """Give *blueprint* the writer's template and an annotation for each of *articles*."""
    (blueprint / "links/roadmap").mkdir(parents=True, exist_ok=True)
    (blueprint / "links/roadmap/_defaults.json").write_bytes(writer.dumps(writer.PROJECT_TEMPLATE))
    for article in articles:
        named, edits = parse_arguments([str(blueprint / article), "origin=cited"], [])
        set_facts(blueprint, named, edits)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_moved_article_takes_every_link_to_it_along(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, OTHER)

    plan = _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/other/basic-lemma.md")

    assert plan.top == ("chapter/lemma", "other/basic-lemma")
    assert plan.renamed == (("roadmap/chapter/lemma.md", "roadmap/other/basic-lemma.md"),)
    assert plan.edited == ("roadmap/chapter/README.md", "roadmap/chapter/result.md", "sources/notes.md")
    assert plan.claims == "not checked"
    assert "the site publishes what this moves at new addresses, and nothing redirects the old ones" in plan.warnings
    files = _bytes(blueprint)
    assert "roadmap/chapter/lemma.md" not in files
    assert files["roadmap/other/basic-lemma.md"] == b"# Lemma\n\nA lemma.\n"
    result = files["roadmap/chapter/result.md"].decode()
    assert "[the lemma](../other/basic-lemma.md#statement)" in result
    assert "- [Lemma](../other/basic-lemma.md)\n" in result
    assert "[l]: ../roadmap/other/basic-lemma.md\n" in files["sources/notes.md"].decode()
    graph = load_graph(blueprint)
    assert graph.nodes["chapter/result"].dependencies == ("other/basic-lemma",)


def test_a_moved_article_rebases_its_own_links(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, {"roadmap/chapter/part/README.md": "# Part\n\nA part.\n"})

    _move(blueprint, "roadmap/chapter/result.md", "roadmap/chapter/part/result.md")

    files = _bytes(blueprint)
    moved = files["roadmap/chapter/part/result.md"].decode()
    assert "[the lemma](../lemma.md#statement)" in moved
    assert "- [Lemma](../lemma.md)\n" in moved
    assert "- [Notes](../../../sources/notes.md)\n" in moved
    assert "[the result](../roadmap/chapter/part/result.md)" in files["sources/notes.md"].decode()
    assert load_graph(blueprint).nodes["chapter/part/result"].sources == ("../../../sources/notes.md",)


def test_a_new_destination_is_spelled_as_the_old_one_was(tmp_path: Path) -> None:
    use = "# Use\n\nA use.\n\n## Depends on\n\n- [Lemma](lemm%61.md)\n"
    blueprint = _vault(tmp_path, {"roadmap/chapter/use.md": use})

    _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/lemma,2.md")

    # A destination written percent-encoded is written so again; any other only where Markdown needs it.
    assert (blueprint / "roadmap/chapter/use.md").read_text(encoding="utf-8") == use.replace("lemm%61", "lemma%2C2")
    assert "- [Lemma](lemma,2.md)\n" in (blueprint / "roadmap/chapter/README.md").read_text(encoding="utf-8")
    assert load_graph(blueprint).nodes["chapter/use"].dependencies == ("chapter/lemma,2",)


def test_a_rebased_link_encodes_a_hash_in_a_folders_name(tmp_path: Path) -> None:
    # Out of C#, the lemma's link to the result there would otherwise name '../C', with a fragment after it.
    extra = {
        "roadmap/README.md": VAULT["roadmap/README.md"] + "- [C#](C%23/README.md)\n",
        "roadmap/C#/README.md": "# C#\n\nThe language.\n\n- [Lemma](lemma.md)\n- [Result](result.md)\n",
        "roadmap/C#/lemma.md": "# Lemma\n\nA lemma, before [the result](result.md).\n",
        "roadmap/C#/result.md": "# Result\n\nA result.\n",
    }
    blueprint = _vault(tmp_path, {**extra, **OTHER})

    _move(blueprint, "roadmap/C#/lemma.md", "roadmap/other/lemma.md")

    moved = (blueprint / "roadmap/other/lemma.md").read_text(encoding="utf-8")
    assert moved == "# Lemma\n\nA lemma, before [the result](../C%23/result.md).\n"


def test_a_moved_articles_escaped_link_is_rebased(tmp_path: Path) -> None:
    # Markdown reads 'lecture\_notes.md' as 'lecture_notes.md', so that is the file the link names from the new folder.
    extra = {
        "sources/lecture_notes.md": "# Lecture notes\n",
        "roadmap/chapter/part/README.md": "# Part\n\nA part.\n",
        "roadmap/chapter/result.md": (
            "# Result\n\nA result.\n\n## Sources\n\n- [Notes](../../sources/lecture\\_notes.md)\n"
        ),
    }
    blueprint = _vault(tmp_path, extra)

    _move(blueprint, "roadmap/chapter/result.md", "roadmap/chapter/part/result.md")

    moved = (blueprint / "roadmap/chapter/part/result.md").read_text(encoding="utf-8")
    assert "- [Notes](../../../sources/lecture_notes.md)\n" in moved


def test_a_folder_article_moves_with_its_folder_and_annotations(tmp_path: Path) -> None:
    extra = {
        "roadmap/chapter/figure.png": "not really a picture",
        "roadmap/chapter/.draft": "hidden, and carried",
        "roadmap/chapter/plot.py": "print('plot')\n",
        "roadmap/chapter/section/README.md": "# Section\n\nBy [the lemma](../lemma.md).\n\n- [Item](item.md)\n",
        "roadmap/chapter/section/item.md": "# Item\n\nAn item.\n",
        "roadmap/chapter/README.md": VAULT["roadmap/chapter/README.md"] + "- [Section](section/README.md)\n",
    }
    blueprint = _vault(tmp_path, extra)
    (blueprint / "roadmap/chapter/plot.py").chmod(0o755)
    (blueprint / "roadmap/chapter/empty").mkdir()
    _migrated(blueprint, "roadmap/chapter/result.md", "roadmap/chapter/README.md")
    (blueprint / "links/roadmap/chapter/notes.txt").write_text("not an annotation\n", encoding="utf-8")
    (blueprint / "links/roadmap/chapter/.keep").write_text("", encoding="utf-8")

    plan = _move(blueprint, "roadmap/chapter/README.md", "roadmap/part/README.md")

    assert plan.top == ("chapter", "part")
    assert dict(plan.ids) == {
        "chapter": "part",
        "chapter/lemma": "part/lemma",
        "chapter/result": "part/result",
        "chapter/section": "part/section",
        "chapter/section/item": "part/section/item",
    }
    files = _bytes(blueprint)
    assert not [name for name in files if name.startswith(("roadmap/chapter/", "links/roadmap/chapter/"))]
    assert files["roadmap/part/figure.png"] == b"not really a picture"
    assert files["roadmap/part/.draft"] == b"hidden, and carried"
    assert files["links/roadmap/part/notes.txt"] == b"not an annotation\n"
    assert "links/roadmap/part/.keep" in files
    assert _mode(blueprint / "roadmap/part/plot.py") == 0o755
    assert "- [Chapter](part/README.md)\n" in files["roadmap/README.md"].decode()
    assert "By [the lemma](../lemma.md)." in files["roadmap/part/section/README.md"].decode()
    assert "../../../roadmap/part/result.md" in files["links/roadmap/part/result.jsonl"].decode()
    graph = load_graph(blueprint)
    assert set(graph.nodes) == {"roadmap", *plan.ids.values()}
    assert graph.nodes["part/result"].origin == "cited"
    after = read_links(blueprint, {node.path: node.id for node in graph.nodes.values()})
    assert after is not None
    assert after.files[blueprint / "roadmap/part/result.md"] == ("links/roadmap/part/result.jsonl",)
    # Its folders go, the empty one included.
    assert not (blueprint / "roadmap/chapter").exists()
    assert not (blueprint / "links/roadmap/chapter").exists()


def test_an_undone_move_keeps_each_files_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blueprint = _vault(tmp_path, {"roadmap/chapter/plot.py": "print('plot')\n"})
    (blueprint / "roadmap/chapter/plot.py").chmod(0o755)
    before = _bytes(blueprint)
    plan = _plan(blueprint, "roadmap/chapter/README.md", "roadmap/part/README.md")
    monkeypatch.setattr(move, "_read_back", lambda plan: ["part: reads back otherwise"])

    assert _undone(plan) == ["part: reads back otherwise", "nothing moved"]

    assert _bytes(blueprint) == before
    assert _mode(blueprint / "roadmap/chapter/plot.py") == 0o755


def test_a_moved_articles_annotations_move_with_it(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, OTHER)
    _migrated(blueprint, "roadmap/chapter/lemma.md")
    page = blueprint / "roadmap/chapter/lemma.md"
    mathlib = writer.annotation_paths(blueprint, page).mathlib
    mathlib.parent.mkdir(parents=True)
    (blueprint / "links/mathlib/_defaults.json").write_bytes(writer.dumps(writer.MATHLIB_TEMPLATE))
    row = {"subject": writer._subject(page, mathlib), "object": [{"location": {"declaration": "Nat.succ_le"}}]}
    mathlib.write_bytes(writer.dumps(row))
    assert load_graph(blueprint).nodes["chapter/lemma"].mathlib_declaration == "Nat.succ_le"
    old = (blueprint / "links/roadmap/chapter/lemma.jsonl").read_text(encoding="utf-8")
    old_mathlib = mathlib.read_text(encoding="utf-8")

    plan = _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/other/lemma.md")

    assert ("links/roadmap/chapter/lemma.jsonl", "links/roadmap/other/lemma.jsonl") in plan.renamed
    assert ("links/mathlib/roadmap/chapter/lemma.jsonl", "links/mathlib/roadmap/other/lemma.jsonl") in plan.renamed
    # Only the reference changes; every other byte of the row stays.
    new = (blueprint / "links/roadmap/other/lemma.jsonl").read_text(encoding="utf-8")
    assert new == old.replace("roadmap/chapter/lemma.md", "roadmap/other/lemma.md")
    new_mathlib = (blueprint / "links/mathlib/roadmap/other/lemma.jsonl").read_text(encoding="utf-8")
    assert new_mathlib == old_mathlib.replace("roadmap/chapter/lemma.md", "roadmap/other/lemma.md")
    lemma = load_graph(blueprint).nodes["other/lemma"]
    assert (lemma.origin, lemma.mathlib_declaration) == ("cited", "Nat.succ_le")
    assert not (blueprint / "links/roadmap/chapter").exists()


def test_an_annotation_file_never_read_moves_as_it_is(tmp_path: Path) -> None:
    # Read as no article's annotation before, it need not be read as one after.
    blueprint = _vault(tmp_path, OTHER)
    (blueprint / "links/roadmap/chapter").mkdir(parents=True)
    (blueprint / "links/roadmap/chapter/lemma.jsonl").write_text("not an annotation\n", encoding="utf-8")

    _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/other/lemma.md")

    assert (blueprint / "links/roadmap/other/lemma.jsonl").read_text(encoding="utf-8") == "not an annotation\n"


def test_a_reference_in_a_file_the_move_does_not_own_is_rewritten_alone(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, OTHER)
    row = {"subject": [{"type": "generic", "uri": "../roadmap/chapter/lemma.md"}], "note": "kept as written"}
    absolute = {"subject": [{"type": "generic", "uri": str(blueprint / "roadmap/chapter/lemma.md")}]}
    rows = ["  " + json.dumps(row), json.dumps(absolute)]
    (blueprint / "links").mkdir()
    (blueprint / "links/hand.jsonl").write_bytes("".join(f"{row}\r\n" for row in rows).encode())

    plan = _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/other/lemma.md")

    assert "links/hand.jsonl" in plan.edited
    # A relative reference stays relative, and an absolute one absolute.
    expected = "".join(f"{row.replace('chapter/lemma.md', 'other/lemma.md')}\r\n" for row in rows)
    assert (blueprint / "links/hand.jsonl").read_bytes() == expected.encode()


def test_a_moved_annotation_file_rebases_every_reference(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, {"roadmap/part/README.md": "# Part\n\nA part.\n"})
    row = {
        "$schema": "../../schema.json",
        "subject": [
            {"type": "generic", "uri": "../../../roadmap/chapter/lemma.md", "location": {"profile": "../p.json"}},
            # A file beside it moves with it, so the reference stays.
            {"type": "generic", "uri": "../../../roadmap/chapter/result.md", "location": {"profile": "p.txt"}},
        ],
        "object": {"type": "generic", "uri": "../../../sources/notes.md"},
        "citation": {"uri": "../kept.md"},
    }
    (blueprint / "links/roadmap/chapter").mkdir(parents=True)
    (blueprint / "links/roadmap/chapter/hand.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (blueprint / "links/roadmap/chapter/p.txt").write_text("a profile\n", encoding="utf-8")

    _move(blueprint, "roadmap/chapter/README.md", "roadmap/part/chapter/README.md")

    moved = json.loads((blueprint / "links/roadmap/part/chapter/hand.jsonl").read_text(encoding="utf-8"))
    lemma, result = "../../../../roadmap/part/chapter/lemma.md", "../../../../roadmap/part/chapter/result.md"
    assert moved == {
        "$schema": "../../../schema.json",
        "subject": [
            {"type": "generic", "uri": lemma, "location": {"profile": "../../p.json"}},
            {"type": "generic", "uri": result, "location": {"profile": "p.txt"}},
        ],
        "object": {"type": "generic", "uri": "../../../../sources/notes.md"},
        "citation": {"uri": "../kept.md"},
    }
    assert (blueprint / "links/roadmap/part/chapter/p.txt").read_text(encoding="utf-8") == "a profile\n"


def test_a_rebased_reference_to_a_folder_keeps_its_slash(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, {"roadmap/part/README.md": "# Part\n\nA part.\n"})
    row = {**CITING_BESIDE, "object": {"type": "generic", "uri": "../../../sources/"}}
    (blueprint / "links/roadmap/chapter").mkdir(parents=True)
    (blueprint / "links/roadmap/chapter/hand.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    _move(blueprint, "roadmap/chapter/README.md", "roadmap/part/chapter/README.md")

    moved = json.loads((blueprint / "links/roadmap/part/chapter/hand.jsonl").read_text(encoding="utf-8"))
    assert moved["object"]["uri"] == "../../../../sources/"


def test_what_literature_links_does_not_read_as_a_reference_is_left(tmp_path: Path) -> None:
    # Rows literature-links cannot read, which the loader leaves out, and an extension's own member keep every byte.
    rows = [
        '{"subject": [{"type": "generic", "uri": "../roadmap/chapter/lemma.md"}],}',
        '{"subject": [{"type": "generic", "uri": "../roadmap/chapter/lemma.md#\\udce9"}]}',
        '{"subject": [], "extensions": {"x": {"uri": "../roadmap/chapter/lemma.md"}}}',
        '{"subject": {"type": "generic", "uri": "../roadmap/chapter/lemma.md"}}',
    ]
    blueprint = _vault(tmp_path, OTHER)
    (blueprint / "links").mkdir()
    (blueprint / "links/hand.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

    _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/other/lemma.md")

    moved = [*rows[:3], rows[3].replace("chapter/lemma.md", "other/lemma.md")]
    assert (blueprint / "links/hand.jsonl").read_text(encoding="utf-8") == "\n".join(moved) + "\n"


@pytest.mark.parametrize(
    ("old", "new", "issue"),
    [
        ("roadmap/chapter/lemma.md", "roadmap/chapter/result.md", "roadmap/chapter/result.md: exists"),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/lemma.md", "the article is there already"),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/Lemma.md", f"only in case; {RENAME}"),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/.lemma.md", "a hidden path"),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/a=b.md", "a path holding '='"),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/lemma#2.md", CANNOT_NAME),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/lemma?.md", CANNOT_NAME),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/lemma:2.md", CANNOT_NAME),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/le|mma.md", CANNOT_NAME),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/le\\mma.md", CANNOT_NAME),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/le\udce9mma.md", "not UTF-8, which a link names a file in"),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/le\u0301mma.md", "not composed (NFC) as Git records it"),
        ("roadmap/chapter/lemma.md", "roadmap/Chapter/l.md", "roadmap/Chapter differs only in case from chapter"),
        ("roadmap/chapter/README.md", "roadmap/Chapter/inner/README.md", "differs only in case from chapter"),
        ("roadmap/chapter/lemma.md", "roadmap/loose/lemma.md", "roadmap/loose has no README.md"),
        ("roadmap/chapter/README.md", "roadmap/top/part/README.md", "roadmap/top has no README.md"),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/lemma.MD", "not a Markdown path"),
        ("roadmap/chapter/lemma.md", "sources/lemma.md", "not a Markdown path"),
        ("roadmap/chapter/lemma.md", "roadmap/chapter/other/README.md", "a folder article moves as"),
        ("roadmap/chapter/README.md", "roadmap/chapter.md", "a folder article moves as"),
        ("roadmap/chapter/README.md", "roadmap/chapter/inner/README.md", "inside the folder that moves"),
        ("roadmap/README.md", "roadmap/top/README.md", "the roadmap's own README stays"),
        ("roadmap/chapter/lemma.md", "roadmap/roadmap.md", "is the roadmap's own README's"),
        ("roadmap/chapter/lemma.md", "roadmap/chapter.md", "roadmap/chapter/README.md has its node ID"),
        ("roadmap/chapter/lemma.md", "roadmap/af_0123456789abcdef01234567.md", "the form of an article ID"),
    ],
)
def test_a_move_no_blueprint_could_take_is_refused(tmp_path: Path, old: str, new: str, issue: str) -> None:
    blueprint = _vault(tmp_path)
    before = _bytes(blueprint)

    issues = _refused(blueprint, old, new)

    assert any(issue in found for found in issues), issues
    assert _bytes(blueprint) == before


@pytest.mark.parametrize(
    ("old", "new", "linked"),
    [
        ("roadmap/chapter/lemma.md", "roadmap/other/lemma.md", "links/roadmap/chapter/lemma.jsonl"),
        ("roadmap/chapter/README.md", "roadmap/part/README.md", "links/roadmap/chapter"),
    ],
)
def test_annotations_behind_a_symbolic_link_refuse_the_move(tmp_path: Path, old: str, new: str, linked: str) -> None:
    # The loader never follows the link, so nothing behind it is the article's to carry.
    blueprint = _vault(tmp_path, OTHER)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "lemma.jsonl").write_text("{}\n", encoding="utf-8")
    (blueprint / "links/roadmap").mkdir(parents=True)
    (blueprint / "links/roadmap/chapter").symlink_to(shared, target_is_directory=True)

    issues = _refused(blueprint, old, new)

    assert f"{linked}: reached through a symbolic link, which a move does not follow" in issues
    assert [path.name for path in shared.iterdir()] == ["lemma.jsonl"]


def test_a_link_the_new_name_would_break_refuses_the_move(tmp_path: Path) -> None:
    # The backtick in the new name would pair with the one after the link, and the link would read as code.
    blueprint = _vault(tmp_path, {"roadmap/chapter/use.md": "# Use\n\nSee [the lemma](lemma.md) and `code`.\n"})
    before = _bytes(blueprint)

    assert _refused(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/le`mma.md") == [
        "roadmap/chapter/use.md:3: a link to lemma.md cannot be rewritten in place; edit it by hand first"
    ]
    assert _bytes(blueprint) == before


def test_a_link_the_site_reads_past_its_first_parenthesis_refuses_the_move(tmp_path: Path) -> None:
    # The graph's readers end the destination at its first ')', the site at the one that balances the '(', so no new
    # destination keeps both reading the book.
    page = "# Result\n\nA result.\n\n## Sources\n\n- [The book](../../sources/Book_(2001).md)\n"
    extra = {"sources/Book_(2001).md": "# Book\n", "roadmap/chapter/part/README.md": "# Part\n\nA part.\n"}
    blueprint = _vault(tmp_path, {**extra, "roadmap/chapter/result.md": page})
    before = _bytes(blueprint)

    issues = _refused(blueprint, "roadmap/chapter/result.md", "roadmap/chapter/part/result.md")

    stale = "a link to ../../sources/Book_(2001 cannot be rewritten in place; edit it by hand first"
    assert issues == [f"roadmap/chapter/result.md:7: {stale}"]
    assert _bytes(blueprint) == before


def test_a_link_no_reader_counts_is_named_and_left(tmp_path: Path) -> None:
    extra = {"roadmap/chapter/notes.md": "# Notes\n\nShown as code: `[Lemma](lemma.md)`.\n\n![Figure](lemma.md)\n"}
    blueprint = _vault(tmp_path, {**extra, **OTHER})

    plan = _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/other/lemma.md")

    left = [warning for warning in plan.warnings if warning.startswith("roadmap/chapter/notes.md:")]
    assert [warning.split(":")[1] for warning in left] == ["3", "5"]
    assert (blueprint / "roadmap/chapter/notes.md").read_text(encoding="utf-8") == extra["roadmap/chapter/notes.md"]


def test_a_read_back_card_linking_the_article_is_named_and_left(tmp_path: Path) -> None:
    card = f"{move.READBACKS_DIR}/af_0123456789abcdef01234567/card.md"
    blueprint = _vault(tmp_path, {card: "# Card\n\nRead from [the lemma](../../roadmap/chapter/lemma.md).\n"})
    before = (blueprint / card).read_bytes()

    plan = _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")

    assert f"{card}: links to what this moves; read-backs keep their bytes" in plan.warnings
    assert (blueprint / card).read_bytes() == before


THEOREM = "roadmap/chapter/theorem.md"
CITED = {
    "roadmap/chapter/source#1.txt": "First line.\nSecond line.\n",
    "roadmap/chapter/cited.md": APPROVED + "# Cited\n\nA claim.\n\n## Sources\n\n- [Source](source%231.txt#L1-L2)\n",
}


@pytest.mark.parametrize(
    ("extra", "old", "new", "node_id"),
    [
        ({THEOREM: APPROVED + "# Theorem\n\nBy [the lemma](lemma.md).\n"}, "lemma.md", "l.md", "theorem"),
        ({THEOREM: APPROVED + "# On [the lemma](lemma.md)\n\nA claim.\n"}, "lemma.md", "l.md", "theorem"),
        (CITED, "README.md", "../part/README.md", "cited"),
    ],
    ids=["statement", "title", "passage"],
)
def test_an_approval_whose_material_the_move_changes_drifts(
    tmp_path: Path, extra: dict[str, str], old: str, new: str, node_id: str
) -> None:
    # A link in the statement or the title, or the place of the passage cited, is part of what the approval hashes.
    blueprint = _vault(tmp_path, extra)

    plan = _plan(blueprint, f"roadmap/chapter/{old}", f"roadmap/chapter/{new}")

    assert f"chapter/{node_id}: {DRIFTS} it cites" in plan.warnings


def test_atlas_and_structure_that_name_the_article_are_reported_not_rewritten(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path)
    atlas = '{"schema": "autoform-atlas/v1", "areas": {"Algebra": ["chapter\\/lemma"]}}'
    (blueprint / "atlas.json").write_text(atlas, encoding="utf-8")
    structure = "# Structure\n\n- [lemma.md](roadmap/chapter/lemma.md)\n"
    (blueprint / "structure.md").write_text(structure, encoding="utf-8")
    assert load_graph(blueprint).nodes["chapter/lemma"].area == "Algebra"

    # The area atlas.json gave is lost until someone edits it, which the move does not wait for.
    plan = _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")

    assert "atlas.json names chapter/lemma: edit it, or run its generator again" in plan.warnings
    assert "structure.md lists the articles by path: regenerate it" in plan.warnings
    assert (blueprint / "atlas.json").read_text(encoding="utf-8") == atlas
    assert (blueprint / "structure.md").read_text(encoding="utf-8") == structure


def test_an_unpublished_article_has_no_address_to_lose(tmp_path: Path) -> None:
    drafts = {
        "roadmap/drafts/README.md": "---\npublished: false\n---\n\n# Drafts\n\n- [Draft](draft.md)\n",
        "roadmap/drafts/draft.md": "# Draft\n\nA draft.\n",
    }
    blueprint = _vault(tmp_path, drafts)

    plan = _plan(blueprint, "roadmap/drafts/draft.md", "roadmap/drafts/sketch.md")

    assert not [warning for warning in plan.warnings if "new addresses" in warning]


def test_a_move_that_reads_back_otherwise_is_undone(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path)
    before = _bytes(blueprint)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")
    # Written otherwise than planned: the title changes, and the result loses its dependency.
    lemma, result = blueprint / "roadmap/chapter/l.md", blueprint / "roadmap/chapter/result.md"
    plan.changes[lemma] = plan.changes[lemma].replace(b"# Lemma", b"# Other")
    plan.changes[result] = plan.changes[result].replace(b"- [Lemma](l.md)\n", b"")

    issues = _undone(plan)

    assert "chapter/l: its title reads back otherwise" in issues
    assert "chapter/result: its dependencies read back otherwise" in issues
    assert issues[-1] == "nothing moved"
    assert _bytes(blueprint) == before


#: Another article, which OTHER_ROW annotates.
ANOTHER = {
    "roadmap/chapter/README.md": VAULT["roadmap/chapter/README.md"] + "- [Other](other.md)\n",
    "roadmap/chapter/other.md": "---\narticle_id: af_0123456789abcdef01234567\n---\n\n# Other\n\nAnother result.\n",
}
OTHER_ROW = {
    # The article's facts then come from its annotation, so its article_id is here too.
    "id": "af_0123456789abcdef01234567",
    "subject": [{"type": "generic", "uri": "../../../roadmap/chapter/other.md"}],
    "object": {"type": "generic", "uri": "../../../roadmap/chapter/lemma.md"},
}


#: The same annotation in a file that is not the article's own, as a file annotating several articles is.
SHARED_ROW = {
    **OTHER_ROW,
    "subject": [{"type": "generic", "uri": "../roadmap/chapter/other.md"}],
    "object": {"type": "generic", "uri": "../roadmap/chapter/lemma.md"},
}


#: A row that only cites the lemma, so it holds none of the facts the read-back compares, in a file annotating several
#: articles and in one beside the lemma's own.
CITING = {
    "subject": [{"type": "generic", "uri": "../roadmap/chapter/lemma.md"}],
    "object": {"type": "generic", "uri": "../sources/notes.md"},
}
CITING_BESIDE = {
    "subject": [{"type": "generic", "uri": "../../../roadmap/chapter/lemma.md"}],
    "object": {"type": "generic", "uri": "../../../sources/notes.md"},
}


def test_an_annotation_not_read_once_moved_undoes_the_move(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path, OTHER)
    _migrated(blueprint, "roadmap/chapter/lemma.md")
    before = _bytes(blueprint)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/other/lemma.md")
    # Written as it was, its subject names the old path.
    plan.changes[blueprint / "links/roadmap/other/lemma.jsonl"] = before["links/roadmap/chapter/lemma.jsonl"]

    issues = _undone(plan)

    assert "links/roadmap/other/lemma.jsonl: not read as its article's annotation once moved" in issues
    assert _bytes(blueprint) == before


@pytest.mark.parametrize(
    ("extra", "old", "new", "written", "row"),
    [
        (
            {"links/shared.jsonl": json.dumps(CITING) + "\n", **OTHER},
            "roadmap/chapter/lemma.md",
            "roadmap/other/lemma.md",
            "links/shared.jsonl",
            CITING,
        ),
        (
            {"links/roadmap/chapter/hand.jsonl": json.dumps(CITING_BESIDE) + "\n"},
            "roadmap/chapter/README.md",
            "roadmap/part/README.md",
            "links/roadmap/part/hand.jsonl",
            CITING_BESIDE,
        ),
        (
            {"links/roadmap/chapter/other.jsonl": json.dumps(OTHER_ROW) + "\n", **ANOTHER},
            "roadmap/chapter/lemma.md",
            "roadmap/chapter/l.md",
            "links/roadmap/chapter/other.jsonl",
            {**OTHER_ROW, "subject": [{"type": "generic", "uri": "../../../roadmap/chapter/missing.md"}]},
        ),
    ],
    ids=["shared", "carried", "another-article"],
)
def test_any_annotation_file_the_move_writes_must_still_be_read_as_its_articles(
    tmp_path: Path, extra: dict[str, str], old: str, new: str, written: str, row: dict[str, object]
) -> None:
    # Written as it was, or with another subject, the row names no page of its article once moved; it only cites, or
    # the page repeats its facts, so no fact the read-back compares shows that.
    blueprint = _vault(tmp_path, extra)
    before = _bytes(blueprint)
    plan = _plan(blueprint, old, new)
    plan.changes[blueprint / written] = (json.dumps(row) + "\n").encode()

    assert f"{written}: not read as its article's annotation once moved" in _undone(plan)
    assert _bytes(blueprint) == before


def test_an_interrupted_move_is_undone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blueprint = _vault(tmp_path)
    before = _bytes(blueprint)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")

    def interrupted(plan: move.MovePlan) -> list[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr(move, "_read_back", interrupted)

    with pytest.raises(KeyboardInterrupt) as stopped:
        apply_move(plan)

    assert stopped.value.__notes__ == ["nothing moved"]
    assert _bytes(blueprint) == before


def test_a_page_the_writers_undo_removed_is_left_to_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C inside the write: the writer's own undo removes the moved page, which another run then writes again
    with this move's bytes, and the move's cleanup leaves it: the file may be that run's."""

    blueprint = _vault(tmp_path)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")
    moved = blueprint / "roadmap/chapter/l.md"
    real_write, real_remove = writer.atomic_write, writer._remove_if
    theirs: list[bytes] = []

    def stop_at_the_result(path: Path, *args: object, **kwargs: object) -> None:
        if path.name == "result.md":
            raise KeyboardInterrupt
        real_write(path, *args, **kwargs)

    def another_run_after(path: Path, want: str | None) -> None:
        data = path.read_bytes()
        real_remove(path, want)
        if path == moved and not theirs:
            theirs.append(data)
            path.write_bytes(data)  # another run writes the page this undo has just removed

    monkeypatch.setattr(writer, "atomic_write", stop_at_the_result)
    monkeypatch.setattr(writer, "_remove_if", another_run_after)

    with pytest.raises(KeyboardInterrupt) as stopped:
        apply_move(plan)

    assert stopped.value.__notes__ == ["nothing moved"]
    assert theirs
    assert moved.read_bytes() == theirs[0]
    assert (blueprint / "roadmap/chapter/lemma.md").read_bytes() == VAULT["roadmap/chapter/lemma.md"].encode()


def test_a_file_the_undo_cannot_put_back_keeps_its_new_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The read-back fails, and the lemma's old path cannot take it back, as on a full disk: its new copy stays.
    blueprint = _vault(tmp_path)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")
    lemma, real_write = blueprint / "roadmap/chapter/lemma.md", writer.atomic_write

    def full_disk(path: Path, *args: object, **kwargs: object) -> None:
        if path == lemma:
            raise OSError("No space left on device")
        real_write(path, *args, **kwargs)

    monkeypatch.setattr(move, "_read_back", lambda plan: ["chapter/l: reads back otherwise"])
    monkeypatch.setattr(writer, "atomic_write", full_disk)

    with pytest.raises(MoveError) as undone:
        apply_move(plan)

    # The copy kept holds the article, so it is no file left otherwise than it was.
    assert list(undone.value.issues) == [
        "chapter/l: reads back otherwise",
        "roadmap/chapter/l.md: kept, since roadmap/chapter/lemma.md could not be restored",
        "roadmap/chapter/lemma.md: could not be restored: No space left on device",
    ]
    assert undone.value.left == ("roadmap/chapter/lemma.md",)
    assert (blueprint / "roadmap/chapter/l.md").read_text(encoding="utf-8") == VAULT["roadmap/chapter/lemma.md"]
    assert (blueprint / "roadmap/chapter/result.md").read_text(encoding="utf-8") == VAULT["roadmap/chapter/result.md"]


def test_an_interrupted_move_keeps_a_new_copy_its_old_path_cannot_take_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl-C just after the lemma's old path goes, and that path cannot take it back: the writer's own undo keeps the
    # new copy, and so does the move's, with the mode the lemma had.
    blueprint = _vault(tmp_path)
    lemma, real_write, real_remove = blueprint / "roadmap/chapter/lemma.md", writer.atomic_write, writer._remove_if
    lemma.chmod(0o755)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")

    def stop_after(path: Path, want: str | None) -> None:
        real_remove(path, want)
        raise KeyboardInterrupt

    def full_disk(path: Path, *args: object, **kwargs: object) -> None:
        if path == lemma:
            raise OSError("No space left on device")
        real_write(path, *args, **kwargs)

    monkeypatch.setattr(writer, "_remove_if", stop_after)
    monkeypatch.setattr(writer, "atomic_write", full_disk)

    with pytest.raises(KeyboardInterrupt) as stopped:
        apply_move(plan)

    # What the move says replaces the writer's notes, which named the kept copy as not undone.
    assert stopped.value.__notes__ == [
        "roadmap/chapter/l.md: kept, since roadmap/chapter/lemma.md could not be restored",
        "roadmap/chapter/lemma.md: could not be restored: No space left on device",
        "could not restore roadmap/chapter/lemma.md",
    ]
    assert (blueprint / "roadmap/chapter/l.md").read_text(encoding="utf-8") == VAULT["roadmap/chapter/lemma.md"]
    assert _mode(blueprint / "roadmap/chapter/l.md") == 0o755


@pytest.mark.parametrize("stop", ["refused", "interrupted"])
def test_a_moved_defaults_file_its_old_path_cannot_take_back_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    # A _defaults.json that a moved folder's mirror carries is a file like any other, not a template the writer made,
    # so its new copy stays while its old path cannot take it back.
    blueprint = _vault(tmp_path)
    old = blueprint / "links/roadmap/chapter/_defaults.json"
    old.parent.mkdir(parents=True)
    defaults = writer.dumps({"object": {"type": "generic", "uri": "https://example.org/notes"}})
    old.write_bytes(defaults)
    plan = _plan(blueprint, "roadmap/chapter/README.md", "roadmap/part/README.md")
    real_write, real_remove = writer.atomic_write, writer._remove_if

    def full_disk(path: Path, *args: object, **kwargs: object) -> None:
        if path == old:
            raise OSError("No space left on device")
        real_write(path, *args, **kwargs)

    def stop_after(path: Path, want: str | None) -> None:
        real_remove(path, want)
        if path == old:
            raise KeyboardInterrupt

    monkeypatch.setattr(writer, "atomic_write", full_disk)
    if stop == "interrupted":
        monkeypatch.setattr(writer, "_remove_if", stop_after)
    else:
        monkeypatch.setattr(move, "_read_back", lambda plan: ["part: reads back otherwise"])

    with pytest.raises((MoveError, KeyboardInterrupt)) as stopped:
        apply_move(plan)

    said = stopped.value.issues if isinstance(stopped.value, MoveError) else stopped.value.__notes__
    kept = "links/roadmap/part/_defaults.json: kept, since links/roadmap/chapter/_defaults.json could not be restored"
    assert kept in said
    assert (blueprint / "links/roadmap/part/_defaults.json").read_bytes() == defaults
    assert (blueprint / "roadmap/chapter/README.md").read_text(encoding="utf-8") == VAULT["roadmap/chapter/README.md"]


def test_a_second_interrupt_during_the_undo_still_removes_the_new_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl-C once the move is written, and again as its undo puts the lemma back: the undo goes over every step once
    # more, the lemma's new copy among them.
    blueprint = _vault(tmp_path)
    before = _bytes(blueprint)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")
    lemma, real_write = blueprint / "roadmap/chapter/lemma.md", writer.atomic_write
    stops: list[Path] = []

    def interrupted(plan: move.MovePlan) -> list[str]:
        raise KeyboardInterrupt

    def stop_once(path: Path, *args: object, **kwargs: object) -> None:
        if path == lemma and not stops:
            stops.append(path)
            raise KeyboardInterrupt
        real_write(path, *args, **kwargs)

    monkeypatch.setattr(move, "_read_back", interrupted)
    monkeypatch.setattr(writer, "atomic_write", stop_once)

    with pytest.raises(KeyboardInterrupt) as stopped:
        apply_move(plan)

    assert stops
    assert stopped.value.__notes__ == ["nothing moved"]
    assert _bytes(blueprint) == before


def test_an_interrupted_move_its_own_undo_restores_says_only_that_nothing_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl-C just after the lemma's old path goes, which the writer's own undo finds busy: the move's undo puts it back,
    # so what the writer noted holds no longer.
    blueprint = _vault(tmp_path)
    before = _bytes(blueprint)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")
    lemma, real_write, real_remove = blueprint / "roadmap/chapter/lemma.md", writer.atomic_write, writer._remove_if
    busy: list[Path] = []

    def stop_after(path: Path, want: str | None) -> None:
        real_remove(path, want)
        if path == lemma:
            raise KeyboardInterrupt

    def busy_once(path: Path, *args: object, **kwargs: object) -> None:
        if path == lemma and not busy:
            busy.append(path)
            raise OSError("Resource busy")
        real_write(path, *args, **kwargs)

    monkeypatch.setattr(writer, "_remove_if", stop_after)
    monkeypatch.setattr(writer, "atomic_write", busy_once)

    with pytest.raises(KeyboardInterrupt) as stopped:
        apply_move(plan)

    assert busy
    assert stopped.value.__notes__ == ["nothing moved"]
    assert _bytes(blueprint) == before


def test_a_new_copy_removed_while_its_old_path_stays_empty_is_named_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Were the copy undone before its old path refused the file, neither would hold it, and the error says so.
    blueprint = _vault(tmp_path)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")
    lemma, real_undo = blueprint / "roadmap/chapter/lemma.md", writer.undo

    def copy_first(steps: list[writer.Written], finished: set[writer.Written] | None = None) -> None:
        real_undo([step for step in steps if step.path != lemma], finished)
        stuck = [step for step in steps if step.path == lemma]
        raise writer.AnnotationWriteError([f"{lemma}: not undone: No space left on device"], stuck)

    monkeypatch.setattr(move, "_read_back", lambda plan: ["chapter/l: reads back otherwise"])
    monkeypatch.setattr(writer, "undo", copy_first)

    with pytest.raises(MoveError) as undone:
        apply_move(plan)

    lost = "roadmap/chapter/l.md: removed, though roadmap/chapter/lemma.md could not be restored, so it is lost"
    assert lost in undone.value.issues
    assert undone.value.left == ("roadmap/chapter/lemma.md",)


def test_a_new_copy_the_undo_cannot_remove_is_left_beside_the_file_put_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = _vault(tmp_path)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")
    copy, real_remove = blueprint / "roadmap/chapter/l.md", writer._remove_if

    def busy(path: Path, want: str | None) -> None:
        if path == copy:
            raise OSError("Resource busy")
        real_remove(path, want)

    monkeypatch.setattr(move, "_read_back", lambda plan: ["chapter/l: reads back otherwise"])
    monkeypatch.setattr(writer, "_remove_if", busy)

    with pytest.raises(MoveError) as undone:
        apply_move(plan)

    beside = "roadmap/chapter/l.md: could not be removed, beside roadmap/chapter/lemma.md: Resource busy"
    assert beside in undone.value.issues
    assert undone.value.left == ("roadmap/chapter/l.md",)
    assert (blueprint / "roadmap/chapter/lemma.md").read_text(encoding="utf-8") == VAULT["roadmap/chapter/lemma.md"]


def test_an_interrupt_before_the_old_paths_go_leaves_them_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Old paths go last, so Ctrl-C at the last edit finds the lemma where it was, with nothing to put back.
    blueprint = _vault(tmp_path)
    before = _bytes(blueprint)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")
    lemma, real_write = blueprint / "roadmap/chapter/lemma.md", writer.atomic_write

    def stop_at_the_notes(path: Path, *args: object, **kwargs: object) -> None:
        if path == lemma:
            raise OSError("No space left on device")
        if path.name == "notes.md":
            raise KeyboardInterrupt
        real_write(path, *args, **kwargs)

    monkeypatch.setattr(writer, "atomic_write", stop_at_the_notes)

    with pytest.raises(KeyboardInterrupt) as stopped:
        apply_move(plan)

    assert stopped.value.__notes__ == ["nothing moved"]
    assert _bytes(blueprint) == before


def test_a_stop_while_a_refused_write_is_undone_keeps_modes_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = _vault(tmp_path, {"roadmap/chapter/plot.py": "print('plot')\n"})
    (blueprint / "roadmap/chapter/plot.py").chmod(0o755)
    before = _bytes(blueprint)
    plan = _plan(blueprint, "roadmap/chapter/README.md", "roadmap/part/README.md")
    real_undo, calls = writer.undo, []

    def not_kept(plan: move.MovePlan) -> None:
        raise MoveError(["roadmap/part/plot.py: its mode could not be kept: denied"])

    def stopped_after(steps: list[writer.Written], finished: set[writer.Written] | None = None) -> None:
        calls.append(steps)
        real_undo(steps, finished)
        if len(calls) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(move, "_keep_modes", not_kept)
    monkeypatch.setattr(writer, "undo", stopped_after)

    with pytest.raises(KeyboardInterrupt) as stopped:
        apply_move(plan)

    assert stopped.value.__notes__ == ["nothing moved"]
    assert _bytes(blueprint) == before
    assert _mode(blueprint / "roadmap/chapter/plot.py") == 0o755


WRAPPED_ITEM = "# Chapter\n\n- [The lemma on finite generation of\n  modules](lemma.md)\n- [Result](result.md)\n"
CLIPPED = (
    "# Result\n\nThis follows from [the main\nlemma](lemma.md) and then "
    + " ".join(f"w{n}" for n in range(120))
    + ".\n"
)


@pytest.mark.parametrize(
    ("name", "text"),
    [("roadmap/chapter/README.md", WRAPPED_ITEM), ("roadmap/chapter/result.md", CLIPPED)],
    ids=["part-of-a-link", "clipped"],
)
def test_a_summary_holding_a_destination_reads_back_alike(tmp_path: Path, name: str, text: str) -> None:
    # The summary keeps a wrapped link's destination, or the end of one, and a clip that falls elsewhere once the
    # destination is longer; neither is a difference the move made.
    blueprint = _vault(tmp_path, {name: text, **OTHER})

    _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/other/l.md")

    assert "../other/l.md" in (blueprint / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("book", "text"),
    [
        ("Book (2001).md", "# On [the book](<../../sources/Book (2001).md>)\n\nA result.\n"),
        ("Book 2001).md", "# Result\n\nAs [the\nbook](<../../sources/Book 2001).md>) shows.\n"),
    ],
    ids=["title", "summary"],
)
def test_an_angle_destination_holding_a_parenthesis_reads_back_alike(tmp_path: Path, book: str, text: str) -> None:
    # It ends at its '>', not at the first ')' it holds; the summary keeps a link that wraps, as the title keeps any.
    extra = {f"sources/{book}": "# Book\n", "roadmap/chapter/part/README.md": "# Part\n\nA part.\n"}
    blueprint = _vault(tmp_path, {**extra, "roadmap/chapter/result.md": text})

    _move(blueprint, "roadmap/chapter/result.md", "roadmap/chapter/part/result.md")

    assert "chapter/part/result" in load_graph(blueprint).nodes


def test_two_spellings_of_one_source_read_back_as_one(tmp_path: Path) -> None:
    use = "# Use\n\nA use.\n\n## Sources\n\n- [Lemma](lemma.md)\n- [Lemma again](./lemma.md)\n"
    blueprint = _vault(tmp_path, {"roadmap/chapter/use.md": use, **OTHER})

    _move(blueprint, "roadmap/chapter/lemma.md", "roadmap/other/l.md")

    assert load_graph(blueprint).nodes["chapter/use"].sources == ("../other/l.md",)


def test_a_destination_still_naming_its_file_stays_as_written(tmp_path: Path) -> None:
    # In a folder that moves, a link to a file that moves with it names it still, as does an image no reader counts,
    # and a reference in its mirror to a file beside it there.
    lemma = "# Lemma\n\nA lemma, by [the result](./result.md).\n\n![Figure](./figure.png)\n"
    row = {
        "subject": [{"type": "generic", "uri": "../../../roadmap/chapter/lemma.md"}],
        "object": {"type": "generic", "uri": "https://example.org/notes", "location": {"profile": "./profile.txt"}},
    }
    extra = {
        "roadmap/chapter/lemma.md": lemma,
        "roadmap/chapter/figure.png": "picture",
        "links/roadmap/chapter/lemma.jsonl": json.dumps(row) + "\n",
        "links/roadmap/chapter/profile.txt": "a profile\n",
    }
    blueprint = _vault(tmp_path, extra)

    plan = _move(blueprint, "roadmap/chapter/README.md", "roadmap/part/README.md")

    assert (blueprint / "roadmap/part/lemma.md").read_text(encoding="utf-8") == lemma
    assert not [warning for warning in plan.warnings if "figure.png" in warning]
    moved = json.loads((blueprint / "links/roadmap/part/lemma.jsonl").read_text(encoding="utf-8"))
    assert moved["subject"][0]["uri"] == "../../../roadmap/part/lemma.md"
    assert moved["object"]["location"]["profile"] == "./profile.txt"


def test_a_move_the_graph_refuses_once_written_is_undone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blueprint = _vault(tmp_path)
    before = _bytes(blueprint)
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md")

    def refused(blueprint_dir: Path) -> None:
        raise GraphValidationError(["chapter/l: refused"])

    monkeypatch.setattr(move, "load_graph", refused)

    assert _undone(plan) == ["chapter/l: refused", "nothing moved"]
    assert _bytes(blueprint) == before


def _git(*arguments: str, cwd: Path, stdin: str | None = None) -> str:
    names = {"GIT_AUTHOR_NAME": "test", "GIT_COMMITTER_NAME": "test"}
    emails = {"GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_EMAIL": "test@example.com"}
    environment = {**os.environ, **names, **emails}
    done = subprocess.run(
        ["git", *arguments], cwd=cwd, input=stdin, env=environment, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _board(tmp_path: Path) -> Path:
    board = tmp_path / "board.git"
    _git("init", "--bare", "-q", str(board), cwd=tmp_path)
    return board


def _claim(tmp_path: Path, board: Path, target: str, worker: str) -> None:
    scratch = str(tmp_path / f"{worker}-scratch.git")
    assert main(["claim", "acquire", target, "--repo", str(board), "--worker-id", worker, "--scratch", scratch]) == 0


@pytest.mark.parametrize(
    ("target", "page"),
    [
        ("chapter/result", VAULT["roadmap/chapter/result.md"]),
        (
            "af_0123456789abcdef01234567",
            "---\narticle_id: af_0123456789abcdef01234567\n---\n\n" + VAULT["roadmap/chapter/result.md"],
        ),
        ("chapter/lemma", VAULT["roadmap/chapter/result.md"]),
    ],
    ids=["path-id", "article-id", "moved"],
)
def test_a_live_claim_another_worker_holds_refuses_the_move(tmp_path: Path, target: str, page: str) -> None:
    # The lemma's move rewrites the result, claimed by its path ID or its article ID as `autoform claim` takes it, and
    # moves the lemma itself.
    blueprint = _vault(tmp_path, {"roadmap/chapter/result.md": page})
    board = _board(tmp_path)
    _claim(tmp_path, board, target, "someone")
    before = _bytes(blueprint)

    assert _refused(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md", repo=str(board), worker_id="me") == [
        f"{target}: claimed by someone; move it once the claim is released"
    ]
    assert _bytes(blueprint) == before
    plan = _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md", repo=str(board), worker_id="someone")
    assert plan.claims == "checked"


#: An annotation of the other article's with no object, and the folder default it inherits the lemma as one from.
INHERITING = {
    "links/roadmap/chapter/other.jsonl": json.dumps({"id": OTHER_ROW["id"], "subject": OTHER_ROW["subject"]}) + "\n",
    "links/roadmap/chapter/_defaults.json": json.dumps({"object": OTHER_ROW["object"]}) + "\n",
}


@pytest.mark.parametrize(
    "files",
    [
        {".implementation-notes/af_0123456789abcdef01234567.md": "# Notes\n\nSee [it](../roadmap/chapter/lemma.md).\n"},
        {"links/roadmap/chapter/other.jsonl": json.dumps(OTHER_ROW) + "\n"},
        {"links/shared.jsonl": json.dumps(SHARED_ROW) + "\n"},
        INHERITING,
    ],
    ids=["note", "annotation", "shared", "inherited"],
)
def test_a_claim_on_an_article_whose_files_the_move_rewrites_refuses_it(tmp_path: Path, files: dict[str, str]) -> None:
    # Its page stays as it is; its implementation note, an annotation file of its, or a folder default its annotation
    # inherits names the lemma.
    blueprint = _vault(tmp_path, {**ANOTHER, **files})
    board = _board(tmp_path)
    _claim(tmp_path, board, "af_0123456789abcdef01234567", "someone")

    assert _refused(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md", repo=str(board), worker_id="me") == [
        "af_0123456789abcdef01234567: claimed by someone; move it once the claim is released"
    ]


def test_an_unreadable_claim_refuses_the_move(tmp_path: Path) -> None:
    blueprint = _vault(tmp_path)
    board = _board(tmp_path)
    commit = _git("commit-tree", _git("mktree", cwd=board, stdin=""), "-m", "not json", cwd=board)
    _git("update-ref", CLAIM_REF_PREFIX + author_claim_key("chapter/result"), commit, cwd=board)

    assert _refused(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md", repo=str(board), worker_id="me") == [
        "chapter/result: its claim cannot be read, so it may be held; move it once the claim is released"
    ]


def test_claims_are_read_from_the_checkouts_origin(tmp_path: Path) -> None:
    project = tmp_path / "project"
    blueprint = _vault(project)
    board = _board(tmp_path)
    _git("init", "-q", cwd=project)
    assert _plan(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md", worker_id="me").claims == "not checked"
    # The origin's first URL, as `autoform claim` takes it.
    _git("remote", "add", "origin", "../board.git", cwd=project)
    _git("remote", "set-url", "--add", "origin", str(tmp_path / "mirror.git"), cwd=project)
    _claim(tmp_path, board, "chapter/result", "someone")

    assert _refused(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md", worker_id="me") == [
        "chapter/result: claimed by someone; move it once the claim is released"
    ]

    # An origin that cannot be read is no reason to skip the claims.
    (project / ".git/config").write_text("[broken\n", encoding="utf-8")
    issues = _refused(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md", worker_id="me")
    assert issues[0].startswith("the origin remote could not be read, so claims cannot be checked: "), issues


def test_a_claim_board_that_cannot_be_read_refuses_the_move(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _vault(tmp_path)
    board = str(tmp_path / "no-board.git")

    issues = _refused(blueprint, "roadmap/chapter/lemma.md", "roadmap/chapter/l.md", repo=board, worker_id="me")

    assert issues[0].startswith("the claim board could not be read, so claims cannot be checked: ")
    # A dry run writes nothing, so offline it warns instead.
    old, new = str(blueprint / "roadmap/chapter/lemma.md"), str(blueprint / "roadmap/chapter/l.md")
    assert main(["move", str(blueprint), old, new, "--dry-run", "--repo", board, "--worker-id", "me"]) == 0
    printed = capsys.readouterr()
    assert "warning: the claim board could not be read, so claims were not checked: " in printed.err
    assert printed.out.endswith("claims: not checked\n")


def test_move_on_the_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blueprint = _vault(tmp_path)
    before = _bytes(blueprint)
    old, new = str(blueprint / "roadmap/chapter/lemma.md"), str(blueprint / "roadmap/chapter/l.md")

    assert main(["move", str(blueprint), old, new, "--dry-run", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["schema"] == "autoform-move-plan/v1"
    assert printed["dry_run"] is True
    assert printed["node_ids"] == [{"from": "chapter/lemma", "to": "chapter/l"}]
    assert printed["renamed"] == [{"from": "roadmap/chapter/lemma.md", "to": "roadmap/chapter/l.md"}]
    assert _bytes(blueprint) == before

    assert main(["move", str(blueprint), old, new]) == 0
    assert capsys.readouterr().out.startswith("moved roadmap/chapter/lemma.md to roadmap/chapter/l.md\n")
    assert main(["move", str(blueprint), old, new]) == 2
    assert f"error: {new}: exists\n" in capsys.readouterr().err


def test_an_interrupted_move_on_the_command_line_exits_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _vault(tmp_path)
    before = _bytes(blueprint)

    def interrupted(plan: move.MovePlan) -> list[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr(move, "_read_back", interrupted)

    old, new = str(blueprint / "roadmap/chapter/lemma.md"), str(blueprint / "roadmap/chapter/l.md")
    assert main(["move", str(blueprint), old, new]) == 130
    assert capsys.readouterr().err.endswith("error: nothing moved\n")
    assert _bytes(blueprint) == before


def test_a_move_interrupted_removing_the_folders_it_emptied_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = _vault(tmp_path)

    def interrupted(plan: move.MovePlan) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(move, "_remove_emptied", interrupted)

    old, new = str(blueprint / "roadmap/chapter/lemma.md"), str(blueprint / "roadmap/chapter/l.md")
    assert main(["move", str(blueprint), old, new]) == 130
    printed = capsys.readouterr()
    assert printed.out.startswith("moved roadmap/chapter/lemma.md to roadmap/chapter/l.md\n")
    assert printed.err.endswith("error: interrupted while removing the folders the move emptied; some may remain\n")
    assert (blueprint / "roadmap/chapter/l.md").is_file()
