"""Resolve blueprint ``lean:`` declarations to their source location.

Scanning the project's own Lean files keeps two promises at once: a proved node
can link to the line that proves it, and a ``lean:`` name that resolves to
nothing is a validation error rather than a broken link -- the job
``leanblueprint checkdecls`` does for LaTeX blueprints.

The scanner is a lexical pass, not an elaborator. It tracks ``namespace`` and
comment nesting, which is enough for declarations written in the ordinary way,
and deliberately reports nothing it cannot see rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_NAMESPACE = re.compile(r"^\s*namespace\s+(.+)$")
_SECTION = re.compile(r"^\s*section\b\s*(\S*)")
_END = re.compile(r"^\s*end\b\s*(\S*)")
_DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque|axiom)\s+(.+)$"
)
_IGNORED_DIRECTORIES = frozenset({".lake", ".git", "lake-packages", "build"})
_MANAGED_OUTPUT_SCHEMAS = frozenset(
    {
        ("packets", "autoform-skeleton-packets/v1"),
        ("passages", "autoform-skeleton-passages/v1"),
    }
)


@dataclass(frozen=True, slots=True)
class Declaration:
    """One Lean declaration found in the project's sources."""

    name: str
    path: Path
    line: int
    keyword: str


@dataclass(frozen=True, slots=True)
class SourceIndex:
    """Every declaration the scanner found, keyed by fully qualified name."""

    root: Path
    declarations: dict[str, Declaration]
    source_digest: str

    def find(self, name: str) -> Declaration | None:
        return self.declarations.get(name)


def index_project(root: str | Path) -> SourceIndex:
    """Scan ``*.lean`` beneath *root* and index declarations by full name."""
    root_path = Path(root).expanduser().resolve()
    declarations: dict[str, Declaration] = {}
    digest = hashlib.sha256()
    if not root_path.is_dir():
        return SourceIndex(root=root_path, declarations=declarations, source_digest=digest.hexdigest())

    paths: list[Path] = []
    for directory, names, files in os.walk(root_path):
        current = Path(directory)
        names[:] = sorted(
            name
            for name in names
            if name not in _IGNORED_DIRECTORIES
            and not _is_managed_output(current / name)
        )
        paths.extend(current / name for name in files if name.endswith(".lean"))

    for path in sorted(paths):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(root_path)
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.encode("utf-8"))
        digest.update(b"\0")
        for declaration in _scan(text, relative):
            # First definition wins, so an earlier file is not masked by a later
            # one when a name is genuinely duplicated across namespaces.
            declarations.setdefault(declaration.name, declaration)
    return SourceIndex(root=root_path, declarations=declarations, source_digest=digest.hexdigest())


def _is_managed_output(path: Path) -> bool:
    """Whether ``path`` is an Autoform packet tree rather than project source."""

    manifest = path / "manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and (
        payload.get("kind"), payload.get("schema")
    ) in _MANAGED_OUTPUT_SCHEMAS


def _scan(text: str, relative: Path) -> list[Declaration]:
    found: list[Declaration] = []
    namespaces: list[str] = []
    scopes: list[str | None] = []

    for number, line in enumerate(_without_lean_comments(text).splitlines(), start=1):
        if not line.strip():
            continue

        namespace_match = _NAMESPACE.match(line)
        if namespace_match:
            name = _name_token(namespace_match.group(1))
            if name is None:
                continue
            namespaces.append(name)
            scopes.append(name)
            continue

        section_match = _SECTION.match(line)
        if section_match:
            scopes.append(None)
            continue

        end_match = _END.match(line)
        if end_match:
            if scopes:
                closed = scopes.pop()
                if closed is not None and namespaces:
                    namespaces.pop()
            continue

        declaration_match = _DECLARATION.match(line)
        if declaration_match:
            keyword = declaration_match.group(1)
            name = _name_token(declaration_match.group(2))
            if name is None:
                continue
            qualified = ".".join([*namespaces, name])
            found.append(Declaration(qualified, relative, number, keyword))
    return found


