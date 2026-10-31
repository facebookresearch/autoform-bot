import errno
import importlib.metadata
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import autoform_cli.__main__ as cli
from autoform_cli.library.generate import (
    LIBRARY_MARKER,
    GenerateError,
    SharedName,
    VERIFIED_TOOLCHAINS,
    check_index,
    generate_index,
    library_sources,
    render_library_probe,
    write_index,
)
from autoform_cli.library.index import INDEX_FILE, module_digest, parse_index
from autoform_cli.skeleton import SkeletonError
from tests.library_fixture import entry, manifest

A = "/-! Header of A. -/\n\n/-- Doubling. -/\ntheorem double_eq : True := trivial\n"
B = "theorem other : True := trivial\n"


def library(tmp_path: Path, files: dict[str, str] | None = None, *, lakefile: str | None = None) -> Path:
    root = tmp_path / "lib"
    root.mkdir(parents=True)
    (root / "lakefile.toml").write_text(
        lakefile or 'name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nglobs = ["Lib.+"]\n', encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n", encoding="utf-8")
    (root / "lake-manifest.json").write_text(
        json.dumps({"version": "1.2.0", "name": "lib", "packagesDir": ".lake/packages", "packages": []}),
        encoding="utf-8",
    )
    for relative, text in (files or {"Lib/A.lean": A, "Lib/B.lean": B}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def record(marker: str, source: str, module: str, name: str, written: str, **changes) -> str:
    at = source.index(written, source.index("theorem"))
    line = source.count("\n", 0, at)
    column = at - (source.rfind("\n", 0, at) + 1)
    start = source.index("/--") if "/--" in source else source.index("theorem")
    start_line = source.count("\n", 0, start)
    values = {
        "record": "constant",
        "name": name,
        "module": module,
        "private": False,
        "wrapper": None,
        "range": [[start_line + 1, 0], [line + 1, len(source.split("\n")[line])]],
        "selection": [[line + 1, column], [line + 1, column + len(written)]],
        "signature": "True",
        "docstring": None,
        "mentions": ["True"],
        "axioms": [],
    }
    values.update(changes)
    return marker + json.dumps(values, ensure_ascii=False) + "\n"


def runner_for(*records: str, headers: dict[str, str | None] | None = None):
    from autoform_cli.library.generate import LIBRARY_MARKER as marker

    calls: list[str] = []

    def run(probe: str, root: Path, **options: object) -> str:
        calls.append(probe)
        modules = headers if headers is not None else {"Lib.A": "Header of A.", "Lib.B": None}
        lines = [
            marker + json.dumps({"record": "module", "name": name, "module_doc": doc}) + "\n"
            for name, doc in modules.items()
        ]
        return "".join(lines) + "".join(text.replace("M ", marker, 1) for text in records)

    run.calls = calls
    return run


def test_an_index_is_generated_from_the_probe_and_the_sources(tmp_path: Path) -> None:
    root = library(tmp_path)
    runner = runner_for(
        record("M ", A, "Lib.A", "double_eq", "double_eq", docstring="Doubling."),
        record("M ", B, "Lib.B", "other", "other", axioms=["sorryAx"]),
    )
    generated = generate_index(root, runner=runner)
    index = parse_index(generated.data)
    assert index == generated.index
    assert [(m.name, m.source_file, m.module_doc, m.module_system) for m in index.modules] == [
        ("Lib.A", "Lib/A.lean", "Header of A.", False),
        ("Lib.B", "Lib/B.lean", None, False),
    ]
    assert index.modules[0].sha256 == module_digest(A.encode())
    assert [(d.name, d.kind, d.status, d.line) for d in index.declarations] == [("double_eq", "theorem", "complete", 4)]
    assert generated.left_out == 1
    assert (index.header.package, index.header.source_dirs, index.header.lean_toolchain) == (
        "lib",
        ("Lib",),
        "leanprover/lean4:v4.34.1",
    )
    assert runner.calls[0].startswith("import Lib.A\nimport Lib.B\n")
    assert not (root / INDEX_FILE).exists()


def test_two_runs_give_the_same_bytes_with_no_path_in_them(tmp_path: Path) -> None:
    root = library(tmp_path)
    runner = runner_for(record("M ", A, "Lib.A", "double_eq", "double_eq"))
    first = generate_index(root, runner=runner).data
    assert generate_index(root, runner=runner).data == first
    assert str(tmp_path).encode() not in first


def test_crlf_sources_give_the_index_of_lf_sources(tmp_path: Path) -> None:
    lf = generate_index(library(tmp_path / "lf"), runner=runner_for(record("M ", A, "Lib.A", "double_eq", "double_eq")))
    files = {"Lib/A.lean": A.replace("\n", "\r\n"), "Lib/B.lean": B.replace("\n", "\r\n")}
    crlf = generate_index(
        library(tmp_path / "crlf", files), runner=runner_for(record("M ", A, "Lib.A", "double_eq", "double_eq"))
    )
    assert crlf.index.modules == lf.index.modules
    assert crlf.index.declarations == lf.index.declarations


def test_the_index_file_is_not_indexed_as_a_source(tmp_path: Path) -> None:
    root = library(tmp_path)
    (root / INDEX_FILE).write_text("stale\n", encoding="utf-8")
    (root / "Lib" / INDEX_FILE).write_text("stray\n", encoding="utf-8")
    generated = generate_index(root, runner=runner_for())
    assert [module.source_file for module in generated.index.modules] == ["Lib/A.lean", "Lib/B.lean"]


def test_only_the_named_libraries_are_indexed(tmp_path: Path) -> None:
    lakefile = (
        'name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nglobs = ["Lib.+"]\n\n'
        '[[lean_lib]]\nname = "LibTest"\nglobs = ["LibTest.+"]\n'
    )
    root = library(tmp_path, {"Lib/A.lean": A, "LibTest/T.lean": B}, lakefile=lakefile)
    generated = generate_index(root, libraries=("Lib",), runner=runner_for(headers={"Lib.A": None}))
    assert generated.index.header.source_dirs == ("Lib",)
    assert [module.name for module in generated.index.modules] == ["Lib.A"]


def test_an_unknown_library_name_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GenerateError, match="no lean_lib named Nope; the package has: Lib"):
        generate_index(library(tmp_path), libraries=("Nope",), runner=runner_for())


def test_a_lean_file_no_library_selects_is_refused(tmp_path: Path) -> None:
    lakefile = 'name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nglobs = ["Lib.A"]\n'
    root = library(tmp_path, lakefile=lakefile)
    with pytest.raises(GenerateError, match=r"Lib/B\.lean is under an indexed source directory but no indexed library builds it"):
        generate_index(root, runner=runner_for())


def test_lean_files_outside_every_source_directory_are_not_read_or_counted(tmp_path: Path) -> None:
    root = library(tmp_path)
    for directory in ("Other", "v1/Deep", ".github"):
        (root / directory).mkdir(parents=True)
        for number in range(20):
            outside = root / directory / f"M{number}.lean"
            outside.write_bytes(b"\xff not Lean, and not UTF-8")
            outside.chmod(0)
    (root / "Stray.lean").write_bytes(b"\xff")
    (root / "Stray.lean").chmod(0)
    sources = library_sources(root)
    assert sources.modules == {"Lib.A": "Lib/A.lean", "Lib.B": "Lib/B.lean"}
    assert sorted(sources.digests) == ["Lib/A.lean", "Lib/B.lean"]
    generated = generate_index(root, runner=runner_for(record("M ", A, "Lib.A", "double_eq", "double_eq")))
    assert [module.name for module in generated.index.modules] == ["Lib.A", "Lib.B"]


_GLOB_FILES = {
    "Lib.lean": B,
    "Lib/A.lean": B,
    "Lib/AB.lean": B,
    "Lib/A/Inner.lean": B,
    "Lib/A/Inner/Leaf.lean": B,
}


@pytest.mark.parametrize(
    ("globs", "selected"),
    [
        # `M` is that one module, `M.*` the module and its submodules, `M.+` its submodules only.
        (["Lib.*"], ["Lib", "Lib.A", "Lib.A.Inner", "Lib.A.Inner.Leaf", "Lib.AB"]),
        (["Lib", "Lib.+"], ["Lib", "Lib.A", "Lib.A.Inner", "Lib.A.Inner.Leaf", "Lib.AB"]),
        (["Lib", "Lib.A", "Lib.AB", "Lib.A.Inner", "Lib.A.Inner.Leaf"], None),
        (["Lib", "Lib.A.*", "Lib.AB"], None),
        (["Lib", "Lib.A", "Lib.A.+", "Lib.AB"], None),
    ],
)
def test_a_glob_selects_the_modules_lake_builds(tmp_path: Path, globs: list[str], selected: list[str] | None) -> None:
    lakefile = f'name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nglobs = {json.dumps(globs)}\n'
    sources = library_sources(library(tmp_path, _GLOB_FILES, lakefile=lakefile))
    everything = sorted(name.removesuffix(".lean").replace("/", ".") for name in _GLOB_FILES)
    assert sorted(sources.modules) == (everything if selected is None else sorted(selected))


@pytest.mark.parametrize(
    ("globs", "unbuilt"),
    [
        # One module only: nothing under it.
        (["Lib"], "Lib/A.lean, Lib/A/Inner.lean, Lib/A/Inner/Leaf.lean, Lib/AB.lean"),
        # Submodules only: not the module itself.
        (["Lib.+"], "Lib.lean"),
        (["Lib", "Lib.A.+", "Lib.AB"], "Lib/A.lean"),
        # A glob matches whole name components: `Lib.A.*` is not `Lib.AB`.
        (["Lib", "Lib.A.*"], "Lib/AB.lean"),
        (["Lib", "Lib.A", "Lib.AB", "Lib.A.Inner"], "Lib/A/Inner/Leaf.lean"),
        # No glob: Lake builds no module, where a missing key builds the root.
        ([], "Lib.lean, Lib/A.lean, Lib/A/Inner.lean, Lib/A/Inner/Leaf.lean, Lib/AB.lean"),
    ],
)
def test_a_file_the_globs_leave_out_is_refused_by_name(tmp_path: Path, globs: list[str], unbuilt: str) -> None:
    lakefile = f'name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nglobs = {json.dumps(globs)}\n'
    with pytest.raises(GenerateError) as raised:
        library_sources(library(tmp_path, _GLOB_FILES, lakefile=lakefile))
    assert str(raised.value).startswith(f"{unbuilt} is under an indexed source directory but no indexed library builds it")


def test_a_glob_of_one_library_does_not_select_a_file_of_another(tmp_path: Path) -> None:
    # Both libraries name a module `Lib.A`, each under its own source directory.
    lakefile = (
        'name = "lib"\n\n[[lean_lib]]\nname = "One"\nsrcDir = "one"\nroots = ["Lib"]\nglobs = ["Lib.A"]\n\n'
        '[[lean_lib]]\nname = "Two"\nsrcDir = "two"\nroots = ["Pkg"]\nglobs = ["Pkg.+"]\n'
    )
    root = library(tmp_path, {"one/Lib/A.lean": B, "two/Pkg/A.lean": B, "two/Pkg/B.lean": B}, lakefile=lakefile)
    assert library_sources(root).modules == {"Lib.A": "one/Lib/A.lean", "Pkg.A": "two/Pkg/A.lean", "Pkg.B": "two/Pkg/B.lean"}
    (root / "one" / "Lib" / "B.lean").write_text(B, encoding="utf-8")
    with pytest.raises(GenerateError, match=r"^one/Lib/B\.lean is under an indexed source directory"):
        library_sources(root)


@pytest.mark.parametrize("glob", ["Lib.?", "Lib.", ".+", "Lib.*.A", "Lib A"])
def test_a_glob_lake_does_not_read_is_refused(tmp_path: Path, glob: str) -> None:
    lakefile = f'name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nglobs = {json.dumps([glob])}\n'
    with pytest.raises(GenerateError, match="lean_lib Lib: the module glob .* is not `M`"):
        library_sources(library(tmp_path, lakefile=lakefile))


def test_a_glob_that_names_a_module_with_no_source_file_is_refused(tmp_path: Path) -> None:
    lakefile = 'name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nglobs = ["Lib.+", "Lib.Gone"]\n'
    with pytest.raises(GenerateError, match="lean_lib Lib: module Lib.Gone has no source file"):
        library_sources(library(tmp_path, lakefile=lakefile))


def test_a_module_the_probe_did_not_report_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GenerateError, match="the probe did not load Lib.B"):
        generate_index(library(tmp_path), runner=runner_for(headers={"Lib.A": None}))


