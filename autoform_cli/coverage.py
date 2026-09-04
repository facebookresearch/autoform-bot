"""Parse the machine-checkable source coverage contract.

The contract is one Markdown table in ``coverage/README.md``. Because it is the
only place a project states what its roadmap is supposed to cover, every rule
here is written to fail closed: anything that does not visibly render, and any
evidence that says nothing, is rejected rather than quietly accepted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit

from .graph import GraphValidationError, SOURCE_UNIT_PATTERN, load_graph

from .markdown import (
    INLINE_CODE,
    Content,
    PublishedTable,
    content,
    link_targets,
    local_target_issue,
    published_tables,
    rendered_visible_text,
)

COVERAGE_SCHEMA = "autoform-coverage/v1"
COVERAGE_V2_SCHEMA = "autoform-coverage/v2"
COVERAGE_DISPOSITIONS = ("MAPPED", "DECOMPOSED", "DEFERRED", "OUT")
_EXPECTED_HEADER = ("Area", "Coverage", "Evidence")
_V2_EXPECTED_HEADER = (
    "Unit",
    "Area",
    "Lines",
    "Locator",
    "Unit SHA-256",
    "Coverage",
    "Evidence",
)
_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LINE_SPAN = re.compile(r"([1-9][0-9]*)-([1-9][0-9]*)\Z")
_FRONTMATTER_LIKE_FENCE = re.compile(
    r"(?:--(?:[ \t]*(?:ya?ml|#.*))?|-{3,}.*)\Z",
    re.IGNORECASE,
)
_FRONTMATTER_KEY = re.compile(r"^[ \t]*[\"']?(?P<key>[A-Za-z][A-Za-z0-9_-]*)[\"']?")
_YAML_HEX_ESCAPE = re.compile(
    r"\\U(?P<long>[0-9A-Fa-f]{8})|"
    r"\\u(?P<short>[0-9A-Fa-f]{4})|"
    r"\\x(?P<byte>[0-9A-Fa-f]{2})"
)

#: Stem of the marker that stands in for a row's cells when tracing which
#: published table those source lines became. Grown by `_unique_marker` until
#: neither the source nor the rendered page can produce it, so provenance never
#: rests on a coincidence.
_ROW_MARKER = "autoformcoveragerowmarker"
#: Words that name the absence of a decision.
_PLACEHOLDER_EVIDENCE = frozenset({"pending", "placeholder", "todo", "tbd", "unknown"})
#: Punctuation that turns a leading placeholder into a marker, as in ``TODO:``.
_MARKER_PUNCTUATION = re.compile(r"^[\s]*[:\-\u2013\u2014]")


@dataclass(frozen=True, order=True, slots=True)
class CoverageIssue:
    """One structural problem in a coverage contract."""

    line: int
    reason: str
    code: str = "invalid-coverage-contract"


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """One source area and its explicit roadmap disposition."""

    area: str
    disposition: str
    evidence: str
    line: int

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CoverageUnit:
    """One exact, LF-terminated span in a v2 source artifact."""

    unit: str
    area: str
    start_line: int
    end_line: int
    locator: str
    unit_sha256: str
    disposition: str
    evidence: str
    line: int
    roadmap_nodes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "area": self.area,
            "coverage": self.disposition,
            "end_line": self.end_line,
            "evidence": self.evidence,
            "line": self.line,
            "locator": self.locator,
            "roadmap_nodes": list(self.roadmap_nodes),
            "start_line": self.start_line,
            "unit": self.unit,
            "unit_sha256": self.unit_sha256,
        }


@dataclass(frozen=True, order=True, slots=True)
class CoverageNodeBinding:
    """A reciprocal source-unit to roadmap-leaf binding."""

    node_id: str
    unit: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Canonical coverage rows, counts, and source binding.

    The summary describes what the author *declared*. It is deliberately not a
    measurement of the source tree: nothing here reads the Lean project or
    counts proved declarations.
    """

    schema: str
    source_path: str
    source_sha256: str
    entries: tuple[CoverageEntry, ...]
    artifact_path: str | None = None
    artifact_sha256: str | None = None
    units: tuple[CoverageUnit, ...] = ()
    node_bindings: tuple[CoverageNodeBinding, ...] = ()
    _roadmap_sha256: str | None = field(default=None, repr=False, compare=False)

    @property
    def counts(self) -> dict[str, int]:
        totals = Counter(entry.disposition for entry in self.entries)
        return {disposition: totals[disposition] for disposition in COVERAGE_DISPOSITIONS}

    @property
    def complete(self) -> bool:
        """Whether every author-declared row reached a terminal disposition.

        Terminal means the row is no longer ``MAPPED`` -- the author has either
        decomposed it into roadmap articles, deferred it to a named milestone,
        or excluded it with a reason.

        This is a claim about the *contract*, not about the project. It does not
        establish that the declared rows cover the source exhaustively, and it
        says nothing about whether the linked roadmap articles are formalized or
        proved. A project that declares one narrow area and disposes of it
        reports ``complete`` while most of its source remains undeclared.
        """

        return bool(self.entries) and not self.counts["MAPPED"]

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "complete": self.complete,
            "counts": self.counts,
            "entries": [entry.as_dict() for entry in self.entries],
            "schema": self.schema,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
        }
        if self.schema == COVERAGE_V2_SCHEMA:
            result.update(
                {
                    "artifact_path": self.artifact_path,
                    "artifact_sha256": self.artifact_sha256,
                    "contract_sha256": self.source_sha256,
                    "node_bindings": [binding.as_dict() for binding in self.node_bindings],
                    "units": [unit.as_dict() for unit in self.units],
                }
            )
        return result

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def load_coverage(blueprint_dir: str | Path) -> tuple[CoverageSummary | None, tuple[CoverageIssue, ...]]:
    """Read and validate ``coverage/README.md`` without modifying it."""

    blueprint = Path(blueprint_dir).expanduser().resolve()
    path = blueprint / "coverage" / "README.md"
    try:
        content = path.read_bytes()
        text = content.decode("utf-8")
    except FileNotFoundError:
        return None, (
            CoverageIssue(0, "coverage contract is missing", "missing-coverage-contract"),
        )
    except UnicodeError:
        return None, (CoverageIssue(0, "coverage contract cannot be read as UTF-8"),)
    except OSError:
        return None, (CoverageIssue(0, "coverage contract cannot be read"),)

    schema_values, frontmatter, frontmatter_end, frontmatter_issues = _coverage_frontmatter(text)
    ambiguous_schema_line = _ambiguous_v2_schema_line(text)
    if frontmatter_issues and (schema_values or ambiguous_schema_line is not None):
        return None, tuple(frontmatter_issues)
    if schema_values:
        if len(schema_values) != 1:
            return None, (
                CoverageIssue(
                    schema_values[1][0],
                    "coverage contract declares more than one schema",
                    "coverage-schema-mixed",
                ),
            )
        schema_line, schema = schema_values[0]
        if schema != COVERAGE_V2_SCHEMA:
            return None, (
                CoverageIssue(
                    schema_line,
                    f"unsupported coverage schema {schema!r}",
                    "coverage-schema-unknown",
                ),
            )
        return _load_coverage_v2(
            blueprint,
            path,
            content,
            text,
            frontmatter,
            frontmatter_end,
        )

    if ambiguous_schema_line is not None:
        return None, (
            CoverageIssue(
                ambiguous_schema_line,
                "autoform-coverage/v2 appears in malformed or unsupported coverage frontmatter",
                "coverage-schema-ambiguous",
            ),
        )

    published_headers = {table.headers for table in published_tables(text)}
    if _V2_EXPECTED_HEADER in published_headers:
        code = (
            "coverage-schema-mixed"
            if _EXPECTED_HEADER in published_headers
            else "coverage-v2-schema-required"
        )
        return None, (
            CoverageIssue(
                0,
                "a rendered v2 coverage table requires exact 'schema: autoform-coverage/v2' frontmatter",
                code,
            ),
        )

    rows, issues = _parse_table(text)
    issues.extend(_validate_evidence(rows, blueprint=blueprint, coverage_path=path))
    if issues:
        return None, tuple(issues)
    return (
        CoverageSummary(
            schema=COVERAGE_SCHEMA,
            source_path="coverage/README.md",
            source_sha256=hashlib.sha256(content).hexdigest(),
            entries=tuple(rows),
        ),
        (),
    )


