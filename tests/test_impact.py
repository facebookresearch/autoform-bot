from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autoform_cli import __main__ as cli, claims, skeleton
from autoform_cli.impact import (
    IMPACT_MARKER,
    IMPACT_SCHEMA,
    ConstantRecord,
    ImpactArticle,
    ImpactError,
    compute_impact,
    format_impact,
    parse_impact_output,
    project_modules,
    render_impact_probe,
    revision_impact,
)
from autoform_cli.runtime import load_runtime_graph
from autoform_cli.skeleton import LeanLibrary, SkeletonError, lean_libraries
from autoform_cli.work import work_context

_SKELETON_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "skeleton-project"


# --------------------------------------------------------------------------- #
# Hand-written records
# --------------------------------------------------------------------------- #


def _rec(
    name: str,
    kind: str = "theorem",
    *,
    module: str = "Demo",
    type_uses: tuple[str, ...] = (),
    value_uses: tuple[str, ...] = (),
    **fields: object,
) -> ConstantRecord:
    return ConstantRecord(name=name, kind=kind, module=module, type_uses=type_uses, value_uses=value_uses, **fields)


def _records(*records: ConstantRecord) -> dict[str, ConstantRecord]:
    return {record.name: record for record in records}


def _article(
    node_id: str, *declarations: str, article_id: str | None = None, dependencies: tuple[str, ...] = ()
) -> ImpactArticle:
    return ImpactArticle(node_id, article_id, declarations, dependencies)


def _impact(records, articles, revised: str, declarations=None, **kwargs):
    article = next(item for item in articles if item.id == revised)
    names = article.declarations if declarations is None else declarations
    return compute_impact(records, articles, article, names, source_revision="rev", **kwargs)


def _ids(items) -> list[str]:
    return [item.id for item in items]


def _lean_key(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:48] or "declaration"
    return f"lean/{slug}-{hashlib.sha256(name.encode()).hexdigest()[:16]}"


def test_type_uses_impact_statements_and_theorem_values_impact_proofs() -> None:
    records = _records(
        _rec("A.f", "def"),
        _rec("A.stated", type_uses=("A.f",)),
        _rec("A.proved", value_uses=("A.f",)),
        _rec("A.chained", value_uses=("A.stated",)),
        _rec("A.further", value_uses=("A.proved",)),
        _rec("A.byDef", "def", value_uses=("A.f",)),
        _rec("A.byOpaque", "opaque", value_uses=("A.f",)),
        _rec("A.overDef", type_uses=("A.byDef",)),
        _rec("A.ax", "axiom", type_uses=("A.byOpaque",)),
    )
    articles = [
        _article("f", "A.f"),
        _article("stated", "A.stated"),
        _article("proved", "A.proved"),
        _article("chained", "A.chained"),
        _article("further", "A.further"),
        _article("by-def", "A.byDef"),
        _article("by-opaque", "A.byOpaque"),
        _article("over-def", "A.overDef"),
        _article("ax", "A.ax"),
    ]

    report = _impact(records, articles, "f")

    # A definition's or opaque constant's value is part of its meaning, so
    # what states anything about them changes too; a theorem's proof is not,
    # so proof impact stops one step past the statements it uses.
    assert _ids(report.statement_impacted) == ["ax", "by-def", "by-opaque", "over-def", "stated"]
    assert _ids(report.proof_impacted) == ["chained", "proved"]
    assert report.helpers == ()
    assert not report.contained


def test_constructor_edges_carry_an_inductive_s_meaning() -> None:
    records = _records(
        _rec("A.Size", "def"),
        _rec("A.Shape", "inductive", value_uses=("A.Shape.mk",)),
        _rec("A.Shape.mk", "constructor", type_uses=("A.Size", "A.Shape"), parent="A.Shape"),
        _rec("A.Shape.rec", "recursor", type_uses=("A.Shape", "A.Shape.mk"), parent="A.Shape"),
        _rec("A.area", "def", type_uses=("A.Shape",)),
        _rec("A.Other", "inductive", value_uses=("A.Other.mk",)),
        _rec("A.Other.mk", "constructor", type_uses=("A.Other",), parent="A.Other"),
    )
    articles = [
        _article("size", "A.Size"),
        _article("shape", "A.Shape"),
        _article("area", "A.area"),
        _article("other", "A.Other"),
    ]

    report = _impact(records, articles, "size")

    assert _ids(report.statement_impacted) == ["area", "shape"]
    assert report.proof_impacted == ()
    assert [(helper.name, helper.kind, helper.impact, helper.owner) for helper in report.helpers] == [
        ("A.Shape.mk", "constructor", "statement", "shape"),
        ("A.Shape.rec", "recursor", "statement", "shape"),
    ]


def test_simp_companions_and_nested_proofs_carry_proof_impact() -> None:
    records = _records(
        _rec("A.P", "def"),
        _rec("A.P_iff", type_uses=("A.P",)),
        _rec("A.P_iff._simp_1", type_uses=("A.P",), value_uses=("A.P_iff",), internal=True, parent="A.P_iff"),
        _rec("A.usesSimp", type_uses=("A.P",), value_uses=("A.P_iff._simp_1",)),
        _rec("A.g._proof_1", value_uses=("A.P_iff",), internal=True, parent="A.g"),
        _rec("A.g", "def", value_uses=("A.g._proof_1",)),
        _rec("A.mid", value_uses=("A.P_iff",)),
        _rec("A.top", value_uses=("A.mid",)),
    )
    articles = [
        _article("p", "A.P"),
        _article("iff", "A.P_iff"),
        _article("uses-simp", "A.usesSimp"),
        _article("g", "A.g"),
        _article("top", "A.top"),
    ]

    report = _impact(records, articles, "iff")

    # `simp` reaches the lemma only through its `_simp_1` companion, and the
    # definition only through the `_proof_1` its nested proof became; a
    # theorem someone wrote (A.mid) stops the propagation instead.
    assert report.statement_impacted == ()
    assert _ids(report.proof_impacted) == ["g", "uses-simp"]
    assert [(helper.name, helper.impact) for helper in report.helpers] == [("A.mid", "proof")]


def test_helpers_report_location_and_the_article_owning_the_nearest_ancestor() -> None:
    records = _records(
        _rec("A.base", "def"),
        _rec("A.base.match_1", "def", type_uses=("A.base",), internal=True, parent="A.base"),
        _rec("A.uses", type_uses=("A.base",)),
        _rec("A.uses.aux", type_uses=("A.base",), parent="A.uses"),
        _rec("A.uses.aux.deep", value_uses=("A.base",), parent="A.uses.aux"),
        _rec("A.shared", "def"),
        _rec("A.shared.aux", type_uses=("A.base",), parent="A.shared"),
        _rec("_private.Demo.Extra.0.A.priv", module="Demo.Extra", value_uses=("A.base",)),
    )
    articles = [
        _article("base", "A.base"),
        _article("uses", "A.uses"),
        _article("shared-b", "A.shared"),
        _article("shared-a", "A.shared"),
    ]
    located: list[str] = []

    def locate(record: ConstantRecord) -> tuple[str | None, int | None]:
        located.append(record.name)
        return ("Demo.lean", len(located)) if record.module == "Demo" else (None, None)

    report = _impact(records, articles, "base", locate=locate)

    assert [helper.as_dict() for helper in report.helpers] == [
        {
            "name": "A.shared.aux",
            "kind": "theorem",
            "impact": "statement",
            "module": "Demo",
            "path": "Demo.lean",
            "line": 1,
            "owner": "shared-a",
            "claim_target": "shared-a",
        },
        {
            "name": "A.uses.aux",
            "kind": "theorem",
            "impact": "statement",
            "module": "Demo",
            "path": "Demo.lean",
            "line": 2,
            "owner": "uses",
            "claim_target": "uses",
        },
        {
            "name": "A.uses.aux.deep",
            "kind": "theorem",
            "impact": "proof",
            "module": "Demo",
            "path": "Demo.lean",
            "line": 3,
            "owner": "uses",
            "claim_target": "uses",
        },
        {
            "name": "_private.Demo.Extra.0.A.priv",
            "kind": "theorem",
            "impact": "proof",
            "module": "Demo.Extra",
            "path": None,
            "line": None,
            "owner": None,
            "claim_target": _lean_key("_private.Demo.Extra.0.A.priv"),
        },
    ]
    assert located == [helper.name for helper in report.helpers]
    assert _ids(report.statement_impacted) == ["uses"]
    # The helpers are repaired under their owners' claims, so shared-a is
    # claimed too, and the unowned helper under a key of its own.
    assert report.claim_targets == ("base", _lean_key("_private.Demo.Extra.0.A.priv"), "shared-a", "uses")


