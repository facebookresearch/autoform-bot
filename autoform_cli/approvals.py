"""Whether repository policy guards a statement approval, as opposed to whether it is current.

``review_approved`` records the hash of the complete review surface. Matching
the current hash shows only that nothing changed since someone wrote it down;
``autoform review check`` prints that hash, so anyone can paste it.

``review_approved`` is thus a drift checksum. Who may change it is repository
policy: a CODEOWNERS rule giving ``blueprint/`` a code owner, under a ruleset
that requires code-owner review and approval of the most recent push (see
README.md). ``GitHubPolicyVerifier`` checks that policy once per run, on the
default branch as GitHub holds it now. While it holds, every current approval
is labelled policy-enforced; otherwise, or without a check, self-approved. The
label describes the repository, not who approved one hash.
"""

from __future__ import annotations

import base64
import json
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from .review import ReviewState


DEFAULT_GITHUB_API_URL = "https://api.github.com"
CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
POLICY_ENFORCED = "policy-enforced"
SELF_APPROVED = "self-approved"
_PAGE_SIZE = 100
_MAX_PAGES = 30
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_UNCOVERED_SHOWN = 10
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
# GitHub ignores a code owner without write access; these permissions have it.
_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
# Underscores appear in Enterprise Managed User logins such as octocat_acme.
_LOGIN = r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?"
_USER_OWNER = re.compile(rf"@{_LOGIN}\Z")
_TEAM_OWNER = re.compile(rf"@{_LOGIN}/[A-Za-z0-9_.-]+\Z")
_EMAIL_OWNER = re.compile(r"[^@\s]+@[^@\s]+\Z")
_TOKEN_SEPARATOR = re.compile(r"[ \t]+")
_T = TypeVar("_T")


def _printable(text: str) -> str:
    """``text`` with each character that is not printable escaped as Python writes it, such as ``\\n``.

    Reasons quote CODEOWNERS lines and GitHub's own answers, which a pull
    request author can choose. A newline in one, printed to a CI log, could
    start a line with ``::``, which GitHub Actions runs as a workflow command.
    The runner also runs the older ``##[command]`` form anywhere in a line, so
    the second ``#`` of each ``##[`` is written ``\\x23``. Every reason the
    verifier records and every ApprovalError message passes through here.
    """

    escaped = "".join(char if char.isprintable() else char.encode("unicode_escape").decode("ascii") for char in text)
    return escaped.replace("##[", "#\\x23[")


class ApprovalError(ValueError):
    """The repository policy could not be read, so no approval may be labelled."""

    def __init__(self, message: str) -> None:
        message = _printable(message)
        self.issues = (message,)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ApprovalStatus:
    """One current approval, and whether the repository policy enforced it."""

    node_id: str
    review_hash: str
    enforced: bool = False
    #: Why the policy check found the policy not holding; None when it held or none ran.
    reason: str | None = None

    @property
    def label(self) -> str:
        return POLICY_ENFORCED if self.enforced else SELF_APPROVED


def approval_statuses(
    states: Mapping[str, ReviewState],
    verifier: GitHubPolicyVerifier | None = None,
) -> dict[str, ApprovalStatus]:
    """Label each article whose ``review_approved`` is its current review hash.

    Without a verifier every approval is self-approved, with no reason, and no
    network is used. With one, the policy is checked once for all approvals,
    and not at all when there are none.
    """

    approved = {node_id: state.review_hash for node_id, state in sorted(states.items()) if state.status == "approved"}
    reason = verifier.check() if verifier is not None and approved else None
    return {
        node_id: ApprovalStatus(node_id, review_hash, enforced=verifier is not None and reason is None, reason=reason)
        for node_id, review_hash in approved.items()
    }


# CODEOWNERS


@dataclass(frozen=True, slots=True)
class CodeOwnersRule:
    """One CODEOWNERS line: a path pattern and the owners it assigns.

    ``problem`` says why GitHub's reading of the line cannot be decided here.
    """

    line: int
    pattern: str
    owners: tuple[str, ...]
    problem: str | None = None


