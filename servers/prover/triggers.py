"""STRUCTURED steering triggers — deterministic signals over the event stream.

Phase 2 of the steering plan (proposal #8): instead of asking an LLM judge
"is the run off-course?" on a wall-clock cadence, the driver feeds every
normalized :class:`~servers.prover.base.Event` through this engine, and the
judge is consulted only when a **tier-0 structured signal** actually fires —
detection is deterministic and free; the model is reserved for confirmation of
the one signal that genuinely needs judgement. Most signals compose their own
correction, so most steers cost zero judge calls.

The five signals (all pure functions of the observed events; per-signal
cooldowns replace the old blanket ``min_gap_s`` cadence):

* ``repeated_build_error`` — the *same* error (normalized fingerprint: paths and
  numbers stripped) has occurred N times. Self-composing.
* ``sorry_not_decreasing`` — the last K payload-bearing edits have not reduced
  the ``sorry``/``admit`` count. Self-composing.
* ``off_goal_edits`` — K consecutive ``.lean`` edits outside the target
  module. **Not** self-composing (legitimate lemma-hunting looks identical to
  drift), so this one summons the judge.
* ``stall`` — reasoning continues but no edit/tool/build activity for T
  seconds. Self-composing.
* ``forbidden_token`` — an edit *wrote* a discipline-violating token (a new
  ``axiom``, ``native_decide``) into the project. Self-composing, immediate.

Everything here is stdlib-pure and backend-agnostic: the engine sees only
normalized events (their ``path``/``payload`` fields are populated by the
adapters), the clock is injectable, and no method ever blocks or calls out.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from .base import Event, EventKind

SIGNAL_REPEATED_ERROR = "repeated_build_error"
SIGNAL_SORRY_STUCK = "sorry_not_decreasing"
SIGNAL_OFF_GOAL = "off_goal_edits"
SIGNAL_STALL = "stall"
SIGNAL_FORBIDDEN = "forbidden_token"

_SORRY_RE = re.compile(r"\b(?:sorry|admit)\b")
_WORD_RE = re.compile(r"[a-z0-9]+")
# A NEW axiom keyword at line start, or native_decide anywhere. Deliberately
# high-precision (an `axiom` inside an identifier like `axiom_of_choice` does
# not match): a trigger is an early-warning steer, not the gate — the honesty
# gate still catches everything; false positives here waste a steer.
_FORBIDDEN_RE = re.compile(r"(?m)^\s*axiom\b|\bnative_decide\b")
_PATHLIKE_RE = re.compile(r"[\w./\\-]+\.(?:lean|olean|c|o)\b")
_NUM_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")


def error_fingerprint(text: str) -> str:
    """Normalize an error so "the same error" matches across paths/line numbers."""
    t = (text or "").strip().lower()
    t = _PATHLIKE_RE.sub("<path>", t)
    t = _NUM_RE.sub("<n>", t)
    t = _WS_RE.sub(" ", t)
    return t[:160]


@dataclass(frozen=True)
class Trigger:
    """One fired structured signal.

    ``correction`` is the deterministic corrective instruction when the signal
    can compose its own (most can); ``""`` means the signal needs the tier-1
    judge to decide whether/how to steer (currently only ``off_goal_edits``).
    """

    signal: str
    detail: str
    correction: str = ""


@dataclass
class TriggerConfig:
    """Thresholds and per-signal cooldowns (seconds). All injectable in tests."""

    repeat_error_threshold: int = 3
    sorry_window: int = 3
    off_goal_threshold: int = 2
    stall_seconds: float = 900.0
    cooldown_s: dict[str, float] = field(default_factory=lambda: {
        SIGNAL_REPEATED_ERROR: 300.0,
        SIGNAL_SORRY_STUCK: 600.0,
        SIGNAL_OFF_GOAL: 300.0,
        SIGNAL_STALL: 900.0,
        SIGNAL_FORBIDDEN: 60.0,
    })


class TriggerEngine:
    """Accumulates the run's events and fires cooldown-gated structured signals.

    One engine per run (it is stateful: fingerprints, streaks, the stall
    clock). The driver calls :meth:`observe` for every event and acts on the
    returned :class:`Trigger`\\ s per its capability policy; :meth:`summary`
    lands in the result meta as telemetry either way, so even a backend that is
    never steered mid-run (``BETWEEN_TURNS``) reports what the signals saw —
    the dispatch layer can fold that into the *next attempt's* prompt.

    Args:
        node_hint: The target node id — either a natural-language plan id
            (``"Chernoff bound"``, the production shape per the plan schema) or
            a dotted Lean-style name (``"Foo.Bar.baz_thm"``). Split into words
            and matched against the *whole words* of an edit path, so
            ``Chernoff bound`` matches ``ProbBook/Chernoff.lean`` while
            ``Bar`` does NOT match ``Barrier/``. ``""`` disables the off-goal
            signal (no hint → never flag).
        config: Thresholds and cooldowns.
        clock: Injectable monotonic clock (tests pass a fake).
    """

    def __init__(
        self,
        *,
        node_hint: str = "",
        config: TriggerConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = config or TriggerConfig()
        self._clock = clock
        self._goal_words = {w for w in _WORD_RE.findall(node_hint.lower()) if len(w) > 2}
        self._goal_path = (node_hint.replace(".", "/") + ".lean") if node_hint else ""
        self._fp_counts: Counter[str] = Counter()
        self._fp_fired: set[str] = set()
        self._sorry_history: list[int] = []
        self._foreign_streak = 0
        self._last_progress = clock()
        self._last_fired: dict[str, float] = {}
        self.fired: Counter[str] = Counter()
        self.suppressed: Counter[str] = Counter()

    # ------------------------------------------------------------------ core

    def observe(self, event: Event) -> list[Trigger]:
        """Feed one event; return the signals that fire on it (post-cooldown)."""
        out: list[Trigger] = []
        now = self._clock()
        kind = event.kind

        # Stall: activity kinds reset the clock; pure reasoning past the budget
        # fires (and resets, so the next stall needs a fresh quiet stretch).
        if kind in (EventKind.EDIT, EventKind.TOOL, EventKind.ERROR, EventKind.RESULT):
            self._last_progress = now
        elif kind in (EventKind.THINKING, EventKind.MESSAGE):
            quiet = now - self._last_progress
            if quiet > self._cfg.stall_seconds:
                self._emit(
                    out, SIGNAL_STALL,
                    f"no edit/tool activity for {int(quiet // 60)} min while reasoning continues",
                    correction=(
                        "No file edits or tool runs for a long stretch while reasoning "
                        "continues. Commit to the most promising approach and TEST it "
                        "now — edit the file and run the build/REPL — instead of "
                        "planning further."
                    ),
                )
                self._last_progress = now

        if kind is EventKind.ERROR:
            self._observe_error(out, event)
        elif kind is EventKind.EDIT:
            self._observe_edit(out, event)
        return out

    def summary(self) -> dict:
        """Telemetry for the result meta: what fired, what cooldowns swallowed."""
        return {"fired": dict(self.fired), "suppressed": dict(self.suppressed)}

    # ------------------------------------------------------------- internals

    def _emit(self, out: list[Trigger], signal: str, detail: str, correction: str = "") -> bool:
        """Fire ``signal`` unless its cooldown swallows it; True iff it fired."""
        now = self._clock()
        cooldown = self._cfg.cooldown_s.get(signal, 300.0)
        last = self._last_fired.get(signal)
        if last is not None and (now - last) < cooldown:
            self.suppressed[signal] += 1
            return False
        self._last_fired[signal] = now
        self.fired[signal] += 1
        out.append(Trigger(signal=signal, detail=detail, correction=correction))
        return True

    def _observe_error(self, out: list[Trigger], event: Event) -> None:
        fp = error_fingerprint(event.content)
        if not fp:
            return
        self._fp_counts[fp] += 1
        n = self._fp_counts[fp]
        # Each distinct fingerprint fires at most once (repeats past the
        # threshold are the SAME stuck loop, not new information) — but it is
        # consumed only by an ACTUAL fire: a threshold-crossing swallowed by the
        # signal cooldown re-arms, so the loop gets its steer on a later repeat
        # instead of losing it for the whole run.
        if n >= self._cfg.repeat_error_threshold and fp not in self._fp_fired:
            first_line = (event.content or "").strip().splitlines()[0][:200]
            fired = self._emit(
                out, SIGNAL_REPEATED_ERROR,
                f"same error x{n}: {first_line}",
                correction=(
                    f"The same build error has now occurred {n} times: \"{first_line}\". "
                    "Stop repeating the failing approach — read the FULL error, check "
                    "the imports and namespaces it names, and fix the root cause "
                    "before editing again."
                ),
            )
            if fired:
                self._fp_fired.add(fp)

    def _observe_edit(self, out: list[Trigger], event: Event) -> None:
        payload = event.payload or ""
        path = event.path or ""

        if payload:
            hit = _FORBIDDEN_RE.search(payload)
            if hit:
                token = hit.group(0).strip()
                self._emit(
                    out, SIGNAL_FORBIDDEN,
                    f"wrote `{token}` to {path or 'a file'}",
                    correction=(
                        f"You just wrote `{token}` into {path or 'the project'}. That "
                        "violates the prover discipline (no new axioms, no "
                        "native_decide). Remove it and prove honestly — or reply "
                        "FAILED — <the concrete blocker>."
                    ),
                )
            count = len(_SORRY_RE.findall(payload))
            self._sorry_history.append(count)
            if len(self._sorry_history) >= self._cfg.sorry_window:
                tail = self._sorry_history[-self._cfg.sorry_window:]
                if tail[-1] > 0 and all(b >= a for a, b in zip(tail, tail[1:])):
                    self._emit(
                        out, SIGNAL_SORRY_STUCK,
                        f"sorry count non-decreasing across {len(tail)} edits (now {tail[-1]})",
                        correction=(
                            f"Your last {len(tail)} edits have not reduced the "
                            f"sorry/admit count (now {tail[-1]}). Focus on eliminating "
                            "ONE existing sorry completely rather than restructuring "
                            "or adding scaffolding."
                        ),
                    )
                    self._sorry_history = []  # restart accumulation post-signal

        if path.endswith(".lean"):
            if self._on_goal(path):
                self._foreign_streak = 0
            else:
                self._foreign_streak += 1
                if self._foreign_streak >= self._cfg.off_goal_threshold:
                    self._foreign_streak = 0
                    self._emit(
                        out, SIGNAL_OFF_GOAL,
                        f"{self._cfg.off_goal_threshold} consecutive edits outside "
                        f"the target module (latest: {path})",
                        correction="",  # judgement call: lemma-hunting vs drift → judge
                    )

    def _on_goal(self, path: str) -> bool:
        if not self._goal_words and not self._goal_path:
            return True  # no hint → never flag an edit as off-goal
        p = path.lower()
        if self._goal_path and p.endswith(self._goal_path.lower()):
            return True
        # Whole-word overlap, with the ``.lean`` extension dropped first so the
        # word "lean" in a hint can never blanket-match every source file. Word
        # matching (not substring) keeps "Bar" from matching "Barrier".
        stem = p[:-5] if p.endswith(".lean") else p
        return bool(self._goal_words & set(_WORD_RE.findall(stem)))