def test_a_source_changed_while_the_probe_ran_is_refused(tmp_path: Path) -> None:
    root = library(tmp_path)
    inner = runner_for()

    def runner(probe: str, lean_root: Path, **options: object) -> str:
        (root / "Lib" / "B.lean").write_text(B + "-- edited\n", encoding="utf-8")
        return inner(probe, lean_root, **options)

    with pytest.raises(GenerateError, match="the sources changed while the probe ran"):
        generate_index(root, runner=runner)


def test_a_source_deleted_while_the_probe_ran_is_refused_by_name(tmp_path: Path) -> None:
    root = library(tmp_path)
    inner = runner_for()

    def runner(probe: str, lean_root: Path, **options: object) -> str:
        (root / "Lib" / "B.lean").unlink()
        return inner(probe, lean_root, **options)

    with pytest.raises(GenerateError, match=r"the sources changed while the probe ran.*Lib/B\.lean"):
        generate_index(root, runner=runner)


def test_a_source_added_while_the_probe_ran_is_refused_by_name(tmp_path: Path) -> None:
    root = library(tmp_path)
    inner = runner_for()

    def runner(probe: str, lean_root: Path, **options: object) -> str:
        (root / "Lib" / "C.lean").write_text(B, encoding="utf-8")
        return inner(probe, lean_root, **options)

    with pytest.raises(GenerateError, match=r"the sources changed while the probe ran.*Lib/C\.lean"):
        generate_index(root, runner=runner)


def test_a_library_whose_source_directory_is_the_root_is_indexed(tmp_path: Path) -> None:
    lakefile = 'name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nsrcDir = "."\nglobs = ["Lib", "Lib.+"]\n'
    root = library(tmp_path, {"Lib.lean": B, "Lib/A.lean": A}, lakefile=lakefile)
    (root / INDEX_FILE).write_text("stale\n", encoding="utf-8")
    generated = generate_index(root, runner=runner_for(headers={"Lib": None, "Lib.A": "Header of A."}))
    assert generated.index.header.source_dirs == ("Lib",)
    assert [(m.name, m.source_file) for m in generated.index.modules] == [("Lib", "Lib.lean"), ("Lib.A", "Lib/A.lean")]


def test_a_library_with_a_nested_source_directory_is_indexed(tmp_path: Path) -> None:
    lakefile = 'name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nsrcDir = "src"\nglobs = ["Lib.+"]\n'
    root = library(tmp_path, {"src/Lib/A.lean": A, "src/Lib/B.lean": B}, lakefile=lakefile)
    (root / "src" / INDEX_FILE).write_text("stale\n", encoding="utf-8")
    generated = generate_index(root, runner=runner_for())
    assert generated.index.header.source_dirs == ("src/Lib",)
    assert [(m.name, m.source_file) for m in generated.index.modules] == [
        ("Lib.A", "src/Lib/A.lean"),
        ("Lib.B", "src/Lib/B.lean"),
    ]


def test_a_record_larger_than_the_reader_accepts_is_refused_by_name(tmp_path: Path) -> None:
    huge = record("M ", A, "Lib.A", "double_eq", "double_eq", signature="x" * (1024 * 1024))
    with pytest.raises(GenerateError, match="double_eq .* larger than the 1024 KiB"):
        generate_index(library(tmp_path), runner=runner_for(huge))


def test_two_modules_with_one_public_name_are_refused_with_both_named(tmp_path: Path) -> None:
    from autoform_cli.skeleton import SkeletonError

    def runner(probe: str, lean_root: Path, **options: object) -> str:
        raise SkeletonError(
            ["the library index probe failed; is the project built with `lake build`?\n"
             "error: import Lib.B failed, environment already contains 'Lib.same' from Lib.A"]
        )

    with pytest.raises(GenerateError, match="Lib.A and Lib.B both declare Lib.same"):
        generate_index(library(tmp_path), runner=runner)


def test_the_verified_toolchains_are_the_one_the_probe_was_tested_on() -> None:
    assert VERIFIED_TOOLCHAINS == ("leanprover/lean4:v4.34.1", "leanprover/lean4:v4.35.0")


@pytest.mark.parametrize("toolchain", ["leanprover/lean4:v4.27.0", "leanprover/lean4:v4.36.0", "leanprover/lean4:nightly-2026-01-01"])
def test_a_toolchain_the_probe_was_not_verified_on_is_refused_before_any_probe_runs(tmp_path: Path, toolchain: str) -> None:
    root = library(tmp_path)
    (root / "lean-toolchain").write_text(toolchain + "\n", encoding="utf-8")
    runner = runner_for()

    with pytest.raises(GenerateError) as refusal:
        generate_index(root, runner=runner)

    assert str(refusal.value) == (
        f"the package pins {toolchain}, but the library probe is verified only on "
        "leanprover/lean4:v4.34.1, leanprover/lean4:v4.35.0"
    )
    assert runner.calls == []


