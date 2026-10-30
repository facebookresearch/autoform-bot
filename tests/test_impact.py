from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import autoform_cli.lean as lean_module
from autoform_cli import __main__ as cli, claims, lean_probe, runtime
from autoform_cli import _tree_snapshot as tree_snapshot_module
from autoform_cli._tree_snapshot import TreeChangedError
from autoform_cli.impact import (
    IMPACT_MARKER,
    IMPACT_MODULE_MARKER,
    IMPACT_SCHEMA,
    MODULE_IMPORT_IMPACT_SCHEMA,
    ConstantRecord,
    ImpactArticle,
    ImpactError,
    ModuleEvidence,
    compute_impact,
    compute_module_import_impact,
    format_impact,
    format_module_import_impact,
    import_closure,
    module_import_impact,
    parse_impact_output,
    parse_module_evidence,
    project_modules,
    render_impact_probe,
    revision_impact,
    _module_source_paths,
)
from autoform_cli.lean import snapshot_project_sources
from autoform_cli.lean_probe import LeanLibrary, SkeletonError, lean_libraries
from autoform_cli.work import work_context
from tests.conftest import _SKELETON_FIXTURE, _lean_toolchain_available


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
    node_id: str,
    *declarations: str,
    article_id: str | None = None,
    dependencies: tuple[str, ...] = (),
    statement_dependencies: tuple[str, ...] = (),
    stated: bool = False,
    mathlib: bool = False,
) -> ImpactArticle:
    return ImpactArticle(node_id, article_id, declarations, dependencies, statement_dependencies, stated, mathlib)


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


def test_module_import_impact_reports_reverse_import_scope_without_blocking() -> None:
    records = {
        "Demo.Base.value": _rec("Demo.Base.value", module="Demo.Base"),
        "Demo.Base.helper": _rec("Demo.Base.helper", module="Demo.Base"),
        "Demo.Use.result": _rec("Demo.Use.result", module="Demo.Use"),
        "Demo.Down.result": _rec("Demo.Down.result", module="Demo.Down"),
        "Demo.Other.result": _rec("Demo.Other.result", module="Demo.Other"),
    }
    modules = {
        "Demo.Base": ModuleEvidence("Demo.Base", "/project/Demo/Base.lean"),
        "Demo.Use": ModuleEvidence("Demo.Use", "/project/Demo/Use.lean", ("Demo.Base",)),
        "Demo.Down": ModuleEvidence("Demo.Down", "/project/Demo/Down.lean", ("Demo.Use",)),
        "Demo.Other": ModuleEvidence("Demo.Other", "/project/Demo/Other.lean"),
    }
    articles = (
        ImpactArticle("base", "af_base", ("Demo.Base.value",)),
        ImpactArticle("use", "af_use", ("Demo.Use.result",)),
        ImpactArticle("down", None, ("Demo.Down.result",)),
        ImpactArticle("other", None, ("Demo.Other.result",)),
    )

    report = compute_module_import_impact(
        records,
        modules,
        articles,
        articles[0],
        "Demo.Base",
        source_revision="roadmap",
        lean_source_revision="sources",
    )

    assert report.affected_modules == ("Demo.Base", "Demo.Down", "Demo.Use")
    assert [(article.id, article.modules) for article in report.affected_articles] == [
        ("base", ("Demo.Base",)),
        ("down", ("Demo.Down",)),
        ("use", ("Demo.Use",)),
    ]
    assert report.claim_targets == ("af_base", report.module_claim_target)
    assert [
        (helper.name, helper.module, helper.claim_target, helper.claim_key)
        for helper in report.affected_unowned_helpers
    ] == [
        (
            "Demo.Base.helper",
            "Demo.Base",
            _lean_key("Demo.Base.helper"),
            claims.author_claim_key(_lean_key("Demo.Base.helper")),
        )
    ]
    assert report.affected_claim_targets == ("af_use", "down", _lean_key("Demo.Base.helper"))
    assert report.module_claim_key == claims.author_claim_key(report.module_claim_target)
    assert report.claim_keys == tuple(claims.author_claim_key(target) for target in report.claim_targets)
    assert report.affected_claim_keys == tuple(
        claims.author_claim_key(target) for target in report.affected_claim_targets
    )
    assert report.module_claim_target == "lean-module/demo-base-" + hashlib.sha256(b"Demo.Base").hexdigest()[:16]
    assert report.article_local is False
    assert report.as_dict()["schema"] == MODULE_IMPORT_IMPACT_SCHEMA
    assert re.fullmatch(r"[0-9a-f]{64}", report.module_graph_revision)
    changed_graph = compute_module_import_impact(
        records,
        {**modules, "Demo.Down": ModuleEvidence("Demo.Down", "/project/Demo/Down.lean")},
        articles,
        articles[0],
        "Demo.Base",
        source_revision="roadmap",
    )
    assert changed_graph.module_graph_revision != report.module_graph_revision
    formatted = format_module_import_impact(report)
    assert formatted[0] == "Import edit allowed in Demo.Base for base [af_base]."
    assert f"Module graph revision: {report.module_graph_revision}" in formatted
    assert "Module scope: loaded-root-library-graph" in formatted
    assert formatted[-1] == "Claim targets of the import edit: " + ", ".join(
        f"{target} (key {key})"
        for target, key in zip(report.claim_targets, report.claim_keys)
    )

    with pytest.raises(ImpactError, match="not a loaded project module: Missing"):
        compute_module_import_impact(
            records,
            modules,
            articles,
            articles[0],
            "Missing",
            source_revision="roadmap",
        )


def test_module_import_impact_reports_unresolved_local_articles_as_unknown_scope() -> None:
    records = {"Demo.value": _rec("Demo.value")}
    modules = {"Demo": ModuleEvidence("Demo", "/project/Demo.lean")}
    articles = (
        ImpactArticle("selected", None, ("Demo.value",)),
        ImpactArticle("unknown", "af_unknown", ("Demo.missing",)),
        ImpactArticle("mathlib", None, ("Nat.add_zero",), mathlib=True),
    )

    report = compute_module_import_impact(
        records,
        modules,
        articles,
        articles[0],
        "Demo",
        source_revision="roadmap",
    )

    assert [(article.id, article.declarations) for article in report.unresolved_articles] == [
        ("unknown", ("Demo.missing",))
    ]
    assert report.affected_claim_targets == ()
    assert report.article_local is False
    assert "Affected built articles: none beyond the selected article." in format_module_import_impact(
        report
    )

    empty = compute_module_import_impact(
        {},
        modules,
        (ImpactArticle("selected", None, ()),),
        ImpactArticle("selected", None, ()),
        "Demo",
        source_revision="roadmap",
    )
    assert "Affected built articles: none in the loaded reverse-import scope." in (
        format_module_import_impact(empty)
    )


def test_module_import_impact_cuts_a_long_module_slug_but_keeps_its_digest() -> None:
    module = "Demo.ThisModuleNameRunsPastTheFortyEightCharacterSlugLimit"
    modules = {module: ModuleEvidence(module, "/project/Demo/Long.lean")}
    selected = ImpactArticle("chapter/sel", None, ())

    report = compute_module_import_impact({}, modules, (selected,), selected, module, source_revision="rev")

    # The slug keeps 48 characters; the digest of the full name tells long names apart.
    digest = hashlib.sha256(module.encode()).hexdigest()[:16]
    assert report.module_claim_target == f"lean-module/demo-thismodulenamerunspastthefortyeightcharacte-{digest}"


def test_module_import_impact_lists_articles_through_the_helpers_they_own() -> None:
    # chapter/x names the structure; its API lemma lives in the edited module,
    # so x is affected through the lemma it owns, not through a name.
    records = {
        "Demo.A.Foo": _rec("Demo.A.Foo", "inductive", module="Demo.A"),
        "Demo.A.Foo.lemma": _rec("Demo.A.Foo.lemma", module="Demo.B", parent="Demo.A.Foo"),
        "Demo.B.main": _rec("Demo.B.main", module="Demo.B"),
    }
    modules = {
        "Demo.A": ModuleEvidence("Demo.A", "/project/Demo/A.lean"),
        "Demo.B": ModuleEvidence("Demo.B", "/project/Demo/B.lean", ("Demo.A",)),
    }
    articles = (
        ImpactArticle("chapter/sel", None, ("Demo.B.main",)),
        ImpactArticle("chapter/x", "af_x", ("Demo.A.Foo",)),
    )

    report = compute_module_import_impact(records, modules, articles, articles[0], "Demo.B", source_revision="rev")

    assert [(article.id, article.modules) for article in report.affected_articles] == [
        ("chapter/sel", ("Demo.B",)),
        ("chapter/x", ("Demo.B",)),
    ]
    assert report.affected_claim_targets == ("af_x",)
    assert report.affected_unowned_helpers == ()
    assert report.article_local is False
    assert "  chapter/x [af_x]: Demo.B" in format_module_import_impact(report)


def test_module_import_impact_is_not_article_local_with_an_unowned_helper() -> None:
    records = {
        "Demo.main": _rec("Demo.main"),
        "Demo.helper": _rec("Demo.helper"),
    }
    modules = {"Demo": ModuleEvidence("Demo", "/project/Demo.lean")}
    selected = ImpactArticle("chapter/sel", None, ("Demo.main",))

    report = compute_module_import_impact(records, modules, (selected,), selected, "Demo", source_revision="rev")
    assert report.affected_claim_targets == (_lean_key("Demo.helper"),)
    assert report.article_local is False

    alone = compute_module_import_impact(
        {"Demo.main": records["Demo.main"]}, modules, (selected,), selected, "Demo", source_revision="rev"
    )
    assert alone.article_local is True
    assert (
        "Article scope: no other article and no unowned helper is in the reverse-import scope."
        in format_module_import_impact(alone)
    )


def test_import_closure_scope_owns_records_by_name_prefix_even_without_the_parent_loaded() -> None:
    # Demo.Foo is declared in a module the narrowed probe did not load, so the
    # lemma's loaded parent is missing; the article naming Demo.Foo still owns it.
    records = {
        "Demo.Foo.lemma": _rec("Demo.Foo.lemma", module="Demo.B"),
        "Demo.B.main": _rec("Demo.B.main", module="Demo.B"),
    }
    modules = {"Demo.B": ModuleEvidence("Demo.B", "/project/Demo/B.lean")}
    articles = (
        ImpactArticle("chapter/sel", None, ("Demo.B.main",)),
        ImpactArticle("chapter/x", "af_x", ("Demo.Foo",)),
    )

    report = compute_module_import_impact(
        records, modules, articles, articles[0], "Demo.B", source_revision="rev", module_scope="import-closure"
    )

    assert [article.id for article in report.affected_articles] == ["chapter/sel", "chapter/x"]
    # Helpers keep the default parent-chain rule, so the default scope's helper
    # claim key for the parentless lemma is still reported.
    default = compute_module_import_impact(records, modules, articles, articles[0], "Demo.B", source_revision="rev")
    assert [helper.name for helper in default.affected_unowned_helpers] == ["Demo.Foo.lemma"]
    assert report.affected_unowned_helpers == default.affected_unowned_helpers
    assert report.as_dict()["module_scope"] == "import-closure"
    assert "Module scope: import-closure" in format_module_import_impact(report)


def test_import_closure_scope_is_never_article_local() -> None:
    records = {"Demo.main": _rec("Demo.main")}
    modules = {"Demo": ModuleEvidence("Demo", "/project/Demo.lean")}
    selected = ImpactArticle("chapter/sel", None, ("Demo.main",))

    report = compute_module_import_impact(
        records, modules, (selected,), selected, "Demo", source_revision="rev", module_scope="import-closure"
    )

    assert report.selected_affected is True
    assert report.article_local is False


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
    assert [(helper.name, helper.kind, helper.impact, helper.owners) for helper in report.helpers] == [
        ("A.Shape.mk", "constructor", "statement", ("shape",)),
        ("A.Shape.rec", "recursor", "statement", ("shape",)),
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
            "owners": ["shared-a", "shared-b"],
            "claim_targets": ["shared-a", "shared-b"],
        },
        {
            "name": "A.uses.aux",
            "kind": "theorem",
            "impact": "statement",
            "module": "Demo",
            "path": "Demo.lean",
            "line": 2,
            "owners": ["uses"],
            "claim_targets": ["uses"],
        },
        {
            "name": "A.uses.aux.deep",
            "kind": "theorem",
            "impact": "proof",
            "module": "Demo",
            "path": "Demo.lean",
            "line": 3,
            "owners": ["uses"],
            "claim_targets": ["uses"],
        },
        {
            "name": "_private.Demo.Extra.0.A.priv",
            "kind": "theorem",
            "impact": "proof",
            "module": "Demo.Extra",
            "path": None,
            "line": None,
            "owners": [],
            "claim_targets": [_lean_key("_private.Demo.Extra.0.A.priv")],
        },
    ]
    assert located == [helper.name for helper in report.helpers]
    assert _ids(report.statement_impacted) == ["uses"]
    # The helpers are repaired under their owners' claims, so shared-a is
    # claimed too, and the unowned helper under a key of its own.
    assert report.claim_targets == (
        "base",
        _lean_key("_private.Demo.Extra.0.A.priv"),
        "shared-a",
        "shared-b",
        "uses",
    )


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


