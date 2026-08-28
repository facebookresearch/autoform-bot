"""Derive blueprint progress states from asserted facts and the graph.

A node asserts only what a human or agent checked: the statement is in Lean,
the proof is in Lean, it is upstreamed, it is not ready to attempt. Everything
else -- ready to state, ready to prove, and above all *fully* proved -- follows
from the DAG and is recomputed on every run, so it cannot drift.

The state names and palette mirror ``leanblueprint`` so a reader who knows the
Lean community's blueprints can read this one without a key. Fill encodes proof
progress, stroke encodes statement progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import Graph, Node


#: Lean commands that introduce data rather than a proposition to prove. Such a
#: node is complete as soon as its statement is formalized -- there is no
#: separate proof obligation.
DEFINITION_DECLARATIONS = frozenset(
    {"abbrev", "class", "def", "definition", "inductive", "instance", "structure"}
)


@dataclass(frozen=True, slots=True)
class State:
    """How one derived state is named and drawn, in each colour scheme."""

    key: str
    label: str
    fill: str
    stroke: str
    text: str
    dark_fill: str
    dark_stroke: str
    dark_text: str


#: Ordered most complete first; this is also the legend order.
#:
#: Only finished work is filled in; everything in progress is an outline, which
#: keeps a chapter quiet when most of it is still open. The hues are Facebook's
#: semantic set -- #31A24C green, #0064E0 blue, #F7B928 amber, #B0B3B8 grey --
#: so that finished, actionable, blocked and untouched read at a glance without
#: anyone learning a legend. Green tracks proof progress, blue marks what a
#: contributor can pick up now, amber marks what nothing can start on.
#:
#: Dark is not light dimmed: on #18191A a saturated fill closes up, so dark
#: states are near-black panels with a bright stroke and brighter label.
STATES: tuple[State, ...] = (
    State("mathlib", "in mathlib", "#1C5C33", "#134426", "#FFFFFF",
          "#10281A", "#42B72A", "#8BE78B"),
    State("fully_proved", "fully proved", "#31A24C", "#22773A", "#FFFFFF",
          "#13301E", "#42B72A", "#8BE78B"),
    State("proved", "proved", "#8ED4A2", "#22773A", "#0B2415",
          "#122A1B", "#31A24C", "#6BD97F"),
    State("defined", "defined", "#C3E9CE", "#22773A", "#0B2415",
          "#122A1B", "#2B8F44", "#6BD97F"),
    State("can_prove", "ready to prove", "#FFFFFF", "#0064E0", "#0064E0",
          "#101F33", "#2D88FF", "#7FB8FF"),
    State("stated", "statement formalized", "#FFFFFF", "#31A24C", "#22773A",
          "#18191A", "#31A24C", "#E4E6EB"),
    State("can_state", "ready to state", "#FFFFFF", "#0082FB", "#0B4EA2",
          "#18191A", "#2D88FF", "#E4E6EB"),
    State("not_ready", "not ready", "#FFF3D6", "#B77900", "#5C3D00",
          "#2E2205", "#F7B928", "#FFD772"),
    State("planned", "planned", "#FFFFFF", "#CED0D4", "#65676B",
          "#18191A", "#4E4F50", "#B0B3B8"),
)

_BY_KEY = {state.key: state for state in STATES}


@dataclass(frozen=True, slots=True)
class NodeStatus:
    """The derived progress of a single node."""

    node_id: str
    state: State
    stated: bool
    proved: bool
    fully_proved: bool

    @property
    def key(self) -> str:
        return self.state.key

    @property
    def label(self) -> str:
        return self.state.label


def is_definition(node: Node) -> bool:
    """Whether *node* introduces data instead of a proposition."""
    return (node.declaration or "").casefold() in DEFINITION_DECLARATIONS


def derive(graph: Graph) -> dict[str, NodeStatus]:
    """Return the derived status of every node in *graph*, keyed by node id."""
    statuses: dict[str, NodeStatus] = {}
    for node_id in topological_order(graph):
        node = graph.nodes[node_id]
        definition = is_definition(node)
        # A definition carries no proof obligation, so writing it down proves it.
        stated = node.statement_formalized or node.mathlib
        proved = node.proof_formalized or node.mathlib or (definition and stated)

        def done(dependency_id: str, attribute: str) -> bool:
            dependency = statuses.get(dependency_id)
            return dependency is not None and getattr(dependency, attribute)

        can_state = all(done(other, "stated") for other in node.statement_dependencies)
        can_prove = can_state and all(done(other, "proved") for other in node.proof_dependencies)
        fully_proved = proved and all(
            done(other, "fully_proved") for other in node.dependencies
        )
        statuses[node_id] = NodeStatus(
            node_id=node_id,
            state=_BY_KEY[
                _classify(
                    node,
                    definition=definition,
                    stated=stated,
                    proved=proved,
                    fully_proved=fully_proved,
                    can_state=can_state,
                    can_prove=can_prove,
                )
            ],
            stated=stated,
            proved=proved,
            fully_proved=fully_proved,
        )
    return statuses


def _classify(
    node: Node,
    *,
    definition: bool,
    stated: bool,
    proved: bool,
    fully_proved: bool,
    can_state: bool,
    can_prove: bool,
) -> str:
    if node.mathlib:
        return "mathlib"
    if fully_proved:
        return "fully_proved"
    if proved:
        return "defined" if definition else "proved"
    if stated:
        return "can_prove" if can_prove else "stated"
    if node.not_ready:
        return "not_ready"
    return "can_state" if can_state else "planned"


def topological_order(graph: Graph) -> list[str]:
    """Order nodes so every prerequisite precedes its dependents.

    ``load_graph`` rejects cycles, so a plain depth-first walk suffices; the
    ``visiting`` guard only protects callers who build a ``Graph`` by hand.
    """
    order: list[str] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in seen or node_id in visiting:
            return
        visiting.add(node_id)
        for dependency in graph.nodes[node_id].dependencies:
            if dependency in graph.nodes:
                visit(dependency)
        visiting.discard(node_id)
        seen.add(node_id)
        order.append(node_id)

    for node_id in sorted(graph.nodes):
        visit(node_id)
    return order


def summarize(statuses: dict[str, NodeStatus]) -> list[tuple[State, int]]:
    """Count nodes per state, in legend order, omitting empty states."""
    counts = {state.key: 0 for state in STATES}
    for status in statuses.values():
        counts[status.key] += 1
    return [(state, counts[state.key]) for state in STATES if counts[state.key]]


__all__ = [
    "DEFINITION_DECLARATIONS",
    "STATES",
    "NodeStatus",
    "State",
    "derive",
    "is_definition",
    "summarize",
    "topological_order",
]
