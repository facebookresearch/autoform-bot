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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from .graph import Graph, frontmatter_value
from .readback import Readback
from .review import ReviewBundle, ReviewError


DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_GITHUB_WEB_URL = "https://github.com"
DEFAULT_VERIFY_WORKFLOW = ".github/workflows/autoform-verify.yml"
GITHUB_REVIEW_METHOD = "github-review"
CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
SELF_APPROVED = "self-approved"
_PAGE_SIZE = 100
_MAX_PAGES = 30
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_HISTORY = 500
# GitHub ignores a code owner without write access; these associations have it.
_WRITE_ACCESS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
# Underscores appear in Enterprise Managed User logins such as octocat_acme.
_LOGIN = r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?"
_REVIEWER = re.compile(rf"{_LOGIN}\Z")
_USER_OWNER = re.compile(rf"@{_LOGIN}\Z")
_TEAM_OWNER = re.compile(rf"@{_LOGIN}/[A-Za-z0-9_.-]+\Z")
_EMAIL_OWNER = re.compile(r"[^@\s]+@[^@\s]+\Z")
_UNSUPPORTED_PATTERN = ("!", "[", "]", "\\")
_TOKEN_SEPARATOR = re.compile(r"[ \t]+")
_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_T = TypeVar("_T")


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
    authenticated, which callers show next to the self-approved label, and
    ``web_url``, the only site a rendered page links references to.
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
    """One CODEOWNERS line: a path pattern and the owners it assigns.

    ``problem`` says why GitHub's reading of the line cannot be decided here.
    Such a rule keeps a ``regex`` matching every path it could apply to (None
    for every path), so that ownership is undecidable only where it matters.
    """

    line: int
    pattern: str
    owners: tuple[str, ...]
    regex: re.Pattern[str] | None
    problem: str | None = None

    def matches(self, path: str) -> bool:
        return self.regex is None or self.regex.fullmatch(path) is not None


def parse_codeowners(text: str, *, source: str = "CODEOWNERS") -> tuple[CodeOwnersRule, ...]:
    """Parse CODEOWNERS as GitHub splits it, marking lines that cannot be decided.

    Lines end at a line feed, without a trailing carriage return, and tokens
    are separated by spaces and tabs. A line holding any other control or
    separator character, a pattern using negation, character classes, or
    escapes, or an owner that is not @user, @org/team, or an email, could be
    read differently by GitHub; it is kept as an undecidable rule rather than
    guessed, because a misread rule could let the wrong person approve.
    """

    rules: list[CodeOwnersRule] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        location = f"{source}:{number}"
        hidden = next(
            (
                character
                for character in line
                if character not in " \t" and unicodedata.category(character)[0] in {"C", "Z"}
            ),
            None,
        )
        if hidden is not None:
            rules.append(
                CodeOwnersRule(
                    number,
                    line,
                    (),
                    None,
                    f"{location} contains U+{ord(hidden):04X}, which GitHub may read as a line or token break",
                )
            )
            continue
        tokens = _TOKEN_SEPARATOR.split(line.strip(" \t"))
        if not tokens[0] or tokens[0].startswith("#"):
            continue
        pattern, owners, problem = tokens[0], [], None
        for token in tokens[1:]:
            if token.startswith("#"):
                break
            if _USER_OWNER.match(token) or _TEAM_OWNER.match(token) or _EMAIL_OWNER.match(token):
                owners.append(token)
            elif problem is None:
                problem = f"{location}: unsupported owner {token!r}; use @user, @org/team, or an email"
        regex, pattern_problem = _pattern_regex(pattern, location)
        rules.append(CodeOwnersRule(number, pattern, tuple(owners), regex, pattern_problem or problem))
    return tuple(rules)


