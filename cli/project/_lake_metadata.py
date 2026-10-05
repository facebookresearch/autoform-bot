"""Pure Lake metadata decoding used by offline project inspection.

This module mirrors only the Lake syntax and resolution fields that Autoform
reports, following the Lake 4.32.2 behavior used by the current release
catalog. It performs no filesystem, process, Git, or network I/O;
orchestration and diagnostics remain in :mod:`cli.project.inspect`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .catalog import canonical_git_url

_TARGET_KINDS = ("lean_lib", "lean_exe", "input_file", "input_dir")  # the kinds lakefile.toml declares
_MATHLIB_NAME = (("str", "mathlib"),)
_LAKE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[^ \t\r\n]+)?")  # Lake's StdVer
_MANIFEST_VERSION = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:-[^ \t\r\n]+)?")
_URL_CREDENTIALS = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@")
_LEAN_ID_BEGIN_ESCAPE = "«"
_LEAN_ID_END_ESCAPE = "»"


class _JsonInteger(str):
    """A JSON integer kept as bounded input text instead of materialized as a Python bigint."""


@dataclass(frozen=True, slots=True)
class LakeTarget:
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class LakeProject:
    config: str
    name: str | None
    version: str | None
    targets: tuple[LakeTarget, ...]


@dataclass(frozen=True, slots=True)
class _Requirements:
    """lakefile.toml's require entries, as Lake resolves them against the root manifest.

    ``mathlib`` is the direct Mathlib requirement Lake keeps, if Lake resolves it from the manifest.
    ``transitive`` is whether another requirement could pull Mathlib in; it is not evidence that the
    dependency's current configuration actually does so, because dependency lakefiles are not read.
    ``declared`` is whether there is any require entry, and ``lookups`` holds the Lean name and
    spelling of each requirement Lake looks up in the manifest and overrides rather than satisfying
    with the root package itself.
    """

    mathlib: dict | None
    transitive: bool
    declared: bool = False
    lookups: tuple[tuple[tuple[tuple[str, str | int], ...], str], ...] = ()


@dataclass(frozen=True, slots=True)
class _LockedPackages:
    """The package names a manifest or package-overrides file records, and its Mathlib entry."""

    names: frozenset[tuple[tuple[str, str | int], ...]]
    mathlib: MathlibLock | None


@dataclass(frozen=True, slots=True)
class MathlibLock:
    """A Lake manifest or package-overrides entry for Mathlib."""

    type: str
    source: str
    inherited: bool
    url: str | None = None
    input_rev: str | None = None
    rev: str | None = None
    dir: str | None = None
    sub_dir: str | None = None
    config_file: str | None = None
    manifest_file: str | None = None

    @property
    def loads_like_a_release(self) -> bool:
        """Whether Lake loads Mathlib from its repository root with its own lakefile and manifest."""

        return (
            self.type == "git"
            and self.sub_dir in (None, "", ".", "./")
            # Extensionless `lakefile` is Lake's default and resolves by
            # preferring lakefile.lean before lakefile.toml.
            and self.config_file in ("lakefile", "lakefile.lean")
            and self.manifest_file == "lake-manifest.json"
        )


def _lakefile_problem(config: dict) -> str | None:
    """Why Lake would refuse the fields Autoform reads, if it would."""

    if not _is_name(config.get("name")):
        return "it has no package name"
    version = config.get("version")
    if version is not None and not (isinstance(version, str) and _LAKE_VERSION.fullmatch(version)):
        return "its version is not major.minor.patch"
    if not all(_are_named_tables(config.get(key, [])) for key in ("require", *_TARGET_KINDS)):
        return "a require or target entry has no name"
    for requirement in config.get("require", []):
        problem = _requirement_problem(requirement)
        if problem is not None:
            return problem
    # Lake's decodeTargetDecls keeps one name map for every target kind, so a
    # reported lean_lib or lean_exe also clashes with an input target.
    targets = [_canonical_toml_name(entry["name"]) for key in _TARGET_KINDS for entry in config.get(key, [])]
    if len(set(targets)) != len(targets):
        return "two targets share a name"
    return None


def _requirement_problem(requirement: dict[str, object]) -> str | None:
    """Validate the fields Lake 4.32's ``Dependency.decodeToml`` consumes."""

    for key in ("rev", "scope"):
        if key in requirement and type(requirement[key]) is not str:
            return f"a require entry has a non-string {key}"
    if "options" in requirement and not (
        type(requirement["options"]) is dict
        and all(type(key) is str and type(value) is str for key, value in requirement["options"].items())
    ):
        return "a require entry has malformed options"
    if "version" in requirement:
        version = requirement["version"]
        if type(version) is not str or not _input_version_is_supported(version):
            return "a require entry has an invalid version constraint"

    # Lake gives `path` precedence over `git`, and `git` precedence over
    # `source`; fields in shadowed source forms are not decoded.
    if "path" in requirement:
        return None if type(requirement["path"]) is str else "a require entry has a non-string path"
    if "git" in requirement:
        git = requirement["git"]
        if type(git) is str:
            if "subDir" in requirement and type(requirement["subDir"]) is not str:
                return "a require entry has a non-string subDir"
            return None
        if type(git) is dict and type(git.get("url")) is str:
            # The table form reads subDir from the inner table but, somewhat
            # surprisingly, reads rev only from the enclosing requirement.
            if "subDir" in git and type(git["subDir"]) is not str:
                return "a require git table has a non-string subDir"
            return None
        return "a require entry has a malformed git source"
    if "source" not in requirement:
        return None
    source = requirement["source"]
    if type(source) is not dict or type(source.get("type")) is not str:
        return "a require entry has a malformed source"
    if source["type"] == "path":
        return None if type(source.get("dir")) is str else "a require path source has no string dir"
    if source["type"] == "git":
        if type(source.get("url")) is not str:
            return "a require git source has no string url"
        for key in ("rev", "subDir"):
            if key in source and type(source[key]) is not str:
                return f"a require git source has a non-string {key}"
        return None
    return "a require source has an unknown type"


