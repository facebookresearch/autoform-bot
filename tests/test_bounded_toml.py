from __future__ import annotations

import sys

import pytest

from autoform_cli.bounded_toml import BoundedTomlError, loads_bounded_toml

LIMIT = 128


def test_depth_limit_is_independent_of_python_recursion_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "getrecursionlimit", lambda: 10_000)

    with pytest.raises(BoundedTomlError):
        loads_bounded_toml("value = " + "[" * 129 + "0" + "]" * 129 + "\n", max_depth=LIMIT)


def test_very_deep_toml_fails_without_recursion_errors() -> None:
    with pytest.raises(BoundedTomlError):
        loads_bounded_toml("value = " + "[" * 1500 + "0" + "]" * 1500 + "\n", max_depth=LIMIT)


def test_dotted_table_header_depth_is_bounded() -> None:
    with pytest.raises(BoundedTomlError):
        loads_bounded_toml("[" + ".".join(["a"] * 200) + "]\nvalue = 0\n", max_depth=LIMIT)


def test_multiline_string_terminator_cannot_hide_excessive_depth() -> None:
    nested = "[" * 130 + "0" + "]" * 130
    with pytest.raises(BoundedTomlError):
        loads_bounded_toml('x = """abc""""\ny = ' + nested + "\n", max_depth=LIMIT)


def test_dotted_keys_within_the_limit_still_parse() -> None:
    payload = loads_bounded_toml(
        "[leanOptions]\n"
        "weak.linter.mathlibStandardSet = true\n"
        "# a.b.c.d.e comment must not leak into the next line\n"
        "pp.unicode.fun = true\n",
        max_depth=LIMIT,
    )

    assert payload["leanOptions"]["pp"]["unicode"]["fun"] is True


def test_malformed_toml_is_a_bounded_error() -> None:
    with pytest.raises(BoundedTomlError):
        loads_bounded_toml("name = [\n", max_depth=LIMIT)
