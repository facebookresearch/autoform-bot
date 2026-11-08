"""The vault's ``tex-macros.json``: the project macros the site's formulas may use.

A blueprint data contract, not site code: review records the macros a
statement uses as :func:`project_macros` keeps them, so changing the format or
:data:`NEUTRALISED` changes approval hashes. The site's MathJax configuration
is built from the same parse.
"""

from __future__ import annotations

import json
import re

#: Project macros, in the form of MathJax's ``tex.macros``, kept in the vault.
TEX_MACROS = "tex-macros.json"

#: The commands of the page's packages that change formulas other than their
#: own (a definition, a tag form, a package option, a label), or give a
#: symbol the color a formula writes, like the site's status marks. The
#: articles on a page share one TeX input, so the input maps each to an
#: undefined command, which shows in red and does nothing, however a formula
#: or a macro reaches it. Other definitions, ``\newcommand``, ``\def``,
#: ``\require`` among them, are in no package the page loads.
NEUTRALISED = (
    "label", "mmlToken", "DeclareMathOperator", "DeclarePairedDelimiter", "DeclarePairedDelimiterX",
    "DeclarePairedDelimiterXPP", "DeclarePairedDelimiters", "DeclarePairedDelimitersX",
    "DeclarePairedDelimitersXPP", "newtagform", "renewtagform", "usetagform", "mathtoolsset",
)

_MACRO_NAME = re.compile(r"[A-Za-z]+")


def project_macros(data: bytes | None) -> dict[str, object]:
    """The macros the site defines from ``data``, a ``tex-macros.json``'s
    bytes or ``None`` when there is none: those
    :func:`~autoform_cli.ui.mathjax.mathjax_script` keeps."""

    return _tex_macros(data)[0]


def _tex_macros(data: bytes | None) -> tuple[dict[str, object], list[str]]:
    """The macros in ``data``, a ``tex-macros.json``'s bytes or ``None`` when
    there is none, and what is wrong with the ones it leaves out.

    Each maps a name of letters to a body, to ``[body, arguments]``, or to
    ``[body, arguments, default]``, as MathJax's ``tex.macros`` does, and is
    not one of :data:`NEUTRALISED`. A file that is no such object gives no
    macros.
    """

    if data is None:
        return {}, []
    try:
        macros = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_names)
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        return {}, [f"{TEX_MACROS}: not valid JSON: {exc}"]
    if not isinstance(macros, dict):
        return {}, [f"{TEX_MACROS}: must be a JSON object from macro names to definitions"]
    issues: list[str] = []
    kept: dict[str, object] = {}
    for name, definition in macros.items():
        if not _MACRO_NAME.fullmatch(name):
            issues.append(f"{TEX_MACROS}: {name!r} is not a macro name; use letters only")
            continue
        if name in NEUTRALISED or name == "notallowed":
            issues.append(f"{TEX_MACROS}: \\{name} is a command the site turns off; choose another name")
            continue
        if (
            isinstance(definition, list)
            and len(definition) in {2, 3}
            and isinstance(definition[0], str)
            and type(definition[1]) is int
            and all(isinstance(default, str) for default in definition[2:])
        ):
            if not 0 <= definition[1] <= 9:
                issues.append(f"{TEX_MACROS}: \\{name} takes {definition[1]} arguments; use 0 to 9")
                continue
        elif not isinstance(definition, str):
            issues.append(
                f"{TEX_MACROS}: \\{name} must be a body, [body, arguments], or [body, arguments, default]"
            )
            continue
        kept[name] = definition
    return kept, issues


def _unique_names(pairs: list[tuple[str, object]]) -> dict[str, object]:
    found: dict[str, object] = {}
    for name, value in pairs:
        if name in found:
            raise ValueError(f"{name!r} is defined twice")
        found[name] = value
    return found


__all__ = [
    "NEUTRALISED",
    "TEX_MACROS",
    "project_macros",
]