def test_revised_names_resolve_by_component_and_are_never_their_own_helpers() -> None:
    records = _records(_rec("A.leaf", "def"), _rec("A.user", type_uses=("A.leaf",)))
    articles = [_article("owner", "A.user"), _article("empty")]

    report = _impact(records, articles, "empty", ["A.«leaf»", "A.leaf"])

    assert report.declarations == ("A.«leaf»",)
    assert _ids(report.statement_impacted) == ["owner"]
    assert report.helpers == ()


def test_revising_names_that_are_not_project_local_is_refused() -> None:
    records = _records(_rec("A.leaf", "def"))
    articles = [_article("leaf", "A.leaf")]

    with pytest.raises(ImpactError, match=r"^not a project-local constant: Nat\.add, A\.missing$"):
        _impact(records, articles, "leaf", ["Nat.add", "A.leaf", "A.missing", "Nat.add"])
    with pytest.raises(ImpactError, match="^leaf: nothing to revise; pass --declaration NAME$"):
        _impact(records, articles, "leaf", [])


def test_undeclared_dependencies_and_claim_targets() -> None:
    records = _records(
        _rec("A.base", "def"),
        _rec("A.direct", type_uses=("A.base",)),
        _rec("A.transitive", type_uses=("A.base",)),
        _rec("A.loose", value_uses=("A.base",)),
        _rec("A.detached", type_uses=("A.base",)),
    )
    articles = [
        _article("chapter/base", "A.base", article_id="af_base"),
        _article("chapter/direct", "A.direct", article_id="af_direct", dependencies=("chapter/base",)),
        _article("chapter/transitive", "A.transitive", dependencies=("chapter/direct",)),
        _article("chapter/loose", "A.loose", article_id="af_loose", dependencies=("chapter/middle",)),
        _article("chapter/middle", dependencies=("chapter/loose",)),
        _article("chapter/detached", "A.detached", article_id="af_detached"),
    ]

    report = _impact(records, articles, "chapter/base")

    assert _ids(report.statement_impacted) == ["chapter/detached", "chapter/direct", "chapter/transitive"]
    assert _ids(report.proof_impacted) == ["chapter/loose"]
    assert report.undeclared_dependencies == ("chapter/detached", "chapter/loose")
    assert report.claim_targets == ("af_base", "af_detached", "af_direct", "af_loose", "chapter/transitive")


def test_deprecated_constants_report_users_through_internal_details() -> None:
    records = _records(
        _rec("A.new"),
        _rec("A.old", deprecated=True, replacement="A.new"),
        _rec("A.user", value_uses=("A.old",), uses_deprecated=("A.old",)),
        _rec(
            "A.wrapped._proof_1",
            value_uses=("A.old",),
            uses_deprecated=("A.old",),
            internal=True,
            parent="A.wrapped",
        ),
        _rec("A.wrapped", "def", value_uses=("A.wrapped._proof_1",)),
        _rec("A.older", deprecated=True),
        _rec("A.stray._proof_1", value_uses=("A.older",), uses_deprecated=("A.older",), internal=True),
        _rec("A.unused", deprecated=True, replacement="A.new"),
    )
    articles = [_article("new", "A.new")]

    report = _impact(records, articles, "new")

    # An internal user stands for its own users, unless nothing uses it: a
    # constant something still mentions is never reported as safe to delete.
    assert [item.as_dict() for item in report.deprecated] == [
        {"name": "A.old", "replacement": "A.new", "users": ["A.user", "A.wrapped"], "articles": []},
        {"name": "A.older", "replacement": None, "users": ["A.stray._proof_1"], "articles": []},
        {"name": "A.unused", "replacement": "A.new", "users": [], "articles": []},
    ]
    assert report.deprecated_unused == ("A.unused",)
    assert report.contained


def test_a_contained_revision_says_it_can_be_revised_in_place() -> None:
    records = _records(_rec("A.leaf", "def"), _rec("A.other"))
    articles = [_article("chapter/leaf", "A.leaf", article_id="af_leaf"), _article("chapter/other", "A.other")]

    report = _impact(records, articles, "chapter/leaf")

    assert report.contained
    assert report.as_dict()["contained"] is True
    assert report.claim_targets == ("af_leaf",)
    assert format_impact(report) == [
        "Revising A.leaf of chapter/leaf [af_leaf]",
        "Graph source revision: rev",
        "Contained: nothing outside chapter/leaf uses A.leaf, so it can be revised in place.",
        "Claim targets: af_leaf",
    ]


def test_private_declarations_resolve_by_their_user_facing_names() -> None:
    privT = "_private.Demo.B.0.A.privT"
    records = _records(
        _rec("A.base", "def"),
        _rec(privT, module="Demo.B", type_uses=("A.base",), user_name="A.privT"),
        _rec(f"{privT}.aux", module="Demo.B", type_uses=("A.base",), user_name="A.privT.aux", parent=privT),
        _rec("_private.Demo.C.0.A.privU", "def", module="Demo.C", user_name="A.privU"),
        _rec("A.privU", "def", type_uses=("A.base",)),
    )
    articles = [
        _article("base", "A.base"),
        _article("priv", "A.privT", article_id="af_priv"),
        _article("u", "A.privU"),
    ]

    report = _impact(records, articles, "base")

    # The article names the private theorem as its source does, so it is
    # impacted and owns the theorem's `where` helper; an exact kernel name
    # wins over a private constant with the same user-facing name.
    assert _ids(report.statement_impacted) == ["priv", "u"]
    assert report.statement_impacted[0].declarations == ("A.privT",)
    assert [(helper.name, helper.owner) for helper in report.helpers] == [(f"{privT}.aux", "priv")]
    assert report.claim_targets == ("base", "af_priv", "u")
    revised = _impact(records, articles, "base", ["A.privT"])
    assert revised.declarations == ("A.privT",)
    assert revised.helpers == ()


