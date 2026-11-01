"""Read the Git repository a Lean root lives in.

Render links code to the web only when every Lean file it captured matches the
one commit its ref names (``render --ref``, else ``detect_ref``); a checkout
that Git converts, such as an LFS file or a line-end attribute, still matches.
``verify_repository_snapshot`` resolves that ref and checks the files, and one
file that does not match drops every code link. ``detect_repository_url`` and
``detect_ref`` read GitHub Actions' ``GITHUB_REPOSITORY`` and ``GITHUB_SHA``
when the root lies in ``GITHUB_WORKSPACE``, and ask Git when the root is
outside it or the variable is empty; ``repository_top_level`` places the root
in its working tree. Every Git command here runs without the caller's ``GIT_*``
variables, so the given directory alone selects the repository; the debrief
store, ``work import-impact`` and ``autoform --version`` run ``git`` too.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def verify_repository_snapshot(
    root: str | Path,
    requested_ref: str,
    files: Iterable[tuple[Path, bytes]],
    *,
    required_directories: Iterable[Path] = (),
    reasons: list[str] | None = None,
    spellings: dict[str, str] | None = None,
) -> str | None:
    """Return the commit whose tree holds every captured file, byte for byte.

    A clean checkout that Git converts on the way out (LFS, eol attributes) is
    accepted by hashing the captured bytes through Git's clean filter. Otherwise
    ``None``, with the first reason appended to *reasons*, naming paths relative
    to the repository only. The shared Git runner strips ambient overrides.
    Given *spellings*, a file Git committed under another spelling of its name
    is accepted too, and its committed path recorded there (``_git_tree_oids``).

    *root* may be any directory of the working tree: files are compared at their
    paths from its top level.
    """

    def refuse(reason: str) -> None:
        if reasons is not None:
            reasons.append(reason)

    root = Path(root).resolve()
    commit = git(root, "rev-parse", "--verify", "--end-of-options", f"{requested_ref}^{{commit}}")
    algorithm = git(root, "rev-parse", "--show-object-format")
    if commit is None or algorithm not in {"sha1", "sha256"}:
        return refuse(f"{requested_ref!r} does not name a commit in a local Git repository")
    top_level = repository_top_level(root)  # where tree paths start
    if top_level is None:
        return refuse(
            "the Lean root is outside its Git repository's working tree, or spelled other than as Git spells it"
        )
    root = top_level
    captured: dict[str, bytes] = {}
    try:
        for path, data in files:
            relative = path.relative_to(root).as_posix()
            if captured.setdefault(relative, data) != data:
                return refuse(f"two captured files claim {relative}")
        directories = [path.relative_to(root).as_posix() for path in required_directories]
    except ValueError:
        return refuse("a captured file or directory is outside the repository")
    blobs = _git_tree_oids(root, commit, tuple(sorted(captured)), spellings)
    if blobs is None:
        return refuse("the commit's tree could not be read")
    for path, data in captured.items():
        if path not in blobs:
            return refuse(f"{path} is not a file in commit {commit[:12]}")
        framed = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
        if hashlib.new(algorithm, framed).hexdigest() != blobs[path] and (
            _git_clean_blob_id(root, path, data) != blobs[path]
        ):
            return refuse(f"{path} differs from commit {commit[:12]}")
    if any(git(root, "cat-file", "-t", f"{commit}:{directory}") != "tree" for directory in directories):
        return refuse(f"a required directory is not in commit {commit[:12]}")
    return commit


def _git_clean_blob_id(root: Path, path: str, data: bytes) -> str | None:
    """Hash captured bytes as Git would store them at *path* (clean filter, eol)."""

    try:
        result = subprocess.run(
            ["git", "hash-object", "--stdin", f"--path={path}"],
            cwd=str(root),
            input=data,
            capture_output=True,
            env=_git_environment(),
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.decode("ascii", "replace").strip()
    return output if result.returncode == 0 and output else None


def _git_tree_oids(
    root: Path,
    commit: str,
    paths: tuple[str, ...],
    spellings: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Read raw blob IDs of regular files at exact paths without Git's quoting layer.

    macOS Git with ``core.precomposeUnicode`` finds a name decomposed on disk
    under the composed spelling it committed, which is not always NFC, and
    prints that spelling. Given *spellings*, such a record is matched to the
    path asked for by NFC form, and the committed path recorded under it.
    """

    result: dict[str, str] = {}
    for start in range(0, len(paths), 128):
        batch = paths[start : start + 128]
        if not batch:
            continue
        try:
            completed = subprocess.run(
                [
                    "git",
                    "ls-tree",
                    "-rz",
                    "--full-tree",
                    commit,
                    "--",
                    *(f":(literal){path}" for path in batch),
                ],
                cwd=str(root),
                capture_output=True,
                env=_git_environment(),
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        asked = set(batch)
        composed = {} if spellings is None else {unicodedata.normalize("NFC", path): path for path in batch}
        for record in completed.stdout.split(b"\0"):
            if not record:
                continue
            header, separator, encoded_path = record.partition(b"\t")
            fields = header.split()
            if not separator or len(fields) != 3 or fields[0] not in {b"100644", b"100755"}:
                continue
            printed = os.fsdecode(encoded_path)
            path = printed if printed in asked else composed.get(unicodedata.normalize("NFC", printed), printed)
            result[path] = fields[2].decode("ascii")
            if spellings is not None and path != printed:
                spellings[path] = printed
    return result


def detect_repository_url(root: str | Path) -> str | None:
    """Find the project's web URL from the CI environment or the git remote."""
    repository = (
        os.environ.get("GITHUB_REPOSITORY")
        if _is_github_workspace(root)
        else None
    )
    if repository:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server.rstrip('/')}/{repository}"
    remote = git(root, "config", "--local", "--get", "remote.origin.url")
    return _normalize_remote(remote) if remote else None


