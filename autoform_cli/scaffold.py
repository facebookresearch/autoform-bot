"""Write a blueprint vault deterministically instead of describing one.

Setup used to instruct an agent, in prose, to create ``blueprint/`` with a
landing page, ``roadmap/``, ``coverage/``, and ``sources/``, and to imitate the
bundled example. Agents improvise: a real project came back with chapter pages
as siblings of their directories rather than as ``<chapter>/README.md``, which
parses cleanly and publishes a book with no chapters at all. The structure is
fixed, so the tool writes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_MAX_GITIGNORE_BYTES = 1024 * 1024
_MAX_TEMPLATE_ENTRIES = 512
_MAX_TEMPLATE_DEPTH = 16
_MAX_TEMPLATE_FILE_BYTES = 1024 * 1024
_MAX_TEMPLATE_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_SCAFFOLD_SOURCE_BYTES = 1024 * 1024
_MAX_GIT_TREE_LIST_BYTES = 256 * 1024
_MAX_GIT_REMOTE_LIST_BYTES = 64 * 1024
_WINDOWS_STAT_VIEWS = os.name == "nt"

#: Template paths whose leading dot is dropped on disk so packaging tools and
#: ignore rules do not swallow them.
_DOTTED = {
    "gitignore": ".gitignore",
    "blueprint/gitignore": "blueprint/.gitignore",
    "github": ".github",
}

DEFAULT_AUTOFORM_SOURCE = "https://github.com/facebookresearch/autoform-bot.git"
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SOURCE_HOST = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)
_SOURCE_PATH_PART = re.compile(r"[A-Za-z0-9._~-]+")
_GITHUB_SCP_SOURCE = re.compile(
    r"git@github\.com:(?P<path>[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)+)"
)
_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{(?P<name>[A-Z][A-Z0-9_]*)\}\}")
#: Template files `autoform project new` refuses to publish a project without.
_REQUIRED_TEMPLATE_PATHS = frozenset(
    {
        "README.md",
        "blueprint/README.md",
        "blueprint/coverage/README.md",
        "blueprint/gitignore",
        "blueprint/javascripts/mathjax.js",
        "blueprint/roadmap/README.md",
        "blueprint/sources/README.md",
        "github/CODEOWNERS.autoform.example",
        "github/autoform_audit.py",
        "github/workflows/autoform-verify.yml",
        "github/workflows/blueprint-pages.yml",
        "gitignore",
        "mkdocs.yml",
        "theme/main.html",
    }
)
#: Where `claude plugin install` records the marketplace each plugin came from.
_PLUGIN_REGISTRY = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"


def _here() -> Path:
    """The Autoform directory this CLI is running out of."""

    return Path(__file__).resolve().parent.parent


def _git(*args: str, root: Path | None = None) -> str | None:
    """Read a value from an Autoform checkout, defaulting to this one."""

    try:
        done = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(root or _here()), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    value = done.stdout.strip()
    return value if done.returncode == 0 and value else None


def _checkout_root(directory: Path) -> Path | None:
    """*directory* if it is itself the root of a Git checkout, otherwise ``None``.

    ``git -C`` searches upwards, so asking an installed copy for "its" origin
    answers with whatever repository happens to enclose it. Autoform installed
    into a project's own virtualenv sits under that project, so the plain
    question pins the project's CI to the project, at the project's HEAD --
    a pin that is both wrong and confidently specific.
    """

    top = _git("rev-parse", "--show-toplevel", root=directory)
    if top is None:
        return None
    return directory if Path(top).resolve() == directory.resolve() else None


def _marketplace_checkout() -> Path | None:
    """The checkout an installed plugin copy was made from, if it is on disk.

    `claude plugin install` copies into
    ``~/.claude/plugins/cache/<marketplace>/<plugin>/<version>`` from a
    marketplace it keeps as a real Git checkout, and records where in
    ``known_marketplaces.json``. That checkout is this code's actual provenance,
    so reading it is not the guess :func:`plugin_pin` refuses to make.

    Returns ``None`` on anything unexpected: not running from a plugin cache, no
    registry, no such marketplace, or a location that is not a checkout of
    Autoform. A wrong answer here is worse than no answer.
    """

    parts = _here().parts
    try:
        cache = len(parts) - 1 - parts[::-1].index("cache")
    except ValueError:
        return None
    if cache + 1 >= len(parts):
        return None
    try:
        registry = json.loads(_PLUGIN_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = registry.get(parts[cache + 1]) if isinstance(registry, dict) else None
    location = entry.get("installLocation") if isinstance(entry, dict) else None
    if not isinstance(location, str) or not location:
        return None
    checkout = Path(location).expanduser()
    # Insist it is a checkout of *this* project, and is itself the root of one
    # rather than a directory sitting somewhere inside an unrelated repository.
    if not (checkout / "autoform_cli" / "scaffold.py").is_file():
        return None
    return _checkout_root(checkout)


def _normalize_autoform_source(source: str, *, allow_github_scp: bool = False) -> str | None:
    """Return a safe, credential-free HTTPS Git source or ``None``.

    Generated workflows persist this value and pass it to a shell. Keep the
    accepted language deliberately small instead of attempting to quote every
    URL or Git transport syntax. The one non-URL form is GitHub's SCP-style
    origin, which is normalized only when reading local checkout provenance.
    """

    if not source or source != source.strip() or "?" in source or "#" in source:
        return None
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in source):
        return None
    if allow_github_scp:
        scp = _GITHUB_SCP_SOURCE.fullmatch(source)
        if scp is not None:
            path = scp.group("path")
            source = f"https://github.com/{path if path.endswith('.git') else f'{path}.git'}"
    try:
        parsed = urlsplit(source)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.lower() != parsed.hostname.lower()
        or not _SOURCE_HOST.fullmatch(parsed.hostname)
    ):
        return None
    parts = parsed.path.split("/")
    if (
        len(parts) < 2
        or parts[0]
        or any(part in {"", ".", ".."} for part in parts[1:])
        or any(_SOURCE_PATH_PART.fullmatch(part) is None for part in parts[1:])
        or not parts[-1].endswith(".git")
        or parts[-1] == ".git"
    ):
        return None
    return source


def _git_checkout_clean(root: Path) -> bool:
    """Whether *root* has no tracked or staged changes from ``HEAD``."""

    git = ["git", "--no-optional-locks", "--no-replace-objects", "-c", "core.fsmonitor=false"]
    options = ["--quiet", "--no-ext-diff", "--no-textconv", "HEAD", "--"]
    try:
        return all(
            subprocess.run(
                [*git, "-C", str(root), "diff", *cached, *options],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            ).returncode
            == 0
            for cached in ((), ("--cached",))
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _git_bytes(root: Path, *args: str, limit: int) -> bytes | None:
    """Run a bounded local Git object query."""

    try:
        done = subprocess.run(
            ["git", "--no-optional-locks", "--no-replace-objects", "-C", str(root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or len(done.stdout) > limit:
        return None
    return done.stdout


def _safe_remote_source(root: Path, remote: str) -> str | None:
    source = _git("remote", "get-url", remote, root=root)
    if not source:
        return None
    if not source.endswith(".git"):
        source = f"{source}.git"
    return _normalize_autoform_source(source, allow_github_scp=True)


def _remote_contains_ref(root: Path, remote: str, ref: str) -> bool:
    """Whether a cached tracking ref for *remote* contains *ref*."""

    prefix = f"refs/remotes/{remote}/"
    contained = _git_bytes(
        root,
        "for-each-ref",
        f"--contains={ref}",
        "--count=1",
        "--format=%(refname)",
        prefix,
        limit=_MAX_GIT_REMOTE_LIST_BYTES,
    )
    if contained is None:
        return False
    try:
        refs = contained.decode("utf-8").splitlines()
    except UnicodeError:
        return False
    return bool(refs) and all(name.startswith(prefix) and name != prefix for name in refs)


def _select_remote_source(root: Path, ref: str) -> str | None:
    """Choose a safe remote whose cached tracking refs contain *ref*.

    Prefer Autoform's canonical source, then conventional ``origin`` and
    ``upstream`` names. Without those, accept only one distinct safe source so
    automatic pinning never guesses between unrelated remotes.
    """

    encoded = _git_bytes(root, "remote", limit=_MAX_GIT_REMOTE_LIST_BYTES)
    if encoded is None:
        return None
    try:
        names = encoded.decode("utf-8").splitlines()
    except UnicodeError:
        return None
    candidates: dict[str, str] = {}
    for name in names:
        if _REMOTE_NAME.fullmatch(name) is None or not _remote_contains_ref(root, name, ref):
            continue
        source = _safe_remote_source(root, name)
        if source is not None:
            candidates[name] = source
    if DEFAULT_AUTOFORM_SOURCE in candidates.values():
        return DEFAULT_AUTOFORM_SOURCE
    for preferred in ("origin", "upstream"):
        if preferred in candidates:
            return candidates[preferred]
    sources = set(candidates.values())
    return sources.pop() if len(sources) == 1 else None


def _committed_scaffold_entries(
    root: Path,
    ref: str,
) -> dict[str, tuple[str, str]]:
    """Return ``path -> (mode, blob id)`` for the pinned scaffold surface."""

    listing = _git_bytes(
        root,
        "ls-tree",
        "-rz",
        "--full-tree",
        ref,
        "--",
        "autoform_cli/scaffold.py",
        "autoform_cli/templates",
        limit=_MAX_GIT_TREE_LIST_BYTES,
    )
    if listing is None:
        raise ScaffoldError(["the pinned Autoform tree could not be read safely"])
    entries: dict[str, tuple[str, str]] = {}
    template_count = 0
    object_length: int | None = None
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode, kind, object_id = metadata.split(b" ")
            path = encoded_path.decode("utf-8")
            object_name = object_id.decode("ascii")
        except (UnicodeError, ValueError):
            raise ScaffoldError(["the pinned Autoform tree is invalid"]) from None
        if (
            path in entries
            or kind != b"blob"
            or mode not in {b"100644", b"100755"}
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", object_name) is None
            or (object_length is not None and len(object_name) != object_length)
        ):
            raise ScaffoldError(["the pinned Autoform tree is invalid"])
        object_length = len(object_name)
        if path == "autoform_cli/scaffold.py":
            entries[path] = (mode.decode("ascii"), object_name)
            continue
        prefix = "autoform_cli/templates/"
        if not path.startswith(prefix) or template_count >= _MAX_TEMPLATE_ENTRIES:
            raise ScaffoldError(["the pinned Autoform template tree is invalid"])
        template_count += 1
        entries[path] = (mode.decode("ascii"), object_name)
    if "autoform_cli/scaffold.py" not in entries:
        raise ScaffoldError(["the pinned Autoform renderer is missing"])
    return entries


def _git_blob_id(content: bytes, length: int) -> str | None:
    algorithm = {40: "sha1", 64: "sha256"}[length]
    try:
        digest = hashlib.new(algorithm, usedforsecurity=False)
    except ValueError:
        return None
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _canonical_template_mode(mode: int) -> int:
    """Canonical generated mode for Git's one-bit executable distinction."""

    return 0o755 if mode & 0o111 else 0o644


