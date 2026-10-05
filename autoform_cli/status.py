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
#: separate proof obligation. ``axiom`` stays out: counting an assumption as
#: proved would hide it.
DEFINITION_DECLARATIONS = frozenset(
    {
        "abbrev",
        "class",
        "def",
        "definition",
        "inductive",
        "instance",
        "irreducible_def",
        "opaque",
        "structure",
    }
)

#: Modifiers that may precede the command without changing what it introduces,
#: as in ``noncomputable def``.
_DECLARATION_MODIFIERS = frozenset(
    {"local", "noncomputable", "partial", "private", "protected", "scoped", "unsafe"}
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
#: contributor can pick up now, amber marks what nothing can start on. Violet
#: marks a proof that compiles but rests on an open statement (a theorem whose
#: Lean proof is still ``sorry``): filled because the work is done, never green
#: so that it cannot be mistaken for a sorry-free proof.
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
    State("conditional", "conditionally proved", "#E9DFFC", "#6B3FCF", "#2E1065",
          "#231A36", "#9F7AEA", "#D6C8FA"),
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
    """The derived progress of a single node.

    ``waiting_on`` names the prerequisites that keep an unproved node from its
    next phase, in authored order. ``assumes`` names the open statements (stated,
    not proved) a proof of this node rests on; it is empty unless the project
    allows open statements.
    """

    node_id: str
    state: State
    stated: bool
    proved: bool
    fully_proved: bool
    can_state: bool = False
    can_prove: bool = False
    assumes: tuple[str, ...] = ()
    waiting_on: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.state.key

    @property
    def label(self) -> str:
        return self.state.label


def is_definition(node: Node) -> bool:
    """Whether *node* introduces data instead of a proposition."""
    words = (node.declaration or "").casefold().split()
    while len(words) > 1 and words[0] in _DECLARATION_MODIFIERS:
        words.pop(0)
    return " ".join(words) in DEFINITION_DECLARATIONS


def derive(graph: Graph) -> dict[str, NodeStatus]:
    """Return the derived status of every node in *graph*, keyed by node id.

    Readiness follows the project's policy. Under the default strict policy CI
    rejects every ``sorry``, so a theorem's statement lands only with its proof:
    both phases wait for the statement prerequisites to be stated and the proof
    prerequisites to be proved. When ``roadmap/README.md`` sets
    ``open_statements: allowed``, a statement may land with a ``sorry`` proof, so
    a statement waits only for its statement prerequisites and a proof for every
    prerequisite to be stated.
    """
    open_policy = graph.open_statements
    statuses: dict[str, NodeStatus] = {}
    # The open statements a proof reaches through each node, as the Lean walk
    # would: an open statement contributes itself and whatever its statement
    # reaches, and a proved node is entered fully.
    reaches: dict[str, frozenset[str]] = {}
    for node_id in topological_order(graph):
        node = graph.nodes[node_id]
        definition = is_definition(node)
        # A definition carries no proof obligation, so writing it down proves it.
        stated = node.statement_formalized or node.mathlib
        proved = node.proof_formalized or node.mathlib or (definition and stated)

        def done(dependency_id: str, attribute: str) -> bool:
            dependency = statuses.get(dependency_id)
            return dependency is not None and getattr(dependency, attribute)

        unstated = [other for other in node.statement_dependencies if not done(other, "stated")]
        # A sorry proof compiles under the open policy, so there a proof
        # prerequisite only has to be stated.
        needed = "stated" if open_policy else "proved"
        unmet: list[str] = []
        for other in node.proof_dependencies:
            if other not in unstated and other not in unmet and not done(other, needed):
                unmet.append(other)
        assumes: tuple[str, ...] = ()
        if open_policy:
            can_state = not unstated
            can_prove = stated and not unstated and not unmet
            waiting = unstated + unmet if stated else unstated
            # Mathlib and unstated nodes reach nothing, so they get no entry.
            if not node.mathlib:
                reached = frozenset().union(*(reaches.get(other, frozenset()) for other in node.dependencies))
                assumes = tuple(sorted(reached))
                if proved:
                    reaches[node_id] = reached
                elif stated:
                    reaches[node_id] = frozenset({node_id}).union(
                        *(reaches.get(other, frozenset()) for other in node.statement_dependencies)
                    )
        else:
            can_state = not unstated and not unmet
            can_prove = stated and can_state
            waiting = unstated + unmet
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
                    assumes=assumes,
                )
            ],
            stated=stated,
            proved=proved,
            fully_proved=fully_proved,
            can_state=can_state,
            can_prove=can_prove,
            assumes=assumes,
            waiting_on=() if proved else tuple(waiting),
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
    assumes: tuple[str, ...],
) -> str:
    if node.mathlib:
        return "mathlib"
    if fully_proved:
        return "fully_proved"
    if proved:
        if assumes:
            return "conditional"
        return "defined" if definition else "proved"
    if stated:
        return "can_prove" if can_prove else "stated"
    if node.not_ready:
        return "not_ready"
    return "can_state" if can_state else "planned"


def topological_order(graph: Graph) -> list[str]:
    """Order nodes so every prerequisite precedes its dependents.

    ``load_graph`` rejects cycles. The explicit stack keeps the same depth-first
    order without depending on Python's recursion limit; the ``visiting`` guard
    only protects callers who build a ``Graph`` by hand.
    """
    order: list[str] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    for root_id in sorted(graph.nodes):
        if root_id in seen:
            continue
        visiting.add(root_id)
        frames = [(root_id, 0)]
        while frames:
            node_id, dependency_index = frames[-1]
            dependencies = graph.nodes[node_id].dependencies
            if dependency_index == len(dependencies):
                frames.pop()
                visiting.discard(node_id)
                seen.add(node_id)
                order.append(node_id)
                continue

            dependency = dependencies[dependency_index]
            frames[-1] = (node_id, dependency_index + 1)
            if dependency not in graph.nodes or dependency in seen or dependency in visiting:
                continue
            visiting.add(dependency)
            frames.append((dependency, 0))
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
