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

from dataclasses import dataclass, replace
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
#: keeps a chapter quiet when most of it is still open. Finished fills are
#: light tints, deepest for mathlib, so that done work does not outshout the
#: blue a contributor can pick up next, and so that a card's title can stay in
#: the page's ink on every fill; only the status word under the title (text)
#: and the stroke carry the colour. A small mark of a finished state is filled
#: with its stroke and is then the only status signal, so the four finished
#: strokes keep 1.5:1 between neighbours: darkest for mathlib on light and
#: brightest for it on dark, so more finished always reads stronger. Dark
#: defined keeps 3:1 on a selected rail row too. The four greens span the
#: lightness that keeps 3:1 on the map, so the filled amber and grey marks
#: (not ready, inventory pending) each match some green's lightness and part
#: from it by hue alone, which some colour-blind readers lose. The hues are
#: Facebook's semantic set -- #31A24C green, #0064E0 blue, #F7B928 amber,
#: #B0B3B8 grey -- so that finished, actionable, blocked and untouched read at
#: a glance without anyone learning a legend. Green tracks proof progress, blue
#: marks what a contributor can pick up now, amber marks what nothing can start
#: on. Violet marks a proof that compiles but rests on an open statement (a
#: theorem whose Lean proof is still ``sorry``): filled because the work is
#: done, never green so that it cannot be mistaken for a sorry-free proof.
#:
#: Dark is not light dimmed: on #18191A a saturated fill closes up, so dark
#: states are near-black panels with a bright stroke and label (mathlib's
#: stroke, the top of the ladder above, outshines its label). The outlined
#: states take the #242526 surface, not the page's #18191A, so that on the
#: page and on the explorer's map they still read as cards, and planned's grey
#: stroke keeps 3:1 against both.
STATES: tuple[State, ...] = (
    State("mathlib", "in mathlib", "#C6E6CF", "#123C20", "#134426",
          "#10281A", "#B2F3BA", "#8BE78B"),
    State("fully_proved", "fully proved", "#D7EEDD", "#1B5B2E", "#1E6B34",
          "#13301E", "#4DD35F", "#8BE78B"),
    State("proved", "proved", "#E5F4E9", "#277B3E", "#1E6B34",
          "#122A1B", "#32AC4E", "#6BD97F"),
    State("defined", "defined", "#EEF8F0", "#349D4D", "#1E6B34",
          "#122A1B", "#288841", "#6BD97F"),
    State("conditional", "conditionally proved", "#E9DFFC", "#6B3FCF", "#2E1065",
          "#231A36", "#9F7AEA", "#D6C8FA"),
    State("can_prove", "ready to prove", "#FFFFFF", "#0064E0", "#0064E0",
          "#101F33", "#2D88FF", "#7FB8FF"),
    State("stated", "statement formalized", "#FFFFFF", "#2B8F44", "#22773A",
          "#242526", "#31A24C", "#E4E6EB"),
    State("can_state", "ready to state", "#FFFFFF", "#0082FB", "#0B4EA2",
          "#242526", "#2D88FF", "#E4E6EB"),
    State("not_ready", "not ready", "#FFF3D6", "#B77900", "#5C3D00",
          "#2E2205", "#F7B928", "#FFD772"),
    State("planned", "planned", "#FFFFFF", "#CED0D4", "#65676B",
          "#242526", "#737578", "#B0B3B8"),
)

_BY_KEY = {state.key: state for state in STATES}

#: Neutral statuses for module catalogs, which record inspection of existing
#: Lean rather than formalization targets: checked once fully proved, pending
#: until then. Each takes a grey stroke of its own, apart from planned's, so
#: planned targets and both kinds of inventory stay apart in a status bar:
#: in each scheme any two of the three greys keep 1.5:1 between them, and
#: on dark each keeps 3:1 against its fill and the page.
INVENTORY_STATES: tuple[State, ...] = (
    replace(
        _BY_KEY["planned"],
        key="inventory_checked",
        label="inventory checked",
        stroke="#65676B",
        dark_stroke="#C8CACE",
    ),
    # Pending must remain a visible grey card on the light sunken map, with
    # a label that stays readable after the metadata's muted colour mix.
    State("inventory_pending", "inventory pending", "#BDC3CB", "#85898E", "#45494F",
          "#242526", "#9EA1A6", "#B0B3B8"),
)

