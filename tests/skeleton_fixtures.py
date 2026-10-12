"""Skeleton values the review tests build on."""

from __future__ import annotations

from dataclasses import replace

from autoform_cli.skeleton import DeclarationSkeleton

SEMANTIC = '{"generated":[],"root":{"safety":"safe","type":{"sort":{"zero":null}}}}'
BLUEPRINT_HASH = "sha256:" + "0" * 64


def declaration(name: str = "Review.result", **fields: object) -> DeclarationSkeleton:
    """A theorem with nothing in its trust boundary; ``fields`` replace any field."""

    return replace(
        DeclarationSkeleton(
            name=name,
            kind="theorem",
            module="Review",
            path="Review.lean",
            start_line=1,
            end_line=1,
            signature=f"{name} : True",
            raw_signature=f"{name} : True",
            semantic=SEMANTIC,
            lean_version="4.32.2",
            depends=(),
            trusted=(),
            assumed=(),
            assumed_semantics=(),
            boundary_modules=(),
            axioms=(),
            axiom_semantics=(),
        ),
        **fields,
    )
