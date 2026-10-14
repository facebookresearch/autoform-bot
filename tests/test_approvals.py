"""The repository-policy check behind policy-enforced approvals."""

from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from autoform_cli import approvals
from autoform_cli.__main__ import main
from autoform_cli.approvals import ApprovalError, GitHubClient, GitHubPolicyVerifier, approval_statuses
from autoform_cli.review import ReviewState
from tests.test_review_cli import _approved_articles, _Extraction


_HASH = "sha256:" + "a" * 64
# Code owner review protects only what has an owner, so the fixture owns everything.
_CODEOWNERS = "* @owner\nblueprint/ @alice\n"


class FakeGitHub:
    """Answers GitHub's REST API for a repository set up as the README asks.

    A ruleset this token cannot bypass requires code owner review on main,
    dismisses stale approvals, and requires approval of the last push;
    .github/CODEOWNERS owns every path, and everyone named can write.
    """

    api_url = "https://api.github.com"

    def __init__(self, codeowners: str | bytes | None = _CODEOWNERS) -> None:
        self.repository: dict = {"id": 1, "full_name": "owner/project", "owner": {"login": "owner"}}
        self.calls: list[str] = []
        # path -> what GET answers instead, raised if an exception.
        self.answers: dict[str, object] = {}
        # None answers 404, as for a branch no rule applies to.
        self.rules: list[dict] | None = [
            {
                "type": "pull_request",
                "ruleset_id": 1,
                "parameters": {
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": True,
                    "require_last_push_approval": True,
                    "required_approving_review_count": 1,
                },
            }
        ]
        # ruleset id -> GET /rulesets/{id}; a missing id answers 404.
        self.rulesets: dict[int, dict] = {
            1: {"id": 1, "enforcement": "active", "current_user_can_bypass": "never"}
        }
        # location -> CODEOWNERS text, or a contents answer as GitHub gives it.
        self.files: dict[str, object] = {} if codeowners is None else {".github/CODEOWNERS": codeowners}
        # What GET /codeowners/errors lists; None answers 404, as for a branch without CODEOWNERS.
        self.codeowners_errors: object = []
        # login -> permission; None answers 404, as for someone who is not a collaborator.
        self.permissions: dict[str, str | None] = {}

    def get(self, path: str, query: dict | None = None) -> object | None:
        self.calls.append(path)
        if path in self.answers:
            answer = self.answers[path]
            if isinstance(answer, BaseException):
                raise answer
            return answer
        query = dict(query or {})
        if path == "":
            return {**self.repository, "default_branch": "main"}
        if path == "/rules/branches/main":
            if self.rules is None:
                return None
            size, page = int(query["per_page"]), int(query["page"])
            return self.rules[(page - 1) * size : page * size]
        if path.startswith("/rulesets/"):
            return self.rulesets.get(int(path.rsplit("/", 1)[1]))
        if path.startswith("/contents/"):
            assert query == {"ref": "main"}
            found = self.files.get(path[len("/contents/") :])
            if found is None or isinstance(found, (dict, list)):
                return found
            data = found.encode() if isinstance(found, str) else found
            # GitHub wraps base64 content at 60 columns.
            return {"type": "file", "encoding": "base64", "content": base64.encodebytes(data).decode()}
        if path == "/codeowners/errors":
            assert query == {"ref": "main"}
            return None if self.codeowners_errors is None else {"errors": self.codeowners_errors}
        if path.startswith("/collaborators/") and path.endswith("/permission"):
            login = urllib.parse.unquote(path.split("/")[2])
            permission = self.permissions.get(login.lower(), "write")
            return None if permission is None else {"permission": permission}
        raise AssertionError(f"unexpected GitHub API request {path}")


def _reason(github: FakeGitHub) -> str | None:
    return GitHubPolicyVerifier(github).check()  # type: ignore[arg-type]


def _refused(github: FakeGitHub, *reasons: str) -> None:
    reason = _reason(github)
    assert reason is not None, "the policy held"
    for text in reasons:
        assert text in reason, reason


# What one check of a repository that holds the policy reads, whatever the number of approvals.
_CALLS = [
    "",
    "/rules/branches/main",
    "/rulesets/1",
    "/contents/.github/CODEOWNERS",
    "/codeowners/errors",
    "/collaborators/owner/permission",
    "/collaborators/alice/permission",
]


