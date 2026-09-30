"""Who approved a statement review, as opposed to whether it is current.

``review_approved`` records the hash of the complete review surface. Matching
the current hash shows only that nothing changed since someone wrote it down;
``autoform review check`` prints that hash, so anyone can paste it. This module
keeps the second property separate: an approval is authenticated only when a
verifier finds evidence that an allowed human approved that exact hash.
Everything else is shown as self-approved.

The first verifier reads GitHub pull request reviews. Signed approvals are
meant to plug into the same ``ApprovalVerifier`` slot.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .graph import Graph, frontmatter_value
from .readback import Readback
from .review import ReviewBundle, ReviewError


DEFAULT_GITHUB_API_URL = "https://api.github.com"
GITHUB_REVIEW_METHOD = "github-review"
CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
SELF_APPROVED = "self-approved"
_PAGE_SIZE = 100
_MAX_PAGES = 30
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_INTRODUCING_COMMITS = 50
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_USER_OWNER = re.compile(r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\Z")
_TEAM_OWNER = re.compile(r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+\Z")
_EMAIL_OWNER = re.compile(r"[^@\s]+@[^@\s]+\Z")
_UNSUPPORTED_PATTERN = ("!", "[", "\\")


class ApprovalError(ValueError):
    """Approval evidence could not be gathered or its rules could not be read."""

    def __init__(self, message: str) -> None:
        self.issues = (message,)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ApprovalAttestation:
    """Evidence that ``reviewer`` approved exactly ``review_hash`` for one article."""

    node_id: str
    review_hash: str
    reviewer: str
    method: str
    reference: str


class ApprovalVerifier(Protocol):
    """Authenticates recorded approvals from evidence outside the blueprint.

    ``verify`` receives node ids mapped to the hashes their articles record and
    returns an attestation for each approval it authenticates, naming that
    exact hash. An approval it cannot authenticate is simply absent. A verifier
    may also expose ``reasons``, node ids mapped to why they were not
    authenticated, which callers show next to the self-approved label.
    """

    method: str

    def verify(self, graph: Graph, approvals: Mapping[str, str]) -> dict[str, ApprovalAttestation]: ...


@dataclass(frozen=True, slots=True)
class ApprovalStatus:
    """One current approval and, when a verifier found it, who made it."""

    node_id: str
    review_hash: str
    attestation: ApprovalAttestation | None = None
    reason: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.attestation is not None

    @property
    def label(self) -> str:
        if self.attestation is None:
            return SELF_APPROVED
        return f"approved by @{self.attestation.reviewer} ({self.attestation.method.replace('-', ' ')})"


def approval_statuses(
    graph: Graph,
    approvals: Mapping[str, str],
    verifier: ApprovalVerifier | None = None,
) -> dict[str, ApprovalStatus]:
    """Label each approval authenticated or self-approved.

    Without a verifier nothing is authenticated and no network is used. An
    attestation that names another article or hash is discarded, so a faulty
    verifier can only make a label weaker.
    """

    attestations = verifier.verify(graph, approvals) if verifier is not None and approvals else {}
    reasons: Mapping[str, str] = getattr(verifier, "reasons", {}) if verifier is not None else {}
    statuses: dict[str, ApprovalStatus] = {}
    for node_id, review_hash in sorted(approvals.items()):
        attestation = attestations.get(node_id)
        if attestation is not None and (attestation.node_id, attestation.review_hash) != (node_id, review_hash):
            statuses[node_id] = ApprovalStatus(
                node_id, review_hash, reason="the verifier's evidence names a different article or hash"
            )
        elif attestation is not None:
            statuses[node_id] = ApprovalStatus(node_id, review_hash, attestation)
        elif verifier is None:
            statuses[node_id] = ApprovalStatus(node_id, review_hash, reason="no approval verifier was run")
        else:
            statuses[node_id] = ApprovalStatus(
                node_id, review_hash, reason=reasons.get(node_id, f"{verifier.method} found no evidence")
            )
    return statuses


def current_approvals(
    graph: Graph,
    bundle: ReviewBundle,
    readbacks: Mapping[tuple[str, str], Readback],
) -> dict[str, str]:
    """Map each article whose ``review_approved`` equals its current review hash to that hash.

    ``bundle`` must already be validated against ``graph``, and ``readbacks``
    are the cards it was checked with. Articles with incomplete testimony or a
    stale hash are left out; ``review check`` reports those separately.
    """

    approvals: dict[str, str] = {}
    for node in graph.nodes.values():
        if node.review_approved is None or node.article_id is None or bundle.article(node.article_id) is None:
            continue
        try:
            expected = bundle.review_hash(node.article_id, readbacks)
        except ReviewError:
            continue
        if node.review_approved == expected:
            approvals[node.id] = expected
    return approvals


def approvals_at(graph: Graph, ref: str, node_ids: list[str] | None = None) -> dict[str, str]:
    """The ``review_approved`` each article's path recorded at ``ref``.

    Articles are matched by path, so a moved article has no approval at
    ``ref`` and its approval counts as new.
    """

    root = repository_root(graph.blueprint_dir)
    _require_commit(root, ref)
    recorded: dict[str, str] = {}
    for node_id in sorted(graph.nodes if node_ids is None else node_ids):
        text = _show(root, ref, _relative_path(graph.nodes[node_id].path, root))
        value = None if text is None else frontmatter_value(text, "review_approved")
        if value is not None:
            recorded[node_id] = value
    return recorded


# CODEOWNERS


@dataclass(frozen=True, slots=True)
class CodeOwnersRule:
    """One CODEOWNERS line: a path pattern and the owners it assigns."""

    line: int
    pattern: str
    owners: tuple[str, ...]
    regex: re.Pattern[str]

    def matches(self, path: str) -> bool:
        return self.regex.fullmatch(path) is not None


def parse_codeowners(text: str, *, source: str = "CODEOWNERS") -> tuple[CodeOwnersRule, ...]:
    """Parse the subset of CODEOWNERS whose meaning is unambiguous.

    Negation, character classes, and escapes are refused rather than guessed,
    because a misread rule could let the wrong person approve.
    """

    rules: list[CodeOwnersRule] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        tokens = raw.split()
        if not tokens or tokens[0].startswith("#"):
            continue
        pattern, owners = tokens[0], []
        regex = _pattern_regex(pattern, f"{source}:{number}")
        for token in tokens[1:]:
            if token.startswith("#"):
                break
            if not (_USER_OWNER.match(token) or _TEAM_OWNER.match(token) or _EMAIL_OWNER.match(token)):
                raise ApprovalError(f"{source}:{number}: unsupported owner {token!r}; use @user, @org/team, or an email")
            owners.append(token)
        rules.append(CodeOwnersRule(number, pattern, tuple(owners), regex))
    return tuple(rules)


def code_owners(rules: tuple[CodeOwnersRule, ...], path: str) -> tuple[str, ...]:
    """The owners of a repository-relative POSIX path; the last matching rule wins."""

    for rule in reversed(rules):
        if rule.matches(path):
            return rule.owners
    return ()


def individual_owners(owners: tuple[str, ...]) -> tuple[str, ...]:
    """The ``@user`` owners, without the ``@``. Teams and emails never authenticate."""

    return tuple(owner[1:] for owner in owners if _USER_OWNER.match(owner))


def _pattern_regex(pattern: str, location: str) -> re.Pattern[str]:
    if any(character in pattern for character in _UNSUPPORTED_PATTERN):
        raise ApprovalError(
            f"{location}: unsupported CODEOWNERS pattern {pattern!r}; negation, character classes, "
            "and escapes are refused so ownership is never guessed"
        )
    directory = pattern.endswith("/")
    body = pattern.strip("/")
    # gitignore rule: a slash anywhere but the end anchors to the repository root.
    anchored = pattern.startswith("/") or "/" in body
    segments = body.split("/")
    if not body or any(not segment for segment in segments):
        raise ApprovalError(f"{location}: malformed CODEOWNERS pattern {pattern!r}")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            parts.append(".*" if last else "(?:[^/]+/)*")
            continue
        translated = "".join(
            "[^/]*" if character == "*" else "[^/]" if character == "?" else re.escape(character)
            for character in segment
        )
        parts.append(translated if last else translated + "/")
    expression = "".join(parts)
    if not anchored:
        expression = "(?:[^/]+/)*" + expression
    if directory:
        expression += "/.+"
    elif segments[-1] != "**" and not any(character in segments[-1] for character in "*?"):
        # A literal final name may be a file or a directory. GitHub documents
        # that a wildcard final segment such as docs/* matches only direct
        # children, so those patterns get no descendant suffix.
        expression += "(?:/.+)?"
    return re.compile(expression)


def load_codeowners(root: Path, ref: str) -> tuple[str, tuple[CodeOwnersRule, ...]] | None:
    """Read the CODEOWNERS file GitHub would use at ``ref``, or None when there is none."""

    _require_commit(root, ref)
    for location in CODEOWNERS_LOCATIONS:
        text = _show(root, ref, location)
        if text is not None:
            return location, parse_codeowners(text, source=f"{ref}:{location}")
    return None


# GitHub


class GitHubClient:
    """A minimal read-only GitHub REST client over the standard library.

    Paths are relative to ``/repos/{repository}``. A 404 is returned as None,
    meaning no evidence; every other failure raises ``ApprovalError``. The
    token is never forwarded across a redirect.
    """

    def __init__(
        self,
        token: str,
        repository: str,
        *,
        api_url: str = DEFAULT_GITHUB_API_URL,
        timeout: float = 30.0,
    ) -> None:
        if not _REPOSITORY.match(repository):
            raise ApprovalError(f"GITHUB_REPOSITORY must be owner/name, not {repository!r}")
        if not api_url.startswith("https://"):
            raise ApprovalError(f"GITHUB_API_URL must be an https URL, not {api_url!r}")
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, query: Mapping[str, str | int] | None = None) -> object | None:
        url = f"{self.api_url}/repos/{self.repository}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "autoform-review-authentication",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read(500).decode("utf-8", "replace").strip()
            raise ApprovalError(f"GitHub API GET {path} failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ApprovalError(f"GitHub API GET {path} failed: {exc}") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ApprovalError(f"GitHub API GET {path} returned more than {_MAX_RESPONSE_BYTES} bytes")
        try:
            return json.loads(body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ApprovalError(f"GitHub API GET {path} returned malformed JSON") from exc


class _BudgetSpent(Exception):
    pass


class GitHubReviewVerifier:
    """Authenticate approvals from approving pull request reviews.

    Approval (A, H) is authenticated when a pull request that introduced H to
    A's file has an APPROVED review that is its reviewer's latest review other
    than a comment, whose reviewer is not the pull request's author but is an
    individual ``@user`` code owner of A's path in CODEOWNERS at
    ``trusted_ref``, and whose commit records ``review_approved: H`` in A's
    file. The pull request is found from the most recent commit in this
    checkout's history that introduced H to that file.
    """

    method = GITHUB_REVIEW_METHOD

    def __init__(self, client: GitHubClient, *, trusted_ref: str = "HEAD", max_requests: int = 500) -> None:
        self.client = client
        self.trusted_ref = trusted_ref
        self.max_requests = max_requests
        self.requests = 0
        self.reasons: dict[str, str] = {}
        self._pulls: dict[str, list[dict]] = {}
        self._reviews: dict[int, list[dict]] = {}
        self._recorded: dict[tuple[str, str], str | None] = {}

    @classmethod
    def from_environment(
        cls,
        *,
        trusted_ref: str = "HEAD",
        environ: Mapping[str, str] | None = None,
    ) -> GitHubReviewVerifier:
        """Build from the variables GitHub Actions provides, naming any that are missing."""

        env = os.environ if environ is None else environ
        missing = [name for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY") if not env.get(name)]
        if missing:
            raise ApprovalError(
                f"GitHub review authentication needs {' and '.join(missing)} in the environment "
                "(a token that can read pull requests, and the repository as owner/name)"
            )
        client = GitHubClient(
            env["GITHUB_TOKEN"],
            env["GITHUB_REPOSITORY"],
            api_url=env.get("GITHUB_API_URL") or DEFAULT_GITHUB_API_URL,
        )
        return cls(client, trusted_ref=trusted_ref)

    def verify(self, graph: Graph, approvals: Mapping[str, str]) -> dict[str, ApprovalAttestation]:
        self.reasons = {}
        if not approvals:
            return {}
        root = repository_root(graph.blueprint_dir)
        if _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == b"true":
            raise ApprovalError("GitHub review authentication needs full Git history; check out with fetch-depth: 0")
        codeowners = load_codeowners(root, self.trusted_ref)
        attestations: dict[str, ApprovalAttestation] = {}
        for node_id, review_hash in sorted(approvals.items()):
            node = graph.nodes.get(node_id)
            if node is None:
                self.reasons[node_id] = "no such article in the blueprint"
                continue
            try:
                attestation, reason = self._verify_one(root, node_id, node.path, review_hash, codeowners)
            except _BudgetSpent:
                attestation = None
                reason = f"not checked: the budget of {self.max_requests} GitHub API requests was spent"
            if attestation is not None:
                attestations[node_id] = attestation
            else:
                self.reasons[node_id] = reason
        return attestations

    def _verify_one(
        self,
        root: Path,
        node_id: str,
        article: Path,
        review_hash: str,
        codeowners: tuple[str, tuple[CodeOwnersRule, ...]] | None,
    ) -> tuple[ApprovalAttestation | None, str]:
        path = _relative_path(article, root)
        if codeowners is None:
            return None, f"{self.trusted_ref} has no CODEOWNERS file, so no reviewer is allowed"
        location, rules = codeowners
        owners = code_owners(rules, path)
        allowed = {login.casefold() for login in individual_owners(owners)}
        if not owners:
            return None, f"{location} at {self.trusted_ref} names no code owner for {path}"
        if not allowed:
            return None, (
                f"the code owners of {path} are {' '.join(owners)}; teams and email owners cannot be verified "
                "with a workflow token, so name an individual @user"
            )
        commit = self._introducing_commit(root, path, review_hash)
        if commit is None:
            return None, f"no commit in this checkout's history records this hash in {path}"
        pulls = self._pulls_for(commit)
        if not pulls:
            return None, f"commit {commit[:12]}, which recorded this hash, belongs to no pull request"
        notes: list[str] = []
        for pull in pulls:
            number = pull.get("number")
            if not isinstance(number, int):
                continue
            author = _login(pull.get("user")).casefold()
            latest: dict[str, dict] = {}
            for review in self._reviews_for(number):
                login = _login(review.get("user"))
                if login and review.get("state") not in {"COMMENTED", "PENDING"}:
                    latest[login.casefold()] = review
            for key, review in latest.items():
                login = _login(review.get("user"))
                if key not in allowed:
                    if review.get("state") == "APPROVED":
                        notes.append(f"@{login} approved #{number} but is not an individual code owner of {path}")
                    continue
                if review.get("state") != "APPROVED":
                    notes.append(f"@{login}'s latest review of #{number} is {review.get('state')}")
                    continue
                if key == author:
                    notes.append(f"@{login} approved #{number} but is its author")
                    continue
                reviewed = review.get("commit_id")
                if not isinstance(reviewed, str) or self._recorded_at(reviewed, path) != review_hash:
                    notes.append(f"@{login} approved #{number} at a commit where {path} does not record this hash")
                    continue
                return ApprovalAttestation(node_id, review_hash, login, self.method, _review_url(pull, review)), ""
            if not latest:
                notes.append(f"#{number} has no review")
        if not notes:
            notes.append(f"no pull request for commit {commit[:12]} has a review")
        return None, "; ".join(notes)

    def _get(self, path: str, query: Mapping[str, str | int] | None = None) -> object | None:
        if self.requests >= self.max_requests:
            raise _BudgetSpent
        self.requests += 1
        return self.client.get(path, query)

    def _pages(self, path: str) -> list[dict]:
        items: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._get(path, {"per_page": _PAGE_SIZE, "page": page})
            if batch is None:
                return items
            if not isinstance(batch, list):
                raise ApprovalError(f"GitHub API GET {path} did not return a list")
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < _PAGE_SIZE:
                return items
        raise ApprovalError(f"GitHub API GET {path} has more than {_MAX_PAGES * _PAGE_SIZE} entries")

    def _pulls_for(self, commit: str) -> list[dict]:
        if commit not in self._pulls:
            pulls = self._pages(f"/commits/{commit}/pulls")
            self._pulls[commit] = sorted(pulls, key=lambda pull: _number(pull.get("number")))
        return self._pulls[commit]

    def _reviews_for(self, number: int) -> list[dict]:
        if number not in self._reviews:
            # GitHub lists reviews oldest first, so the last one kept per
            # reviewer is that reviewer's current verdict.
            self._reviews[number] = self._pages(f"/pulls/{number}/reviews")
        return self._reviews[number]

    def _recorded_at(self, commit: str, path: str) -> str | None:
        key = (commit, path)
        if key not in self._recorded:
            content = self._get(f"/contents/{urllib.parse.quote(path)}", {"ref": commit})
            value = None
            if isinstance(content, dict) and content.get("type") == "file" and content.get("encoding") == "base64":
                try:
                    text = base64.b64decode(str(content.get("content", ""))).decode("utf-8")
                except (ValueError, UnicodeError):
                    text = ""
                value = frontmatter_value(text, "review_approved")
            self._recorded[key] = value
        return self._recorded[key]

    def _introducing_commit(self, root: Path, path: str, review_hash: str) -> str | None:
        log = _git(root, "--literal-pathspecs", "log", f"-S{review_hash}", "--format=%H", "--", path)
        if log.returncode != 0:
            raise ApprovalError(f"git log failed for {path}: {log.stderr.decode('utf-8', 'replace').strip()}")
        for commit in log.stdout.decode("ascii", "replace").split()[:_MAX_INTRODUCING_COMMITS]:
            text = _show(root, commit, path)
            if text is not None and frontmatter_value(text, "review_approved") == review_hash:
                return commit
        return None


def _number(value: object) -> int:
    return value if isinstance(value, int) else 0


def _login(user: object) -> str:
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) else ""


def _review_url(pull: dict, review: dict) -> str:
    url = review.get("html_url")
    if isinstance(url, str) and url:
        return url
    return f"{pull.get('html_url', '')}#pullrequestreview-{review.get('id', '')}"


# Git


def repository_root(directory: Path) -> Path:
    result = _git(Path(directory), "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise ApprovalError(f"{directory} is not inside a Git checkout, so approvals have no history to check")
    return Path(result.stdout.decode("utf-8").strip()).resolve()


def _relative_path(path: Path, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise ApprovalError(f"{path} is outside the Git checkout {root}") from exc


def _require_commit(root: Path, ref: str) -> None:
    if ref.startswith("-") or _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise ApprovalError(f"{ref!r} is not a commit in this checkout; fetch it (fetch-depth: 0) or name another")


def _show(root: Path, ref: str, path: str) -> str | None:
    result = _git(root, "cat-file", "blob", f"{ref}:{path}")
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError:
        return None


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=str(root),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApprovalError(f"git {arguments[0]} failed: {exc}") from exc


__all__ = [
    "CODEOWNERS_LOCATIONS",
    "DEFAULT_GITHUB_API_URL",
    "GITHUB_REVIEW_METHOD",
    "SELF_APPROVED",
    "ApprovalAttestation",
    "ApprovalError",
    "ApprovalStatus",
    "ApprovalVerifier",
    "CodeOwnersRule",
    "GitHubClient",
    "GitHubReviewVerifier",
    "approval_statuses",
    "approvals_at",
    "code_owners",
    "current_approvals",
    "individual_owners",
    "load_codeowners",
    "parse_codeowners",
    "repository_root",
]
