"""Pinned name lists: what a list holds, what check warns about, and that the site never publishes a list."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autoform_cli.__main__ import main
from autoform_cli.graph import load_graph, pinned_list_omissions
from autoform_cli.library import pinned_names
from autoform_cli.library.pinned_names import load_pinned_lists
from tests.pinned_names_fixture import MATHLIB, PACKAGES, dump, header, name, write_pinned_lists

CORE = PACKAGES["core"]


def _blueprint(tmp_path: Path, articles: dict[str, str] | None = None) -> Path:
    """A blueprint that check, render and audit accept, with an article per front matter given."""

    blueprint = tmp_path / "blueprint"
    (blueprint / "roadmap").mkdir(parents=True)
    (blueprint / "coverage").mkdir()
    (blueprint / "coverage" / "README.md").write_text(
        "# Coverage\n\n| Area | Coverage | Evidence |\n| --- | --- | --- |\n| Narrative | OUT | No target |\n",
        encoding="utf-8",
    )
    for stem, frontmatter in (articles or {"result": "declaration: theorem"}).items():
        (blueprint / "roadmap" / f"{stem}.md").write_text(
            f"---\n{frontmatter}\n---\n\n# {stem}\n\nA precise statement.\n\n## Depends on\n\nNo prerequisites.\n",
            encoding="utf-8",
        )
    return blueprint


@pytest.mark.parametrize(
    ("name", "found", "lists"),
    [
        ("Nat.add_comm", "Nat.add_comm", ("mathlib",)),
        # A Lean name, however it is quoted or rooted.
        ("_root_.«Nat».add_comm", "Nat.add_comm", ("mathlib",)),
        ("A.«two words»", "A.«two words»", ("mathlib",)),
        ("Theorems100.«82».cannot_cube_a_cube", "Theorems100.«82».cannot_cube_a_cube", ("mathlib",)),
        ("εNFA", "εNFA", ("mathlib",)),
        ("pow_card_eq_one'", "pow_card_eq_one'", ("mathlib",)),
        # A to_additive twin is a name of its own.
        ("Finset.prod_range_succ", "Finset.prod_range_succ", ("mathlib",)),
        # Two lists that pin one repository at one revision agree; the first list's record stands.
        ("Agree.decl", "Agree.decl", ("mathlib", "other")),
        ("Twice.decl", None, ("mathlib", "other")),
        ("Missing.decl", None, ()),
        # Not Lean names, though their components spell a listed one.
        *(
            pytest.param(malformed, None, (), id=repr(malformed))
            for malformed in ("Nat..add_comm", "Nat.add_comm.", "«Nat.add_comm", ".Nat.add_comm", "«Nat\x07».add_comm")
        ),
    ],
)
def test_lookup_reads_a_name_as_lean_does(tmp_path: Path, name: str, found: str | None, lists: tuple[str, ...]) -> None:
    write_pinned_lists(tmp_path)
    lookup = load_pinned_lists(tmp_path).lookup(name)

    assert (lookup.record and lookup.record.name, lookup.lists) == (found, lists)
    assert lookup.record is None or lookup.record.list == "mathlib"


def test_a_record_carries_its_package_and_deprecation(tmp_path: Path) -> None:
    write_pinned_lists(tmp_path)
    lists = load_pinned_lists(tmp_path)
    nat, deprecated, unlined = (lists.lookup(name).record for name in ("Nat", "Finset.card_le_of_subset", "instAddNat"))

    assert lists.problems == ()
    assert (nat.package.key, nat.package.source_root, nat.package.revision, nat.module, nat.kind, nat.line) == (
        "core",
        "src/",
        CORE["revision"],
        "Init.Prelude",
        "inductive",
        1349,
    )
    assert (deprecated.deprecated, deprecated.replacement) == (True, "Finset.card_le_card")
    assert (unlined.line, unlined.deprecated, unlined.replacement) == (None, False, None)
    assert lists.lookup("Elsewhere.decl").record.package.docs == "https://docs.example.org/elsewhere/"
    assert lists.lookup("Undocumented.decl").record.package.docs is None


def test_lists_are_ordered_by_stem(tmp_path: Path) -> None:
    folder = tmp_path / "libraries"
    folder.mkdir()
    agreeing = dump([header({"mathlib": MATHLIB}), name("Shared.decl", "mathlib", "Mathlib.Shared")])
    # File-name order would put a-b.jsonl first, since "-" sorts before ".".
    for stem in ("a", "a-b"):
        (folder / f"{stem}.jsonl").write_text(agreeing, encoding="utf-8")
    lookup = load_pinned_lists(tmp_path).lookup("Shared.decl")

    assert (lookup.record.list, lookup.lists) == ("a", ("a", "a-b"))


def _list(*records: dict[str, object], packages: dict[str, dict[str, object]] = PACKAGES) -> bytes:
    return dump([header(packages), *records]).encode("utf-8")


_HEADER = dump([header(PACKAGES)]).encode("utf-8")
_NAT = name("Nat.add_comm", "core", "Init.Data.Nat.Basic")
_GOOD = name("Good.decl", "core", "Init.Good")


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        pytest.param(b"\xff\n", "it is not valid UTF-8", id="not-utf-8"),
        pytest.param(b"\xef\xbb\xbf" + _HEADER, "it starts with a byte-order mark", id="byte-order-mark"),
        pytest.param(b"", "it has no header", id="empty"),
        pytest.param(_HEADER[:-1], "its header does not end with a newline", id="header-cut-short"),
        pytest.param(b"\n" + _HEADER, "line 1 is an empty line", id="empty-first-line"),
        pytest.param(b"{\n", "line 1 is not JSON", id="not-json"),
        pytest.param(b'{"line": NaN}\n', "line 1 is not JSON", id="nan"),
        pytest.param(b'{"record": "header", "record": "header"}\n', "line 1 repeats the key record", id="repeated-key"),
        pytest.param(b"[]\n", "line 1 is not a JSON object", id="not-an-object"),
        pytest.param(dump([_NAT]).encode("utf-8"), "it has no header on its first line", id="no-header"),
        pytest.param(
            dump([{**header(PACKAGES), "schema": "autoform-pinned-names/v1"}]).encode("utf-8"),
            "unknown schema autoform-pinned-names/v1",
            id="later-schema",
        ),
        pytest.param(
            dump([{**header(PACKAGES), "extra": 1}]).encode("utf-8"), "header: unexpected key extra", id="header-key"
        ),
        pytest.param(
            dump([{**header(PACKAGES), "coverage": "complete"}]).encode("utf-8"),
            "header: coverage is not a value this schema allows",
            id="reserved-coverage",
        ),
        pytest.param(_list(packages={}), "header: packages is not a value this schema allows", id="no-packages"),
        pytest.param(
            _list(packages={"mathlib": []}), "package mathlib is not a JSON object", id="package-not-an-object"
        ),
        *(
            pytest.param(
                _list(packages={"mathlib": {**MATHLIB, key: value}}),
                f"package mathlib: {key} is not a value this schema allows",
                id=f"{key}-{value}",
            )
            for key, value in (
                ("repository", "https://github.com/leanprover-community/mathlib4.git"),
                ("repository", "https://github.com/leanprover-community/mathlib4/"),
                ("repository", "http://github.com/leanprover-community/mathlib4"),
                # The address reads as github.com, but its host is another.
                ("repository", "https://github.com@example.org/leanprover-community/mathlib4"),
                ("repository", "https://github.com/leanprover-community/mathlib4?"),
                ("repository", "https://github.com/leanprover-community/mathlib4#"),
                ("repository", "https://github.com"),
                ("revision", "v4.36.0"),
                ("source_root", "src"),
                ("source_root", "../src/"),
                ("docs", "https://leanprover-community.github.io/mathlib4_docs"),
                ("docs", "http://leanprover-community.github.io/mathlib4_docs/"),
            )
        ),
    ],
)
def test_a_list_whose_header_breaks_a_rule_is_left_out_whole(tmp_path: Path, data: bytes, reason: str) -> None:
    (write_pinned_lists(tmp_path) / "bad.jsonl").write_bytes(data)
    lists = load_pinned_lists(tmp_path)

    assert lists.problems == (("libraries/bad.jsonl", f"ignored: {reason}"),)
    assert lists.lookup("Nat.add_comm").lists == ("mathlib",)


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        pytest.param(_HEADER + b"\n" + _list(_GOOD)[len(_HEADER) :], "line 2 is an empty line", id="empty-line"),
        pytest.param(_HEADER + b"{\n" + _list(_GOOD)[len(_HEADER) :], "line 2 is not JSON", id="not-json"),
        *(
            pytest.param(_list({**_NAT, **change}, _GOOD), f"line 2: {key} is not a value this schema allows", id=key)
            for key, change in (
                ("name", {"name": "Nat..add_comm"}),
                ("package", {"package": "nowhere"}),
                ("module", {"module": "Init..Nat"}),
                ("kind", {"kind": "lemma"}),
                ("line", {"line": 0}),
                ("signature", {"signature": 1}),
                ("deprecated", {"deprecated": {"replacement": "Nat.add_comm'", "since": "2024-01-01"}}),
            )
        ),
        pytest.param(
            _list({**_NAT, "deprecated": "x"}, _GOOD),
            "line 2: deprecated is not a value this schema allows",
            id="deprecated-not-an-object",
        ),
        pytest.param(_list({**_NAT, "extra": 1}, _GOOD), "line 2: unexpected key extra", id="name-key"),
        pytest.param(
            _list(_GOOD, {**_GOOD, "name": "«Good».decl"}),
            "line 3 names «Good».decl again, after line 2",
            id="repeated-name",
        ),
        pytest.param(_list({**_NAT, "record": "module"}, _GOOD), "line 2 is not a name record", id="other-record"),
        pytest.param(
            _list({**_NAT, "signature": "x" * (1024 * 1024)}, _GOOD), "line 2 is longer than 1024 KiB", id="long-line"
        ),
        pytest.param(
            _list(_GOOD) + dump([_NAT]).encode("utf-8")[:-1],
            "line 3 does not end with a newline, so it may be cut short",
            id="last-line-cut-short",
        ),
    ],
)
def test_a_bad_record_is_skipped_and_the_rest_of_its_list_loads(tmp_path: Path, data: bytes, reason: str) -> None:
    (write_pinned_lists(tmp_path) / "bad.jsonl").write_bytes(data)
    lists = load_pinned_lists(tmp_path)

    assert lists.problems == (("libraries/bad.jsonl", f"skipped a bad record: {reason}"),)
    assert lists.lookup("Good.decl").lists == ("bad",)


def test_the_problem_counts_the_bad_records_and_names_the_first(tmp_path: Path) -> None:
    (write_pinned_lists(tmp_path) / "bad.jsonl").write_bytes(
        _list(_GOOD, {**_NAT, "kind": "lemma"}, {**_NAT, "line": 0})
    )

    assert load_pinned_lists(tmp_path).problems == (
        ("libraries/bad.jsonl", "skipped 2 bad records; the first: line 3: kind is not a value this schema allows"),
    )


def test_a_list_over_the_size_cap_is_left_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pinned_names, "MAX_FILE_BYTES", 1024 * 1024)
    (write_pinned_lists(tmp_path) / "large.jsonl").write_bytes(
        _list({**_NAT, "signature": "x" * 1_000_000}, {**_NAT, "name": "Nat.succ", "signature": "x" * 100_000})
    )

    assert load_pinned_lists(tmp_path).problems == (("libraries/large.jsonl", "ignored: it is larger than 1 MiB"),)


def test_only_the_folder_s_own_lists_are_read(tmp_path: Path) -> None:
    folder = write_pinned_lists(tmp_path)
    # As render does, the loader skips hidden entries, and it reads only *.jsonl files directly in the folder.
    for skipped in (".draft.jsonl", ".jsonl", "notes.md", "nested/inner.jsonl"):
        (folder / skipped).parent.mkdir(exist_ok=True)
        (folder / skipped).write_bytes(b"\xff\n")
    (folder / "folder.jsonl").mkdir()
    (folder / "link.jsonl").symlink_to(folder / "mathlib.jsonl")

    assert load_pinned_lists(tmp_path).problems == (
        ("libraries/folder.jsonl", "ignored: it is not a regular file"),
        ("libraries/link.jsonl", "ignored: it is not a regular file"),
    )


def test_the_folder_must_be_named_exactly(tmp_path: Path) -> None:
    assert load_pinned_lists(tmp_path) is None
    (tmp_path / "libraries").write_text("not a folder\n", encoding="utf-8")
    assert load_pinned_lists(tmp_path).problems == (("libraries", "ignored: it is not a folder"),)
    (tmp_path / "libraries").unlink()
    # A case-insensitive filesystem would open this as libraries/, which render never sees.
    write_pinned_lists(tmp_path).rename(tmp_path / "Libraries")
    assert load_pinned_lists(tmp_path) is None


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="permissions do not bind root")
def test_a_list_or_folder_that_cannot_be_read_is_named(tmp_path: Path) -> None:
    folder = write_pinned_lists(tmp_path)
    (folder / "other.jsonl").chmod(0)
    try:
        assert load_pinned_lists(tmp_path).problems == (
            ("libraries/other.jsonl", "ignored: it cannot be read: Permission denied"),
        )
        folder.chmod(0)
        assert load_pinned_lists(tmp_path).problems == (("libraries", "ignored: it cannot be read: Permission denied"),)
    finally:
        folder.chmod(0o755)
        (folder / "other.jsonl").chmod(0o644)


@pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot hold a newline")
def test_a_list_cannot_forge_a_report_line(tmp_path: Path) -> None:
    folder = write_pinned_lists(tmp_path)
    forged = "\nerror: r: forged"
    (folder / f"bad{forged}.jsonl").write_bytes(dump([{**header(PACKAGES), "schema": forged}]).encode("utf-8"))
    disputed = {"mathlib": {**MATHLIB, "revision": "f" * 40}}
    (folder / f"z{forged}.jsonl").write_text(
        dump([header(disputed), name("Subgroup.card_mul_index", "mathlib", "Mathlib.GroupTheory.Index")]),
        encoding="utf-8",
    )
    lists = load_pinned_lists(tmp_path)
    [warning] = pinned_names.name_warnings(lists, "r", "Subgroup.card_mul_index", None)

    assert lists.problems == (
        ("libraries/bad\\nerror: r: forged.jsonl", "ignored: unknown schema \\nerror: r: forged"),
    )
    assert warning == (
        "r: mathlib_declaration Subgroup.card_mul_index is ambiguous: libraries/mathlib.jsonl and "
        "libraries/z\\nerror: r: forged.jsonl pin different sources"
    )


def test_check_warns_about_the_mathlib_names_the_lists_do_not_vouch_for(tmp_path: Path) -> None:
    blueprint = _blueprint(
        tmp_path,
        {
            "core-at-its-module-path": "declaration: inductive\nmathlib_declaration: Nat\nmathlib_file: Init/Prelude.lean",
            "core-at-its-repository-path": "declaration: inductive\nmathlib_declaration: Nat\n"
            "mathlib_file: src/Init/Prelude.lean",
            "deprecated": "declaration: theorem\nmathlib_declaration: Finset.card_le_of_subset Finset.card_pos'",
            # A disputed first name has no one file to compare with.
            "disagreed": "declaration: definition\nmathlib_declaration: Twice.decl\nmathlib_file: Mathlib/Other.lean",
            "known": "declaration: theorem\nmathlib_declaration: Finset.sum_range_succ\n"
            "mathlib_file: Mathlib/Algebra/BigOperators/Basic.lean",
            "known-with-backslashes": "declaration: theorem\nmathlib_declaration: Finset.sum_range_succ\n"
            "mathlib_file: Mathlib\\Algebra\\BigOperators\\Basic.lean",
            # The file is compared with the first name only, the one it is written for.
            "second-file": "declaration: theorem\n"
            "mathlib_declaration: Subgroup.card_mul_index, Subgroup.card_subgroup_dvd_card\n"
            "mathlib_file: Mathlib/Algebra/Group/Subgroup/Finite.lean",
            "unknown": "declaration: theorem\n"
            "mathlib_declaration: Missing.decl Nat.add_comm Nat..add_comm Nat.add_comm. Missing.decl\n"
            "mathlib_file: Mathlib/Missing.lean",
        },
    )
    write_pinned_lists(blueprint)

    assert load_graph(blueprint).warnings == (
        "deprecated: mathlib_declaration Finset.card_le_of_subset is deprecated at its pinned revision; "
        "use Finset.card_le_card",
        "deprecated: mathlib_declaration Finset.card_pos' is deprecated at its pinned revision, with no replacement "
        "named",
        "disagreed: mathlib_declaration Twice.decl is ambiguous: libraries/mathlib.jsonl and libraries/other.jsonl "
        "pin different sources",
        "second-file: mathlib_file is 'Mathlib/Algebra/Group/Subgroup/Finite.lean', but Subgroup.card_mul_index is "
        "declared in Mathlib/GroupTheory/Index.lean",
        "unknown: mathlib_declaration Missing.decl was not found in any pinned name list",
        "unknown: mathlib_declaration Nat..add_comm was not found in any pinned name list",
        "unknown: mathlib_declaration Nat.add_comm. was not found in any pinned name list",
    )


@pytest.mark.parametrize("unusable", [False, True], ids=["no-folder", "no-usable-list"])
def test_without_a_usable_list_no_name_is_warned_about(tmp_path: Path, unusable: bool) -> None:
    blueprint = _blueprint(tmp_path, {"unknown": "declaration: theorem\nmathlib_declaration: Missing.decl"})
    if unusable:
        (blueprint / "libraries").mkdir()
        (blueprint / "libraries" / "broken.jsonl").write_bytes(b"\xff\n")
    graph = load_graph(blueprint)

    assert graph.warnings == ()
    assert pinned_list_omissions(graph) == (
        (("libraries/broken.jsonl", "ignored: it is not valid UTF-8"),) if unusable else ()
    )


def test_check_render_and_audit_name_a_list_left_out_and_publish_none(tmp_path: Path, capsys) -> None:
    blueprint = _blueprint(tmp_path)
    (write_pinned_lists(blueprint) / "broken.jsonl").write_bytes(b"\xff\n")
    warning = "libraries/broken.jsonl: ignored: it is not valid UTF-8"

    assert main(["check", str(blueprint)]) == 0
    checked = capsys.readouterr().out
    assert f"warning: {warning}" in checked.splitlines()
    # A list is no file the site leaves out: the site never had a place for it.
    assert ".jsonl" not in checked.replace(warning, "")
    assert main(["render", str(blueprint), "--output", str(tmp_path / "site")]) == 0
    rendered = capsys.readouterr().out
    assert f"warning: {warning}" in rendered.splitlines()
    assert ".jsonl" not in rendered.replace(warning, "")
    assert not list((tmp_path / "site").rglob("*.jsonl"))
    assert main(["audit", str(blueprint)]) == 0
    assert (
        "warning: libraries/broken.jsonl: invalid-pinned-list: ignored: it is not valid UTF-8"
        in capsys.readouterr().out.splitlines()
    )