def test_stated_markdown_statement_dependents_the_lean_does_not_show_are_reported() -> None:
    records = _records(
        _rec("A.base", "def"),
        _rec("A.direct", type_uses=("A.base",)),
        _rec("A.inlined"),
        _rec("A.proofOnly", value_uses=("A.base",)),
        _rec("A.unstated"),
        _rec("A.viaProof"),
    )

    def stated(node_id: str, *declarations: str, on: str = "base", **fields: object) -> ImpactArticle:
        return _article(node_id, *declarations, dependencies=(on,), statement_dependencies=(on,), stated=True, **fields)

    articles = [
        _article("base", "A.base", article_id="af_base", stated=True),
        stated("direct", "A.direct"),
        # Unstated and named by no article: it only carries the statement edge.
        _article("middle", dependencies=("base",), statement_dependencies=("base",)),
        stated("inlined", "A.inlined", on="middle", article_id="af_inlined"),
        stated("proof-only", "A.proofOnly"),
        _article("unstated", "A.unstated", dependencies=("base",), statement_dependencies=("base",)),
        _article("via-proof", "A.viaProof", dependencies=("base",), stated=True),
        # Stated by Mathlib, whose declaration cannot use A.base.
        stated("in-mathlib", mathlib=True),
    ]

    report = _impact(records, articles, "base")

    assert _ids(report.statement_impacted) == ["direct"]
    assert _ids(report.proof_impacted) == ["proof-only"]
    assert report.unused_statement_dependencies == ("inlined", "proof-only")
    assert report.as_dict()["unused_statement_dependencies"] == ["inlined", "proof-only"]
    assert report.claim_targets == ("af_base", "af_inlined", "direct", "proof-only")
    assert "Statement dependents in Markdown that are not statement impacted: inlined, proof-only" in (
        format_impact(report)
    )


def test_an_unused_statement_dependency_alone_keeps_a_revision_from_being_contained() -> None:
    records = _records(_rec("A.leaf", "def"), _rec("A.inlined"))
    articles = [
        _article("leaf", "A.leaf", stated=True),
        _article("inlined", "A.inlined", dependencies=("leaf",), statement_dependencies=("leaf",), stated=True),
    ]

    report = _impact(records, articles, "leaf")

    assert not report.contained
    assert report.claim_targets == ("leaf", "inlined")
    lines = format_impact(report)
    assert lines.pop(2) == "Lean source revision: unbound"
    assert re.fullmatch(r"Lean build revision: [0-9a-f]{64}", lines.pop(2))
    assert lines == [
        "Revising A.leaf of leaf",
        "Graph source revision: rev",
        "Statement dependents in Markdown that are not statement impacted: inlined",
        "Claim targets: leaf, inlined",
    ]


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
    lines = format_impact(report)
    assert lines.pop(2) == "Lean source revision: unbound"
    assert re.fullmatch(r"Lean build revision: [0-9a-f]{64}", lines.pop(2))
    assert lines == [
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
    assert [(helper.name, helper.owners) for helper in report.helpers] == [
        (f"{privT}.aux", ("priv",))
    ]
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
    assert [(helper.name, helper.owners) for helper in report.helpers] == [
        (f"{privT}.aux", ("priv",))
    ]
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
    assert [(helper.name, helper.owners) for helper in owned.helpers] == [
        ("A.S.mk", ("s",)),
        ("A.S.rec", ("s",)),
    ]
    assert owned.claim_targets == ("s",)
    assert format_impact(owned)[4] == "Contained: nothing outside s uses A.S, so it can be revised in place."
    assert not shared.contained
    assert [(helper.name, helper.owners) for helper in shared.helpers] == [
        ("A.T.aux", ("other", "t")),
        ("A.loose", ()),
    ]
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
    assert [(helper.name, helper.owners, helper.claim_targets) for helper in left.helpers] == [
        ("A.T.aux", ("t",), ("af_t",)),
        ("A.bridge", (), (key,)),
    ]
    assert [(helper.name, helper.claim_targets) for helper in right.helpers] == [
        ("A.bridge", (key,))
    ]
    assert left.claim_targets == ("af_left", "af_t", key)
    assert right.claim_targets == ("af_right", key)
    assert not right.contained
    assert json.loads(right.to_json())["helpers"][0]["claim_targets"] == [key]


def test_a_revised_declaration_owned_by_another_article_claims_that_article() -> None:
    records = _records(
        _rec("A.R", "def"),
        _rec("A.T", "inductive"),
        _rec("A.T.aux", parent="A.T"),
    )
    articles = [
        _article("r", "A.R", article_id="af_r"),
        _article("t", "A.T", article_id="af_t"),
    ]

    report = _impact(records, articles, "r", ["A.T.aux"])

    assert report.declarations == ("A.T.aux",)
    assert report.helpers == ()
    assert report.claim_targets == ("af_r", "af_t")
    assert not report.contained


def test_revisions_of_one_unowned_declaration_contend_for_its_claim() -> None:
    records = _records(
        _rec("A.R", "def"),
        _rec("A.S", "def"),
        _rec("A.loose", "def"),
    )
    articles = [
        _article("r", "A.R", article_id="af_r"),
        _article("s", "A.S", article_id="af_s"),
    ]

    from_r = _impact(records, articles, "r", ["A.loose"])
    from_s = _impact(records, articles, "s", ["A.loose"])

    key = _lean_key("A.loose")
    assert from_r.claim_targets == ("af_r", key)
    assert from_s.claim_targets == ("af_s", key)
    assert not from_r.contained
    assert not from_s.contained


def test_declaration_override_claims_every_owner_of_the_revised_declaration() -> None:
    records = _records(
        _rec("A.R", "def"),
        _rec("A.R.aux", parent="A.R"),
        _rec("A.Shared", "inductive"),
        _rec("A.Shared.aux", parent="A.Shared"),
    )
    articles = [
        _article("r", "A.R", "A.Shared", article_id="af_r"),
        _article("shared", "A.Shared", article_id="af_shared"),
    ]

    report = _impact(records, articles, "r", ["A.Shared.aux"])
    own = _impact(records, articles, "r", ["A.R.aux"])

    # The override replaces r's normal declarations, but the roadmap still
    # says both articles own the revised declaration's nearest named parent.
    assert report.declarations == ("A.Shared.aux",)
    assert report.statement_impacted == ()
    assert report.helpers == ()
    assert report.claim_targets == ("af_r", "af_shared")
    assert not report.contained
    assert own.claim_targets == ("af_r",)
    assert own.contained


def test_markdown_paths_count_from_every_article_a_revised_declaration_belongs_to() -> None:
    records = _records(
        _rec("A.R", "def"),
        _rec("A.X", "def"),
        _rec("A.O", "def"),
        _rec("A.O.aux", "def", parent="A.O"),
        _rec("A.loose", "def"),
        _rec("A.useX", type_uses=("A.X",)),
        _rec("A.inlinesX"),
        _rec("A.inlinesAux"),
    )

    def stated(node_id: str, *declarations: str, on: str) -> ImpactArticle:
        return _article(node_id, *declarations, dependencies=(on,), statement_dependencies=(on,), stated=True)

    articles = [
        _article("r", "A.R"),
        _article("t", "A.X", stated=True),
        _article("o-a", "A.O"),
        _article("o-b", "A.O"),
        _article("uses-x", "A.useX", dependencies=("t",)),
        stated("inlines-x", "A.inlinesX", on="t"),
        stated("inlines-aux", "A.inlinesAux", on="o-b"),
    ]

    named = _impact(records, articles, "r", ["A.X"])
    owned = _impact(records, articles, "r", ["A.O.aux"])
    unowned = _impact(records, articles, "r", ["A.loose"])

    # A.X belongs to t, so t needs no path to r, a path to t declares uses-x,
    # and inlines-x rests on A.X through t although its Lean shows no use.
    assert _ids(named.statement_impacted) == ["t", "uses-x"]
    assert named.undeclared_dependencies == ()
    assert named.unused_statement_dependencies == ("inlines-x",)
    assert named.claim_targets == ("r", "inlines-x", "t", "uses-x")
    # The unnamed A.O.aux belongs to both of A.O's articles.
    assert owned.unused_statement_dependencies == ("inlines-aux",)
    assert owned.claim_targets == ("r", "inlines-aux", "o-a", "o-b")
    # A.loose belongs to no article, so only paths to r count.
    assert unowned.unused_statement_dependencies == ()
    assert unowned.claim_targets == ("r", _lean_key("A.loose"))


def test_an_unowned_helper_claim_key_is_ref_safe_for_any_name() -> None:
    names = ("_private.Demo.Extra.0.A.priv", "A.«weird name»", "«∀»", "A." + "long" * 20)
    records = _records(_rec("A.base", "def"), *(_rec(name, type_uses=("A.base",)) for name in names))
    articles = [_article("base", "A.base")]

    report = _impact(records, articles, "base")

    keys = {helper.name: helper.claim_targets[0] for helper in report.helpers}
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


def _module_line(
    module: str,
    *imports: str,
    source_path: str | None = None,
    olean_path: str | None = None,
) -> str:
    return IMPACT_MODULE_MARKER + json.dumps(
        {
            "module": module,
            "source_path": source_path or f"/project/{module.replace('.', '/')}.lean",
            "olean_path": olean_path or f"/project/.lake/build/lib/lean/{module.replace('.', '/')}.olean",
            "direct_local_imports": list(imports),
        }
    )


def test_probe_output_breaks_lines_only_at_newlines() -> None:
    # Lean's JSON printer leaves U+0085, U+2028 and U+2029 unescaped, and
    # `str.splitlines` would split a record at each of them.
    name = "A.«b\x85c\u2028d\u2029e»"
    module = {
        "module": "Demo",
        "source_path": "/project/Demo\u2028.lean",
        "olean_path": "/project/.lake/build/lib/lean/Demo.olean",
        "direct_local_imports": [],
    }
    text = "\n".join(
        [
            IMPACT_MARKER + json.dumps(_payload(name), ensure_ascii=False),
            IMPACT_MODULE_MARKER + json.dumps(module, ensure_ascii=False),
        ]
    )

    assert list(parse_impact_output(text)) == [name]
    assert parse_module_evidence(text)["Demo"].source_path == "/project/Demo\u2028.lean"


def test_probe_output_is_read_from_marker_lines() -> None:
    text = "\n".join(
        [
            "warning: unrelated Lean output",
            _module_line("A", "A.Base"),
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


def test_module_evidence_exposes_a_closed_direct_local_import_graph() -> None:
    text = "\n".join(
        [
            _line(_payload("A.Base.value", module="A.Base")),
            _module_line("A", "A.Base", "A.Util"),
            "warning: unrelated Lean output",
            _module_line("A.Base"),
            _module_line("A.Util", "A.Base"),
        ]
    )

    evidence = parse_module_evidence(text)

    assert evidence == {
        "A": ModuleEvidence(
            "A",
            "/project/A.lean",
            ("A.Base", "A.Util"),
            "/project/.lake/build/lib/lean/A.olean",
        ),
        "A.Base": ModuleEvidence(
            "A.Base",
            "/project/A/Base.lean",
            olean_path="/project/.lake/build/lib/lean/A/Base.olean",
        ),
        "A.Util": ModuleEvidence(
            "A.Util",
            "/project/A/Util.lean",
            ("A.Base",),
            "/project/.lake/build/lib/lean/A/Util.olean",
        ),
    }
    # The existing constant parser remains compatible with the mixed stream.
    assert parse_impact_output(text) == {
        "A.Base.value": ConstantRecord(name="A.Base.value", kind="theorem", module="A.Base")
    }


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ([IMPACT_MODULE_MARKER + "{"], "invalid module-evidence JSON"),
        (
            [
                IMPACT_MODULE_MARKER
                + json.dumps(
                    {
                        "module": "A",
                        "source_path": "/project/A.lean",
                        "olean_path": "/project/.lake/build/lib/lean/A.olean",
                        "direct_local_imports": [],
                        "extra": 1,
                    }
                )
            ],
            "module evidence with unexpected fields",
        ),
        (
            [
                IMPACT_MODULE_MARKER
                + json.dumps(
                    {
                        "module": "A",
                        "source_path": "/project/A.lean",
                        "olean_path": "/project/.lake/build/lib/lean/A.olean",
                        "direct_local_imports": "B",
                    }
                )
            ],
            "malformed module evidence",
        ),
        ([_module_line("A", "B", "B")], "duplicate direct imports"),
        ([_module_line("A"), _module_line("A")], "module evidence for A twice"),
        (["no module records here"], "emitted no project-module evidence"),
        ([_module_line("A", "A")], "reported A importing itself"),
        ([_module_line("A", "B")], r"A importing unknown local modules: \['B'\]"),
    ],
)
def test_malformed_module_evidence_fails_closed(lines: list[str], message: str) -> None:
    with pytest.raises(SkeletonError, match=message):
        parse_module_evidence("\n".join(lines))


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


def _glob_walks(probe: str) -> object:
    """The JSON glob walks a rendered impact probe decodes."""

    literal = re.search(r'AutoformImpact\.globDirectories ("(?:[^"\\]|\\.)*")', probe)
    assert literal is not None
    return json.loads(json.loads(literal.group(1)))


def test_rendered_probe_imports_modules_and_names_exact_local_modules() -> None:
    source = render_impact_probe(
        imports=["Demo.B", "Demo", "Demo.B"],
        project_modules=["Internal", "Demo"],
    )

    assert source.startswith("import Demo\nimport Demo.B\n-- Autoform impact probe.")
    # The module names reach Lean as one JSON string, not a list literal.
    assert f"AutoformImpact.requestedNames {lean_probe.lean_requests(('Demo', 'Internal'))}" in source
    assert ".isPrefixOf" not in source
    assert f'"{lean_probe.PROBE_OUTPUT_ENV}"' in source
    assert IMPACT_MARKER in source
    assert IMPACT_MODULE_MARKER in source
    assert "env.header.moduleData[moduleIdx.toNat]!.imports" in source
    assert "findOLean module" in source
    # Neither the inventory checks nor module evidence run by default.
    assert source.count("if false then") == 2
    assert _glob_walks(source) == []
    # The root artifact directory turns on the inventory checks, with the walk
    # of each `M.*` or `M.+` glob's directory, and module evidence is separate.
    checked = render_impact_probe(
        imports=["Demo"],
        project_modules=["Demo"],
        artifact_dir=Path("/lean/.lake/build/lib/lean"),
        glob_directories=[(Path("/lean/Demo"), "Demo", "Demo.*")],
    )
    assert (checked.count("if true then"), checked.count("if false then")) == (1, 1)
    assert 'IO.FS.realPath "/lean/.lake/build/lib/lean"' in checked
    assert _glob_walks(checked) == [[str(Path("/lean/Demo")), ["Demo"], "Demo.*"]]
    evidence = render_impact_probe(
        imports=["Demo"],
        project_modules=["Demo"],
        artifact_dir=Path("/lean/.lake/build/lib/lean"),
        emit_module_evidence=True,
    )
    assert evidence.count("if true then") == 2
    with pytest.raises(SkeletonError, match="no imports"):
        render_impact_probe(imports=[], project_modules=["Demo"])


_SHADOWED_STD = "object file '/deps/Std/Data.olean' of module Std.Data does not exist"
#: The impact probe's two refusals of a root-package module the inventory lacks.
_UNCAPTURED = (
    "the impact probe loaded root-package module Demo.X from /lean/Demo/X.olean, but the inventory lacks it",
    "the impact probe found root-package module Demo.build.X under glob Demo.*, but the inventory lacks it",
)


@pytest.mark.parametrize(
    ("stderr", "message"),
    [
        ("unknown module", "the {label} failed; is the project built with `lake build`?\nunknown module"),
        (
            _SHADOWED_STD,
            "the {label} cannot load toolchain module Std.Data: a dependency library probably provides modules "
            "under `Std`, which hides the toolchain's own `Std`; rename that library's modules\n" + _SHADOWED_STD,
        ),
        # The project is built, so these refusals come alone, without the rebuild hint.
        *((f"Probe.lean:131:0: error: {refusal}\nnext line", refusal) for refusal in _UNCAPTURED),
        # Even after more output than a failure reason keeps.
        ("warning: unused\n" * 200 + f"error: {_UNCAPTURED[0]}", _UNCAPTURED[0]),
    ],
)
def test_probe_failures_name_the_impact_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, message: str
) -> None:
    (tmp_path / "lake-manifest.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("autoform_cli.lean_probe.shutil.which", lambda executable: "/bin/lake")
    monkeypatch.setattr("autoform_cli.lean_probe._check_artifacts_fresh", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "autoform_cli.lean_probe._run_bounded_command",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr),
    )
    probe = render_impact_probe(imports=["Demo"], project_modules=["Demo"])

    with pytest.raises(SkeletonError) as impact:
        lean_probe.run_probe(probe, tmp_path, label="impact probe")
    with pytest.raises(SkeletonError) as default:
        lean_probe.run_probe(probe, tmp_path)

    assert impact.value.issues == (message.format(label="impact probe"),)
    assert default.value.issues == (message.format(label="skeleton probe"),)


