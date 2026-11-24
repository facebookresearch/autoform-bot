"""Stops, failures and other runs' edits at the steps a write goes through, named in one place.

The annotation writer records each step before its file changes, and ``links set`` and ``move`` read their result
back before they keep it. The tests that stop a run, fail one of its files, or let another run edit a file meanwhile
say where with :class:`Faults`; it is the only code that names the functions those steps go through, so renaming one
fails here rather than in every such test.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from autoform_cli import annotation_writer, links_set, move

#: Each step, and the function each call of it goes through.
_STEPS: dict[str, tuple[ModuleType, str]] = {
    # One file written or put back, and one removed, each only if it holds what the step expects.
    "write": (annotation_writer, "atomic_write"),
    "remove": (annotation_writer, "_remove_if"),
    # The folders an undone step had made.
    "remove folders": (annotation_writer, "_remove_folders"),
    # A whole write, and a whole undo.
    "write files": (annotation_writer, "write"),
    "undo": (annotation_writer, "undo"),
    # The facts taken out of a page's frontmatter.
    "strip": (annotation_writer, "strip_facts"),
    "links set read-back": (links_set, "_read_back"),
    "move read-back": (move, "_read_back"),
    # The modes a move gives the files it wrote, and the folders it removes once they are empty.
    "move modes": (move, "_keep_modes"),
    "move emptied folders": (move, "_remove_emptied"),
}

_MADE = object()


@dataclass
class Rule:
    """What happens at the calls of a step one :meth:`Faults.at` names; ``fired`` counts them."""

    path: Path | None
    name: str | None
    when: Callable[[object], bool] | None
    nth: Collection[int] | None
    before: Callable[[], object] | None
    raises: BaseException | type[BaseException] | None
    returns: object
    instead: Callable[..., object] | None
    after: Callable[[], object] | None
    raises_after: BaseException | type[BaseException] | None
    seen: int = 0
    fired: int = 0

    def matches(self, first: object) -> bool:
        if self.path is not None and first != self.path:
            return False
        if self.name is not None and not (isinstance(first, Path) and first.name == self.name):
            return False
        if self.when is not None and not self.when(first):
            return False
        self.seen += 1
        return self.nth is None or self.seen in self.nth


class Faults:
    """The rules a test sets on the steps of a run, undone with the test's ``monkeypatch``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self._rules: dict[str, list[Rule]] = {}

    def at(
        self,
        step: str,
        *,
        path: Path | None = None,
        name: str | None = None,
        when: Callable[[object], bool] | None = None,
        nth: int | Collection[int] | None = None,
        before: Callable[[], object] | None = None,
        raises: BaseException | type[BaseException] | None = None,
        returns: object = _MADE,
        instead: Callable[..., object] | None = None,
        after: Callable[[], object] | None = None,
        raises_after: BaseException | type[BaseException] | None = None,
    ) -> Rule:
        """At each call of *step*, or only those whose first argument is *path*, a file named *name*, or one *when*
        holds for, and among them the *nth* (a number or several, from 1): call *before*; then raise *raises*, return
        *returns*, call *instead* with the real function and the call's arguments, or else make the call; then call
        *after* and raise *raises_after*. Of a step's rules the first that matches a call applies, and a call none
        matches is made as it is."""

        if step not in self._rules:
            self._rules[step] = []
            module, attribute = _STEPS[step]
            real = getattr(module, attribute)
            rules = self._rules[step]

            def called(*args: object, **kwargs: object) -> object:
                first = args[0] if args else None
                rule = next((rule for rule in rules if rule.matches(first)), None)
                if rule is None:
                    return real(*args, **kwargs)
                rule.fired += 1
                if rule.before is not None:
                    rule.before()
                if rule.raises is not None:
                    raise rule.raises
                if rule.returns is not _MADE:
                    result = rule.returns
                elif rule.instead is not None:
                    result = rule.instead(real, *args, **kwargs)
                else:
                    result = real(*args, **kwargs)
                if rule.after is not None:
                    rule.after()
                if rule.raises_after is not None:
                    raise rule.raises_after
                return result

            self._monkeypatch.setattr(module, attribute, called)
        rule = Rule(
            path,
            name,
            when,
            (nth,) if isinstance(nth, int) else nth,
            before,
            raises,
            returns,
            instead,
            after,
            raises_after,
        )
        self._rules[step].append(rule)
        return rule