def test_a_helper_of_an_unnamed_private_declaration_claims_the_article_naming_its_owner() -> None:
    privT = "_private.Demo.B.0.A.privT"
    records = _records(
        _rec("A.base", "def"),
        _rec(privT, module="Demo.B", user_name="A.privT"),
        _rec(f"{privT}.aux", module="Demo.B", type_uses=("A.base",), user_name="A.privT.aux", parent=privT),
    )
    articles = [_article("base", "A.base"), _article("priv", "A.privT", article_id="af_priv")]

    report = _impact(records, articles, "base")

    assert report.statement_impacted == ()
    assert [(helper.name, helper.owner) for helper in report.helpers] == [(f"{privT}.aux", "priv")]
    assert report.claim_targets == ("base", "af_priv")


def test_a_name_several_private_declarations_share_is_refused() -> None:
    records = _records(
        _rec("A.base", "def"),
        _rec("_private.Demo.B.0.A.dup", module="Demo.B", user_name="A.dup"),
        _rec("_private.Demo.C.0.A.dup", module="Demo.C", user_name="A.dup"),
    )
    articles = [_article("base", "A.base"), _article("dup", "A.dup")]
    message = "names several private declarations: _private.Demo.B.0.A.dup, _private.Demo.C.0.A.dup"

    with pytest.raises(ImpactError, match=f"^dup: lean: A\\.dup {message}$"):
        _impact(records, articles, "base")
    with pytest.raises(ImpactError, match=f"^A\\.dup {message}$"):
        _impact(records, articles, "dup")


def test_an_alias_shares_its_target_s_statement() -> None:
    records = _records(
        _rec("A.plain"),
        _rec("A.alias", value_uses=("A.plain",), alias_of="A.plain"),
        _rec("A.usesAlias", value_uses=("A.alias",)),
        _rec("A.statesAlias", type_uses=("A.alias",)),
    )
    articles = [
        _article("plain", "A.plain"),
        _article("uses-alias", "A.usesAlias"),
        _article("states-alias", "A.statesAlias"),
    ]

    report = _impact(records, articles, "plain")

    # The alias copied A.plain's type, so revising A.plain revises the alias,
    # what states anything about it, and every proof that uses it.
    assert _ids(report.statement_impacted) == ["states-alias"]
    assert _ids(report.proof_impacted) == ["uses-alias"]
    assert [(helper.name, helper.impact) for helper in report.helpers] == [("A.alias", "statement")]
    assert report.claim_targets == ("plain", _lean_key("A.alias"), "states-alias", "uses-alias")


def test_a_deprecated_constant_s_own_companions_are_not_its_users() -> None:
    records = _records(
        _rec("A.old", "def", deprecated=True),
        _rec("A.old.eq_1", type_uses=("A.old",), uses_deprecated=("A.old",), internal=True, parent="A.old"),
        _rec("A.oldThm", deprecated=True),
        _rec(
            "A.oldThm._simp_1",
            value_uses=("A.oldThm",),
            uses_deprecated=("A.oldThm",),
            internal=True,
            parent="A.oldThm",
        ),
        _rec("A.viaSimp", value_uses=("A.oldThm._simp_1",)),
        _rec("A.other"),
    )
    articles = [_article("other", "A.other")]

    report = _impact(records, articles, "other")

    # Deleting A.old deletes its equation lemma, but a real user of a
    # companion still uses the constant behind it.
    assert [(item.name, item.users) for item in report.deprecated] == [("A.old", ()), ("A.oldThm", ("A.viaSimp",))]
    assert report.deprecated_unused == ("A.old",)


def test_a_deprecated_constant_another_article_names_is_not_unused() -> None:
    records = _records(
        _rec("A.new"),
        _rec("A.old", deprecated=True, replacement="A.new"),
        _rec("A.mine", deprecated=True, replacement="A.new"),
        _rec("A.used", deprecated=True),
        _rec("A.user", value_uses=("A.used",), uses_deprecated=("A.used",)),
    )
    articles = [
        _article("new", "A.new", "A.mine"),
        _article("b", "A.old"),
        _article("a", "A.old", "A.used"),
        _article("user", "A.user"),
    ]

    report = _impact(records, articles, "new", ["A.new"])

    # The revised article drops A.mine from lean: in the commit deleting it.
    assert [item.as_dict() for item in report.deprecated] == [
        {"name": "A.mine", "replacement": "A.new", "users": [], "articles": []},
        {"name": "A.old", "replacement": "A.new", "users": [], "articles": ["a", "b"]},
        {"name": "A.used", "replacement": None, "users": ["A.user"], "articles": ["a"]},
    ]
    assert report.deprecated_unused == ("A.mine",)
    assert format_impact(report)[-5:] == [
        "Deprecated:",
        "  A.mine -> A.new: no users, safe to delete",
        "  A.old -> A.new: named by a, b",
        "  A.used: used by A.user; named by a",
        "Claim targets: new",
    ]


def test_helpers_the_revised_article_owns_keep_a_revision_contained() -> None:
    records = _records(
        _rec("A.S", "inductive", value_uses=("A.S.mk",)),
        _rec("A.S.mk", "constructor", type_uses=("A.S",), parent="A.S"),
        _rec("A.S.rec", "recursor", type_uses=("A.S", "A.S.mk"), parent="A.S"),
        _rec("A.T", "inductive"),
        _rec("A.T.aux", type_uses=("A.T",), parent="A.T"),
        _rec("A.loose", type_uses=("A.T",)),
    )
    articles = [_article("s", "A.S"), _article("t", "A.T"), _article("other", "A.T")]

    owned = _impact(records, articles, "s")
    shared = _impact(records, articles, "t")

    # A structure's generated companions are repaired under its own claim and
    # are still listed; a helper owned by another article or by none is not.
    assert owned.contained
    assert [(helper.name, helper.owner) for helper in owned.helpers] == [("A.S.mk", "s"), ("A.S.rec", "s")]
    assert owned.claim_targets == ("s",)
    assert format_impact(owned)[2] == "Contained: nothing outside s uses A.S, so it can be revised in place."
    assert not shared.contained
    assert [(helper.name, helper.owner) for helper in shared.helpers] == [("A.T.aux", "other"), ("A.loose", None)]
    assert shared.claim_targets == ("t", _lean_key("A.loose"), "other")


def test_revisions_touching_one_unowned_helper_contend_for_its_claim() -> None:
    records = _records(
        _rec("A.left", "def"),
        _rec("A.right", "def"),
        _rec("A.bridge", type_uses=("A.left", "A.right")),
        _rec("A.T", "inductive"),
        _rec("A.T.aux", type_uses=("A.left",), parent="A.T"),
    )
    articles = [
        _article("left", "A.left", article_id="af_left"),
        _article("right", "A.right", article_id="af_right"),
        _article("t", "A.T", article_id="af_t"),
    ]

    left = _impact(records, articles, "left")
    right = _impact(records, articles, "right")

    # No article names A.bridge, so both revisions claim one key derived from
    # its name, shaped like an author claim key; an owned helper is claimed
    # under its owner's claim target.
    key = "lean/a-bridge-" + hashlib.sha256(b"A.bridge").hexdigest()[:16]
    assert claims._validate_key(key) == key
    assert [(helper.name, helper.owner, helper.claim_target) for helper in left.helpers] == [
        ("A.T.aux", "t", "af_t"),
        ("A.bridge", None, key),
    ]
    assert [(helper.name, helper.claim_target) for helper in right.helpers] == [("A.bridge", key)]
    assert left.claim_targets == ("af_left", "af_t", key)
    assert right.claim_targets == ("af_right", key)
    assert not right.contained
    assert json.loads(right.to_json())["helpers"][0]["claim_target"] == key