def parse_codeowners(text: str, *, source: str = "CODEOWNERS") -> tuple[CodeOwnersRule, ...]:
    """Parse CODEOWNERS as GitHub splits it, marking lines that cannot be decided.

    Lines end at a line feed, without a trailing carriage return, and tokens
    are separated by spaces and tabs. A line holding any other control or
    separator character, or an owner that is not @user, @org/team, or an
    email, could be read differently by GitHub; it is kept as an undecidable
    rule rather than guessed, because a misread rule could leave a path
    without the owner it seems to have.
    """

    rules: list[CodeOwnersRule] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        location = f"{source}:{number}"
        hidden = next((char for char in line if char not in " \t" and unicodedata.category(char)[0] in "CZ"), None)
        if hidden is not None:
            problem = f"{location} contains U+{ord(hidden):04X}, which GitHub may read as a line or token break"
            rules.append(CodeOwnersRule(number, line, (), problem))
            continue
        tokens = _TOKEN_SEPARATOR.split(line.strip(" \t"))
        if not tokens[0] or tokens[0].startswith("#"):
            continue
        owners, problem = [], None
        for token in tokens[1:]:
            if token.startswith("#"):
                break
            if _USER_OWNER.match(token) or _TEAM_OWNER.match(token) or _EMAIL_OWNER.match(token):
                owners.append(token)
            elif problem is None:
                problem = f"{location}: unsupported owner {token!r}; use @user, @org/team, or an email"
        rules.append(CodeOwnersRule(number, tokens[0], tuple(owners), problem))
    return tuple(rules)


# GitHub


