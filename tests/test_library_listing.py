from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from autoform_cli import __main__ as cli
from autoform_cli.library.index import INDEX_FILE
from autoform_cli.library.listing import LIBRARY_LIST_SCHEMA, list_libraries
from tests.library_fixture import MATHLIB_REV, REV, checkout, entry, make_library, manifest


def _with_more_packages(tmp_path: Path):
    library = make_library(tmp_path)
    (library.project / "lake-manifest.json").write_text(
        manifest(
            [
                entry("atlas"),
                entry("mathlib", MATHLIB_REV, inherited=True),
                entry("local", path="../local"),
                entry("absent"),
            ]
        ),
        encoding="utf-8",
    )
    checkout(library.project, "mathlib", MATHLIB_REV)
    return library


def test_each_locked_package_is_listed_with_whether_search_can_use_it(tmp_path: Path) -> None:
    library = _with_more_packages(tmp_path)

    listing = list_libraries(library.project)

    rows = [(item.name, item.type, item.revision, item.index, item.usable, item.reason) for item in listing.packages]
    assert rows == [
        ("absent", "git", REV, False, False, "it is not checked out"),
        ("atlas", "git", REV, True, True, None),
        ("local", "path", None, False, False, "it is a path dependency, which has no locked revision"),
        ("mathlib", "git", MATHLIB_REV, False, False, f"it has no index ({INDEX_FILE})"),
    ]


def test_a_package_with_an_index_search_would_refuse_is_not_usable(tmp_path: Path) -> None:
    library = make_library(tmp_path)
    (library.root / "Lib/Convex.lean").write_text("theorem changed : True := trivial\n", encoding="utf-8")

    (atlas,) = [item for item in list_libraries(library.project).packages if item.name == "atlas"]

    assert (atlas.index, atlas.usable) == (True, False)
    assert atlas.reason == "1 source file differs from its index: Lib/Convex.lean"


def test_listing_starts_no_process_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library = _with_more_packages(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("listing must not start a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    before = {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    list_libraries(library.project)

    assert {path: path.read_bytes() for path in sorted(tmp_path.rglob("*")) if path.is_file()} == before


def test_library_list_cli_emits_stable_json(tmp_path: Path, capsys) -> None:
    library = _with_more_packages(tmp_path)

    assert cli.main(["library", "list", str(library.project), "--json"]) == 0
    first = capsys.readouterr().out
    assert cli.main(["library", "list", str(library.project), "--json"]) == 0
    assert capsys.readouterr().out == first

    payload = json.loads(first)
    assert payload["schema"] == LIBRARY_LIST_SCHEMA
    assert payload["packages"][1] == {
        "index": True,
        "name": "atlas",
        "reason": None,
        "revision": REV,
        "type": "git",
        "usable": True,
    }
    assert first == json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def test_library_list_cli_prints_one_line_per_package_and_escapes_names(tmp_path: Path, capsys) -> None:
    library = make_library(tmp_path)
    (library.project / "lake-manifest.json").write_text(
        manifest([entry("atlas"), entry("«bad\u001b[31mname»")]), encoding="utf-8"
    )

    assert cli.main(["library", "list", str(library.project)]) == 0
    out = capsys.readouterr().out

    assert "atlas  git 111111111111  usable" in out
    assert "bad\\x1b[31mname  git 111111111111  not usable: it is not checked out" in out
    assert "\u001b" not in out


def test_library_list_cli_reports_an_unreadable_project_with_exit_2(tmp_path: Path, capsys) -> None:
    assert cli.main(["library", "list", str(tmp_path)]) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == "error: the Lean root has no lake-manifest.json; run `lake update` first\n"


def test_a_package_whose_checkout_cannot_be_read_does_not_hide_the_others(tmp_path: Path, capsys) -> None:
    library = _with_more_packages(tmp_path)
    git = library.root / ".git"
    try:
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            pytest.skip("chmod 000 does not deny access here")
        git.chmod(0)
        packages = {item.name: item for item in list_libraries(library.project).packages}
        assert cli.main(["library", "list", str(library.project)]) == 0
    finally:
        git.chmod(0o755)
    out = capsys.readouterr().out

    assert (packages["atlas"].usable, packages["atlas"].reason) == (False, "its checkout cannot be read")
    assert packages["absent"].reason == "it is not checked out"
    assert "atlas  git 111111111111  not usable: its checkout cannot be read" in out
    assert "absent  git 111111111111  not usable: it is not checked out" in out
