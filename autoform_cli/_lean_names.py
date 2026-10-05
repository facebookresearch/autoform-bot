"""Parse Lean surface names and render them as structural ``Name`` terms."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


_LEAN_NAME_PART = r"«([^»]+)»|([^.\s«»]+)"
_LEAN_NAME = re.compile(rf"(?:{_LEAN_NAME_PART})(?:\.(?:{_LEAN_NAME_PART}))*")


class LeanNameError(ValueError):
    """A surface spelling cannot represent a Lean ``Name``."""


@dataclass(frozen=True, slots=True)
class LeanNamePart:
    """One component of a dot-separated Lean surface name."""

    text: str
    quoted: bool

    @property
    def numeric(self) -> bool:
        """Whether Lean parses this component as ``Name.num`` rather than ``Name.str``."""

        return not self.quoted and self.text.isascii() and self.text.isdigit()


def parse_lean_name(name: str) -> tuple[LeanNamePart, ...]:
    """Parse the surface spelling of a Lean ``Name`` without interpreting its text.

    Guillemets quote one component, so ``A.«b.c d»`` has two components.
    Plain ASCII decimal components are marked numeric; quoted decimals remain
    strings.
    """

    if not _LEAN_NAME.fullmatch(name):
        raise LeanNameError(f"invalid Lean name: {name!r}")
    return tuple(
        LeanNamePart(quoted or plain, bool(quoted))
        for quoted, plain in re.findall(_LEAN_NAME_PART, name)
    )


def render_lean_name_term(name: str) -> str:
    """Render *name* as a Lean term built only from ``Name.str`` and ``Name.num``."""

    result = "Name.anonymous"
    for part in parse_lean_name(name):
        if part.numeric:
            # Preserve ``int``'s prior leading-zero normalization without its
            # bounded conversion of an arbitrarily large decimal component.
            digits = part.text.lstrip("0") or "0"
            result = f"Name.num ({result}) {digits}"
        else:
            result = f"Name.str ({result}) {json.dumps(part.text, ensure_ascii=False)}"
    return result


__all__ = ["LeanNameError", "LeanNamePart", "parse_lean_name", "render_lean_name_term"]
