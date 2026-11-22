"""Git operations of the work loop: fetching the base branch, worker worktrees, commits and guarded pushes.

The harness is the only writer to a remote. A push names an explicit refspec,
leases the branch on the value it last saw (create-only for a new branch),
refuses the base and default branches and any branch outside the configured
prefix, and checks afterwards that the remote holds exactly what was pushed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .. import lean_probe

# A hunk header of `git diff -U0`: the new side's first line and line count.
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_GITHUB_URL = re.compile(r"(?:github\.com[:/])([^/:\s]+)/([^/\s]+?)(?:\.git)?/?$")
BASE_REF_PREFIX = "refs/autoform-work/base/"


class GitError(RuntimeError):
    pass


def run_command(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 600,
    output_limit: int = 64 * 1024 * 1024,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a program other than Git through Autoform's bounded runner (time, output and every descendant).

    ``check`` raises :class:`GitError` naming the output's tail on a nonzero exit status; a time limit, an
    output limit or a termination signal always raises it.
    """

    argv = [os.fspath(part) for part in command]
    try:
        result = lean_probe.run_command(argv, cwd=cwd, timeout=timeout, env=None if env is None else dict(env),
                                        output_limit=output_limit)
    except (lean_probe.SkeletonError, OSError) as error:
        raise GitError(f"{' '.join(argv)[:200]}: {error}") from error
    if check and result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-3000:]
        raise GitError(f"{' '.join(argv)[:200]} failed with exit status {result.returncode}: {tail}")
    return result


def git(
    cwd: Path | None,
    *args: str,
    check: bool = True,
    timeout: float | None = 600,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Git; ``check`` raises :class:`GitError` naming its output's tail."""

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=None if env is None else dict(env),
            input=input_text,
            text=True,
            errors="replace",
            capture_output=True,
            timeout=timeout,
            stdin=None if input_text is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise GitError(f"git {' '.join(args)[:200]} ran out of time after {timeout}s") from error
    except OSError as error:
        raise GitError(f"cannot run git: {error}") from error
    if check and result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-3000:]
        raise GitError(f"git {' '.join(args)[:200]} failed with exit status {result.returncode}: {tail}")
    return result