@pytest.mark.parametrize("toolchain", ["v4.34.1", "4.34.1", "leanprover/lean4:4.34.1"])
def test_a_spelling_elan_resolves_to_the_verified_toolchain_is_indexed_as_written(tmp_path: Path, toolchain: str) -> None:
    root = library(tmp_path)
    (root / "lean-toolchain").write_text(toolchain + "\n", encoding="utf-8")

    index = generate_index(root, runner=runner_for()).index

    assert index.header.lean_toolchain == toolchain


def test_a_refused_toolchain_exits_2_and_writes_nothing(tmp_path: Path, capsys) -> None:
    root = library(tmp_path)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.27.0\n", encoding="utf-8")

    assert cli.main(["library", "index", str(root)]) == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("error: the package pins leanprover/lean4:v4.27.0, but the library probe is verified")
    assert not (root / INDEX_FILE).exists()


def test_the_verified_toolchain_goes_on_to_the_probe(tmp_path: Path) -> None:
    runner = runner_for()
    generate_index(library(tmp_path), runner=runner)
    assert len(runner.calls) == 1


def test_a_failed_probe_says_the_toolchain_may_be_the_cause(tmp_path: Path) -> None:
    def runner(probe: str, lean_root: Path, **options: object) -> str:
        raise SkeletonError(["the library index probe failed; is the project built with `lake build`?\nerror: boom"])

    with pytest.raises(GenerateError) as refusal:
        generate_index(library(tmp_path), runner=runner)

    assert str(refusal.value).startswith("the library index probe failed; is the project built with `lake build`?\nerror: boom")
    assert str(refusal.value).endswith(
        "\nIf the project is built, the toolchain leanprover/lean4:v4.34.1 may be the cause."
    )


def test_the_probe_names_the_constant_when_it_cannot_print_read_a_docstring_or_collect_axioms() -> None:
    # The failures themselves need Lean; this checks the three lookups cannot run outside the wrapper.
    probe = render_library_probe(("Lib.A",))

    for what in ("print the type of", "read the docstring of", "collect the axioms of"):
        assert f'about "{what}" user module' in probe
    assert 'throwError "cannot {what} {name} in {module}: {e.toMessageData}"' in probe
    for bare in ("(← printed type)", "← docstringIn", "let axioms ← if"):
        assert bare not in probe


def test_the_probe_names_its_modules_in_one_string_however_many_there_are() -> None:
    # An `Array Name` literal of 2,808 names exceeded Lean's maximum recursion depth on ATLAS.
    names = [f"Lib.M{i}" for i in range(3000)]
    probe = render_library_probe(names)
    start = probe.index("def libraryModules")
    definition = probe[start : probe.index("\n\n", start)]
    # Booleans, so a failure does not make pytest diff a string of 3,000 names.
    is_array_literal = "#[" in definition or "Name.str" in definition
    assert not is_array_literal
    literal = definition[definition.index('"') : definition.index('" : String)') + 1]
    assert json.loads(literal).split("\n") == sorted(names)


def test_two_copies_of_one_library_in_differently_named_folders_give_identical_bytes(tmp_path: Path) -> None:
    first = generate_index(library(tmp_path / "one"), runner=runner_for()).data
    second = generate_index(library(tmp_path / "another-name" / "checkout"), runner=runner_for()).data

    assert first == second

    copies = []
    for folder in ("one", "another-name/checkout"):
        root = library(tmp_path / "fallback" / folder, lakefile='name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nglobs = ["Lib.+"]\n')
        _without_manifest_name(root)
        copies.append(generate_index(root, runner=runner_for()).data)
    assert copies[0] == copies[1]


def _without_manifest_name(root: Path) -> None:
    (root / "lake-manifest.json").write_text(
        json.dumps({"version": "1.2.0", "packagesDir": ".lake/packages", "packages": []}), encoding="utf-8"
    )


def test_the_package_name_falls_back_to_the_lakefile_not_to_the_folder(tmp_path: Path) -> None:
    root = library(tmp_path / "folder-name", lakefile='name = "from_lakefile"\n\n[[lean_lib]]\nname = "Lib"\nglobs = ["Lib.+"]\n')
    _without_manifest_name(root)

    assert generate_index(root, runner=runner_for()).index.header.package == "from_lakefile"


def test_a_package_that_names_itself_nowhere_is_refused_and_not_named_after_its_folder(tmp_path: Path) -> None:
    root = library(tmp_path / "folder-name", lakefile='[[lean_lib]]\nname = "Lib"\nglobs = ["Lib.+"]\n')
    _without_manifest_name(root)
    runner = runner_for()

    with pytest.raises(GenerateError) as refusal:
        generate_index(root, runner=runner)

    assert str(refusal.value) == (
        "the package has no name: its lake-manifest.json sets none and neither does its lakefile.toml"
    )
    assert runner.calls == []


def _shared(name: str, *modules: str) -> str:
    return "M " + json.dumps({"record": "shared", "name": name, "modules": list(modules)}) + "\n"


# Two modules that state one theorem the same way: Lean imports both and keeps one copy.
_TWICE = {
    "Lib/A.lean": "/-- The copy in A. -/\ntheorem same (n : Nat) :  n = n := rfl\n",
    "Lib/B.lean": "\n\n\ntheorem same (n : Nat) : n = n := rfl\n",
    "Lib/C.lean": "theorem same (n : Nat) : n = n := rfl\n",
}
_THREE = {"Lib.A": None, "Lib.B": None, "Lib.C": None}


def _same(module: str = "Lib.A", **changes) -> str:
    return record("M ", _TWICE[f"Lib/{module[-1]}.lean"], module, "same", "same", **changes)


def test_a_name_two_modules_declare_alike_is_indexed_once_under_the_first_by_name(tmp_path: Path) -> None:
    runner = runner_for(_same(docstring="The copy in A."), _shared("same", "Lib.A", "Lib.B"), headers=_THREE)
    generated = generate_index(library(tmp_path, _TWICE), runner=runner)
    assert [(d.name, d.module, d.line, d.kind, d.statement, d.docstring) for d in generated.index.declarations] == [
        ("same", "Lib.A", 2, "theorem", "theorem same (n : Nat) :  n = n", "The copy in A.")
    ]
    assert generated.shared == (SharedName("same", "Lib.A", ("Lib.B",)),)
    assert parse_index(generated.data) == generated.index


def test_the_module_of_a_shared_name_does_not_follow_the_order_the_probe_lists_its_holders_in(tmp_path: Path) -> None:
    root = library(tmp_path, _TWICE)
    first = generate_index(root, runner=runner_for(_same(), _shared("same", "Lib.A", "Lib.B", "Lib.C"), headers=_THREE))
    second = generate_index(root, runner=runner_for(_same(), _shared("same", "Lib.C", "Lib.B", "Lib.A"), headers=_THREE))
    assert first.data == second.data
    assert first.shared == second.shared == (SharedName("same", "Lib.A", ("Lib.B", "Lib.C")),)


def test_a_shared_name_reported_under_another_module_than_the_first_by_name_is_refused(tmp_path: Path) -> None:
    # The source read for a record is its module's; a record of the copy Lean kept would be read from the wrong file.
    runner = runner_for(_same("Lib.B"), _shared("same", "Lib.A", "Lib.B"), headers=_THREE)
    with pytest.raises(GenerateError) as raised:
        generate_index(library(tmp_path, _TWICE), runner=runner)
    assert str(raised.value) == (
        "the probe reported same under Lib.B, but Lib.A, Lib.B declare it and it is indexed under the first of them; "
        "this is a bug in Autoform"
    )


def test_a_shared_name_left_out_for_its_first_copy_is_reported_as_left_out(tmp_path: Path, monkeypatch, capsys) -> None:
    # Another module may hold a whole proof of it; the count alone would not say so.
    root = library(tmp_path, _TWICE)
    runner = runner_for(
        _same(axioms=["sorryAx"]),
        _shared("same", "Lib.C", "Lib.A", "Lib.B"),
        headers=_THREE,
    )
    generated = generate_index(root, runner=runner)
    assert (generated.index.declarations, generated.shared, generated.left_out) == ((), (), 1)
    assert generated.shared_left_out == (SharedName("same", "Lib.A", ("Lib.B", "Lib.C")),)
    _cli(monkeypatch, runner)
    assert cli.main(["library", "index", str(root)]) == 0
    output = capsys.readouterr()
    assert output.out == f"Wrote {INDEX_FILE}: 3 modules, 0 declarations (0 wanted).\n"
    assert output.err == (
        "warning: same is left out: its declaration in Lib.A depends on sorry or another axiom; "
        "Lib.B, Lib.C declare it too\n"
        "Left out 1 declaration that depends on sorry or another axiom.\n"
    )
    assert cli.main(["library", "index", str(root), "--check"]) == 0
    output = capsys.readouterr()
    assert (output.out, output.err) == (f"{INDEX_FILE} is current.\n", "")


