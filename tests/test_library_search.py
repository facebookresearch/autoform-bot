from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from autoform_cli import __main__ as cli
from autoform_cli.library.index import INDEX_SCHEMA
from autoform_cli.library.locate import LibraryError
from autoform_cli.library.search import (
    LIBRARY_FIELDS,
    SEARCH_LIBRARY_SCHEMA,
    library_argument,
    search_with_libraries,
)
from autoform_cli.search import SEARCH_SCHEMA, SearchError, search_blueprint
from tests.library_fixture import MATHLIB_REV, REV, declaration, entry, make_library, manifest
from tests.test_search import _project

_DOCS = {"Lib.Convex": "Hahn-Banach separation in locally convex spaces."}


def _library(tmp_path: Path, *declarations, **options):
    # The blueprint and the Lake project share one root, as in a real consumer.
    _project(tmp_path)
    return make_library(tmp_path, declarations=declarations, module_docs=_DOCS, **options)


def _search(library, query: str, *, limit: int = 20, name: str = "atlas", index_path: Path | None = None):
    result = search_with_libraries(
        library.project, query, lean_root=library.project, libraries=[(name, index_path)], limit=limit
    )
    return result.libraries[0]


def _names(library, query: str) -> list[str]:
    return [hit.declaration.name for hit in _search(library, query).hits]


def _matched(library, query: str) -> dict[str, tuple[str, ...]]:
    return {hit.declaration.name: hit.matched_fields for hit in _search(library, query).hits}


def test_a_declaration_is_found_by_its_name_docstring_module_header_or_mentions(tmp_path: Path) -> None:
    library = _library(
        tmp_path,
        declaration(
            "Lib.Convex.choquet_representation",
            docstring="Every point is a barycentre.",
            mentions=("Convex", "MeasureTheory.Measure"),
        ),
    )

    assert _matched(library, "choquet") == {"Lib.Convex.choquet_representation": ("lean",)}
    assert _matched(library, "barycentre") == {"Lib.Convex.choquet_representation": ("docstring",)}
    assert _matched(library, "banach") == {"Lib.Convex.choquet_representation": ("module_doc",)}
    assert _matched(library, "measuretheory") == {"Lib.Convex.choquet_representation": ("mentions",)}
    assert _matched(library, "Lib.Convex") == {"Lib.Convex.choquet_representation": ("qualified_names",)}
    assert _names(library, "nowhere") == []


def test_every_term_must_occur_and_the_kind_does_not_empty_a_query(tmp_path: Path) -> None:
    library = _library(
        tmp_path,
        declaration(
            "Lib.Convex.choquet_representation", docstring=None, signature="Sentinel", statement="theorem marker"
        ),
    )

    assert _matched(library, "Choquet representation theorem") == {
        "Lib.Convex.choquet_representation": ("lean", "kind")
    }
    assert _names(library, "choquet lemma") == []
    # The signature and the statement are returned and never matched.
    assert _names(library, "sentinel") == []
    assert _names(library, "marker") == []


def test_a_whole_lean_name_matches_as_the_name(tmp_path: Path) -> None:
    library = _library(
        tmp_path,
        declaration("IsCompact.exists_isMinOn", docstring=None),
        declaration("Lib.Convex.cites", docstring="See IsCompact.exists_isMinOn."),
    )

    assert _names(library, "IsCompact.exists_isMinOn") == ["IsCompact.exists_isMinOn", "Lib.Convex.cites"]
    assert _matched(library, "IsCompact.exists_isMinOn")["IsCompact.exists_isMinOn"] == ("lean",)


def test_a_namespace_inside_quoting_marks_matches_as_a_namespace(tmp_path: Path) -> None:
    library = _library(tmp_path, declaration("Lib.«weird name».bar", docstring=None))

    # The space belongs to the quoted component, so `Lib.weird` names no declaration.
    assert _matched(library, "Lib.weird") == {"Lib.«weird name».bar": ("qualified_names",)}
    assert _matched(library, "bar") == {"Lib.«weird name».bar": ("lean",)}