def test_a_revised_declaration_no_article_names_is_claimed_like_a_helper() -> None:
    records = _records(
        _rec("A.R", "def"),
        _rec("A.R.aux", "def", parent="A.R"),
        _rec("A.S", "def"),
        _rec("A.T", "inductive"),
        _rec("A.T.aux", "def", parent="A.T"),
        _rec("A.loose", "def"),
    )
    articles = [_article("r", "A.R"), _article("s", "A.S"), _article("t", "A.T")]

    from_r = _impact(records, articles, "r", ["A.loose"])
    from_s = _impact(records, articles, "s", ["A.loose"])
    owned = _impact(records, articles, "r", ["A.T.aux"])
    own = _impact(records, articles, "r", ["A.R.aux"])

    # Nothing uses A.loose, yet two revisions of it contend for the claim
    # keyed by its name; a revised declaration another article owns is
    # claimed under that article, and one the revised article owns adds no
    # claim.
    assert from_r.claim_targets == ("r", _lean_key("A.loose"))
    assert from_s.claim_targets == ("s", _lean_key("A.loose"))
    assert not from_r.contained
    assert owned.claim_targets == ("r", "t")
    assert not owned.contained
    assert own.claim_targets == ("r",)
    assert own.contained


def test_an_unowned_helper_claim_key_is_ref_safe_for_any_name() -> None:
    names = ("_private.Demo.Extra.0.A.priv", "A.«weird name»", "«∀»", "A." + "long" * 20)
    records = _records(_rec("A.base", "def"), *(_rec(name, type_uses=("A.base",)) for name in names))
    articles = [_article("base", "A.base")]

    report = _impact(records, articles, "base")

    keys = {helper.name: helper.claim_target for helper in report.helpers}
    assert keys == {name: _lean_key(name) for name in names}
    assert keys["«∀»"].startswith("lean/declaration-")
    assert all(claims._validate_key(key) == key for key in keys.values())
    assert len(keys["A." + "long" * 20]) == len("lean/") + 48 + 1 + 16


# --------------------------------------------------------------------------- #
# Probe output
# --------------------------------------------------------------------------- #


def _payload(name: str, **fields: object) -> dict[str, object]:
    record: dict[str, object] = {
        "name": name,
        "kind": "theorem",
        "instance": False,
        "internal": False,
        "module": "Demo",
        "parent": None,
        "type_uses": [],
        "value_uses": [],
        "deprecated": False,
        "replacement": None,
        "uses_deprecated": [],
        "value_missing": False,
        "user_name": None,
        "alias_of": None,
    }
    record.update(fields)
    return record


def _line(payload: dict[str, object]) -> str:
    return IMPACT_MARKER + json.dumps(payload)


def test_probe_output_is_read_from_marker_lines() -> None:
    text = "\n".join(
        [
            "warning: unrelated Lean output",
            _line(_payload("A.b", type_uses=["A.a"], parent="A")),
            _line(_payload("A.a", kind="def", instance=True)),
            _line(_payload("_private.Demo.0.A.c", value_uses=["A.b"], user_name="A.c", alias_of="A.b")),
            "",
        ]
    )

    records = parse_impact_output(text)

    assert records == {
        "A.a": ConstantRecord(name="A.a", kind="def", module="Demo", instance=True),
        "A.b": ConstantRecord(name="A.b", kind="theorem", module="Demo", type_uses=("A.a",), parent="A"),
        "_private.Demo.0.A.c": ConstantRecord(
            name="_private.Demo.0.A.c",
            kind="theorem",
            module="Demo",
            value_uses=("A.b",),
            user_name="A.c",
            alias_of="A.b",
        ),
    }
    assert records["_private.Demo.0.A.c"].meaning_uses == ("A.b",)


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([IMPACT_MARKER + "{"], "emitted invalid JSON"),
        ([_line({**_payload("A.a"), "extra": 1})], "record with unexpected fields"),
        ([_line(_payload("A.a", type_uses="A.b"))], "malformed 'type_uses' field"),
        ([_line(_payload("A.a", value_uses=[1]))], "malformed 'value_uses' field"),
        ([_line(_payload("A.a", kind="lemma"))], "malformed record for 'A.a'"),
        ([_line(_payload(""))], "malformed record for ''"),
        ([_line(_payload("A.a", user_name=""))], "malformed record for 'A.a'"),
        ([_line(_payload("A.a", user_name=1))], "malformed 'user_name' field"),
        ([_line(_payload("A.a", alias_of="A.b"))], "malformed record for 'A.a'"),
        ([_line(_payload("A.a")), _line(_payload("A.a"))], "emitted A.a twice"),
        (["no records here"], "found no project-local constants"),
        ([_line(_payload("A.a", value_missing=True))], "could not read the value of A.a"),
        ([_line(_payload("A.a", value_uses=["A.gone"]))], r"A\.a using unknown constants: \['A\.gone'\]"),
    ],
)
def test_malformed_probe_output_fails_closed(lines: list[str], message: str) -> None:
    with pytest.raises(SkeletonError, match=message):
        parse_impact_output("\n".join(lines))


def test_rendered_probe_imports_modules_and_names_local_prefixes() -> None:
    source = render_impact_probe(imports=["Demo.B", "Demo", "Demo.B"], project_roots=["Demo.B", "Demo"])

    assert source.startswith("import Demo\nimport Demo.B\n-- Autoform impact probe.")
    assert (
        'let projectRoots : List Name := [Name.str (Name.anonymous) "Demo", '
        'Name.str (Name.str (Name.anonymous) "Demo") "B"]'
    ) in source
    assert f'"{skeleton.PROBE_OUTPUT_ENV}"' in source
    assert IMPACT_MARKER in source
    with pytest.raises(SkeletonError, match="no imports"):
        render_impact_probe(imports=[], project_roots=["Demo"])


_SHADOWED_STD = "object file '/deps/Std/Data.olean' of module Std.Data does not exist"


@pytest.mark.parametrize(
    ("stderr", "message"),
    [
        ("unknown module", "the {label} failed; is the project built with `lake build`?\nunknown module"),
        (
            _SHADOWED_STD,
            "the {label} cannot load toolchain module Std.Data: a dependency library probably provides modules "
            "under `Std`, which hides the toolchain's own `Std`; rename that library's modules\n" + _SHADOWED_STD,
        ),
    ],
)
def test_probe_failures_name_the_impact_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, message: str
) -> None:
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("autoform_cli.skeleton.shutil.which", lambda executable: "/bin/lake")
    monkeypatch.setattr("autoform_cli.skeleton._check_artifacts_fresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "autoform_cli.skeleton._run_bounded_command",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr),
    )
    probe = render_impact_probe(imports=["Demo"], project_roots=["Demo"])

    with pytest.raises(SkeletonError) as impact:
        skeleton.run_probe(probe, tmp_path, label="impact probe")
    with pytest.raises(SkeletonError) as default:
        skeleton.run_probe(probe, tmp_path)

    assert impact.value.issues == (message.format(label="impact probe"),)
    assert default.value.issues == (message.format(label="skeleton probe"),)