class GitHubClient:
    """A minimal read-only GitHub REST client over the standard library.

    Paths are relative to ``/repos/{repository}``. A 404 is returned as None;
    any other failure, such as a 5xx, a 403, a timeout, or an oversized or
    malformed answer, raises ``ApprovalError``. The token is never forwarded
    across a redirect.
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
                "User-Agent": "autoform-review-policy",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        endpoint = path or "of the repository"
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read(500).decode("utf-8", "replace").strip()
            raise ApprovalError(f"GitHub API GET {endpoint} failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ApprovalError(f"GitHub API GET {endpoint} failed: {exc}") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ApprovalError(f"GitHub API GET {endpoint} returned more than {_MAX_RESPONSE_BYTES} bytes")
        try:
            return json.loads(body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ApprovalError(f"GitHub API GET {endpoint} returned malformed JSON") from exc


class _Refused(Exception):
    """The repository policy does not hold, for the stated reason."""


class GitHubPolicyVerifier:
    """Whether the repository's policy makes every change to the default branch need a code owner's approval.

    autoform_cli/README.md lists every condition and why it is needed. All of
    them are read from GitHub as the default branch is now, not from the
    checkout: rulesets this verifier's token cannot bypass must require code
    owner review, dismiss stale approvals and require approval of the most
    recent push, and CODEOWNERS must give every path an owner GitHub enforces.
    ``check`` returns why the policy does not hold, or None when it does; a
    request that fails raises ``ApprovalError`` rather than label anything.
    """

    def __init__(self, client: GitHubClient) -> None:
        self.client = client
        # Why the last check found the policy not holding; None when it held or no check ran.
        self.reason: str | None = None
        self._cache: dict[tuple[object, ...], object] = {}

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> GitHubPolicyVerifier:
        """Build from the variables GitHub Actions provides, naming any that are missing."""

        env = os.environ if environ is None else environ
        missing = [name for name in ("GITHUB_TOKEN", "GITHUB_REPOSITORY") if not env.get(name)]
        if missing:
            raise ApprovalError(
                f"the GitHub policy check needs {' and '.join(missing)} in the environment "
                "(a token that can read the repository, and the repository as owner/name)"
            )
        api_url = env.get("GITHUB_API_URL") or DEFAULT_GITHUB_API_URL
        return cls(GitHubClient(env["GITHUB_TOKEN"], env["GITHUB_REPOSITORY"], api_url=api_url))

    def check(self) -> str | None:
        try:
            self._check()
        except _Refused as exc:
            self.reason = _printable(str(exc))
        else:
            self.reason = None
        return self.reason

    def _check(self) -> None:
        default = self._repository()["default_branch"]
        self._check_ruleset(default)
        location, rules = self._codeowners(default)
        self._check_codeowners_errors(default)
        self._check_coverage(default, location, rules)

    def _check_ruleset(self, default: str) -> None:
        """The default branch's active pull request rules turn on every one of ``_REVIEW_SETTINGS``.

        Rules from several rulesets add up, GitHub enforcing the strictest, so
        each setting may come from any of them, but only from a ruleset this
        verifier's token cannot bypass: a workflow whose token can bypass it
        can push to the default branch unreviewed. Human bypass actors cannot
        be read with a workflow token.
        """

        reviews = [rule for rule in self._rules(default) if rule.get("type") == "pull_request"]
        if not any(_turns_on(rule, "require_code_owner_review") for rule in reviews):
            raise _Refused(
                f"no active ruleset on {default} has a pull request rule requiring code owner review, so a pull "
                "request can merge without its code owners; classic branch protection cannot be read with a "
                "workflow token and does not count"
            )
        whys = [(rule, self._bypassed(rule)) for rule in reviews]
        held = [rule for rule, why in whys if why is None]
        bypassed = [why for _, why in whys if why is not None]
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
        ruleset = self._once(("ruleset", number), lambda: self.client.get(f"/rulesets/{number}"))
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

    def _codeowners(self, default: str) -> tuple[str, tuple[CodeOwnersRule, ...]]:
        """The CODEOWNERS file GitHub uses on ``default``: the first location that exists.

        One that cannot be read is refused rather than passed over for the
        next, since GitHub would not pass over it either.
        """

        for location in CODEOWNERS_LOCATIONS:
            answer = self.client.get(f"/contents/{location}", {"ref": default})
            if answer is None:
                continue
            # The contents API leaves out the content of a file over 1 MB.
            if not (isinstance(answer, dict) and answer.get("type") == "file" and answer.get("encoding") == "base64"):
                raise _Refused(f"{location} on {default} cannot be read as a file of at most 1 MB")
            try:
                text = base64.b64decode(str(answer.get("content", ""))).decode("utf-8")
            except (ValueError, UnicodeError):
                raise _Refused(f"{location} on {default} is not UTF-8, so the code owners it names cannot be decided") from None
            return location, parse_codeowners(text, source=location)
        raise _Refused(f"{default} has no CODEOWNERS file, so no change needs a code owner's review")

    def _check_codeowners_errors(self, default: str) -> None:
        """GitHub's own reading of CODEOWNERS on the default branch finds nothing wrong.

        GitHub skips a line it cannot parse and ignores an owner it cannot
        use, such as an unknown user or a team without write access. The
        local parser only narrows what GitHub accepts, so any error refuses.
        """

        answer = self.client.get("/codeowners/errors", {"ref": default})
        if answer is None:
            raise _Refused(f"GitHub finds no CODEOWNERS file on {default}, so it requires no code owner review")
        errors = answer.get("errors") if isinstance(answer, dict) else None
        if not isinstance(errors, list):
            raise ApprovalError("GitHub API GET /codeowners/errors did not return a list of errors")
        if errors:
            raise _Refused(
                f"GitHub reports {len(errors)} error(s) in CODEOWNERS on {default}, so it does not enforce "
                f"every line as written: {_first_ten([_codeowners_error(error) for error in errors])}"
            )

    def _check_coverage(self, default: str, location: str, rules: tuple[CodeOwnersRule, ...]) -> None:
        """Every path that could exist has an owner GitHub enforces, which the rules alone show.

        The last matching rule decides, and ``*`` matches every path, so each
        path is decided by the last ``*`` rule or a later one; all of them must
        name an enforced owner. Checking only the files the branch tracks would
        miss one a pull request adds, such as a new workflow. The blueprint
        gets no exception: its articles and cards are what approvals rest on.
        """

        catch_all = next((rule for rule in reversed(rules) if rule.pattern == "*"), None)
        if catch_all is None:
            raise _Refused(
                f"{location} on {default} has no `*` rule, the only pattern the verifier reads as "
                "matching every path, so it cannot show that a file no rule matches, such as a new workflow, "
                "needs code owner review; give every path an owner with a first line like `* @owner`"
            )
        uncovered = [
            why for rule in rules if rule.line >= catch_all.line and (why := self._uncovered_by(rule)) is not None
        ]
        if uncovered:
            raise _Refused(
                f"{location} on {default} leaves the paths of {len(uncovered)} rule(s) without a code owner "
                f"GitHub enforces, so they can change without code owner review: {_first_ten(uncovered)}"
            )

    def _uncovered_by(self, rule: CodeOwnersRule) -> str | None:
        """Why a deciding rule leaves its paths without an enforced owner, or None."""

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
        logins = [owner[1:] for owner in rule.owners if _USER_OWNER.match(owner)]
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

    # Requests.

    def _repository(self) -> dict:
        def fetch() -> dict:
            repository = self.client.get("")
            branch = repository.get("default_branch") if isinstance(repository, dict) else None
            if not isinstance(repository, dict) or not isinstance(branch, str) or not branch:
                raise ApprovalError("GitHub API GET of the repository did not name its default branch")
            return repository

        return self._once(("repository",), fetch)

    def _organization(self) -> str:
        """The repository owner's login, without case: the only account whose teams can own its files."""

        owner = self._repository().get("owner")
        login = owner.get("login") if isinstance(owner, dict) else None
        if not isinstance(login, str) or not re.fullmatch(_LOGIN, login):
            raise ApprovalError("GitHub API GET of the repository did not name its owner")
        return login.lower()

    def _permission(self, login: str) -> str | None:
        """The login's permission on the repository; None for someone who is not a collaborator."""

        def fetch() -> str | None:
            answer = self.client.get(f"/collaborators/{urllib.parse.quote(login, safe='')}/permission")
            if answer is None:
                return None
            permission = answer.get("permission") if isinstance(answer, dict) else None
            if not isinstance(permission, str):
                raise ApprovalError(f"GitHub API GET of @{login}'s permission did not name one")
            return permission

        return self._once(("permission", login.lower()), fetch)

    def _rules(self, default: str) -> list[dict]:
        """The active rules on ``default``, across pages; GitHub answers 404 when no rule applies."""

        path = f"/rules/branches/{urllib.parse.quote(default, safe='')}"
        rules: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self.client.get(path, {"per_page": _PAGE_SIZE, "page": page})
            if batch is None and page == 1:
                return rules
            if not isinstance(batch, list):
                raise ApprovalError(f"GitHub API GET {path} did not return a list")
            rules.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < _PAGE_SIZE:
                return rules
        raise ApprovalError(f"GitHub API GET {path} has more than {_MAX_PAGES * _PAGE_SIZE} entries")

    def _once(self, key: tuple[object, ...], fetch: Callable[[], _T]) -> _T:
        if key not in self._cache:
            self._cache[key] = fetch()
        return self._cache[key]  # type: ignore[return-value]


def _turns_on(rule: dict, name: str) -> bool:
    """Whether a rule is a pull request rule whose parameter ``name`` is true."""

    parameters = rule.get("parameters")
    return rule.get("type") == "pull_request" and isinstance(parameters, dict) and parameters.get(name) is True


def _first_ten(reasons: list[str]) -> str:
    more = len(reasons) - _UNCOVERED_SHOWN
    return "; ".join(reasons[:_UNCOVERED_SHOWN]) + (f"; and {more} more" if more > 0 else "")


def _codeowners_error(error: object) -> str:
    """One entry of GET /codeowners/errors, as path:line and its kind."""

    entry = error if isinstance(error, dict) else {}
    path, line, kind = entry.get("path"), entry.get("line"), entry.get("kind")
    return (
        f"{path if isinstance(path, str) else 'CODEOWNERS'}:{line if isinstance(line, int) else '?'} "
        f"{kind if isinstance(kind, str) else 'error'}"
    )


__all__ = [
    "ApprovalError",
    "ApprovalStatus",
    "GitHubClient",
    "GitHubPolicyVerifier",
    "approval_statuses",
    "parse_codeowners",
]
