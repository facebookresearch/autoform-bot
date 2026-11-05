"""Strict JSON loading hooks: no duplicate keys and no non-finite numbers."""

from __future__ import annotations

from typing import Any


def reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value