def test_the_exact_name_key_follows_the_field_keys_and_precedes_the_status(tmp_path: Path) -> None:
    library = _library(
        tmp_path,
        declaration("Lib.foo", docstring=None),
        declaration("Lib.foo'", docstring=None, status="wanted"),
        declaration("Lib.bar", docstring="A separation."),
        declaration("Lib.bar'", docstring=None),
    )

    # By status and name alone the complete Lib.foo would come first.
    assert _names(library, "Lib.foo'") == ["Lib.foo'", "Lib.foo"]
    assert _names(library, "foo'") == ["Lib.foo'", "Lib.foo"]
    # A better worst field still comes first: Lib.bar' finds `separation` only in its module header.
    assert _names(library, "Lib.bar' separation") == ["Lib.bar", "Lib.bar'"]


def test_a_filler_word_names_no_declaration(tmp_path: Path) -> None:
    library = _library(
        tmp_path,
        declaration("Lib.FreeGroup.of", docstring="Group units."),
        declaration("Lib.Abelian.thing", docstring="Group units."),
    )

    # `of` is dropped from the terms, so it does not name Lib.FreeGroup.of.
    assert _names(library, "group of units") == ["Lib.Abelian.thing", "Lib.FreeGroup.of"]


def test_hits_sort_by_field_then_typed_form_then_status_then_name(tmp_path: Path) -> None:
    library = _library(
        tmp_path,
        declaration("Lib.Convex.b_separation", status="wanted"),
        declaration("Lib.Convex.c_separation"),
        declaration("Lib.Convex.a_separation"),
        declaration("Lib.Convex.z", docstring="A separation result."),
        declaration("Lib.Convex.separated", docstring=None),
    )

    # The name first, complete before wanted; then the docstring; then the
    # module header, which every declaration of the module shares.
    assert _names(library, "separation") == [
        "Lib.Convex.a_separation",
        "Lib.Convex.c_separation",
        "Lib.Convex.b_separation",
        "Lib.Convex.z",
        "Lib.Convex.separated",
    ]
    # "separating" is cut to a stem that "separation" and "separated" contain; no name holds the word as typed.
    assert _names(library, "separating")[-1] == "Lib.Convex.z"
    assert LIBRARY_FIELDS == ("lean", "docstring", "module_doc", "qualified_names", "mentions", "kind")


def test_a_name_holding_the_word_as_typed_precedes_one_reached_only_by_its_stem(tmp_path: Path) -> None:
    # Both match in the name; name order alone would put the stem-only hit first.
    library = _library(
        tmp_path,
        declaration("Lib.Convex.a_separation", docstring=None),
        declaration("Lib.Convex.b_separated", docstring=None),
    )

    # The query word "separated" is cut to the stem "separat", which both names hold.
    assert _matched(library, "separated") == {
        "Lib.Convex.a_separation": ("lean",),
        "Lib.Convex.b_separated": ("lean",),
    }
    assert _names(library, "separated") == ["Lib.Convex.b_separated", "Lib.Convex.a_separation"]


def test_the_better_worst_field_precedes_when_the_best_fields_tie(tmp_path: Path) -> None:
    # Both hold "separation" in the name; "barycentre" is in the docstring of one and only in the mentions of the
    # other, whose name comes first.
    library = _library(
        tmp_path,
        declaration("Lib.Convex.a_separation", docstring=None, mentions=("Barycentre",)),
        declaration("Lib.Convex.z_separation", docstring="A barycentre."),
    )

    assert _matched(library, "separation barycentre") == {
        "Lib.Convex.a_separation": ("lean", "mentions"),
        "Lib.Convex.z_separation": ("lean", "docstring"),
    }
    assert _names(library, "separation barycentre") == ["Lib.Convex.z_separation", "Lib.Convex.a_separation"]


def test_the_same_name_in_two_modules_is_two_hits(tmp_path: Path) -> None:
    library = _library(
        tmp_path,
        declaration("Lib.wanted_result", module="Lib.Convex", status="wanted", kind="theorem"),
        declaration("Lib.wanted_result", module="Lib.Classic", status="wanted", kind="theorem"),
    )

    assert [(hit.declaration.name, hit.declaration.module) for hit in _search(library, "wanted_result").hits] == [
        ("Lib.wanted_result", "Lib.Classic"),
        ("Lib.wanted_result", "Lib.Convex"),
    ]