def network_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def toplevel(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").stdout.strip())


def github_repository(url: str) -> str | None:
    """``owner/name`` of a GitHub remote URL (HTTPS or SSH), else ``None``."""

    match = _GITHUB_URL.search(url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def remote_url(repository: str) -> str:
    # AUTOFORM_WORK_GIT_BASE replaces https://github.com/ (tests point it at local bare repositories).
    base = os.environ.get("AUTOFORM_WORK_GIT_BASE", "https://github.com/")
    return f"{base.rstrip('/')}/{repository}.git"


def fetch_branch(checkout: Path, repository: str, branch: str, *, env: Mapping[str, str] | None = None) -> str:
    """Fetch ``branch`` of ``repository`` into ``refs/autoform-work/base/<branch>`` and return its commit.

    The loop keeps its own ref, so it never moves the checkout's branches or remotes.
    """

    ref = BASE_REF_PREFIX + branch
    git(
        checkout, "fetch", "--quiet", "--no-tags", remote_url(repository), f"+refs/heads/{branch}:{ref}",
        env=network_env(env), timeout=1800,
    )
    return git(checkout, "rev-parse", ref).stdout.strip()


def fetch_commit(checkout: Path, url: str, ref: str, *, env: Mapping[str, str] | None = None) -> str:
    """Fetch one branch of ``url`` into ``refs/autoform-work/heads/<ref>`` and return its commit."""

    local = "refs/autoform-work/heads/" + ref
    git(checkout, "fetch", "--quiet", "--no-tags", url, f"+refs/heads/{ref}:{local}", env=network_env(env), timeout=1800)
    return git(checkout, "rev-parse", local).stdout.strip()


def is_worktree_of(path: Path, checkout: Path) -> bool:
    if not (path / ".git").exists():
        return False
    mine = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    theirs = git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    return mine.returncode == 0 and mine.stdout.strip() == theirs.stdout.strip()


def ensure_worktree(checkout: Path, path: Path, start: str) -> Path:
    """A detached worktree of ``checkout`` at ``path``, created at ``start`` when it does not exist yet."""

    if is_worktree_of(path, checkout):
        return path
    if path.exists():
        raise GitError(f"{path} exists and is not a worktree of {checkout}; remove it or choose another --state-dir")
    path.parent.mkdir(parents=True, exist_ok=True)
    git(checkout, "worktree", "prune", check=False)
    git(checkout, "worktree", "add", "--quiet", "--detach", str(path), start, timeout=3600)
    return path


def reset_worktree(path: Path, commit: str) -> None:
    """Throw away everything in the worktree but ``.lake`` and check out ``commit`` detached."""

    git(path, "merge", "--abort", check=False)
    git(path, "checkout", "--quiet", "--force", "--detach", commit, timeout=3600)
    git(path, "reset", "--quiet", "--hard", commit, timeout=3600)
    git(path, "clean", "-ffdxq", "-e", ".lake", timeout=3600)


def _copy_tree(source: Path, target: Path) -> None:
    """Copy a directory, sharing blocks where the filesystem can (reflink on Linux, clonefile on macOS)."""

    if sys.platform == "darwin":
        command = ["cp", "-cR", str(source), str(target)]
    else:
        command = ["cp", "-a", "--reflink=auto", str(source), str(target)]
    try:
        copied = run_command(command, timeout=7200).returncode == 0
    except GitError:
        copied = False
    if not copied:
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target, symlinks=True)


def prepare_lake(worktree: Path, checkout: Path) -> list[str]:
    """Give a worktree its own copy of the checkout's Lake packages and build; return what it did.

    The copies share blocks with the checkout where the filesystem can (reflink on Linux, clonefile on
    macOS), so they cost little there; they are copies, never links, as the Formalize skill's leaf procedure
    asks, so no worker's Lake can change the checkout's packages. The packages are copied again when the
    worktree's manifest stops matching the copy, and the build only once, so later builds are incremental.
    """

    done: list[str] = []
    lake = worktree / ".lake"
    packages = lake / "packages"
    stamp = lake / "autoform-work-packages.json"
    source_packages = checkout / ".lake" / "packages"
    manifest = worktree / "lake-manifest.json"
    same = _same_file(manifest, checkout / "lake-manifest.json")
    if packages.is_symlink():
        packages.unlink()
        done.append("removed a linked .lake/packages")
    copied_for = stamp.read_bytes() if stamp.is_file() else None
    current = manifest.read_bytes() if manifest.is_file() else b""
    if same and source_packages.is_dir() and (not packages.exists() or copied_for != current):
        lake.mkdir(exist_ok=True)
        shutil.rmtree(packages, ignore_errors=True)
        _copy_tree(source_packages, packages)
        stamp.write_bytes(current)
        done.append("copied the checkout's .lake/packages")
    build, source_build = lake / "build", checkout / ".lake" / "build"
    if not build.exists() and source_build.is_dir():
        lake.mkdir(exist_ok=True)
        _copy_tree(source_build, build)
        done.append("copied the checkout's .lake/build")
    return done


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def merge_base(path: Path, a: str, b: str) -> str:
    return git(path, "merge-base", a, b).stdout.strip()


def head(path: Path) -> str:
    return git(path, "rev-parse", "HEAD").stdout.strip()


# Plain diff output whatever the user's configuration says: no colour, no external diff driver or text conversion.
_PLAIN_DIFF = ("-c", "color.ui=false", "-c", "color.diff=false", "-c", "diff.external=", "-c", "core.quotePath=false")


# The lines of a diff that carry no content: a mode-only or binary change has nothing else.
_HEADER_LINES = ("diff --git ", "index ", "old mode ", "new mode ", "deleted file mode ", "new file mode ",
                 "similarity index ", "--- ", "+++ ", "Binary files ")


def changed_files(path: Path, base: str) -> list[tuple[str, str]]:
    """``(status, path)`` of every change from ``base`` to the working tree, untracked files included."""

    git(path, "add", "--all")
    # NUL-separated, so no path is quoted or escaped, whatever its characters.
    out = git(path, *_PLAIN_DIFF, "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv",
              "--name-status", "--no-renames", "-z", base).stdout
    fields = out.split("\0")
    rows: list[tuple[str, str]] = []
    for status, name in zip(fields[0::2], fields[1::2]):
        if status and name:
            rows.append((status.strip(), name))
    return rows


def added_lines(path: Path, base: str, name: str) -> set[int]:
    """Line numbers (from 1) of ``name`` in the index that are new relative to ``base``."""

    # --text: a `-diff` or `binary` attribute must not turn a Lean file's lines into "Binary files differ".
    diff = git(path, *_PLAIN_DIFF, "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "--text", "-U0",
               "--no-renames", base, "--", name).stdout
    lines: set[int] = set()
    hunks = 0
    for match in _HUNK.finditer(diff):
        hunks += 1
        start, count = int(match.group(1)), int(match.group(2) or 1)
        lines.update(range(start, start + count))
    if not hunks:
        # No hunk is only right when the content did not change (a mode-only change, say).
        before = git(path, "rev-parse", "-q", "--verify", f"{base}:{name}", check=False).stdout.strip()
        after = git(path, "rev-parse", "-q", "--verify", f":{name}", check=False).stdout.strip()
        if before != after and after:
            raise GitError(f"cannot read the diff of {name}: its content changed but Git gave no hunk")
    return lines


def tree_of_worktree(path: Path) -> str:
    """The tree object of the worktree's files as they are, untracked files included."""

    git(path, "add", "--all")
    return git(path, "write-tree").stdout.strip()


def export_tree(repository: Path, commit: str, directory: str, destination: Path) -> Path:
    """Write ``directory`` (``""``: everything) as it is in ``commit``'s tree under ``destination``, replacing what
    was there; return ``destination``. The blobs are written as stored: no attribute, filter or archive rule
    applies."""

    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    listing = git(repository, "ls-tree", "-r", "-z", "--full-tree", commit, *(["--", directory] if directory else []))
    for entry in listing.stdout.split("\0"):
        if not entry:
            continue
        meta, _, name = entry.partition("\t")
        mode, kind, blob = meta.split()
        target = destination / name
        if kind != "blob" or not target.resolve().is_relative_to(destination.resolve()):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = subprocess.run(["git", "cat-file", "blob", blob], cwd=repository, capture_output=True,
                                  timeout=600, check=True).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise GitError(f"git cat-file {blob}: {error}") from error
        if mode == "120000":
            target.symlink_to(data.decode("utf-8", errors="replace"))
            continue
        target.write_bytes(data)
        if mode == "100755":
            target.chmod(0o755)
    return destination


def identity(checkout: Path) -> tuple[str, str]:
    """The checkout's configured ``user.name`` and ``user.email``, which commits are made as."""

    name = git(checkout, "config", "user.name", check=False).stdout.strip()
    email = git(checkout, "config", "user.email", check=False).stdout.strip()
    if not name or not email:
        raise GitError("set git's user.name and user.email: the loop commits as you")
    return name, email


def commit(path: Path, message: str, *, author: tuple[str, str], tree: str | None = None) -> str:
    """Commit the worktree, or exactly ``tree`` when given (the tree that was verified), on top of HEAD and of
    a merge in progress; return the commit."""

    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1], GIT_COMMITTER_NAME=author[0], GIT_COMMITTER_EMAIL=author[1]
    )
    git(path, "add", "--all")
    if tree is None:
        tree = git(path, "write-tree").stdout.strip()
    parents = ["-p", head(path)]
    merging = git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).stdout.strip()
    if merging:
        parents += ["-p", merging]
    made = git(path, "commit-tree", tree, *parents, "-F", "-", env=env, input_text=message).stdout.strip()
    if merging:
        git(path, "merge", "--quit")
    git(path, "reset", "--quiet", "--soft", made)
    return made