def _input_version_is_supported(value: str) -> bool:
    """Recognize Lake 4.32's ``InputVer.parse`` / ``VerRange.parse`` grammar."""

    if value.startswith("git#"):
        return True
    index = 0
    clause_has_term = False
    needs_term = True
    while index < len(value):
        if _lake_whitespace(value[index]):
            index += 1
            continue
        if value[index] == ",":
            if needs_term:
                return False
            needs_term = True
            index += 1
            continue
        if value.startswith("||", index):
            # Lake checks whether the current conjunction has a term, but not
            # its `needsRange` flag here (so even `term, || term` is accepted).
            if not clause_has_term:
                return False
            clause_has_term = False
            needs_term = True
            index += 2
            continue
        end = _version_range_term_end(value, index)
        if end is None:
            return False
        clause_has_term = True
        needs_term = False
        index = end
    return clause_has_term and not needs_term


def _version_range_term_end(value: str, index: int) -> int | None:
    for operator in ("<=", ">=", "!=", "<", "≤", ">", "≥", "=", "≠"):
        if value.startswith(operator, index):
            match = re.match(r"[0-9]+\.[0-9]+\.[0-9]+", value[index + len(operator) :])
            if match is None:
                return None
            end = index + len(operator) + match.end()
            if end < len(value) and value[end] == "-":
                end += 1
                while end < len(value) and not _lake_whitespace(value[end]):
                    end += 1
            return end

    prefix = value[index] if value[index] in "^~" else None
    start = index + 1 if prefix is not None else index
    end = start
    while end < len(value) and (value[end].isascii() and value[end].isalnum() or value[end] in ".*"):
        end += 1
    if end == start:
        return None
    components = value[start:end].split(".")
    if not 1 <= len(components) <= 3 or any(not component for component in components):
        return None
    if prefix is not None:
        if any(not component.isascii() or not component.isdigit() for component in components):
            return None
        suffix = ""
        if end < len(value) and value[end] == "-":
            suffix_start = end + 1
            end = suffix_start
            while end < len(value) and not _lake_whitespace(value[end]):
                end += 1
            suffix = value[suffix_start:end]
            if not suffix:
                return None
        if (
            prefix == "^"
            and len(components) == 3
            and all(_normalize_decimal(component) == "0" for component in components)
            and not suffix
        ):
            return None
        return end

    if end < len(value) and value[end] == "-":
        return None
    wildcards = {"x", "X", "*"}
    first_wild = next((position for position, component in enumerate(components) if component in wildcards), None)
    if first_wild is None:
        return None
    if any(
        component not in wildcards and (not component.isascii() or not component.isdigit()) for component in components
    ):
        return None
    if any(component not in wildcards for component in components[first_wild:]):
        return None
    return end