def test_a_shared_name_declared_as_an_axiom_in_its_first_module_is_reported_as_left_out(tmp_path: Path) -> None:
    files = _TWICE | {"Lib/A.lean": "axiom same (n : Nat) : n = n\n"}
    runner = runner_for(
        record("M ", "theorem " + files["Lib/A.lean"], "Lib.A", "same", "same", range=[[1, 0], [1, 28]], selection=[[1, 6], [1, 10]]),
        _shared("same", "Lib.A", "Lib.B"),
        headers=_THREE,
    )
    generated = generate_index(library(tmp_path, files), runner=runner)
    assert (generated.index.declarations, generated.left_out) == ((), 1)
    assert generated.shared_left_out == (SharedName("same", "Lib.A", ("Lib.B",)),)


def test_the_command_warns_of_each_shared_name_and_counts_them(tmp_path: Path, monkeypatch, capsys) -> None:
    files = _TWICE | {"Lib/D.lean": "theorem also : True := trivial\n", "Lib/E.lean": "theorem also : True := trivial\n"}
    root = library(tmp_path, files)
    runner = runner_for(
        _same(),
        record("M ", files["Lib/D.lean"], "Lib.D", "also", "also"),
        _shared("same", "Lib.B", "Lib.C", "Lib.A"),
        _shared("also", "Lib.D", "Lib.E"),
        _shared("also.eq_1", "Lib.D", "Lib.E"),
        headers=_THREE | {"Lib.D": None, "Lib.E": None},
    )
    _cli(monkeypatch, runner)
    assert cli.main(["library", "index", str(root)]) == 0
    output = capsys.readouterr()
    assert output.out == f"Wrote {INDEX_FILE}: 5 modules, 2 declarations (0 wanted, 2 declared in more than one module).\n"
    assert output.err == (
        "warning: also is declared in Lib.D and also in Lib.E; it is indexed once, under Lib.D\n"
        "warning: same is declared in Lib.A and also in Lib.B, Lib.C; it is indexed once, under Lib.A\n"
    )
    # A check has nothing to say about them: the index is what generation gives.
    before = (root / INDEX_FILE).read_bytes()
    assert cli.main(["library", "index", str(root), "--check"]) == 0
    output = capsys.readouterr()
    assert (output.out, output.err) == (f"{INDEX_FILE} is current.\n", "")
    assert (root / INDEX_FILE).read_bytes() == before
    assert check_index(root, runner=runner) == ()


def test_a_name_two_modules_hold_that_is_not_indexed_is_not_reported(tmp_path: Path) -> None:
    # Two modules that unfold one definition both hold its equation lemma, and nothing is wrong.
    generated_beside = record("M ", A, "Lib.A", "double_eq.eq_1", "double_eq", range=[[4, 8], [4, 17]])
    runner = runner_for(
        record("M ", A, "Lib.A", "double_eq", "double_eq"),
        generated_beside,
        _shared("double_eq.eq_1", "Lib.A", "Lib.B"),
        _shared("fact.eq_def", "Lib.A", "Lib.B"),
    )
    generated = generate_index(library(tmp_path), runner=runner)
    assert [d.name for d in generated.index.declarations] == ["double_eq"]
    assert generated.shared == ()


def test_two_wanted_results_with_one_name_in_two_modules_are_both_indexed(tmp_path: Path) -> None:
    # Wanted placeholders are private to their module, so two modules may use one name.
    files = {"Lib/A.lean": "instance_wanted : Nonempty Empty\n", "Lib/B.lean": "instance_wanted : Nonempty Unit\n"}

    def wanted(module: str, source: str) -> str:
        whole = [[1, 0], [1, len(source) - 1]]
        return record(
            "M ", "theorem " + source, module, "instNonempty", "instance_wanted",
            private=True, wrapper="DefWanted", range=whole, selection=whole,
        )

    runner = runner_for(*(wanted(f"Lib.{path[4]}", source) for path, source in files.items()), headers={"Lib.A": None, "Lib.B": None})
    generated = generate_index(library(tmp_path, files), runner=runner)
    assert [(d.name, d.module, d.status, d.statement) for d in parse_index(generated.data).declarations] == [
        ("instNonempty", "Lib.A", "wanted", "instance_wanted : Nonempty Empty"),
        ("instNonempty", "Lib.B", "wanted", "instance_wanted : Nonempty Unit"),
    ]


def test_a_complete_and_a_wanted_result_with_one_name_in_two_modules_are_both_indexed(tmp_path: Path) -> None:
    files = {"Lib/A.lean": "theorem claim : True := trivial\n", "Lib/B.lean": "theorem_wanted claim : True\n"}
    runner = runner_for(
        record("M ", files["Lib/A.lean"], "Lib.A", "claim", "claim"),
        record("M ", "theorem " + files["Lib/B.lean"], "Lib.B", "claim", "claim", private=True, wrapper="ProofWanted",
               range=[[1, 0], [1, 26]], selection=[[1, 15], [1, 20]]),
        headers={"Lib.A": None, "Lib.B": None},
    )
    generated = generate_index(library(tmp_path, files), runner=runner)
    assert [(d.name, d.module, d.status) for d in parse_index(generated.data).declarations] == [
        ("claim", "Lib.A", "complete"),
        ("claim", "Lib.B", "wanted"),
    ]


# What `lean_probe.run_probe` raises when `lake --rehash --no-build build` says a module needs building.
def _stale(modules: list[str], detail: str) -> SkeletonError:
    return SkeletonError(
        [f"Lean build artifacts are stale; run `lake build {' '.join(modules)}` before running the library index probe\n{detail}"]
    )


def test_a_stale_build_is_refused_in_one_readable_line(tmp_path: Path, monkeypatch, capsys) -> None:
    lakefile = (
        'name = "lib"\n\n[[lean_lib]]\nname = "Lib"\nglobs = ["Lib.+"]\n\n'
        '[[lean_lib]]\nname = "Extra"\nglobs = ["Extra.+"]\n\n'
        '[[lean_lib]]\nname = "LibTest"\nglobs = ["LibTest.+"]\n'
    )
    files = {f"Lib/M{number}.lean": B for number in range(1500)} | {"Extra/E.lean": B, "LibTest/T.lean": B}
    root = library(tmp_path, files, lakefile=lakefile)
    # Lake replays the warnings of the modules that are built before it names the one that is not.
    lake = (
        "⚠ [2/9] Replayed Lib.M3\nwarning: Lib/M3.lean:1:8: declaration uses `sorry`\n"
        "✖ [3/9] Building Lib.M7\nerror: target is out-of-date and needs to be rebuilt\n"
        "Some required targets logged failures:\n- Lib.M7"
    )

    def runner(probe: str, lean_root: Path, **options: object) -> str:
        imported = [line.removeprefix("import ") for line in probe.splitlines() if line.startswith("import ")]
        raise _stale(imported[:-1], lake)

    with pytest.raises(GenerateError) as raised:
        generate_index(root, libraries=("Lib", "Extra"), runner=runner)
    assert str(raised.value) == (
        "the build is missing or stale; run `lake build Extra Lib` and index again (Lake: ✖ [3/9] Building Lib.M7)"
    )
    _cli(monkeypatch, runner)
    assert cli.main(["library", "index", str(root), "--check"]) == 1  # no index yet
    assert cli.main(["library", "index", str(root)]) == 2
    error = capsys.readouterr().err.splitlines()[-1]
    assert error.startswith("error: the build is missing or stale; run `lake build Extra Lib LibTest` and index again")
    assert len(error) < 200 and "Lib.M8" not in error and "Replayed" not in error


def test_a_stale_build_with_nothing_from_lake_still_names_the_libraries(tmp_path: Path) -> None:
    def runner(probe: str, lean_root: Path, **options: object) -> str:
        raise _stale(["Lib.A", "Lib.B"], "")

    with pytest.raises(GenerateError) as raised:
        generate_index(library(tmp_path), runner=runner)
    assert str(raised.value) == "the build is missing or stale; run `lake build Lib` and index again"


def test_a_stale_build_lake_explains_in_another_way_gives_its_first_line(tmp_path: Path) -> None:
    def runner(probe: str, lean_root: Path, **options: object) -> str:
        raise _stale(["Lib.A", "Lib.B"], "error: no such target\nand more")

    with pytest.raises(GenerateError) as raised:
        generate_index(library(tmp_path), runner=runner)
    assert str(raised.value) == (
        "the build is missing or stale; run `lake build Lib` and index again (Lake: error: no such target)"
    )


def test_a_long_first_line_from_lake_is_cut(tmp_path: Path) -> None:
    def runner(probe: str, lean_root: Path, **options: object) -> str:
        raise _stale(["Lib.A", "Lib.B"], "\n" + "x" * 5000 + "\nsecond")

    with pytest.raises(GenerateError) as raised:
        generate_index(library(tmp_path), runner=runner)
    assert len(str(raised.value)) < 400 and str(raised.value).endswith("...)") and "second" not in str(raised.value)


