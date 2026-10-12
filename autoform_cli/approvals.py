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

from .graph import Graph
from .readback import Readback
from .review import ReviewBundle, ReviewError


SELF_APPROVED = "self-approved"


@dataclass(frozen=True, slots=True)
class ApprovalAttestation:
    """Evidence that ``reviewer`` approved exactly ``review_hash`` for one article."""

    node_id: str
    review_hash: str
    reviewer: str
    method: str
    reference: str


@dataclass(frozen=True, slots=True)
class ApprovalStatus:
    """One current approval and, when a verifier found it, who made it."""

    node_id: str
    review_hash: str
    attestation: ApprovalAttestation | None = None
    reason: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.attestation is not None

    @property
    def label(self) -> str:
        if self.attestation is None:
            return SELF_APPROVED
        return f"approved by @{self.attestation.reviewer} ({self.attestation.method.replace('-', ' ')})"


def approval_statuses(graph: Graph, approvals: Mapping[str, str]) -> dict[str, ApprovalStatus]:
    """Label each approval self-approved.

    Without a verifier nothing is authenticated and no network is used.
    """

    return {
        node_id: ApprovalStatus(node_id, review_hash, reason="no approval verifier was run")
        for node_id, review_hash in sorted(approvals.items())
    }


def current_approvals(
    graph: Graph,
    bundle: ReviewBundle,
    readbacks: Mapping[tuple[str, str], Readback],
) -> dict[str, str]:
    """Map each article whose ``review_approved`` equals its current review hash to that hash.

    ``bundle`` must already be validated against ``graph``, and ``readbacks``
    are the cards it was checked with. Articles with incomplete testimony or a
    stale hash are left out; ``review check`` reports those separately.
    """

    approvals: dict[str, str] = {}
    for node in graph.nodes.values():
        if node.review_approved is None or node.article_id is None or bundle.article(node.article_id) is None:
            continue
        try:
            expected = bundle.review_hash(node.article_id, readbacks)
        except ReviewError:
            continue
        if node.review_approved == expected:
            approvals[node.id] = expected
    return approvals


__all__ = [
    "ApprovalAttestation",
    "ApprovalStatus",
    "approval_statuses",
    "current_approvals",
]