def _states(**approved: str | None) -> dict[str, ReviewState]:
    return {node_id: ReviewState(node_id, approval, _HASH, ()) for node_id, approval in approved.items()}


def test_the_policy_is_checked_once_and_labels_every_approval() -> None:
    github = FakeGitHub()
    verifier = GitHubPolicyVerifier(github)  # type: ignore[arg-type]

    statuses = approval_statuses(_states(b=_HASH, c=_HASH, d=None, e="sha256:" + "f" * 64), verifier)

    assert [(status.node_id, status.label, status.reason) for status in statuses.values()] == [
        ("b", "policy-enforced", None),
        ("c", "policy-enforced", None),
    ]
    assert github.calls == _CALLS
    github.calls.clear()
    assert approval_statuses(_states(d=None), verifier) == {}
    assert github.calls == []
    unchecked = approval_statuses(_states(b=_HASH))
    assert (unchecked["b"].label, unchecked["b"].enforced, unchecked["b"].reason) == ("self-approved", False, None)


# Rulesets.

_NO_RULESET = "no active ruleset on main has a pull request rule requiring code owner review"
_STALE = "Dismiss stale pull request approvals when new commits are pushed (dismiss_stale_reviews_on_push)"
_LAST_PUSH = "Require approval of the most recent reviewable push (require_last_push_approval)"
_CODE_OWNERS = "Require review from Code Owners (require_code_owner_review)"


@pytest.mark.parametrize(
    "rules",
    [
        None,
        [],
        [{"type": "pull_request", "parameters": {"require_code_owner_review": False}}],
        [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}],
        [{"type": "pull_request"}],
        [{"type": "pull_request", "parameters": {"require_code_owner_review": "true"}}],
        [{"type": "required_status_checks", "parameters": {"require_code_owner_review": True}}],
        [{"type": "deletion"}, {"type": "non_fast_forward"}],
    ],
)
def test_the_policy_needs_a_ruleset_requiring_code_owner_review(rules: list | None) -> None:
    github = FakeGitHub()
    github.rules = rules

    _refused(github, _NO_RULESET, "classic branch protection cannot be read with a workflow token")


def test_a_code_owner_rule_on_a_later_page_of_rules_counts() -> None:
    github = FakeGitHub()
    github.rules = [{"type": "deletion"}] * 100 + github.rules  # type: ignore[operator]

    assert _reason(github) is None


@pytest.mark.parametrize(
    ("parameters", "texts"),
    [
        (
            {"dismiss_stale_reviews_on_push": False},
            [_STALE, "an approval still counts after pushes its reviewer never saw"],
        ),
        (
            {"require_last_push_approval": False},
            [_LAST_PUSH, "a code owner can push to someone else's pull request and approve their own push"],
        ),
        ({"dismiss_stale_reviews_on_push": "true"}, [_STALE]),
        ({"require_last_push_approval": None}, [_LAST_PUSH]),
        (
            {"dismiss_stale_reviews_on_push": False, "require_last_push_approval": False},
            [f"{_STALE} or {_LAST_PUSH}", "pushes its reviewer never saw; and a code owner can push"],
        ),
    ],
)
def test_the_policy_needs_stale_approvals_dismissed_and_the_last_push_approved(parameters: dict, texts: list) -> None:
    """Repro: without these settings, an owner's approval of a typo fix still
    counts after the author pushes a CODEOWNERS line naming themselves."""

    github = FakeGitHub()
    github.rules[0]["parameters"].update(parameters)  # type: ignore[index]

    _refused(github, "no active ruleset on main has a pull request rule with ", *texts)


def test_the_review_settings_may_come_from_different_rulesets() -> None:
    """Deliberate guard: GitHub enforces the strictest of every ruleset's rules, so they add up."""

    github = FakeGitHub()
    github.rules[0]["parameters"].update(require_last_push_approval=False)  # type: ignore[index]
    github.rules.append(  # type: ignore[union-attr]
        {
            "type": "pull_request",
            "ruleset_id": 2,
            "parameters": {"require_code_owner_review": False, "require_last_push_approval": True},
        }
    )
    github.rulesets[2] = {**github.rulesets[1], "id": 2}

    assert _reason(github) is None


