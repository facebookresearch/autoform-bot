"""Fetch the scaffold template subtree from one immutable Git commit."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

from .provenance import normalize_git_source


TemplateSnapshot = tuple[tuple[str, bytes, int], ...]

_PREFIX = "autoform_cli/templates/"
_MAX_ENTRIES = 1_024
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_LIST_BYTES = 1024 * 1024
_MAX_DEPTH = 32
_FULL_SHA_LENGTH = 40
_DOTTED_PREFIXES = {
    "gitignore": ".gitignore",
    "blueprint/gitignore": "blueprint/.gitignore",
    "github": ".github",
}
_REQUIRED_TEMPLATES = frozenset(
    {
        "README.md",
        "blueprint/README.md",
        "blueprint/coverage/README.md",
        "blueprint/gitignore",
        "blueprint/javascripts/mathjax.js",
        "blueprint/roadmap/README.md",
        "blueprint/sources/README.md",
        "github/autoform_audit.py",
        "github/workflows/autoform-verify.yml",
        "github/workflows/blueprint-pages.yml",
        "gitignore",
        "mkdocs.yml",
        "theme/main.html",
    }
)


class TemplateSnapshotError(ValueError):
    """The recorded commit's scaffold templates could not be read safely."""


def _destination(relative: str) -> str:
    for template_prefix, real_prefix in _DOTTED_PREFIXES.items():
        if relative == template_prefix:
            return real_prefix
        if relative.startswith(f"{template_prefix}/"):
            return real_prefix + relative[len(template_prefix) :]
    return relative


def _git_environment(home: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GCM_INTERACTIVE": "never",
            "GIT_ASKPASS": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": os.fspath(home),
            "LC_ALL": "C",
            "NETRC": os.devnull,
            "USERPROFILE": os.fspath(home),
            "XDG_CONFIG_HOME": os.fspath(home),
        }
    )
    return environment


def _run_git(
    arguments: list[str],
    *,
    cwd: Path,
    home: Path,
    deadline: float,
    input_bytes: bytes | None = None,
    max_output: int,
) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TemplateSnapshotError("Fetching Autoform templates timed out.")
    options: dict[str, object] = {}
    if os.name == "posix":
        options["start_new_session"] = True
    process: subprocess.Popen[bytes] | None = None
    request: object | None = None
    output_file: object | None = None

    def stop() -> None:
        if process is None or process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait()

    try:
        if input_bytes is not None:
            request = tempfile.TemporaryFile()
            request.write(input_bytes)
            request.seek(0)
        output_file = tempfile.TemporaryFile()
        process = subprocess.Popen(
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                *arguments,
            ],
            cwd=cwd,
            env=_git_environment(home),
            stdin=request if request is not None else subprocess.DEVNULL,
            stdout=output_file,
            stderr=subprocess.DEVNULL,
            **options,
        )
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop()
                raise TemplateSnapshotError("Fetching Autoform templates timed out.")
            if os.fstat(output_file.fileno()).st_size > max_output:
                stop()
                raise TemplateSnapshotError("The recorded Autoform templates are too large.")
            time.sleep(min(0.01, remaining))
        size = os.fstat(output_file.fileno()).st_size
        if size > max_output:
            raise TemplateSnapshotError("The recorded Autoform templates are too large.")
        output_file.seek(0)
        output = output_file.read(max_output + 1)
    except TemplateSnapshotError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        stop()
        raise TemplateSnapshotError("The recorded Autoform templates are unavailable.") from error
    except BaseException:
        stop()
        raise
    finally:
        if request is not None:
            request.close()
        if output_file is not None:
            output_file.close()
    if process.returncode != 0:
        raise TemplateSnapshotError("The recorded Autoform templates are invalid.")
    if len(output) > max_output:
        raise TemplateSnapshotError("The recorded Autoform templates are too large.")
    return bytes(output)