def test_a_hit_carries_what_an_agent_needs_to_use_it(tmp_path: Path) -> None:
    library = _library(
        tmp_path,
        declaration("Lib.Convex.separation"),
        declaration("Lib.Classic.classic", module="Lib.Classic", docstring="A classic separation fact."),
        declaration("Lib.Convex.open_separation", status="wanted"),
    )

    hits = {hit.declaration.name: hit.as_dict() for hit in _search(library, "separation").hits}

    assert hits["Lib.Convex.separation"] == {
        "auto_named": False,
        "docstring": "Two disjoint convex sets are separated by a hyperplane.",
        "import": "import Lib.Convex",
        "kind": "theorem",
        "line": 3,
        # One term, so one field: the best one it occurs in.
        "matched_fields": ["lean"],
        "mentions": ["True"],
        "module": "Lib.Convex",
        # The header did not decide this match, so it is not repeated here.
        "module_doc": None,
        "module_system": True,
        "name": "Lib.Convex.separation",
        "signature": "True",
        "source_file": "Lib/Convex.lean",
        "statement": "public theorem separation : True",
        "status": "complete",
    }
    assert hits["Lib.Classic.classic"]["module_system"] is False
    assert hits["Lib.Classic.classic"]["module_doc"] is None
    assert hits["Lib.Classic.classic"]["import"] == "import Lib.Classic"
    assert hits["Lib.Convex.open_separation"]["import"] is None
    by_header = _search(library, "banach").hits[0].as_dict()
    assert by_header["matched_fields"] == ["module_doc"]
    assert by_header["module_doc"] == "Hahn-Banach separation in locally convex spaces."


def test_the_result_is_search_v2_and_its_bytes_are_stable(tmp_path: Path) -> None:
    library = _library(tmp_path, declaration(), project_toolchain="leanprover/lean4:v4.32.2")
    arguments = {"lean_root": library.project, "libraries": [("atlas", None)]}

    result = search_with_libraries(library.project, "separation", **arguments)
    payload = json.loads(result.to_json())

    assert result.to_json() == search_with_libraries(library.project, "separation", **arguments).to_json()
    assert result.to_json() == json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert payload["schema"] == SEARCH_LIBRARY_SCHEMA == "autoform-search/v2"
    blueprint = search_blueprint(library.project, "separation", lean_root=library.project).as_dict()
    assert {key: value for key, value in payload.items() if key not in ("schema", "libraries")} == {
        key: value for key, value in blueprint.items() if key != "schema"
    }
    assert blueprint["schema"] == SEARCH_SCHEMA == "autoform-search/v1"
    (atlas,) = payload["libraries"]
    assert {key: value for key, value in atlas.items() if key != "hits"} == {
        "differences": [
            {
                "library": "leanprover/lean4:v4.34.1",
                "name": None,
                "project": "leanprover/lean4:v4.32.2",
                "what": "lean_toolchain",
            }
        ],
        "generator": "0.9.0",
        "index_schema": INDEX_SCHEMA,
        "name": "atlas",
        "probe": 1,
        "revision": REV,
        "total_matches": 1,
    }
    assert "compatible" not in atlas


def test_the_limit_bounds_each_list_and_the_total_counts_every_match(tmp_path: Path) -> None:
    library = _library(tmp_path, *(declaration(f"Lib.Convex.separation_{number:04d}") for number in range(3000)))

    result = _search(library, "separation", limit=5)

    assert result.total_matches == 3000
    assert [hit.declaration.name for hit in result.hits] == [f"Lib.Convex.separation_{n:04d}" for n in range(5)]


def test_an_index_with_no_declarations_or_no_match_is_an_empty_answer(tmp_path: Path) -> None:
    empty = _library(tmp_path)

    result = _search(empty, "separation")

    assert (result.total_matches, result.hits) == (0, ())