@pytest.mark.parametrize(
    ("ruleset", "reason"),
    [
        (
            {"current_user_can_bypass": "always"},
            "ruleset 1 does not count, because GitHub says this verifier's token can bypass it "
            "(current_user_can_bypass is 'always', not 'never')",
        ),
        ({"current_user_can_bypass": "pull_requests_only"}, "(current_user_can_bypass is 'pull_requests_only'"),
        ({"current_user_can_bypass": "exempt"}, "(current_user_can_bypass is 'exempt'"),
        ({"current_user_can_bypass": None}, "(current_user_can_bypass is None"),
        ({"enforcement": "evaluate"}, "ruleset 1 is not active"),
        ({"id": 2}, "ruleset 1 cannot be read"),
        (None, "ruleset 1 cannot be read"),
    ],
)
def test_a_ruleset_this_token_can_bypass_does_not_count(ruleset: dict | None, reason: str) -> None:
    """A workflow whose token can bypass the ruleset can push to main unreviewed."""

    github = FakeGitHub()
    if ruleset is None:
        del github.rulesets[1]
    else:
        github.rulesets[1].update(ruleset)

    _refused(
        github,
        "no active ruleset on main that this verifier's token cannot bypass has a pull request rule with "
        f"{_CODE_OWNERS} or {_STALE} or {_LAST_PUSH}, so a pull request can merge without its code owners",
        reason,
    )


@pytest.mark.parametrize("ruleset_id", [None, True], ids=["missing", "boolean"])
def test_a_pull_request_rule_that_names_no_ruleset_does_not_count(ruleset_id: object) -> None:
    """True == 1, so read as a number a boolean id would reuse ruleset 1's answer and count as held."""

    github = FakeGitHub()
    held = github.rules[0]  # type: ignore[index]
    only_owners = {**held["parameters"], "dismiss_stale_reviews_on_push": False, "require_last_push_approval": False}
    unnamed = {key: value for key, value in held.items() if key != "ruleset_id"}
    if ruleset_id is not None:
        unnamed["ruleset_id"] = ruleset_id
    github.rules = [{**held, "parameters": only_owners}, unnamed]

    _refused(github, "a pull request rule names no ruleset, so who can bypass it cannot be read")


def test_a_ruleset_the_token_cannot_bypass_counts_beside_one_it_can() -> None:
    """Deliberate guard: a bypassable ruleset is left out, not held against the others."""

    github = FakeGitHub()
    github.rules.insert(0, {**github.rules[0], "ruleset_id": 2})  # type: ignore[union-attr,index]
    github.rulesets[2] = {**github.rulesets[1], "id": 2, "current_user_can_bypass": "always"}

    assert _reason(github) is None


# CODEOWNERS coverage.

_UNCOVERED = (
    ".github/CODEOWNERS on main leaves the paths of 1 rule(s) without a code owner GitHub enforces, so they can "
    "change without code owner review"
)


@pytest.mark.parametrize(
    ("codeowners", "permissions", "uncovered"),
    [
        ("* @reader\nblueprint/ @alice\n", {"reader": "read"}, ": line 1: @reader cannot write"),
        ("* @triager\nblueprint/ @alice\n", {"triager": "triage"}, ": line 1: @triager cannot write"),
        ("* @gone\nblueprint/ @alice\n", {"gone": None}, ": line 1: @gone cannot write"),
        ("* owner@example.com\nblueprint/ @alice\n", {}, ": line 1: email owners cannot be verified"),
        ("*\nblueprint/ @alice\n", {}, ": line 1 names no owner"),
        ("* @org/maintainers\nblueprint/ @alice\n", {}, ": line 1: @org/maintainers is not a team of owner"),
        ("* @owner\n.github/ \u2028@owner\nblueprint/ @alice\n", {}, ".github/CODEOWNERS:2 contains U+2028"),
        ("* @owner\nblueprint/ \u00a0@alice\n", {}, ".github/CODEOWNERS:2 contains U+00A0"),
        ("* @owner\n.github/ @owner!\nblueprint/ @alice\n", {}, "unsupported owner '@owner!'"),
        ("* @owner\nblueprint/ @reader\nblueprint/roadmap/ @alice\n", {"reader": "read"}, ": line 2: @reader cannot"),
        # Unowned, it would let a pull request publish HTML on the site without code owner review.
        ("* @owner\nblueprint/ @alice\n/blueprint/roadmap/*.html\n", {}, ": line 3 names no owner"),
        (
            "* @owner\nblueprint/ @alice\n/docs/ @reader alice@example.com @org/docs\n",
            {"reader": "read"},
            ": line 3: @reader cannot write; @org/docs is not a team of owner; email owners cannot be verified",
        ),
    ],
)
def test_every_rule_from_the_last_catch_all_on_needs_a_code_owner_who_can_write(
    codeowners: str, permissions: dict, uncovered: str
) -> None:
    """Every path is decided by the last `*` rule or a later one, tracked or not."""

    github = FakeGitHub(codeowners)
    github.permissions.update(permissions)

    _refused(github, _UNCOVERED, uncovered)