def code_owners(rules: tuple[CodeOwnersRule, ...], path: str) -> tuple[str, ...]:
    """The owners of a repository-relative POSIX path; the last matching rule wins.

    Raises ``ApprovalError`` when the deciding rule could be an undecidable one.
    """

    for rule in reversed(rules):
        if rule.matches(path):
            if rule.problem is not None:
                raise ApprovalError(f"{rule.problem}, so the code owners of {path} cannot be decided")
            return rule.owners
    return ()


def individual_owners(owners: tuple[str, ...]) -> tuple[str, ...]:
    """The ``@user`` owners, without the ``@``. Teams and emails never authenticate."""

    return tuple(owner[1:] for owner in owners if _USER_OWNER.match(owner))


def _pattern_regex(pattern: str, location: str) -> tuple[re.Pattern[str] | None, str | None]:
    if any(character in pattern for character in _UNSUPPORTED_PATTERN):
        return _superset_regex(pattern), (
            f"{location}: unsupported CODEOWNERS pattern {pattern!r}; negation, character classes, "
            "and escapes are not read"
        )
    expression = _glob_expression(pattern, widen=False)
    if expression is None:
        return None, f"{location}: malformed CODEOWNERS pattern {pattern!r}"
    return re.compile(expression), None


def _superset_regex(pattern: str) -> re.Pattern[str] | None:
    """Match every path an unsupported pattern could mean, or None for any path."""

    variants = [pattern, pattern[1:]] if pattern.startswith("!") else [pattern]
    expressions: list[str] = []
    for variant in variants:
        segments = variant.split("/")
        if any(segment.endswith("\\") for segment in segments[:-1]):
            return None  # an escaped slash moves the segment boundaries
        # A class or escape stays within one path segment, so a wildcard
        # segment covers both its literal and its gitignore readings.
        widened = "/".join(
            "*" if any(character in segment for character in "[]\\") else segment for segment in segments
        )
        expression = _glob_expression(widened, widen=True)
        if expression is None:
            return None
        expressions.append(expression)
    return re.compile("|".join(f"(?:{expression})" for expression in expressions))


def _glob_expression(pattern: str, *, widen: bool) -> str | None:
    directory = pattern.endswith("/")
    body = pattern.strip("/")
    # gitignore rule: a slash anywhere but the end anchors to the repository root.
    anchored = pattern.startswith("/") or "/" in body
    segments = body.split("/")
    if not body or any(not segment for segment in segments):
        return None
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
    elif widen or (segments[-1] != "**" and not any(character in segments[-1] for character in "*?")):
        # A literal final name may be a file or a directory. GitHub documents
        # that a wildcard final segment such as docs/* matches only direct
        # children, so those patterns get no descendant suffix.
        expression += "(?:/.+)?"
    return expression


def load_codeowners(root: Path, ref: str, *, name: str | None = None) -> tuple[str, tuple[CodeOwnersRule, ...]] | None:
    """Read the CODEOWNERS file GitHub would use at ``ref``, or None when there is none.

    GitHub uses the first location that exists, so one that is not UTF-8 is
    an error rather than a reason to read the next.
    """

    _require_commit(root, ref)
    label = name or ref
    for location in CODEOWNERS_LOCATIONS:
        data = _blob(root, ref, location)
        if data is None:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise ApprovalError(
                f"{label}:{location} is not UTF-8, so the code owners it names cannot be decided"
            ) from None
        return location, parse_codeowners(text, source=f"{label}:{location}")
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


class _Refused(Exception):
    """One approval is not authenticated, for the stated reason."""