def _relative_path(encoded: bytes) -> str:
    try:
        path = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise TemplateSnapshotError("The recorded Autoform template path is invalid.") from error
    if not path.startswith(_PREFIX):
        raise TemplateSnapshotError("The recorded Autoform template path is invalid.")
    relative = path.removeprefix(_PREFIX)
    parsed = PurePosixPath(relative)
    if (
        not relative
        or relative.startswith("/")
        or "\\" in relative
        or parsed.as_posix() != relative
        or len(parsed.parts) > _MAX_DEPTH
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise TemplateSnapshotError("The recorded Autoform template path is invalid.")
    return relative


def _parse_tree(listing: bytes) -> dict[str, tuple[str, int]]:
    entries: dict[str, tuple[str, int]] = {}
    folded: set[str] = set()
    destinations: set[str] = set()
    for raw in listing.split(b"\0"):
        if not raw:
            continue
        if len(entries) >= _MAX_ENTRIES:
            raise TemplateSnapshotError("The recorded Autoform template tree is too large.")
        try:
            header, encoded_path = raw.split(b"\t", 1)
            encoded_mode, kind, encoded_object = header.split(b" ", 2)
            mode = int(encoded_mode, 8)
            object_id = encoded_object.decode("ascii")
        except (UnicodeDecodeError, ValueError) as error:
            raise TemplateSnapshotError("The recorded Autoform template tree is invalid.") from error
        relative = _relative_path(encoded_path)
        destination = _destination(relative).casefold()
        if (
            kind != b"blob"
            or mode not in {0o100644, 0o100755}
            or len(object_id) != _FULL_SHA_LENGTH
            or any(character not in "0123456789abcdef" for character in object_id)
            or relative in entries
            or relative.casefold() in folded
            or destination in destinations
        ):
            raise TemplateSnapshotError("The recorded Autoform template tree is invalid.")
        entries[relative] = (object_id, mode)
        folded.add(relative.casefold())
        destinations.add(destination)
    if set(entries) != _REQUIRED_TEMPLATES:
        raise TemplateSnapshotError("The recorded Autoform template tree is not the expected surface.")
    return entries


def _parse_sizes(output: bytes, object_ids: tuple[str, ...]) -> dict[str, int]:
    lines = output.splitlines()
    if len(lines) != len(object_ids):
        raise TemplateSnapshotError("The recorded Autoform template objects are invalid.")
    sizes: dict[str, int] = {}
    for expected, line in zip(object_ids, lines, strict=True):
        fields = line.split(b" ")
        if len(fields) != 3 or fields[1] != b"blob" or not fields[2].isdigit():
            raise TemplateSnapshotError("The recorded Autoform template objects are invalid.")
        try:
            found = fields[0].decode("ascii")
        except UnicodeDecodeError as error:
            raise TemplateSnapshotError("The recorded Autoform template objects are invalid.") from error
        size = int(fields[2])
        if found != expected or size > _MAX_FILE_BYTES:
            raise TemplateSnapshotError("The recorded Autoform template objects are too large.")
        sizes[found] = size
    return sizes


def _parse_blobs(
    output: bytes,
    object_ids: tuple[str, ...],
    sizes: dict[str, int],
) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    cursor = 0
    for expected in object_ids:
        line_end = output.find(b"\n", cursor)
        if line_end < 0 or line_end - cursor > 128:
            raise TemplateSnapshotError("The recorded Autoform template objects are invalid.")
        fields = output[cursor:line_end].split(b" ")
        if len(fields) != 3 or fields[1] != b"blob" or not fields[2].isdigit():
            raise TemplateSnapshotError("The recorded Autoform template objects are invalid.")
        try:
            found = fields[0].decode("ascii")
        except UnicodeDecodeError as error:
            raise TemplateSnapshotError("The recorded Autoform template objects are invalid.") from error
        size = sizes[expected]
        start = line_end + 1
        end = start + size
        if found != expected or int(fields[2]) != size or output[end : end + 1] != b"\n":
            raise TemplateSnapshotError("The recorded Autoform template objects are invalid.")
        contents[expected] = output[start:end]
        cursor = end + 1
    if cursor != len(output):
        raise TemplateSnapshotError("The recorded Autoform template objects are invalid.")
    return contents


def _require_blobless_initial_fetch(output: bytes) -> None:
    if any(object_type == b"blob" for object_type in output.splitlines()):
        raise TemplateSnapshotError("The Git server ignored the template-only blob filter.")


def fetch_template_snapshot(source: str, revision: str) -> TemplateSnapshot:
    """Return bounded template bytes from exactly ``source`` at ``revision``."""

    if not isinstance(revision, str) or normalize_git_source(source) != source or len(
        revision
    ) != _FULL_SHA_LENGTH or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise TemplateSnapshotError("The recorded Autoform template identity is invalid.")
    deadline = time.monotonic() + 60
    with tempfile.TemporaryDirectory(prefix="autoform-templates-") as temporary:
        scratch = Path(temporary)
        repository = scratch / "repository.git"
        home = scratch / "home"
        home.mkdir(mode=0o700)
        _run_git(
            ["init", "--bare", "--template=", os.fspath(repository)],
            cwd=scratch,
            home=home,
            deadline=deadline,
            max_output=16 * 1024,
        )
        _run_git(
            ["remote", "add", "origin", source],
            cwd=repository,
            home=home,
            deadline=deadline,
            max_output=16 * 1024,
        )
        _run_git(
            ["config", "remote.origin.promisor", "true"],
            cwd=repository,
            home=home,
            deadline=deadline,
            max_output=16 * 1024,
        )
        _run_git(
            ["config", "remote.origin.partialCloneFilter", "blob:none"],
            cwd=repository,
            home=home,
            deadline=deadline,
            max_output=16 * 1024,
        )
        _run_git(
            [
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--depth=1",
                "--filter=blob:none",
                "origin",
                revision,
            ],
            cwd=repository,
            home=home,
            deadline=deadline,
            max_output=1024 * 1024,
        )
        resolved = _run_git(
            ["rev-parse", "--verify", "FETCH_HEAD^{commit}"],
            cwd=repository,
            home=home,
            deadline=deadline,
            max_output=128,
        ).decode("ascii", errors="strict").strip()
        if resolved != revision:
            raise TemplateSnapshotError("The recorded Autoform commit did not resolve exactly.")
        listing = _run_git(
            ["ls-tree", "-rz", "-r", "--full-tree", resolved, "--", _PREFIX.removesuffix("/")],
            cwd=repository,
            home=home,
            deadline=deadline,
            max_output=_MAX_LIST_BYTES,
        )
        entries = _parse_tree(listing)
        _require_blobless_initial_fetch(
            _run_git(
                ["cat-file", "--batch-all-objects", "--batch-check=%(objecttype)"],
                cwd=repository,
                home=home,
                deadline=deadline,
                max_output=_MAX_LIST_BYTES,
            )
        )
        object_ids = tuple(sorted({object_id for object_id, _ in entries.values()}))
        request = b"".join(f"{object_id}\n".encode("ascii") for object_id in object_ids)
        _run_git(
            [
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                "--recurse-submodules=no",
                "--filter=blob:none",
                "--stdin",
                "origin",
            ],
            cwd=repository,
            home=home,
            deadline=deadline,
            input_bytes=request,
            max_output=1024 * 1024,
        )
        sizes = _parse_sizes(
            _run_git(
                ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                cwd=repository,
                home=home,
                deadline=deadline,
                input_bytes=request,
                max_output=_MAX_LIST_BYTES,
            ),
            object_ids,
        )
        total = sum(sizes[object_id] for object_id, _ in entries.values())
        if total > _MAX_TOTAL_BYTES:
            raise TemplateSnapshotError("The recorded Autoform template tree is too large.")
        blobs = _parse_blobs(
            _run_git(
                ["cat-file", "--batch"],
                cwd=repository,
                home=home,
                deadline=deadline,
                input_bytes=request,
                max_output=_MAX_TOTAL_BYTES + _MAX_LIST_BYTES,
            ),
            object_ids,
            sizes,
        )
    snapshot: list[tuple[str, bytes, int]] = []
    for relative, (object_id, mode) in sorted(entries.items()):
        content = blobs[object_id]
        if Path(relative).suffix not in {".js", ".html"} and not relative.endswith("gitignore"):
            try:
                content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise TemplateSnapshotError(
                    "The recorded Autoform template content is invalid."
                ) from error
        snapshot.append((relative, content, stat.S_IMODE(mode)))
    return tuple(snapshot)


__all__ = ["TemplateSnapshot", "TemplateSnapshotError", "fetch_template_snapshot"]
