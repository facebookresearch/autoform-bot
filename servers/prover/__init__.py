"""Backend-neutral prover execution over canonical runtime nodes.

Claude, Codex, and Muse adapters normalize their event streams into one shared
contract. The driver applies bounded steering, cancellation, and verification;
Lean diagnostics are delegated to the main-owned shared runtime.
"""

from __future__ import annotations

from .base import Event, EventKind, ProofResult, ProverAdapter, Run

__all__ = [
    "Event",
    "EventKind",
    "ProofResult",
    "ProverAdapter",
    "Run",
]
