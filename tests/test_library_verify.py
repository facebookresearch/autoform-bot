from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli.library.index import INDEX_FILE
from autoform_cli.library.locate import LibraryError, read_workspace
from autoform_cli.library.verify import Difference, load_library
from tests.library_fixture import MATHLIB_REV, REV, build_index, entry, make_library, manifest, write_index


def _load(library, name: str = "atlas", index_path: Path | None = None):
    return load_library(read_workspace(library.project), name, index_path)


def _refusal(library, name: str = "atlas", index_path: Path | None = None) -> str:
    with pytest.raises(LibraryError) as refusal:
        _load(library, name, index_path)
    assert refusal.value.library == name
    return refusal.value.reason


def test_a_current_index_is_loaded_with_its_revision(tmp_path: Path) -> None:
    library = make_library(tmp_path)

    verified = _load(library)

    assert (verified.name, verified.revision) == ("atlas", REV)
    assert [item.name for item in verified.index.declarations] == ["Lib.Convex.separation"]
    assert verified.differences == ()


def test_a_package_in_a_sub_directory_or_with_a_quoted_name_is_loaded(tmp_path: Path) -> None:
    assert _load(make_library(tmp_path / "a", sub_dir="sub/pkg", packages_dir="deps/pkgs")).revision == REV
    assert _load(make_library(tmp_path / "b", name="«my-pkg»"), "my-pkg").name == "my-pkg"