def _lake_whitespace(character: str) -> bool:
    return character in " \t\r\n"


def _validate_manifest_root(payload: dict[str, object]) -> None:
    """Validate every field decoded by ``Lake.Manifest.fromJson?`` in 4.32."""

    name = payload.get("name")
    if name is not None and (type(name) is not str or _canonical_manifest_name(name) is None):
        raise ValueError("name")
    _json_default(payload, "lakeDir", ".lake", str)
    _json_default(payload, "fixedToolchain", False, bool)
    _json_optional(payload, "packagesDir", str)


def _decode_package_entry(entry: object, source: str) -> tuple[tuple[tuple[str, str], ...], MathlibLock]:
    """Mirror ``Lake.PackageEntry.fromJson?`` for the current manifest layout."""

    if type(entry) is not dict:
        raise ValueError("package entry")
    name = _canonical_manifest_name(_json_required(entry, "name", str))
    if name is None:
        raise ValueError("package name")
    _json_default(entry, "scope", "", str)
    inherited = _json_required(entry, "inherited", bool)
    config_file = _json_default(entry, "configFile", "lakefile", str)
    manifest_file = _json_default(entry, "manifestFile", "lake-manifest.json", str)
    package_type = _json_required(entry, "type", str)
    common = {
        "source": source,
        "inherited": inherited,
        "config_file": config_file,
        "manifest_file": manifest_file,
    }
    if package_type == "path":
        return name, MathlibLock("path", dir=_json_required(entry, "dir", str), **common)
    if package_type != "git":
        raise ValueError("package type")
    return name, MathlibLock(
        "git",
        url=_redact(_json_required(entry, "url", str)),
        input_rev=_json_optional(entry, "inputRev", str),
        rev=_json_required(entry, "rev", str),
        sub_dir=_json_optional(entry, "subDir", str),
        **common,
    )


def _json_required(mapping: dict[str, object], key: str, expected: type):
    if key not in mapping or type(mapping[key]) is not expected:
        raise ValueError(key)
    return mapping[key]


def _json_default(mapping: dict[str, object], key: str, default, expected: type):
    value = mapping.get(key)
    if value is None:
        return default
    if type(value) is not expected:
        raise ValueError(key)
    return value


def _json_optional(mapping: dict[str, object], key: str, expected: type):
    value = mapping.get(key)
    if value is None:
        return None
    if type(value) is not expected:
        raise ValueError(key)
    return value


def _manifest_layout(version: object) -> str | None:
    """Lake reads versions from 0.5.0 through any 1.x; versions before 0.7 are legacy."""

    if isinstance(version, _JsonInteger):
        numeric_version = _normalize_unsigned_decimal(version)
        if numeric_version is None or _decimal_less_than(numeric_version, "5"):
            return None
        return "legacy" if _decimal_less_than(numeric_version, "7") else "current"
    if type(version) is not str or (match := _MANIFEST_VERSION.fullmatch(version)) is None:
        return None
    major, minor, _patch = (_normalize_decimal(part) for part in match.groups())
    if major == "1":
        return "current"
    if major != "0" or _decimal_less_than(minor, "5"):
        return None
    return "legacy" if _decimal_less_than(minor, "7") else "current"