@pytest.mark.parametrize(
    "codeowners",
    [
        # Repro: owning every tracked file by name leaves a new workflow unowned.
        "/.github/CODEOWNERS @owner\n/blueprint/README.md @owner\n/blueprint/roadmap/ @alice\n",
        "blueprint/ @alice\n",
        # Deliberate guard: only `*` is read as matching every path.
        "** @owner\nblueprint/ @alice\n",
        "/** @owner\nblueprint/ @alice\n",
    ],
)
def test_codeowners_without_a_star_rule_leave_new_files_unowned(codeowners: str) -> None:
    _refused(
        FakeGitHub(codeowners),
        ".github/CODEOWNERS on main has no `*` rule, the only pattern the verifier reads as matching every path, "
        "so it cannot show that a file no rule matches, such as a new workflow, needs code owner review",
        "give every path an owner with a first line like `* @owner`",
    )


@pytest.mark.parametrize(
    ("codeowners", "permissions"),
    [
        # Rules before the last `*` decide nothing.
        ("/docs/ @reader\n* @owner\nblueprint/ @alice\n", {"reader": "read"}),
        ("* @reader\n* @owner\nblueprint/ @alice\n", {"reader": "read"}),
        # A team of the repository's owner, whatever the case, or one owner who can write, covers a path.
        ("* @owner/maintainers\nblueprint/ @alice\n", {}),
        ("* @Owner/maintainers\nblueprint/ @alice\n", {}),
        ("* @org/maintainers @owner/maintainers\nblueprint/ @alice\n", {}),
        ("* @owner\nblueprint/ @alice\n", {"owner": "admin"}),
        ("* @owner\nblueprint/ @alice\n", {"owner": "maintain"}),
        ("* @reader @owner\nblueprint/ @alice\n", {"reader": "read"}),
        ("* owner@example.com @owner\nblueprint/ @alice\n", {}),
        # Enterprise Managed User logins hold underscores; comments and CRLF line ends are read as GitHub does.
        ("# owners\r\n* @octocat_acme # everyone\r\nblueprint/ @alice\r\n", {}),
    ],
)
def test_the_rules_that_do_cover_every_path(codeowners: str, permissions: dict) -> None:
    github = FakeGitHub(codeowners)
    github.permissions.update(permissions)

    assert _reason(github) is None


def test_a_team_of_an_owner_whose_login_has_capitals_covers_every_path() -> None:
    """GitHub compares logins without case, and owners such as GoogleCloudPlatform are mixed case."""

    github = FakeGitHub("* @owner/maintainers\nblueprint/ @alice\n")
    github.repository["owner"] = {"login": "Owner"}

    assert _reason(github) is None


def test_the_uncovered_rules_are_named_ten_at_a_time() -> None:
    github = FakeGitHub(_CODEOWNERS + "".join(f"/Lib{index:02d}.lean @reader{index:02d}\n" for index in range(12)))
    github.permissions.update({f"reader{index:02d}": "read" for index in range(12)})

    reason = _reason(github) or ""
    assert "leaves the paths of 12 rule(s) without a code owner" in reason
    assert "line 12: @reader09 cannot write; and 2 more" in reason
    assert "@reader10" not in reason