def test_the_generator_version_is_read_before_lean_starts(tmp_path: Path, monkeypatch) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)

    def never(*args: object, **options: object) -> str:
        raise AssertionError("the probe must not run when the index could not be written anyway")

    with pytest.raises(GenerateError, match="^Autoform is not installed, so the index cannot record which version wrote it"):
        generate_index(library(tmp_path), runner=never)


def test_a_range_outside_the_source_is_refused_and_not_a_traceback(tmp_path: Path, monkeypatch, capsys) -> None:
    beyond = record("M ", A, "Lib.A", "double_eq", "double_eq", selection=[[40, 8], [40, 17]])
    with pytest.raises(GenerateError, match=r"double_eq in Lib\.A .* the source"):
        generate_index(library(tmp_path / "a"), runner=runner_for(beyond))
    root = library(tmp_path / "b")
    _cli(monkeypatch, runner_for(record("M ", A, "Lib.A", "double_eq", "double_eq", range=[[3, 0], [90, 0]])))
    assert cli.main(["library", "index", str(root)]) == 2
    assert capsys.readouterr().err.startswith("error: the probe places double_eq in Lib.A at ")
    assert not (root / INDEX_FILE).exists()


def test_a_non_ascii_module_name_is_refused(tmp_path: Path) -> None:
    root = library(tmp_path, {"Lib/Été.lean": B})
    with pytest.raises(GenerateError, match="is not a module name the index format holds"):
        generate_index(root, runner=runner_for())


def test_the_probe_imports_each_module_plainly_and_is_not_a_module_file() -> None:
    probe = render_library_probe(("Lib.B", "Lib.A"))
    lines = probe.splitlines()
    assert lines[:2] == ["import Lib.A", "import Lib.B"]
    assert "module" not in {line.strip() for line in lines}
    assert not any(line.startswith(("public import", "meta import")) for line in lines)
    assert LIBRARY_MARKER in probe
    assert '("Lib.A\\nLib.B" : String)' in probe
    assert "{" + "output_limit" + "}" not in probe


def _written(tmp_path: Path):
    root = library(tmp_path)
    runner = runner_for(record("M ", A, "Lib.A", "double_eq", "double_eq"))
    generated = generate_index(root, runner=runner)
    return root, runner, generated, write_index(root, generated)


def test_the_index_is_written_at_the_package_root_byte_for_byte(tmp_path: Path) -> None:
    root, _runner, generated, path = _written(tmp_path)
    assert path == root / INDEX_FILE
    assert path.read_bytes() == generated.data
    assert sorted(item.name for item in root.iterdir() if item.name.startswith(".")) == []


def test_a_failed_install_leaves_the_previous_index_and_no_stage(tmp_path: Path, monkeypatch) -> None:
    root, runner, generated, path = _written(tmp_path)
    before = path.read_bytes()
    (root / "Lib" / "B.lean").write_text(B + "\ntheorem more : True := trivial\n", encoding="utf-8")
    changed = generate_index(root, runner=runner)
    assert changed.data != before

    def fail(outputs: object) -> None:
        raise OSError("interrupted")

    monkeypatch.setattr("autoform_cli.library.generate.replace_outputs", fail)
    with pytest.raises(GenerateError, match=f"^{INDEX_FILE} could not be written: interrupted; the previous one is unchanged$"):
        write_index(root, changed)
    assert path.read_bytes() == before
    assert [item.name for item in root.iterdir() if "autoform-stage" in item.name] == []


def test_an_index_that_is_a_symbolic_link_is_not_written_through(tmp_path: Path) -> None:
    root = library(tmp_path)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("keep\n", encoding="utf-8")
    (root / INDEX_FILE).symlink_to(target)
    with pytest.raises(GenerateError, match="is not a regular file"):
        write_index(root, generate_index(root, runner=runner_for()))
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_a_write_failure_gives_the_reason_the_operating_system_gave(tmp_path: Path, monkeypatch) -> None:
    root, runner, generated, path = _written(tmp_path)

    def full(destination: Path, content: str | None = None) -> Path:
        raise OSError(errno.ENOSPC, "No space left on device", str(destination))

    monkeypatch.setattr("autoform_cli.library.generate.stage_output", full)
    with pytest.raises(GenerateError) as raised:
        write_index(root, generated)
    assert str(raised.value) == (
        f"{INDEX_FILE} could not be written: No space left on device; the previous one is unchanged"
    )
    assert path.read_bytes() == generated.data


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs file modes that bind")
def test_a_first_index_that_cannot_be_written_says_why_and_leaves_none(tmp_path: Path) -> None:
    root = library(tmp_path)
    generated = generate_index(root, runner=runner_for())
    root.chmod(0o555)
    try:
        with pytest.raises(GenerateError) as raised:
            write_index(root, generated)
    finally:
        root.chmod(0o755)
    assert str(raised.value) == f"{INDEX_FILE} could not be written: Permission denied; none was written"
    assert not (root / INDEX_FILE).exists()


def test_a_failed_rollback_is_not_reported_as_an_unchanged_index(tmp_path: Path, monkeypatch) -> None:
    root, runner, generated, path = _written(tmp_path)
    (root / "Lib" / "B.lean").write_text(B + "\ntheorem more : True := trivial\n", encoding="utf-8")
    changed = generate_index(root, runner=runner)
    backup = root / f".{INDEX_FILE}.autoform-backup-0123"

    def fail(outputs: list) -> None:
        # What `replace_outputs` does when the install fails and the previous file cannot be put back.
        os.replace(path, backup)
        raise SkeletonError(
            [
                "could not publish skeleton output: [Errno 5] Input/output error",
                f"could not restore skeleton output {path}; previous output remains at {backup}: [Errno 5] Input/output error",
            ]
        )

    monkeypatch.setattr("autoform_cli.library.generate.replace_outputs", fail)
    with pytest.raises(GenerateError) as raised:
        write_index(root, changed)
    message = str(raised.value)
    assert "unchanged" not in message
    assert message.startswith(f"{INDEX_FILE} could not be written: could not publish skeleton output: [Errno 5] Input/output error; ")
    assert f"previous output remains at {backup}" in message
    assert message.endswith(f"; {INDEX_FILE} is not the file it was before")


def test_check_passes_on_a_current_index_and_writes_nothing(tmp_path: Path) -> None:
    root, runner, _generated, path = _written(tmp_path)
    before = {item: item.read_bytes() for item in root.rglob("*") if item.is_file()}
    assert check_index(root, runner=runner) == ()
    assert {item: item.read_bytes() for item in root.rglob("*") if item.is_file()} == before


def test_check_names_a_changed_module_without_running_lean(tmp_path: Path) -> None:
    root, _runner, _generated, _path = _written(tmp_path)
    (root / "Lib" / "B.lean").write_text(B + "-- edited\n", encoding="utf-8")
    (root / "Lib" / "C.lean").write_text(B, encoding="utf-8")
    (root / "Lib" / "A.lean").unlink()

    def never(*args: object, **options: object) -> str:
        raise AssertionError("the probe must not run when the sources already differ")

    assert check_index(root, runner=never) == (
        "changed since the index was generated: Lib/B.lean",
        "not in the index: Lib/C.lean",
        "in the index but missing: Lib/A.lean",
    )


def test_check_reports_a_changed_pin_without_running_lean(tmp_path: Path) -> None:
    root, _runner, _generated, _path = _written(tmp_path)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.34.0\n", encoding="utf-8")
    assert check_index(root, runner=lambda *a, **k: 1 / 0) == (
        "the toolchain, the manifest or the Lake configuration changed since the index was generated",
    )


def test_check_catches_an_index_merged_from_two_current_ones(tmp_path: Path) -> None:
    # Every module digest is right and so is the tree, but a declaration record is not what generation gives.
    root, _runner, _generated, path = _written(tmp_path)
    stale = runner_for(record("M ", A, "Lib.A", "double_eq", "double_eq", signature="1 = 1"))
    assert check_index(root, runner=stale) == ("the index differs from what generation gives for these sources",)
    assert path.exists()


def test_check_reports_a_missing_or_unreadable_index(tmp_path: Path) -> None:
    root = library(tmp_path)
    assert check_index(root, runner=runner_for()) == (f"there is no {INDEX_FILE}",)
    (root / INDEX_FILE).write_text("not an index\n", encoding="utf-8")
    assert check_index(root, runner=runner_for())[0].startswith("the index is not usable: ")


def test_check_accepts_an_index_checked_out_with_crlf(tmp_path: Path) -> None:
    root, runner, generated, path = _written(tmp_path)
    path.write_bytes(generated.data.replace(b"\n", b"\r\n"))
    assert check_index(root, runner=runner) == ()


def _cli(monkeypatch, runner):
    monkeypatch.setattr("autoform_cli.lean_probe.run_probe", runner)


