from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.graph import Graph, Node, load_graph
from autoform_cli.status import NodeStatus, derive, summarize


def _node(blueprint: Path, relative: str, body: str = "", **metadata: str) -> None:
    path = blueprint / "roadmap" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    properties = [*(f"{key}: {value}" for key, value in metadata.items())]
    title = relative.removesuffix(".md").replace("-", " ").title()
    path.write_text(
        "\n".join(["---", *properties, "---", "", f"# {title}", "", body]) + "\n",
        encoding="utf-8",
    )


def _states(blueprint: Path) -> dict[str, str]:
    return {node_id: status.key for node_id, status in derive(load_graph(blueprint)).items()}


def test_a_bare_node_is_ready_to_state(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "leaf.md")

    assert _states(blueprint) == {"leaf": "can_state"}


def test_a_node_waiting_on_an_unstated_prerequisite_is_only_planned(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "base.md")
    _node(blueprint, "top.md", "## Depends on\n\n- [Base](base.md)\n")

    assert _states(blueprint)["top"] == "planned"


def test_definitions_need_no_proof(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "d.md", declaration="def", statement="formalized")

    statuses = derive(load_graph(blueprint))

    assert statuses["d"].proved
    assert statuses["d"].key == "fully_proved"


def test_a_definition_resting_on_unfinished_work_is_merely_defined(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "gap.md")
    _node(
        blueprint,
        "d.md",
        "## Depends on\n\n- [Gap](gap.md)\n",
        declaration="def",
        statement="formalized",
    )

    assert _states(blueprint)["d"] == "defined"


def test_can_prove_needs_every_proof_prerequisite_proved(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "tool.md", declaration="theorem", statement="formalized", proof="formalized")
    _node(
        blueprint,
        "ready.md",
        "## Proof depends on\n\n- [Tool](tool.md)\n",
        declaration="theorem",
        statement="formalized",
    )
    _node(blueprint, "gap.md", declaration="theorem")
    _node(
        blueprint,
        "waiting.md",
        "## Proof depends on\n\n- [Gap](gap.md)\n",
        declaration="theorem",
        statement="formalized",
    )

    states = _states(blueprint)

    assert states["ready"] == "can_prove"
    assert states["waiting"] == "stated"


def test_fully_proved_is_transitive(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    for name in ("a.md", "b.md", "c.md"):
        body = "" if name == "a.md" else f"## Depends on\n\n- [x]({chr(ord(name[0]) - 1)}.md)\n"
        _node(
            blueprint,
            name,
            body,
            declaration="theorem",
            statement="formalized",
            proof="formalized",
        )

    assert set(_states(blueprint).values()) == {"fully_proved"}

    # Break the base and the colour must retreat all the way up the chain.
    (blueprint / "roadmap" / "a.md").write_text(
        "---\ndeclaration: theorem\n---\n\n# A\n", encoding="utf-8"
    )
    states = _states(blueprint)
    assert states == {"a": "can_state", "b": "proved", "c": "proved"}


def test_mathlib_and_not_ready_are_asserted_not_derived(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "up.md", declaration="theorem", mathlib="true")
    _node(blueprint, "stuck.md", declaration="theorem", not_ready="true")

    states = _states(blueprint)

    assert states["up"] == "mathlib"
    assert states["stuck"] == "not_ready"


def test_summary_counts_in_legend_order(tmp_path: Path) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "a.md", declaration="theorem", statement="formalized", proof="formalized")
    _node(blueprint, "b.md")
    _node(blueprint, "c.md")

    summary = summarize(derive(load_graph(blueprint)))

    assert [(state.label, count) for state, count in summary] == [
        ("fully proved", 1),
        ("ready to state", 2),
    ]


@pytest.mark.parametrize(
    "declaration",
    [
        "def",
        "structure",
        "instance",
        "class",
        "abbrev",
        "opaque",
        "irreducible_def",
        "noncomputable def",
        "private noncomputable def",
        "Noncomputable Instance",
        "protected def",
        "partial def",
        "unsafe def",
        "local instance",
        "scoped instance",
    ],
)
def test_every_definition_keyword_skips_the_proof_obligation(
    tmp_path: Path, declaration: str
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "d.md", declaration=declaration, statement="formalized")

    assert derive(load_graph(blueprint))["d"].proved