def test_impact_probe_freshness_messages_never_mention_skeletons(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("autoform_cli.skeleton.shutil.which", lambda executable: "/bin/lake")
    monkeypatch.setattr(
        "autoform_cli.skeleton._run_bounded_command",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 3, stdout="", stderr="Demo is out of date"),
    )
    probe = render_impact_probe(imports=["Demo"], project_roots=["Demo"])

    def issues(label: str | None) -> tuple[str, ...]:
        with pytest.raises(SkeletonError) as caught:
            skeleton.run_probe(probe, tmp_path, **({} if label is None else {"label": label}))
        return caught.value.issues

    missing = "lake-manifest.json is missing; run `lake build` before"
    assert issues("impact probe") == (f"{missing} running the impact probe",)
    assert issues(None) == (f"{missing} extracting skeletons",)
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")
    stale = "Lean build artifacts are stale; run `lake build Demo` before"
    assert issues("impact probe") == (f"{stale} running the impact probe\nDemo is out of date",)
    assert issues(None) == (f"{stale} extracting skeletons\nDemo is out of date",)


# --------------------------------------------------------------------------- #
# Project modules
# --------------------------------------------------------------------------- #


def _sources(root: Path, *modules: str) -> Path:
    for module in modules:
        path = root.joinpath(*module.split(".")).with_suffix(".lean")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return root


def _library(src: Path, *globs: str, roots: tuple[str, ...] = ("Demo",)) -> LeanLibrary:
    return LeanLibrary(name="Demo", src_dir=src, roots=roots, globs=globs)


def test_project_modules_follow_lake_globs(tmp_path: Path) -> None:
    src = _sources(
        tmp_path,
        "Demo",
        "Demo.Sub",
        "Demo.Sub.A",
        "Demo.Sub.B.C",
        "Demo.Extra",
        "Demo.Extra.X",
        "Demo.Extra.Y.Z",
        "Other.Lone",
    )
    (tmp_path / "Demo" / "Sub" / "notes.md").write_text("", encoding="utf-8")

    # `M` is one module, `M.*` the module and its submodules, `M.+` only its
    # submodules; every glob base marks local constants, like a root.
    assert project_modules([_library(src, "Demo", "Demo.Sub.*", "Demo.Extra.+")]) == (
        ("Demo", "Demo.Extra.X", "Demo.Extra.Y.Z", "Demo.Sub", "Demo.Sub.A", "Demo.Sub.B.C"),
        ("Demo", "Demo.Extra", "Demo.Sub"),
    )
    # Without globs, Lake builds the roots.
    assert project_modules([_library(src, roots=("Demo", "Other.Lone"))]) == (
        ("Demo", "Other.Lone"),
        ("Demo", "Other.Lone"),
    )


@pytest.mark.parametrize(
    ("glob", "message"),
    [
        ("Demo.**", r"cannot read module glob 'Demo\.\*\*'"),
        ("Demo/Odd", "cannot read module glob 'Demo/Odd'"),
        ("Demo.Missing", "module Demo.Missing has no source file"),
        ("Demo.Missing.*", "module Demo.Missing has no source file"),
        ("Demo.Missing.+", r"glob Demo\.Missing\.\+ names no source directory Demo/Missing"),
        ("Demo.Odd.+", r"cannot import .*bad-name\.lean"),
        ("Demo.Empty.+", "the Lake configuration selects no modules"),
    ],
)
def test_project_modules_fail_closed(tmp_path: Path, glob: str, message: str) -> None:
    src = _sources(tmp_path, "Demo", "Demo.Odd.Fine")
    (tmp_path / "Demo" / "Odd" / "bad-name.lean").write_text("", encoding="utf-8")
    (tmp_path / "Demo" / "Empty").mkdir()

    with pytest.raises(SkeletonError, match=message):
        project_modules([_library(src, glob)])


def test_lean_libraries_read_one_glob_or_an_array(tmp_path: Path) -> None:
    (tmp_path / "lakefile.toml").write_text(
        'name = "Demo"\n\n'
        '[[lean_lib]]\nname = "One"\nglobs = "One.+"\n\n'
        '[[lean_lib]]\nname = "Many"\nglobs = ["Many", "Many.Sub.*"]\n\n'
        '[[lean_lib]]\nname = "Plain"\n\n'
        '[[lean_lib]]\nname = "Odd"\nglobs = [1]\n',
        encoding="utf-8",
    )

    assert {library.name: (library.roots, library.globs) for library in lean_libraries(tmp_path)} == {
        "One": (("One",), ("One.+",)),
        "Many": (("Many",), ("Many", "Many.Sub.*")),
        "Plain": (("Plain",), ()),
        "Odd": (("Odd",), ()),
    }


# --------------------------------------------------------------------------- #
# The command, with a stubbed probe
# --------------------------------------------------------------------------- #

_BASE_ID = "af_000000000000000000000001"
_USES_ID = "af_000000000000000000000002"


def _write_article(
    project: Path, name: str, *, metadata: list[str], depends: tuple[str, ...] = (), title: str | None = None
) -> None:
    path = project / "blueprint/roadmap/chapter" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ["---", *metadata, "---", "", f"# {title or path.stem.title()}", "", "A precise statement."]
    if depends:
        text.extend(["", "## Depends on", "", *(f"- [dependency]({target})" for target in depends)])
    path.write_text("\n".join(text) + "\n", encoding="utf-8")


def _blueprint_project(tmp_path: Path, prefix: str) -> Path:
    """A roadmap whose articles name ``{prefix}.base``, ``.uses``, ``.loose`` and nothing."""

    project = tmp_path / "project"
    _write_article(project, "README.md", title="Chapter", metadata=["article_id: af_0000000000000000000000c0"])
    _write_article(
        project,
        "base.md",
        metadata=[f"article_id: {_BASE_ID}", "declaration: definition", "statement: formalized", f"lean: {prefix}.base"],
    )
    _write_article(
        project,
        "uses.md",
        metadata=[f"article_id: {_USES_ID}", "declaration: theorem", "statement: formalized", f"lean: {prefix}.uses"],
        depends=("base.md",),
    )
    _write_article(
        project,
        "loose.md",
        metadata=["declaration: theorem", "statement: formalized", f"lean: {prefix}.loose"],
    )
    _write_article(project, "empty.md", metadata=["declaration: theorem"])
    return project


def _stub_lean_root(tmp_path: Path) -> Path:
    root = tmp_path / "lean"
    root.mkdir()
    (root / "lakefile.toml").write_text('name = "Demo"\n\n[[lean_lib]]\nname = "Demo"\n', encoding="utf-8")
    (root / "Demo.lean").write_text(
        "namespace Demo\n\ndef base (n : Nat) : Nat := n\n\ntheorem base_eq (n : Nat) : base n = n := rfl\n\nend Demo\n",
        encoding="utf-8",
    )
    return root


_STUB_RECORDS = [
    _payload("Demo.base", kind="def"),
    _payload("Demo.uses", type_uses=["Demo.base"]),
    _payload("Demo.base_eq", type_uses=["Demo.base"]),
    _payload("Demo.loose", value_uses=["Demo.base", "Demo.old"], uses_deprecated=["Demo.old"]),
    _payload("Demo.old", deprecated=True, replacement="Demo.base_eq"),
    _payload("Demo.gone", deprecated=True),
]


def _stub_probe(monkeypatch: pytest.MonkeyPatch, records=_STUB_RECORDS, *, error: SkeletonError | None = None):
    calls: list[dict[str, object]] = []

    def run_probe(probe: str, lean_root: Path, **kwargs: object) -> str:
        calls.append({"probe": probe, "lean_root": lean_root, **kwargs})
        if error is not None:
            raise error
        return "\n".join(["lake env lean noise", *(_line(record) for record in records)]) + "\n"

    monkeypatch.setattr("autoform_cli.skeleton.run_probe", run_probe)
    return calls


