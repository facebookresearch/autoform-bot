"""Normalize Autoform's authored declaration-intent vocabulary.

This is blueprint policy, not a Lean parser.  The artifact probe asks Lean for
the actual declaration kind and compares it with this normalized intent.
"""

from __future__ import annotations


DECLARATION_KIND_ALIASES = {
    "abbrev": "abbrev",
    "axiom": "axiom",
    "class": "class",
    "corollary": "theorem",
    "def": "def",
    "definition": "def",
    "inductive": "inductive",
    "instance": "instance",
    "lemma": "theorem",
    "opaque": "opaque",
    "proposition": "theorem",
    "structure": "structure",
    "theorem": "theorem",
}

_SOURCE_KEYWORDS = {
    "abbrev": frozenset({"abbrev"}),
    "axiom": frozenset({"axiom"}),
    "class": frozenset({"class"}),
    "def": frozenset({"def"}),
    "inductive": frozenset({"inductive"}),
    "instance": frozenset({"instance"}),
    "opaque": frozenset({"opaque"}),
    "structure": frozenset({"structure"}),
    "theorem": frozenset({"lemma", "theorem"}),
}


def declaration_kind(intent: str | None) -> str | None:
    """Return the kernel-checkable kind represented by authored intent."""

    if intent is None:
        return None
    return DECLARATION_KIND_ALIASES.get(intent.strip().casefold())


def declaration_keywords(intent: str | None) -> frozenset[str] | None:
    """Return source keywords accepted for an authored declaration intent."""

    kind = declaration_kind(intent)
    return _SOURCE_KEYWORDS.get(kind) if kind is not None else None


__all__ = ["DECLARATION_KIND_ALIASES", "declaration_kind", "declaration_keywords"]