def repository_top_level(root: str | Path) -> Path | None:
    """The top level of *root*'s Git working tree, spelled as *root* is.

    *root*'s last parts must spell Git's path to it, compared in NFC, since
    macOS Git composes names; otherwise, or outside a working tree, the top
    level is unknown and this is ``None``. *root* should be resolved, as
    ``lean.resolve_lean_root`` gives it.
    """
    try:
        output = git(root, "rev-parse", "--is-inside-work-tree", "--show-prefix")
    except ValueError:  # Git printed a name that is not UTF-8
        return None
    lines = output.splitlines() if output is not None else []
    if not lines or lines[0] != "true":
        return None
    parts = lines[1].rstrip("/").split("/") if len(lines) > 1 else []
    if not parts:
        return Path(root)
    spelled = Path(root).parts[-len(parts) :]
    if [unicodedata.normalize("NFC", part) for part in spelled] != [
        unicodedata.normalize("NFC", part) for part in parts
    ]:
        return None
    return Path(root).parents[len(parts) - 1]


def detect_ref(root: str | Path) -> str | None:
    """Prefer the exact commit so links keep pointing at the reviewed code."""
    github_sha = os.environ.get("GITHUB_SHA") if _is_github_workspace(root) else None
    return github_sha or git(root, "rev-parse", "HEAD")


def _is_github_workspace(root: str | Path) -> bool:
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if not workspace:
        return False
    try:
        resolved_root = Path(root).resolve()
        resolved_workspace = Path(workspace).resolve()
    except (OSError, RuntimeError):
        return False
    return resolved_root == resolved_workspace or resolved_root.is_relative_to(
        resolved_workspace
    )


def _normalize_remote(remote: str) -> str | None:
    remote = remote.strip()
    if remote.startswith("git@"):
        host, _, path = remote[4:].partition(":")
        if not path:
            return None
        remote = f"https://{host}/{path}"
    try:
        parsed = urlsplit(remote)
    except ValueError:
        return None
    if parsed.scheme == "ssh" and parsed.username == "git" and parsed.hostname:
        host = (
            f"[{parsed.hostname}]"
            if ":" in parsed.hostname
            else parsed.hostname
        )
        try:
            port = parsed.port
        except ValueError:
            return None
        remote = f"https://{host}{f':{port}' if port is not None else ''}{parsed.path}"
        try:
            parsed = urlsplit(remote)
        except ValueError:
            return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not path or path == "/":
        return None
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def git(root: str | Path, *arguments: str, environment: Mapping[str, str] | None = None) -> str | None:
    """Run Git in ``root``; ``environment`` adds variables to the stripped environment."""

    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(root),
            capture_output=True,
            env={**_git_environment(), **(environment or {})},
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


def _git_environment() -> dict[str, str]:
    """Make the explicit working directory the only Git repository selector."""

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


__all__ = [
    "detect_ref",
    "detect_repository_url",
    "git",
    "repository_top_level",
    "verify_repository_snapshot",
]
