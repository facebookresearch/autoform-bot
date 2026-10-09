from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoform_cli.library.locate import (
    HeadError,
    LibraryError,
    WorkspaceError,
    checkout_paths,
    find_package,
    read_workspace,
    resolve_head,
)
from tests.library_fixture import MATHLIB_REV, REV, checkout, entry, manifest


def _project(tmp_path: Path, packages: list[dict[str, object]], **root: object) -> Path:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n", encoding="utf-8")
    (project / "lake-manifest.json").write_text(manifest(packages, **root), encoding="utf-8")
    return project


def test_a_workspace_lists_its_locked_packages_by_unescaped_name(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        [
            entry("atlas"),
            entry("mathlib", MATHLIB_REV, inherited=True),
            entry("«my-pkg»"),
            entry("local", path="../local"),
        ],
    )

    workspace = read_workspace(project)

    assert workspace.lean_toolchain == "leanprover/lean4:v4.34.1"
    assert workspace.packages_dir == ".lake/packages"
    assert [(package.name, package.type, package.rev, package.inherited) for package in workspace.packages] == [
        ("atlas", "git", REV, False),
        ("local", "path", None, False),
        ("mathlib", "git", MATHLIB_REV, True),
        ("my-pkg", "git", REV, False),
    ]
    assert workspace.overridden == frozenset()


def test_packages_dir_defaults_and_must_stay_inside_the_project(tmp_path: Path) -> None:
    assert read_workspace(_project(tmp_path, [entry("atlas")], packages_dir=None)).packages_dir == ".lake/packages"
    for index, unsafe in enumerate(("/abs/packages", "../packages", ".lake/../../packages")):
        project = _project(tmp_path / str(index), [entry("atlas")], packages_dir=unsafe)
        with pytest.raises(WorkspaceError, match="packagesDir"):
            read_workspace(project)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (None, "has no lake-manifest.json"),
        ("{", "not a Lake manifest"),
        ('{"version": "1.2.0", "packages": [{"name": "atlas"}]}', "not a Lake manifest"),
        ('{"version": 4, "packages": []}', "not a Lake manifest"),
        (manifest([entry("atlas")], lakeDir="build/.lake"), "lakeDir"),
    ],
)
def test_a_manifest_autoform_cannot_use_is_refused(tmp_path: Path, text: str | None, message: str) -> None:
    project = _project(tmp_path, [])
    if text is None:
        (project / "lake-manifest.json").unlink()
    else:
        (project / "lake-manifest.json").write_text(text, encoding="utf-8")

    with pytest.raises(WorkspaceError, match=message):
        read_workspace(project)


def test_a_library_is_found_by_either_spelling_of_its_name(tmp_path: Path) -> None:
    workspace = read_workspace(_project(tmp_path, [entry("atlas"), entry("«my-pkg»")]))

    assert find_package(workspace, "atlas").name == "atlas"
    assert find_package(workspace, "my-pkg").name == "my-pkg"
    assert find_package(workspace, "«my-pkg»").name == "my-pkg"


def test_a_name_that_is_not_locked_lists_the_packages_that_are(tmp_path: Path) -> None:
    workspace = read_workspace(_project(tmp_path, [entry("atlas"), entry("mathlib", MATHLIB_REV)]))

    with pytest.raises(LibraryError) as refusal:
        find_package(workspace, "Atlas")

    assert refusal.value.library == "Atlas"
    assert str(refusal.value) == (
        "library Atlas: it is not a package in lake-manifest.json; the locked packages are: atlas, mathlib"
    )


def test_a_path_dependency_and_an_overridden_package_are_refused(tmp_path: Path) -> None:
    project = _project(tmp_path, [entry("atlas"), entry("local", path="../local")])
    (project / ".lake").mkdir()
    (project / ".lake/package-overrides.json").write_text(
        json.dumps({"schemaVersion": "1.2.0", "packages": [entry("atlas", path="../other")]}), encoding="utf-8"
    )
    workspace = read_workspace(project)

    assert workspace.overridden == frozenset({"atlas"})
    with pytest.raises(LibraryError, match="library local: it is a path dependency, which has no locked revision"):
        find_package(workspace, "local")
    with pytest.raises(LibraryError, match=r"library atlas: \.lake/package-overrides\.json replaces it"):
        find_package(workspace, "atlas")


def test_the_checkout_is_under_packages_dir_and_the_package_root_adds_sub_dir(tmp_path: Path) -> None:
    project = _project(
        tmp_path, [entry("atlas"), entry("mono", sub_dir="sub/pkg"), entry("«my-pkg»")], packages_dir="deps/pkgs"
    )
    for name in ("atlas", "mono", "my-pkg"):
        checkout(project, name, packages_dir="deps/pkgs")
    (project / "deps/pkgs/mono/sub/pkg").mkdir(parents=True)
    workspace = read_workspace(project)

    assert checkout_paths(workspace, find_package(workspace, "atlas")) == (
        project / "deps/pkgs/atlas",
        project / "deps/pkgs/atlas",
    )
    assert checkout_paths(workspace, find_package(workspace, "mono")) == (
        project / "deps/pkgs/mono",
        project / "deps/pkgs/mono/sub/pkg",
    )
    assert checkout_paths(workspace, find_package(workspace, "my-pkg"))[0] == project / "deps/pkgs/my-pkg"