def _template_surface(
    templates: tuple[tuple[str, bytes, int], ...],
) -> tuple[tuple[str, bytes, int], ...]:
    return tuple(
        (relative, content, _canonical_template_mode(mode))
        for relative, content, mode in templates
    )


def _matches_committed_scaffold(
    committed: dict[str, tuple[str, str]],
    templates: tuple[tuple[str, bytes, int], ...],
    scaffold_source: bytes,
    scaffold_mode: int,
) -> bool:
    """Whether retained bytes and output-affecting modes equal the Git tree."""

    # _committed_scaffold_entries guarantees one hash length and the renderer entry.
    object_length = len(committed["autoform_cli/scaffold.py"][1])
    actual = {
        path: (
            committed.get(path, ("", ""))[0]
            if _WINDOWS_STAT_VIEWS
            else ("100755" if mode & 0o111 else "100644"),
            _git_blob_id(content, object_length),
        )
        for path, content, mode in (
            ("autoform_cli/scaffold.py", scaffold_source, scaffold_mode),
            *(
                (f"autoform_cli/templates/{relative}", content, mode)
                for relative, content, mode in templates
            ),
        )
    }
    return actual == committed


def plugin_pin(
    templates: tuple[tuple[str, bytes, int], ...] | None = None,
) -> tuple[str, str]:
    """The Autoform source and commit generated CI should install, if knowable.

    Read from the Autoform checkout this CLI runs out of, or, when there is none
    because `claude plugin install` copied the directory without its `.git`,
    from the marketplace checkout that copy was made from. Both are records of
    where this code came from rather than assumptions about it, and both must be
    the root of a checkout: a directory that merely sits inside somebody else's
    repository answers questions about that repository.

    Tracked checkout files must be clean, a cached tracking ref for the selected
    remote must contain HEAD, and the committed renderer plus template paths,
    bytes, and executable-bit classifications must match both the retained
    generation snapshot and any installed plugin copy. Otherwise the source and
    commit do not describe fetchable scaffold behavior, so no pin is returned.
    Every Git identity and tree query ignores local replacement objects.

    Returns empty strings when neither is available. An earlier version fell
    back to `facebookresearch/autoform-bot@main` instead. That commit predates
    `autoform_cli` entirely, so every project scaffolded through the plugin got
    CI that installed a build with no `autoform` command and failed at the first
    step, with nothing in the workflow to explain why. A wrong pin is worse than
    no pin: guessing here is what made the failure silent.
    """

    try:
        retained_templates = _read_templates(_TEMPLATES) if templates is None else templates
    except ScaffoldError:
        return "", ""

    running_root = _here()
    root = _checkout_root(running_root) or _marketplace_checkout()
    if root is None:
        return "", ""
    ref = _git("rev-parse", "HEAD", root=root)
    if ref is None or not _FULL_SHA.fullmatch(ref) or not _git_checkout_clean(root):
        return "", ""
    source = _select_remote_source(root, ref)
    if source is None:
        return "", ""
    try:
        checkout_templates = _read_templates(root / "autoform_cli" / "templates")
        running_scaffold, _running_identity, running_mode = _read_bounded_regular_file(
            running_root / "autoform_cli" / "scaffold.py",
            limit=_MAX_SCAFFOLD_SOURCE_BYTES,
            label="Autoform scaffold source",
        )
        checkout_scaffold, _checkout_identity, checkout_mode = _read_bounded_regular_file(
            root / "autoform_cli" / "scaffold.py",
            limit=_MAX_SCAFFOLD_SOURCE_BYTES,
            label="Autoform scaffold source",
        )
        committed = _committed_scaffold_entries(root, ref)
    except ScaffoldError:
        return "", ""
    if (
        _template_surface(retained_templates) != _template_surface(checkout_templates)
        or (running_scaffold, _canonical_template_mode(running_mode))
        != (checkout_scaffold, _canonical_template_mode(checkout_mode))
        or not _matches_committed_scaffold(
            committed,
            checkout_templates,
            checkout_scaffold,
            checkout_mode,
        )
        or _git("rev-parse", "HEAD", root=root) != ref
        or _select_remote_source(root, ref) != source
        or not _git_checkout_clean(root)
    ):
        return "", ""
    return source, ref


