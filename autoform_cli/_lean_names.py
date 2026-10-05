"""Parse Lean surface names and render them as structural ``Name`` terms."""

from __future__ import annotations

import json
from dataclasses import dataclass


_LEAN_ID_BEGIN_ESCAPE = "«"
_LEAN_ID_END_ESCAPE = "»"


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

    parts: list[LeanNamePart] = []
    index = 0
    while index < len(name):
        character = name[index]
        if character == _LEAN_ID_BEGIN_ESCAPE:
            end = name.find(_LEAN_ID_END_ESCAPE, index + 1)
            if end < 0:
                break
            parts.append(LeanNamePart(name[index + 1 : end], True))
            index = end + 1
        elif _lean_is_id_first(character):
            start = index
            index += 1
            while index < len(name) and _lean_is_id_rest(name[index]):
                index += 1
            parts.append(LeanNamePart(name[start:index], False))
        elif "0" <= character <= "9":
            start = index
            while index < len(name) and "0" <= name[index] <= "9":
                index += 1
            parts.append(LeanNamePart(name[start:index], False))
        else:
            break
        if index == len(name):
            return tuple(parts)
        if name[index] != ".":
            break
        index += 1
    raise LeanNameError(f"invalid Lean name: {name!r}")


def _lean_is_id_first(character: str) -> bool:
    return character == "_" or "a" <= character <= "z" or "A" <= character <= "Z" or _lean_is_letter_like(character)


def _lean_is_id_rest(character: str) -> bool:
    return (
        "a" <= character <= "z"
        or "A" <= character <= "Z"
        or "0" <= character <= "9"
        or character in "_'!?"
        or _lean_is_letter_like(character)
        or _lean_is_subscript_alnum(character)
    )


def _lean_is_letter_like(character: str) -> bool:
    code = ord(character)
    return (
        (0x3B1 <= code <= 0x3C9 and code != 0x3BB)
        or (0x391 <= code <= 0x3A9 and code not in {0x3A0, 0x3A3})
        or 0x3CA <= code <= 0x3FB
        or 0x1F00 <= code <= 0x1FFE
        or 0x2100 <= code <= 0x214F
        or 0x1D49C <= code <= 0x1D59F
        or (0xC0 <= code <= 0xFF and code not in {0xD7, 0xF7})
        or 0x100 <= code <= 0x17F
    )


def _lean_is_subscript_alnum(character: str) -> bool:
    code = ord(character)
    return 0x2080 <= code <= 0x2089 or 0x2090 <= code <= 0x209C or 0x1D62 <= code <= 0x1D6A or code == 0x2C7C


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
