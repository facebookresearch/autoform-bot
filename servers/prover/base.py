"""The prover-backend ADAPTER interface — the one swappable contract.

A backend proves a node by implementing four methods. The *driver*
(:mod:`servers.prover.driver`) and the *steering judge*
(:mod:`servers.prover.steerer`) are written **against this interface alone**, so
they are identical for every backend. Only the
adapter's ``start`` / ``events`` / ``steer`` / ``result`` differ.

The contract the design pins down is::

    (target node + spec) -> proof written back into the node

so an adapter takes a ``node`` (the target id), a ``spec`` (its statement + the
structural hints that make it the right formalization), and the Lean
``project_dir``; it returns a :class:`ProofResult` whose ``status`` is
``"proved"`` or ``"failed"``. Producing the proof is the adapter's whole job — it
does NOT review, score, or touch the sidecar.

Everything here is plain ``dataclass`` / ``ABC`` with no third-party imports, so
the module (and the package contract) imports with no optional dependency
installed.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    """Normalized event kinds the steerer reasons over.

    A backend maps its own native event vocabulary onto these so the *shared*
    steerer never sees a backend-specific event type. ``str``-valued so an event
    window serializes cleanly into the judge prompt.
    """

    THINKING = "thinking"      # the prover's reasoning / planning
    EDIT = "edit"              # a file edit / proof-state change
    MESSAGE = "message"        # assistant prose / status text
    TOOL = "tool"              # a tool call or its result (build, search, …)
    ERROR = "error"            # a compile/proof error or backend error
    RESULT = "result"          # a terminal/summary event
    OTHER = "other"            # anything else (kept, but rarely steered on)


@dataclass
class Event:
    """One normalized event from a running prover.

    Args:
        kind: The :class:`EventKind` this event maps to.
        content: A short text payload (reasoning excerpt, edited file, error
            text, …) — what the steering judge actually reads.
        raw: The backend's native event object, kept for adapters that need it
            (never read by the shared driver/steerer).
        path: For ``EDIT`` (and file-touching ``TOOL``) events: the file path
            the event touched, when the backend exposes it. The structured
            steering triggers (:mod:`servers.prover.triggers`) use it for
            on-goal/off-goal attribution; ``""`` = unknown.
        payload: For ``EDIT`` events: the text actually *written* (the new
            file/patch content), when the backend exposes it. The triggers
            compute sorry-counts and forbidden-token hits from it — normalized
            here precisely so the trigger layer stays backend-agnostic;
            ``""`` = unknown.
    """

    kind: EventKind
    content: str = ""
    raw: Any = None
    path: str = ""
    payload: str = ""

    def render(self, *, limit: int = 300) -> str:
        """One-line ``[KIND] content`` rendering for the steer window."""
        text = (self.content or "").strip().replace("\n", " ")
        if len(text) > limit:
            text = text[:limit] + "…"
        return f"[{self.kind.value}] {text}"


@dataclass
class Run:
    """Opaque handle to one in-flight proving run.

    The driver threads this back into ``events`` / ``steer`` / ``result``; only
    the owning adapter interprets its fields. ``goal`` is carried here so the
    driver and steerer never need the spec separately.
    """

    backend: str
    goal: str = ""
    project_dir: str = ""
    handle: Any = None                       # the adapter's native run object
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProofResult:
    """Outcome of a proving run — the proof written into the node, or a failure.

    Args:
        status: ``"proved"`` or ``"failed"`` (the only two terminal verdicts the
            backend reports; it never self-certifies beyond this).
        proof_text: The Lean proof / changed content on success (or a best-effort
            summary of what was landed).
        reason: A short human-readable reason — required on ``"failed"`` (the
            honest blocker), optional on ``"proved"``.
        backend: Which backend produced the result.
        landed_files: Number of files written into the project (informational).
        meta: Backend-specific extras (project id, task id, …) — never required
            by the driver.
    """

    status: str
    proof_text: str = ""
    reason: str = ""
    backend: str = ""
    landed_files: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def proved(self) -> bool:
        return self.status == "proved"


class SteeringCapability(str, Enum):
    """How a backend can be steered — the granularity at which a correction lands.

    The driver reads this to choose a per-backend steering policy (see
    :mod:`servers.prover.driver`), so the loop stays backend-agnostic while doing
    the *right* thing per tier instead of one-size-fits-all:

    * ``NONE`` — a terminal API tool loop or sampling backend with no resumable
      host session. No live judge and no fold; a correction can only enter the
      *next whole attempt*, handled above the driver.
    * ``BETWEEN_TURNS`` — a headless CLI (``claude -p`` / ``codex exec``) whose
      correction can land only as the *next turn* of a resumed session (a live
      judgement is delivered turn-granularly, not mid-turn). The per-event live
      judge is **low-value for its cost here** — a judge call per event window
      *plus* an extra resumed turn — so the driver **skips it by default** and
      relies instead on the deterministic **verify-gate fold**: the honesty
      gate's own reason, fed back verbatim as one corrective turn. Correctness is
      unaffected either way — the honesty gate still protects every verdict; what
      is traded off is general mid-run *drift*-steering for the CLI backends,
      recoverable via ``judge_policy="always"`` and, later, the structured
      triggers of proposal #8 phase 2.
    * ``AT_TOOL_CALLS`` — a session exposing tool-call-boundary hooks (the Agent
      SDK path, proposal #6). No adapter implements it yet; reserved so the
      driver's policy is written against the *capability*, not a backend name.
      Treated like ``BETWEEN_TURNS`` for the fold (a hook session is resumable).
    * ``IN_FLIGHT`` — a live task that accepts a mid-run correction (Aristotle's
      ``project.ask``). The per-event live judge drives it; its result is
      terminal, so it does not fold.

    The default (:attr:`ProverAdapter.steering`) is ``BETWEEN_TURNS`` — the honest
    floor for a headless CLI: an adapter is assumed only turn-granular unless it
    declares otherwise.
    """

    NONE = "none"
    BETWEEN_TURNS = "between_turns"
    AT_TOOL_CALLS = "at_tool_calls"
    IN_FLIGHT = "in_flight"


class ProverAdapter(abc.ABC):
    """The one interface a backend implements; the driver/steerer use only this.

    Implementations:

    * :class:`servers.prover.claude_adapter.ClaudeAdapter`
    * :class:`servers.prover.codex_adapter.CodexAdapter`
    * :class:`servers.prover.muse_adapter.MuseAdapter`

    The four methods are the *entire* per-backend surface. Adapters expose these
    synchronous signatures so the driver is a plain loop with no event-loop
    assumptions.
    """

    #: The value selected by the MCP tool's ``backend`` argument.
    name: str = "abstract"

    #: The granularity at which this backend's :meth:`steer` lands (see
    #: :class:`SteeringCapability`). The driver keys its per-backend steering
    #: policy — live judge vs verify-gate fold — off this flag, never off
    #: :attr:`name`. Default is the honest floor for a headless CLI.
    steering: SteeringCapability = SteeringCapability.BETWEEN_TURNS

    @abc.abstractmethod
    def start(self, node: str, spec: str, project_dir: str) -> Run:
        """Launch a proving run for ``node`` against ``spec`` in ``project_dir``.

        Returns a :class:`Run` handle (carrying the ``goal`` the steerer judges
        against). Must not block on completion — the driver pulls progress via
        :meth:`events`.
        """

    @abc.abstractmethod
    def events(self, run: Run):
        """Yield :class:`Event`\\ s as the run progresses, ending when terminal.

        An iterator (generator). Each item is a normalized :class:`Event`; the
        driver appends it to the steer window. When the iterator is exhausted the
        run is finished and the driver calls :meth:`result`.

        RE-ENTRANCY CONTRACT (fold-capable adapters): for a backend whose
        :attr:`steering` is ``BETWEEN_TURNS`` or ``AT_TOOL_CALLS``, the driver's
        verify-gate fold may call :meth:`steer` *after* this iterator exhausted
        and then call ``events(run)`` **again**. That re-entry must run ONLY the
        queued corrective turn — never replay the initial turn. The CLI adapters
        implement this with a ``started`` flag on their run state; a new
        fold-capable adapter must do the equivalent.
        """

    @abc.abstractmethod
    def steer(self, run: Run, message: str) -> None:
        """Inject a corrective ``message`` into the live run (in-flight steer).

        Called by the driver only when the *shared* steerer decides the run is
        off-course. Best-effort: a steer that cannot be delivered (run already
        finished, transient API error) must not raise — it logs and is dropped.
        """

    def bind_cancel_event(self, cancel_event: Any) -> None:
        """Bind an optional cancellation event before :meth:`start`.

        Adapters that own cancellable subprocesses override this. The default is
        a no-op so lightweight and externally managed adapters remain compatible.
        """

    @abc.abstractmethod
    def result(self, run: Run) -> ProofResult:
        """Collect the terminal :class:`ProofResult` once :meth:`events` ends."""