def test_one_refused_library_refuses_the_whole_call(tmp_path: Path) -> None:
    library = _library(tmp_path, declaration())
    (library.root / "Lib/Convex.lean").write_text("theorem changed : True := trivial\n", encoding="utf-8")

    with pytest.raises(LibraryError, match="library atlas: 1 source file differs from its index"):
        search_with_libraries(library.project, "separation", lean_root=library.project, libraries=[("atlas", None)])
    # The blueprint alone is still searchable.
    assert search_blueprint(library.project, "separating").total_matches == 1


def test_a_library_named_twice_or_without_a_lean_root_is_refused(tmp_path: Path) -> None:
    library = _library(tmp_path, declaration())

    with pytest.raises(SearchError, match="library atlas: given twice"):
        search_with_libraries(
            library.project, "separation", lean_root=library.project, libraries=[("atlas", None), ("atlas", None)]
        )
    with pytest.raises(SearchError, match="library atlas: given twice"):
        search_with_libraries(
            library.project, "separation", lean_root=library.project, libraries=[("atlas", None), ("«atlas»", None)]
        )
    with pytest.raises(SearchError, match="--library needs --lean-root"):
        search_with_libraries(library.project, "separation", lean_root=None, libraries=[("atlas", None)])


def test_searching_starts_no_process_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library = _library(tmp_path, declaration())

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("search must not start a process or open a socket")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    before = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    assert _search(library, "separation").total_matches == 1

    assert {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()} == before


def test_an_index_from_a_path_is_searched(tmp_path: Path) -> None:
    library = _library(tmp_path, declaration())
    published = tmp_path / "asset.jsonl"
    published.write_bytes((library.root / "autoform-library-index.jsonl").read_bytes())
    (library.root / "autoform-library-index.jsonl").unlink()

    assert _search(library, "separation", index_path=published).total_matches == 1


def test_a_library_argument_is_a_name_with_an_optional_index_path() -> None:
    assert library_argument("atlas") == ("atlas", None)
    assert library_argument("atlas=/tmp/index.jsonl") == ("atlas", Path("/tmp/index.jsonl"))
    assert library_argument("atlas=a=b.jsonl") == ("atlas", Path("a=b.jsonl"))
    for malformed in ("", "=index.jsonl", "atlas="):
        with pytest.raises(ValueError):
            library_argument(malformed)


def test_a_library_the_size_of_atlas_is_searched_within_the_bound(tmp_path: Path) -> None:
    # 1,800 modules and 9,000 declarations: an index of about 15 MB, most of it text that is not matched.
    _project(tmp_path)
    sources = {"Lib.lean": "-- root\n"}
    declarations = []
    for module in range(1800):
        sources[f"Lib/M{module:04d}.lean"] = f"theorem t{module} : True := trivial\n" * 20
        for item in range(5):
            declarations.append(
                declaration(
                    f"Lib.M{module:04d}.result_{item}",
                    module=f"Lib.M{module:04d}",
                    docstring="A statement about convex sets and their separation. " * 4,
                    statement="theorem result : " + "(x : Nat) → " * 120 + "True",
                )
            )
    library = make_library(tmp_path, sources=sources, declarations=tuple(declarations))
    assert (library.root / "autoform-library-index.jsonl").stat().st_size > 12 * 1024 * 1024

    started = time.perf_counter()
    result = _search(library, "separation hyperplane", limit=20)
    elapsed = time.perf_counter() - started

    assert result.total_matches == 0
    assert _search(library, "result_3", limit=20).total_matches == 1800
    # Measured at about one second for the library part; the bound leaves room for a loaded CI machine.
    assert elapsed < 15, f"library search took {elapsed:.1f} s"


def _cli(library, *arguments: str) -> list[str]:
    return ["search", str(library.project), *arguments, "--lean-root", str(library.project)]


