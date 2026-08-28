from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.graph import load_graph
from autoform_cli.status import derive, summarize


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


@pytest.mark.parametrize("declaration", ["def", "structure", "instance", "class", "abbrev"])
def test_every_definition_keyword_skips_the_proof_obligation(
    tmp_path: Path, declaration: str
) -> None:
    blueprint = tmp_path / "blueprint"
    _node(blueprint, "d.md", declaration=declaration, statement="formalized")

    assert derive(load_graph(blueprint))["d"].proved