def _name_token(text: str) -> str | None:
    """Read one possibly guillemet-quoted Lean identifier from ``text``."""

    quoted = False
    for index, character in enumerate(text):
        if character == "«":
            if quoted:
                return None
            quoted = True
        elif character == "»":
            if not quoted:
                return None
            quoted = False
        elif not quoted and (character.isspace() or character in ":(){}[]⦃⦄,"):
            return text[:index] or None
    return None if quoted else text or None


def _raw_string_close(text: str, index: int) -> str | None:
    """Return the closing delimiter when ``text[index:]`` starts a raw string."""

    if text[index : index + 1] != "r":
        return None
    cursor = index + 1
    while text[cursor : cursor + 1] == "#":
        cursor += 1
    if text[cursor : cursor + 1] != '"':
        return None
    return '"' + "#" * (cursor - index - 1)


@dataclass(slots=True)
class _LexContext:
    kind: str
    close: str = ""
    interpolated: bool = False
    escaped: bool = False
    depth: int = 0


def _interpolated_quote(text: str, index: int) -> bool:
    """Whether the quote at ``index`` starts an interpolated string macro."""

    if index < 2 or text[index - 1] != "!":
        return False
    cursor = index - 2
    if not (text[cursor].isalnum() or text[cursor] == "_"):
        return False
    while cursor >= 0 and (text[cursor].isalnum() or text[cursor] in {"_", "'"}):
        cursor -= 1
    return cursor < index - 2


def _starts_char_literal(text: str, index: int) -> bool:
    """Distinguish a character literal from the apostrophe in a Lean name."""

    if index > 0 and (text[index - 1].isalnum() or text[index - 1] in {"_", "'"}):
        return False
    escaped = False
    for character in text[index + 1 :]:
        if character == "\n":
            return False
        if not escaped and character == "'":
            return True
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
    return False


def _without_lean_comments(text: str) -> str:
    """Remove nested Lean comments without treating string contents as comments.

    Newlines inside comments are retained so declaration line numbers remain
    coordinates into the original file.
    """

    out: list[str] = []
    index = 0
    block_depth = 0
    contexts = [_LexContext("code")]
    while index < len(text):
        pair = text[index : index + 2]
        if block_depth:
            if pair == "-/":
                block_depth -= 1
                index += 2
                continue
            if pair == "/-":
                block_depth += 1
                index += 2
                continue
            if text[index] == "\n":
                out.append("\n")
            index += 1
            continue
        context = contexts[-1]
        if context.kind == "string":
            if not context.escaped and text.startswith(context.close, index):
                out.append(context.close)
                index += len(context.close)
                contexts.pop()
                continue
            char = text[index]
            if context.interpolated and not context.escaped and char == "{":
                if text[index : index + 2] == "{{":
                    out.append("{{")
                    index += 2
                    continue
                out.append(char)
                index += 1
                contexts.append(_LexContext("interpolation", depth=1))
                continue
            out.append(char)
            index += 1
            if context.close in {'"', "'"}:
                if context.escaped:
                    context.escaped = False
                elif char == "\\":
                    context.escaped = True
            continue
        raw_close = _raw_string_close(text, index)
        if raw_close is not None:
            prefix_length = len(raw_close) + 1
            out.append(text[index : index + prefix_length])
            index += prefix_length
            contexts.append(_LexContext("string", close=raw_close))
            continue
        if text[index] == '"':
            out.append('"')
            index += 1
            contexts.append(
                _LexContext(
                    "string",
                    close='"',
                    interpolated=_interpolated_quote(text, index - 1),
                )
            )
            continue
        if text[index] == "«":
            out.append("«")
            index += 1
            contexts.append(_LexContext("string", close="»"))
            continue
        if text[index] == "'" and _starts_char_literal(text, index):
            out.append("'")
            index += 1
            contexts.append(_LexContext("string", close="'"))
            continue
        if pair == "--":
            newline = text.find("\n", index + 2)
            if newline < 0:
                break
            out.append("\n")
            index = newline + 1
            continue
        if pair == "/-":
            block_depth = 1
            out.append(" ")
            index += 2
            continue
        if context.kind == "interpolation":
            if text[index] == "{":
                context.depth += 1
            elif text[index] == "}":
                context.depth -= 1
                if context.depth == 0:
                    out.append("}")
                    index += 1
                    contexts.pop()
                    continue
        out.append(text[index])
        index += 1
    return "".join(out)