def check_branch_name(branch: str, *, prefix: str, protected: Sequence[str]) -> None:
    """Refuse a branch outside ``prefix``, a protected one, or a name Git would refuse."""

    if not branch.startswith(prefix) or branch == prefix:
        raise GitError(f"refusing to push {branch!r}: the loop pushes only branches under {prefix!r}")
    if branch in protected:
        raise GitError(f"refusing to push the protected branch {branch!r}")
    result = git(None, "check-ref-format", f"refs/heads/{branch}", check=False)
    if result.returncode != 0:
        raise GitError(f"refusing to push {branch!r}: not a valid branch name")


def credential_args(gh: str) -> list[str]:
    """Git options that take HTTPS credentials from ``gh`` and from nothing else."""

    return ["-c", "credential.helper=", "-c", f"credential.helper=!{gh} auth git-credential"]


def credential_env(env: Mapping[str, str], gh: str) -> dict[str, str]:
    """``GIT_CONFIG_*`` variables that add ``gh`` as git's HTTPS credential helper, after any already set."""

    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    return {
        f"GIT_CONFIG_KEY_{count}": "credential.helper",
        f"GIT_CONFIG_VALUE_{count}": "",
        f"GIT_CONFIG_KEY_{count + 1}": "credential.helper",
        f"GIT_CONFIG_VALUE_{count + 1}": f"!{gh} auth git-credential",
        "GIT_CONFIG_COUNT": str(count + 2),
    }


def remote_branch(url: str, branch: str, *, gh: str, env: Mapping[str, str] | None = None) -> str | None:
    out = git(None, *credential_args(gh), "ls-remote", url, f"refs/heads/{branch}", env=network_env(env)).stdout.split()
    return out[0] if out else None


def safe_push(
    path: Path,
    *,
    url: str,
    commit_sha: str,
    branch: str,
    old: str | None,
    prefix: str,
    protected: Sequence[str],
    gh: str,
    env: Mapping[str, str] | None = None,
) -> None:
    """Push ``commit_sha`` to ``refs/heads/<branch>`` of ``url`` only, leased on ``old`` (``None``: create).

    An update must descend from ``old``, so a repair can only add commits, and no tag goes with the push.
    """

    check_branch_name(branch, prefix=prefix, protected=protected)
    if old is not None and git(path, "merge-base", "--is-ancestor", old, commit_sha, check=False).returncode != 0:
        raise GitError(f"refusing to push {branch}: {commit_sha} does not descend from its head {old}")
    lease = f"--force-with-lease=refs/heads/{branch}:{old or ''}"
    git(path, *credential_args(gh), "-c", "push.followTags=false", "push", "--quiet", "--no-verify",
        "--no-follow-tags", lease, url, f"{commit_sha}:refs/heads/{branch}", env=network_env(env), timeout=3600)
    now = remote_branch(url, branch, gh=gh, env=env)
    if now != commit_sha:
        raise GitError(f"after pushing {branch}, the remote holds {now}, not {commit_sha}")