def _coverage_frontmatter(
    text: str,
) -> tuple[list[tuple[int, str]], dict[str, tuple[int, str]], int, list[CoverageIssue]]:
    """Read the intentionally small coverage frontmatter language.

    V1 remains schema-less. The presence of any ``schema`` declaration opts
    into strict schema selection, so a typo or two competing declarations can
    never be interpreted as the legacy contract.
    """

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return [], {}, 0, []
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return [], {}, len(lines), [
            CoverageIssue(1, "coverage frontmatter is unterminated", "coverage-frontmatter-invalid")
        ]

    schemas: list[tuple[int, str]] = []
    values: dict[str, tuple[int, str]] = {}
    issues: list[CoverageIssue] = []
    for line_number, raw in enumerate(lines[1:end], start=2):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            issues.append(
                CoverageIssue(
                    line_number,
                    "expected 'key: value' in coverage frontmatter",
                    "coverage-frontmatter-invalid",
                )
            )
            continue
        key, value = (part.strip() for part in stripped.split(":", 1))
        value = _unquote_frontmatter_scalar(value)
        if key == "schema":
            schemas.append((line_number, value))
            if key in values:
                continue
        if key in values:
            issues.append(
                CoverageIssue(
                    line_number,
                    f"duplicate coverage frontmatter key {key!r}",
                    "coverage-frontmatter-duplicate-key",
                )
            )
            continue
        values[key] = (line_number, value)
    return schemas, values, end + 1, issues


def _ambiguous_v2_schema_line(text: str) -> int | None:
    """Find v2 intent inside a frontmatter block we could not select.

    Exact frontmatter is strict about the selector's spelling, quoting, and
    separator. Detection is deliberately broader only inside that block: any
    un-commented v2 schema token means a malformed selector must not downgrade
    to permissive legacy v1. A near frontmatter fence at the start gets the same
    treatment, while prose and fenced examples in the Markdown body do not.
    """

    lines = text.splitlines()
    if not lines:
        return None
    opening = lines[0].lstrip("\ufeff").strip()
    if _FRONTMATTER_LIKE_FENCE.fullmatch(opening) is None:
        return None
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        end = len(lines)
    for line_number, line in enumerate(lines[1:end], start=2):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if _frontmatter_line_signals_v2(stripped):
            return line_number
    return None


def _frontmatter_line_signals_v2(line: str) -> bool:
    def decode_hex_escape(match: re.Match[str]) -> str:
        value = match.group("short") or match.group("long") or match.group("byte")
        assert value is not None
        try:
            return chr(int(value, 16))
        except ValueError:
            return match.group(0)

    folded = _YAML_HEX_ESCAPE.sub(decode_hex_escape, line).casefold()
    if COVERAGE_V2_SCHEMA.casefold() in folded:
        return True
    normalized = re.sub(r"[\s\"'\\]", "", folded).replace("_", "-")
    if COVERAGE_V2_SCHEMA.casefold() in normalized:
        return True
    key_match = _FRONTMATTER_KEY.match(folded)
    return (
        key_match is not None
        and _within_one_schema_edit(key_match.group("key").casefold())
        and "autoform-coverage/" in normalized
    )