@pytest.mark.parametrize("declaration", ["axiom", "noncomputable", "private theorem", "def foo"])
def test_assumptions_and_propositions_keep_the_proof_obligation(
    tmp_path: Path, declaration: str
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "d.md", declaration=declaration, statement="formalized")

    assert not derive(load_graph(blueprint))["d"].proved


def _mixed_statuses(blueprint: Path, policy: str) -> dict[str, NodeStatus]:
    """One graph with every kind of node the two policies treat differently.

    ``hidden``, ``open``, ``inner`` and ``side`` are open statements: stated
    theorems with no Lean proof. ``reduction`` and ``top`` are proved on top of
    them, ``up`` is in Mathlib, and ``gap`` and ``bridge`` are not stated.
    """
    _node(blueprint, "README.md", open_statements=policy)
    _node(blueprint, "kind.md", declaration="def", statement="formalized")
    theorem = {"declaration": "theorem"}
    stated = {**theorem, "statement": "formalized"}
    proved = {**stated, "proof": "formalized"}
    _node(blueprint, "lemma.md", "## Depends on\n\n- [Kind](kind.md)\n", **proved)
    _node(blueprint, "hidden.md", **stated)
    _node(blueprint, "up.md", "## Depends on\n\n- [Hidden](hidden.md)\n", **theorem, mathlib="true")
    _node(blueprint, "open.md", "## Depends on\n\n- [Kind](kind.md)\n", **stated)
    _node(
        blueprint,
        "reduction.md",
        "## Depends on\n\n- [Lemma](lemma.md)\n\n## Proof depends on\n\n- [Open](open.md)\n",
        **proved,
    )
    _node(blueprint, "gap.md", **theorem)
    _node(blueprint, "inner.md", **stated)
    _node(blueprint, "side.md", **stated)
    _node(
        blueprint,
        "outer.md",
        "## Depends on\n\n- [Inner](inner.md)\n\n## Proof depends on\n\n- [Side](side.md)\n",
        **stated,
    )
    _node(blueprint, "bridge.md", "## Depends on\n\n- [Open](open.md)\n", **theorem)
    _node(
        blueprint,
        "top.md",
        "## Depends on\n\n- [Up](up.md)\n\n## Proof depends on\n\n- [Outer](outer.md)\n- [Bridge](bridge.md)\n",
        **proved,
    )
    _node(
        blueprint,
        "waits.md",
        "## Depends on\n\n- [Gap](gap.md)\n\n## Proof depends on\n\n- [Gap](gap.md)\n- [Bridge](bridge.md)\n",
        **theorem,
    )
    _node(
        blueprint,
        "pending.md",
        "## Proof depends on\n\n- [Gap](gap.md)\n- [Reduction](reduction.md)\n",
        **stated,
    )
    return derive(load_graph(blueprint))


def _readiness(
    statuses: dict[str, NodeStatus],
) -> dict[str, tuple[str, bool, bool, tuple[str, ...], tuple[str, ...]]]:
    return {
        node_id: (status.key, status.can_state, status.can_prove, status.waiting_on, status.assumes)
        for node_id, status in statuses.items()
    }


def test_the_strict_policy_waits_for_proof_prerequisites_to_be_proved(tmp_path: Path) -> None:
    statuses = _mixed_statuses(tmp_path / "blueprint", "forbidden")

    assert _readiness(statuses) == {
        "roadmap": ("can_state", True, False, (), ()),
        "kind": ("fully_proved", True, True, (), ()),
        "lemma": ("fully_proved", True, True, (), ()),
        "hidden": ("can_prove", True, True, (), ()),
        "up": ("mathlib", True, True, (), ()),
        "open": ("can_prove", True, True, (), ()),
        # Proved, so nothing is waited on, but under this policy its unproved
        # proof prerequisite would have kept the statement from landing.
        "reduction": ("proved", False, False, (), ()),
        "gap": ("can_state", True, False, (), ()),
        "inner": ("can_prove", True, True, (), ()),
        "side": ("can_prove", True, True, (), ()),
        "outer": ("stated", False, False, ("side",), ()),
        "bridge": ("can_state", True, False, (), ()),
        "top": ("proved", False, False, (), ()),
        # Unstated statement prerequisites first, then unproved proof ones, once each.
        "waits": ("planned", False, False, ("gap", "bridge"), ()),
        "pending": ("stated", False, False, ("gap",), ()),
    }


