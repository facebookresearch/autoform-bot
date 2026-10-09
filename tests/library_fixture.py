"""Build a consumer project with one locked library checkout, without running Git or Lean."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from autoform_cli.library.index import (
    INDEX_FILE,
    TREE_FILES,
    IndexDeclaration,
    IndexDependency,
    IndexHeader,
    IndexModule,
    LibraryIndex,
    dump_index,
    module_digest,
    tree_digest,
)

REV = "1" * 40
MATHLIB_REV = "2" * 40


def entry(
    name: str,
    rev: str = REV,
    *,
    inherited: bool = False,
    sub_dir: str | None = None,
    path: str | None = None,
) -> dict[str, object]:
    """One package entry as Lake 4.32 to 4.34 writes it (manifest version 1.2.0)."""

    common = {
        "name": name,
        "scope": "",
        "inherited": inherited,
        "configFile": "lakefile.toml",
        "manifestFile": "lake-manifest.json",
    }
    if path is not None:
        return {**common, "type": "path", "dir": path}
    return {
        **common,
        "type": "git",
        "url": f"https://example.invalid/{name}",
        "rev": rev,
        "inputRev": "main",
        "subDir": sub_dir,
    }


def manifest(packages: list[dict[str, object]], *, packages_dir: str | None = ".lake/packages", **root) -> str:
    payload: dict[str, object] = {"version": "1.2.0", "name": "project", "lakeDir": ".lake", "packages": packages}
    if packages_dir is not None:
        payload["packagesDir"] = packages_dir
    payload.update(root)
    return json.dumps(payload, indent=1) + "\n"


def checkout(project: Path, name: str, rev: str = REV, *, packages_dir: str = ".lake/packages") -> Path:
    """Create the directory Lake clones ``name`` into, with ``HEAD`` detached at ``rev``."""

    directory = project / packages_dir / name
    (directory / ".git").mkdir(parents=True)
    (directory / ".git/HEAD").write_text(rev + "\n", encoding="utf-8")
    return directory


SOURCES = {
    "Lib.lean": "import Lib.Convex\n",
    "Lib/Convex.lean": "module\n/-! Convex sets and separation. -/\npublic theorem separation : True := trivial\n",
    "Lib/Classic.lean": "theorem classic : True := trivial\n",
}


def declaration(name: str = "Lib.Convex.separation", *, module: str = "Lib.Convex", **changes) -> IndexDeclaration:
    fields = {
        "kind": "theorem",
        "status": "complete",
        "line": 3,
        "signature": "True",
        "statement": "public theorem separation : True",
        "docstring": "Two disjoint convex sets are separated by a hyperplane.",
        "mentions": ("True",),
        "auto_named": False,
    }
    return IndexDeclaration(name=name, module=module, **{**fields, **changes})


def build_index(
    root: Path,
    declarations: tuple[IndexDeclaration, ...],
    *,
    source_dirs: tuple[str, ...] = ("Lib",),
    module_docs: dict[str, str] | None = None,
) -> LibraryIndex:
    """Index the sources and pin files that are on disk under ``root``, as the generator will."""

    modules = []
    for directory in source_dirs:
        files = sorted((root / directory).rglob("*.lean")) + [root / f"{directory}.lean"]
        for path in files:
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            name = relative.removesuffix(".lean").replace("/", ".")
            data = path.read_bytes()
            modules.append(
                IndexModule(
                    name=name,
                    source_file=relative,
                    sha256=module_digest(data),
                    module_doc=(module_docs or {}).get(name),
                    module_system=data.startswith(b"module"),
                )
            )
    locked = json.loads((root / "lake-manifest.json").read_text(encoding="utf-8"))["packages"]
    header = IndexHeader(
        generator="0.9.0",
        probe=1,
        package="atlas",
        source_dirs=source_dirs,
        lean_toolchain=(root / "lean-toolchain").read_text(encoding="utf-8").split("\n", 1)[0].strip(),
        dependencies=tuple(
            IndexDependency(item["name"], item["type"], item.get("rev")) for item in locked if not item["inherited"]
        ),
        tree=tree_digest(
            ((module.name, module.sha256) for module in modules),
            ((name, (root / name).read_bytes()) for name in TREE_FILES if (root / name).is_file()),
        ),
    )
    return LibraryIndex(header, tuple(modules), declarations)


def write_index(root: Path, index: LibraryIndex) -> None:
    (root / INDEX_FILE).write_bytes(dump_index(index))


@dataclass(frozen=True, slots=True)
class Library:
    project: Path
    checkout: Path
    root: Path
    index: LibraryIndex


def make_library(
    tmp_path: Path,
    *,
    name: str = "atlas",
    sub_dir: str | None = None,
    packages_dir: str = ".lake/packages",
    sources: dict[str, str] | None = None,
    declarations: tuple[IndexDeclaration, ...] | None = None,
    module_docs: dict[str, str] | None = None,
    project_toolchain: str = "leanprover/lean4:v4.34.1",
    project_mathlib: str = MATHLIB_REV,
) -> Library:
    """A project that locks ``name`` at ``REV`` with a current index in its checkout."""

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "lean-toolchain").write_text(project_toolchain + "\n", encoding="utf-8")
    (project / "lake-manifest.json").write_text(
        manifest(
            [entry(name, sub_dir=sub_dir), entry("mathlib", project_mathlib, inherited=True)],
            packages_dir=packages_dir,
        ),
        encoding="utf-8",
    )
    directory = checkout(project, name.replace("«", "").replace("»", ""), packages_dir=packages_dir)
    root = directory / sub_dir if sub_dir else directory
    root.mkdir(parents=True, exist_ok=True)
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.34.1\n", encoding="utf-8")
    (root / "lakefile.toml").write_text('name = "atlas"\n', encoding="utf-8")
    (root / "lake-manifest.json").write_text(manifest([entry("mathlib", MATHLIB_REV)]), encoding="utf-8")
    for relative, text in (SOURCES if sources is None else sources).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
    index = build_index(root, (declaration(),) if declarations is None else declarations, module_docs=module_docs)
    write_index(root, index)
    return Library(project, directory, root, index)
