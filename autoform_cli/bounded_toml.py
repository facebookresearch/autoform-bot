"""Parse bounded TOML without exposing parser recursion failures."""

from __future__ import annotations

from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


class BoundedTomlError(ValueError):
    """TOML is malformed or exceeds the accepted structural depth."""


def loads_bounded_toml(text: str, *, max_depth: int) -> dict[str, Any]:
    """Parse a TOML document whose syntax and result stay within ``max_depth``."""
    if _syntactic_depth_exceeds(text, max_depth):
        raise BoundedTomlError("TOML nesting limit exceeded")
    try:
        payload = tomllib.loads(text)
    except (ValueError, RecursionError, MemoryError) as error:
        raise BoundedTomlError("invalid TOML") from error
    if _semantic_depth_exceeds(payload, max_depth):
        raise BoundedTomlError("TOML nesting limit exceeded")
    return payload


def _syntactic_depth_exceeds(text: str, limit: int) -> bool:
    """Bound bracket and dotted-key nesting before invoking the parser."""
    depth = 0
    key_components = 0
    in_key = True
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif quote[0] == '"' and character == "\\":
                escaped = True
            elif len(quote) == 3 and character == quote[0]:
                run_end = index
                while run_end < len(text) and text[run_end] == quote[0]:
                    run_end += 1
                if run_end - index >= 3:
                    index = run_end - 1
                    quote = None
            elif len(quote) == 1 and character == quote:
                quote = None
        elif character == "#":
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline
            continue
        elif text.startswith("'''", index) or text.startswith('\"\"\"', index):
            quote = text[index : index + 3]
            index += 2
        elif character in "'\"":
            quote = character
        elif character in "[{":
            depth += 1
            if depth > limit:
                return True
            if character == "{":
                in_key, key_components = True, 0
        elif character in "]}":
            depth = max(0, depth - 1)
        elif character in "\n,":
            in_key, key_components = True, 0
        elif character == "=":
            in_key = False
        elif character == "." and in_key:
            key_components += 1
            if depth + key_components > limit:
                return True
        index += 1
    return False


def _semantic_depth_exceeds(value: Any, limit: int) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            if depth > limit:
                return True
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            if depth > limit:
                return True
            stack.extend((child, depth + 1) for child in current)
    return False


__all__ = ["BoundedTomlError", "loads_bounded_toml"]
