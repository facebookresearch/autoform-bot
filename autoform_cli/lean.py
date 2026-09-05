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

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_LINE_COMMENT = re.compile(r"--.*$")
_NAMESPACE = re.compile(r"^\s*namespace\s+(\S+)")
_SECTION = re.compile(r"^\s*section\b\s*(\S*)")
_END = re.compile(r"^\s*end\b\s*(\S*)")
_DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:public|private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque|axiom)\s+"
    r"([^\s:(){}\[\]⦃⦄,]+)"
)
_IGNORED_DIRECTORIES = frozenset({".lake", ".git", "lake-packages", "build"})


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
    occurrences: dict[str, tuple[Declaration, ...]]

    def find(self, name: str) -> Declaration | None:
        return self.declarations.get(name)


def index_project(root: str | Path) -> SourceIndex:
    """Scan ``*.lean`` beneath *root* and index declarations by full name."""
    root_path = Path(root).expanduser().resolve()
    declarations: dict[str, Declaration] = {}
    occurrences: dict[str, list[Declaration]] = {}
    if not root_path.is_dir():
        return SourceIndex(root=root_path, declarations=declarations, occurrences={})

    for path in sorted(root_path.rglob("*.lean")):
        if _IGNORED_DIRECTORIES.intersection(path.relative_to(root_path).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(root_path)
        for declaration in _scan(text, relative):
            occurrences.setdefault(declaration.name, []).append(declaration)
            # First definition wins, so an earlier file is not masked by a later
            # one when a name is genuinely duplicated across namespaces.
            declarations.setdefault(declaration.name, declaration)
    return SourceIndex(
        root=root_path,
        declarations=declarations,
        occurrences={name: tuple(found) for name, found in occurrences.items()},
    )


def _scan(text: str, relative: Path) -> list[Declaration]:
    found: list[Declaration] = []
    namespaces: list[str] = []
    scopes: list[str | None] = []
    comment_depth = 0

    for number, raw in enumerate(text.splitlines(), start=1):
        line, comment_depth = _strip_comments(raw, comment_depth)
        if not line.strip():
            continue

        namespace_match = _NAMESPACE.match(line)
        if namespace_match:
            name = namespace_match.group(1)
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
            keyword, name = declaration_match.group(1), declaration_match.group(2)
            qualified = ".".join([*namespaces, name])
            found.append(Declaration(qualified, relative, number, keyword))
    return found


def _strip_comments(line: str, depth: int) -> tuple[str, int]:
    """Remove Lean comments from *line*, carrying block-comment depth across."""
    out: list[str] = []
    index = 0
    while index < len(line):
        pair = line[index : index + 2]
        if depth:
            if pair == "-/":
                depth -= 1
                index += 2
                continue
            if pair == "/-":
                depth += 1
                index += 2
                continue
            index += 1
            continue
        if pair == "/-":
            depth += 1
            index += 2
            continue
        out.append(line[index])
        index += 1
    return _LINE_COMMENT.sub("", "".join(out)), depth


def declaration_names(lean: str) -> list[str]:
    """Split a ``lean:`` frontmatter value into individual declaration names."""
    return [name.strip() for name in lean.replace(",", " ").split() if name.strip()]


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
        return self.declaration_url(declaration) if declaration is not None else None

    def declaration_url(self, declaration: Declaration) -> str | None:
        """Return a permanent link to a particular declaration occurrence."""
        if not self.repository_url or not self.ref:
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
]