def _within_one_schema_edit(value: str) -> bool:
    expected = "schema"
    if value == expected:
        return True
    if abs(len(value) - len(expected)) > 1:
        return False
    if len(value) == len(expected):
        differences = [
            index
            for index, pair in enumerate(zip(value, expected))
            if pair[0] != pair[1]
        ]
        if len(differences) == 1:
            return True
        return (
            len(differences) == 2
            and differences[1] == differences[0] + 1
            and value[differences[0]] == expected[differences[1]]
            and value[differences[1]] == expected[differences[0]]
        )
    shorter, longer = (value, expected) if len(value) < len(expected) else (expected, value)
    mismatch = next(
        (index for index, pair in enumerate(zip(shorter, longer)) if pair[0] != pair[1]),
        len(shorter),
    )
    return shorter[mismatch:] == longer[mismatch + 1 :]


def _unquote_frontmatter_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _load_coverage_v2(
    blueprint: Path,
    path: Path,
    contract_bytes: bytes,
    text: str,
    frontmatter: dict[str, tuple[int, str]],
    frontmatter_end: int,
) -> tuple[CoverageSummary | None, tuple[CoverageIssue, ...]]:
    issues: list[CoverageIssue] = []
    allowed = {"schema", "artifact", "artifact_sha256"}
    for key, (line, _) in frontmatter.items():
        if key not in allowed:
            issues.append(
                CoverageIssue(
                    line,
                    f"unsupported v2 coverage frontmatter key {key!r}",
                    "coverage-frontmatter-unknown-key",
                )
            )
    for key in ("artifact", "artifact_sha256"):
        if key not in frontmatter:
            issues.append(
                CoverageIssue(
                    1,
                    f"v2 coverage frontmatter is missing {key!r}",
                    f"coverage-{key.replace('_', '-')}-missing",
                )
            )
    if issues:
        return None, tuple(issues)

    artifact_line, artifact_value = frontmatter["artifact"]
    hash_line, declared_artifact_hash = frontmatter["artifact_sha256"]
    artifact_relative, artifact_issue = _artifact_relative_path(artifact_value)
    if artifact_issue is not None:
        return None, (CoverageIssue(artifact_line, artifact_issue, "coverage-artifact-path-invalid"),)
    if _SHA256.fullmatch(declared_artifact_hash) is None:
        return None, (
            CoverageIssue(
                hash_line,
                "artifact_sha256 must be exactly 64 lowercase hexadecimal characters",
                "coverage-artifact-hash-invalid",
            ),
        )
    assert artifact_relative is not None
    artifact_path = blueprint.joinpath(*artifact_relative.parts)
    artifact_bytes, read_issue = _read_source_artifact(blueprint, artifact_path)
    if read_issue is not None:
        return None, (CoverageIssue(artifact_line, read_issue[1], read_issue[0]),)
    assert artifact_bytes is not None
    format_issue = _canonical_artifact_issue(artifact_bytes)
    if format_issue is not None:
        return None, (CoverageIssue(artifact_line, format_issue[1], format_issue[0]),)
    actual_artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    if actual_artifact_hash != declared_artifact_hash:
        return None, (
            CoverageIssue(
                hash_line,
                "artifact_sha256 does not match the named source artifact",
                "coverage-artifact-hash-stale",
            ),
        )

    units, table_issues = _parse_v2_table(text, frontmatter_end)
    issues.extend(table_issues)
    if not table_issues:
        issues.extend(_validate_unit_partition(units, artifact_bytes))
    bindings: tuple[CoverageNodeBinding, ...] = ()
    roadmap_sha256: str | None = None
    if not issues:
        units, bindings, roadmap_sha256, binding_issues = _validate_v2_bindings(
            units,
            blueprint=blueprint,
            coverage_path=path,
        )
        issues.extend(binding_issues)
    if issues:
        return None, tuple(issues)

    entries = tuple(
        CoverageEntry(unit.area, unit.disposition, unit.evidence, unit.line) for unit in units
    )
    return (
        CoverageSummary(
            schema=COVERAGE_V2_SCHEMA,
            source_path="coverage/README.md",
            source_sha256=hashlib.sha256(contract_bytes).hexdigest(),
            entries=entries,
            artifact_path=artifact_relative.as_posix(),
            artifact_sha256=actual_artifact_hash,
            units=tuple(units),
            node_bindings=bindings,
            _roadmap_sha256=roadmap_sha256,
        ),
        (),
    )


def _artifact_relative_path(value: str) -> tuple[PurePosixPath | None, str | None]:
    windows = PureWindowsPath(value)
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or windows.is_absolute()
        or path.parts[:1] != ("sources",)
        or len(path.parts) < 2
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return None, "artifact must be a portable relative file below sources/"
    return path, None


