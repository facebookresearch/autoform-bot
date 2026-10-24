"""Decide from the Lean probe's records and the source text which declarations a library index holds.

The probe reports what only the environment knows. The keyword a declaration
is written with, the name as written, its header and a wanted result's
docstring exist only in the source, so they are read here.

A declaration is indexed when all of these hold:

1. It is not a constructor, recursor, projection, auxiliary recursor,
   ``noConfusion`` or matcher, and its name is not an internal detail.
2. It has a declaration range.
3. Its range starts with a declaration keyword, after any docstring,
   attributes and modifiers. This leaves out what a command or attribute
   generates beside a declaration, and ``where`` and ``let rec`` helpers.
4. The source text at its selection range is its name, a dot-separated suffix
   of it, or the keyword ``instance``. A leading ``_root_.`` is not part of the
   written name, so ``theorem _root_.Nat.foo`` is indexed as ``Nat.foo``, and
   the quotes of a component such as ``«odd name»`` are not compared.

A record's ``line`` is that of the declaration's first modifier or, with none,
of its keyword, not of the docstring or an attribute above it. Its
``statement`` starts there and ends at the first of these, outside brackets,
comments, strings and character literals: a ``:=``, unless it belongs to a
``let`` or ``have`` written in the type; the word ``where``; a ``|`` that
starts a line, or is followed by a ``=>`` before the next ``:=``, unless it
belongs to an absolute value (a ``|`` and a closing ``|`` with no space inside,
as in ``|x|``, or, at the start of a line, a ``|`` that follows a ``:``, a
comma or an infix sign and has a closing ``|`` before any ``=>``), a ``|`` that
is part of an operator such as ``<|`` being no bar; and, for an ``inductive``
type, the first ``|``. The statement may be cut short and never holds a part
of the body. A wanted placeholder has no alternatives and no ``where``: its
statement runs to the end of the command, or to its ``:=`` when a
``def_wanted`` or ``instance_wanted`` is written with a body.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass

from .index import KINDS, IndexDeclaration

#: Axioms a complete declaration may rest on.
STANDARD_AXIOMS = frozenset({"propext", "Quot.sound", "Classical.choice"})
#: The Batteries commands that declare a wanted result, and the kind each is indexed as.
WANTED_COMMANDS = {
    "theorem_wanted": "theorem",
    "proof_wanted": "theorem",
    "def_wanted": "def",
    "instance_wanted": "instance",
}
_MODIFIERS = frozenset(
    {"public", "private", "protected", "noncomputable", "unsafe", "partial", "nonrec", "scoped", "local", "meta"}
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPEN, _CLOSE = "([{⟨", ")]}⟩"
#: The words that bind a name in a term with `:=`.
_BINDERS = frozenset({"let", "letI", "have", "haveI"})
#: The colon, the comma and the infix signs: what follows one is as a rule a term. Some also end
#: a type (`ℕ+`, `ℝ*`), so one of them alone does not show that a `|` after it opens a term.
_OPERAND_NEXT = frozenset(":,→↔∧∨¬=≠≤≥<>+-*/^∣∈∉⊆⊂∩∪⊔⊓∘×•↦\\")
_CONSTANT_KEYS = frozenset(
    {
        "record",
        "name",
        "module",
        "private",
        "wrapper",
        "range",
        "selection",
        "signature",
        "docstring",
        "mentions",
        "axioms",
    }
)


class ProbeOutputError(ValueError):
    """The library probe printed something this generator does not read."""


@dataclass(frozen=True, slots=True)
class ProbeConstant:
    name: str
    module: str
    private: bool
    #: ``ProofWanted``, ``DefWanted`` or ``DerivedWanted`` when the type's conclusion is one of them.
    wrapper: str | None
    #: One-based lines and zero-based columns in code points, as Lean reports them.
    range: tuple[tuple[int, int], tuple[int, int]]
    selection: tuple[tuple[int, int], tuple[int, int]]
    signature: str
    docstring: str | None
    mentions: tuple[str, ...]
    axioms: tuple[str, ...]


def parse_probe_output(
    text: str, marker: str
) -> tuple[dict[str, str | None], tuple[ProbeConstant, ...], dict[str, tuple[str, ...]]]:
    """Decode the probe's marked lines into module headers, constants and shared names.

    A shared name is a public one that more than one library module holds, with those modules.
    """

    modules: dict[str, str | None] = {}
    constants: list[ProbeConstant] = []
    shared: dict[str, tuple[str, ...]] = {}
    for line in text.split("\n"):
        if not line.startswith(marker):
            continue
        try:
            record = json.loads(line[len(marker) :])
        except ValueError:
            raise ProbeOutputError("the library probe printed a record that is not JSON") from None
        if type(record) is not dict:
            raise ProbeOutputError("the library probe printed a record that is not an object")
        if record.get("record") == "module":
            name, doc = record.get("name"), record.get("module_doc", 0)
            if (
                type(name) is not str
                or not (doc is None or type(doc) is str)
                or record.keys() != {"record", "name", "module_doc"}
            ):
                raise ProbeOutputError("the library probe printed a malformed module record")
            if name in modules:
                raise ProbeOutputError(f"the library probe reported module {name} twice")
            modules[name] = doc
        elif record.get("record") == "constant":
            constants.append(_constant(record))
        elif record.get("record") == "shared":
            name, holders = record.get("name"), record.get("modules")
            if (
                record.keys() != {"record", "name", "modules"}
                or type(name) is not str
                or not name
                or type(holders) is not list
                or any(type(holder) is not str or not holder for holder in holders)
                or len(holders) < 2
                or len(set(holders)) != len(holders)
            ):
                raise ProbeOutputError("the library probe printed a malformed shared-name record")
            if name in shared:
                raise ProbeOutputError(f"the library probe reported the shared name {name} twice")
            shared[name] = tuple(holders)
        else:
            raise ProbeOutputError("the library probe printed a record of an unknown kind")
    return modules, tuple(constants), shared


def _constant(record: dict[str, object]) -> ProbeConstant:
    def span(value: object) -> tuple[tuple[int, int], tuple[int, int]]:
        if (
            type(value) is not list
            or len(value) != 2
            or any(
                type(point) is not list or len(point) != 2 or any(type(n) is not int for n in point)
                for point in value
            )
        ):
            raise ProbeOutputError("the library probe printed a malformed source range")
        (line, column), (end_line, end_column) = value
        if line < 1 or column < 0 or (end_line, end_column) < (line, column):
            raise ProbeOutputError("the library probe printed a malformed source range")
        return (line, column), (end_line, end_column)

    def strings(value: object) -> tuple[str, ...]:
        if type(value) is not list or any(type(item) is not str for item in value):
            raise ProbeOutputError("the library probe printed a malformed name list")
        return tuple(value)

    if record.keys() != _CONSTANT_KEYS:
        raise ProbeOutputError("the library probe printed a malformed constant record")
    texts = {key: record[key] for key in ("name", "module", "signature")}
    optional = {key: record[key] for key in ("wrapper", "docstring")}
    if (
        any(type(value) is not str or not value for value in texts.values())
        or any(not (value is None or type(value) is str) for value in optional.values())
        or type(record["private"]) is not bool
    ):
        raise ProbeOutputError("the library probe printed a malformed constant record")
    return ProbeConstant(
        **texts,
        **optional,
        private=record["private"],
        range=span(record["range"]),
        selection=span(record["selection"]),
        mentions=strings(record["mentions"]),
        axioms=strings(record["axioms"]),
    )


def classify(constant: ProbeConstant, source: str) -> IndexDeclaration | str:
    """Return the index record for ``constant``, or why it is left out.

    ``generated``: its range does not start with a declaration keyword, so a
    command or attribute produced it beside the declaration it is written on.
    ``name``: the text at its name is not the name. ``axioms``: it is an axiom,
    or rests on one other than the three standard ones. ``wrapper``: it has the type of
    a wanted placeholder but was not declared with a wanted command.

    A range that is not in ``source`` raises: the probe did not compile this text.
    """

    lines = source.replace("\r\n", "\n").split("\n")
    for what, span in (("", constant.range), ("its name ", constant.selection)):
        for at, column in span:
            if at > len(lines) or column > len(lines[at - 1]):
                raise ProbeOutputError(
                    f"the probe places {what}{constant.name} in {constant.module} at line {at}, column {column}, "
                    "which is outside the source file; the source is not the one the library was built from"
                )
    (line, column), (end_line, end_column) = constant.range
    body = "\n".join([*lines[line - 1 : end_line - 1], lines[end_line - 1][:end_column]])[column:]
    docstring, keyword, start = _leading(body)
    if keyword in WANTED_COMMANDS:
        if constant.wrapper is None:
            return "generated"
        kind, status = WANTED_COMMANDS[keyword], "wanted"
        if docstring is None:
            docstring = _docstring_above("\n".join(lines[: line - 1]) + "\n" + lines[line - 1][:column])
    elif constant.wrapper is not None:
        return "wrapper"
    elif keyword == "axiom":
        return "axioms"
    elif keyword not in KINDS:
        return "generated"
    else:
        kind, status, docstring = keyword, "complete", constant.docstring

    (selection_line, selection_column), (selection_end_line, selection_end_column) = constant.selection
    written = (
        lines[selection_line - 1][selection_column:selection_end_column]
        if selection_line == selection_end_line
        else ""
    )
    # `_root_.` leaves the current namespace and is no part of the name. Lean prints a name with
    # `«»` around a component only where it needs them, whatever the source has.
    spelled, name = _unquoted(written.removeprefix("_root_.")), _unquoted(constant.name)
    # Lean selects the keyword of an anonymous `instance`. Batteries gives the name it makes for an
    # anonymous `instance_wanted` the range of the whole command, so there the selection is the range.
    anonymous = written == "instance" or (keyword == "instance_wanted" and constant.selection == constant.range)
    if not anonymous and name != spelled and not name.endswith(f".{spelled}"):
        return "name"
    if status == "complete" and set(constant.axioms) - STANDARD_AXIOMS:
        return "axioms"
    return IndexDeclaration(
        name=constant.name,
        module=constant.module,
        kind=kind,
        status=status,
        # Where the header starts, which is below the range's first line under a docstring or attributes.
        line=line + body.count("\n", 0, start),
        signature=constant.signature,
        statement=_header(body, start, wanted=status == "wanted"),
        docstring=docstring,
        mentions=tuple(sorted(set(constant.mentions))),
        auto_named=anonymous,
    )


def _unquoted(name: str) -> str:
    return name.replace("«", "").replace("»", "")


def starts_with_module(source: str) -> bool:
    """Whether a Lean file is a module file: its first token is the keyword ``module``."""

    index = _skip_blank(source, 0)
    match = _WORD.match(source, index)
    return match is not None and match.group() == "module"


def _block_end(text: str, start: int) -> int:
    """Return the index after the block comment that opens at ``start``; Lean's block comments nest."""

    depth, index = 0, start
    while index < len(text):
        if text.startswith("/-", index):
            depth, index = depth + 1, index + 2
        elif text.startswith("-/", index):
            depth, index = depth - 1, index + 2
            if depth == 0:
                return index
        else:
            index += 1
    return len(text)


