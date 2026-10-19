"""Publication must not assign repeated Lean names by filesystem order."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.lean import Alias, Declaration, index_project, snapshot_project_sources


def _sources(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "keyword", "value"),
    [("CurveDivisor.coeff", "def", "Nat := 0"), ("BorsukUlam.borsuk_ulam", "theorem", "True := trivial")],
)
def test_repeated_public_names_are_not_selected_for_publication(tmp_path: Path, name: str, keyword: str, value: str) -> None:
    _sources(tmp_path, {"A.lean": f"{keyword} {name} : {value}\n", "B.lean": f"{keyword} {name} : {value}\n"})

    for requested in (None, (name,)):
        index = index_project(tmp_path, names=requested)
        assert index.find(name) is None
        assert [item.path.as_posix() for item in index.occurrences(name)] == ["A.lean", "B.lean"]
        assert index.ambiguity(name) == "A.lean:1, B.lean:1"
        assert index.elsewhere[name] == (Path("B.lean"),)
        assert index.declaration_lines == {Path("A.lean"): (1,), Path("B.lean"): (1,)}


@pytest.mark.parametrize(
    "local",
    [
        "@[simp] private theorem Demo.main : True := trivial\n",
        "@[simp]\nprivate\nnoncomputable def Demo.main : Nat := 0\n",
        "theorem_wanted Demo.main : True\n",
        "proof_wanted Demo.main : True\n",
        "def_wanted Demo.main : Nat\n",
        "instance_wanted Demo.main : Inhabited Nat\n",
    ],
)
@pytest.mark.parametrize("public_first", [False, True])
def test_unique_public_declaration_wins_over_private_or_wanted(
    tmp_path: Path, local: str, public_first: bool,
) -> None:
    public_file, local_file = ("A.lean", "B.lean") if public_first else ("B.lean", "A.lean")
    _sources(tmp_path, {public_file: "theorem Demo.main : True := trivial\n", local_file: local})

    for requested in (None, ("Demo.main",)):
        index = index_project(tmp_path, names=requested)
        found = index.find("Demo.main")
        assert found is not None and found.path == Path(public_file)
        assert found.keyword == "theorem" and not found.private
        assert index.ambiguity("Demo.main") == ""
        assert index.elsewhere["Demo.main"] == (Path(local_file),)


def test_only_repeated_private_or_wanted_names_are_ambiguous(tmp_path: Path) -> None:
    _sources(tmp_path, {
        "A.lean": "private theorem Demo.main : True := trivial\n",
        "B.lean": "proof_wanted Demo.main : True\n",
    })
    index = index_project(tmp_path)

    assert index.find("Demo.main") is None
    assert index.occurrences("Demo.main")[0].private is True
    assert index.ambiguity("Demo.main") == "A.lean:1, B.lean:1"


def test_alias_uses_its_earlier_private_occurrence_in_every_filter(tmp_path: Path) -> None:
    _sources(tmp_path, {
        "A.lean": "namespace N\nprivate theorem aux : True := trivial\ntheorem helper : True := trivial\nend N\n",
        "B.lean": "namespace N\nprivate theorem aux : True := trivial\nalias good := aux\nend N\n",
    })
    snapshots = [snapshot_project_sources(tmp_path, names=names) for names in (None, ("N.good",), ("N.good", "N.helper"))]

    assert len({snapshot.revision for snapshot in snapshots}) == 1
    assert all(len(snapshot.source_files) == 2 for snapshot in snapshots)
    for snapshot in snapshots:
        index = snapshot.index
        assert index.find("N.aux") is None
        target = index.reached(index.find("N.good"))
        assert target is not None and target.path == Path("B.lean") and target.line == 2 and target.private
        assert index.declaration_lines == {Path("A.lean"): (2, 3), Path("B.lean"): (2, 3)}


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("universe u\ndef polymorphic.{u} (α : Type u) := α\n", "polymorphic"),
        ("universe u v\ndef polymorphic.{u, v} (α : Type u) (β : Type v) := α\n", "polymorphic"),
        ("universe u\nnamespace N\ndef _root_.poly.{u} (α : Type u) := α\nend N\n", "poly"),
        ("universe u\nnamespace N\npublic def\n    poly.{u} (α : Type u) := α\nend N\n", "N.poly"),
    ],
)
def test_explicit_universes_keep_canonical_names_when_filtered(tmp_path: Path, source: str, name: str) -> None:
    _sources(tmp_path, {"A.lean": source})

    for requested in (None, (name,)):
        index = index_project(tmp_path, names=requested)
        assert index.find(name) is not None
        assert set(index.declarations) == {name}


def test_existing_positional_alias_argument_keeps_its_meaning() -> None:
    alias = Alias(("base",), atomic=True, iff=False)
    declaration = Declaration("old", Path("A.lean"), 2, "alias", False, alias)

    assert declaration.alias is alias
    assert declaration.private is False
