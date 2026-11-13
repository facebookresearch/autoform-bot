from __future__ import annotations

import os
import shutil
import threading
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path

import pytest

from autoform_cli.library.index import INDEX_FILE
from autoform_cli.library.locate import LibraryError, read_workspace
from autoform_cli.library.verify import Difference, _index_bytes, load_library
from autoform_cli.project import _snapshot
from tests.library_fixture import GOLDEN, MATHLIB_REV, REV, build_index, entry, make_library, manifest, write_index


_STALE = (
    "its index is stale: the library's pin files (lean-toolchain, lakefile, lake-manifest.json) changed "
    "since the index was generated, or the index was merged or edited"
)


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


def test_the_golden_index_is_loaded_as_published(tmp_path: Path) -> None:
    # The consumer accepts the checkout whose index test_library_index pins byte for byte.
    library = make_library(tmp_path, name="gold", sources={}, declarations=())
    shutil.copytree(GOLDEN, library.root, dirs_exist_ok=True)

    verified = _load(library, "gold")

    assert (verified.name, verified.revision) == ("gold", REV)
    assert verified.differences == (Difference("dependency", "batteries", "3" * 40, None),)


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


def test_the_sources_checked_are_the_files_lean_reads_on_every_python(tmp_path: Path) -> None:
    # Lean reads `..lean` as the module `«.»`, and `.lean` as no source. Python 3.14's PurePath finds no suffix in
    # `..lean`, which left it unchecked.
    library = make_library(tmp_path)
    (library.root / "Lib/..lean").write_text("theorem dots : True := trivial\n", encoding="utf-8")
    (library.root / "Lib/.lean").write_text("-- not a source\n", encoding="utf-8")
    assert _refusal(library) == "1 Lean file is not in its index: Lib/..lean"


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

    assert _refusal(library) == _STALE


def test_the_index_is_read_no_further_than_one_byte_past_the_limit(tmp_path: Path, monkeypatch) -> None:
    # One byte over is enough for the parser to refuse by size; reading further only costs memory.
    path = tmp_path / INDEX_FILE
    path.write_bytes(b"x" * 1000)
    monkeypatch.setattr("autoform_cli.library.verify.MAX_INDEX_BYTES", 100)
    assert len(_index_bytes(path)) == 101


def test_an_index_older_than_its_lakefile_is_refused_as_stale(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    with (library.root / "lakefile.toml").open("a", encoding="utf-8") as lakefile:
        lakefile.write("# a comment\n")

    assert _refusal(library) == _STALE


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


def test_loading_starts_no_process_and_writes_nothing(
    tmp_path: Path, no_side_effects: AbstractContextManager[None]
) -> None:
    library = make_library(tmp_path)

    with no_side_effects:
        _load(library)


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


def _refusal_without_blocking(library, name: str = "atlas") -> str:
    """The refusal, or a failure when reading blocks (a named pipe waits for a writer)."""

    outcome: list[object] = []

    def run() -> None:
        try:
            _load(library, name)
            outcome.append(None)
        except LibraryError as error:
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive(), "reading blocked on a file that is not a regular file"
    assert isinstance(outcome[0], LibraryError)
    return outcome[0].reason


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_as_the_index_is_refused_without_blocking(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    (library.root / INDEX_FILE).unlink()
    os.mkfifo(library.root / INDEX_FILE)

    assert _refusal_without_blocking(library) == "its index is not a regular file"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
@pytest.mark.parametrize("name", ["HEAD", "packed-refs", "commondir", "config", "refs/heads/main"])
def test_a_named_pipe_in_git_metadata_is_refused_by_name_without_blocking(tmp_path: Path, name: str) -> None:
    library = make_library(tmp_path)
    git = library.checkout / ".git"
    if name in ("refs/heads/main", "packed-refs"):
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    path = git / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    os.mkfifo(path)

    assert _refusal_without_blocking(library) == f"its Git metadata file {path.name} is not a regular file"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_as_the_git_file_is_refused_by_name_without_blocking(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    shutil.rmtree(library.checkout / ".git")
    os.mkfifo(library.checkout / ".git")

    assert _refusal_without_blocking(library) == "its Git metadata file .git is not a regular file"


@pytest.mark.parametrize("name", ["lean-toolchain", "lake-manifest.json", "lakefile.lean", "lakefile.toml"])
def test_a_pin_file_that_is_a_symbolic_link_is_refused_by_name(tmp_path: Path, name: str) -> None:
    library = make_library(tmp_path)
    target = tmp_path / "elsewhere"
    path = library.root / name
    target.write_bytes(path.read_bytes() if path.exists() else b"-- not a package file\n")
    path.unlink(missing_ok=True)
    path.symlink_to(target)

    assert _refusal(library) == f"its {name} is a symbolic link"


@pytest.mark.parametrize("name", ["lean-toolchain", "lake-manifest.json", "lakefile.lean", "lakefile.toml"])
def test_a_pin_file_that_is_a_directory_is_refused_by_name(tmp_path: Path, name: str) -> None:
    library = make_library(tmp_path)
    path = library.root / name
    path.unlink(missing_ok=True)
    path.mkdir()

    assert _refusal(library) == f"its {name} is not a regular file"


def test_two_oversized_lakefiles_are_refused_and_not_given_one_tree_digest(tmp_path: Path) -> None:
    reasons = []
    for index, filler in enumerate(("a", "b")):
        library = make_library(tmp_path / str(index))
        (library.root / "lakefile.lean").write_text(filler * (2 * 1024 * 1024), encoding="utf-8")
        reasons.append(_refusal(library))

    assert reasons == ["its lakefile.lean cannot be read (it is over 1 MiB or unreadable)"] * 2


def test_pin_files_are_read_until_two_reads_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library = make_library(tmp_path)
    lakefile = library.root / "lakefile.toml"
    whole = lakefile.read_bytes()
    capture = _snapshot.capture_decision_snapshot
    reads = 0
    torn = {1}

    def read_while_writing(root: Path):
        nonlocal reads
        if Path(root).resolve() != library.root.resolve():
            return capture(root)
        reads += 1
        if reads not in torn:
            return capture(root)
        lakefile.write_bytes(whole[: len(whole) // 2])
        try:
            return capture(root)
        finally:
            lakefile.write_bytes(whole)

    monkeypatch.setattr(_snapshot, "capture_decision_snapshot", read_while_writing)
    # The first read catches a write halfway; the next two agree, so the index matches.
    assert _load(library).revision == REV

    reads, torn = 0, {1, 3, 5}
    assert _refusal(library) == "its pin files changed while they were being read; retry after the checkout is idle"


def test_a_missing_pin_file_is_still_only_absent(tmp_path: Path) -> None:
    library = make_library(tmp_path)

    assert not (library.root / "lakefile.lean").exists()
    assert _load(library).revision == REV


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