def _line_end(text: str, start: int) -> int:
    end = text.find("\n", start)
    return len(text) if end < 0 else end


def _skip_blank(text: str, index: int) -> int:
    """Skip whitespace and comments."""

    while index < len(text):
        if text[index].isspace():
            index += 1
        elif text.startswith("/-", index):
            index = _block_end(text, index)
        elif text.startswith("--", index):
            index = _line_end(text, index)
        else:
            break
    return index


def _leading(text: str) -> tuple[str | None, str | None, int]:
    """Read past a docstring, attributes and modifiers.

    Returns the docstring, the first word that is not a modifier, and where
    the header starts: at the first modifier, or at that word.
    """

    index, docstring, start = 0, None, None
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if text.startswith("/--", index):
            end = _block_end(text, index)
            docstring, index = text[index + 3 : end - 2].strip(), end
        elif text.startswith("/-", index):
            index = _block_end(text, index)
        elif text.startswith("--", index):
            index = _line_end(text, index)
        elif text.startswith("@[", index):
            depth, index = 0, index + 1  # step over the `@`, or the depth is 0 at once and the loop stops
            while index < len(text):
                depth += (text[index] == "[") - (text[index] == "]")
                index += 1
                if depth == 0:
                    break
        else:
            match = _WORD.match(text, index)
            if match is not None and match.group() in _MODIFIERS:
                start, index = (index if start is None else start), match.end()
                continue
            return docstring, match.group() if match is not None else None, index if start is None else start