def test_the_open_policy_waits_only_for_prerequisites_to_be_stated(tmp_path: Path) -> None:
    statuses = _mixed_statuses(tmp_path / "blueprint", "allowed")

    assert _readiness(statuses) == {
        "roadmap": ("can_state", True, False, (), ()),
        "kind": ("fully_proved", True, True, (), ()),
        "lemma": ("fully_proved", True, True, (), ()),
        "hidden": ("can_prove", True, True, (), ()),
        "up": ("mathlib", True, True, (), ()),
        "open": ("can_prove", True, True, (), ()),
        "reduction": ("conditional", True, True, (), ("open",)),
        "gap": ("can_state", True, False, (), ()),
        "inner": ("can_prove", True, True, (), ()),
        "side": ("can_prove", True, True, (), ()),
        "outer": ("can_prove", True, True, (), ("inner", "side")),
        "bridge": ("can_state", True, False, (), ("open",)),
        # can_prove reads the prerequisites, not the assertion: bridge is not stated.
        "top": ("conditional", True, False, (), ("inner", "outer")),
        # An unstated node waits only on its statement prerequisites.
        "waits": ("planned", False, False, ("gap",), ()),
        "pending": ("stated", True, False, ("gap",), ("open",)),
    }


@pytest.mark.parametrize("policy", ["forbidden", "allowed"])
def test_a_fully_proved_node_assumes_nothing(tmp_path: Path, policy: str) -> None:
    statuses = _mixed_statuses(tmp_path / "blueprint", policy)

    fully_proved = {node_id for node_id, status in statuses.items() if status.fully_proved}
    assert fully_proved == {"kind", "lemma"}
    assert all(status.assumes == () for status in statuses.values() if status.fully_proved)


def test_assumptions_reach_what_the_lean_proof_can_reach(tmp_path: Path) -> None:
    """``top`` rests on ``up``, ``outer`` and ``bridge``, and each stops the walk differently.

    Mathlib ``up`` is upstream, so ``hidden`` behind it is not reached. ``bridge``
    is not stated, so it has no Lean declaration to lead to ``open``. ``outer``
    is open: its own sorry and its statement prerequisite ``inner`` are reached,
    but ``side``, which only its missing proof would use, is not.
    """
    statuses = _mixed_statuses(tmp_path / "blueprint", "allowed")

    assert statuses["top"].assumes == ("inner", "outer")
    # The walk stops at bridge for its dependents, but bridge's own proof would
    # still rest on open. A Mathlib node rests on nothing.
    assert statuses["bridge"].assumes == ("open",)
    assert statuses["up"].assumes == ()


@pytest.mark.parametrize("policy", ["forbidden", "allowed"])
def test_a_retracted_theorem_still_naming_its_lean_stays_an_open_statement(tmp_path: Path, policy: str) -> None:
    """A retracted statement's declaration, and any sorry in it, stay in the build until it is restated.

    A definition carries no sorry the audit would accept, so a retracted one is
    not open.
    """
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "README.md", open_statements=policy)
    _node(blueprint, "retracted.md", declaration="theorem", lean="Ns.retracted")
    _node(blueprint, "old.md", declaration="def", lean="Ns.old")
    _node(blueprint, "gone.md", declaration="theorem")
    _node(
        blueprint,
        "reduction.md",
        "## Proof depends on\n\n- [Retracted](retracted.md)\n- [Old](old.md)\n- [Gone](gone.md)\n",
        declaration="theorem",
        statement="formalized",
        proof="formalized",
        lean="Ns.reduction",
    )

    statuses = derive(load_graph(blueprint))

    assert statuses["retracted"].key == "can_state"
    if policy == "allowed":
        assert (statuses["reduction"].key, statuses["reduction"].assumes) == ("conditional", ("retracted",))
    else:
        assert (statuses["reduction"].key, statuses["reduction"].assumes) == ("proved", ())


@pytest.mark.parametrize("open_statements", [False, True])
def test_a_missing_dependency_counts_as_neither_stated_nor_proved(
    tmp_path: Path, open_statements: bool
) -> None:
    node = Node(
        id="result",
        title="Result",
        path=tmp_path / "result.md",
        dependencies=("ghost", "phantom"),
        statement_dependencies=("ghost",),
        proof_dependencies=("phantom",),
        declaration="theorem",
        statement_formalized=True,
    )
    graph = Graph(blueprint_dir=tmp_path, nodes={"result": node}, open_statements=open_statements)

    status = derive(graph)["result"]

    assert (status.key, status.can_state, status.can_prove) == ("stated", False, False)
    assert status.waiting_on == ("ghost", "phantom")
    assert status.assumes == ()