def test_a_checkout_with_crlf_line_endings_matches_an_index_made_from_lf(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    for path in [
        *library.root.rglob("*.lean"),
        library.root / "lean-toolchain",
        library.root / "lakefile.toml",
        library.root / "lake-manifest.json",
        library.root / INDEX_FILE,
    ]:
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    assert _load(library).revision == REV


def test_a_checkout_at_another_revision_or_without_readable_head_is_refused(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    (library.checkout / ".git/HEAD").write_text("3" * 40 + "\n", encoding="utf-8")
    assert _refusal(library) == "its checkout is at 333333333333, not the locked revision 111111111111"

    (library.checkout / ".git/HEAD").write_text("ref: refs/heads/.invalid\n", encoding="utf-8")
    (library.checkout / ".git/reftable").mkdir()
    assert _refusal(library) == "its HEAD cannot be read without Git (reftable ref storage)"


def test_a_missing_symlinked_or_malformed_index_is_refused(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    index = library.root / INDEX_FILE
    data = index.read_bytes()

    index.unlink()
    assert _refusal(library) == f"it has no index ({INDEX_FILE})"

    (tmp_path / "elsewhere.jsonl").write_bytes(data)
    index.symlink_to(tmp_path / "elsewhere.jsonl")
    assert _refusal(library) == "its index is not a regular file"

    index.unlink()
    index.write_bytes(data.replace(b"autoform-library-index/v0", b"autoform-library-index/v9"))
    assert _refusal(library) == "its index is not usable: unknown index schema autoform-library-index/v9"


def test_sources_that_do_not_match_the_index_are_refused_by_name(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    (library.root / "Lib/Convex.lean").write_text("theorem changed : True := trivial\n", encoding="utf-8")
    assert _refusal(library) == "1 source file differs from its index: Lib/Convex.lean"

    library = make_library(tmp_path / "extra")
    (library.root / "Lib/New.lean").write_text("theorem added : True := trivial\n", encoding="utf-8")
    assert _refusal(library) == "1 Lean file is not in its index: Lib/New.lean"

    library = make_library(tmp_path / "missing")
    (library.root / "Lib/Classic.lean").unlink()
    assert _refusal(library) == "1 indexed source file is missing: Lib/Classic.lean"

    library = make_library(tmp_path / "outside")
    (library.root / "Scratch.lean").write_text("-- not under a source directory\n", encoding="utf-8")
    assert _load(library).revision == REV


def test_a_symbolic_link_under_a_source_directory_is_refused(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    (library.root / "Lib/Link.lean").symlink_to("Convex.lean")
    assert _refusal(library) == "a source directory holds the symbolic link Lib/Link.lean"

    library = make_library(tmp_path / "directory")
    (library.root / "Lib/Linked").symlink_to(tmp_path, target_is_directory=True)
    assert _refusal(library) == "a source directory holds the symbolic link Lib/Linked"


def test_an_index_merged_from_two_current_ones_is_refused(tmp_path: Path) -> None:
    # Every module digest is right, and the tree value is one side's.
    library = make_library(tmp_path)
    stale = library.index.header.tree
    (library.root / "Lib/Classic.lean").write_text("theorem classic : True := by sorry\n", encoding="utf-8")
    current = build_index(library.root, library.index.declarations)
    write_index(library.root, replace(current, header=replace(current.header, tree=stale)))

    assert _refusal(library) == (
        "its index does not match its sources and pins as a whole; it was merged or edited, not generated"
    )


def test_a_header_that_disagrees_with_the_checkout_is_refused(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    header = library.index.header
    write_index(library.root, replace(library.index, header=replace(header, lean_toolchain="leanprover/lean4:v4.30.0")))
    assert _refusal(library) == (
        "its index was generated for leanprover/lean4:v4.30.0, but the checkout pins leanprover/lean4:v4.34.1"
    )

    write_index(library.root, replace(library.index, header=replace(header, dependencies=())))
    assert _refusal(library) == "its index does not list the dependencies its lake-manifest.json locks"


def test_pins_that_differ_from_the_project_are_reported_not_refused(tmp_path: Path) -> None:
    library = make_library(tmp_path, project_toolchain="leanprover/lean4:v4.32.2", project_mathlib="5" * 40)

    assert _load(library).differences == (
        Difference("lean_toolchain", None, "leanprover/lean4:v4.34.1", "leanprover/lean4:v4.32.2"),
        Difference("dependency", "mathlib", MATHLIB_REV, "5" * 40),
    )

    alias = make_library(tmp_path / "alias", project_toolchain="v4.34.1")
    assert _load(alias).differences == ()


def test_an_index_given_by_path_is_checked_against_the_checkout(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    published = tmp_path / "asset.jsonl"
    published.write_bytes((library.root / INDEX_FILE).read_bytes())
    (library.root / INDEX_FILE).unlink()

    assert _load(library, index_path=published).revision == REV

    (library.root / "Lib/Convex.lean").write_text("theorem changed : True := trivial\n", encoding="utf-8")
    assert _refusal(library, index_path=published) == "1 source file differs from its index: Lib/Convex.lean"
    assert _refusal(library, index_path=tmp_path / "absent.jsonl") == "it has no index (absent.jsonl)"


def test_loading_starts_no_process_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library = make_library(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("verification must not start a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    before = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    _load(library)

    assert {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()} == before


def test_an_index_with_no_declarations_is_usable(tmp_path: Path) -> None:
    assert _load(make_library(tmp_path, declarations=())).index.declarations == ()


def test_a_source_directory_that_is_a_symbolic_link_is_refused(tmp_path: Path) -> None:
    library = make_library(tmp_path, sources={"Lib.lean": "-- no modules below\n"}, declarations=())
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Evil.lean").write_text("theorem evil : True := trivial\n", encoding="utf-8")
    (library.root / "Lib").symlink_to(outside, target_is_directory=True)

    assert _refusal(library) == "the source directory Lib is a symbolic link"


def test_a_symbolic_link_above_a_nested_source_directory_is_refused(tmp_path: Path) -> None:
    library = make_library(tmp_path, sources={"src/notes.txt": "no modules\n"})
    write_index(library.root, build_index(library.root, (), source_dirs=("src/Lib",)))
    outside = tmp_path / "outside"
    (outside / "Lib").mkdir(parents=True)
    (outside / "Lib/Evil.lean").write_text("theorem evil : True := trivial\n", encoding="utf-8")
    shutil.rmtree(library.root / "src")
    (library.root / "src").symlink_to(outside, target_is_directory=True)

    assert _refusal(library) == "the source directory src is a symbolic link"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_file_that_is_not_regular_under_a_source_directory_is_refused(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    os.mkfifo(library.root / "Lib/Pipe.lean")

    assert _refusal(library) == "a source directory holds Lib/Pipe.lean, which is not a regular file"


def test_a_long_toolchain_in_the_index_is_cut_in_the_refusal(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    header = replace(library.index.header, lean_toolchain="leanprover/lean4:" + "v" * 5000)
    write_index(library.root, replace(library.index, header=header))

    reason = _refusal(library)

    assert reason.startswith("its index was generated for leanprover/lean4:vvv")
    assert reason.endswith("..., but the checkout pins leanprover/lean4:v4.34.1")
    assert len(reason) < 250


def test_only_the_direct_dependencies_of_the_library_are_listed_in_its_index(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    manifest_path = library.root / "lake-manifest.json"
    packages = [entry("mathlib", MATHLIB_REV), entry("batteries", "4" * 40, inherited=True)]
    manifest_path.write_text(manifest(packages), encoding="utf-8")
    write_index(library.root, build_index(library.root, library.index.declarations, module_docs=None))

    verified = _load(library)

    assert [item.name for item in verified.index.header.dependencies] == ["mathlib"]