def test_github_reads_the_first_codeowners_location_that_exists() -> None:
    github = FakeGitHub(None)
    github.files = {"CODEOWNERS": "* @reader\n", "docs/CODEOWNERS": _CODEOWNERS}
    github.permissions["reader"] = "read"

    _refused(github, "CODEOWNERS on main leaves the paths of 1 rule(s)", "@reader cannot write")
    assert github.calls[3:5] == ["/contents/.github/CODEOWNERS", "/contents/CODEOWNERS"]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (b"* @owner\n\xff\n", ".github/CODEOWNERS on main is not UTF-8"),
        # The contents API leaves out the content of a file over 1 MB; GitHub does not fall through to the next.
        ({"type": "file", "encoding": "none", "content": ""}, ".github/CODEOWNERS on main cannot be read"),
        ([{"type": "file"}], ".github/CODEOWNERS on main cannot be read"),
    ],
    ids=["not-utf8", "too-large", "directory"],
)
def test_a_codeowners_file_that_cannot_be_read_owns_nothing(answer: object, reason: str) -> None:
    github = FakeGitHub(None)
    github.files = {".github/CODEOWNERS": answer, "CODEOWNERS": _CODEOWNERS}

    _refused(github, reason)


def test_no_codeowners_file_owns_nothing() -> None:
    _refused(FakeGitHub(None), "main has no CODEOWNERS file, so no change needs a code owner's review")


# GitHub's reading of CODEOWNERS.


@pytest.mark.parametrize(
    ("codeowners", "line", "error"),
    [
        (_CODEOWNERS, 2, "Invalid pattern"),
        (_CODEOWNERS, 2, "Invalid owner"),
        # actions/checkout's CODEOWNERS is `* @actions/actions-runtime`, which GitHub reports this way.
        ("* @owner/actions-runtime\nblueprint/ @alice\n", 1, "Unknown owner"),
    ],
)
def test_any_error_github_reports_in_codeowners_breaks_the_policy(codeowners: str, line: int, error: str) -> None:
    """GitHub skips a line it cannot parse and ignores an owner it cannot use,
    such as a team without write access; the local parser cannot see either."""

    github = FakeGitHub(codeowners)
    github.codeowners_errors = [{"line": line, "column": 1, "kind": error, "path": ".github/CODEOWNERS"}]

    _refused(
        github,
        "GitHub reports 1 error(s) in CODEOWNERS on main, so it does not enforce every line as written: "
        f".github/CODEOWNERS:{line} {error}",
    )


def test_errors_github_reports_are_named_ten_at_a_time() -> None:
    github = FakeGitHub()
    github.codeowners_errors = [{"line": line, "kind": "Unknown owner", "path": "CODEOWNERS"} for line in range(1, 13)]

    _refused(github, "GitHub reports 12 error(s) in CODEOWNERS on main", "CODEOWNERS:10 Unknown owner; and 2 more")


def test_codeowners_github_finds_nothing_for_break_the_policy() -> None:
    github = FakeGitHub()
    github.codeowners_errors = None

    _refused(github, "GitHub finds no CODEOWNERS file on main, so it requires no code owner review")


# Requests and the token.


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda github: github.answers.update({"/codeowners/errors": {"errors": "many"}}), "did not return a list of errors"),
        (lambda github: github.answers.update({"": {"id": 1}}), "did not name its default branch"),
        (lambda github: github.repository.pop("owner"), "GitHub API GET of the repository did not name its owner"),
        (lambda github: github.answers.update({"/rules/branches/main": {"rules": []}}), "did not return a list"),
        (
            lambda github: github.answers.update({"/collaborators/owner/permission": {"role_name": "write"}}),
            "GitHub API GET of @owner's permission did not name one",
        ),
        (
            lambda github: github.answers.update({"/rulesets/1": ApprovalError("GitHub API GET /rulesets/1 failed")}),
            "GitHub API GET /rulesets/1 failed",
        ),
    ],
    ids=["errors", "default-branch", "owner", "rules", "permission", "failed-request"],
)
def test_an_answer_that_cannot_be_read_labels_nothing(edit: object, message: str) -> None:
    """A self-approved label from a partial answer would downgrade a site whose policy holds."""

    github = FakeGitHub()
    edit(github)  # type: ignore[operator]

    with pytest.raises(ApprovalError, match=message):
        _reason(github)