def test_impact_probe_freshness_messages_never_mention_skeletons(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "autoform_cli.lean_probe._run_bounded_command",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 3, stdout="", stderr="Demo is out of date"),
    )
    probe = render_impact_probe(imports=["Demo"], project_modules=["Demo"])

    def issues(label: str | None) -> tuple[str, ...]:
        with pytest.raises(SkeletonError) as caught:
            lean_probe.run_probe(probe, tmp_path, **({} if label is None else {"label": label}))
        return caught.value.issues

    monkeypatch.setattr("autoform_cli.lean_probe.shutil.which", lambda executable: None)
    no_lake = "lake is not on PATH;"
    assert issues("impact probe") == (f"{no_lake} running the impact probe requires a built Lean project",)
    assert issues(None) == (f"{no_lake} extracting skeletons requires a built Lean project",)
    monkeypatch.setattr("autoform_cli.lean_probe.shutil.which", lambda executable: "/bin/lake")
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


def test_project_modules_accept_more_than_ten_thousand_source_files(tmp_path: Path) -> None:
    # atlas-lean keeps 13,080 .lean files under srcDir "." while its globs select
    # about 3,300 of them; the size of the tree must not refuse the project.
    unselected = [(Path("Unselected") / f"M{index}.lean", b"") for index in range(10_001)]
    snapshot = SimpleNamespace(
        index=SimpleNamespace(root=tmp_path), source_files=[(Path("Demo.lean"), b""), *unselected]
    )

    imports, local = project_modules([_library(tmp_path, "Demo")], snapshot)

    assert imports == ("Demo",)
    assert len(local) == 10_002


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
    _sources(tmp_path / ".lake" / "packages" / "dep", "Demo.External")
    snapshot = snapshot_project_sources(tmp_path)

    # `M` is one module, `M.*` the module and its submodules, `M.+` only its
    # submodules. Locality is every repository-owned source module, independent
    # of target globs and namespace prefixes.
    assert project_modules([_library(src, "Demo", "Demo.Sub.*", "Demo.Extra.+")], snapshot) == (
        ("Demo", "Demo.Extra.X", "Demo.Extra.Y.Z", "Demo.Sub", "Demo.Sub.A", "Demo.Sub.B.C"),
        (
            "Demo",
            "Demo.Extra",
            "Demo.Extra.X",
            "Demo.Extra.Y.Z",
            "Demo.Sub",
            "Demo.Sub.A",
            "Demo.Sub.B.C",
            "Other.Lone",
        ),
    )
    # Without globs, Lake builds the roots.
    assert project_modules([_library(src, roots=("Demo", "Other.Lone"))], snapshot) == (
        ("Demo", "Other.Lone"),
        (
            "Demo",
            "Demo.Extra",
            "Demo.Extra.X",
            "Demo.Extra.Y.Z",
            "Demo.Sub",
            "Demo.Sub.A",
            "Demo.Sub.B.C",
            "Other.Lone",
        ),
    )


@pytest.mark.parametrize(
    ("libraries", "message"),
    [
        ("same-module", "project module Shared maps to more than one source"),
        ("same-source", "maps to more than one module: Sub.Shared, Shared"),
        ("same-source-glob", "maps to more than one module: Sub.Shared, Shared"),
    ],
)
def test_project_modules_refuse_ambiguous_module_source_claims(
    tmp_path: Path, libraries: str, message: str
) -> None:
    if libraries == "same-module":
        first = _sources(tmp_path / "first", "Shared")
        second = _sources(tmp_path / "second", "Shared")
        configured = (
            _library(first, roots=("Shared",)),
            _library(second, roots=("Shared",)),
        )
    else:
        root = _sources(tmp_path, "Sub.Shared")
        configured = (
            # Selected by its root, or by a glob outside the library's roots.
            _library(root, roots=("Sub.Shared",))
            if libraries == "same-source"
            else _library(root, "Sub.Shared", roots=("Other",)),
            _library(root / "Sub", roots=("Shared",)),
        )

    snapshot = snapshot_project_sources(tmp_path)
    project_modules(configured, snapshot)
    with pytest.raises(SkeletonError, match=message):
        project_modules(configured, snapshot, strict_module_ownership=True)


def test_project_modules_accept_a_library_nested_in_another_srcdir(tmp_path: Path) -> None:
    # Demo's srcDir also holds extra/Extra.lean and a stray extra/Demo.lean.
    # Demo selects neither `extra.Extra` nor `extra.Demo`, and Extra does not
    # select `Demo`, so each file is at most one selected module.
    root = _sources(tmp_path, "Demo", "extra.Extra", "extra.Demo")
    configured = (
        _library(root, roots=("Demo",)),
        _library(root / "extra", roots=("Extra",)),
    )

    snapshot = snapshot_project_sources(tmp_path)
    assert project_modules(configured, snapshot, strict_module_ownership=True) == (
        ("Demo", "Extra"),
        ("Demo", "Extra", "extra.Demo", "extra.Extra"),
    )


def test_project_modules_claim_only_modules_lake_can_build(tmp_path: Path) -> None:
    # Root Foo prefixes Foo.Extra, but Lake builds a module under a root only
    # when a glob also matches the root, and Foo.Basic does not match Foo. So
    # Foo/Extra.lean is only the root module of the library nested in Foo/.
    root = _sources(tmp_path, "Foo", "Foo.Basic", "Foo.Extra")
    configured = (
        _library(root, "Foo.Basic", roots=("Foo",)),
        _library(root / "Foo", roots=("Extra",)),
    )

    snapshot = snapshot_project_sources(tmp_path)
    assert project_modules(configured, snapshot, strict_module_ownership=True) == (
        ("Extra", "Foo.Basic"),
        ("Basic", "Extra", "Foo", "Foo.Basic", "Foo.Extra"),
    )
    # Foo.lean is under root Foo too, but no library builds it, so no claim.
    assert _module_source_paths(configured, snapshot, strict=True) == {
        "Foo.Basic": "Foo/Basic.lean",
        "Extra": "Foo/Extra.lean",
    }


def test_project_modules_match_library_roots_by_whole_components(tmp_path: Path) -> None:
    # Root `Demo` selects `Demo.X` but not `DemoTests.DemoTests`, so
    # DemoTests/DemoTests.lean is only the root module of DemoTests.
    root = _sources(tmp_path, "Demo", "DemoTests.DemoTests")
    configured = (
        _library(root, roots=("Demo",)),
        _library(root / "DemoTests", roots=("DemoTests",)),
    )

    snapshot = snapshot_project_sources(tmp_path)
    assert project_modules(configured, snapshot, strict_module_ownership=True) == (
        ("Demo", "DemoTests"),
        ("Demo", "DemoTests", "DemoTests.DemoTests"),
    )


def test_project_modules_accept_two_libraries_selecting_one_file_on_a_shared_srcdir(tmp_path: Path) -> None:
    # A main library and a test library often share srcDir "."; when both select
    # Demo.Util, that is still one module from one file, which Lake builds once.
    root = _sources(tmp_path, "Demo", "Demo.Util", "DemoTest")
    configured = (
        _library(root, roots=("Demo",)),
        _library(root, "Demo.Util", roots=("DemoTest",)),
    )

    snapshot = snapshot_project_sources(tmp_path)
    imports, local = project_modules(configured, snapshot, strict_module_ownership=True)
    assert "Demo.Util" in imports
    assert set(local) == {"Demo", "Demo.Util", "DemoTest"}


def test_project_modules_accept_the_same_stray_file_in_sibling_srcdirs(tmp_path: Path) -> None:
    # No library selects `Scratch`, so Lake builds neither copy and the two
    # files do not compete for one module.
    configured = (
        _library(_sources(tmp_path / "aa", "Demo", "Scratch"), roots=("Demo",)),
        _library(_sources(tmp_path / "bb", "Tools", "Scratch"), roots=("Tools",)),
    )

    snapshot = snapshot_project_sources(tmp_path)
    assert project_modules(configured, snapshot, strict_module_ownership=True) == (
        ("Demo", "Tools"),
        ("Demo", "Scratch", "Tools"),
    )


def test_project_modules_select_from_captured_files_not_empty_directory_metadata(tmp_path: Path) -> None:
    src = _sources(tmp_path, "Demo")
    (tmp_path / "Demo").mkdir()
    snapshot = snapshot_project_sources(tmp_path)

    # An empty submodule directory and a missing one select the same captured
    # modules. This keeps M.* usable with only M.lean without a live is_dir read.
    (tmp_path / "Demo").rmdir()
    assert project_modules([_library(src, "Demo.*")], snapshot) == (("Demo",), ("Demo",))