def test_search_cli_without_library_is_unchanged_by_this_feature(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())

    assert cli.main([*_cli(library, "separating"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema"] == "autoform-search/v1"
    assert "libraries" not in payload


def test_search_cli_without_library_notes_a_package_that_publishes_an_index(tmp_path: Path, capsys) -> None:
    # An agent that searched only the blueprint read "no match" as "new": the
    # note names the library it skipped and how to search it.
    library = _library(tmp_path, declaration())

    assert cli.main(_cli(library, "separating")) == 0
    captured = capsys.readouterr()

    assert "note:" not in captured.out
    assert captured.err == (
        "note: atlas publishes a search index this search did not read; search it too with "
        f"--lean-root {library.project} --library atlas\n"
    )


def test_search_cli_notes_the_library_on_stderr_and_keeps_the_json(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())

    assert cli.main([*_cli(library, "separating"), "--json"]) == 0
    captured = capsys.readouterr()

    assert json.loads(captured.out)["schema"] == "autoform-search/v1"
    assert "--library atlas" in captured.err


def test_search_cli_notes_the_library_for_a_blueprint_target_without_lean_root(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())

    assert cli.main(["search", str(library.project / "blueprint"), "separating"]) == 0

    assert f"--lean-root {library.project} --library atlas" in capsys.readouterr().err


def test_search_cli_has_no_note_when_the_library_is_searched(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())

    assert cli.main([*_cli(library, "separation"), "--library", "atlas"]) == 0

    assert "note:" not in capsys.readouterr().err


def test_search_cli_has_no_note_without_an_indexed_package(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())
    (library.root / "autoform-library-index.jsonl").unlink()

    assert cli.main(_cli(library, "separating")) == 0
    assert "note:" not in capsys.readouterr().err

    _project(tmp_path / "plain")  # a blueprint with no Lake project at all
    assert cli.main(["search", str(tmp_path / "plain" / "project"), "separating"]) == 0
    assert capsys.readouterr().err == ""


def test_search_cli_emits_v2_json_with_a_library(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())

    assert cli.main([*_cli(library, "separation"), "--library", "atlas", "--json"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert out == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    assert payload["schema"] == "autoform-search/v2"
    assert [hit["name"] for hit in payload["libraries"][0]["hits"]] == ["Lib.Convex.separation"]
    assert payload["libraries"][0]["hits"][0]["import"] == "import Lib.Convex"


def test_search_cli_prints_library_hits_after_the_blueprint_hits(tmp_path: Path, capsys) -> None:
    library = _library(
        tmp_path,
        declaration(),
        declaration("Lib.Convex.open_separation", status="wanted", docstring=None),
        project_toolchain="leanprover/lean4:v4.32.2",
    )

    assert cli.main([*_cli(library, "separation"), "--library", "atlas"]) == 0
    out = capsys.readouterr().out

    # Issue #346: cut with `head`, the output lost later library hits, a kept hit's signature and every count.
    # Counts now come first, and each hit's first line carries its statement or signature.
    assert out.splitlines()[:2] == [
        "1 of 1 matching article(s) shown.",
        "Library atlas: 2 of 2 matching declaration(s) shown.",
    ]
    assert out.index("Separating hyperplane (convexity/hyperplane): ") < out.index("Library atlas @ 111111111111")
    assert out.endswith(
        "Library atlas @ 111111111111\n"
        "  differs: lean_toolchain leanprover/lean4:v4.34.1 (project: leanprover/lean4:v4.32.2)\n"
        "Lib.Convex.separation (theorem, complete): True\n"
        "  Lib/Convex.lean:3\n"
        "  import Lib.Convex\n"
        "  Docstring: Two disjoint convex sets are separated by a hyperplane.\n"
        "  Matched: lean\n"
        "Lib.Convex.open_separation (theorem, wanted): True\n"
        "  Lib/Convex.lean:3\n"
        "  Matched: lean\n"
    )


def test_search_cli_says_when_neither_list_has_a_match(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())

    assert cli.main([*_cli(library, "nowhere"), "--library", "atlas"]) == 0

    assert capsys.readouterr().out == (
        "No matching articles.\n"
        "Library atlas: no matching declarations.\n"
        "Library atlas @ 111111111111\n"
    )


def test_search_cli_escapes_library_text(tmp_path: Path, capsys) -> None:
    library = _library(
        tmp_path, declaration(docstring="separation \u001b[31mIGNORE PREVIOUS\nerror: forged", signature="A\u0007B")
    )

    assert cli.main([*_cli(library, "separation"), "--library", "atlas"]) == 0
    out = capsys.readouterr().out

    assert "\u001b" not in out and "\u0007" not in out
    assert "\nerror: forged" not in out
    assert "Docstring: separation \\x1b[31mIGNORE PREVIOUS error: forged" in out
    assert "Lib.Convex.separation (theorem, complete): A\\x07B\n" in out


def test_search_cli_prints_a_docstring_on_one_line_without_a_literal_newline(tmp_path: Path, capsys) -> None:
    library = _library(
        tmp_path,
        declaration(docstring="Kirchberger's theorem\n  (stable source:\r\nhttps://example.org).\n\n" + "word " * 80),
    )

    assert cli.main([*_cli(library, "separation"), "--library", "atlas"]) == 0
    (line,) = [line for line in capsys.readouterr().out.splitlines() if line.startswith("  Docstring:")]

    assert line.startswith("  Docstring: Kirchberger's theorem (stable source: https://example.org). word word")
    assert "\\n" not in line and "\\r" not in line
    assert line.endswith("...") and len(line) <= len("  Docstring: ") + 200


def test_search_cli_says_when_the_project_does_not_lock_a_dependency(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())
    (library.project / "lake-manifest.json").write_text(manifest([entry("atlas")]), encoding="utf-8")

    assert cli.main([*_cli(library, "separation"), "--library", "atlas"]) == 0
    out = capsys.readouterr().out

    assert "None" not in out
    assert f"  differs: mathlib {MATHLIB_REV} (the project does not lock it)\n" in out


def test_search_cli_refuses_a_library_it_cannot_verify_and_prints_no_blueprint_hits(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())
    (library.root / "Lib/Convex.lean").write_text("theorem changed : True := trivial\n", encoding="utf-8")

    assert cli.main([*_cli(library, "separating"), "--library", "atlas", "--json"]) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == "error: library atlas: 1 source file differs from its index: Lib/Convex.lean\n"


def test_search_cli_refuses_library_without_lean_root_or_with_a_malformed_argument(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())

    assert cli.main(["search", str(library.project), "separation", "--library", "atlas"]) == 2
    assert capsys.readouterr().err == (
        "error: --library needs --lean-root, which names the Lake project that locks the library\n"
    )
    with pytest.raises(SystemExit) as stopped:
        cli.main([*_cli(library, "separation"), "--library", "atlas="])
    assert stopped.value.code == 2


def test_search_cli_reports_a_project_without_a_manifest(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())
    (library.project / "lake-manifest.json").unlink()

    assert cli.main([*_cli(library, "separation"), "--library", "atlas"]) == 2
    assert capsys.readouterr().err == "error: the Lean root has no lake-manifest.json; run `lake update` first\n"


def _deny(path: Path):
    if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("chmod 000 does not deny access here")
    path.chmod(0)


def test_search_cli_refuses_a_library_whose_checkout_cannot_be_read(tmp_path: Path, capsys) -> None:
    library = _library(tmp_path, declaration())
    git = library.root / ".git"
    try:
        _deny(git)
        assert cli.main([*_cli(library, "separation"), "--library", "atlas"]) == 2
    finally:
        git.chmod(0o755)
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == "error: library atlas: its checkout cannot be read\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
@pytest.mark.parametrize("where", ["index", "HEAD"])
def test_search_and_list_return_promptly_when_a_library_file_is_a_named_pipe(tmp_path: Path, where: str) -> None:
    library = _library(tmp_path, declaration())
    path = library.root / "autoform-library-index.jsonl" if where == "index" else library.checkout / ".git/HEAD"
    path.unlink()
    os.mkfifo(path)
    root = str(library.project)
    commands = [
        [sys.executable, "-m", "autoform_cli", "search", root, "separation", "--lean-root", root, "--library", "atlas"],
        [sys.executable, "-m", "autoform_cli", "library", "list", root],
    ]

    for command in commands:
        done = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        text = done.stdout + done.stderr
        assert "library atlas" in text.lower() or "atlas" in text
        assert "regular file" in text
        if "search" in command:
            assert done.returncode == 2