def test_missing_github_environment_names_what_is_missing() -> None:
    with pytest.raises(ApprovalError, match="needs GITHUB_TOKEN and GITHUB_REPOSITORY"):
        GitHubPolicyVerifier.from_environment(environ={})
    with pytest.raises(ApprovalError, match="needs GITHUB_REPOSITORY in"):
        GitHubPolicyVerifier.from_environment(environ={"GITHUB_TOKEN": "token"})
    with pytest.raises(ApprovalError, match="owner/name"):
        GitHubPolicyVerifier.from_environment(environ={"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "project"})
    with pytest.raises(ApprovalError, match="GITHUB_API_URL must be an https URL"):
        GitHubPolicyVerifier.from_environment(
            environ={"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/p", "GITHUB_API_URL": "http://github.example"}
        )


def _answering(monkeypatch: pytest.MonkeyPatch, answer: bytes | Exception) -> list:
    """Make every request answer ``answer``, raised if an exception; return the requests made."""

    requests: list = []

    def urlopen(request: object, timeout: float) -> object:
        requests.append(request)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)

    monkeypatch.setattr(approvals.urllib.request, "urlopen", urlopen)
    return requests


def test_the_client_reads_404_as_nothing_and_keeps_the_token_off_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _answering(monkeypatch, urllib.error.HTTPError("", 404, "Not Found", {}, io.BytesIO(b"")))  # type: ignore[arg-type]
    client = GitHubClient("secret", "owner/project", api_url="https://github.example/api/v3/")

    assert client.get("/contents/CODEOWNERS", {"ref": "main"}) is None
    assert requests[0].full_url == "https://github.example/api/v3/repos/owner/project/contents/CODEOWNERS?ref=main"
    # Kept off redirects, so the token never follows one to another host.
    assert requests[0].unredirected_hdrs == {"Authorization": "Bearer secret"}


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (urllib.error.HTTPError("", 502, "error", {}, io.BytesIO(b"Bad Gateway")), "failed with HTTP 502: Bad Gateway"),  # type: ignore[arg-type]
        (urllib.error.HTTPError("", 403, "error", {}, io.BytesIO(b"rate limited")), "failed with HTTP 403: rate limited"),  # type: ignore[arg-type]
        (urllib.error.URLError("timed out"), "failed: <urlopen error timed out>"),
        (b"not json", "returned malformed JSON"),
        (b"[" + b" " * 64 + b"]", "returned more than 32 bytes"),
    ],
    ids=["http", "forbidden", "network", "malformed", "oversized"],
)
def test_a_failed_request_says_what_it_asked_for(
    monkeypatch: pytest.MonkeyPatch, failure: bytes | Exception, message: str
) -> None:
    """The repository's own path is empty, which would leave `GitHub API GET  failed` naming nothing."""

    _answering(monkeypatch, failure)
    monkeypatch.setattr(approvals, "_MAX_RESPONSE_BYTES", 32)

    with pytest.raises(ApprovalError) as raised:
        GitHubClient("secret", "owner/project").get("")
    assert str(raised.value) == f"GitHub API GET of the repository {message}"


@pytest.mark.parametrize(
    ("char", "escaped"),
    [
        # Bidi overrides and isolates reorder a name on screen; zero-width characters hide one.
        ("\u202e", "\\u202e"),
        ("\u2066", "\\u2066"),
        ("\u200b", "\\u200b"),
        # A terminal escape sequence, a C1 line break, NUL, and a tab.
        ("\x1b", "\\x1b"),
        ("\x85", "\\x85"),
        ("\x00", "\\x00"),
        ("\t", "\\t"),
    ],
    ids=["rlo", "lri", "zwsp", "esc", "nel", "nul", "tab"],
)
def test_a_reason_escapes_every_character_that_is_not_printable(char: str, escaped: str) -> None:
    assert approvals._printable(f"notes{char}.md") == f"notes{escaped}.md"


# The CLI and the site.


def _policy_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[Path, FakeGitHub]:
    """The review CLI fixture, both articles approved, and the GitHub the CLI asks about it."""

    monkeypatch.setattr("autoform_cli.__main__.extract_skeletons", _Extraction())
    blueprint = _approved_articles(tmp_path, capsys)
    capsys.readouterr()
    github = FakeGitHub()
    monkeypatch.setattr(approvals, "GitHubClient", lambda *args, **kwargs: github)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/project")
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    return blueprint, github


def _check(tmp_path: Path, blueprint: Path, *extra: str) -> int:
    return main(["review", "check", str(blueprint), "--lean-root", str(tmp_path), "--policy", "github", *extra])