def _read_source_artifact(
    blueprint: Path, artifact: Path
) -> tuple[bytes | None, tuple[str, str] | None]:
    """Read one regular artifact without following a symlink in its path."""

    try:
        relative = artifact.relative_to(blueprint)
        current = blueprint
        identities: list[tuple[Path, tuple[int, int]]] = []
        for part in relative.parts[:-1]:
            current /= part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                return None, (
                    "coverage-artifact-symlink",
                    "source artifact path contains a symbolic link or non-directory component",
                )
            identities.append((current, (metadata.st_dev, metadata.st_ino)))
        final_metadata = artifact.lstat()
        if stat.S_ISLNK(final_metadata.st_mode):
            return None, (
                "coverage-artifact-symlink",
                "source artifact is a symbolic link",
            )
        if not stat.S_ISREG(final_metadata.st_mode):
            return None, (
                "coverage-artifact-not-regular",
                "source artifact is not a regular file",
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(artifact, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                return None, (
                    "coverage-artifact-not-regular",
                    "source artifact is not a regular file",
                )
            stream = os.fdopen(descriptor, "rb", buffering=0, closefd=False)
            try:
                artifact_bytes = stream.read()
            finally:
                stream.close()
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current_metadata = artifact.lstat()
        if stat.S_ISLNK(current_metadata.st_mode) or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or (after.st_dev, after.st_ino) != (
            current_metadata.st_dev,
            current_metadata.st_ino,
        ):
            return None, (
                "coverage-artifact-changed",
                "source artifact changed while it was read",
            )
        for parent, identity in identities:
            metadata = parent.lstat()
            if stat.S_ISLNK(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != identity:
                return None, (
                    "coverage-artifact-changed",
                    "source artifact path changed while it was read",
                )
        return artifact_bytes, None
    except FileNotFoundError:
        return None, ("coverage-artifact-missing", "source artifact does not exist")
    except OSError:
        return None, ("coverage-artifact-unreadable", "source artifact cannot be read safely")


def _canonical_artifact_issue(data: bytes) -> tuple[str, str] | None:
    if not data:
        return "coverage-artifact-empty", "source artifact is empty"
    if data.startswith(b"\xef\xbb\xbf"):
        return "coverage-artifact-bom", "source artifact must not contain a UTF-8 BOM"
    if b"\x00" in data:
        return "coverage-artifact-nul", "source artifact contains a NUL byte"
    if b"\r" in data:
        return "coverage-artifact-cr", "source artifact must use LF line endings"
    if not data.endswith(b"\n"):
        return "coverage-artifact-final-lf", "source artifact must end with LF"
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        return "coverage-artifact-utf8", "source artifact is not canonical UTF-8"
    if decoded.encode("utf-8") != data:
        return "coverage-artifact-utf8", "source artifact is not canonical UTF-8"
    return None


def _parse_v2_table(text: str, frontmatter_end: int) -> tuple[list[CoverageUnit], list[CoverageIssue]]:
    view = content(text)
    lines = view.lines
    source_lines = text.splitlines()
    header_indexes: list[int] = []
    for index in range(frontmatter_end, len(lines) - 1):
        if view.is_hidden(index) or view.is_hidden(index + 1):
            continue
        if _cells(lines[index]) != _V2_EXPECTED_HEADER:
            continue
        separator = _cells(lines[index + 1])
        if len(separator) == len(_V2_EXPECTED_HEADER) and all(
            _SEPARATOR.fullmatch(cell) for cell in separator
        ):
            header_indexes.append(index)
    page_tables = published_tables(text)
    if any(table.headers == _EXPECTED_HEADER for table in page_tables):
        return [], [
            CoverageIssue(
                0,
                "coverage contract mixes v1 and v2 rendered tables",
                "coverage-schema-mixed",
            )
        ]
    contract_tables = [table for table in page_tables if table.headers == _V2_EXPECTED_HEADER]
    if not header_indexes:
        return [], [
            CoverageIssue(
                0,
                "v2 coverage contract has no 'Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence' table",
                "coverage-table-missing",
            )
        ]
    if len(header_indexes) == 1 and not contract_tables:
        return [], [
            CoverageIssue(header_indexes[0] + 1, "v2 coverage table does not render as a table", "coverage-table-unrendered")
        ]
    if len(header_indexes) != 1 or len(contract_tables) != 1:
        line = header_indexes[1] + 1 if len(header_indexes) > 1 else header_indexes[0] + 1
        return [], [
            CoverageIssue(line, "v2 coverage contract must have exactly one coverage table", "coverage-table-ambiguous")
        ]
    header_index = header_indexes[0]
    units: list[CoverageUnit] = []
    issues: list[CoverageIssue] = []
    seen: dict[str, int] = {}
    parsed_rows: list[tuple[str, ...]] = []
    row_indexes: list[int] = []
    for index in range(header_index + 2, len(lines)):
        raw = lines[index]
        if view.ends_block(index) or not raw.strip():
            break
        cells = _cells(raw)
        line_number = index + 1
        if len(_cells(source_lines[index])) != len(cells):
            issues.append(
                CoverageIssue(
                    line_number,
                    "an HTML comment changes this v2 coverage row's column layout",
                    "coverage-row-hidden-layout",
                )
            )
            continue
        if len(cells) != len(_V2_EXPECTED_HEADER):
            issues.append(
                CoverageIssue(line_number, "v2 coverage row must have exactly seven columns", "coverage-row-columns")
            )
            continue
        parsed_rows.append(cells)
        row_indexes.append(index)
        unit, area, span_text, locator, unit_hash, disposition_text, evidence = cells
        disposition = _inline_code(disposition_text).upper()
        span_match = _LINE_SPAN.fullmatch(_inline_code(span_text))
        valid = True
        if SOURCE_UNIT_PATTERN.fullmatch(unit) is None:
            issues.append(CoverageIssue(line_number, f"invalid source unit id {unit!r}", "coverage-unit-id-invalid"))
            valid = False
        elif unit in seen:
            issues.append(
                CoverageIssue(
                    line_number,
                    f"duplicate source unit id {unit!r}; first declared at line {seen[unit]}",
                    "coverage-unit-id-duplicate",
                )
            )
            valid = False
        else:
            seen[unit] = line_number
        for label, value in (("area", area), ("locator", locator), ("evidence", evidence)):
            visible = _visible_markdown(value)
            if not _has_substance(visible):
                issues.append(
                    CoverageIssue(
                        line_number,
                        f"coverage {label} has no substantive visible text",
                        f"coverage-{label}-empty",
                    )
                )
                valid = False
            elif label == "evidence" and _is_placeholder(visible):
                issues.append(CoverageIssue(line_number, "coverage evidence is a placeholder", "coverage-evidence-placeholder"))
                valid = False
        if span_match is None:
            issues.append(
                CoverageIssue(
                    line_number,
                    "source line span must use inclusive one-based START-END syntax",
                    "coverage-unit-span-invalid",
                )
            )
            valid = False
            start_line = end_line = 0
        else:
            start_line, end_line = (int(value) for value in span_match.groups())
            if end_line < start_line:
                issues.append(CoverageIssue(line_number, "source line span ends before it starts", "coverage-unit-span-invalid"))
                valid = False
        if _SHA256.fullmatch(unit_hash) is None:
            issues.append(
                CoverageIssue(
                    line_number,
                    "unit SHA-256 must be exactly 64 lowercase hexadecimal characters",
                    "coverage-unit-hash-invalid",
                )
            )
            valid = False
        if disposition not in COVERAGE_DISPOSITIONS:
            issues.append(
                CoverageIssue(
                    line_number,
                    f"unknown coverage disposition {disposition_text!r}",
                    "coverage-disposition-invalid",
                )
            )
            valid = False
        if valid:
            units.append(
                CoverageUnit(
                    unit,
                    area,
                    start_line,
                    end_line,
                    locator,
                    unit_hash,
                    disposition,
                    evidence,
                    line_number,
                )
            )
    if not units and not issues:
        issues.append(CoverageIssue(header_index + 1, "v2 coverage table has no rows", "coverage-table-empty"))
    if not issues:
        issues.extend(
            _correlation_issues(
                source_lines,
                contract_tables[0],
                page_tables,
                row_indexes,
                parsed_rows,
                header_index,
            )
        )
    return units, issues


def _validate_unit_partition(units: list[CoverageUnit], artifact: bytes) -> list[CoverageIssue]:
    issues: list[CoverageIssue] = []
    source_lines = artifact.splitlines(keepends=True)
    expected_start = 1
    for unit in units:
        if unit.start_line > expected_start:
            issues.append(
                CoverageIssue(
                    unit.line,
                    f"source coverage has a gap before line {unit.start_line}",
                    "coverage-unit-gap",
                )
            )
        elif unit.start_line < expected_start:
            issues.append(
                CoverageIssue(
                    unit.line,
                    f"source coverage overlaps or is out of order at line {unit.start_line}",
                    "coverage-unit-overlap",
                )
            )
        if unit.end_line > len(source_lines):
            issues.append(
                CoverageIssue(
                    unit.line,
                    f"source line span exceeds the artifact's {len(source_lines)} lines",
                    "coverage-unit-bounds",
                )
            )
        elif unit.start_line <= unit.end_line:
            actual = hashlib.sha256(
                b"".join(source_lines[unit.start_line - 1 : unit.end_line])
            ).hexdigest()
            if actual != unit.unit_sha256:
                issues.append(
                    CoverageIssue(
                        unit.line,
                        f"unit SHA-256 is stale for {unit.unit!r}",
                        "coverage-unit-hash-stale",
                    )
                )
        expected_start = max(expected_start, unit.end_line + 1)
    if units and expected_start <= len(source_lines):
        issues.append(
            CoverageIssue(
                units[-1].line,
                f"source coverage stops at line {expected_start - 1} of {len(source_lines)}",
                "coverage-unit-gap",
            )
        )
    return issues


def _validate_v2_bindings(
    units: list[CoverageUnit],
    *,
    blueprint: Path,
    coverage_path: Path,
) -> tuple[
    list[CoverageUnit],
    tuple[CoverageNodeBinding, ...],
    str | None,
    list[CoverageIssue],
]:
    issues: list[CoverageIssue] = []
    try:
        graph = load_graph(blueprint)
    except GraphValidationError as error:
        return (
            units,
            (),
            None,
            [
                CoverageIssue(
                    0,
                    f"roadmap cannot be validated for source bindings: {reason}",
                    "coverage-roadmap-invalid",
                )
                for reason in error.issues
            ],
        )
    roadmap_sha256 = _roadmap_source_provenance(
        (
            node.path.resolve().relative_to(graph.blueprint_dir.resolve()).as_posix(),
            node.source_sha256 or "",
        )
        for node in graph.nodes.values()
    )
    by_path = {node.path.resolve(): node for node in graph.nodes.values()}
    units_by_id = {unit.unit: unit for unit in units}
    expected: set[tuple[str, str]] = set()
    updated: list[CoverageUnit] = []
    for unit in units:
        node_ids: list[str] = []
        targets = link_targets(unit.evidence) if unit.disposition == "DECOMPOSED" else ()
        if unit.disposition == "DECOMPOSED" and not targets:
            issues.append(
                CoverageIssue(
                    unit.line,
                    "DECOMPOSED coverage evidence must link to at least one roadmap leaf",
                    "coverage-decomposed-target-missing",
                )
            )
        seen_targets: set[str] = set()
        for target in targets:
            split = urlsplit(target)
            raw_path = unquote(split.path)
            if split.scheme or split.netloc or not raw_path:
                issues.append(
                    CoverageIssue(
                        unit.line,
                        f"DECOMPOSED evidence target is not a local roadmap article: {target!r}",
                        "coverage-decomposed-target-outside-roadmap",
                    )
                )
                continue
            problem = local_target_issue(coverage_path, target, blueprint, label="coverage")
            if problem is not None:
                issues.append(CoverageIssue(unit.line, problem[1], "coverage-decomposed-target-invalid"))
                continue
            candidate = (coverage_path.parent / raw_path).resolve()
            node = by_path.get(candidate)
            if node is None:
                issues.append(
                    CoverageIssue(
                        unit.line,
                        f"DECOMPOSED evidence target is outside blueprint/roadmap: {target!r}",
                        "coverage-decomposed-target-outside-roadmap",
                    )
                )
                continue
            if node.id in seen_targets:
                issues.append(
                    CoverageIssue(
                        unit.line,
                        f"duplicate source-unit mapping to roadmap node {node.id!r}",
                        "coverage-node-binding-duplicate",
                    )
                )
                continue
            seen_targets.add(node.id)
            if not node.formalizable or graph.children(node.id):
                issues.append(
                    CoverageIssue(
                        unit.line,
                        f"DECOMPOSED evidence target {node.id!r} is not a formalizable roadmap leaf",
                        "coverage-decomposed-target-not-leaf",
                    )
                )
                continue
            node_ids.append(node.id)
            expected.add((unit.unit, node.id))
        updated.append(
            CoverageUnit(
                unit.unit,
                unit.area,
                unit.start_line,
                unit.end_line,
                unit.locator,
                unit.unit_sha256,
                unit.disposition,
                unit.evidence,
                unit.line,
                tuple(sorted(node_ids)),
            )
        )

    authored: set[tuple[str, str]] = set()
    for node in graph.nodes.values():
        for unit_id in node.source_units:
            if unit_id not in units_by_id:
                issues.append(
                    CoverageIssue(
                        0,
                        f"roadmap node {node.id!r} names unknown source unit {unit_id!r}",
                        "coverage-node-binding-unknown-unit",
                    )
                )
                continue
            if not node.formalizable or graph.children(node.id):
                issues.append(
                    CoverageIssue(
                        0,
                        f"roadmap node {node.id!r} binds source units but is not a formalizable leaf",
                        "coverage-node-binding-not-leaf",
                    )
                )
            authored.add((unit_id, node.id))
    for unit_id, node_id in sorted(expected - authored):
        issues.append(
            CoverageIssue(
                units_by_id[unit_id].line,
                f"roadmap node {node_id!r} does not reciprocally list source unit {unit_id!r}",
                "coverage-node-binding-missing-reciprocal",
            )
        )
    for unit_id, node_id in sorted(authored - expected):
        issues.append(
            CoverageIssue(
                0,
                f"roadmap node {node_id!r} lists source unit {unit_id!r} without reciprocal DECOMPOSED evidence",
                "coverage-node-binding-one-way",
            )
        )
    bindings = tuple(
        CoverageNodeBinding(node_id=node_id, unit=unit_id)
        for unit_id, node_id in sorted(expected & authored)
    )
    return updated, bindings, roadmap_sha256, issues


def _roadmap_source_provenance(sources: Iterable[tuple[str, str]]) -> str:
    """Identify exact roadmap bytes without retaining another source copy."""

    digest = hashlib.sha256(b"autoform-roadmap-provenance/v1\0")
    for relative, source_sha256 in sorted(sources):
        encoded_path = os.fsencode(relative)
        encoded_sha256 = source_sha256.encode("ascii")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(encoded_sha256)
    return digest.hexdigest()


def _parse_table(text: str) -> tuple[list[CoverageEntry], list[CoverageIssue]]:
    # Only published Markdown can carry the contract. Commented-out and
    # code-block tables are masked to blank lines first, which keeps every
    # surviving index aligned with the author's own line numbering.
    view = content(text)
    lines = view.lines
    source_lines = text.splitlines()
    header_indexes: list[int] = []
    for index in range(len(lines) - 1):
        if view.is_hidden(index) or view.is_hidden(index + 1):
            # The table is commented out or fenced, so it publishes nothing.
            continue
        if _cells(lines[index]) != _EXPECTED_HEADER:
            continue
        separator = _cells(lines[index + 1])
        if len(separator) != 3 or not all(_SEPARATOR.fullmatch(cell) for cell in separator):
            continue
        header_indexes.append(index)

    # Masking shows what the cells *say*; only the renderer knows whether any of
    # it publishes. Whether a table renders depends on its surroundings as much
    # as on its own two structural lines -- a comment can break the delimiter row
    # while preserving its column count, and a paragraph directly above the
    # header turns the whole thing into one lazy paragraph.
    page_tables = published_tables(text)
    contract_tables = [table for table in page_tables if table.headers == _EXPECTED_HEADER]
    if header_indexes and not contract_tables:
        return [], [
            CoverageIssue(
                header_indexes[0] + 1,
                "coverage table does not render as a table; check for an HTML comment in "
                "the header or separator, and for a missing blank line above it",
            )
        ]
    if not header_indexes and contract_tables:
        # The page shows a contract table we did not recognise, which means it is
        # not in the top-level form the audit reads. Name a quoted table directly;
        # otherwise retain the canonical outer-pipe guidance.
        blockquote_index = _blockquoted_header_index(lines, view)
        if blockquote_index is not None:
            return [], [
                CoverageIssue(
                    blockquote_index + 1,
                    "coverage table must be a top-level table; remove the blockquote markers",
                )
            ]
        return [], [
            CoverageIssue(
                0,
                "coverage table must be written with a leading and trailing pipe on every row",
            )
        ]

    if not header_indexes:
        return [], [CoverageIssue(0, "coverage contract has no 'Area | Coverage | Evidence' table")]
    if len(header_indexes) > 1:
        return [], [CoverageIssue(header_indexes[1] + 1, "coverage contract has multiple coverage tables")]
    if len(contract_tables) > 1:
        # One source table but several on the page: which one carries the
        # contract is ambiguous, and the reader cannot tell either.
        return [], [
            CoverageIssue(header_indexes[0] + 1, "coverage contract has multiple coverage tables")
        ]

    header_index = header_indexes[0]
    entries: list[CoverageEntry] = []
    issues: list[CoverageIssue] = []
    seen_areas: dict[str, int] = {}
    parsed_rows: list[tuple[str, ...]] = []
    row_indexes: list[int] = []
    for index in range(header_index + 2, len(lines)):
        raw = lines[index]
        if view.ends_block(index):
            # A blank line the author typed ends the table. Hidden content also
            # ends it, for every renderer as well as for us, so any row written
            # below it is published by nobody and must be reported.
            break
        if not raw.strip():
            issues.extend(_unpublished_row_issues(view, index))
            break
        cells = _cells(raw)
        line_number = index + 1
        # A renderer splits cells before it strips comments, so a comment that
        # contains a pipe changes the column layout a reader sees. Reject the
        # disagreement rather than parse a different table than the one shown.
        if len(_cells(source_lines[index])) != len(cells):
            issues.append(
                CoverageIssue(
                    line_number,
                    "an HTML comment changes this coverage row's column layout",
                )
            )
            continue
        if len(cells) != 3:
            issues.append(CoverageIssue(line_number, "coverage row must have exactly three columns"))
            continue
        area, disposition_text, evidence = cells
        parsed_rows.append(cells)
        row_indexes.append(index)
        disposition = _inline_code(disposition_text).upper()
        if not area:
            issues.append(CoverageIssue(line_number, "coverage area is empty"))
        if disposition not in COVERAGE_DISPOSITIONS:
            allowed = ", ".join(COVERAGE_DISPOSITIONS)
            issues.append(
                CoverageIssue(line_number, f"unknown coverage disposition {disposition_text!r}; expected {allowed}")
            )
        if not evidence:
            issues.append(CoverageIssue(line_number, "coverage evidence is empty"))
        normalized_area = area.casefold()
        if normalized_area in seen_areas:
            issues.append(
                CoverageIssue(
                    line_number,
                    f"duplicate coverage area {area!r}; first declared at line {seen_areas[normalized_area]}",
                )
            )
        else:
            seen_areas[normalized_area] = line_number
        if area and disposition in COVERAGE_DISPOSITIONS and evidence:
            entries.append(CoverageEntry(area, disposition, evidence, line_number))

    if not entries and not issues:
        issues.append(CoverageIssue(header_index + 1, "coverage table has no rows"))
    if not issues:
        issues.extend(
            _correlation_issues(
                source_lines,
                contract_tables[0],
                page_tables,
                row_indexes,
                parsed_rows,
                header_index,
            )
        )
    return entries, issues


def _correlation_issues(
    source_lines: list[str],
    published: PublishedTable,
    page_tables: list[PublishedTable],
    row_indexes: list[int],
    parsed_rows: list[tuple[str, ...]],
    header_index: int,
) -> list[CoverageIssue]:
    """Check the rows we validated are the rows the page actually shows.

    Equal content is not proof of provenance. A canonical-looking table that
    renders as a paragraph, sitting above an unrelated raw-HTML table with the
    same rows, would satisfy any comparison of values while publishing nothing
    itself. So the source rows are rendered again carrying a marker, and the
    published table has to be the one that marker turns up in.

    Substituting rows is only sound if it changes nothing else, and that is not
    something to assume: an unclosed ``<style>`` inside a row swallows the rest of
    the document, so removing that row *exposes* tables the page never published,
    one of which can then supply the marker. The trace therefore has to leave the
    page's topology intact -- same tables, same order, same headers, identical
    rows everywhere except the one position being traced. Anything else means the
    substitution changed the document rather than only the rows, and the trace
    says nothing about the original.
    """

    marked = list(source_lines)
    marker = _unique_marker("\n".join(source_lines), page_tables)
    markers = []
    for position, index in enumerate(row_indexes):
        token = f"{marker}{position}"
        markers.append(token)
        marked[index] = f"| {' | '.join(token for _ in published.headers)} |"
    untraceable = [
        CoverageIssue(
            header_index + 1,
            "coverage rows are not the rows the page publishes; the table a reader "
            "sees was not produced by these lines",
        )
    ]
    unstable = [
        CoverageIssue(
            header_index + 1,
            "coverage rows cannot be traced to the table the page publishes because a "
            "row here changes what else the page renders; check for an unclosed element "
            "or a table inside a cell",
        )
    ]
    marked_tables = published_tables("\n".join(marked))
    if len(marked_tables) != len(page_tables):
        return unstable
    moved = [
        position
        for position, (before, after) in enumerate(zip(page_tables, marked_tables))
        if before != after
    ]
    if not moved:
        # Substituting the rows changed nothing a reader sees, so these lines
        # published nothing.
        return untraceable
    if len(moved) > 1:
        return unstable
    position = moved[0]
    traced = marked_tables[position]
    if page_tables[position] != published or traced.headers != page_tables[position].headers:
        return unstable
    if [row[0] for row in traced.rows] != markers:
        return untraceable

    shown = [tuple(rendered_visible_text(cell) for cell in row) for row in parsed_rows]
    if published.rows == tuple(shown):
        return []
    if len(published.rows) != len(shown):
        detail = (
            f"the page shows {len(published.rows)} row(s) and the contract declares {len(shown)}"
        )
    else:
        differing = next(
            (
                position
                for position, (page, source) in enumerate(zip(published.rows, shown))
                if page != source
            ),
            0,
        )
        detail = (
            f"row {differing + 1} reads {' | '.join(shown[differing])!r} here "
            f"but {' | '.join(published.rows[differing])!r} on the page"
        )
    return [
        CoverageIssue(
            header_index + 1,
            f"coverage rows do not match the table the page publishes; {detail}",
        )
    ]


def _unique_marker(text: str, page_tables: list[PublishedTable]) -> str:
    """Return a marker neither the source nor the published page can produce.

    A fixed marker is only a convention, and provenance that rests on a
    convention can be arranged around: an author who writes the marker as an area
    lets an unrelated table answer for their own. Checking the source alone is not
    enough either, because rendering *synthesises* text that the source never
    contained literally -- ``&#97;utoformcoveragerowmarker0`` and
    ``autoform<span></span>coveragerowmarker0`` both normalise to the marker. The
    comparison the check performs is against published cell text, so that is what
    the marker has to be absent from. Growing the candidate until it appears in
    neither keeps the check deterministic while making the collision impossible
    rather than unlikely. Testing the stem is enough, since every per-row token
    contains it.
    """

    corpus = "\n".join(
        [text, *(cell for table in page_tables for row in (table.headers, *table.rows) for cell in row)]
    )
    marker = _ROW_MARKER
    while marker in corpus:
        marker += "x"
    return marker


def _blockquoted_header_index(lines: tuple[str, ...], view: Content) -> int | None:
    for index in range(len(lines) - 1):
        if view.is_hidden(index) or view.is_hidden(index + 1):
            continue
        header = re.sub(r"^ {0,3}>[ \t]?", "", lines[index], count=1)
        separator_line = re.sub(r"^ {0,3}>[ \t]?", "", lines[index + 1], count=1)
        if header == lines[index] or separator_line == lines[index + 1]:
            continue
        separator = _cells(separator_line)
        if _cells(header) == _EXPECTED_HEADER and len(separator) == 3 and all(
            _SEPARATOR.fullmatch(cell) for cell in separator
        ):
            return index
    return None


def _looks_like_row(line: str) -> bool:
    """Whether ``line`` is the shape of a table row the renderer would accept.

    Deliberately looser than :func:`_cells`, which demands both outer pipes.
    Python-Markdown accepts ``A | OUT | reason`` and ``| A | OUT | reason`` as
    rows just as readily, so a stranded row written either way has to be
    reported rather than passed over for failing the canonical form.
    """

    bare = INLINE_CODE.sub("", line).strip()
    if not bare:
        return False
    return bare.startswith("|") or bare.count("|") >= 2


def _unpublished_row_issues(view: Content, start: int) -> list[CoverageIssue]:
    """Report rows written after hidden content inside the table body.

    The table has already ended at ``start``. Anything shaped like a table row
    between there and the next blank line the author actually typed looks like a
    declaration they expected to count, so name each one rather than let the
    contract shrink in silence. Row shape is judged loosely, by
    :func:`_looks_like_row`: a row with the wrong column count, or written
    without its outer pipes, is still a row somebody meant to declare.
    Trailing notes with no rows after them are left alone.
    """

    issues: list[CoverageIssue] = []
    for index in range(start, len(view.lines)):
        if view.ends_block(index):
            break
        if _looks_like_row(view.lines[index]):
            issues.append(
                CoverageIssue(
                    index + 1,
                    "coverage row follows hidden content and would not be published; "
                    "move the comment or code block below the table",
                )
            )
    return issues


def _validate_evidence(
    entries: list[CoverageEntry],
    *,
    blueprint: Path,
    coverage_path: Path,
) -> list[CoverageIssue]:
    issues: list[CoverageIssue] = []
    roadmap = (blueprint / "roadmap").resolve()
    for entry in entries:
        visible_evidence = _visible_markdown(entry.evidence)
        if not _has_substance(visible_evidence):
            issues.append(CoverageIssue(entry.line, "coverage evidence has no substantive content"))
            continue
        if _is_placeholder(visible_evidence):
            issues.append(CoverageIssue(entry.line, "coverage evidence is a placeholder"))
            continue
        if entry.disposition != "DECOMPOSED":
            continue

        targets = link_targets(entry.evidence)
        if not targets:
            issues.append(
                CoverageIssue(
                    entry.line,
                    "DECOMPOSED coverage evidence must link to at least one roadmap article",
                )
            )
            continue
        # Every link offered as proof of decomposition must resolve, using the
        # audit's rules so `render` cannot publish evidence the audit rejects.
        # One good link beside a broken one is a broken claim.
        broken: list[str] = []
        for target in targets:
            problem = local_target_issue(coverage_path, target, blueprint, label="coverage")
            if problem is not None:
                broken.append(problem[1])
        if broken:
            issues.extend(CoverageIssue(entry.line, reason) for reason in broken)
            continue
        if not any(_is_roadmap_article(target, coverage_path=coverage_path, roadmap=roadmap) for target in targets):
            issues.append(
                CoverageIssue(
                    entry.line,
                    "DECOMPOSED coverage evidence has no link to an existing roadmap article",
                )
            )
    return issues


def _visible_markdown(value: str) -> str:
    """Return the text of ``value`` a reader would actually see as evidence.

    Inline code is removed outright rather than unwrapped: it is illustration
    rather than justification, so a cell whose only content is a code span
    states no reason at all. Everything else is reduced the way a renderer
    reduces it, which matters because a URL, an HTML tag, and an entity all
    carry word characters while showing the reader nothing. ``[ ](missing.md)``
    publishes an empty link, not evidence.
    """

    return rendered_visible_text(INLINE_CODE.sub("", value))


def _has_substance(visible: str) -> bool:
    """Whether anything a reader could act on survives emphasis and punctuation."""

    return bool(re.search(r"\w", re.sub(r"[*_~\\]", "", visible)))


def _is_placeholder(visible: str) -> bool:
    """Whether the evidence only announces that a decision is still outstanding.

    Two shapes are rejected. A cell whose every word is a placeholder, however
    decorated -- ``TBD``, ``**TODO.**`` -- and a cell that opens with one used as
    a marker, where punctuation separates it from the rest: ``TODO: choose a
    milestone``.

    A status word that merely begins a sentence is left alone, because it is
    usually carrying real information: "Pending Mathlib PR 1234" and "Unknown
    provenance, excluded by agreement" both name something a reader can check.
    Rejecting those pushed authors toward vaguer wording to satisfy the checker.

    The gap this leaves is a marker written without punctuation, as in "TODO
    choose a milestone". That reads as prose to any rule cheap enough to trust,
    so it is left to human review rather than guessed at.
    """

    stripped = re.sub(r"[*_~\\]", "", visible)
    words = re.findall(r"\w+", stripped.casefold())
    if not words:
        return False
    if all(word in _PLACEHOLDER_EVIDENCE for word in words):
        return True
    if words[0] not in _PLACEHOLDER_EVIDENCE:
        return False
    _, _, remainder = stripped.casefold().partition(words[0])
    return _MARKER_PUNCTUATION.match(remainder) is not None


def _is_roadmap_article(target: str, *, coverage_path: Path, roadmap: Path) -> bool:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return False
    try:
        raw_path = unquote(parsed.path)
        if not raw_path or "\x00" in raw_path:
            return False
        candidate = (coverage_path.parent / raw_path).resolve()
        candidate.relative_to(roadmap)
        return candidate.is_file() and candidate.suffix.casefold() == ".md"
    except (OSError, RuntimeError, ValueError):
        return False


def _cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return ()
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in stripped[1:-1]:
        if escaped:
            current.append(character if character == "|" else f"\\{character}")
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return tuple(cells)


def _inline_code(value: str) -> str:
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        return value[1:-1].strip()
    return value.strip()


__all__ = [
    "COVERAGE_DISPOSITIONS",
    "COVERAGE_SCHEMA",
    "COVERAGE_V2_SCHEMA",
    "CoverageEntry",
    "CoverageIssue",
    "CoverageNodeBinding",
    "CoverageSummary",
    "CoverageUnit",
    "load_coverage",
]