class GitHubReviewVerifier:
    """Authenticate approvals from the pull request that recorded them.

    On the default branch, approval (A, H) at ``trusted_ref`` R, where p is
    A's path, is authenticated when all of these hold:

    1. Walking R's first-parent history of p, M is the oldest commit of the
       unbroken run ending at R in which p records H, so M's first parent
       does not. Moving the article starts a new run.
    2. Exactly one pull request P merged into the default branch is
       associated with M; a direct push has none.
    3. P's diff adds a frontmatter line to p recording ``review_approved: H``
       where a reviewer sees it, and p records H at P's head commit.
    4. A reviewer whose latest verdict on P is an approval of P's head commit
       is not P's author, has write access (OWNER, MEMBER, or COLLABORATOR),
       and is an individual ``@user`` code owner of p in CODEOWNERS both at
       M's first parent and at R.
    5. ``verify_workflow`` succeeded in a pull_request run on P's head commit
       and P does not change that workflow. Its build runs ``review check``,
       which fails unless H is current, so H described what the reviewer saw.

    With ``pull_request`` N, the pre-merge gate, P is pull request N, code
    owners come from R alone (the gate's base commit), and steps 1, 2, and 5
    are skipped: nothing is merged or finished yet.

    Anything that cannot be checked, including a failed request, a spent
    request budget, or undecidable ownership, leaves that one approval
    self-approved and says why in ``reasons``.
    """

    method = GITHUB_REVIEW_METHOD

    def __init__(
        self,
        client: GitHubClient,
        *,
        trusted_ref: str = "HEAD",
        pull_request: int | None = None,
        verify_workflow: str = DEFAULT_VERIFY_WORKFLOW,
        web_url: str | None = None,
        max_requests: int = 500,
    ) -> None:
        self.client = client
        self.trusted_ref = trusted_ref
        self.pull_request = pull_request
        self.verify_workflow = verify_workflow
        self.web_url = (web_url or _web_url(getattr(client, "api_url", DEFAULT_GITHUB_API_URL))).rstrip("/")
        self.max_requests = max_requests
        self.requests = 0
        self.reasons: dict[str, str] = {}
        self._cache: dict[tuple[object, ...], object] = {}

    @classmethod
    def from_environment(
        cls,
        *,
        trusted_ref: str = "HEAD",
        pull_request: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> GitHubReviewVerifier:
        """Build from the variables GitHub Actions provides, naming any that are missing.

        ``AUTOFORM_VERIFY_WORKFLOW`` names the workflow whose success shows a
        hash was current, when it is not the scaffolded autoform-verify.yml.
        """

        env = os.environ if environ is None else environ
        missing = [name for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY") if not env.get(name)]
        if missing:
            raise ApprovalError(
                f"GitHub review authentication needs {' and '.join(missing)} in the environment "
                "(a token that can read pull requests and Actions runs, and the repository as owner/name)"
            )
        api_url = env.get("GITHUB_API_URL") or DEFAULT_GITHUB_API_URL
        client = GitHubClient(env["GITHUB_TOKEN"], env["GITHUB_REPOSITORY"], api_url=api_url)
        web_url = env.get("GITHUB_SERVER_URL") or _web_url(api_url)
        if not web_url.startswith("https://"):
            raise ApprovalError(f"GITHUB_SERVER_URL must be an https URL, not {web_url!r}")
        return cls(
            client,
            trusted_ref=trusted_ref,
            pull_request=pull_request,
            verify_workflow=env.get("AUTOFORM_VERIFY_WORKFLOW") or DEFAULT_VERIFY_WORKFLOW,
            web_url=web_url,
        )

    def verify(self, graph: Graph, approvals: Mapping[str, str]) -> dict[str, ApprovalAttestation]:
        self.reasons = {}
        if not approvals:
            return {}
        # Only a checkout that cannot answer at all stops here; everything
        # else is decided, or refused, one approval at a time.
        root = repository_root(graph.blueprint_dir)
        if _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == b"true":
            raise ApprovalError("GitHub review authentication needs full Git history; check out with fetch-depth: 0")
        trusted = _commit_id(root, self.trusted_ref)
        attestations: dict[str, ApprovalAttestation] = {}
        for node_id, review_hash in sorted(approvals.items()):
            node = graph.nodes.get(node_id)
            try:
                if node is None:
                    raise _Refused("no such article in the blueprint")
                attestations[node_id] = self._verify_one(root, trusted, node_id, node.path, review_hash)
            except _Refused as exc:
                self.reasons[node_id] = str(exc)
            except ApprovalError as exc:
                self.reasons[node_id] = str(exc)
            except _BudgetSpent:
                self.reasons[node_id] = f"not checked: the budget of {self.max_requests} GitHub API requests was spent"
        return attestations

    def _verify_one(
        self, root: Path, trusted: str, node_id: str, article: Path, review_hash: str
    ) -> ApprovalAttestation:
        path = _relative_path(article, root)
        wanted = review_hash.lower()
        if self.pull_request is None:
            commit, parent = self._introduction(root, trusted, path, wanted)
            before = (parent, f"{parent[:12]} (before {commit[:12]})")
            owners = self._owners(root, path, before, (trusted, self.trusted_ref))
            pull = self._merged_pull(commit)
        else:
            owners = self._owners(root, path, (trusted, self.trusted_ref))
            pull = self._gate_pull()
        self._check_recorded(pull, path, wanted)
        reviewer, review = self._approver(pull, path, owners)
        if self.pull_request is None:
            self._check_verified(pull)
        return ApprovalAttestation(node_id, review_hash, reviewer, self.method, self._review_url(pull, review))

    # 1. Which commit recorded the hash.

    def _introduction(self, root: Path, trusted: str, path: str, wanted: str) -> tuple[str, str]:
        if _value_at(root, trusted, path) != wanted:
            raise _Refused(f"{path} does not record this hash at {self.trusted_ref}")
        # First parents only: a merge counts as its own change of the file,
        # so neither a side branch's history nor a merge resolution hides it.
        log = _git(root, "--literal-pathspecs", "rev-list", "--first-parent", trusted, "--", path)
        if log.returncode != 0:
            raise ApprovalError(f"git rev-list failed for {path}: {log.stderr.decode('utf-8', 'replace').strip()}")
        commits = log.stdout.decode("ascii", "replace").split()
        for commit in commits[:_MAX_HISTORY]:
            parent = _parent(root, commit)
            if parent is None:
                raise _Refused(
                    f"{path} has recorded this hash since the first commit {commit[:12]}, "
                    "which no pull request can have reviewed"
                )
            if _value_at(root, parent, path) == wanted:
                continue
            if _value_at(root, commit, path) != wanted:
                raise _Refused(f"the first-parent history of {path} is inconsistent at {commit[:12]}")
            return commit, parent
        if len(commits) > _MAX_HISTORY:
            raise _Refused(f"more than {_MAX_HISTORY} first-parent commits changed {path} while it recorded this hash")
        raise _Refused(f"no first-parent commit of {self.trusted_ref} records this hash in {path}")

    # 4, first half. Who may approve, from Git alone.

    def _owners(self, root: Path, path: str, *refs: tuple[str, str]) -> frozenset[str]:
        allowed: frozenset[str] | None = None
        for commit, label in refs:
            here = self._owners_at(root, commit, label, path)
            allowed = here if allowed is None else allowed & here
        if not allowed:
            raise _Refused(
                f"no individual @user is a code owner of {path} both at {refs[0][1]} and at {refs[-1][1]}"
            )
        return allowed

    def _owners_at(self, root: Path, commit: str, label: str, path: str) -> frozenset[str]:
        name = label.split(" ", 1)[0]
        codeowners = self._once(("codeowners", commit), lambda: load_codeowners(root, commit, name=name))
        if codeowners is None:
            raise _Refused(f"{label} has no CODEOWNERS file, so no reviewer is allowed")
        location, rules = codeowners
        owners = code_owners(rules, path)
        if not owners:
            raise _Refused(f"{location} at {label} names no code owner for {path}")
        logins = individual_owners(owners)
        if not logins:
            raise _Refused(
                f"the code owners of {path} at {label} are {' '.join(owners)}; teams and email owners cannot be "
                "verified with a workflow token, so name an individual @user"
            )
        # Validated ASCII logins, which GitHub compares without case.
        return frozenset(login.lower() for login in logins)

    # 2. Which pull request that commit belongs to.

    def _default_branch(self) -> str:
        def fetch() -> str:
            repository = self._get("")
            branch = repository.get("default_branch") if isinstance(repository, dict) else None
            if not isinstance(branch, str) or not branch:
                raise ApprovalError("GitHub API GET of the repository did not name its default branch")
            return branch

        return self._once(("default-branch",), fetch)

    def _merged_pull(self, commit: str) -> dict:
        default = self._default_branch()
        pulls = self._once(("pulls", commit), lambda: self._pages(f"/commits/{commit}/pulls"))
        merged = [pull for pull in pulls if pull.get("merged_at") and _base_ref(pull) == default]
        if not merged:
            raise _Refused(
                f"commit {commit[:12]}, which recorded this hash, came from no pull request merged into {default}"
            )
        if len(merged) > 1:
            numbers = ", ".join(f"#{_number(pull.get('number'))}" for pull in merged)
            raise _Refused(
                f"commit {commit[:12]}, which recorded this hash, belongs to several pull requests: {numbers}"
            )
        return _checked_pull(merged[0])

    def _gate_pull(self) -> dict:
        number = self.pull_request
        pull = self._once(("pull", number), lambda: self._get(f"/pulls/{number}"))
        if not isinstance(pull, dict) or pull.get("number") != number:
            raise _Refused(f"pull request #{number} was not found")
        default = self._default_branch()
        if _base_ref(pull) != default:
            raise _Refused(
                f"#{number} targets {_base_ref(pull)!r}, not the default branch {default!r}; "
                "only a pull request into the default branch can approve"
            )
        return _checked_pull(pull)

    # 3. That the pull request visibly recorded the hash.

    def _check_recorded(self, pull: dict, path: str, wanted: str) -> None:
        number, head = pull["number"], pull["head"]["sha"]
        entry = next((item for item in self._files(number) if item.get("filename") == path), None)
        if entry is None:
            raise _Refused(f"#{number} does not change {path}, so it cannot have recorded this hash")
        patch = entry.get("patch")
        added = _added_lines(patch, entry) if isinstance(patch, str) else None
        if added is None:
            raise _Refused(f"GitHub shows no complete diff of {path} in #{number}, so the recorded hash cannot be seen")
        text = self._once(("contents", head, path), lambda: self._text_at(head, path))
        if text is None or frontmatter_value(text, "review_approved") != wanted:
            raise _Refused(f"{path} does not record this hash at the head {head[:12]} of #{number}")
        if not _adds_approval(added, text, wanted):
            raise _Refused(f"the diff of #{number} does not add a review_approved line with this hash to {path}")

    # 4, second half. Who approved it.

    def _approver(self, pull: dict, path: str, owners: frozenset[str]) -> tuple[str, dict]:
        number, head = pull["number"], pull["head"]["sha"]
        author = _login(pull.get("user")).lower()
        reviews = self._once(("reviews", number), lambda: self._pages(f"/pulls/{number}/reviews"))
        latest: dict[str, dict] = {}
        for review in sorted(reviews, key=_review_order):
            login = _login(review.get("user"))
            # A comment is not a verdict, so it neither grants nor voids one.
            if _REVIEWER.match(login) and review.get("state") not in {"COMMENTED", "PENDING"}:
                latest[login.lower()] = review
        notes: list[str] = []
        for key, review in latest.items():
            login, state = _login(review.get("user")), review.get("state")
            if key not in owners:
                if state == "APPROVED":
                    notes.append(f"@{login} approved #{number} but is not an individual code owner of {path}")
            elif state != "APPROVED":
                notes.append(f"@{login}'s latest review of #{number} is {state}")
            elif key == author:
                notes.append(f"@{login} approved #{number} but is its author")
            elif review.get("author_association") not in _WRITE_ACCESS:
                notes.append(
                    f"@{login} approved #{number} but GitHub does not show them with write access "
                    f"({review.get('author_association')})"
                )
            elif review.get("commit_id") != head:
                notes.append(f"@{login} approved an earlier commit of #{number}, not its head {head[:12]}")
            else:
                return login, review
        if not latest:
            notes.append(f"#{number} has no review")
        raise _Refused("; ".join(notes) or f"#{number} has no approving review by a code owner of {path}")

    # 5. That the hash was current where it was approved.

    def _check_verified(self, pull: dict) -> None:
        number, head = pull["number"], pull["head"]["sha"]
        workflow = self.verify_workflow
        if any(workflow in (item.get("filename"), item.get("previous_filename")) for item in self._files(number)):
            raise _Refused(f"#{number} changes {workflow}, so its own run of it is no evidence")
        runs = self._once(
            ("runs", head),
            lambda: self._pages("/actions/runs", {"head_sha": head, "event": "pull_request"}, key="workflow_runs"),
        )
        if not any(_succeeded(run, workflow, head) for run in runs):
            raise _Refused(
                f"{workflow} has no successful pull_request run on the head {head[:12]} of #{number}, "
                "so nothing shows this hash was current there"
            )

    # Requests.

    def _files(self, number: int) -> list[dict]:
        return self._once(("files", number), lambda: self._pages(f"/pulls/{number}/files"))

    def _once(self, key: tuple[object, ...], fetch: Callable[[], _T]) -> _T:
        """Fetch once per verifier; a failure is remembered and raised again."""

        if key not in self._cache:
            try:
                self._cache[key] = fetch()
            except ApprovalError as exc:
                self._cache[key] = exc
        value = self._cache[key]
        if isinstance(value, ApprovalError):
            raise value
        return value  # type: ignore[return-value]

    def _get(self, path: str, query: Mapping[str, str | int] | None = None) -> object | None:
        if self.requests >= self.max_requests:
            raise _BudgetSpent
        self.requests += 1
        return self.client.get(path, query)

    def _pages(self, path: str, query: Mapping[str, str | int] | None = None, *, key: str | None = None) -> list[dict]:
        items: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._get(path, {**(query or {}), "per_page": _PAGE_SIZE, "page": page})
            if batch is None:
                return items
            if key is not None:
                batch = batch.get(key) if isinstance(batch, dict) else None
            if not isinstance(batch, list):
                raise ApprovalError(f"GitHub API GET {path} did not return a list")
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < _PAGE_SIZE:
                return items
        raise ApprovalError(f"GitHub API GET {path} has more than {_MAX_PAGES * _PAGE_SIZE} entries")

    def _text_at(self, commit: str, path: str) -> str | None:
        content = self._get(f"/contents/{urllib.parse.quote(path)}", {"ref": commit})
        if not (isinstance(content, dict) and content.get("type") == "file" and content.get("encoding") == "base64"):
            return None
        try:
            return base64.b64decode(str(content.get("content", ""))).decode("utf-8")
        except (ValueError, UnicodeError):
            return None

    def _review_url(self, pull: dict, review: dict) -> str:
        """A link to the review, only ever on this verifier's GitHub host."""

        prefix = self.web_url + "/"
        url = review.get("html_url")
        if isinstance(url, str) and url.startswith(prefix):
            return url
        pull_url = pull.get("html_url")
        if isinstance(pull_url, str) and pull_url.startswith(prefix):
            return f"{pull_url}#pullrequestreview-{_number(review.get('id'))}"
        return ""


def _checked_pull(pull: dict) -> dict:
    number = pull.get("number")
    head = pull.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(number, int) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ApprovalError("GitHub returned a pull request without a number and head commit")
    return pull


def _base_ref(pull: dict) -> str:
    base = pull.get("base")
    ref = base.get("ref") if isinstance(base, dict) else None
    return ref if isinstance(ref, str) else ""


def _added_lines(patch: str, entry: dict) -> list[tuple[int, str]] | None:
    """Lines a GitHub patch adds, by line number in the new file; None if truncated or malformed."""

    added: list[tuple[int, str]] = []
    removed = 0
    line: int | None = None
    rows = patch.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    for row in rows:
        if row.startswith("@@"):
            hunk = _HUNK.match(row)
            if hunk is None:
                return None
            line = int(hunk.group(1))
            continue
        marker = row[:1]
        if line is None or marker not in {"+", "-", " ", "\\"}:
            return None
        if marker == "+":
            added.append((line, row[1:]))
            line += 1
        elif marker == "-":
            removed += 1
        elif marker == " ":
            line += 1
    # GitHub cuts long patches short; counts that disagree mean lines are missing.
    if len(added) != entry.get("additions") or removed != entry.get("deletions"):
        return None
    return added


def _adds_approval(added: list[tuple[int, str]], text: str, wanted: str) -> bool:
    """Whether an added line is a frontmatter line recording ``review_approved: wanted``."""

    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return False
    for number, row in added:
        if not 2 <= number <= end or lines[number - 1] != row:
            continue
        entry = row[:-1] if row.endswith("\r") else row
        # One visible line in the diff must be one line to the parser too.
        if len(entry.splitlines()) == 1 and frontmatter_value(f"---\n{entry}\n---\n", "review_approved") == wanted:
            return True
    return False


def _review_order(review: dict) -> tuple[str, int]:
    submitted = review.get("submitted_at")
    return (submitted if isinstance(submitted, str) else "", _number(review.get("id")))


def _succeeded(run: dict, workflow: str, head: str) -> bool:
    path = run.get("path")
    return (
        isinstance(path, str)
        and path.split("@", 1)[0] == workflow
        and run.get("event") == "pull_request"
        and run.get("head_sha") == head
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    )


def _web_url(api_url: str) -> str:
    """The web host that goes with a GitHub API URL."""

    parts = urllib.parse.urlsplit(api_url)
    if parts.netloc == "api.github.com":
        return DEFAULT_GITHUB_WEB_URL
    path = parts.path.rstrip("/")
    host = parts.netloc
    if path.endswith("/api/v3"):
        path = path[: -len("/api/v3")]
    elif host.startswith("api."):
        host = host[len("api.") :]
    return urllib.parse.urlunsplit((parts.scheme, host, path, "", ""))


def _number(value: object) -> int:
    return value if isinstance(value, int) else 0


def _login(user: object) -> str:
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) else ""


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


def _commit_id(root: Path, ref: str) -> str:
    result = None if ref.startswith("-") else _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if result is None or result.returncode != 0:
        raise ApprovalError(f"{ref!r} is not a commit in this checkout; fetch it (fetch-depth: 0) or name another")
    return result.stdout.decode("ascii").strip()


def _require_commit(root: Path, ref: str) -> None:
    _commit_id(root, ref)


def _parent(root: Path, commit: str) -> str | None:
    result = _git(root, "rev-parse", "--verify", "--quiet", f"{commit}^1")
    return result.stdout.decode("ascii").strip() if result.returncode == 0 else None


def _value_at(root: Path, ref: str, path: str) -> str | None:
    text = _show(root, ref, path)
    return None if text is None else frontmatter_value(text, "review_approved")


def _blob(root: Path, ref: str, path: str) -> bytes | None:
    result = _git(root, "cat-file", "blob", f"{ref}:{path}")
    return result.stdout if result.returncode == 0 else None


def _show(root: Path, ref: str, path: str) -> str | None:
    data = _blob(root, ref, path)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeError:
        return None


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            # Replacement refs could rewrite the history being judged.
            ["git", "--no-replace-objects", *arguments],
            cwd=str(root),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApprovalError(f"git {arguments[0]} failed: {exc}") from exc


__all__ = [
    "ApprovalAttestation",
    "ApprovalError",
    "ApprovalStatus",
    "ApprovalVerifier",
    "GitHubClient",
    "GitHubReviewVerifier",
    "approval_statuses",
    "approvals_at",
    "code_owners",
    "current_approvals",
    "parse_codeowners",
]
