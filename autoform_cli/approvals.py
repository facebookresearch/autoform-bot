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
from pathlib import Path, PurePosixPath
from typing import Protocol, TypeVar

from .graph import Graph, frontmatter_value
from .readback import READBACKS_DIR, Readback
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
# GitHub lists at most 250 commits and 3000 files of a pull request.
_MAX_PULL_COMMITS = 250
_MAX_PULL_FILES = 3000
# GitHub does not load a CODEOWNERS file of 3 MB or more.
_MAX_CODEOWNERS_BYTES = 3_000_000
_UNCOVERED_SHOWN = 10
# GitHub allows a workflow's GITHUB_TOKEN 1000 API requests an hour in one
# repository, shared by every run there. A run may make _BASE_REQUESTS,
# enough for one rebase merge of _MAX_PULL_COMMITS commits, and
# _REQUESTS_PER_APPROVAL more for each approval, about what an approval
# recorded in its own pull request needs, but never more than _MAX_REQUESTS.
# That bounds one run, not the hour: two full builds in an hour can run out,
# and the approvals the refused requests leave are unchecked.
_GITHUB_TOKEN_HOURLY_LIMIT = 1000
_BASE_REQUESTS = 500
_REQUESTS_PER_APPROVAL = 10
_MAX_REQUESTS = 900
# What the default branch's pull request rules must turn on: the parameter, the
# name GitHub's ruleset settings show, and what goes wrong without it.
_REVIEW_SETTINGS = (
    ("require_code_owner_review", "Require review from Code Owners", "a pull request can merge without its code owners"),
    (
        "dismiss_stale_reviews_on_push",
        "Dismiss stale pull request approvals when new commits are pushed",
        "an approval still counts after pushes its reviewer never saw",
    ),
    (
        "require_last_push_approval",
        "Require approval of the most recent reviewable push",
        "a code owner can push to someone else's pull request and approve their own push",
    ),
)
# Associations that can hold write access. Only a prefilter: the collaborator
# permission endpoint decides, because MEMBER is any member of the owning
# organization.
_WRITE_ACCESS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
# GitHub ignores a code owner without write access; these permissions have it.
_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
# Underscores appear in Enterprise Managed User logins such as octocat_acme.
_LOGIN = r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?"
_REVIEWER = re.compile(rf"{_LOGIN}\Z")
# A pull request an app opens has an author such as dependabot[bot].
_AUTHOR = re.compile(rf"{_LOGIN}(?:\[bot\])?\Z")
_USER_OWNER = re.compile(rf"@{_LOGIN}\Z")
_TEAM_OWNER = re.compile(rf"@{_LOGIN}/[A-Za-z0-9_.-]+\Z")
_EMAIL_OWNER = re.compile(r"[^@\s]+@[^@\s]+\Z")
_UNSUPPORTED_PATTERN = ("!", "[", "]", "\\")
_TOKEN_SEPARATOR = re.compile(r"[ \t]+")
_HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_T = TypeVar("_T")


def _printable(text: str) -> str:
    """``text`` with each character that is not printable escaped as Python writes it, such as ``\\n``.

    Reasons quote file names, refs, and GitHub's own answers, which a pull
    request author can choose. A newline in one, printed to a CI log, could
    start a line with ``::``, which GitHub Actions runs as a workflow command.
    Every reason the verifier records and every ApprovalError message passes
    through here.
    """

    return "".join(char if char.isprintable() else char.encode("unicode_escape").decode("ascii") for char in text)


class ApprovalError(ValueError):
    """Approval evidence could not be gathered or its rules could not be read."""

    def __init__(self, message: str) -> None:
        message = _printable(message)
        self.issues = (message,)
        super().__init__(message)


class HeadCheckError(ApprovalError):
    """The build cannot be shown to be of the head of the default branch, so nothing it renders may be published.

    Unlike every other refusal or failed request, this one stops the whole run
    instead of labelling each approval self-approved: a Pages build that
    deployed would replace a correct site with one where every approval reads
    self-approved.
    """


class SupersededBuildError(HeadCheckError):
    """The build is of a commit the default branch has moved past."""


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
    authenticated, which callers show next to the self-approved label,
    ``unchecked``, those of them it could not finish checking, such as for a
    failed request, which a later run may authenticate, and ``web_url``, the
    only site a rendered page links references to.
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

    rule = _deciding_rule(rules, path)
    if rule is None:
        return ()
    if rule.problem is not None:
        raise ApprovalError(f"{rule.problem}, so the code owners of {path} cannot be decided")
    return rule.owners