def _normalize_unsigned_decimal(value: str) -> str | None:
    if not value or any(character < "0" or character > "9" for character in value):
        return None
    return _normalize_decimal(value)


def _normalize_decimal(value: str) -> str:
    """Canonicalize known ASCII digits without constructing an unbounded integer."""

    return value.lstrip("0") or "0"


def _decimal_less_than(left: str, right: str) -> bool:
    return (len(left), left) < (len(right), right)


def _reject_json_constant(constant: str) -> None:
    raise ValueError(f"Lake's JSON parser rejects {constant}")


def _is_stale(requirement: dict, locked: MathlibLock) -> bool:
    """Whether lakefile.toml asks for a different Mathlib source than the lock records."""

    kind, git, revision = _requirement_source(requirement)
    if kind is None:
        return False
    if (kind == "path") != (locked.type == "path"):
        return True
    return locked.type == "git" and (
        revision != locked.input_rev
        or (git is not None and canonical_git_url(_redact(git)) != canonical_git_url(locked.url))
    )


def _requirement_source(requirement: dict) -> tuple[str | None, str | None, str | None]:
    """Return the source fields selected by Lake's precedence rules."""

    if "path" in requirement:
        return "path", None, None
    if "git" in requirement:
        git = requirement["git"]
        url = git if isinstance(git, str) else git["url"]
        revision = requirement.get("rev")
        return "git", url, revision if isinstance(revision, str) else None
    source = requirement.get("source")
    if isinstance(source, dict):
        if source.get("type") == "path":
            return "path", None, None
        if source.get("type") == "git":
            revision = source.get("rev")
            return "git", source["url"], revision if isinstance(revision, str) else None
    revision = requirement.get("rev")
    return None, None, revision if isinstance(revision, str) else None


def _redact(url: str | None) -> str | None:
    """Hide credentials embedded in a Git URL, since reports end up in logs."""

    return None if url is None else _URL_CREDENTIALS.sub(r"\1***@", url)


def _is_name(value: object) -> bool:
    # Lake's stringToLegalOrSimpleName accepts even the empty string by
    # falling back to a simple escaped Name.
    return type(value) is str


def _are_named_tables(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(entry, dict) and _is_name(entry.get("name")) for entry in value)


def _canonical_manifest_name(value: str) -> tuple[tuple[str, str], ...] | None:
    """Return the structural Name produced by JSON's strict ``String.toName``."""

    if value == "[anonymous]":
        return ()
    parts = _split_lean_name(value)
    if parts is None:
        return None
    return tuple((kind, _normalize_decimal(text) if kind == "num" else text) for kind, text in parts)


def _canonical_toml_name(value: str) -> tuple[tuple[str, str], ...]:
    """Return Lake's Name, including TOML's simple-name fallback."""

    parts = _split_lean_name(value)
    if parts is None:
        return (("str", value),)
    return tuple((kind, _normalize_decimal(text) if kind == "num" else text) for kind, text in parts)


def _split_lean_name(value: str) -> list[tuple[str, str]] | None:
    parts: list[tuple[str, str]] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == _LEAN_ID_BEGIN_ESCAPE:
            end = value.find(_LEAN_ID_END_ESCAPE, index + 1)
            if end < 0:
                return None
            parts.append(("str", value[index + 1 : end]))
            index = end + 1
        elif _lean_is_id_first(character):
            start = index
            index += 1
            while index < len(value) and _lean_is_id_rest(value[index]):
                index += 1
            parts.append(("str", value[start:index]))
        elif "0" <= character <= "9":
            start = index
            while index < len(value) and "0" <= value[index] <= "9":
                index += 1
            parts.append(("num", value[start:index]))
        else:
            return None
        if index == len(value):
            return parts
        if value[index] != ".":
            return None
        index += 1
    return None


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