def _docstring_above(text: str) -> str | None:
    """The docstring that ends just before the end of ``text``, if there is one."""

    stripped = text.rstrip()
    if not stripped.endswith("-/"):
        return None
    start = stripped.rfind("/--")
    if start < 0 or _block_end(stripped, start) != len(stripped):
        return None
    return stripped[start + 3 : -2].strip()


def _literal_end(text: str, index: int) -> int | None:
    """Where the string, raw string or character literal that starts at ``index`` ends, if one does."""

    character = text[index]
    after = text[index - 1] if index else ""
    # A prime, or an `r` or quote inside a name, is part of that name.
    inside_name = after.isalnum() or after in "_'!?.«"
    if character == '"':
        end = index + 1
        while end < len(text) and text[end] != '"':
            end += 2 if text[end] == "\\" else 1
        return end + 1
    if inside_name:
        return None
    if character == "r":
        quotes = index + 1
        while text.startswith("#", quotes):
            quotes += 1
        if not text.startswith('"', quotes):
            return None
        close = text.find('"' + "#" * (quotes - index - 1), quotes + 1)
        return len(text) if close < 0 else close + quotes - index
    if character == "'":
        if text.startswith("\\", index + 1):
            close = text.find("'", index + 3)
            return index + 3 if close < 0 else close + 1
        if text.startswith("'", index + 2):
            return index + 3
    return None