def test_project_modules_read_a_dotted_file_stem_as_one_component(tmp_path: Path) -> None:
    src = _sources(tmp_path, "Demo", "Demo.Foo")
    # Lake names these Demo.«Foo.Bar» and Demo.«Foo.x»y», not submodules of Demo.Foo.
    for name in ("Foo.Bar.lean", "Foo.x»y.lean"):
        (tmp_path / "Demo" / name).write_text("", encoding="utf-8")
    snapshot = snapshot_project_sources(tmp_path)

    assert project_modules([_library(src, "Demo.Foo.*")], snapshot) == (
        ("Demo.Foo",),
        ("Demo", "Demo.Foo", "Demo.«Foo.Bar»"),
    )


@pytest.mark.parametrize(
    ("glob", "message"),
    [
        ("Demo.**", r"cannot read module glob 'Demo\.\*\*'"),
        ("Demo/Odd", "cannot read module glob 'Demo/Odd'"),
        ("Demo.Missing", "module Demo.Missing has no source file"),
        ("Demo.Missing.*", "module Demo.Missing has no source file"),
        ("Demo.Missing.+", "the Lake configuration selects no modules"),
        ("Demo.Odd.+", r"cannot import .*bad-name\.lean"),
        # Lake builds this one as Demo.«Foo», which `import Demo.«Foo»` cannot name.
        ("Demo.Quoted.+", r"cannot import .*«Foo»\.lean"),
        ("Demo.Unquotable.+", r"cannot import .*x»y\.lean"),
        ("Demo.Empty.+", "the Lake configuration selects no modules"),
    ],
)
def test_project_modules_fail_closed(tmp_path: Path, glob: str, message: str) -> None:
    src = _sources(tmp_path, "Demo", "Demo.Odd.Fine")
    (tmp_path / "Demo" / "Odd" / "bad-name.lean").write_text("", encoding="utf-8")
    for name in ("Quoted/«Foo».lean", "Unquotable/x»y.lean"):
        (tmp_path / "Demo" / name).parent.mkdir()
        (tmp_path / "Demo" / name).write_text("", encoding="utf-8")
    (tmp_path / "Demo" / "Empty").mkdir()
    snapshot = snapshot_project_sources(tmp_path)

    with pytest.raises(SkeletonError, match=message):
        project_modules([_library(src, glob)], snapshot)


def test_import_closure_over_approximates_every_spelling_of_an_import() -> None:
    sources = [
        ("Q.M", b"def m := 1\n"),
        ("Q.Guillemets", "import «Q».«M»\n".encode()),
        ("Q.Unicode", "import Q.MΣ\n".encode()),
        ("Q.DocComment", b"/-- header -/\nimport Q.M\n"),
        ("Q.Later", b"import Q.Unicode\n"),
        ("Q.Apart", b"import Q.Other\n"),
    ]

    assert import_closure("Q.M", sources, ()) == {"Q.M", "Q.Guillemets", "Q.Unicode", "Q.DocComment", "Q.Later"}


def test_import_closure_refuses_a_file_outside_the_inventory_that_may_import_it() -> None:
    sources = [("Q.M", b""), ("Q.U", b"import Q.M\n")]
    outside = [(".lake/packages/D/D/X.lean", b"import Q.U\n")]

    with pytest.raises(ImpactError, match=r"\.lake/packages/D/D/X\.lean may import Q\.U"):
        import_closure("Q.M", sources, outside)
    assert import_closure("Q.M", sources, [(".lake/packages/D/D/Y.lean", b"import Mathlib\n")]) == {"Q.M", "Q.U"}


def test_lean_libraries_read_one_glob_or_an_array(tmp_path: Path) -> None:
    (tmp_path / "lakefile.toml").write_text(
        'name = "Demo"\nbuildDir = "output"\nleanLibDir = "compiled"\n\n'
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
    assert {library.artifact_dir for library in lean_libraries(tmp_path)} == {
        (tmp_path / "output/compiled").resolve()
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
    """A roadmap whose articles name ``{prefix}.base``, ``.uses``, ``.loose``, ``.inlines`` and nothing."""

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
    _write_article(
        project,
        "inlines.md",
        metadata=["declaration: theorem", "statement: formalized", f"lean: {prefix}.inlines"],
        depends=("base.md",),
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
    _payload("Demo.inlines"),
    _payload("Demo.old", deprecated=True, replacement="Demo.base_eq"),
    _payload("Demo.gone", deprecated=True),
]


def _stub_probe(
    monkeypatch: pytest.MonkeyPatch,
    records=_STUB_RECORDS,
    *,
    modules: tuple[ModuleEvidence, ...] = (),
    error: SkeletonError | None = None,
):
    calls: list[dict[str, object]] = []

    def run_probe(probe: str, lean_root: Path, **kwargs: object) -> str:
        calls.append({"probe": probe, "lean_root": lean_root, **kwargs})
        if error is not None:
            raise error
        return "\n".join(
            [
                "lake env lean noise",
                *(
                    _module_line(
                        evidence.module,
                        *evidence.direct_local_imports,
                        source_path=evidence.source_path,
                        olean_path=evidence.olean_path
                        or str(
                            lean_root
                            / ".lake/build/lib/lean"
                            / Path(*evidence.module.split(".")).with_suffix(".olean")
                        ),
                    )
                    for evidence in modules
                ),
                *(_line(record) for record in records),
            ]
        ) + "\n"

    monkeypatch.setattr("autoform_cli.lean_probe.run_probe", run_probe)
    return calls


def test_cli_leaves_a_mathlib_statement_dependent_unclaimed(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    _write_article(
        project,
        "known.md",
        metadata=["declaration: theorem", "mathlib: true", "mathlib_declaration: Nat.add_zero"],
        depends=("base.md",),
    )
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch)

    code = cli.main(["work", "impact", _BASE_ID, str(project), "--lean-root", str(lean_root), "--json"])

    output = capsys.readouterr()
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["unused_statement_dependencies"] == ["chapter/inlines"]
    assert "chapter/known" not in report["claim_targets"]


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
    assert re.fullmatch(r"[0-9a-f]{64}", report.pop("lean_source_revision"))
    assert re.fullmatch(r"[0-9a-f]{64}", report.pop("build_revision"))
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
                "owners": [],
                "claim_targets": ["lean/demo-base-eq-7f17aa41d1461243"],
            }
        ],
        "undeclared_dependencies": ["chapter/loose"],
        "unused_statement_dependencies": ["chapter/inlines"],
        "deprecated": [
            {"name": "Demo.gone", "replacement": None, "users": [], "articles": []},
            {"name": "Demo.old", "replacement": "Demo.base_eq", "users": ["Demo.loose"], "articles": []},
        ],
        "deprecated_unused": ["Demo.gone"],
        "claim_targets": [
            _BASE_ID,
            _USES_ID,
            "chapter/inlines",
            "chapter/loose",
            "lean/demo-base-eq-7f17aa41d1461243",
        ],
    }
    (call,) = calls
    assert call["label"] == "impact probe"
    assert call["timeout"] == 30
    assert call["lean_root"] == lean_root.resolve()
    assert str(call["probe"]).startswith("import Demo\n-- Autoform impact probe.")


def _closure_lean_root(tmp_path: Path) -> Path:
    root = tmp_path / "lean"
    (root / "Demo").mkdir(parents=True)
    (root / "lakefile.toml").write_text(
        'name = "Demo"\n\n[[lean_lib]]\nname = "Demo"\nglobs = ["Demo.+"]\n', encoding="utf-8"
    )
    (root / "Demo" / "A.lean").write_text("def Demo.base (n : Nat) : Nat := n\n", encoding="utf-8")
    (root / "Demo" / "B.lean").write_text("import Demo.A\n", encoding="utf-8")
    (root / "Demo" / "Apart.lean").write_text("def Demo.apart : Nat := 0\n", encoding="utf-8")
    return root


def test_import_closure_scope_imports_only_candidate_importers(tmp_path: Path, monkeypatch) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _closure_lean_root(tmp_path)
    calls = _stub_probe(
        monkeypatch,
        records=[_payload("Demo.base", kind="def", module="Demo.A")],
        modules=(
            ModuleEvidence("Demo.A", str((lean_root / "Demo/A.lean").resolve())),
            ModuleEvidence("Demo.B", str((lean_root / "Demo/B.lean").resolve()), ("Demo.A",)),
        ),
    )

    report = module_import_impact(project, _BASE_ID, "Demo.A", lean_root=lean_root, module_scope="import-closure")

    lines = calls[0]["probe"].splitlines()
    imports = [line for line in lines if line.startswith("import Demo")]
    assert imports == ["import Demo.A", "import Demo.B"]
    # The glob walk still checks the full selection, so the unloaded Demo.Apart is not refused.
    requested = lines[lines.index("  let selectedModules : Std.HashSet Name :=") + 1]
    selected = json.loads(json.loads(requested.split("requestedNames ", 1)[1].removesuffix(").toList")))
    assert [name for name, _parts in selected] == ["Demo.A", "Demo.Apart", "Demo.B"]
    assert report.module_scope == "import-closure"
    assert report.affected_modules == ("Demo.A", "Demo.B")


def test_import_closure_scope_reaches_a_roots_library_module_through_its_root(tmp_path: Path, monkeypatch) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = tmp_path / "lean"
    (lean_root / "Demo").mkdir(parents=True)
    (lean_root / "lakefile.toml").write_text(
        'name = "Demo"\n\n[[lean_lib]]\nname = "Demo"\nroots = ["Demo"]\n', encoding="utf-8"
    )
    (lean_root / "Demo.lean").write_text("import Demo.A\n", encoding="utf-8")
    (lean_root / "Demo" / "A.lean").write_text("def Demo.base (n : Nat) : Nat := n\n", encoding="utf-8")
    calls = _stub_probe(
        monkeypatch,
        records=[_payload("Demo.base", kind="def", module="Demo.A")],
        modules=(
            ModuleEvidence("Demo", str((lean_root / "Demo.lean").resolve()), ("Demo.A",)),
            ModuleEvidence("Demo.A", str((lean_root / "Demo/A.lean").resolve())),
        ),
    )

    report = module_import_impact(project, _BASE_ID, "Demo.A", lean_root=lean_root, module_scope="import-closure")

    imports = [line for line in calls[0]["probe"].splitlines() if line.startswith("import Demo")]
    assert imports == ["import Demo"]
    assert report.affected_modules == ("Demo", "Demo.A")


def test_import_closure_scope_keeps_an_importer_another_library_spells_the_same(tmp_path: Path, monkeypatch) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _closure_lean_root(tmp_path)
    with (lean_root / "lakefile.toml").open("a", encoding="utf-8") as lakefile:
        lakefile.write('\n[[lean_lib]]\nname = "Extra"\nsrcDir = "extra"\nroots = ["Extra"]\n')
    (lean_root / "extra" / "Demo").mkdir(parents=True)
    (lean_root / "extra" / "Extra.lean").write_text("def Extra.x : Nat := 0\n", encoding="utf-8")
    # A dormant file Extra's srcDir also spells Demo.B must not hide the real importer.
    (lean_root / "extra" / "Demo" / "B.lean").write_text("def Demo.dormant : Nat := 0\n", encoding="utf-8")
    calls = _stub_probe(
        monkeypatch,
        records=[_payload("Demo.base", kind="def", module="Demo.A")],
        modules=(
            ModuleEvidence("Demo.A", str((lean_root / "Demo/A.lean").resolve())),
            ModuleEvidence("Demo.B", str((lean_root / "Demo/B.lean").resolve()), ("Demo.A",)),
        ),
    )

    module_import_impact(project, _BASE_ID, "Demo.A", lean_root=lean_root, module_scope="import-closure")

    assert "import Demo.B" in calls[0]["probe"].splitlines()


