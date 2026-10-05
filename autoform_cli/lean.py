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
from dataclasses import dataclass, field
from pathlib import Path

_NAMESPACE = re.compile(r"^\s*namespace\s+(.+)$")
_SECTION = re.compile(r"^\s*section\b\s*(\S*)")
_END = re.compile(r"^\s*end\b\s*(\S*)")
_DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque|axiom|irreducible_def)\s+(.+)$"
)
_ATTRIBUTES = re.compile(r"@\[[^\]]*\]")
_IGNORED_DIRECTORIES = frozenset({".lake", ".git", "lake-packages", "build"})
#: Known schemas of the skeleton command's packet and passage manifests.
PACKET_SCHEMA = "autoform-skeleton-packets/v2"
PASSAGE_SCHEMA = "autoform-skeleton-passages/v2"
MANAGED_OUTPUT_SCHEMAS = frozenset(
    {
        ("packets", "autoform-skeleton-packets/v1"),
        ("packets", PACKET_SCHEMA),
        ("passages", "autoform-skeleton-passages/v1"),
        ("passages", PASSAGE_SCHEMA),
    }
)


@dataclass(frozen=True, slots=True)
class Declaration:
    """One Lean declaration found in the project's sources."""

    name: str
    path: Path
    line: int
    keyword: str
    #: A private declaration is indexed by the name its source writes, without
    #: the module-specific prefix Lean gives it.
    private: bool = False


@dataclass(frozen=True, slots=True)
class SourceIndex:
    """Every declaration the scanner found, keyed by fully qualified name.

    ``declarations`` keeps the first lexical definition of each name.
    ``duplicates`` lists every definition of a name that resolves to more than
    one declaration: two public ones, or, with no public one, two private ones.
    A public name and private ones elsewhere are distinct in Lean, so they are
    not duplicates.
    """

    root: Path
    declarations: dict[str, Declaration]
    source_digest: str
    duplicates: dict[str, tuple[Declaration, ...]] = field(default_factory=dict)

    def find(self, name: str) -> Declaration | None:
        return self.declarations.get(name)


def index_project(root: str | Path) -> SourceIndex:
    """Scan ``*.lean`` beneath *root* and index declarations by full name."""
    root_path = Path(root).expanduser().resolve()
    digest = hashlib.sha256()
    if not root_path.is_dir():
        return SourceIndex(root=root_path, declarations={}, source_digest=digest.hexdigest())

    paths: list[Path] = []
    for directory, names, files in os.walk(root_path):
        current = Path(directory)
        # A nested checkout, such as a worker's Git worktree under .claude/,
        # is another copy of the sources, so its declarations must not
        # shadow this project's.
        names[:] = sorted(
            name
            for name in names
            if name not in _IGNORED_DIRECTORIES
            and not os.path.lexists(current / name / ".git")
            and not _is_managed_output(current / name)
        )
        paths.extend(current / name for name in files if name.endswith(".lean"))

    found: dict[str, list[Declaration]] = {}
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
            found.setdefault(declaration.name, []).append(declaration)
    # First definition wins, so an earlier file is not masked by a later one
    # when a name is genuinely duplicated across namespaces.
    declarations = {name: same[0] for name, same in found.items()}
    duplicates = {name: tuple(same) for name, same in found.items() if _ambiguous(same)}
    return SourceIndex(
        root=root_path, declarations=declarations, source_digest=digest.hexdigest(), duplicates=duplicates
    )


def _ambiguous(same: list[Declaration]) -> bool:
    """Whether a name resolves to several declarations, as ``impact`` resolves it."""

    public = sum(1 for declaration in same if not declaration.private)
    return public > 1 or (public == 0 and len(same) > 1)


def _is_managed_output(path: Path) -> bool:
    """Whether ``path`` is an Autoform packet tree rather than project source."""

    manifest = path / "manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and (
        payload.get("kind"), payload.get("schema")
    ) in MANAGED_OUTPUT_SCHEMAS


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
            modifiers = _ATTRIBUTES.sub(" ", line[: declaration_match.start(1)]).split()
            found.append(Declaration(qualified, relative, number, keyword, private="private" in modifiers))
    return found