def test_cli_writes_the_impact_report_as_canonical_json(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    calls = _stub_probe(monkeypatch)
    source_revision, _ = work_context(project, "chapter/base")

    code = cli.main(
        ["work", "impact", _BASE_ID, str(project), "--lean-root", str(lean_root), "--json", "--timeout", "30"]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    assert output.err == ""
    report = json.loads(output.out)
    assert output.out == json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    assert report == {
        "schema": IMPACT_SCHEMA,
        "source_revision": source_revision,
        "article": {"id": "chapter/base", "article_id": _BASE_ID, "claim_target": _BASE_ID},
        "declarations": ["Demo.base"],
        "contained": False,
        "statement_impacted": [
            {"id": "chapter/uses", "article_id": _USES_ID, "claim_target": _USES_ID, "declarations": ["Demo.uses"]}
        ],
        "proof_impacted": [
            {"id": "chapter/loose", "article_id": None, "claim_target": "chapter/loose", "declarations": ["Demo.loose"]}
        ],
        "helpers": [
            {
                "name": "Demo.base_eq",
                "kind": "theorem",
                "impact": "statement",
                "module": "Demo",
                "path": "Demo.lean",
                "line": 5,
                "owner": None,
                "claim_target": "lean/demo-base-eq-7f17aa41d1461243",
            }
        ],
        "undeclared_dependencies": ["chapter/loose"],
        "deprecated": [
            {"name": "Demo.gone", "replacement": None, "users": [], "articles": []},
            {"name": "Demo.old", "replacement": "Demo.base_eq", "users": ["Demo.loose"], "articles": []},
        ],
        "deprecated_unused": ["Demo.gone"],
        "claim_targets": [_BASE_ID, _USES_ID, "chapter/loose", "lean/demo-base-eq-7f17aa41d1461243"],
    }
    (call,) = calls
    assert call["label"] == "impact probe"
    assert call["timeout"] == 30
    assert call["lean_root"] == lean_root.resolve()
    assert str(call["probe"]).startswith("import Demo\n-- Autoform impact probe.")


def test_cli_text_report_lists_each_section(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    calls = _stub_probe(monkeypatch)
    source_revision, _ = work_context(project, "chapter/base")

    code = cli.main(["work", "impact", "chapter/base", str(project), "--lean-root", str(lean_root)])

    output = capsys.readouterr()
    assert code == 0, output.err
    assert output.out.splitlines() == [
        f"Revising Demo.base of chapter/base [{_BASE_ID}]",
        f"Graph source revision: {source_revision}",
        "Statement impacted:",
        f"  chapter/uses [{_USES_ID}]: Demo.uses",
        "Proof impacted:",
        "  chapter/loose: Demo.loose",
        "Helpers no article names:",
        "  Demo.base_eq (theorem, statement) Demo.lean:5; no owner; claim lean/demo-base-eq-7f17aa41d1461243",
        "Impacted without a Markdown dependency path to the revised article: chapter/loose",
        "Deprecated:",
        "  Demo.gone: no users, safe to delete",
        "  Demo.old -> Demo.base_eq: used by Demo.loose",
        f"Claim targets: {_BASE_ID}, {_USES_ID}, chapter/loose, lean/demo-base-eq-7f17aa41d1461243",
    ]
    assert calls[0]["timeout"] == skeleton.DEFAULT_PROBE_TIMEOUT


def test_cli_declaration_flag_replaces_the_article_s_names(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch)

    code = cli.main(
        [
            "work",
            "impact",
            "chapter/empty",
            str(project),
            "--lean-root",
            str(lean_root),
            "--declaration",
            "Demo.gone",
            "--declaration",
            "Demo.gone",
            "--json",
        ]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["article"] == {"id": "chapter/empty", "article_id": None, "claim_target": "chapter/empty"}
    assert report["declarations"] == ["Demo.gone"]
    # No article names Demo.gone, so revising it claims its own key too.
    assert report["contained"] is False
    assert report["claim_targets"] == ["chapter/empty", _lean_key("Demo.gone")]


def test_cli_text_escapes_terminal_control_characters(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch, [*_STUB_RECORDS, _payload("Demo.bad\x1b[2Jname", type_uses=["Demo.base"])])

    code = cli.main(["work", "impact", "chapter/base", str(project), "--lean-root", str(lean_root)])

    output = capsys.readouterr()
    assert code == 0, output.err
    assert "\x1b" not in output.out
    line = "  Demo.bad\\x1b[2Jname (theorem, statement) Demo.lean; no owner; claim lean/demo-bad-2jname-6d30903d669991fb"
    assert line in output.out.splitlines()


@pytest.mark.parametrize(
    ("arguments", "message", "probed"),
    [
        (["chapter/nope"], "error: no article matches 'chapter/nope'", False),
        (["chapter/empty"], "error: chapter/empty names no lean: declaration; pass --declaration NAME", False),
        (
            ["chapter/base", "--declaration", "Nat.add", "--declaration", "Demo.\x07bell"],
            "error: not a project-local constant: Nat.add, Demo.\\x07bell",
            True,
        ),
    ],
)
def test_cli_refuses_questions_it_cannot_answer(
    tmp_path: Path, monkeypatch, capsys, arguments: list[str], message: str, probed: bool
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    calls = _stub_probe(monkeypatch)
    selector, *flags = arguments

    code = cli.main(["work", "impact", selector, str(project), "--lean-root", str(lean_root), *flags])

    output = capsys.readouterr()
    assert code == 2
    assert output.out == ""
    assert output.err.splitlines()[0].startswith(message)
    assert bool(calls) is probed


def test_cli_reports_probe_failures_line_by_line(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    failure = SkeletonError(["the impact probe failed; is the project built with `lake build`?\nDemo.lean:1:0: \x1b[31m"])
    _stub_probe(monkeypatch, error=failure)

    code = cli.main(["work", "impact", "chapter/base", str(project), "--lean-root", str(lean_root)])

    output = capsys.readouterr()
    assert code == 2
    assert output.out == ""
    assert output.err.splitlines() == [
        "error: the impact probe failed; is the project built with `lake build`?",
        "Demo.lean:1:0: \\x1b[31m",
    ]


def test_cli_requires_a_lake_project_and_a_lean_root(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    empty_root = tmp_path / "not-lean"
    empty_root.mkdir()
    calls = _stub_probe(monkeypatch)

    code = cli.main(["work", "impact", "chapter/base", str(project), "--lean-root", str(empty_root)])

    output = capsys.readouterr()
    assert code == 2
    assert output.err.splitlines() == [f"error: no lakefile.toml or lakefile.lean in {empty_root.resolve()}"]
    assert calls == []
    with pytest.raises(SystemExit) as missing:
        cli.main(["work", "impact", "chapter/base", str(project)])
    assert missing.value.code == 2


def test_revision_impact_reports_an_article_nothing_uses_as_contained(tmp_path: Path, monkeypatch) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch)

    report = revision_impact(project, "chapter/uses", lean_root=lean_root)

    assert report.declarations == ("Demo.uses",)
    assert report.contained
    assert report.claim_targets == (_USES_ID,)


def test_helpers_are_located_by_source_name_in_their_module_s_file(tmp_path: Path, monkeypatch) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    (lean_root / "Demo").mkdir()
    (lean_root / "Demo" / "Extra.lean").write_text(
        "namespace Demo\n\nprivate theorem secret (n : Nat) : base n = n := rfl\n\nend Demo\n", encoding="utf-8"
    )
    # A file outside the library that reuses names the library's modules declare.
    (lean_root / "Scratch.lean").write_text(
        "theorem Demo.twin : True := trivial\n\ntheorem Demo.made : True := trivial\n", encoding="utf-8"
    )
    _stub_probe(
        monkeypatch,
        [
            *_STUB_RECORDS,
            _payload(
                "_private.Demo.Extra.0.Demo.secret",
                module="Demo.Extra",
                type_uses=["Demo.base"],
                user_name="Demo.secret",
            ),
            _payload("Demo.twin", type_uses=["Demo.base"]),
            _payload("Demo.made", module="Demo.Made", type_uses=["Demo.base"]),
            _payload("Demo.lost", module="Demo.Made", type_uses=["Demo.base"]),
        ],
    )

    report = revision_impact(project, "chapter/base", lean_root=lean_root)

    # A private name is looked up by its source name. An indexed declaration
    # in another file than the module's own gives only that file, without a
    # line; a module without a source file falls back to the index alone.
    assert [(helper.name, helper.path, helper.line) for helper in report.helpers] == [
        ("Demo.base_eq", "Demo.lean", 5),
        ("Demo.lost", None, None),
        ("Demo.made", "Scratch.lean", 3),
        ("Demo.twin", "Demo.lean", None),
        ("_private.Demo.Extra.0.Demo.secret", "Demo/Extra.lean", 3),
    ]


def _rewrite_base(project: Path) -> None:
    metadata = [f"article_id: {_BASE_ID}", "declaration: definition", "statement: formalized", "lean: Demo.base"]
    _write_article(project, "base.md", metadata=metadata, title="Base, revised")


def _delete_empty(project: Path) -> None:
    (project / "blueprint/roadmap/chapter/empty.md").unlink()


@pytest.mark.parametrize(("selector", "change"), [("chapter/base", _rewrite_base), ("chapter/empty", _delete_empty)])
def test_revision_impact_refuses_a_roadmap_that_changes_while_it_is_read(
    tmp_path: Path, monkeypatch, selector: str, change
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    calls = _stub_probe(monkeypatch)

    def changed(target: Path):
        change(project)
        return load_runtime_graph(target)

    monkeypatch.setattr("autoform_cli.impact.load_runtime_graph", changed)

    with pytest.raises(ImpactError, match="^the roadmap changed while it was read; rerun the command$"):
        revision_impact(project, selector, lean_root=lean_root)
    assert calls == []


# --------------------------------------------------------------------------- #
# The real probe
# --------------------------------------------------------------------------- #


def _lean_toolchain_available() -> bool:
    """Whether a project pinned like the skeleton fixture builds here without a download.

    This repeats tests/test_skeleton.py's check, including its
    AUTOFORM_REQUIRE_REAL_LEAN_TESTS switch, rather than importing that module.
    """

    def unavailable(reason: str) -> bool:
        if os.environ.get("AUTOFORM_REQUIRE_REAL_LEAN_TESTS") == "1":
            raise RuntimeError(f"real Lean tests are required but unavailable: {reason}")
        return False

    if shutil.which("lake") is None:
        return unavailable("lake is not on PATH")
    elan = shutil.which("elan")
    if elan is None:
        return True
    pinned = (_SKELETON_FIXTURE / "lean-toolchain").read_text(encoding="utf-8").strip()
    try:
        listed = subprocess.run([elan, "toolchain", "list"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return unavailable("elan toolchain discovery failed")
    available = any(line.split()[:1] == [pinned] for line in listed.stdout.splitlines())
    return available or unavailable(f"{pinned} is not installed")


_IMP_BASIC = """\
namespace Imp

def base (n : Nat) : Nat := n

theorem base_eq (n : Nat) : base n = n := rfl

theorem uses (n : Nat) : base n = n := base_eq n

def P (n : Nat) : Prop := base n = n

@[simp] theorem P_iff (n : Nat) : P n ↔ True := ⟨fun _ => trivial, fun _ => base_eq n⟩

@[deprecated base_eq (since := "2026-10-05")]
theorem oldEq (n : Nat) : base n = n := base_eq n

@[deprecated base_eq (since := "2026-10-05")]
theorem oldUnused (n : Nat) : base n = n := base_eq n

end Imp
"""

_IMP_EXTRA = """\
import Imp.Basic

namespace Imp

theorem usesSimp (n : Nat) : P n := by simp

theorem proved (n : Nat) : n + 0 = n := base_eq n

set_option linter.deprecated false in
theorem usesOld (n : Nat) : base n = n := oldEq n

theorem apart (n : Nat) : n = n := rfl

end Imp
"""


_IMP_ALIAS = """\
import Lean

open Lean Elab Command

/-- The theorem branch of Batteries' `alias`: the alias copies the target's
type, and its value is the target constant. -/
elab "imp_alias " n:ident " := " t:ident : command => do
  let target ← liftCoreM <| realizeGlobalConstNoOverloadWithInfo t
  let info ← getConstInfo target
  liftCoreM <| addDecl <| .thmDecl { info.toConstantVal with
    name := (← getCurrNamespace) ++ n.getId
    value := mkConst target (info.levelParams.map mkLevelParam) }
"""

_IMP_MORE = """\
import Imp.Alias

namespace Imp

def seed : Nat := 2

private theorem privSeed : seed = 2 := aux
where aux : seed = 2 := rfl

theorem seedEq : seed = 2 := rfl

imp_alias seedAlias := seedEq

theorem seedAgain : seed = 1 + 1 := seedEq

partial def seedLoop (n : Nat) : Nat := if n = 0 then seed else seedLoop (n - 1)

theorem usesAlias : seed = 2 ∧ True := ⟨seedAlias, trivial⟩

@[simp, deprecated seedEq (since := "2026-10-05")]
def oldSeed : Nat := 2

structure Box where
  v : Nat

end Imp
"""


def _imp_project(root: Path) -> Path:
    """A built Lean project whose glob selects a module its root never imports."""

    lean_root = root / "lean"
    (lean_root / "Imp").mkdir(parents=True)
    shutil.copy(_SKELETON_FIXTURE / "lean-toolchain", lean_root / "lean-toolchain")
    (lean_root / "lakefile.toml").write_text(
        'name = "Imp"\ndefaultTargets = ["Imp"]\n\n[[lean_lib]]\nname = "Imp"\nglobs = ["Imp.*"]\n',
        encoding="utf-8",
    )
    (lean_root / "Imp.lean").write_text("import Imp.Basic\n", encoding="utf-8")
    (lean_root / "Imp" / "Basic.lean").write_text(_IMP_BASIC, encoding="utf-8")
    (lean_root / "Imp" / "Extra.lean").write_text(_IMP_EXTRA, encoding="utf-8")
    (lean_root / "Imp" / "Alias.lean").write_text(_IMP_ALIAS, encoding="utf-8")
    (lean_root / "Imp" / "More.lean").write_text(_IMP_MORE, encoding="utf-8")
    build = subprocess.run(["lake", "build"], cwd=lean_root, capture_output=True, text=True, timeout=600, check=False)
    assert build.returncode == 0, build.stdout + build.stderr
    return lean_root


def _imp_roadmap(root: Path) -> Path:
    project = root / "project"
    _write_article(project, "README.md", title="Chapter", metadata=["article_id: af_0000000000000000000000c0"])
    for name, declaration, lean, depends in (
        ("base.md", "definition", "Imp.base", ()),
        ("uses.md", "theorem", "Imp.uses", ("base.md",)),
        ("simp.md", "theorem", "Imp.usesSimp", ()),
        ("proved.md", "theorem", "Imp.proved", ("uses.md",)),
        ("apart.md", "theorem", "Imp.apart", ()),
    ):
        metadata = [f"declaration: {declaration}", "statement: formalized", f"lean: {lean}"]
        _write_article(project, name, metadata=metadata, depends=depends)
    return project


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_the_probe_reads_a_built_project(tmp_path: Path, monkeypatch, capsys) -> None:
    lean_root = _imp_project(tmp_path)
    project = _imp_roadmap(tmp_path)
    source_revision, _ = work_context(project, "chapter/base")
    outputs: list[str] = []
    run_probe = skeleton.run_probe

    def recorded(*args: object, **kwargs: object) -> str:
        outputs.append(run_probe(*args, **kwargs))
        return outputs[-1]

    monkeypatch.setattr("autoform_cli.skeleton.run_probe", recorded)

    code = cli.main(["work", "impact", "chapter/base", str(project), "--lean-root", str(lean_root), "--json"])

    output = capsys.readouterr()
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["declarations"] == ["Imp.base"]
    assert report["contained"] is False
    # Imp.Extra is reached only through the glob. `simp` closes usesSimp, but
    # its statement mentions the definition P, whose value mentions base.
    assert [(item["id"], item["declarations"]) for item in report["statement_impacted"]] == [
        ("chapter/simp", ["Imp.usesSimp"]),
        ("chapter/uses", ["Imp.uses"]),
    ]
    assert [(item["id"], item["declarations"]) for item in report["proof_impacted"]] == [
        ("chapter/proved", ["Imp.proved"])
    ]
    assert [(h["name"], h["kind"], h["impact"], h["path"], h["line"], h["owner"]) for h in report["helpers"]] == [
        ("Imp.P", "def", "statement", "Imp/Basic.lean", 9, None),
        ("Imp.P_iff", "theorem", "statement", "Imp/Basic.lean", 11, None),
        ("Imp.base_eq", "theorem", "statement", "Imp/Basic.lean", 5, None),
        ("Imp.oldEq", "theorem", "statement", "Imp/Basic.lean", 14, None),
        ("Imp.oldUnused", "theorem", "statement", "Imp/Basic.lean", 17, None),
        ("Imp.usesOld", "theorem", "statement", "Imp/Extra.lean", 10, None),
    ]
    assert report["undeclared_dependencies"] == ["chapter/simp"]
    # The equation lemma `@[simp]` gives oldSeed goes when oldSeed does.
    assert report["deprecated"] == [
        {"name": "Imp.oldEq", "replacement": "Imp.base_eq", "users": ["Imp.usesOld"], "articles": []},
        {"name": "Imp.oldSeed", "replacement": "Imp.seedEq", "users": [], "articles": []},
        {"name": "Imp.oldUnused", "replacement": "Imp.base_eq", "users": [], "articles": []},
    ]
    assert report["deprecated_unused"] == ["Imp.oldSeed", "Imp.oldUnused"]
    unowned = sorted(_lean_key(h["name"]) for h in report["helpers"])
    assert [h["claim_target"] for h in report["helpers"]] == [_lean_key(h["name"]) for h in report["helpers"]]
    assert report["claim_targets"] == ["chapter/base", "chapter/proved", "chapter/simp", "chapter/uses", *unowned]
    assert report["source_revision"] == source_revision

    (probed,) = outputs
    records = parse_impact_output(probed)
    assert {record.module for record in records.values()} == {"Imp.Alias", "Imp.Basic", "Imp.Extra", "Imp.More"}
    simp_lemma = next(record for record in records.values() if record.parent == "Imp.P_iff")
    assert simp_lemma.internal and simp_lemma.type_uses == ("Imp.P",)
    assert records["Imp.usesOld"].uses_deprecated == ("Imp.oldEq",)

    # The same records answer the other questions without another probe run.
    articles = [
        ImpactArticle("chapter/base", None, ("Imp.base",)),
        ImpactArticle("chapter/uses", None, ("Imp.uses",), ("chapter/base",)),
        ImpactArticle("chapter/simp", None, ("Imp.usesSimp",)),
        ImpactArticle("chapter/proved", None, ("Imp.proved",), ("chapter/uses",)),
        ImpactArticle("chapter/apart", None, ("Imp.apart",)),
    ]
    simp_only = compute_impact(records, articles, articles[0], ["Imp.P_iff"], source_revision="rev")
    assert simp_only.statement_impacted == ()
    assert _ids(simp_only.proof_impacted) == ["chapter/simp"]
    assert simp_only.helpers == ()
    assert not simp_only.contained
    apart = compute_impact(records, articles, articles[4], ["Imp.apart"], source_revision="rev")
    assert apart.contained
    assert apart.claim_targets == ("chapter/apart",)

    # Articles name a private theorem as its source does; its `where` helper
    # is owned through the private name.
    priv = "_private.Imp.More.0.Imp.privSeed"
    assert records[priv].user_name == "Imp.privSeed"
    assert records[f"{priv}.aux"].parent == priv
    more = [
        ImpactArticle("chapter/seed", None, ("Imp.seed",)),
        ImpactArticle("chapter/priv", None, ("Imp.privSeed",)),
        ImpactArticle("chapter/seed-eq", None, ("Imp.seedEq",)),
        ImpactArticle("chapter/uses-alias", None, ("Imp.usesAlias",)),
        ImpactArticle("chapter/box", None, ("Imp.Box",)),
        ImpactArticle("chapter/loop", None, ("Imp.seedLoop",)),
    ]
    seed = compute_impact(records, more, more[0], ["Imp.seed"], source_revision="rev")
    # A partial def's kernel value is an `Inhabited` witness; its body is in
    # the `_unsafe_rec` companion.
    assert "Imp.seedLoop._unsafe_rec" in records["Imp.seedLoop"].value_uses
    assert _ids(seed.statement_impacted) == ["chapter/loop", "chapter/priv", "chapter/seed-eq", "chapter/uses-alias"]
    assert [(helper.name, helper.owner) for helper in seed.helpers] == [
        ("Imp.seedAgain", None),
        ("Imp.seedAlias", None),
        (f"{priv}.aux", "chapter/priv"),
    ]
    assert compute_impact(records, more, more[1], ["Imp.privSeed"], source_revision="rev").contained
    # The alias's type copies seedEq's without mentioning it.
    assert records["Imp.seedAlias"].alias_of == "Imp.seedEq"
    assert "Imp.seedEq" not in records["Imp.seedAlias"].type_uses
    # A theorem whose type only unfolds to its target's states its own type.
    assert records["Imp.seedAgain"].alias_of is None
    assert records["Imp.seedAgain"].value_uses == ("Imp.seedEq",)
    seed_eq = compute_impact(records, more, more[2], ["Imp.seedEq"], source_revision="rev")
    assert seed_eq.statement_impacted == ()
    assert _ids(seed_eq.proof_impacted) == ["chapter/uses-alias"]
    assert [(helper.name, helper.impact) for helper in seed_eq.helpers] == [
        ("Imp.seedAgain", "proof"),
        ("Imp.seedAlias", "statement"),
    ]
    # A structure's generated companions belong to its own article.
    box = compute_impact(records, more, more[4], ["Imp.Box"], source_revision="rev")
    assert box.contained
    assert box.helpers
    assert {helper.owner for helper in box.helpers} == {"chapter/box"}