def test_import_closure_scope_leaves_out_unresolved_names_the_index_places_outside_it(
    tmp_path: Path, monkeypatch
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _closure_lean_root(tmp_path)
    (lean_root / "Demo" / "Apart.lean").write_text("theorem Demo.loose : True := trivial\n", encoding="utf-8")
    _stub_probe(
        monkeypatch,
        records=[_payload("Demo.base", kind="def", module="Demo.A")],
        modules=(
            ModuleEvidence("Demo.A", str((lean_root / "Demo/A.lean").resolve())),
            ModuleEvidence("Demo.B", str((lean_root / "Demo/B.lean").resolve()), ("Demo.A",)),
        ),
    )

    report = module_import_impact(project, _BASE_ID, "Demo.A", lean_root=lean_root, module_scope="import-closure")

    # Demo.loose is in the unaffected Demo.Apart; Demo.uses and Demo.inlines are declared nowhere.
    assert [names for article in report.unresolved_articles for names in article.declarations] == [
        "Demo.inlines",
        "Demo.uses",
    ]


def test_import_closure_scope_names_only_its_modules_when_stale(tmp_path: Path, monkeypatch) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _closure_lean_root(tmp_path)
    _stub_probe(
        monkeypatch,
        error=SkeletonError([f"{lean_probe.STALE_BUILD}Demo.A Demo.B` before running the impact probe\n✖ Demo.B"]),
    )

    with pytest.raises(SkeletonError) as caught:
        module_import_impact(project, _BASE_ID, "Demo.A", lean_root=lean_root, module_scope="import-closure")

    message = str(caught.value.issues[0])
    assert message.startswith(f"{lean_probe.STALE_BUILD}Demo.A Demo.B` ")
    assert "(2 modules" in message


def test_import_closure_scope_rejects_a_module_no_library_selects(tmp_path: Path, monkeypatch) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _closure_lean_root(tmp_path)
    _stub_probe(monkeypatch)

    with pytest.raises(ImpactError, match="not a project module: Demo.Nope"):
        module_import_impact(project, _BASE_ID, "Demo.Nope", lean_root=lean_root, module_scope="import-closure")


def test_import_closure_scope_refuses_a_package_file_that_may_import_the_closure(
    tmp_path: Path, monkeypatch
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _closure_lean_root(tmp_path)
    package = lean_root / ".lake" / "packages" / "D" / "D"
    package.mkdir(parents=True)
    (package / "X.lean").write_text("import Demo.B\n", encoding="utf-8")
    _stub_probe(monkeypatch)

    with pytest.raises(ImpactError, match=r"\.lake/packages/D/D/X\.lean may import Demo\.B"):
        module_import_impact(project, _BASE_ID, "Demo.A", lean_root=lean_root, module_scope="import-closure")


def test_import_closure_scope_refuses_a_path_dependency_file_outside_the_lean_root(
    tmp_path: Path, monkeypatch
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _closure_lean_root(tmp_path)
    (lean_root / "lake-manifest.json").write_text(
        json.dumps({"packagesDir": ".lake/packages", "packages": [{"type": "path", "dir": "../D", "name": "D"}]}),
        encoding="utf-8",
    )
    (tmp_path / "D").mkdir()
    (tmp_path / "D" / "X.lean").write_text("import Demo.B\n", encoding="utf-8")
    _stub_probe(monkeypatch)

    with pytest.raises(ImpactError, match=r"D/X\.lean may import Demo\.B"):
        module_import_impact(project, _BASE_ID, "Demo.A", lean_root=lean_root, module_scope="import-closure")


def test_import_closure_scope_names_an_outside_path_it_cannot_read(tmp_path: Path, monkeypatch) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _closure_lean_root(tmp_path)
    _stub_probe(monkeypatch)
    (lean_root / "lake-manifest.json").write_text("{", encoding="utf-8")

    with pytest.raises(ImpactError, match=r"cannot read .*lake-manifest\.json; rerun with the default --module-scope"):
        module_import_impact(project, _BASE_ID, "Demo.A", lean_root=lean_root, module_scope="import-closure")

    (lean_root / "lake-manifest.json").unlink()
    package = lean_root / ".lake" / "packages" / "D"
    package.mkdir(parents=True)
    (package / "Gone.lean").symlink_to(package / "missing.lean")
    with pytest.raises(ImpactError, match=r"cannot read .*Gone\.lean; rerun with the default --module-scope"):
        module_import_impact(project, _BASE_ID, "Demo.A", lean_root=lean_root, module_scope="import-closure")


def test_import_impact_rejects_an_unknown_module_scope(tmp_path: Path) -> None:
    with pytest.raises(ImpactError, match="unknown module scope: closure"):
        module_import_impact(tmp_path, _BASE_ID, "Demo.A", lean_root=tmp_path, module_scope="closure")


def test_cli_reports_import_impact_without_refusing_the_edit(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(
        monkeypatch,
        modules=(ModuleEvidence("Demo", str((lean_root / "Demo.lean").resolve())),),
    )

    code = cli.main(
        [
            "work",
            "import-impact",
            _BASE_ID,
            "Demo",
            str(project),
            "--lean-root",
            str(lean_root),
            "--json",
        ]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    report = json.loads(output.out)
    assert output.out == json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
    assert set(report) == {
        "schema",
        "source_revision",
        "lean_source_revision",
        "build_revision",
        "module_graph_revision",
        "article",
        "module",
        "module_scope",
        "module_claim_target",
        "module_claim_key",
        "selected_affected",
        "article_local",
        "affected_modules",
        "affected_articles",
        "unresolved_articles",
        "affected_unowned_helpers",
        "affected_claim_targets",
        "affected_claim_keys",
        "claim_targets",
        "claim_keys",
    }
    assert report["schema"] == MODULE_IMPORT_IMPACT_SCHEMA
    assert report["module"] == "Demo"
    assert report["module_scope"] == "loaded-root-library-graph"
    assert report["affected_modules"] == ["Demo"]
    assert [article["id"] for article in report["affected_articles"]] == [
        "chapter/base",
        "chapter/inlines",
        "chapter/loose",
        "chapter/uses",
    ]
    assert report["claim_targets"][0] == _BASE_ID
    assert report["module_claim_target"] in report["claim_targets"]
    assert set(report["affected_claim_targets"]) >= {
        _USES_ID,
        "chapter/inlines",
        "chapter/loose",
    }
    assert {helper["name"] for helper in report["affected_unowned_helpers"]} >= {
        "Demo.base_eq",
        "Demo.old",
    }
    assert all(
        set(helper) == {"name", "module", "claim_target", "claim_key"}
        for helper in report["affected_unowned_helpers"]
    )
    assert report["unresolved_articles"] == []
    assert re.fullmatch(r"[0-9a-f]{64}", report["module_graph_revision"])


def test_cli_import_impact_accepts_an_article_that_names_no_lean_declaration(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Unlike work impact, an import edit needs no revised declaration, so the
    # command never asks for --declaration, which it does not have.
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch, modules=(ModuleEvidence("Demo", str((lean_root / "Demo.lean").resolve())),))

    code = cli.main(
        ["work", "import-impact", "chapter/empty", "Demo", str(project), "--lean-root", str(lean_root), "--json"]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["selected_affected"] is False
    assert report["claim_targets"][0] == "chapter/empty"


@pytest.mark.parametrize("quote_every_component", [False, True])
def test_cli_import_impact_matches_module_names_by_component(
    tmp_path: Path, monkeypatch, capsys, quote_every_component: bool
) -> None:
    # Lean prints H/2_1.lean as `H.«2_1»` and H/α.lean as `H.α`, while the
    # inventory quotes every component that is not a plain identifier, `H.«α»`
    # too. Names meet by component whichever side quotes one, so a probe that
    # quotes `«α»` matches as well, and the dotted stem of H/a.b.lean keeps its
    # whole artifact name.
    lean_root = tmp_path / "lean"
    (lean_root / "H").mkdir(parents=True)
    (lean_root / "lakefile.toml").write_text('name = "H"\n\n[[lean_lib]]\nname = "H"\n', encoding="utf-8")
    (lean_root / "H.lean").write_text("", encoding="utf-8")
    artifacts = lean_root.resolve() / ".lake/build/lib/lean"
    modules = []
    for stem in ("2_1", "my-lemmas", "a.b", "α", "1"):
        source = lean_root / "H" / f"{stem}.lean"
        source.write_text("", encoding="utf-8")
        name = f"H.{stem}" if stem == "α" and not quote_every_component else f"H.«{stem}»"
        modules.append(ModuleEvidence(name, str(source.resolve()), (), str(artifacts / "H" / f"{stem}.olean")))
    modules.append(
        ModuleEvidence(
            "H",
            str((lean_root / "H.lean").resolve()),
            tuple(item.module for item in modules),
            str(artifacts / "H.olean"),
        )
    )
    calls = _stub_probe(monkeypatch, [_payload("H.stem", module="H.«a.b»")], modules=tuple(modules))
    project = tmp_path / "project"
    _write_article(project, "README.md", title="Chapter", metadata=["article_id: af_0000000000000000000000c0"])
    _write_article(project, "stem.md", metadata=["declaration: definition", "statement: formalized", "lean: H.stem"])

    code = cli.main(
        ["work", "import-impact", "chapter/stem", "H.«a.b»", str(project), "--lean-root", str(lean_root), "--json"]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    assert json.loads(output.out)["affected_modules"] == ["H", "H.«a.b»"]
    # The probe counts the dotted and numeric stems as the modules Lean builds:
    # each stem is one string component, not two components or a number.
    assert '[\\"H\\", \\"a.b\\"]' in calls[0]["probe"]
    assert '[\\"H\\", \\"1\\"]' in calls[0]["probe"]


def test_cli_import_impact_accepts_another_spelling_of_the_lean_root(tmp_path: Path, monkeypatch, capsys) -> None:
    # On a case-insensitive file system `--lean-root` may differ in case from
    # the on-disk path Lean reports; a symbolic link gives the same two
    # spellings of one file on any file system.
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(lean_root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    _stub_probe(monkeypatch, modules=(ModuleEvidence("Demo", str(alias / "Demo.lean")),))

    code = cli.main(["work", "import-impact", _BASE_ID, "Demo", str(project), "--lean-root", str(lean_root), "--json"])

    output = capsys.readouterr()
    assert code == 0, output.err
    assert json.loads(output.out)["affected_modules"] == ["Demo"]


def test_cli_import_impact_rejects_an_unknown_module(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(
        monkeypatch,
        modules=(ModuleEvidence("Demo", str((lean_root / "Demo.lean").resolve())),),
    )

    code = cli.main(
        ["work", "import-impact", _BASE_ID, "Missing", str(project), "--lean-root", str(lean_root)]
    )

    output = capsys.readouterr()
    assert code == 2
    assert output.out == ""
    assert "error: not a loaded project module: Missing" in output.err


@pytest.mark.parametrize(
    ("modules", "message"),
    [
        ((ModuleEvidence("Demo", "/dependency/Demo.lean"),), "expected captured root source"),
        (
            (
                ModuleEvidence(
                    "Demo",
                    "",
                    olean_path="/dependency/.lake/build/lib/lean/Demo.olean",
                ),
            ),
            "expected root artifact",
        ),
        ((ModuleEvidence("Other", "/project/Other.lean"),), "omitted required project modules"),
    ],
)
def test_cli_import_impact_binds_modules_to_captured_root_sources(
    tmp_path: Path,
    monkeypatch,
    capsys,
    modules: tuple[ModuleEvidence, ...],
    message: str,
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    modules = tuple(
        ModuleEvidence(
            evidence.module,
            evidence.source_path or str((lean_root / "Demo.lean").resolve()),
            evidence.direct_local_imports,
            evidence.olean_path,
        )
        for evidence in modules
    )
    _stub_probe(monkeypatch, modules=modules)

    code = cli.main(
        ["work", "import-impact", _BASE_ID, "Demo", str(project), "--lean-root", str(lean_root)]
    )

    output = capsys.readouterr()
    assert code == 2
    assert output.out == ""
    assert message in output.err


def test_cli_work_impact_has_the_probe_check_the_inventory(tmp_path: Path, monkeypatch, capsys) -> None:
    # work impact runs the same inventory checks as import-impact, without
    # asking for module evidence.
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    calls = _stub_probe(monkeypatch)

    code = cli.main(["work", "impact", _BASE_ID, str(project), "--lean-root", str(lean_root), "--json"])

    assert code == 0, capsys.readouterr().err
    (call,) = calls
    artifact_dir = (lean_root / ".lake/build/lib/lean").resolve()
    assert f"IO.FS.realPath {json.dumps(str(artifact_dir))}" in call["probe"]
    assert (call["probe"].count("if true then"), call["probe"].count("if false then")) == (1, 1)


def test_cli_import_impact_has_the_probe_walk_each_glob_directory(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    (lean_root / "lakefile.toml").write_text(
        'name = "Demo"\n\n[[lean_lib]]\nname = "Demo"\nglobs = ["Demo", "Demo.Sub.+"]\n', encoding="utf-8"
    )
    _sources(lean_root, "Demo.Sub.A")
    calls = _stub_probe(
        monkeypatch,
        modules=tuple(
            ModuleEvidence(module, str(lean_root.resolve() / path))
            for module, path in (("Demo", "Demo.lean"), ("Demo.Sub.A", "Demo/Sub/A.lean"))
        ),
    )

    code = cli.main(["work", "import-impact", _BASE_ID, "Demo", str(project), "--lean-root", str(lean_root)])

    output = capsys.readouterr()
    assert code == 0, output.err
    # Only the `M.+` glob reaches into a directory, which the probe walks for
    # modules the inventory lacks.
    directory = str(lean_root.resolve() / "Demo" / "Sub")
    assert _glob_walks(str(calls[0]["probe"])) == [[directory, ["Demo", "Sub"], "Demo.Sub.+"]]
    assert "unless selectedModules.contains (base ++ sub) do" in str(calls[0]["probe"])


def _unbuilt_root_lean(tmp_path: Path) -> Path:
    """Library Foo builds only its glob Foo.Basic; library Extra, nested in Foo/, builds Extra."""

    lean_root = tmp_path / "lean"
    (lean_root / "Foo").mkdir(parents=True)
    (lean_root / "lakefile.toml").write_text(
        'name = "Foo"\n\n[[lean_lib]]\nname = "Foo"\nglobs = ["Foo.Basic"]\n\n'
        '[[lean_lib]]\nname = "Extra"\nsrcDir = "Foo"\n',
        encoding="utf-8",
    )
    (lean_root / "Foo.lean").write_text("import Foo.Basic\n", encoding="utf-8")
    (lean_root / "Foo" / "Basic.lean").write_text("def Foo.base (n : Nat) : Nat := n\n", encoding="utf-8")
    (lean_root / "Foo" / "Extra.lean").write_text("import Foo.Basic\n", encoding="utf-8")
    return lean_root


def test_cli_import_impact_accepts_a_root_lake_does_not_build(tmp_path: Path, monkeypatch, capsys) -> None:
    # Library Foo builds only its glob Foo.Basic; library Extra, nested in Foo/,
    # builds Foo/Extra.lean as Extra, so the file has one module.
    project = _blueprint_project(tmp_path, "Foo")
    lean_root = _unbuilt_root_lean(tmp_path)
    _stub_probe(
        monkeypatch,
        records=[_payload("Foo.base", kind="def", module="Foo.Basic")],
        modules=(
            ModuleEvidence("Foo.Basic", str((lean_root / "Foo/Basic.lean").resolve())),
            ModuleEvidence("Extra", str((lean_root / "Foo/Extra.lean").resolve()), ("Foo.Basic",)),
        ),
    )

    code = cli.main(
        ["work", "import-impact", _BASE_ID, "Foo.Basic", str(project), "--lean-root", str(lean_root), "--json"]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    assert json.loads(output.out)["affected_modules"] == ["Extra", "Foo.Basic"]


def test_cli_import_impact_refuses_a_loaded_module_no_library_builds(tmp_path: Path, monkeypatch, capsys) -> None:
    # Foo.lean is under library Foo's root, but no glob matches Foo, so Lake
    # does not build it and the probe could load it only from a stale .olean.
    project = _blueprint_project(tmp_path, "Foo")
    lean_root = _unbuilt_root_lean(tmp_path)
    _stub_probe(
        monkeypatch,
        records=[_payload("Foo.base", kind="def", module="Foo.Basic")],
        modules=(
            ModuleEvidence("Foo.Basic", str((lean_root / "Foo/Basic.lean").resolve())),
            ModuleEvidence("Extra", str((lean_root / "Foo/Extra.lean").resolve()), ("Foo.Basic",)),
            ModuleEvidence("Foo", str((lean_root / "Foo.lean").resolve()), ("Foo.Basic",)),
        ),
    )

    code = cli.main(
        ["work", "import-impact", _BASE_ID, "Foo.Basic", str(project), "--lean-root", str(lean_root), "--json"]
    )

    output = capsys.readouterr()
    assert code == 2
    assert output.out == ""
    assert "error: the impact probe loaded Foo without a captured root-project source" in output.err


def test_cli_import_impact_reports_an_empty_loaded_module_without_false_locality(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(
        monkeypatch,
        records=(),
        modules=(ModuleEvidence("Demo", str((lean_root / "Demo.lean").resolve())),),
    )

    code = cli.main(
        [
            "work",
            "import-impact",
            _BASE_ID,
            "Demo",
            str(project),
            "--lean-root",
            str(lean_root),
            "--json",
        ]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["selected_affected"] is False
    assert report["article_local"] is False
    assert report["affected_articles"] == []


def test_cli_text_report_lists_each_section(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    calls = _stub_probe(monkeypatch)
    source_revision, _ = work_context(project, "chapter/base")

    code = cli.main(["work", "impact", "chapter/base", str(project), "--lean-root", str(lean_root)])

    output = capsys.readouterr()
    assert code == 0, output.err
    lines = output.out.splitlines()
    assert re.fullmatch(r"Lean source revision: [0-9a-f]{64}", lines.pop(2))
    assert re.fullmatch(r"Lean build revision: [0-9a-f]{64}", lines.pop(2))
    assert lines == [
        f"Revising Demo.base of chapter/base [{_BASE_ID}]",
        f"Graph source revision: {source_revision}",
        "Statement impacted:",
        f"  chapter/uses [{_USES_ID}]: Demo.uses",
        "Proof impacted:",
        "  chapter/loose: Demo.loose",
        "Helpers no article names:",
        "  Demo.base_eq (theorem, statement) Demo.lean:5; no owner; claims lean/demo-base-eq-7f17aa41d1461243",
        "Impacted without a Markdown dependency path to the revised declarations: chapter/loose",
        "Statement dependents in Markdown that are not statement impacted: chapter/inlines",
        "Deprecated:",
        "  Demo.gone: no users, safe to delete",
        "  Demo.old -> Demo.base_eq: used by Demo.loose",
        f"Claim targets: {_BASE_ID}, {_USES_ID}, chapter/inlines, chapter/loose, lean/demo-base-eq-7f17aa41d1461243",
    ]
    assert calls[0]["timeout"] == lean_probe.DEFAULT_PROBE_TIMEOUT


def test_cli_import_impact_text_report_lists_each_section(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    calls = _stub_probe(monkeypatch, modules=(ModuleEvidence("Demo", str((lean_root / "Demo.lean").resolve())),))
    source_revision, _ = work_context(project, "chapter/base")

    code = cli.main(
        ["work", "import-impact", "chapter/base", "Demo", str(project)]
        + ["--lean-root", str(lean_root), "--timeout", "30"]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    lines = output.out.splitlines()
    for label in ("Lean source revision", "Lean build revision", "Module graph revision"):
        assert re.fullmatch(rf"{label}: [0-9a-f]{{64}}", lines.pop(2))

    def keyed(*targets: str) -> str:
        return ", ".join(f"{target} (key {claims.author_claim_key(target)})" for target in targets)

    module = "lean-module/demo-" + hashlib.sha256(b"Demo").hexdigest()[:16]
    helpers = ("Demo.base_eq", "Demo.gone", "Demo.old")
    assert lines == [
        f"Import edit allowed in Demo for chapter/base [{_BASE_ID}].",
        f"Graph source revision: {source_revision}",
        "Module scope: loaded-root-library-graph",
        f"Module claim target: {keyed(module)}",
        "Affected project modules: Demo",
        "Removing an import can also drop modules that only imports bring into the build; "
        "this reverse-import scope does not list them.",
        "Affected articles:",
        "  chapter/inlines: Demo",
        "  chapter/loose: Demo",
        f"  chapter/uses [{_USES_ID}]: Demo",
        "Potentially affected unowned helpers:",
        *(f"  {name} (Demo): {keyed(_lean_key(name))}" for name in helpers),
        "Check other workers, live claims, or semantic changes for: "
        + keyed(_USES_ID, "chapter/inlines", "chapter/loose", *map(_lean_key, helpers)),
        f"Claim targets of the import edit: {keyed(_BASE_ID, module)}",
    ]
    assert calls[0]["timeout"] == 30


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
    line = "  Demo.bad\\x1b[2Jname (theorem, statement) Demo.lean; no owner; claims lean/demo-bad-2jname-6d30903d669991fb"
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


@pytest.mark.parametrize("command", [["impact"], ["import-impact"]])
def test_cli_names_the_libraries_to_build_for_a_stale_build_instead_of_every_module(
    tmp_path: Path, monkeypatch, capsys, command: list[str]
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    modules = " ".join(f"Demo.M{number}" for number in range(2000))
    # What `lean_probe.run_probe` raises: every imported module, then Lake's replay before the unbuilt one.
    lake = (
        "⚠ [2/9] Replayed Demo.M3\nwarning: Demo/M3.lean:1:8: declaration uses `sorry`\n"
        "✖ [3/9] Building Demo.M7\nerror: target is out-of-date and needs to be rebuilt\n"
        "Some required targets logged failures:\n- Demo.M7"
    )
    stale = SkeletonError(
        [f"Lean build artifacts are stale; run `lake build {modules}` before running the impact probe\n{lake}"]
    )
    _stub_probe(monkeypatch, error=stale)
    module = ["Demo"] if command == ["import-impact"] else []

    code = cli.main(["work", *command, _BASE_ID, *module, str(project), "--lean-root", str(lean_root)])

    output = capsys.readouterr()
    assert code == 2
    assert output.err.splitlines() == [
        "error: Lean build artifacts are stale; run `lake build Demo` before running the impact probe "
        "(1 module; Lake: ✖ [3/9] Building Demo.M7)"
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


def test_revision_impact_retries_an_initial_source_capture_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch)
    original_capture = lean_module.BoundProjectSources.capture
    captures = 0

    def changing_once(bound, **kwargs):
        nonlocal captures
        captures += 1
        if captures == 1:
            raise TreeChangedError("directory tree changed while it was captured")
        return original_capture(bound, **kwargs)

    monkeypatch.setattr(lean_module.BoundProjectSources, "capture", changing_once)
    monkeypatch.setattr(lean_module, "_SNAPSHOT_RETRY_DELAY_SECONDS", 0)

    report = revision_impact(project, "chapter/uses", lean_root=lean_root)

    assert report.contained
    assert captures == 3  # failed initial capture, retry, post-probe verification


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


def test_helpers_sharing_a_source_name_are_located_in_their_own_module_s_file(tmp_path: Path, monkeypatch) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    with (lean_root / "Demo.lean").open("a", encoding="utf-8") as demo:
        demo.write("\ntheorem Demo.dup : True := trivial\n")
    # A private twin in a file that sorts first, and one private name in two modules.
    (lean_root / "A.lean").write_text("private theorem Demo.dup : True := trivial\n", encoding="utf-8")
    (lean_root / "Demo").mkdir()
    for module, line in (("Extra", 2), ("More", 4)):
        (lean_root / "Demo" / f"{module}.lean").write_text(
            "namespace Demo\n" + "\n" * (line - 2) + "private theorem aux : True := trivial\nend Demo\n",
            encoding="utf-8",
        )
    _stub_probe(
        monkeypatch,
        [
            *_STUB_RECORDS,
            _payload("Demo.dup", type_uses=["Demo.base"]),
            _payload("_private.A.0.Demo.dup", module="A", type_uses=["Demo.base"], user_name="Demo.dup"),
            _payload(
                "_private.Demo.Extra.0.Demo.aux", module="Demo.Extra", type_uses=["Demo.base"], user_name="Demo.aux"
            ),
            _payload(
                "_private.Demo.More.0.Demo.aux", module="Demo.More", type_uses=["Demo.base"], user_name="Demo.aux"
            ),
        ],
    )

    report = revision_impact(project, "chapter/base", lean_root=lean_root)

    assert [(helper.name, helper.path, helper.line) for helper in report.helpers] == [
        ("Demo.base_eq", "Demo.lean", 5),
        ("Demo.dup", "Demo.lean", 9),
        ("_private.A.0.Demo.dup", "A.lean", 1),
        ("_private.Demo.Extra.0.Demo.aux", "Demo/Extra.lean", 2),
        ("_private.Demo.More.0.Demo.aux", "Demo/More.lean", 4),
    ]


def test_unselected_source_with_a_quoted_module_name_stays_local(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    (lean_root / "Demo").mkdir()
    # A Finder duplicate the library's roots never select.
    (lean_root / "Demo" / "Basic copy.lean").write_text("", encoding="utf-8")
    (lean_root / "Demo" / "α.lean").write_text("", encoding="utf-8")
    # Python 3.13's with_suffix("") rejects this stem; Lean names it Demo.«.».
    (lean_root / "Demo" / "..lean").write_text("", encoding="utf-8")
    # Nothing can import a module whose name has no quoted spelling.
    (lean_root / "Demo" / "x»y.lean").write_text("", encoding="utf-8")
    calls = _stub_probe(
        monkeypatch,
        [
            *_STUB_RECORDS,
            _payload("Demo.copied", module="Demo.«Basic copy»", type_uses=["Demo.base"]),
            # Lean prints this component unquoted; it is still the same module.
            _payload("Demo.greek", module="Demo.α", type_uses=["Demo.base"]),
        ],
    )

    code = cli.main(["work", "impact", _BASE_ID, str(project), "--lean-root", str(lean_root), "--json"])

    output = capsys.readouterr()
    assert code == 0, output.err
    # The probe imports only the selected root but counts the quoted module as
    # local, and the module's constants are located by its source file.
    (call,) = calls
    assert str(call["probe"]).startswith("import Demo\n-- Autoform impact probe.")
    # Each quoted stem reaches the probe as one string component of the JSON list.
    assert '[\\"Demo\\", \\"Basic copy\\"]' in str(call["probe"])
    assert '[\\"Demo\\", \\".\\"]' in str(call["probe"])
    assert "x»y" not in str(call["probe"])
    helpers = {helper["name"]: helper["path"] for helper in json.loads(output.out)["helpers"]}
    assert helpers["Demo.copied"] == "Demo/Basic copy.lean"
    assert helpers["Demo.greek"] == "Demo/α.lean"


def test_revision_impact_reads_the_roadmap_once_before_the_probe(tmp_path: Path, monkeypatch) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    calls = _stub_probe(monkeypatch)
    original = runtime.build_runtime_graph
    probes_before_each_read = []

    def counted(*args, **kwargs):
        probes_before_each_read.append(len(calls))
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "build_runtime_graph", counted)

    revision_impact(project, "chapter/base", lean_root=lean_root)

    # One read selects the article; the read after the probe checks that the roadmap held still.
    assert probes_before_each_read == [0, 1]


def _rewrite_base(project: Path) -> None:
    metadata = [f"article_id: {_BASE_ID}", "declaration: definition", "statement: formalized", "lean: Demo.base"]
    _write_article(project, "base.md", metadata=metadata, title="Base, revised")


def _delete_empty(project: Path) -> None:
    (project / "blueprint/roadmap/chapter/empty.md").unlink()


@pytest.mark.parametrize("change", [_rewrite_base, _delete_empty])
def test_revision_impact_refuses_a_roadmap_that_changes_during_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch)
    original = lean_probe.run_probe

    def run_then_change(*args, **kwargs):
        output = original(*args, **kwargs)
        change(project)
        return output

    monkeypatch.setattr("autoform_cli.lean_probe.run_probe", run_then_change)
    with pytest.raises(
        ImpactError,
        match="^the roadmap changed while the impact probe ran; rerun the command$",
    ):
        revision_impact(project, "chapter/base", lean_root=lean_root)


def test_revision_impact_refuses_project_controls_changed_during_library_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    calls = _stub_probe(monkeypatch)
    original = lean_libraries

    def read_then_change(root: Path):
        libraries = original(root)
        lakefile = root / "lakefile.toml"
        lakefile.write_text(
            lakefile.read_text(encoding="utf-8") + '\nmoreLeanArgs = ["-Dchanged=true"]\n',
            encoding="utf-8",
        )
        return libraries

    monkeypatch.setattr("autoform_cli.impact.lean_libraries", read_then_change)

    with pytest.raises(
        ImpactError,
        match="^the Lake project controls changed while library inventory was read; rebuild and rerun the command$",
    ):
        revision_impact(project, "chapter/base", lean_root=lean_root)
    assert calls == []


def test_revision_impact_refuses_project_controls_changed_during_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch)
    original = lean_probe.run_probe

    def run_then_change(*args, **kwargs):
        output = original(*args, **kwargs)
        lakefile = lean_root / "lakefile.toml"
        lakefile.write_text(
            lakefile.read_text(encoding="utf-8") + '\nmoreLeanArgs = ["-Dchanged=true"]\n',
            encoding="utf-8",
        )
        return output

    monkeypatch.setattr("autoform_cli.lean_probe.run_probe", run_then_change)

    with pytest.raises(
        ImpactError,
        match="^the Lake project controls changed while the impact probe ran; rebuild and rerun the command$",
    ):
        revision_impact(project, "chapter/base", lean_root=lean_root)


def test_revision_impact_refuses_lean_sources_that_change_during_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch)
    original = lean_probe.run_probe

    def run_then_change(*args, **kwargs):
        output = original(*args, **kwargs)
        source = lean_root / "Demo.lean"
        source.write_text(source.read_text(encoding="utf-8") + "\n-- changed\n", encoding="utf-8")
        return output

    monkeypatch.setattr("autoform_cli.lean_probe.run_probe", run_then_change)
    with pytest.raises(
        ImpactError,
        match="^the Lean sources changed while the impact probe ran; rebuild and rerun the command$",
    ):
        revision_impact(project, "chapter/base", lean_root=lean_root)


def test_revision_impact_refuses_byte_identical_source_replacement_during_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch)
    original = lean_probe.run_probe

    def run_then_replace(*args, **kwargs):
        output = original(*args, **kwargs)
        source = lean_root / "Demo.lean"
        data = source.read_bytes()
        source.rename(lean_root / "Demo.previous")
        source.write_bytes(data)
        return output

    monkeypatch.setattr("autoform_cli.lean_probe.run_probe", run_then_replace)
    with pytest.raises(
        ImpactError,
        match="^the Lean sources changed while the impact probe ran; rebuild and rerun the command$",
    ):
        revision_impact(project, "chapter/base", lean_root=lean_root)


def test_revision_impact_accepts_unrelated_churn_during_its_final_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not tree_snapshot_module._DESCRIPTOR_CAPTURE_SUPPORTED:
        pytest.skip("directory descriptor capture is unavailable")
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _stub_lean_root(tmp_path)
    _stub_probe(monkeypatch)
    original = lean_probe.run_probe
    probed = False
    saves = 0

    def run_then_mark(*args, **kwargs):
        nonlocal probed
        output = original(*args, **kwargs)
        probed = True
        return output

    original_checkpoint = tree_snapshot_module._tree_snapshot_checkpoint

    def checkpoint(name: str, path: str) -> None:
        nonlocal saves
        original_checkpoint(name, path)
        if probed and name == "before-final-verification":
            # An editor's temporary file appears while the recapture runs.
            saves += 1
            (lean_root / "notes.md.tmp").write_text(f"{saves}\n", encoding="utf-8")

    monkeypatch.setattr("autoform_cli.lean_probe.run_probe", run_then_mark)
    monkeypatch.setattr(tree_snapshot_module, "_tree_snapshot_checkpoint", checkpoint)

    report = revision_impact(project, "chapter/base", lean_root=lean_root)

    assert saves == 1
    assert report.lean_source_revision == snapshot_project_sources(lean_root).revision


# --------------------------------------------------------------------------- #
# The real probe
# --------------------------------------------------------------------------- #


def test_cli_import_impact_takes_the_import_closure_scope(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _blueprint_project(tmp_path, "Demo")
    lean_root = _closure_lean_root(tmp_path)
    _stub_probe(
        monkeypatch,
        records=[_payload("Demo.base", kind="def", module="Demo.A")],
        modules=(
            ModuleEvidence("Demo.A", str((lean_root / "Demo/A.lean").resolve())),
            ModuleEvidence("Demo.B", str((lean_root / "Demo/B.lean").resolve()), ("Demo.A",)),
        ),
    )

    code = cli.main(
        ["work", "import-impact", _BASE_ID, "Demo.A", str(project), "--lean-root", str(lean_root),
         "--module-scope", "import-closure", "--json"]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["module_scope"] == "import-closure"
    assert report["article_local"] is False


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


def _lake_build(root: Path) -> None:
    build = subprocess.run(["lake", "build"], cwd=root, capture_output=True, text=True, timeout=600, check=False)
    assert build.returncode == 0, build.stdout + build.stderr


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
    _lake_build(lean_root)
    return lean_root


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_impact_probe_reads_more_project_modules_than_lean_compiles_in_a_list(tmp_path: Path) -> None:
    lean_root = _imp_project(tmp_path)
    # Lean's code generator gives up on a list literal past about 1,500 entries.
    modules = ("Imp", "Imp.Basic", *(f"Imp.Absent{index}" for index in range(3000)))
    probe = render_impact_probe(imports=["Imp"], project_modules=modules)

    records = parse_impact_output(lean_probe.run_probe(probe, lean_root, label="impact probe"))

    assert {record.module for record in records.values()} == {"Imp.Basic"}


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
def test_import_impact_rejects_a_dependency_artifact_shadowing_a_root_source(
    tmp_path: Path, capsys
) -> None:
    dependency = tmp_path / "dep"
    root = tmp_path / "root"
    dependency.mkdir()
    root.mkdir()
    for project in (dependency, root):
        shutil.copy(_SKELETON_FIXTURE / "lean-toolchain", project / "lean-toolchain")
    (dependency / "lakefile.toml").write_text(
        'name = "Dep"\nversion = "0.1.0"\ndefaultTargets = ["Dep"]\n\n'
        '[[lean_lib]]\nname = "Dep"\nroots = ["Clash"]\n',
        encoding="utf-8",
    )
    dependency_source = dependency / "Clash.lean"
    dependency_source.write_text(
        'def Clash.owner : String := "dependency"\n', encoding="utf-8"
    )
    (root / "lakefile.toml").write_text(
        'name = "Root"\nversion = "0.1.0"\ndefaultTargets = ["Root"]\n\n'
        '[[require]]\nname = "Dep"\npath = "../dep"\n\n'
        '[[lean_lib]]\nname = "Root"\nroots = ["Clash"]\n',
        encoding="utf-8",
    )
    (root / "Clash.lean").write_text(
        'def Clash.owner : String := "root"\n', encoding="utf-8"
    )
    for command in (("lake", "update"), ("lake", "build"), ("lake", "build", "Dep")):
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    # Keep the dependency OLean but make source lookup fall through to the root
    # file. This is the case a source-only ownership check misclassifies.
    dependency_source.rename(dependency / "Clash.lean.hidden")
    project = tmp_path / "roadmap"
    _write_article(
        project,
        "README.md",
        title="Chapter",
        metadata=["article_id: af_0000000000000000000000c0"],
    )
    _write_article(
        project,
        "base.md",
        metadata=[
            "declaration: definition",
            "statement: formalized",
            "lean: Clash.owner",
        ],
    )

    code = cli.main(
        [
            "work",
            "import-impact",
            "chapter/base",
            "Clash",
            str(project),
            "--lean-root",
            str(root),
        ]
    )

    output = capsys.readouterr()
    assert code == 2
    assert output.out == ""
    assert "project module Clash loaded artifact" in output.err
    assert "dep/.lake/build/lib/lean/Clash.olean" in output.err
    assert "expected root artifact" in output.err


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_import_impact_refuses_a_root_module_outside_the_captured_inventory(
    tmp_path: Path, capsys
) -> None:
    dependency = tmp_path / "dep"
    root = tmp_path / "root"
    dependency.mkdir()
    (root / "Demo" / "Mid").mkdir(parents=True)
    for project in (dependency, root):
        shutil.copy(_SKELETON_FIXTURE / "lean-toolchain", project / "lean-toolchain")
    (dependency / "lakefile.toml").write_text(
        'name = "Dep"\nversion = "0.1.0"\ndefaultTargets = ["Dep"]\n\n[[lean_lib]]\nname = "Dep"\n',
        encoding="utf-8",
    )
    (dependency / "Dep.lean").write_text("def Dep.seed : Nat := 1\n", encoding="utf-8")
    (root / "lakefile.toml").write_text(
        'name = "Demo"\nversion = "0.1.0"\ndefaultTargets = ["Demo"]\n\n'
        '[[require]]\nname = "Dep"\npath = "../dep"\n\n'
        '[[lean_lib]]\nname = "Demo"\n',
        encoding="utf-8",
    )
    for name, text in (
        ("Demo.lean", "import Demo.Top\n"),
        ("Demo/Top.lean", "import Demo.Mid.X\n\ntheorem Demo.Top.t : Demo.Mid.x = 1 := rfl\n"),
        ("Demo/Mid/X.lean", "import Demo.Base\n\ndef Demo.Mid.x : Nat := Demo.Base.b\n"),
        ("Demo/Base.lean", "import Dep\n\ndef Demo.Base.b : Nat := Dep.seed\n"),
    ):
        (root / name).write_text(text, encoding="utf-8")
    for command in (("lake", "update"), ("lake", "build")):
        completed = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=600, check=False)
        assert completed.returncode == 0, completed.stdout + completed.stderr
    # A gitlink file makes Demo/Mid a nested checkout, which the source
    # inventory skips although Lake still builds it into the root package.
    gitlink = root / "Demo" / "Mid" / ".git"
    gitlink.write_text("gitdir: ../../.git/modules/Mid\n", encoding="utf-8")
    project = tmp_path / "roadmap"
    _write_article(project, "README.md", title="Chapter", metadata=["article_id: af_0000000000000000000000c0"])
    for name, declaration, lean in (("base.md", "definition", "Demo.Base.b"), ("top.md", "theorem", "Demo.Top.t")):
        _write_article(
            project, name, metadata=[f"declaration: {declaration}", "statement: formalized", f"lean: {lean}"]
        )
    command = ["work", "import-impact", "chapter/base", "Demo.Base", str(project), "--lean-root", str(root), "--json"]
    revision = ["work", "impact", "chapter/base", str(project), "--lean-root", str(root), "--json"]

    # Both commands run the same probe; without the refusal, work impact would
    # also silently miss that Demo.Top's statement rests on Demo.Base.b.
    for argv in (command, revision):
        code = cli.main(argv)

        output = capsys.readouterr()
        assert code == 2, argv
        assert output.out == ""
        # The project is built, so the refusal comes without the rebuild hint.
        assert output.err.startswith("error: the impact probe loaded root-package module Demo.Mid.X from ")
        assert "but the captured source inventory has no such module" in output.err

    # The same OLeans pass once the inventory captures Demo/Mid: the
    # dependency's and Lean's own modules lie outside the root artifacts.
    gitlink.unlink()
    code = cli.main(command)

    output = capsys.readouterr()
    assert code == 0, output.err
    assert json.loads(output.out)["affected_modules"] == ["Demo", "Demo.Base", "Demo.Mid.X", "Demo.Top"]
    code = cli.main(revision)

    output = capsys.readouterr()
    assert code == 0, output.err
    assert [article["id"] for article in json.loads(output.out)["statement_impacted"]] == ["chapter/top"]


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_import_impact_refuses_a_glob_module_the_inventory_lacks(tmp_path: Path, capsys) -> None:
    root = tmp_path / "lean"
    (root / "Demo" / "docs").mkdir(parents=True)
    shutil.copy(_SKELETON_FIXTURE / "lean-toolchain", root / "lean-toolchain")
    (root / "lakefile.toml").write_text(
        'name = "Demo"\nversion = "0.1.0"\ndefaultTargets = ["Demo", "Tools"]\n\n'
        '[[lean_lib]]\nname = "Demo"\nglobs = ["Demo.*"]\n\n'
        '[[lean_lib]]\nname = "Tools"\nsrcDir = "tools"\n',
        encoding="utf-8",
    )
    vendor = tmp_path / "vendor" / "Sym"
    vendor.mkdir(parents=True)
    (root / "Demo" / "Sym").symlink_to(vendor, target_is_directory=True)
    for name, text in (
        ("Demo.lean", "import Demo.Base\n"),
        ("Demo/Base.lean", "def Demo.Base.b : Nat := 1\n"),
        # Nothing imports these, and the inventory skips each one: a `build`
        # directory, a symlinked directory, and a nested checkout.
        ("Demo/build/X.lean", "import Demo.Base\n\ndef Demo.build.x : Nat := Demo.Base.b\n"),
        ("Demo/Sym/Y.lean", "import Demo.Base\n\ndef Demo.Sym.y : Nat := Demo.Base.b\n"),
        ("Demo/Mid/X.lean", "import Demo.Base\n\ndef Demo.Mid.x : Nat := Demo.Base.b\n"),
        ("Demo/Mid/.git", "gitdir: ../../.git/modules/Mid\n"),
        # A nested checkout without Lean files gives the glob nothing to build.
        ("Demo/docs/.git", "gitdir: ../../.git/modules/docs\n"),
        ("Demo/docs/README.md", "Notes.\n"),
        # Another library's srcDir spells the symlinked module's name too, but
        # no library selects that file, so it must not stand in for it.
        ("tools/Tools.lean", "def Tools.t : Nat := 0\n"),
        ("tools/Demo/Sym/Y.lean", "def Demo.Sym.stray : Nat := 0\n"),
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    _lake_build(root)
    skipped = {"build": "Demo.build.X", "Sym": "Demo.Sym.Y", "Mid": "Demo.Mid.X"}
    for module in skipped.values():
        assert (root / ".lake/build/lib/lean" / Path(*module.split("."))).with_suffix(".olean").is_file()
    aside = tmp_path / "aside"
    aside.mkdir()
    for name in skipped:
        (root / "Demo" / name).rename(aside / name)
    project = tmp_path / "roadmap"
    _write_article(project, "README.md", title="Chapter", metadata=["article_id: af_0000000000000000000000c0"])
    base = ["declaration: definition", "statement: formalized", "lean: Demo.Base.b"]
    _write_article(project, "base.md", metadata=base)
    command = ["work", "import-impact", "chapter/base", "Demo.Base", str(project), "--lean-root", str(root), "--json"]

    revision = ["work", "impact", "chapter/base", str(project), "--lean-root", str(root), "--json"]

    # The probe imports every glob module in the inventory, so one the
    # inventory lacks and nothing imports is never loaded; only the probe's
    # walk of the glob directory, the same walk Lake makes, finds it. Both
    # commands run that probe.
    for name, module in skipped.items():
        (aside / name).rename(root / "Demo" / name)
        codes = [cli.main(argv) for argv in (command, revision)]
        (root / "Demo" / name).rename(aside / name)

        output = capsys.readouterr()
        assert codes == [2, 2], output.out
        assert output.out == ""
        refusal = f"error: the impact probe found root-package module {module} under glob Demo.*, "
        assert output.err.count(refusal) == 2
        assert "but loaded no captured source for it" in output.err

    code = cli.main(command)

    output = capsys.readouterr()
    assert code == 0, output.err
    assert json.loads(output.out)["affected_modules"] == ["Demo", "Demo.Base"]
    assert cli.main(revision) == 0, capsys.readouterr().err


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_import_impact_reads_modules_whose_names_need_quotes(tmp_path: Path, capsys) -> None:
    root = tmp_path / "lean"
    (root / "H").mkdir(parents=True)
    shutil.copy(_SKELETON_FIXTURE / "lean-toolchain", root / "lean-toolchain")
    (root / "lakefile.toml").write_text(
        'name = "H"\nversion = "0.1.0"\ndefaultTargets = ["H"]\n\n[[lean_lib]]\nname = "H"\n', encoding="utf-8"
    )
    stems = ("2_1", "my-lemmas", "a.b", "α", "1")
    (root / "H.lean").write_text("".join(f"import H.«{stem}»\n" for stem in stems), encoding="utf-8")
    for index, stem in enumerate(stems):
        (root / "H" / f"{stem}.lean").write_text(f"def H.v{index} : Nat := {index}\n", encoding="utf-8")
    _lake_build(root)
    project = tmp_path / "roadmap"
    _write_article(project, "README.md", title="Chapter", metadata=["article_id: af_0000000000000000000000c0"])
    _write_article(project, "stem.md", metadata=["declaration: definition", "statement: formalized", "lean: H.v2"])

    code = cli.main(
        ["work", "import-impact", "chapter/stem", "H.«a.b»", str(project), "--lean-root", str(root), "--json"]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["affected_modules"] == ["H", "H.«a.b»"]
    assert [article["id"] for article in report["affected_articles"]] == ["chapter/stem"]


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_import_impact_reads_a_library_nested_in_another_srcdir(tmp_path: Path, capsys) -> None:
    # Tests lives in srcDir "tst" below Demo's srcDir ".", which comes first on
    # the source search path, and a `Tests` directory without Lean files sits
    # beside the lakefile. The first entry with a directory named like the
    # module's root holds no Tests.lean, on any file system; on a
    # case-insensitive one, a srcDir named "tests" alone does the same.
    root = tmp_path / "lean"
    root.mkdir()
    shutil.copy(_SKELETON_FIXTURE / "lean-toolchain", root / "lean-toolchain")
    (root / "lakefile.toml").write_text(
        'name = "Demo"\nversion = "0.1.0"\ndefaultTargets = ["Demo", "Tests"]\n\n'
        '[[lean_lib]]\nname = "Demo"\n\n[[lean_lib]]\nname = "Tests"\nsrcDir = "tst"\n',
        encoding="utf-8",
    )
    for name, text in (
        ("Demo.lean", "import Demo.Basic\n"),
        ("Demo/Basic.lean", "def Demo.Basic.b : Nat := 1\n"),
        ("tst/Tests.lean", "import Demo.Basic\n\ndef Tests.t : Nat := Demo.Basic.b\n"),
        ("Tests/README.md", "Test notes.\n"),
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    _lake_build(root)
    project = tmp_path / "roadmap"
    _write_article(project, "README.md", title="Chapter", metadata=["article_id: af_0000000000000000000000c0"])
    for name, lean in (("b.md", "Demo.Basic.b"), ("t.md", "Tests.t")):
        _write_article(project, name, metadata=["declaration: definition", "statement: formalized", f"lean: {lean}"])

    code = cli.main(
        ["work", "import-impact", "chapter/b", "Demo.Basic", str(project), "--lean-root", str(root), "--json"]
    )

    output = capsys.readouterr()
    assert code == 0, output.err
    report = json.loads(output.out)
    assert report["affected_modules"] == ["Demo", "Demo.Basic", "Tests"]
    assert report["affected_claim_targets"] == ["chapter/t"]


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_import_closure_scope_matches_the_default_modules_on_a_built_project(tmp_path: Path) -> None:
    lean_root = _imp_project(tmp_path)
    project = _imp_roadmap(tmp_path)

    default = module_import_impact(project, "chapter/base", "Imp.Basic", lean_root=lean_root)
    narrow = module_import_impact(
        project, "chapter/base", "Imp.Basic", lean_root=lean_root, module_scope="import-closure"
    )

    assert narrow.affected_modules == default.affected_modules
    assert {a.id for a in narrow.affected_articles} >= {a.id for a in default.affected_articles}
    assert {h.name for h in narrow.affected_unowned_helpers} >= {h.name for h in default.affected_unowned_helpers}


@pytest.mark.skipif(not _lean_toolchain_available(), reason="needs lake and the fixture's Lean toolchain")
def test_the_probe_reads_a_built_project(tmp_path: Path, monkeypatch, capsys) -> None:
    lean_root = _imp_project(tmp_path)
    project = _imp_roadmap(tmp_path)
    source_revision, _ = work_context(project, "chapter/base")
    outputs: list[str] = []
    run_probe = lean_probe.run_probe

    def recorded(*args: object, **kwargs: object) -> str:
        outputs.append(run_probe(*args, **kwargs))
        return outputs[-1]

    monkeypatch.setattr("autoform_cli.lean_probe.run_probe", recorded)

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
    assert [(h["name"], h["kind"], h["impact"], h["path"], h["line"], h["owners"]) for h in report["helpers"]] == [
        ("Imp.P", "def", "statement", "Imp/Basic.lean", 9, []),
        ("Imp.P_iff", "theorem", "statement", "Imp/Basic.lean", 11, []),
        ("Imp.base_eq", "theorem", "statement", "Imp/Basic.lean", 5, []),
        ("Imp.oldEq", "theorem", "statement", "Imp/Basic.lean", 14, []),
        ("Imp.oldUnused", "theorem", "statement", "Imp/Basic.lean", 17, []),
        ("Imp.usesOld", "theorem", "statement", "Imp/Extra.lean", 10, []),
    ]
    assert report["undeclared_dependencies"] == ["chapter/simp"]
    # proved.md states on uses.md in Markdown, but its Lean statement does not mention base.
    assert report["unused_statement_dependencies"] == ["chapter/proved"]
    # The equation lemma `@[simp]` gives oldSeed goes when oldSeed does.
    assert report["deprecated"] == [
        {"name": "Imp.oldEq", "replacement": "Imp.base_eq", "users": ["Imp.usesOld"], "articles": []},
        {"name": "Imp.oldSeed", "replacement": "Imp.seedEq", "users": [], "articles": []},
        {"name": "Imp.oldUnused", "replacement": "Imp.base_eq", "users": [], "articles": []},
    ]
    assert report["deprecated_unused"] == ["Imp.oldSeed", "Imp.oldUnused"]
    unowned = sorted(_lean_key(h["name"]) for h in report["helpers"])
    assert [h["claim_targets"] for h in report["helpers"]] == [
        [_lean_key(h["name"])] for h in report["helpers"]
    ]
    assert report["claim_targets"] == ["chapter/base", "chapter/proved", "chapter/simp", "chapter/uses", *unowned]
    assert report["source_revision"] == source_revision
    assert re.fullmatch(r"[0-9a-f]{64}", report["lean_source_revision"])
    assert re.fullmatch(r"[0-9a-f]{64}", report["build_revision"])

    code = cli.main(
        [
            "work",
            "import-impact",
            "chapter/base",
            "Imp.Basic",
            str(project),
            "--lean-root",
            str(lean_root),
            "--json",
        ]
    )
    import_output = capsys.readouterr()
    assert code == 0, import_output.err
    import_report = json.loads(import_output.out)
    assert import_report["affected_modules"] == ["Imp", "Imp.Basic", "Imp.Extra"]
    assert [article["id"] for article in import_report["affected_articles"]] == [
        "chapter/apart",
        "chapter/base",
        "chapter/proved",
        "chapter/simp",
        "chapter/uses",
    ]
    assert import_report["claim_targets"][0] == "chapter/base"
    assert import_report["claim_targets"][1] == import_report["module_claim_target"]
    assert set(import_report["affected_claim_targets"]) >= {
        "chapter/apart",
        "chapter/proved",
        "chapter/simp",
        "chapter/uses",
    }

    probed, module_probed = outputs
    records = parse_impact_output(probed)
    assert {record.module for record in records.values()} == {"Imp.Alias", "Imp.Basic", "Imp.Extra", "Imp.More"}
    module_evidence = parse_module_evidence(module_probed)
    assert {module: evidence.direct_local_imports for module, evidence in module_evidence.items()} == {
        "Imp": ("Imp.Basic",),
        "Imp.Alias": (),
        "Imp.Basic": (),
        "Imp.Extra": ("Imp.Basic",),
        "Imp.More": ("Imp.Alias",),
    }
    assert {
        module: evidence.source_path for module, evidence in module_evidence.items()
    } == {
        "Imp": str((lean_root / "Imp.lean").resolve()),
        "Imp.Alias": str((lean_root / "Imp/Alias.lean").resolve()),
        "Imp.Basic": str((lean_root / "Imp/Basic.lean").resolve()),
        "Imp.Extra": str((lean_root / "Imp/Extra.lean").resolve()),
        "Imp.More": str((lean_root / "Imp/More.lean").resolve()),
    }
    assert {
        module: evidence.olean_path for module, evidence in module_evidence.items()
    } == {
        module: str(
            (lean_root / ".lake/build/lib/lean" / Path(*module.split("."))).with_suffix(".olean").resolve()
        )
        for module in ("Imp", "Imp.Alias", "Imp.Basic", "Imp.Extra", "Imp.More")
    }
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
    assert [(helper.name, helper.owners) for helper in seed.helpers] == [
        ("Imp.seedAgain", ()),
        ("Imp.seedAlias", ()),
        (f"{priv}.aux", ("chapter/priv",)),
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
    assert {owner for helper in box.helpers for owner in helper.owners} == {"chapter/box"}