def strip_lean_comments(text: str) -> str:
    """Remove every line and block comment, docstrings included, from Lean source.

    Blank lines left behind are dropped, so the result is what the kernel sees
    and nothing an author wrote for a reader.
    """

    kept: list[str] = []
    for line in _without_lean_comments(text).splitlines():
        if line.strip():
            kept.append(line.rstrip())
    return "\n".join(kept)


def declaration_names(lean: str) -> list[str]:
    """Split a ``lean:`` frontmatter value into individual declaration names."""

    names: list[str] = []
    current: list[str] = []
    quoted = False
    for character in lean:
        if character == "«":
            quoted = True
        elif character == "»":
            quoted = False
        if not quoted and (character == "," or character.isspace()):
            if current:
                names.append("".join(current))
                current = []
        else:
            current.append(character)
    if current:
        names.append("".join(current))
    return names


@dataclass(frozen=True, slots=True)
class SourceLinker:
    """Build permalinks into the project's Lean sources."""

    index: SourceIndex
    repository_url: str | None = None
    ref: str | None = None

    def location(self, name: str) -> Declaration | None:
        return self.index.find(name)

    def url(self, name: str) -> str | None:
        """Return a permanent link to *name*, or ``None`` if it cannot be built."""
        declaration = self.index.find(name)
        if declaration is None or not self.repository_url or not self.ref:
            return None
        path = declaration.path.as_posix()
        return f"{self.repository_url}/blob/{self.ref}/{path}#L{declaration.line}"


def build_linker(
    lean_root: str | Path,
    *,
    repository_url: str | None = None,
    ref: str | None = None,
) -> SourceLinker:
    """Index *lean_root* and resolve the repository coordinates to link against."""
    return SourceLinker(
        index=index_project(lean_root),
        repository_url=repository_url or detect_repository_url(lean_root),
        ref=ref or detect_ref(lean_root),
    )


def detect_repository_url(root: str | Path) -> str | None:
    """Find the project's web URL from the CI environment or the git remote."""
    repository = os.environ.get("GITHUB_REPOSITORY")
    if repository:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server.rstrip('/')}/{repository}"
    remote = _git(root, "config", "--get", "remote.origin.url")
    return _normalize_remote(remote) if remote else None


def detect_ref(root: str | Path) -> str | None:
    """Prefer the exact commit so links keep pointing at the reviewed code."""
    return os.environ.get("GITHUB_SHA") or _git(root, "rev-parse", "HEAD")


def _normalize_remote(remote: str) -> str | None:
    remote = remote.strip()
    if remote.startswith("git@"):
        host, _, path = remote[4:].partition(":")
        if not path:
            return None
        remote = f"https://{host}/{path}"
    elif remote.startswith("ssh://git@"):
        remote = "https://" + remote[len("ssh://git@") :]
    if not remote.startswith(("http://", "https://")):
        return None
    return remote[: -len(".git")] if remote.endswith(".git") else remote.rstrip("/")


def _git(root: str | Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


__all__ = [
    "Declaration",
    "SourceIndex",
    "SourceLinker",
    "build_linker",
    "declaration_names",
    "detect_ref",
    "detect_repository_url",
    "index_project",
    "strip_lean_comments",
]