def test_a_package_that_is_not_checked_out_or_escapes_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path, [entry("atlas"), entry("mono", sub_dir="../elsewhere"), entry("«a/b»")])
    checkout(project, "mono")
    workspace = read_workspace(project)

    with pytest.raises(LibraryError, match="library atlas: it is not checked out"):
        checkout_paths(workspace, find_package(workspace, "atlas"))
    with pytest.raises(LibraryError, match="library mono: its subDir"):
        checkout_paths(workspace, find_package(workspace, "mono"))
    with pytest.raises(LibraryError, match="library a/b: its name is not a directory name"):
        checkout_paths(workspace, find_package(workspace, "a/b"))


def test_a_sub_dir_that_leaves_the_checkout_is_refused_even_when_the_target_exists(tmp_path: Path) -> None:
    project = _project(tmp_path, [entry("mono", sub_dir="../elsewhere")])
    checkout(project, "mono")
    (project / ".lake/packages/elsewhere").mkdir()
    workspace = read_workspace(project)

    with pytest.raises(LibraryError, match="library mono: its subDir '../elsewhere' is not inside the checkout"):
        checkout_paths(workspace, find_package(workspace, "mono"))


def test_a_checkout_reached_through_a_symbolic_link_is_refused_by_name(tmp_path: Path) -> None:
    # A small volume is often relieved by moving .lake elsewhere and linking it back.
    project = _project(tmp_path, [entry("atlas")])
    elsewhere = tmp_path / "big-disk"
    checkout(elsewhere, "atlas", packages_dir="packages")
    (project / ".lake").symlink_to(elsewhere, target_is_directory=True)
    workspace = read_workspace(project)

    with pytest.raises(LibraryError) as refusal:
        checkout_paths(workspace, find_package(workspace, "atlas"))

    assert str(refusal.value) == "library atlas: its checkout is reached through the symbolic link .lake"


def _git(tmp_path: Path) -> Path:
    directory = tmp_path / "checkout"
    (directory / ".git/refs/heads").mkdir(parents=True)
    return directory


def test_head_is_resolved_from_a_commit_id_a_ref_file_and_a_packed_ref(tmp_path: Path) -> None:
    directory = _git(tmp_path)
    git = directory / ".git"

    (git / "HEAD").write_text(REV + "\n", encoding="utf-8")
    assert resolve_head(directory) == REV

    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs/heads/main").write_text(MATHLIB_REV + "\n", encoding="utf-8")
    assert resolve_head(directory) == MATHLIB_REV

    (git / "refs/heads/main").unlink()
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{'3' * 40} refs/heads/other\n{REV} refs/heads/main\n^{'4' * 40}\n",
        encoding="utf-8",
    )
    assert resolve_head(directory) == REV


def test_head_is_resolved_through_a_git_file_of_a_linked_worktree(tmp_path: Path) -> None:
    main = _git(tmp_path)
    (main / ".git/refs/heads/topic").write_text(REV + "\n", encoding="utf-8")
    private = main / ".git/worktrees/linked"
    private.mkdir(parents=True)
    (private / "HEAD").write_text("ref: refs/heads/topic\n", encoding="utf-8")
    (private / "commondir").write_text("../..\n", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text(f"gitdir: {private}\n", encoding="utf-8")

    assert resolve_head(linked) == REV


def test_a_head_that_files_cannot_answer_says_why(tmp_path: Path) -> None:
    directory = _git(tmp_path)
    git = directory / ".git"

    with pytest.raises(HeadError, match="no readable Git HEAD"):
        resolve_head(directory)

    (git / "HEAD").write_text("ref: refs/heads/missing\n", encoding="utf-8")
    with pytest.raises(HeadError, match="a ref that does not exist") as missing:
        resolve_head(directory)
    assert not missing.value.needs_git

    (git / "HEAD").write_text("ref: ../../outside\n", encoding="utf-8")
    with pytest.raises(HeadError, match="unsafe ref"):
        resolve_head(directory)

    (git / "HEAD").write_text("ref: refs/heads/.invalid\n", encoding="utf-8")
    (git / "reftable").mkdir()
    with pytest.raises(HeadError, match="cannot be read without Git") as reftable:
        resolve_head(directory)
    assert reftable.value.needs_git

    with pytest.raises(HeadError, match="not a Git checkout"):
        resolve_head(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        manifest([entry("x")]).replace('"x"', '"«\\ud800»"', 1),
        manifest([entry("x", sub_dir="y")]).replace('"y"', '"\\ud800"'),
        manifest([entry("x")]).replace('".lake/packages"', '"\\ud800"'),
    ],
    ids=["name", "subDir", "packagesDir"],
)
def test_a_manifest_value_that_is_not_utf8_is_refused(tmp_path: Path, text: str) -> None:
    assert "\\ud800" in text
    project = _project(tmp_path, [])
    (project / "lake-manifest.json").write_text(text, encoding="utf-8")

    with pytest.raises(WorkspaceError, match="not a Lake manifest"):
        read_workspace(project)


def test_a_git_pointer_or_common_directory_with_a_nul_is_refused(tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: a\0b\n", encoding="utf-8")
    with pytest.raises(HeadError, match="names no Git directory"):
        resolve_head(linked)

    directory = _git(tmp_path)
    (directory / ".git/commondir").write_text("x\0y\n", encoding="utf-8")
    with pytest.raises(HeadError, match="names no common directory"):
        resolve_head(directory)