def _deciding_rule(rules: tuple[CodeOwnersRule, ...], path: str) -> CodeOwnersRule | None:
    return next((rule for rule in reversed(rules) if rule.matches(path)), None)


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

    GitHub uses the first location that exists, so one that is not UTF-8, or
    too large for GitHub to load, is an error rather than a reason to read the
    next.
    """

    _require_commit(root, ref)
    label = name or ref
    for location in CODEOWNERS_LOCATIONS:
        data = _blob(root, ref, location)
        if data is None:
            continue
        if len(data) >= _MAX_CODEOWNERS_BYTES:
            raise ApprovalError(
                f"{label}:{location} has {len(data)} bytes, and GitHub does not load a CODEOWNERS file of 3 MB "
                "or more, so it names no code owner"
            )
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


class _Unanswered(ApprovalError):
    """A request to GitHub failed, so a later run may decide what it would have."""


class _Refused(Exception):
    """One approval is not authenticated, for the stated reason."""


class GitHubReviewVerifier:
    """Authenticate approvals from the pull request that recorded them.

    Nothing is authenticated unless code owner review guards everything an
    approval rests on. Once per run, at ``trusted_ref`` R:

    0. Outside the gate, R is the head of the default branch on GitHub.
       When ``publishing``, as in the Pages build, SupersededBuildError stops
       the run otherwise, so a build the branch has moved past is not
       published, and HeadCheckError stops it when that head cannot be read.
       Without it, as in ``review check``, every approval is self-approved,
       saying why. The active rulesets on the default branch, leaving out any this verifier's token can bypass, have pull
       request rules that require code owner review, dismiss stale approvals
       on push, and require approval of the most recent push. GitHub reports
       no error in CODEOWNERS at R, and it gives every path that could exist
       an owner GitHub enforces: its last ``*`` rule and every rule after it
       name a team of the repository's owner or an individual ``@user`` with
       write access. Otherwise every approval is self-approved, naming the
       rules without one.

    On the default branch, approval (A, H) at R, where p is A's path, is
    authenticated when the steps below hold:

    1. M is a commit in the unbroken run of R's first-parent history of p
       that records H: the oldest commit of the run, whose first parent does
       not record H, or a newer one whose own diff adds a
       ``review_approved: H`` line. Candidates are tried newest first.
       Moving the article starts a new run.
    2. Exactly one pull request P merged into the default branch is
       associated with M; a direct push has none.
    3. P changes only articles and read-back cards, by file name and by
       previous name.
    4. P's diff adds a frontmatter line to p recording ``review_approved: H``
       where a reviewer sees it, and p records H at P's head commit.
    5. A reviewer whose latest verdict on P is an approval of P's head commit
       is not P's author, authored or committed none of P's commits, has
       write, maintain, or admin permission, and is an individual ``@user``
       code owner of p in CODEOWNERS both at P's base B and at R. B is the
       first of M's first-parent ancestors that GitHub does not associate
       with P: M's first parent unless P was rebased onto the branch, when
       P's own earlier commits come between.
    6. P comes from a branch of this repository that headed no other pull
       request, P never changed its base branch, and ``verify_workflow``
       succeeded in a pull_request run on P's head commit from that branch.
       P cannot change that workflow (step 3). Its build runs ``review
       check``, which fails unless H is current, so H described what the
       reviewer saw.

    With ``pull_request`` N, the pre-merge gate, P is pull request N, code
    owners come from R alone (the gate's base commit), and steps 1, 2, and 6
    are skipped: nothing is merged or finished yet. A P from a fork is
    refused in both modes before anything about it is read.

    Anything else that cannot be checked, including a failed request, a
    spent request budget, or undecidable ownership, leaves that one approval
    self-approved and says why in ``reasons``; a failed request or a spent
    budget, which a later run may get past, also puts it in ``unchecked``.
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
        max_requests: int | None = None,
        publishing: bool = True,
    ) -> None:
        self.client = client
        self.trusted_ref = trusted_ref
        self.pull_request = pull_request
        self.publishing = publishing
        self.verify_workflow = verify_workflow
        self.web_url = (web_url or _web_url(getattr(client, "api_url", DEFAULT_GITHUB_API_URL))).rstrip("/")
        self.max_requests = max_requests
        self.budget = 0
        self.requests = 0
        self.reasons: dict[str, str] = {}
        self.unchecked: dict[str, str] = {}
        self._cache: dict[tuple[object, ...], object] = {}
        self._blueprint = ""

    @classmethod
    def from_environment(
        cls,
        *,
        trusted_ref: str = "HEAD",
        pull_request: int | None = None,
        environ: Mapping[str, str] | None = None,
        publishing: bool = True,
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
            publishing=publishing,
        )

    def verify(self, graph: Graph, approvals: Mapping[str, str]) -> dict[str, ApprovalAttestation]:
        try:
            return self._verify(graph, approvals)
        finally:
            self.reasons = {node_id: _printable(reason) for node_id, reason in self.reasons.items()}
            self.unchecked = {node_id: self.reasons[node_id] for node_id in self.unchecked}

    def _verify(self, graph: Graph, approvals: Mapping[str, str]) -> dict[str, ApprovalAttestation]:
        self.reasons = {}
        self.unchecked = {}
        if not approvals:
            return {}
        # Only a checkout that cannot answer at all stops here; everything
        # else is decided, or refused, one approval at a time.
        root = repository_root(graph.blueprint_dir)
        if _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == b"true":
            raise ApprovalError("GitHub review authentication needs full Git history; check out with fetch-depth: 0")
        trusted = _commit_id(root, self.trusted_ref)
        blueprint = _relative_path(graph.blueprint_dir, root)
        self._blueprint = "" if blueprint == "." else blueprint
        self.budget = (
            min(_MAX_REQUESTS, _BASE_REQUESTS + _REQUESTS_PER_APPROVAL * len(approvals))
            if self.max_requests is None
            else self.max_requests
        )
        try:
            self._check_protection(root, trusted)
        except HeadCheckError:
            raise
        except (_Refused, ApprovalError) as exc:
            self.reasons = dict.fromkeys(sorted(approvals), str(exc))
            if isinstance(exc, _Unanswered):
                self.unchecked = dict(self.reasons)
            return {}
        except _BudgetSpent:
            self.reasons = dict.fromkeys(sorted(approvals), self._not_checked())
            self.unchecked = dict(self.reasons)
            return {}
        attestations: dict[str, ApprovalAttestation] = {}
        for node_id, review_hash in sorted(approvals.items()):
            node = graph.nodes.get(node_id)
            try:
                if node is None:
                    raise _Refused("no such article in the blueprint")
                attestations[node_id] = self._verify_one(root, trusted, node_id, node.path, review_hash)
            except (_Refused, ApprovalError) as exc:
                self.reasons[node_id] = str(exc)
                if isinstance(exc, _Unanswered):
                    self.unchecked[node_id] = str(exc)
            except _BudgetSpent:
                self.reasons[node_id] = self.unchecked[node_id] = self._not_checked()
        return attestations

    def _not_checked(self) -> str:
        return (
            f"not checked: the budget of {self.budget} GitHub API requests was spent; a run's budget grows with "
            f"its approvals up to {_MAX_REQUESTS}, under GitHub's limit of {_GITHUB_TOKEN_HOURLY_LIMIT} requests "
            "an hour for a workflow's GITHUB_TOKEN, which every run in the repository shares, and approvals "
            "recorded in one pull request share most of their requests"
        )

    def _verify_one(
        self, root: Path, trusted: str, node_id: str, article: Path, review_hash: str
    ) -> ApprovalAttestation:
        path = _relative_path(article, root)
        wanted = review_hash.lower()
        if self.pull_request is not None:
            owners = self._owners(root, path, (trusted, self.trusted_ref))
            return self._attest(node_id, review_hash, self._gate_pull(), path, wanted, owners)
        candidates = self._introductions(root, trusted, path, wanted)
        reasons: list[str] = []
        unanswered = False
        for commit, parent in candidates:
            try:
                if parent is None:
                    raise _Refused(
                        f"{path} has recorded this hash since the first commit {commit[:12]}, "
                        "which no pull request can have reviewed"
                    )
                # Owners at R first: they cost no request.
                self._owners_at(root, trusted, self.trusted_ref, path)
                pull = self._merged_pull(commit)
                # Before the walk to B, so nothing more about a fork is read.
                self._check_same_repository(pull)
                number = pull["number"]
                base = self._base_before(root, commit, parent, number)
                owners = self._owners(root, path, (base, f"{base[:12]} (before #{number})"), (trusted, self.trusted_ref))
                return self._attest(node_id, review_hash, pull, path, wanted, owners)
            except (_Refused, ApprovalError) as exc:
                reasons.append(str(exc) if len(candidates) == 1 else f"{commit[:12]}: {exc}")
                unanswered = unanswered or isinstance(exc, _Unanswered)
            except _BudgetSpent:
                # Keep what the earlier candidates were refused for.
                reasons.append(self._not_checked())
                unanswered = True
                break
        # A candidate that could not be checked may be the one a later run authenticates.
        raise (_Unanswered if unanswered else _Refused)("; ".join(dict.fromkeys(reasons)))

    def _attest(
        self, node_id: str, review_hash: str, pull: dict, path: str, wanted: str, owners: frozenset[str]
    ) -> ApprovalAttestation:
        self._check_same_repository(pull)
        self._check_content_only(pull)
        self._check_recorded(pull, path, wanted)
        reviewer, review = self._approver(pull, path, owners)
        if self.pull_request is None:
            self._check_verified(pull)
        return ApprovalAttestation(node_id, review_hash, reviewer, self.method, self._review_url(pull, review))

    # 0. Whether code owner review guards everything an approval rests on.

    def _check_protection(self, root: Path, trusted: str) -> None:
        """Refuse every approval unless code owner review is required and owns every path.

        Everything an approval rests on, from CODEOWNERS and workflows to the
        Lean sources, theme, and mkdocs.yml the Pages build runs, must have
        changed only under code owner review, and so must any file a pull
        request could add.
        """

        if self.pull_request is None:
            default = self._current_default_branch(trusted)
        else:
            default = self._default_branch()
        self._check_ruleset(default)
        codeowners = self._once(
            ("codeowners", trusted), lambda: load_codeowners(root, trusted, name=self.trusted_ref)
        )
        if codeowners is None:
            raise _Refused(f"{self.trusted_ref} has no CODEOWNERS file, so no reviewer is allowed")
        self._check_codeowners_errors(trusted)
        self._check_coverage(*codeowners)

    def _current_default_branch(self, trusted: str) -> str:
        """The default branch, of which R must be the head on GitHub now.

        The rulesets and permissions are read as they are now, so a build of
        an older commit, such as a re-run of an old Pages run, would pair them
        with that commit's CODEOWNERS and bring back the approvals a newer
        CODEOWNERS withdrew. When publishing, such a build stops with
        SupersededBuildError, and a failure to read the branch or its head
        stops with HeadCheckError: labelling every approval self-approved
        instead would let either downgrade the live site. Otherwise a
        checkout that is not the head refuses every approval, saying why, and
        a failed lookup leaves every approval unchecked.
        """

        try:
            default = self._default_branch()
            head = self._head(default)
        except (_Refused, ApprovalError, _BudgetSpent) as exc:
            why = f"the budget of {self.budget} GitHub API requests was spent" if isinstance(exc, _BudgetSpent) else exc
            message = f"cannot tell whether {self.trusted_ref} is the head of the default branch on GitHub: {why}"
            if self.publishing:
                raise HeadCheckError(message) from exc
            raise (_Unanswered if isinstance(exc, (_Unanswered, _BudgetSpent)) else ApprovalError)(message) from exc
        if head != trusted:
            reason = (
                f"{self.trusted_ref} is {trusted[:12]}, not {head[:12]}, the head of {default} on GitHub; "
                "approvals are authenticated only at the head of the default branch"
            )
            if not self.publishing:
                raise _Refused(reason)
            raise SupersededBuildError(f"{reason}, so this build stops rather than render every approval self-approved")
        return default

    def _head(self, default: str) -> str:
        """The commit the default branch points to on GitHub."""

        found = self._get(f"/git/ref/heads/{urllib.parse.quote(default)}")
        if found is None:
            raise _Refused(f"GitHub finds no branch {default}, so {self.trusted_ref} cannot be shown to be its head")
        target = found.get("object") if isinstance(found, dict) else None
        head = target.get("sha") if isinstance(target, dict) and target.get("type") == "commit" else None
        if not isinstance(found, dict) or found.get("ref") != f"refs/heads/{default}" or not isinstance(head, str):
            raise ApprovalError(f"GitHub API GET of refs/heads/{default} did not name the commit it points to")
        return head.lower()

    def _check_codeowners_errors(self, trusted: str) -> None:
        """GitHub's own reading of CODEOWNERS at R finds nothing wrong.

        GitHub skips a line it cannot parse and ignores an owner it cannot
        use, such as an unknown user or a team without write access. The
        local parser only narrows what GitHub accepts, so any error refuses.
        """

        answer = self._get("/codeowners/errors", {"ref": trusted})
        if answer is None:
            raise _Refused(f"GitHub finds no CODEOWNERS file at {self.trusted_ref}, so it requires no code owner review")
        errors = answer.get("errors") if isinstance(answer, dict) else None
        if not isinstance(errors, list):
            raise ApprovalError("GitHub API GET /codeowners/errors did not return a list of errors")
        if errors:
            shown = "; ".join(_codeowners_error(error) for error in errors[:_UNCOVERED_SHOWN])
            more = f"; and {len(errors) - _UNCOVERED_SHOWN} more" if len(errors) > _UNCOVERED_SHOWN else ""
            raise _Refused(
                f"GitHub reports {len(errors)} error(s) in CODEOWNERS at {self.trusted_ref}, so it does not enforce "
                f"every line as written: {shown}{more}"
            )

    def _check_coverage(self, location: str, rules: tuple[CodeOwnersRule, ...]) -> None:
        """Every path that could exist has an owner GitHub enforces, which the rules alone show.

        The last matching rule decides, and ``*`` matches every path, so each
        path is decided by the last ``*`` rule or a later one; all of them must
        name an enforced owner. Checking only the files R tracks would miss
        one a pull request adds, such as a new workflow. Articles and cards get
        no exception: any pattern may match a directory, so none can be shown
        to match only Markdown, and the site copies other files under the
        blueprint.
        """

        default = next((rule for rule in reversed(rules) if rule.pattern == "*"), None)
        if default is None:
            raise _Refused(
                f"{location} at {self.trusted_ref} has no `*` rule, the only pattern the verifier reads as "
                "matching every path, so it cannot show that a file no rule matches, such as a new workflow, "
                "needs code owner review; give every path an owner with a first line like `* @owner`"
            )
        uncovered = [
            why for rule in rules if rule.line >= default.line and (why := self._uncovered_by(rule)) is not None
        ]
        if uncovered:
            shown = "; ".join(uncovered[:_UNCOVERED_SHOWN])
            more = f"; and {len(uncovered) - _UNCOVERED_SHOWN} more" if len(uncovered) > _UNCOVERED_SHOWN else ""
            raise _Refused(
                f"{location} at {self.trusted_ref} leaves the paths of {len(uncovered)} rule(s) without a code owner "
                f"GitHub enforces, so they can change without code owner review: {shown}{more}"
            )

    def _check_ruleset(self, default: str) -> None:
        """The default branch's active pull request rules turn on every one of ``_REVIEW_SETTINGS``.

        Rules from several rulesets add up, GitHub enforcing the strictest, so
        each setting may come from any of them, but only from a ruleset this
        verifier's token cannot bypass: a workflow whose token can bypass it
        can push to the default branch unreviewed. Human bypass actors cannot
        be read with a workflow token.
        """

        # A 404 means no rule applies to the branch, which refuses below.
        rules = self._once(
            ("rules", default),
            lambda: self._pages(f"/rules/branches/{urllib.parse.quote(default, safe='')}", missing_ok=True),
        )
        reviews = [rule for rule in rules if rule.get("type") == "pull_request"]
        if not any(_turns_on(rule, "require_code_owner_review") for rule in reviews):
            raise _Refused(
                f"no active ruleset on {default} has a pull request rule requiring code owner review, so a pull "
                "request can merge without its code owners; classic branch protection cannot be read with a "
                "workflow token and does not count"
            )
        held: list[dict] = []
        bypassed: list[str] = []
        for rule in reviews:
            why = self._bypassed(rule)
            if why is None:
                held.append(rule)
            else:
                bypassed.append(why)
        missing = [setting for setting in _REVIEW_SETTINGS if not any(_turns_on(rule, setting[0]) for rule in held)]
        if missing:
            unbypassable = " that this verifier's token cannot bypass" if bypassed else ""
            raise _Refused(
                f"no active ruleset on {default}{unbypassable} has a pull request rule with "
                + " or ".join(f"{label} ({name})" for name, label, _ in missing)
                + ", so "
                + "; and ".join(consequence for _, _, consequence in missing)
                + "".join(f"; {why}" for why in dict.fromkeys(bypassed))
            )

    def _bypassed(self, rule: dict) -> str | None:
        """Why a pull request rule may not hold against this verifier's token, or None when it does."""

        number = rule.get("ruleset_id")
        if not isinstance(number, int) or isinstance(number, bool):
            return "a pull request rule names no ruleset, so who can bypass it cannot be read"
        ruleset = self._once(("ruleset", number), lambda: self._get(f"/rulesets/{number}"))
        if not isinstance(ruleset, dict) or ruleset.get("id") != number:
            return f"ruleset {number} cannot be read, so who can bypass it cannot be either"
        if ruleset.get("enforcement") != "active":
            return f"ruleset {number} is not active"
        bypass = ruleset.get("current_user_can_bypass")
        if bypass != "never":
            return (
                f"ruleset {number} does not count, because GitHub says this verifier's token can bypass it "
                f"(current_user_can_bypass is {bypass!r}, not 'never')"
            )
        return None

    def _uncovered_by(self, rule: CodeOwnersRule) -> str | None:
        """Why the deciding rule leaves its paths without an enforced owner, or None."""

        if rule.problem is not None:
            return rule.problem
        # GitHub enforces a team only with write access, which a workflow token
        # cannot read; GitHub reports a team without it as an unknown owner,
        # which _check_codeowners_errors refuses. Only the repository owner's
        # teams can have access at all.
        organization = self._organization()
        teams = [owner for owner in rule.owners if _TEAM_OWNER.match(owner)]
        if any(team[1:].split("/", 1)[0].lower() == organization for team in teams):
            return None
        logins = individual_owners(rule.owners)
        if any(self._permission(login) in _WRITE_PERMISSIONS for login in logins):
            return None
        if not rule.owners:
            return f"line {rule.line} names no owner"
        reasons = []
        if logins:
            reasons.append(f"{', '.join('@' + login for login in logins)} cannot write")
        if teams:
            reasons.append(f"{', '.join(teams)} {'is' if len(teams) == 1 else 'are'} not a team of {organization}")
        if len(logins) + len(teams) < len(rule.owners):
            reasons.append("email owners cannot be verified")
        return f"line {rule.line}: {'; '.join(reasons)}"

    # 1. Which commits may have recorded the hash.

    def _introductions(self, root: Path, trusted: str, path: str, wanted: str) -> list[tuple[str, str | None]]:
        """Candidate commits, newest first, each with its first parent.

        The run is the unbroken stretch of first-parent commits ending at R in
        which p records H. Its oldest commit, whose first parent does not
        record H, is always a candidate; a newer one is a candidate when its
        own diff adds a ``review_approved`` line with H, as re-approving does.
        """

        if _value_at(root, trusted, path) != wanted:
            raise _Refused(f"{path} does not record this hash at {self.trusted_ref}")
        # First parents only: a merge counts as its own change of the file,
        # so neither a side branch's history nor a merge resolution hides it.
        log = _git(root, "--literal-pathspecs", "rev-list", "--first-parent", trusted, "--", path)
        if log.returncode != 0:
            raise ApprovalError(f"git rev-list failed for {path}: {log.stderr.decode('utf-8', 'replace').strip()}")
        commits = log.stdout.decode("ascii", "replace").split()
        candidates: list[tuple[str, str | None]] = []
        for commit in commits[:_MAX_HISTORY]:
            if _value_at(root, commit, path) != wanted:
                raise _Refused(f"the first-parent history of {path} is inconsistent at {commit[:12]}")
            parent = _parent(root, commit)
            if parent is None or _value_at(root, parent, path) != wanted:
                candidates.append((commit, parent))
                return candidates
            if _adds_approval_between(root, parent, commit, path, wanted):
                candidates.append((commit, parent))
        if len(commits) > _MAX_HISTORY:
            raise _Refused(f"more than {_MAX_HISTORY} first-parent commits changed {path} while it recorded this hash")
        raise _Refused(f"no first-parent commit of {self.trusted_ref} records this hash in {path}")

    # 5, first half. Who may approve, from CODEOWNERS.

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

    def _repository(self) -> dict:
        def fetch() -> dict:
            repository = self._get("")
            branch = repository.get("default_branch") if isinstance(repository, dict) else None
            if not isinstance(repository, dict) or not isinstance(branch, str) or not branch:
                raise ApprovalError("GitHub API GET of the repository did not name its default branch")
            return repository

        return self._once(("repository",), fetch)

    def _default_branch(self) -> str:
        return self._repository()["default_branch"]

    def _organization(self) -> str:
        """The repository owner's login, without case: the only account whose teams can own its files."""

        owner = _login(self._repository().get("owner"))
        if not _REVIEWER.match(owner):
            raise ApprovalError("GitHub API GET of the repository did not name its owner")
        return owner.lower()

    def _commit_pulls(self, commit: str) -> list[dict]:
        return self._once(("pulls", commit), lambda: self._pages(f"/commits/{commit}/pulls"))

    def _base_before(self, root: Path, commit: str, parent: str, number: int) -> str:
        """P's base B: the first of M's first-parent ancestors GitHub does not associate with P.

        A rebase merge puts each of P's commits on the default branch, every
        one associated with P, so M's first parent may be a commit P wrote,
        with a CODEOWNERS P chose. P's ``base.sha`` does not help: GitHub
        sets it to the base branch as of P's last update, which need not be
        the commit P landed on.
        """

        here, _ = self._identity()
        base: str | None = parent
        for _ in range(_MAX_PULL_COMMITS):
            if base is None:
                raise _Refused(
                    f"#{number} introduced every first-parent ancestor of {commit[:12]}, "
                    "so nothing shows the code owners before it"
                )
            numbers = [pull.get("number") for pull in self._commit_pulls(base) if not _other_repository(pull, here)]
            if not all(isinstance(listed, int) for listed in numbers):
                raise ApprovalError(f"GitHub listed a pull request without a number for commit {base[:12]}")
            if number not in numbers:
                return base
            base = _parent(root, base)
        raise _Refused(
            f"#{number} landed more than {_MAX_PULL_COMMITS} commits before {commit[:12]}, "
            "so its base cannot be found"
        )

    def _merged_pull(self, commit: str) -> dict:
        default = self._default_branch()
        here, _ = self._identity()
        # GitHub also lists pull requests into other repositories that contain
        # the commit, such as the upstream one of a fork; none landed it here.
        pulls = [pull for pull in self._commit_pulls(commit) if not _other_repository(pull, here)]
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

    def _identity(self) -> tuple[int, str]:
        repository = self._repository()
        here, name = repository.get("id"), repository.get("full_name")
        if not isinstance(here, int) or not isinstance(name, str) or not _REPOSITORY.match(name):
            raise ApprovalError("GitHub API GET of the repository did not give its id and full name")
        return here, name

    def _check_same_repository(self, pull: dict) -> None:
        """P comes from a branch of this repository and targets it.

        Step 6 cannot tie a fork's run to P, so a fork is refused first, in
        the gate too, before anything about it is read.
        """

        number = pull["number"]
        here, name = self._identity()
        source = pull["head"].get("repo")
        if not isinstance(source, dict) or source.get("id") != here:
            fork = source.get("full_name") if isinstance(source, dict) else None
            raise _Refused(
                f"#{number} comes from {'a deleted repository' if fork is None else repr(fork)}, not a branch of "
                f"{name}, and a workflow token cannot tie a fork's Actions run to its pull request; record "
                "approvals from a branch of this repository"
            )
        target = pull["base"].get("repo")
        if not isinstance(target, dict) or target.get("id") != here:
            raise _Refused(f"#{number} does not target {name}")

    # 3. That the pull request changed nothing a reviewer of articles does not own.

    def _check_content_only(self, pull: dict) -> None:
        number = pull["number"]
        # _files refuses a list at GitHub's cap, so a shorter one is complete without comparing
        # changed_files, which the pull requests GitHub lists for a commit leave out.
        files = self._files(number)
        # A rename moves a file out of where it was owned as much as into where it is.
        names = [name for item in files for name in (item.get("filename"), item.get("previous_filename", ""))]
        content = {name for name in names if isinstance(name, str) and _is_content(name, self._blueprint)}
        other = sorted({str(name) for name in names if name != "" and name not in content})
        if other:
            shown = ", ".join(other[:5]) + (f", and {len(other) - 5} more" if len(other) > 5 else "")
            raise _Refused(
                f"#{number} changes {shown}, not only articles and read-back cards; record approvals in a pull "
                "request that changes only articles and read-back cards"
            )

    # 4. That the pull request visibly recorded the hash.

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

    # 5, second half. Who approved it.

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
            elif (permission := self._permission(login)) not in _WRITE_PERMISSIONS:
                notes.append(
                    f"@{login} approved #{number} but GitHub gives them {permission or 'no'} permission on the "
                    "repository, not write"
                )
            elif key in self._writers(number):
                notes.append(f"@{login} approved #{number} but authored or committed one of its commits")
            else:
                return login, review
        if not latest:
            notes.append(f"#{number} has no review")
        raise _Refused("; ".join(notes) or f"#{number} has no approving review by a code owner of {path}")

    # 6. That the hash was current where it was approved.

    def _check_verified(self, pull: dict) -> None:
        """A successful verify run that GitHub ties to P, and to P alone.

        GitHub lists a run's pull requests only while they are open, so after
        the merge a run is tied to P by its head branch: P's branch, in this
        repository, which no other pull request ever used, while P never
        changed its base branch. The list also names pull requests into other
        repositories from that branch, such as a fork's, which anyone can
        open; none of them can be the pull request a run here belongs to.
        """

        number, head = pull["number"], pull["head"]["sha"]
        workflow = self.verify_workflow
        here, name = self._identity()
        branch = pull["head"].get("ref")
        if not isinstance(branch, str) or not branch:
            raise ApprovalError(f"GitHub returned #{number} without its head branch")
        owner = name.split("/", 1)[0]
        heads = self._once(
            ("heads", branch), lambda: self._pages("/pulls", {"state": "all", "head": f"{owner}:{branch}"})
        )
        numbers = sorted({_number(item.get("number")) for item in heads})
        if numbers != [number]:
            others = ", ".join(f"#{other}" for other in numbers if other != number) or "none listed"
            raise _Refused(
                f"the branch {branch!r} of #{number} also headed other pull requests ({others}), so a run on it "
                f"cannot be tied to #{number}; record approvals from a branch no other pull request used"
            )
        events = self._once(("events", number), lambda: self._pages(f"/issues/{number}/events"))
        if any(event.get("event") == "base_ref_changed" for event in events):
            raise _Refused(
                f"#{number} changed its base branch, so its verify run may have checked it against another branch"
            )
        runs = self._once(
            ("runs", head),
            lambda: self._pages("/actions/runs", {"head_sha": head, "event": "pull_request"}, key="workflow_runs"),
        )
        default = self._default_branch()
        if not any(
            _succeeded(run, workflow, head) and _ran_for(run, number, branch, here, default) for run in runs
        ):
            raise _Refused(
                f"{workflow} has no successful pull_request run on the head {head[:12]} of #{number} from its "
                f"branch {branch!r}, so nothing shows this hash was current there"
            )

    # Requests.

    def _files(self, number: int) -> list[dict]:
        def fetch() -> list[dict]:
            files = self._pages(f"/pulls/{number}/files", limit=_MAX_PULL_FILES)
            if len(files) >= _MAX_PULL_FILES:
                raise ApprovalError(
                    f"#{number} has {len(files)} files listed and GitHub lists at most {_MAX_PULL_FILES}, "
                    "so what it changes cannot be read"
                )
            return files

        return self._once(("files", number), fetch)

    def _permission(self, login: str) -> str | None:
        """The login's permission on the repository; None for someone who is not a collaborator."""

        def fetch() -> str | None:
            answer = self._get(f"/collaborators/{urllib.parse.quote(login, safe='')}/permission")
            if answer is None:
                return None
            permission = answer.get("permission") if isinstance(answer, dict) else None
            if not isinstance(permission, str):
                raise ApprovalError(f"GitHub API GET of @{login}'s permission did not name one")
            return permission

        return self._once(("permission", login.lower()), fetch)

    def _writers(self, number: int) -> frozenset[str]:
        """Everyone GitHub names as the author or committer of one of the pull request's commits."""

        def fetch() -> frozenset[str]:
            commits = self._pages(f"/pulls/{number}/commits")
            if len(commits) >= _MAX_PULL_COMMITS:
                raise ApprovalError(
                    f"#{number} has {len(commits)} commits listed and GitHub lists at most {_MAX_PULL_COMMITS}, "
                    "so who wrote them cannot be checked"
                )
            writers: set[str] = set()
            for commit in commits:
                for role in ("author", "committer"):
                    login = _login(commit.get(role))
                    if not login:
                        sha = commit.get("sha")
                        raise ApprovalError(
                            f"commit {sha[:12] if isinstance(sha, str) else '?'} of #{number} has a {role} GitHub "
                            "links to no account, so no reviewer can be shown not to have written it"
                        )
                    writers.add(login.lower())
            return frozenset(writers)

        return self._once(("writers", number), fetch)

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
        if self.requests >= self.budget:
            raise _BudgetSpent
        self.requests += 1
        try:
            return self.client.get(path, query)
        except ApprovalError as exc:
            raise _Unanswered(str(exc)) from exc

    def _pages(
        self,
        path: str,
        query: Mapping[str, str | int] | None = None,
        *,
        key: str | None = None,
        missing_ok: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        """Every entry of a listing, or its first ``limit`` entries or more when it has that many.

        A 404 on the first page is an empty list only with ``missing_ok``,
        for a listing where it means nothing exists in the whole repository;
        anywhere else it is an answer GitHub should not give, and fails.
        """

        items: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._get(path, {**(query or {}), "per_page": _PAGE_SIZE, "page": page})
            if batch is None and page == 1 and missing_ok:
                return items
            if batch is None and page == 1:
                raise ApprovalError(f"GitHub API GET {path} found nothing (HTTP 404), so the list cannot be read")
            if batch is None:
                # The list shrank between pages, which a later run may read whole.
                raise _Unanswered(f"GitHub API GET {path} found no page {page}, so the list is incomplete")
            if key is not None:
                batch = batch.get(key) if isinstance(batch, dict) else None
            if not isinstance(batch, list):
                raise ApprovalError(f"GitHub API GET {path} did not return a list")
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < _PAGE_SIZE or (limit is not None and len(items) >= limit):
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
    if not _AUTHOR.match(_login(pull.get("user"))):
        raise ApprovalError(f"GitHub returned #{number} without its author, so no reviewer can be shown not to be them")
    return pull


def _turns_on(rule: dict, name: str) -> bool:
    """Whether a rule is a pull request rule whose parameter ``name`` is true."""

    parameters = rule.get("parameters")
    return rule.get("type") == "pull_request" and isinstance(parameters, dict) and parameters.get(name) is True


def _codeowners_error(error: object) -> str:
    """One entry of GET /codeowners/errors, as path:line and its kind."""

    entry = error if isinstance(error, dict) else {}
    path, line, kind = entry.get("path"), entry.get("line"), entry.get("kind")
    return (
        f"{path if isinstance(path, str) else 'CODEOWNERS'}:{line if isinstance(line, int) else '?'} "
        f"{kind if isinstance(kind, str) else 'error'}"
    )


def _is_content(path: str, blueprint: str) -> bool:
    """Whether a repository path is an article or a read-back card: Markdown under roadmap/ or readbacks/."""

    parts = PurePosixPath(path).parts
    prefix = PurePosixPath(blueprint).parts if blueprint else ()
    return (
        PurePosixPath(path).suffix == ".md"
        and len(parts) > len(prefix) + 1
        and parts[: len(prefix)] == prefix
        and parts[len(prefix)] in {"roadmap", READBACKS_DIR}
    )


def _ran_for(run: dict, number: int, branch: str, repository: int, default: str) -> bool:
    """Whether a run is on ``branch`` of this repository and names no pull request but ``number`` into ``default``."""

    source = run.get("head_repository")
    if run.get("head_branch") != branch or not isinstance(source, dict) or source.get("id") != repository:
        return False
    listed = run.get("pull_requests")
    if not isinstance(listed, list):
        return False
    for entry in listed:
        # A pull_request run belongs to a pull request into the repository it
        # runs in, so one into another repository, such as a fork's pull
        # request from this branch, cannot be the run's and is skipped.
        if isinstance(entry, dict) and _other_repository(entry, repository):
            continue
        base = entry.get("base") if isinstance(entry, dict) else None
        target = base.get("repo") if isinstance(base, dict) else None
        if (
            not isinstance(target, dict)
            or entry.get("number") != number
            or base.get("ref") != default
            or target.get("id") != repository
        ):
            return False
    return True


def _other_repository(pull: dict, repository: int) -> bool:
    """Whether GitHub names a base repository for the pull request, and it is not ``repository``."""

    base = pull.get("base")
    target = base.get("repo") if isinstance(base, dict) else None
    here = target.get("id") if isinstance(target, dict) else None
    return isinstance(here, int) and here != repository


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


def _adds_approval_between(root: Path, parent: str, commit: str, path: str, wanted: str) -> bool:
    """Whether ``commit``'s own diff of ``path`` adds a frontmatter line recording ``wanted``."""

    diff = _git(
        root, "--literal-pathspecs", "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
        "-U0", parent, commit, "--", path,
    )
    text = _show(root, commit, path)
    if diff.returncode != 0 or text is None:
        return False
    rows = diff.stdout.decode("utf-8", "replace").split("\n")
    start = next((index for index, row in enumerate(rows) if row.startswith("@@")), len(rows))
    hunks = [row for row in rows[start:] if row]
    counts = {
        "additions": sum(row.startswith("+") for row in hunks),
        "deletions": sum(row.startswith("-") for row in hunks),
    }
    added = _added_lines("\n".join(hunks), counts)
    return added is not None and _adds_approval(added, text, wanted)


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
    "HeadCheckError",
    "SupersededBuildError",
    "approval_statuses",
    "approvals_at",
    "code_owners",
    "current_approvals",
    "parse_codeowners",
]