#: What each state means, for the legends and the explorer's swatches.
MEANINGS = {
    "mathlib": "Upstreamed into Mathlib.",
    "fully_proved": "Proved, and every prerequisite is fully proved too.",
    "proved": "Proof compiles, but something it rests on is not finished.",
    "defined": "Definition is written in Lean.",
    "conditional": "Proof compiles, but rests on an open statement without a recorded Lean proof.",
    "can_prove": "Statement is in Lean and nothing it needs is blocked, so the proof can start.",
    "stated": "Statement is in Lean; the proof is not.",
    "can_state": "Everything its statement uses is in Lean, so the statement can be written in Lean.",
    "not_ready": "Needs more blueprint work before it can be attempted.",
    "planned": "Described in the blueprint only.",
    "inventory_checked": "Module inventory checked in full; it records inspection, not a mathematical proof.",
    "inventory_pending": "Module inventory not yet checked in full; it records inspection, not a mathematical proof.",
}


@dataclass(frozen=True, slots=True)
class NodeStatus:
    """The derived progress of a single node.

    ``waiting_on`` names the prerequisites that keep an unproved node from its
    next phase, in authored order. ``assumes`` names the open statements (not
    proved, but stated or a theorem whose ``statement: retracted`` keeps its
    ``lean:`` declaration in the build) a proof of this node rests on; it is
    empty unless the project allows open statements. A module catalog is never
    an open statement, but a proof resting on one with ``lean:`` assumes the
    open statements the catalog rests on.
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


def declaration_command(declaration: str | None) -> str:
    """The command a ``declaration`` value names, without the modifiers before it: ``def`` in ``noncomputable def``."""
    words = (declaration or "").split()
    while len(words) > 1 and words[0].casefold() in _DECLARATION_MODIFIERS:
        words.pop(0)
    return " ".join(words)


def is_definition(node: Node) -> bool:
    """Whether *node* introduces data instead of a proposition."""
    return declaration_command(node.declaration).casefold() in DEFINITION_DECLARATIONS


def derive(graph: Graph) -> dict[str, NodeStatus]:
    """Return the derived status of every node in *graph*, keyed by node id.

    Readiness follows the project's policy. Under the default strict policy CI
    rejects every ``sorry``, so a theorem's statement lands only with its proof:
    both phases wait for the statement prerequisites to be stated and the proof
    prerequisites to be proved. When ``roadmap/README.md`` sets
    ``open_statements: allowed``, a theorem's statement may land with a ``sorry``
    proof, so a statement waits only for its statement prerequisites and a proof
    for every prerequisite to be stated. A definition has no ``sorry`` to land
    with, so its statement waits for every prerequisite to be stated.
    """
    open_policy = graph.open_statements
    statuses: dict[str, NodeStatus] = {}
    # The open statements a proof reaches through each node, as the Lean walk
    # would: an open statement contributes itself and whatever its statement
    # reaches, and a proved node or a module catalog with `lean:` is entered
    # fully.
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
            # A definition cannot land open: writing it down is its proof, so
            # its body uses its proof prerequisites, and they must be stated.
            can_state = not unstated and not (definition and unmet)
            can_prove = stated and not unstated and not unmet
            waiting = unstated + unmet if stated or definition else unstated
            # Mathlib and unstated nodes reach nothing, so they get no entry,
            # except an article recording `statement: retracted`: a revision
            # retracted its statement while its `lean:` still names the old
            # declaration, which stays in the build, sorry and all, until
            # Formalize restates it. A draft `lean:` on a never-stated theorem
            # is not an assumption. A retracted definition is not open, but its
            # body still reaches what it used. A module catalog licenses no sorry
            # either: one with `lean:` is entered, stated or not, as a definition
            # is, so nothing that rests on it assumes it.
            if not node.mathlib:
                reached = frozenset().union(*(reaches.get(other, frozenset()) for other in node.dependencies))
                assumes = tuple(sorted(reached))
                if proved or ((definition or node.catalog is not None) and node.lean):
                    reaches[node_id] = reached
                elif stated or node.statement_retracted:
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
    "INVENTORY_STATES",
    "MEANINGS",
    "STATES",
    "NodeStatus",
    "State",
    "derive",
    "is_definition",
    "summarize",
    "topological_order",
]
