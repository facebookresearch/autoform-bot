"""Strict reading of the JSON Lines files a tool generates: library indexes and pinned name lists.

Every byte is untrusted, so these helpers refuse anything a schema does not
describe, and they echo text that a third party chose only cut short.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from .._strict_json import reject_json_constant
from ..lean import _ASCII_IDENTIFIER

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
_ECHO_LIMIT = 80


class RecordError(ValueError):
    """A generated file, or one of its records, is not one this reader accepts."""


def relative_path(value: object) -> PurePosixPath | None:
    """Return ``value`` as a path that stays inside the directory it is joined to, or ``None``."""

    if type(value) is not str or not value or any(character in value for character in "\\:\0"):
        return None
    if value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        return None
    return PurePosixPath(value)


def shorten(value: object) -> str:
    """Return ``value`` as text cut to a short length, for echoing text that a third party chose."""

    text = str(value)
    return text if len(text) <= _ECHO_LIMIT else text[:_ECHO_LIMIT] + "..."


def _decode(data: bytes, subject: str, limit: int) -> str:
    """``data`` as text; refuses more than ``limit`` bytes, a byte-order mark, and anything but UTF-8."""

    if len(data) > limit:
        raise RecordError(f"{subject} is larger than {limit // (1024 * 1024)} MiB")
    if data.startswith(b"\xef\xbb\xbf"):
        raise RecordError(f"{subject} starts with a byte-order mark")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise RecordError(f"{subject} is not valid UTF-8") from None


def _record(line: str, where: str) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        record: dict[str, object] = {}
        for key, value in pairs:
            if key in record:
                raise RecordError(f"{where} repeats the key {shorten(key)}")
            record[key] = value
        return record

    try:
        record = json.loads(line, object_pairs_hook=unique, parse_constant=reject_json_constant)
    except RecordError:
        raise
    except (RecursionError, ValueError):
        raise RecordError(f"{where} is not JSON") from None
    if type(record) is not dict:
        raise RecordError(f"{where} is not a JSON object")
    try:
        # A \ud800 escape decodes to a lone surrogate, which no UTF-8 text can hold.
        json.dumps(record, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise RecordError(f"{where} holds text that is not valid Unicode") from None
    return record


def _keys(record: dict[str, object], expected: frozenset[str], where: str) -> None:
    for key in sorted(record.keys() - expected):
        raise RecordError(f"{where}: unexpected key {shorten(key)}")
    for key in sorted(expected - record.keys()):
        raise RecordError(f"{where}: {key} is missing")


def _field(record: dict[str, object], key: str, where: str, accepts) -> object:
    value = record[key]
    if not accepts(value):
        raise RecordError(f"{where}: {key} is not a value this schema allows")
    return value


def _module_name(value: object) -> bool:
    return type(value) is str and all(_ASCII_IDENTIFIER.fullmatch(part) for part in value.split("."))
