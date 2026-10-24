"""Find the elaboration limits a Lean declaration is elaborated under.

``set_option maxHeartbeats N`` changes the limit until the end of the enclosing
``section`` or ``namespace``, or of the file; ``set_option ... in`` changes it
for the one command that follows. A raised limit costs nothing to write and
hides slow automation, so a library accumulates them: a file-wide raise covers
every later declaration whether or not it needs one, and ``0`` removes the
limit, letting elaboration run indefinitely instead of failing. ATLAS v1, built
with Autoform, accumulated 901 raised ``maxHeartbeats`` settings, 497 of them
file- or section-wide and 9 of them ``0`` (autoform-bot issue #260).

Like the declaration index, this is a lexical pass over comment-free source
lines. It tracks ``namespace``/``section``/``mutual``/``end`` nesting and
reads commands at the declaration's own indentation, so a more deeply
indented ``set_option`` is part of the proof above it. That is enough for
code written in the ordinary way, and it reports nothing it cannot see.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Lean's defaults for the limits this module reads.
LIMIT_DEFAULTS = {
    "maxHeartbeats": 200000,
    "synthInstance.maxHeartbeats": 20000,
    "maxRecDepth": 512,
}
#: Options for which ``0`` means no limit at all.
_UNBOUNDED_AT_ZERO = frozenset({"maxHeartbeats", "synthInstance.maxHeartbeats"})

_LIMIT = re.compile(r"set_option\s+(maxHeartbeats|synthInstance\.maxHeartbeats|maxRecDepth)\s+(\d[\d_]*)(\s+in\b)?")
_ANY_OPTION_IN = re.compile(r"set_option\s+\S+\s+\S+\s+in\b\s*")
#: Other commands that, like ``set_option ... in``, only wrap the next command.
_WRAPPER_IN = re.compile(r"(?:open|universe|omit|include|variable)\b.*\bin$")
_ATTRIBUTES_ONLY = re.compile(r"@\[.*\]$")
#: ``section`` with the module system's forms, e.g. ``@[expose] public section``.
_SCOPE_OPEN = re.compile(
    r"(?:@\[[^\]]*\]\s*)*(?:(?:public|private|meta|noncomputable)\s+)*section\b|namespace\s|mutual$"
)
_SCOPE_CLOSE = re.compile(r"end\b")
#: Unindented lines that still belong to the declaration above them.
_CONTINUATION = re.compile(r"(?:termination_by|decreasing_by|where|deriving)\b|\|")


@dataclass(frozen=True, slots=True)
class LimitSetting:
    """One ``set_option`` for an elaboration limit, and how far it reaches.

    ``scope`` is ``declaration`` for ``set_option ... in`` before the
    declaration, ``section`` or ``file`` for a setting that lasts until the
    enclosing ``end`` or the end of the file, and ``proof`` for one inside the
    declaration's own body.
    """

    option: str
    value: int
    line: int
    scope: str

    @property
    def unbounded(self) -> bool:
        return self.value == 0 and self.option in _UNBOUNDED_AT_ZERO

    @property
    def raised(self) -> bool:
        return self.unbounded or self.value > LIMIT_DEFAULTS[self.option]


def limits_in_force(lines: Sequence[str], line: int) -> tuple[LimitSetting, ...]:
    """Return the limit settings for the declaration that starts on ``line``.

    ``lines`` are the file's lines with comments removed and line numbers kept.
    For each option set at all, the innermost setting in force at the
    declaration is returned, followed by every setting inside its proof.
    """

    return limits_by_line(lines, (line,)).get(line, ())


def limits_by_line(lines: Sequence[str], starts: Iterable[int]) -> dict[int, tuple[LimitSetting, ...]]:
    """Return :func:`limits_in_force` for each line in ``starts``.

    The file is read once per indentation those lines start at, not once per
    line, so the cost stays linear in the file however many declarations it holds.
    """

    # Commands start at the declaration's indentation, so a file that indents
    # its commands inside a namespace is read the same way as one that does not.
    by_indent: dict[int, set[int]] = {}
    for line in starts:
        if 0 < line <= len(lines):
            target = lines[line - 1]
            by_indent.setdefault(len(target) - len(target.lstrip()), set()).add(line)
    return {line: found for indent, group in by_indent.items() for line, found in _scan(lines, indent, group).items()}


def _scan(lines: Sequence[str], indent: int, starts: set[int]) -> dict[int, tuple[LimitSetting, ...]]:
    """Read the limits for the declarations on ``starts``, which all start at ``indent``."""

    def is_command(text: str) -> bool:
        stripped = text.strip()
        return bool(stripped) and len(text) - len(text.lstrip()) <= indent and not _CONTINUATION.match(stripped)

    found: dict[int, list[LimitSetting]] = {}
    scopes: list[dict[str, LimitSetting]] = [{}]
    pending: dict[str, LimitSetting] = {}
    for number, text in enumerate(lines[: max(starts)], start=1):
        if number in starts:
            in_force = {**scopes[-1], **pending}
            found[number] = [in_force[option] for option in sorted(in_force)]
        if not is_command(text):
            continue
        stripped = text.strip()
        rest = stripped
        while wrapper := _ANY_OPTION_IN.match(rest):
            limit = _LIMIT.match(rest)
            if limit is not None and limit.end() <= wrapper.end():
                pending[limit.group(1)] = _setting(limit, number, "declaration")
            rest = rest[wrapper.end() :]
        if not rest or _WRAPPER_IN.match(rest) or _ATTRIBUTES_ONLY.match(rest):
            continue
        limit = _LIMIT.match(rest)
        if limit is not None and rest is stripped and limit.group(3) is None:
            scope = "file" if len(scopes) == 1 else "section"
            scopes[-1][limit.group(1)] = _setting(limit, number, scope)
        elif _SCOPE_OPEN.match(rest):
            # A wrapper before ``mutual`` reaches every declaration in the block.
            scopes.append({**scopes[-1], **pending})
        elif _SCOPE_CLOSE.match(rest) and len(scopes) > 1:
            scopes.pop()
        pending = {}

    for line, settings in found.items():
        for number in range(line, len(lines) + 1):
            text = lines[number - 1]
            if number > line and is_command(text):
                break
            settings.extend(_setting(limit, number, "proof") for limit in _LIMIT.finditer(text))
    return {line: tuple(settings) for line, settings in found.items()}


def _setting(limit: re.Match[str], line: int, scope: str) -> LimitSetting:
    return LimitSetting(limit.group(1), int(limit.group(2).replace("_", "")), line, scope)


__all__ = ["LIMIT_DEFAULTS", "LimitSetting", "limits_by_line", "limits_in_force"]