def test_the_command_writes_the_index_and_reports_what_it_left_out(tmp_path: Path, monkeypatch, capsys) -> None:
    root = library(tmp_path)
    _cli(
        monkeypatch,
        runner_for(
            record("M ", A, "Lib.A", "double_eq", "double_eq"),
            record("M ", B, "Lib.B", "other", "other", axioms=["sorryAx"]),
        ),
    )
    assert cli.main(["library", "index", str(root)]) == 0
    output = capsys.readouterr()
    assert output.out == f"Wrote {INDEX_FILE}: 2 modules, 1 declaration (0 wanted).\n"
    assert output.err == "Left out 1 declaration that depends on sorry or another axiom.\n"
    assert parse_index((root / INDEX_FILE).read_bytes()).declarations[0].name == "double_eq"


def test_check_exits_zero_one_and_two(tmp_path: Path, monkeypatch, capsys) -> None:
    root = library(tmp_path)
    runner = runner_for(record("M ", A, "Lib.A", "double_eq", "double_eq"))
    _cli(monkeypatch, runner)
    assert cli.main(["library", "index", str(root), "--check"]) == 1
    assert f"there is no {INDEX_FILE}" in capsys.readouterr().err
    assert not (root / INDEX_FILE).exists()

    assert cli.main(["library", "index", str(root)]) == 0
    capsys.readouterr()
    assert cli.main(["library", "index", str(root), "--check"]) == 0
    assert capsys.readouterr().out == f"{INDEX_FILE} is current.\n"

    def stale(*args: object, **options: object) -> str:
        raise SkeletonError(
            ["Lean build artifacts are stale; run `lake build Lib.A` before running the library index probe"]
        )

    _cli(monkeypatch, stale)
    assert cli.main(["library", "index", str(root), "--check"]) == 2
    assert capsys.readouterr().err == "error: the build is missing or stale; run `lake build Lib` and index again\n"


def test_check_cannot_tell_without_a_package_root(tmp_path: Path, monkeypatch, capsys) -> None:
    _cli(monkeypatch, runner_for())
    with pytest.raises(GenerateError, match="^the package root does not exist or is not a directory$"):
        check_index(tmp_path / "nowhere", runner=runner_for())
    assert cli.main(["library", "index", str(tmp_path / "nowhere"), "--check"]) == 2
    assert capsys.readouterr().err == "error: the package root does not exist or is not a directory\n"
    file = tmp_path / "file"
    file.write_text("", encoding="utf-8")
    assert cli.main(["library", "index", str(file), "--check"]) == 2
    assert capsys.readouterr().err == "error: the package root does not exist or is not a directory\n"