def _scan(text: str, index: int) -> Iterator[tuple[int, int, bool]]:
    """Walk ``text`` from ``index`` outside comments, strings and white space.

    Yields each character's index, its bracket depth (0 outside all brackets; a bracket itself is
    inside) and whether it is the first on its line. A string, raw string or character literal
    is yielded once, at its opening character.
    """

    depth, line_start = 0, True
    while index < len(text):
        character = text[index]
        if text.startswith("/-", index):
            index = _block_end(text, index)
            continue
        if text.startswith("--", index):
            index = _line_end(text, index)
            continue
        if character.isspace():
            line_start = line_start or character == "\n"
            index += 1
            continue
        end = _literal_end(text, index) if character in "\"'r" else None
        if end is None:
            end = index + 1
        depth += character in _OPEN
        yield index, depth, line_start
        depth -= character in _CLOSE
        line_start, index = False, end


def _header(text: str, start: int, *, wanted: bool = False) -> str:
    """The declaration's header: the source from ``start`` up to its body.

    The body starts at the first `:=`, the word `where`, or a `|` that starts a line or is
    followed by a `=>`, outside brackets, comments and strings. For an `inductive` type it
    starts at the first `|`, which opens a constructor. Two things that look like a body
    are part of the header: the `:=` of a `let` or `have` written in the type, and a `|` that
    opens an absolute value. Where the text does not show which it is, the header ends: it may
    be cut short, and never holds a part of the body. A wanted command has no alternatives and
    no `where`, so only a `:=` ends its header.
    """

    #: Whether a `let` or `have` of the header still waits for its `:=`.
    armed = False
    #: Whether a `let` or `have` of the header is defined by alternatives, which are then part of it.
    defined_by_alternatives = False
    #: Whether the last bar opened an absolute value that is still open.
    absolute = False
    #: Whether the header is of an `inductive` type, whose constructors a `|` introduces.
    inductive = False
    #: The last character that is not white space or a comment.
    last: int | None = None
    end = len(text)
    for index, depth, line_start in _scan(text, start):
        if depth == 0:
            if text.startswith(":=", index):
                if not armed:
                    end = index
                    break
                armed = False
            elif text[index] == "|":
                mid_line = "" if line_start or wanted or inductive else _mid_line_bar(text, index)
                if absolute and not line_start:
                    # The closing bar of an absolute value.
                    absolute = False
                    last = index
                    continue
                if not wanted and (
                    inductive
                    or (line_start and not _opens_absolute_value(text, index, last))
                    or (mid_line == "alternative" and not (armed or defined_by_alternatives))
                ):
                    end = index
                    break
                absolute = mid_line == "absolute"
                if mid_line not in ("absolute", "other"):
                    # A binder defined by alternatives has no `:=` of its own; its bars are its own.
                    defined_by_alternatives = defined_by_alternatives or armed
                    armed = False
            else:
                word = _word(text, index)
                if word == "where" and not wanted:
                    end = index
                    break
                inductive = inductive or word in ("inductive", "coinductive")
                armed = armed or word in _BINDERS
        last = index
    return text[start:end].strip()


