"""Two pinned name lists for every test of pinned names: 24 records, cut from Mathlib's data or planted.

``libraries/mathlib.jsonl`` holds 22 records in five packages: Mathlib, core
(whose modules start below ``src/``), Batteries, one without documentation and
one hosted off GitHub. It has a ``to_additive`` twin, primed and guillemet
names, ``Nat`` and its recursor, a record without a line, a deprecated record
with its replacement and one without. Both deprecations are planted, since the
data the records were cut from marks none. ``libraries/other.jsonl`` pins
``Twice.decl`` at another revision, so the two lists disagree about it, and
``Agree.decl`` where ``mathlib.jsonl`` does.
"""

from __future__ import annotations

import json
from pathlib import Path

DOCS = "https://leanprover-community.github.io/mathlib4_docs/"
MATHLIB = {
    "repository": "https://github.com/leanprover-community/mathlib4",
    "revision": "1b1ccf4b088b32fa3f23f2de1127433254a81e1e",
    "source_root": "",
    "docs": DOCS,
}
CORE = {
    "repository": "https://github.com/leanprover/lean4",
    "revision": "5e7e3bbc2f4ce5e5b4ad1fa2b8c4fbd4b5f1d1f8",
    "source_root": "src/",
    "docs": DOCS,
}
PACKAGES = {
    "mathlib": MATHLIB,
    "core": CORE,
    "batteries": {
        "repository": "https://github.com/leanprover-community/batteries",
        "revision": "8d6c853f11a5172efa0e96b9f2be1a83d861cdd9",
        "source_root": "",
        "docs": DOCS,
    },
    "undocumented": {
        "repository": "https://github.com/example/undocumented",
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "source_root": "",
        "docs": None,
    },
    "elsewhere": {
        "repository": "https://git.example.org/lean/elsewhere",
        "revision": "89abcdef0123456789abcdef0123456789abcdef",
        "source_root": "",
        "docs": "https://docs.example.org/elsewhere/",
    },
}


def header(packages: dict[str, dict[str, object]]) -> dict[str, object]:
    return {
        "record": "header",
        "schema": "autoform-pinned-names/v0",
        "generator": "trough ConceptDeps",
        "lean_toolchain": "leanprover/lean4:v4.36.0-rc2",
        "coverage": "cited",
        "packages": packages,
    }


def name(
    name: str,
    package: str,
    module: str,
    kind: str = "theorem",
    line: int | None = 1,
    signature: str | None = None,
    deprecated: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "record": "name",
        "name": name,
        "package": package,
        "module": module,
        "kind": kind,
        "line": line,
        "signature": signature,
        "deprecated": deprecated,
    }


MATHLIB_NAMES = [
    name(
        "Subgroup.card_subgroup_dvd_card",
        "mathlib",
        "Mathlib.Algebra.Group.Subgroup.Finite",
        line=263,
        signature="Subgroup.card_subgroup_dvd_card {G : Type u_1} [Group G] (s : Subgroup G) : Nat.card ↥s ∣ Nat.card G",
    ),
    name("Subgroup.card_mul_index", "mathlib", "Mathlib.GroupTheory.Index", line=241),
    name("A.«two words»", "mathlib", "Mathlib.Test.Quoted", kind="def", line=3),
    name("Theorems100.«82».cannot_cube_a_cube", "mathlib", "Archive.Wiedijk100Theorems.CubingACube", line=526),
    name("pow_card_eq_one'", "mathlib", "Mathlib.GroupTheory.OrderOfElement", line=1067),
    name("εNFA", "mathlib", "Mathlib.Computability.EpsilonNFA", kind="structure", line=34),
    name("Finset.prod_range_succ", "mathlib", "Mathlib.Algebra.BigOperators.Basic", line=1230),
    name("Finset.sum_range_succ", "mathlib", "Mathlib.Algebra.BigOperators.Basic", line=1230),
    name(
        "Finset.card_le_of_subset",
        "mathlib",
        "Mathlib.Data.Finset.Card",
        line=180,
        deprecated={"replacement": "Finset.card_le_card"},
    ),
    name("Finset.card_le_card", "mathlib", "Mathlib.Data.Finset.Card", line=176),
    name("Finset.card_pos'", "mathlib", "Mathlib.Data.Finset.Card", line=212, deprecated={"replacement": None}),
    name("Twice.decl", "mathlib", "Mathlib.Test.Twice", kind="def", line=5),
    name("Agree.decl", "mathlib", "Mathlib.Test.Agree", kind="def", line=7),
    name("Nat", "core", "Init.Prelude", kind="inductive", line=1349),
    name("Nat.rec", "core", "Init.Prelude", kind="recursor", line=1349),
    name("Nat.add_comm", "core", "Init.Data.Nat.Basic", line=180),
    name("List.head?", "core", "Init.Data.List.Basic", kind="def", line=622),
    name("Option.get!", "core", "Init.Data.Option.BasicAux", kind="def", line=22),
    name("instAddNat", "core", "Init.Prelude", kind="instance", line=None),
    name("MLList", "batteries", "Batteries.Data.MLList.Basic", kind="structure", line=31),
    name("Undocumented.decl", "undocumented", "Undocumented.Basic", line=10),
    name("Elsewhere.decl", "elsewhere", "Elsewhere.Basic", line=12),
]
OTHER_NAMES = [
    name("Twice.decl", "next", "Mathlib.Test.Twice", kind="def", line=5),
    name("Agree.decl", "mathlib", "Mathlib.Test.Agree", kind="def", line=7),
]


def dump(records: list[dict[str, object]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


def write_pinned_lists(blueprint: Path) -> Path:
    """Write both lists under ``blueprint/libraries/``; return that folder."""

    folder = blueprint / "libraries"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mathlib.jsonl").write_text(dump([header(PACKAGES), *MATHLIB_NAMES]), encoding="utf-8")
    next_revision = {**MATHLIB, "revision": "fedcba9876543210fedcba9876543210fedcba98"}
    (folder / "other.jsonl").write_text(
        dump([header({"mathlib": MATHLIB, "next": next_revision}), *OTHER_NAMES]), encoding="utf-8"
    )
    return folder
