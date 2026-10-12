"""JSON that ``json.loads`` refuses, for tests of readers that must refuse it cleanly."""

import json
import sys

import pytest


def _too_deep_to_decode(text: str) -> bool:
    try:
        json.loads(text)
    except RecursionError:
        return True
    return False


def _undecodable_json(damage: str) -> str:
    """JSON that json.loads refuses with a RecursionError or a plain ValueError;
    skips the calling test where this interpreter would decode it."""

    if damage == "nested":
        text = '{"schema": ' + "[" * 100_000 + "]" * 100_000 + "}"
        if not _too_deep_to_decode(text):
            pytest.skip("this stack holds a hundred thousand nested arrays")
        return text
    digits = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if not digits:
        pytest.skip("this interpreter converts an integer of any length")
    return '{"schema": 1' + "0" * digits + "}"