def _mid_line_bar(text: str, bar: int) -> str:
    """What the `|` at ``bar``, not the first on its line, is: ``alternative``, ``absolute`` or ``other``.

    A bar that is part of an operator such as `<|` or `|>.` is ``other``. One with no space after
    it whose closing bar, with no space before it, comes before any `=>` is an absolute value. A
    `=>` outside brackets before the next `:=` makes it the start of match alternatives.
    """

    if text[bar - 1 : bar] in ("<", "|") or text[bar + 1 : bar + 2] in (">", "|"):
        return "other"
    opens = text[bar + 1 : bar + 2].strip() != ""
    for index, depth, _line_start in _scan(text, bar + 1):
        if depth != 0:
            continue
        if text.startswith(":=", index):
            break
        if text.startswith("=>", index):
            return "alternative"
        if text[index] == "|" and opens and text[index - 1].strip() != "":
            return "absolute"
    return "other"


def _word(text: str, index: int) -> str | None:
    """The keyword-like word that starts at ``index``, unless it is a part of a longer name."""

    match = _WORD.match(text, index)
    if match is None or (index and (text[index - 1].isalnum() or text[index - 1] in "_.'!?«")):
        return None
    after = text[match.end() : match.end() + 1]
    return None if after.isalnum() or (after and after in "_.'!?»") else match.group()


def _opens_absolute_value(text: str, bar: int, last: int | None) -> bool:
    """Whether the `|` at ``bar``, the first character on its line, opens an absolute value.

    The text must say so on both sides, and when it is in doubt the answer is no. Before the bar,
    the header ends with a sign that needs a right operand, so no type can have ended. After
    the bar, nothing separates it from what it encloses, a closing `|` that does not start a line
    follows, and no `=>` comes outside brackets before the next `:=`: a match alternative has one.
    """

    if last is None or text[last] not in _OPERAND_NEXT or text[bar + 1 : bar + 2].strip() == "":
        return False
    # Mathlib writes a universe-polymorphic sort as `Type*` or `Sort*`, which ends a type.
    if text[last] == "*" and text.endswith(("Type", "Sort"), 0, last):
        return False
    closed = False
    for index, depth, line_start in _scan(text, bar + 1):
        if depth == 0 and text.startswith("=>", index):
            return False
        if depth == 0 and text.startswith(":=", index):
            break
        closed = closed or (depth == 0 and not line_start and text[index] == "|")
    return closed


__all__ = [
    "STANDARD_AXIOMS",
    "WANTED_COMMANDS",
    "ProbeConstant",
    "ProbeOutputError",
    "classify",
    "parse_probe_output",
    "starts_with_module",
]