_NAME_TOKEN = re.compile(r"(?:«[^«»]*»|[^\s:(){}\[\]⦃⦄,«»])+")


def _name_token(text: str) -> str | None:
    """Read one possibly guillemet-quoted Lean identifier from ``text``."""

    match = _NAME_TOKEN.match(text)
    return match.group() if match and text[match.end() : match.end() + 1] not in ("«", "»") else None


_RAW_OPEN = re.compile(r'r(#*)"')
_CHAR_LITERAL = re.compile(r"'(?:\\.|[^'\\\n])*'")


def _without_lean_comments(text: str) -> str:
    """Remove nested Lean comments, keeping strings intact and newlines in place."""

    out: list[str] = []
    stack: list[list] = []  # [closer, interpolated] for a string, [None, depth] for `{…}` in `s!"…"`
    index = 0
    while index < len(text):
        top = stack[-1] if stack else None
        char = text[index]
        if top is not None and top[0] is not None:
            close, interpolated = top
            if text.startswith(close, index):
                out.append(close)
                index += len(close)
                stack.pop()
                continue
            if close in {'"', "'"} and char == "\\":
                out.append(text[index : index + 2])
                index += 2
                continue
            if interpolated and char == "{":
                stack.append([None, 1])
            out.append(char)
            index += 1
            continue
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            if newline < 0:
                break
            out.append("\n")
            index = newline + 1
            continue
        if text.startswith("/-", index):
            # `/--` and `/-!` open a docstring whose body starts after the marker.
            depth, index = 1, index + (3 if text[index + 2 : index + 3] in {"-", "!"} else 2)
            out.append(" ")
            while index < len(text) and depth:
                pair = text[index : index + 2]
                if pair in {"-/", "/-"}:
                    depth += 1 if pair == "/-" else -1
                    index += 2
                    continue
                if text[index] == "\n":
                    out.append("\n")
                index += 1
            continue
        raw = _RAW_OPEN.match(text, index)
        if raw:
            out.append(raw.group())
            index = raw.end()
            stack.append(['"' + raw.group(1), False])
            continue
        previous = text[index - 1] if index else " "
        if char == '"':
            interpolated = previous == "!" and index >= 2 and (text[index - 2].isalnum() or text[index - 2] == "_")
            stack.append(['"', interpolated])
        elif char == "«":
            stack.append(["»", False])
        elif char == "'" and not (previous.isalnum() or previous in "_'") and _CHAR_LITERAL.match(text, index):
            stack.append(["'", False])
        elif top is not None and char in "{}":
            top[1] += 1 if char == "{" else -1
            if top[1] == 0:
                stack.pop()
        out.append(char)
        index += 1
    return "".join(out)


def strip_lean_comments(text: str) -> str:
    """Remove every line and block comment, docstrings included, from Lean source."""

    return "\n".join(
        line.rstrip() for line in _without_lean_comments(text).splitlines() if line.strip()
    )


_DECLARATION_NAME = re.compile(r"(?:«[^»]*(?:»|$)|[^\s,«])+")


def declaration_names(lean: str) -> list[str]:
    """Split a ``lean:`` frontmatter value into individual declaration names."""

    return _DECLARATION_NAME.findall(lean)


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
    scheme, _, rest = remote.partition("://")
    authority, slash, path = rest.partition("/")
    remote = f"{scheme}://{authority.rpartition('@')[2]}{slash}{path}"
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
    "MANAGED_OUTPUT_SCHEMAS",
    "PACKET_SCHEMA",
    "PASSAGE_SCHEMA",
    "SourceIndex",
    "SourceLinker",
    "build_linker",
    "declaration_names",
    "detect_ref",
    "detect_repository_url",
    "index_project",
    "strip_lean_comments",
]