def test_check_cannot_tell_from_an_index_it_cannot_read(tmp_path: Path, monkeypatch, capsys) -> None:
    root = library(tmp_path)
    _cli(monkeypatch, runner_for())
    (root / INDEX_FILE).mkdir()
    with pytest.raises(GenerateError, match=f"^{INDEX_FILE} exists but cannot be read: Is a directory$"):
        check_index(root, runner=runner_for())
    assert cli.main(["library", "index", str(root), "--check"]) == 2
    assert capsys.readouterr().err == f"error: {INDEX_FILE} exists but cannot be read: Is a directory\n"
    (root / INDEX_FILE).rmdir()
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        (root / INDEX_FILE).write_text("{}\n", encoding="utf-8")
        (root / INDEX_FILE).chmod(0)
        assert cli.main(["library", "index", str(root), "--check"]) == 2
        assert capsys.readouterr().err == f"error: {INDEX_FILE} exists but cannot be read: Permission denied\n"


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "soon"])
def test_a_timeout_that_is_not_a_positive_number_of_seconds_is_refused(tmp_path: Path, value: str, capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(["library", "index", str(tmp_path), "--timeout", value])
    assert stopped.value.code == 2
    assert "expected a positive number of seconds" in capsys.readouterr().err


def test_a_file_the_command_cannot_read_is_named_with_the_reason(tmp_path: Path, monkeypatch, capsys) -> None:
    root = library(tmp_path)

    def denied(*args: object, **options: object):
        raise PermissionError(errno.EACCES, "Permission denied", str(root / "lakefile.toml"))

    monkeypatch.setattr(cli, "generate_index", denied)
    assert cli.main(["library", "index", str(root)]) == 2
    assert capsys.readouterr().err == f"error: {root / 'lakefile.toml'} cannot be read: Permission denied\n"
    monkeypatch.setattr(cli, "generate_index", lambda *a, **k: (_ for _ in ()).throw(ValueError("embedded null byte")))
    assert cli.main(["library", "index", str(root)]) == 2
    assert capsys.readouterr().err == "error: the package root cannot be used: embedded null byte\n"


def test_a_refusal_exits_two_and_writes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    root = library(tmp_path)
    _cli(monkeypatch, runner_for())
    assert cli.main(["library", "index", str(root), "--lib", "Nope"]) == 2
    assert capsys.readouterr().err.startswith("error: no lean_lib named Nope")
    assert not (root / INDEX_FILE).exists()


def test_library_list_still_works(tmp_path: Path, capsys) -> None:
    root = library(tmp_path)
    assert cli.main(["library", "list", str(root)]) == 0
    assert capsys.readouterr().out == "No locked packages.\n"


# --------------------------------------------------------------------------- #
# The real probe
# --------------------------------------------------------------------------- #

_FIXTURE = Path(__file__).parent / "fixtures" / "library-project"
_REQUIRED = os.environ.get("AUTOFORM_REQUIRE_REAL_LIBRARY_TESTS") == "1"

# These build the fixture, which clones Batteries from GitHub. They run only on request, as CI does;
# then a missing toolchain or a failed download is a failure, never a skip.
real_library = pytest.mark.skipif(
    not _REQUIRED,
    reason="set AUTOFORM_REQUIRE_REAL_LIBRARY_TESTS=1 to run: needs lake, the fixture's toolchain and a clone of Batteries from GitHub",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    assert _REQUIRED, "a real library test is missing its `real_library` mark"
    if shutil.which("lake") is None:
        pytest.fail("real library tests are required but lake is not on PATH")
    root = tmp_path_factory.mktemp("library") / "fix"
    shutil.copytree(_FIXTURE, root, ignore=shutil.ignore_patterns(".lake"))
    build = subprocess.run(["lake", "build"], cwd=root, capture_output=True, text=True, timeout=1800, check=False)
    if build.returncode != 0:
        pytest.fail(f"the library fixture does not build:\n{(build.stdout + build.stderr)[-2000:]}")
    return root


@pytest.fixture(scope="module")
def indexed(built: Path):
    generated = generate_index(built)
    return {declaration.name: declaration for declaration in generated.index.declarations}, generated


@real_library
def test_real_public_declarations_are_indexed_with_their_keywords(indexed) -> None:
    by_name, _generated = indexed
    assert {name: by_name[name].kind for name in (
        "Fix.double", "Fix.double_eq", "Fix.Count", "Fix.Point", "Fix.Sized", "Fix.Colour", "Fix.pointSized",
        "Fix.Point.swap", "Fix.total", "Fix.shown", "Fix.outer", "Fix.plain", "Fix.after_notation",
    )} == {
        "Fix.double": "def", "Fix.double_eq": "theorem", "Fix.Count": "abbrev", "Fix.Point": "structure",
        "Fix.Sized": "class", "Fix.Colour": "inductive", "Fix.pointSized": "instance",
        "Fix.Point.swap": "theorem", "Fix.total": "def", "Fix.shown": "theorem", "Fix.outer": "def",
        "Fix.plain": "theorem", "Fix.after_notation": "theorem",
    }
    assert by_name["Fix.double"].docstring == "Doubling."
    # The line of the keyword or first modifier: not of the docstring, nor of an attribute before it.
    assert {name: by_name[name].line for name in ("Fix.double", "Fix.double_eq", "Fix.Count", "Fix.Point", "Fix.shown")} == {
        "Fix.double": 10, "Fix.double_eq": 13, "Fix.Count": 15, "Fix.Point": 17, "Fix.shown": 5,
    }
    assert all(by_name[name].status == "complete" for name in ("Fix.double", "Fix.plain"))
    anonymous = [d for d in by_name.values() if d.auto_named and d.status == "complete"]
    assert [d.kind for d in anonymous] == ["instance"]


@real_library
def test_real_generated_private_and_incomplete_declarations_are_left_out(indexed) -> None:
    by_name, generated = indexed
    for name in (
        "Fix.unmarked", "Fix.secret", "Fix.outer.helper", "Fix.total.go", "Fix.Point.mk", "Fix.Point.x",
        "Fix.Point.ext", "Fix.Point.ext_iff", "Fix.Point.rec", "Fix.double.eq_1", "Fix.open_claim", "Fix.gap",
        "Fix.rests_on_gap", "Fix.assumed", "Fix.rests_on_axiom", "Fix.handwritten",
    ):
        assert name not in by_name, name
    assert not any("DecidableEq" in name or "term⟪" in name or "fix_zero" in name for name in by_name)
    # open_claim, rests_on_gap, assumed and rests_on_axiom; gap is private and never a candidate.
    assert generated.left_out == 4


@real_library
def test_real_names_and_keywords_written_in_other_forms(indexed) -> None:
    by_name, _generated = indexed
    found = {
        name: (by_name[name].kind, by_name[name].module, by_name[name].statement) if name in by_name else None
        for name in (
            "Nat.fix_root", "Fix.Pair.ext", "Fix.countdown", "Fix.«odd name»", "Fix.plainquoted", "Fix.Pair.eta",
            "Fix.with_option",
        )
    }
    assert found == {
        # `_root_.` leaves the current namespace; the name has no `Fix.` and no `_root_.`.
        "Nat.fix_root": ("theorem", "Fix.Forms", "theorem _root_.Nat.fix_root (n : Nat) : n + 0 = n"),
        # Written by hand, unlike the `ext` theorems `@[ext] structure Point` generates.
        "Fix.Pair.ext": (
            "theorem", "Fix.Forms",
            "theorem Pair.ext {p q : Pair} (left : p.left = q.left) (right : p.right = q.right) : p = q",
        ),
        "Fix.countdown": ("def", "Fix.Forms", "def countdown (n : Nat) : Nat"),
        # Lean prints a component that is not an identifier in quotes, and one that is without them.
        "Fix.«odd name»": ("theorem", "Fix.Forms", "theorem «odd name» : True"),
        "Fix.plainquoted": ("theorem", "Fix.Forms", "theorem «plainquoted» : True"),
        "Fix.Pair.eta": ("theorem", "Fix.Forms", "protected theorem Pair.eta (p : Pair) : Pair.mk p.left p.right = p"),
        "Fix.with_option": ("theorem", "Fix.Forms", "theorem with_option : (6 : Nat) = 6"),
    }
    assert by_name["Fix.Pair.eta"].docstring == "Rebuilding a pair."
    assert (by_name["Fix.Pair.eta"].line, by_name["Fix.Pair.ext"].line, by_name["Fix.with_option"].line) == (29, 15, 32)
    # What `@[ext]` adds beside the hand-written theorem, and the `let rec` helper.
    for name in ("Fix.Pair.ext_iff", "Fix.countdown.loop", "Fix.Pair.mk", "Fix.Pair.left"):
        assert name not in by_name, name
    assert not any("_root_" in name for name in by_name)


@real_library
def test_real_wanted_results_are_indexed_with_a_signature_and_the_source_docstring(indexed) -> None:
    by_name, _generated = indexed
    wanted = {name: d for name, d in by_name.items() if d.status == "wanted"}
    named = {
        "Fix.wanted_theorem": ("theorem", "A result the library wants."),
        "Fix.wanted_proof": ("theorem", "A proof the library wants."),
        "Fix.wanted_value": ("def", "A definition the library wants."),
        "Fix.wanted_successor": ("def", "A definition built on a wanted one."),
        "Fix.wantedInhabited": ("instance", "An instance the library wants."),
    }
    for name, (kind, docstring) in named.items():
        assert (wanted[name].kind, wanted[name].docstring) == (kind, docstring)
        assert wanted[name].signature and "Wanted" not in wanted[name].signature.split()[0]
    assert [wanted[name].line for name in named] == [8, 11, 14, 17, 20]
    anonymous = [d for name, d in wanted.items() if name not in named]
    assert [(d.kind, d.auto_named, d.docstring, d.line) for d in anonymous] == [
        ("instance", True, "An anonymous instance the library wants.", 23)
    ]
    # The placeholder of the wanted result it is built on is private; it is named as written.
    assert "Fix.wanted_value" in wanted["Fix.wanted_successor"].mentions
    assert not any(name.startswith("_private") for d in by_name.values() for name in d.mentions)


@real_library
def test_real_module_records(indexed) -> None:
    _by_name, generated = indexed
    assert {module.name: module.module_system for module in generated.index.modules} == {
        "Fix.Forms": True, "Fix.Incomplete": False, "Fix.Notation": True, "Fix.Public": True, "Fix.Visibility": True, "Fix.Wanted": True,
    }
    headers = {module.name: module.module_doc for module in generated.index.modules}
    assert headers["Fix.Public"] == "Public declarations of the fixture library."
    assert headers["Fix.Visibility"] is None


@real_library
def test_real_output_is_deterministic_and_ignores_the_librarys_lean_options(
    built: Path, indexed, tmp_path_factory
) -> None:
    by_name, generated = indexed
    assert generate_index(built).data == generated.data
    base = tmp_path_factory.getbasetemp()
    for path in (built, built.resolve(), base, base.resolve()):
        assert str(path).encode() not in generated.data
    # The lakefile sets pp.explicit = true, under which this prints as
    # `∀ (n : Nat), @Eq Nat (Fix.double n) (@HAdd.hAdd Nat Nat Nat (@instHAdd Nat instAddNat) n n)`.
    assert by_name["Fix.double_eq"].signature == "∀ (n : Nat), Fix.double n = n + n"
    # The probe's own choice, which is not core Lean's default.
    assert by_name["Fix.fn_id"].signature == "(fun n ↦ n) = id"


@real_library
def test_a_complete_hit_imports_and_a_wanted_one_does_not_resolve(built: Path, tmp_path: Path) -> None:
    def compiles(text: str) -> bool:
        file = tmp_path / "Consumer.lean"
        file.write_text(text, encoding="utf-8")
        return subprocess.run(["lake", "env", "lean", str(file)], cwd=built, capture_output=True, timeout=300).returncode == 0

    assert compiles("import Fix.Public\n#check @Fix.double_eq\n")
    assert not compiles("import Fix.Wanted\n#check @Fix.wanted_theorem\n")


@real_library
def test_real_check_and_stale_build(built: Path, tmp_path: Path, capsys) -> None:
    root = tmp_path / "fix"
    shutil.copytree(built, root, symlinks=True)
    assert cli.main(["library", "index", str(root)]) == 0
    assert cli.main(["library", "index", str(root), "--check"]) == 0
    source = root / "Fix" / "Public.lean"
    source.write_text(source.read_text(encoding="utf-8") + "\ntheorem late : True := trivial\n", encoding="utf-8")
    # The source walk sees the edit first: exit 1 without running Lean.
    assert cli.main(["library", "index", str(root), "--check"]) == 1
    # Generating against the edited source needs a rebuild: exit 2.
    capsys.readouterr()
    assert cli.main(["library", "index", str(root)]) == 2
    error = capsys.readouterr().err
    assert error.startswith("error: the build is missing or stale; run `lake build Fix` and index again (Lake: ✖ [")
    assert error.rstrip().endswith("Building Fix.Public)")
    assert error.count("\n") == 1 and "Fix.Wanted" not in error


@real_library
def test_real_a_module_set_of_thousands_of_modules_elaborates(built: Path, tmp_path: Path) -> None:
    # Well past the array literal's limit, which fell between 2,003 and 2,808 names on Lean 4.34.1.
    names = [f"Lib.M{i}" for i in range(10000)] + ["Lib.A'", "Lib.B!", "Lib.C?"]
    probe = render_library_probe(names)
    start = probe.index("def libraryModules")
    file = tmp_path / "ModuleSet.lean"
    file.write_text(
        "import Lean\nopen Lean\n\n"
        + probe[start : probe.index("\n\n", start)]
        # Name literals are built by Lean's parser, not by `String.toName`.
        + "\n\n#eval IO.println (libraryModules.size, [`Lib.A', `Lib.B!, `Lib.C?].all libraryModules.contains)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["lake", "env", "lean", str(file)], cwd=built, capture_output=True, text=True, timeout=600, check=False
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]
    assert result.stdout.strip() == "(10003, true)"


@real_library
def test_real_public_name_in_two_modules(built: Path, tmp_path: Path, capsys) -> None:
    root = tmp_path / "fix"
    shutil.copytree(built, root, symlinks=True)
    again = root / "Fix" / "Again.lean"

    def build() -> None:
        subprocess.run(["lake", "build"], cwd=root, check=True, timeout=1800)

    # Lean refuses to import two modules that declare one name in two ways, and says which.
    again.write_text("namespace Fix\ntheorem plain : (5 : Nat) = 5 := rfl\nend Fix\n", encoding="utf-8")
    build()
    with pytest.raises(GenerateError) as raised:
        generate_index(root)
    first, rest = str(raised.value).split(" and ", 1)
    assert {first, rest.split(" ", 1)[0]} == {"Fix.Again", "Fix.Incomplete"}
    assert rest.split(" ", 1)[1] == (
        "both declare Fix.plain; a public name must come from one module, so the library cannot be indexed"
    )

    # Its one exception is a theorem stated the same way twice: it keeps one copy, without a word.
    # The name is indexed once, under the first of the two modules in name order and from that module's source.
    again.write_text(
        "namespace Fix\n\n/-- The copy in Again. -/\ntheorem plain :\n    (4 : Nat) = 4 := rfl\n\nend Fix\n",
        encoding="utf-8",
    )
    build()
    generated = generate_index(root)
    plain = [d for d in generated.index.declarations if d.name == "Fix.plain"]
    assert [(d.module, d.line, d.kind, d.status, d.statement, d.docstring) for d in plain] == [
        ("Fix.Again", 4, "theorem", "complete", "theorem plain :\n    (4 : Nat) = 4", "The copy in Again.")
    ]
    assert generated.shared == (SharedName("Fix.plain", "Fix.Again", ("Fix.Incomplete",)),)
    capsys.readouterr()
    assert cli.main(["library", "index", str(root)]) == 0
    output = capsys.readouterr()
    assert output.out.endswith(" wanted, 1 declared in more than one module).\n")
    assert [line for line in output.err.splitlines() if line.startswith("warning: ")] == [
        "warning: Fix.plain is declared in Fix.Again and also in Fix.Incomplete; it is indexed once, under Fix.Again"
    ]
    assert cli.main(["library", "index", str(root), "--check"]) == 0
    assert capsys.readouterr().err == ""

    # The copy Lean keeps is the one it loads first, and `Fix.Aaa` makes it load `Fix.Zzz` before `Fix.Aab`.
    # The record is still of `Fix.Aab`: its lines, its docstring, and its proof, which is whole where the other is not.
    aab, zzz = root / "Fix" / "Aab.lean", root / "Fix" / "Zzz.lean"
    again.unlink()
    (root / "Fix" / "Aaa.lean").write_text("import Fix.Zzz\n", encoding="utf-8")
    aab.write_text(
        "module\n\n@[expose] public section\n\nnamespace Fix\n\n"
        "/-- The copy in Aab. -/\ntheorem twice :\n    (7 : Nat) = 7 := rfl\n\nend Fix\n",
        encoding="utf-8",
    )
    zzz.write_text("namespace Fix\ntheorem twice : (7 : Nat) = 7 := by sorry\nend Fix\n", encoding="utf-8")
    build()
    before = generate_index(built).left_out
    generated = generate_index(root)
    twice = [d for d in generated.index.declarations if d.name == "Fix.twice"]
    assert [(d.module, d.line, d.status, d.statement, d.docstring) for d in twice] == [
        ("Fix.Aab", 8, "complete", "theorem twice :\n    (7 : Nat) = 7", "The copy in Aab.")
    ]
    assert generated.shared == (SharedName("Fix.twice", "Fix.Aab", ("Fix.Zzz",)),)
    assert generated.left_out == before
    # With the proofs the other way round, the module it is indexed under gives no whole proof: it is left out.
    aab.write_text("namespace Fix\ntheorem twice : (7 : Nat) = 7 := by sorry\nend Fix\n", encoding="utf-8")
    zzz.write_text("namespace Fix\n/-- The copy in Zzz. -/\ntheorem twice : (7 : Nat) = 7 := rfl\nend Fix\n", encoding="utf-8")
    build()
    generated = generate_index(root)
    assert "Fix.twice" not in {d.name for d in generated.index.declarations}
    assert (generated.shared, generated.left_out) == ((), before + 1)
    for extra in (aab, zzz, root / "Fix" / "Aaa.lean"):
        extra.unlink()

    # Batteries names an anonymous wanted instance after its class alone, and the placeholders are private
    # to their module: both are indexed, under one name.
    again.write_text(
        "module\n\npublic import Batteries.Util.ProofWanted\n\nnamespace Fix\n\ninstance_wanted : Nonempty Unit\n\nend Fix\n",
        encoding="utf-8",
    )
    subprocess.run(["lake", "build"], cwd=root, check=True, timeout=1800)
    twice = [d for d in parse_index(generate_index(root).data).declarations if d.name == "Fix.instNonempty"]
    assert [(d.module, d.kind, d.status, d.auto_named, d.statement) for d in twice] == [
        ("Fix.Again", "instance", "wanted", True, "instance_wanted : Nonempty Unit"),
        ("Fix.Wanted", "instance", "wanted", True, "instance_wanted : Nonempty Empty"),
    ]
    # Two modules that unfold one definition both hold its equation lemma, which is no clash.
    again.write_text("import Fix.Public\nnamespace Fix\ntheorem unfolded : double 0 = 0 := by rw [double]\nend Fix\n", encoding="utf-8")
    (root / "Fix" / "Also.lean").write_text(
        "import Fix.Public\nnamespace Fix\ntheorem unfolded_too : double 0 = 0 := by rw [double]\nend Fix\n", encoding="utf-8"
    )
    subprocess.run(["lake", "build"], cwd=root, check=True, timeout=1800)
    generated = generate_index(root)
    assert {"Fix.unfolded", "Fix.unfolded_too"} <= {declaration.name for declaration in generated.index.declarations}
    assert generated.shared == ()


@real_library
def test_real_signature_of_a_shared_name_is_that_of_the_module_it_is_indexed_under(built: Path, tmp_path: Path) -> None:
    # Lean compares the types of two copies up to binder names and annotations, so it keeps either,
    # whichever module it loads first. The record must be of `Fix.Aab` both ways.
    root = tmp_path / "fix"
    shutil.copytree(built, root, symlinks=True)
    (root / "Fix" / "Aab.lean").write_text("namespace Fix\ntheorem twice (n : Nat) : n = n := rfl\nend Fix\n", encoding="utf-8")
    (root / "Fix" / "Zzz.lean").write_text("namespace Fix\ntheorem twice {m : Nat} : m = m := rfl\nend Fix\n", encoding="utf-8")
    for imports in ("", "import Fix.Zzz\n"):
        (root / "Fix" / "Aaa.lean").write_text(imports, encoding="utf-8")
        subprocess.run(["lake", "build"], cwd=root, check=True, timeout=1800)
        generated = generate_index(root)
        twice = [d for d in generated.index.declarations if d.name == "Fix.twice"]
        assert [(d.module, d.statement, d.signature) for d in twice] == [
            ("Fix.Aab", "theorem twice (n : Nat) : n = n", "∀ (n : Nat), n = n")
        ]
        assert generated.shared == (SharedName("Fix.twice", "Fix.Aab", ("Fix.Zzz",)),)


@real_library
def test_a_probe_over_its_budget_is_stopped_and_not_retried(built: Path, tmp_path: Path, monkeypatch) -> None:
    import tempfile

    from autoform_cli import lean_probe

    # The probe is written under the temporary directory; make that one only this test uses.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    calls = []
    run_probe = lean_probe.run_probe

    def counted(*args: object, **options: object) -> str:
        calls.append(1)
        return run_probe(*args, **options)

    monkeypatch.setattr("autoform_cli.lean_probe.run_probe", counted)
    with pytest.raises(GenerateError, match="timed out after 0.01 seconds"):
        generate_index(built, timeout=0.01)
    assert calls == [1]
    assert list(tmp_path.iterdir()) == []
    processes = subprocess.run(["ps", "-A", "-o", "args="], check=True, capture_output=True, text=True).stdout
    assert [line for line in processes.splitlines() if str(tmp_path) in line] == []


@real_library
def test_the_generated_index_is_searched_from_a_consumer(built: Path, tmp_path: Path, capsys) -> None:
    assert cli.main(["library", "index", str(built)]) == 0
    capsys.readouterr()
    consumer = tmp_path / "consumer"
    checkout = consumer / ".lake" / "packages" / "fix"
    shutil.copytree(built, checkout, ignore=shutil.ignore_patterns(".lake"))
    # Not the developer's signing or hooks.
    identity = ("user.name=t", "user.email=t@example.invalid", "commit.gpgsign=false", "core.hooksPath=/dev/null")
    git = ["git", "-C", str(checkout), *(part for setting in identity for part in ("-c", setting))]
    for command in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "fixture"]):
        subprocess.run([*git, *command], check=True)
    revision = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (consumer / "lake-manifest.json").write_text(manifest([entry("fix", rev=revision)]), encoding="utf-8")
    shutil.copyfile(_FIXTURE / "lean-toolchain", consumer / "lean-toolchain")
    (consumer / "blueprint" / "roadmap").mkdir(parents=True)
    (consumer / "blueprint" / "roadmap" / "README.md").write_text("---\n---\n\n# Roadmap\n\nNothing yet.\n", encoding="utf-8")

    code = cli.main(["search", str(consumer), "doubling", "--lean-root", str(consumer), "--library", "fix", "--json"])
    output = capsys.readouterr()
    assert code == 0, output.err
    hits = json.loads(output.out)["libraries"][0]["hits"]
    assert [(hit["name"], hit["import"]) for hit in hits[:2]] == [
        ("Fix.double", "import Fix.Public"),
        ("Fix.double_eq", "import Fix.Public"),
    ]
