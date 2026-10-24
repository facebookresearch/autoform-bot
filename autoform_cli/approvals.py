"""Who approved a statement review, as opposed to whether it is current.

``review_approved`` records the hash of the complete review surface. Matching
the current hash shows only that nothing changed since someone wrote it down;
``autoform review check`` prints that hash, so anyone can paste it, and every
current approval is shown as self-approved.

``review_approved`` is thus a drift checksum. Who may change it is repository
policy: a CODEOWNERS rule giving ``blueprint/`` a code owner, under a ruleset
that requires code-owner review and approval of the most recent push (see
README.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .review import ReviewState


SELF_APPROVED = "self-approved"


@dataclass(frozen=True, slots=True)
class ApprovalStatus:
    """One current approval and how it is labelled."""

    node_id: str
    review_hash: str

    @property
    def label(self) -> str:
        return SELF_APPROVED


def approval_statuses(states: Mapping[str, ReviewState]) -> dict[str, ApprovalStatus]:
    """Label each article whose ``review_approved`` is its current review hash
    self-approved; nothing authenticates an approval, and no network is used."""

    return {
        node_id: ApprovalStatus(node_id, state.review_hash)
        for node_id, state in sorted(states.items())
        if state.status == "approved"
    }


__all__ = [
    "ApprovalStatus",
    "approval_statuses",
]