class ScaffoldError(ValueError):
    """The project could not be scaffolded safely."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    """What a scaffold run wrote, and what it left alone."""

    project: str
    written: tuple[str, ...]
    skipped: tuple[str, ...]
    unpinned: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "written": list(self.written),
            "skipped": list(self.skipped),
            "unpinned": self.unpinned,
        }


@dataclass(frozen=True, slots=True)
class _ScaffoldFile:
    relative: str
    content: bytes
    mode: int


@dataclass(frozen=True, slots=True)
class _TemplateSnapshot:
    templates: tuple[tuple[str, bytes, int], ...]
    generations: tuple[tuple[str, tuple[int, ...]], ...]


def _destination(relative: str) -> str:
    for template_prefix, real_prefix in _DOTTED.items():
        if relative == template_prefix:
            return real_prefix
        if relative.startswith(f"{template_prefix}/"):
            return real_prefix + relative[len(template_prefix) :]
    return relative


def _yaml_scalar(value: str) -> str:
    """Serialize *value* as a quoted YAML scalar.

    JSON strings are valid YAML double-quoted scalars. Using the standard JSON
    serializer preserves the value while escaping line breaks, tabs, nulls,
    quotes, backslashes, and every other control character that could otherwise
    alter the generated document.
    """
    return json.dumps(value, ensure_ascii=False)


def _render(text: str, substitutions: dict[str, str]) -> str:
    """Substitute tokens from the original template exactly once.

    Replacement values are user-controlled in several templates. A sequential
    series of ``str.replace`` calls can reinterpret token-shaped text inside an
    earlier value, corrupting YAML and Markdown or exposing another generated
    value. A single regex pass never scans replacement content again.
    """

    return _TEMPLATE_PLACEHOLDER.sub(
        lambda match: substitutions.get(match.group("name"), match.group(0)),
        text,
    )


def _node_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _is_link(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _read_at_most(descriptor: int, size: int) -> bytes:
    """Read from *descriptor* until end of file or *size* bytes, whichever is first."""

    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_bounded_regular_file(
    path: Path,
    *,
    limit: int,
    label: str,
) -> tuple[bytes, tuple[int, ...], int]:
    """Read one stable, bounded regular file without following links."""

    descriptor: int | None = None
    try:
        before = os.stat(path, follow_symlinks=False)
        before_identity = _node_identity(before)
        if _is_link(before) or not stat.S_ISREG(before.st_mode):
            raise ScaffoldError([f"the {label} is not a regular non-link file"])
        if before.st_size > limit:
            raise ScaffoldError([f"the {label} exceeds its {limit}-byte limit"])
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        opened_identity = _node_identity(opened)
        if (
            _is_link(opened)
            or not stat.S_ISREG(opened.st_mode)
            or _cross_interface_identity(opened_identity)
            != _cross_interface_identity(before_identity)
        ):
            raise ScaffoldError([f"the {label} changed while it was being read"])

        content = _read_at_most(descriptor, limit + 1)
        after_identity = _node_identity(os.fstat(descriptor))
        named_identity = _node_identity(os.stat(path, follow_symlinks=False))
        if (
            opened_identity != after_identity
            or before_identity != named_identity
            or len(content) > limit
        ):
            message = (
                f"the {label} exceeds its {limit}-byte limit"
                if len(content) > limit
                else f"the {label} changed while it was being read"
            )
            raise ScaffoldError([message])
        return content, after_identity, stat.S_IMODE(opened.st_mode)
    except ScaffoldError:
        raise
    except (OSError, TypeError, ValueError):
        raise ScaffoldError([f"the {label} could not be read safely"]) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _capture_template_snapshot(root: Path) -> _TemplateSnapshot:
    """Capture one bounded, link-free generation of the template tree."""

    templates: list[tuple[str, bytes, int]] = []
    generations: list[tuple[str, tuple[int, ...]]] = []
    entry_count = 0
    total_bytes = 0

    def directory_names(directory: Path, *, count: bool) -> tuple[str, ...]:
        nonlocal entry_count
        names: list[str] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if count:
                    entry_count += 1
                    if entry_count > _MAX_TEMPLATE_ENTRIES:
                        raise ScaffoldError(
                            [f"the Autoform template tree exceeds {_MAX_TEMPLATE_ENTRIES} entries"]
                        )
                names.append(entry.name)
                if len(names) > _MAX_TEMPLATE_ENTRIES:
                    raise ScaffoldError(
                        [f"the Autoform template tree exceeds {_MAX_TEMPLATE_ENTRIES} entries"]
                    )
        return tuple(sorted(names))

    def walk(directory: Path, relative: Path, depth: int) -> None:
        nonlocal total_bytes
        if depth > _MAX_TEMPLATE_DEPTH:
            raise ScaffoldError(
                [f"the Autoform template tree exceeds {_MAX_TEMPLATE_DEPTH} directory levels"]
            )
        before = os.stat(directory, follow_symlinks=False)
        before_identity = _node_identity(before)
        if _is_link(before) or not stat.S_ISDIR(before.st_mode):
            raise ScaffoldError(["the Autoform template tree contains a non-directory link"])
        names = directory_names(directory, count=True)
        for name in names:
            path = directory / name
            child_relative = relative / name
            metadata = os.stat(path, follow_symlinks=False)
            if "__pycache__" in child_relative.parts or child_relative.suffix == ".pyc":
                continue
            if _is_link(metadata):
                raise ScaffoldError(["the Autoform template tree contains a link"])
            if stat.S_ISDIR(metadata.st_mode):
                walk(path, child_relative, depth + 1)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ScaffoldError(["the Autoform template tree contains a non-regular file"])
            content, identity, mode = _read_bounded_regular_file(
                path,
                limit=_MAX_TEMPLATE_FILE_BYTES,
                label="Autoform template file",
            )
            total_bytes += len(content)
            if total_bytes > _MAX_TEMPLATE_TOTAL_BYTES:
                raise ScaffoldError(
                    [
                        "the Autoform template tree exceeds its "
                        f"{_MAX_TEMPLATE_TOTAL_BYTES}-byte total limit"
                    ]
                )
            relative_name = child_relative.as_posix()
            templates.append((relative_name, content, mode))
            generations.append((relative_name, identity))

        after_identity = _node_identity(os.stat(directory, follow_symlinks=False))
        if before_identity != after_identity or names != directory_names(directory, count=False):
            raise ScaffoldError(["the Autoform template tree changed while it was being read"])
        generations.append(((relative.as_posix() or ".") + "/", after_identity))

    try:
        walk(root, Path(), 0)
    except ScaffoldError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError):
        raise ScaffoldError(["the Autoform template tree could not be read safely"]) from None
    return _TemplateSnapshot(tuple(sorted(templates)), tuple(sorted(generations)))


def _read_templates(root: Path) -> tuple[tuple[str, bytes, int], ...]:
    """Read one stable, bounded generation of regular non-link templates."""

    first = _capture_template_snapshot(root)
    second = _capture_template_snapshot(root)
    if first != second:
        raise ScaffoldError(["the Autoform template tree changed while it was being read"])
    return second.templates


def _require_complete_templates(templates: tuple[tuple[str, bytes, int], ...]) -> None:
    template_paths = [relative for relative, _content, _mode in templates]
    if len(template_paths) != len(set(template_paths)) or not _REQUIRED_TEMPLATE_PATHS.issubset(
        template_paths
    ):
        raise ScaffoldError(["the Autoform template tree is incomplete"])


def _scaffold_plan(
    templates: tuple[tuple[str, bytes, int], ...],
    *,
    title: str,
    repository_url: str,
    autoform_source: str,
    autoform_ref: str,
) -> tuple[tuple[_ScaffoldFile, ...], tuple[str, ...]]:
    """Render *templates* into project files and list the workflows left out.

    `autoform init` and `autoform project new` share this so both write the
    same bytes. Without *autoform_ref* the CI workflows are omitted.
    """

    substitutions = {
        "PROJECT_TITLE_YAML": _yaml_scalar(title),
        "REPO_URL_YAML": _yaml_scalar(repository_url),
        "PROJECT_TITLE": title,
        "REPO_URL": repository_url,
        "AUTOFORM_SOURCE": autoform_source,
        "AUTOFORM_REF": autoform_ref,
        "AUTOFORM_SOURCE_YAML": _yaml_scalar(autoform_source),
        "AUTOFORM_REF_YAML": _yaml_scalar(autoform_ref),
    }
    files: list[_ScaffoldFile] = []
    skipped: list[str] = []
    for relative, template_content, template_mode in templates:
        destination = _destination(relative)
        if (
            not autoform_ref
            and relative.startswith("github/")
            and relative != "github/CODEOWNERS.autoform.example"
        ):
            skipped.append(destination)
            continue
        if Path(relative).suffix in {".js", ".html"} or relative.endswith("gitignore"):
            content = template_content
        else:
            try:
                text = template_content.decode("utf-8")
            except UnicodeError:
                raise ScaffoldError(["the Autoform template tree contains invalid text"]) from None
            content = _render(text, substitutions).encode("utf-8")
        files.append(
            _ScaffoldFile(
                relative=destination,
                content=content,
                mode=_canonical_template_mode(template_mode),
            )
        )
    return tuple(files), tuple(skipped)


def _cross_interface_identity(identity: tuple[int, ...]) -> tuple[int, ...]:
    """Normalize Windows path-stat birth time versus fstat change time."""

    return identity[:-1] if _WINDOWS_STAT_VIEWS else identity


def _read_gitignore_descriptor(
    descriptor: int,
    path: Path,
) -> tuple[bytes, tuple[int, ...]]:
    """Read a stable bounded snapshot from one retained regular file."""

    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or _is_link(opened):
        raise ScaffoldError([f"refusing to merge non-regular .gitignore: {path}"])
    if opened.st_nlink != 1:
        raise ScaffoldError([f"refusing to merge hard-linked .gitignore: {path}"])
    if opened.st_size > _MAX_GITIGNORE_BYTES:
        raise ScaffoldError(
            [f"refusing to merge .gitignore larger than {_MAX_GITIGNORE_BYTES} bytes"]
        )
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = _read_at_most(descriptor, _MAX_GITIGNORE_BYTES + 1)
    if len(content) > _MAX_GITIGNORE_BYTES:
        raise ScaffoldError(
            [f"refusing to merge .gitignore larger than {_MAX_GITIGNORE_BYTES} bytes"]
        )
    after = os.fstat(descriptor)
    named = os.stat(path, follow_symlinks=False)
    opened_identity = _node_identity(opened)
    after_identity = _node_identity(after)
    named_identity = _node_identity(named)
    if (
        after.st_nlink != 1
        or named.st_nlink != 1
        or _is_link(named)
        or after_identity != opened_identity
        or _cross_interface_identity(named_identity)
        != _cross_interface_identity(opened_identity)
    ):
        raise ScaffoldError([f".gitignore changed while it was being inspected: {path}"])
    return content, named_identity


def _gitignore_suffix(existing: bytes, required: bytes) -> bytes | None:
    """The missing suffix to append while preserving every authored byte."""

    existing_lines = {line.removesuffix(b"\r") for line in existing.splitlines()}
    missing = [
        line
        for line in required.splitlines()
        if line and line.removesuffix(b"\r") not in existing_lines
    ]
    if not missing:
        return None
    separator = b"" if not existing or existing.endswith((b"\n", b"\r")) else b"\n"
    return separator + b"\n".join(missing) + b"\n"


def _gitignore_identity_matches(
    descriptor: int,
    path: Path,
    expected: tuple[int, ...],
) -> bool:
    try:
        opened = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(named.st_mode)
        and not _is_link(opened)
        and not _is_link(named)
        and opened.st_nlink == 1
        and named.st_nlink == 1
        and _cross_interface_identity(_node_identity(opened))
        == _cross_interface_identity(expected)
        == _cross_interface_identity(_node_identity(named))
    )


def _append_gitignore_rules(path: Path, required: bytes) -> bool:
    """Append missing rules through one retained descriptor without replacement."""

    flags = (
        os.O_RDWR
        | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = -1
    write_started = False
    try:
        descriptor = os.open(path, flags)
        existing, identity = _read_gitignore_descriptor(descriptor, path)
        suffix = _gitignore_suffix(existing, required)
        if suffix is None:
            os.close(descriptor)
            descriptor = -1
            return False
        if len(existing) + len(suffix) > _MAX_GITIGNORE_BYTES:
            raise ScaffoldError(
                [f"refusing to merge .gitignore larger than {_MAX_GITIGNORE_BYTES} bytes"]
            )
        if not _gitignore_identity_matches(descriptor, path, identity):
            raise ScaffoldError([f".gitignore changed before Autoform could append to it: {path}"])
        expected = existing + suffix
        offset = 0
        write_started = True
        while offset < len(suffix):
            written = os.write(descriptor, suffix[offset:])
            if written <= 0:
                raise OSError("short .gitignore append")
            offset += written
        os.fsync(descriptor)
        final, _identity = _read_gitignore_descriptor(descriptor, path)
        if final != expected:
            raise OSError(".gitignore changed during append")
        os.close(descriptor)
        descriptor = -1
        return True
    except ScaffoldError:
        if write_started:
            raise ScaffoldError(
                [
                    ".gitignore rules may have been partially appended; "
                    f"inspect the file before retrying: {path}"
                ]
            ) from None
        raise
    except (OSError, TypeError, ValueError):
        message = (
            ".gitignore rules may have been partially appended; inspect the file before retrying"
            if write_started
            else "cannot safely inspect or append to existing .gitignore"
        )
        raise ScaffoldError([f"{message}: {path}"]) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _atomic_write(
    destination: Path,
    content: bytes,
    *,
    mode: int,
) -> None:
    """Replace *destination* from a same-directory temporary file.

    Replacing rather than truncating is essential when an existing destination
    has hard links: ``--force`` must not modify another path to the old inode.
    """

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _within(path: Path, root: Path) -> bool:
    """True when *path* resolves to somewhere at or beneath *root*.

    Resolution follows symlinks, so this is what confines the scaffold: it is
    not enough to reject a symlinked project root, because a link one level
    down -- `project/blueprint` pointing elsewhere -- redirects the whole vault
    out of the project, and `--force` would then overwrite files there.
    """
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def scaffold_project(
    target: str | Path,
    *,
    title: str,
    repository_url: str = "",
    autoform_source: str = "",
    autoform_ref: str = "",
    force: bool = False,
) -> ScaffoldResult:
    """Write the blueprint vault, site config, and CI into *target*.

    Existing files are never overwritten unless *force* is set, except that
    missing Autoform rules are appended through a retained regular root
    ``.gitignore`` descriptor. Skipped paths report everything else the repair
    left in place.
    """

    requested = Path(target).expanduser()
    issues: list[str] = []
    if not title.strip():
        issues.append("project title must not be empty")
    # Checked before resolve(), which would collapse the link and hide it.
    if requested.is_symlink():
        issues.append(f"refusing to scaffold into a symlink: {requested}")
    root = requested.resolve()
    if root.exists() and not root.is_dir():
        issues.append(f"target exists and is not a directory: {root}")
    # A branch name or an abbreviated sha is the same silent failure this
    # gate exists to prevent, just supplied by hand: CI would reinstall a
    # different Autoform later and break a project that was passing.
    # Git treats a sha case-insensitively and always prints lowercase, so an
    # uppercase one pasted from a web UI is valid input, not a mistake.
    given_ref = autoform_ref.strip().lower()
    if given_ref and not _FULL_SHA.fullmatch(given_ref):
        issues.append(
            f"--autoform-ref must be a full 40-character commit sha, not {given_ref!r}; "
            "branches and abbreviated shas do not stay put"
        )
    given_source = ""
    if autoform_source:
        normalized = _normalize_autoform_source(autoform_source)
        if normalized is None:
            issues.append(
                "--autoform-source must be a safe credential-free HTTPS Git URL ending in .git"
            )
        else:
            given_source = normalized
    if issues:
        raise ScaffoldError(issues)

    # Retain the exact stable bytes that pin discovery validates. A later read
    # could race with a template replacement and generate content different
    # from the commit named by the workflows.
    templates = _read_templates(_TEMPLATES)
    _require_complete_templates(templates)
    pinned_source, pinned_ref = ("", "") if given_source else plugin_pin(templates)
    safe_pinned_source = _normalize_autoform_source(pinned_source, allow_github_scp=True)
    if safe_pinned_source is None or not _FULL_SHA.fullmatch(pinned_ref.lower()):
        pinned_source, pinned_ref = "", ""
    else:
        pinned_source, pinned_ref = safe_pinned_source, pinned_ref.lower()
    source = given_source or pinned_source or DEFAULT_AUTOFORM_SOURCE
    # A ref identifies a commit in one repository. Naming a different source
    # while inheriting this checkout's HEAD produces `git+other.git@our-sha`,
    # which does not resolve there, so an explicit source carries its own ref
    # or none at all.
    ref = given_ref or ("" if given_source else pinned_ref)
    # CI installs Autoform from a Git ref. Where Autoform lives is a fixed fact
    # worth defaulting; which commit is not, and a guessed one publishes a
    # project whose first CI step fails for a reason no file in it explains. So
    # the ref alone decides: without one the workflows are skipped and reported.
    unpinned = not ref
    planned, omitted = _scaffold_plan(
        templates,
        title=title.strip(),
        repository_url=repository_url.strip(),
        autoform_source=source,
        autoform_ref=ref,
    )

    written: list[str] = []
    skipped = list(omitted)
    for planned_file in planned:
        destination = root / planned_file.relative
        # Confine every write, not just the root. Reject links outright before
        # checking whether the destination should be skipped: `exists()` is
        # false for a dangling symlink, but opening that path still follows the
        # link and can create a file outside the project.
        probe = root
        for part in Path(planned_file.relative).parts:
            probe = probe / part
            if probe.is_symlink() or (probe.exists() and not _within(probe, root)):
                raise ScaffoldError(
                    [f"refusing to write outside the project through a link: {probe}"]
                )
        if destination.exists() and not force:
            if planned_file.relative == ".gitignore":
                if _append_gitignore_rules(destination, planned_file.content):
                    written.append(planned_file.relative)
                    continue
            skipped.append(planned_file.relative)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(destination, planned_file.content, mode=planned_file.mode)
        written.append(planned_file.relative)
    # Report omitted workflows and files left alone in template order, as
    # they were before the plan was shared with `project new`.
    order = {_destination(relative): index for index, (relative, _content, _mode) in enumerate(templates)}
    skipped.sort(key=order.__getitem__)

    return ScaffoldResult(title.strip(), tuple(written), tuple(skipped), unpinned)


__all__ = [
    "DEFAULT_AUTOFORM_SOURCE",
    "ScaffoldError",
    "ScaffoldResult",
    "plugin_pin",
    "scaffold_project",
]