def _render(tmp_path: Path, blueprint: Path) -> tuple[int, str]:
    site = tmp_path / "site"
    code = main(
        ["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--policy", "github",
         "--output", str(site)]
    )
    pages = "\n".join(path.read_text(encoding="utf-8") for path in site.rglob("*.md")) if site.exists() else ""
    return code, pages


def test_check_and_render_label_approvals_policy_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, github = _policy_project(tmp_path, monkeypatch, capsys)

    assert _check(tmp_path, blueprint) == 0
    output = capsys.readouterr().out
    assert "OK: 2 statement review(s) complete and current" in output
    assert "basics/result: policy-enforced · sha256:" in output
    assert github.calls == _CALLS

    assert _check(tmp_path, blueprint, "--json") == 0
    reported = json.loads(capsys.readouterr().out)["approvals"]
    assert [sorted(item) for item in reported] == [["label", "node_id", "policy_enforced", "reason", "review_hash"]] * 2
    assert [(item["node_id"], item["label"], item["policy_enforced"]) for item in reported] == [
        ("basics/other", "policy-enforced", True),
        ("basics/result", "policy-enforced", True),
    ]

    code, pages = _render(tmp_path, blueprint)
    assert code == 0
    assert pages.count('<span class="bp-review-policy-enforced">policy-enforced · sha256:') == 2
    assert "self-approved" not in pages


def test_check_and_render_say_why_approvals_stayed_self_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, github = _policy_project(tmp_path, monkeypatch, capsys)
    github.rules = []

    assert _check(tmp_path, blueprint) == 0
    output = capsys.readouterr().out
    assert "basics/result: self-approved · sha256:" in output
    assert output.count(f"({_NO_RULESET}") == 2
    assert github.calls == ["", "/rules/branches/main"]

    code, pages = _render(tmp_path, blueprint)
    assert code == 0
    assert pages.count(f'class="bp-review-self-approved" title="{_NO_RULESET}') == 2
    # The build log says why as well, since the page shows it only on hover.
    assert f"warning: every approval is self-approved: {_NO_RULESET}" in capsys.readouterr().out


def test_a_failed_request_stops_check_and_render_before_the_site_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rendering every approval self-approved would let a flaky request downgrade the live site."""

    blueprint, github = _policy_project(tmp_path, monkeypatch, capsys)
    github.answers["/rules/branches/main"] = ApprovalError("GitHub API GET /rules/branches/main failed with HTTP 502")

    assert _check(tmp_path, blueprint) == 2
    output = capsys.readouterr()
    assert "error: GitHub API GET /rules/branches/main failed with HTTP 502" in output.err
    assert "self-approved" not in output.out

    code, pages = _render(tmp_path, blueprint)
    assert code == 1
    assert not (tmp_path / "site").exists() and pages == ""
    assert "self-approved" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("forged", "shown"),
    [
        # After a newline GitHub Actions runs `::` at the start of a line as a command.
        ("notes\n::error::forged\r\u2028", "notes\\n::error::forged\\r\\u2028"),
        # The runner finds `##[` anywhere in a line and runs what follows, such as add-mask or stop-commands.
        ("notes##[add-mask]secret###[error]forged", "notes#\\x23[add-mask]secret##\\x23[error]forged"),
    ],
    ids=["newline", "older-command"],
)
def test_a_reason_never_starts_a_workflow_command_in_the_build_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], forged: str, shown: str
) -> None:
    """A CODEOWNERS file or an answer from GitHub may hold a workflow command."""

    blueprint, github = _policy_project(tmp_path, monkeypatch, capsys)
    github.codeowners_errors = [{"line": 1, "kind": forged, "path": ".github/CODEOWNERS"}]

    assert _check(tmp_path, blueprint) == 0
    assert _render(tmp_path, blueprint)[0] == 0
    output = capsys.readouterr().out
    assert output.count(f".github/CODEOWNERS:1 {shown}") == 3
    github.answers[""] = ApprovalError(f"GitHub API GET of the repository failed with HTTP 502: {forged}")
    assert _render(tmp_path, blueprint)[0] == 1
    output += capsys.readouterr().out
    assert f"failed with HTTP 502: {shown}\n" in output
    assert "##[" not in output and all(not line.lstrip().startswith("::") for line in output.splitlines())

