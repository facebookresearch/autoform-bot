"""Authenticated approvals: who approved a review hash, not only that it is current."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from autoform_cli import approvals
from autoform_cli.__main__ import main
from autoform_cli.approvals import (
    ApprovalAttestation,
    ApprovalError,
    GitHubClient,
    GitHubReviewVerifier,
    GitHubUnavailable,
    SupersededBuildError,
    approval_statuses,
    code_owners,
    parse_codeowners,
)
from autoform_cli.graph import load_graph
from tests.test_review_cli import _approved_batch, _Extraction


_HASH = "sha256:" + "a" * 64
_OTHER_HASH = "sha256:" + "b" * 64
_ARTICLES = {
    "result": "af_0123456789abcdef01234567",
    "other": "af_fedcba9876543210fedcba98",
    "gone": "af_00112233445566778899aabb",
}
_ARTICLE = "blueprint/roadmap/basics/result.md"
_VERIFY = ".github/workflows/autoform-verify.yml"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}
# A rebase merge rewrites every commit it lands, as GitHub does.
_GITHUB_COMMITTER = {"GIT_COMMITTER_NAME": "GitHub", "GIT_COMMITTER_EMAIL": "noreply@github.com"}


def _run_git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENTITY, **(env or {})},
    ).stdout


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return _run_git(root, *args, env=env).strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "--no-verify", "-m", message)
    return _git(root, "rev-parse", "HEAD")


# Code owner review protects only what has an owner, so the fixture owns everything.
_CODEOWNERS = "* @owner\nblueprint/ @alice\n"


def _project(tmp_path: Path, codeowners: str | None = _CODEOWNERS) -> Path:
    root = tmp_path / "project"
    chapter = root / "blueprint" / "roadmap" / "basics"
    chapter.mkdir(parents=True)
    (root / "blueprint" / "README.md").write_text("# Blueprint\n", encoding="utf-8")
    (root / "blueprint" / "roadmap" / "README.md").write_text(
        "# Roadmap\n\n- [Basics](basics/README.md)\n", encoding="utf-8"
    )
    (chapter / "README.md").write_text(
        "# Basics\n\n" + "".join(f"- [{name.title()}]({name}.md)\n" for name in _ARTICLES), encoding="utf-8"
    )
    for name, article_id in _ARTICLES.items():
        (chapter / f"{name}.md").write_text(
            "---\n"
            f"article_id: {article_id}\n"
            "declaration: theorem\n"
            f"lean: Review.{name}\n"
            "statement: formalized\n"
            "---\n\n"
            f"# {name.title()}\n\nTruth holds.\n\n"
            "## Depends on\n\nNone.\n",
            encoding="utf-8",
        )
    if codeowners is not None:
        (root / ".github").mkdir()
        (root / ".github" / "CODEOWNERS").write_text(codeowners, encoding="utf-8")
    _git(root, "init", "--quiet", "--initial-branch=main")
    _commit(root, "Start the blueprint")
    return root


def _approve(root: Path, name: str, value: str | None) -> None:
    article = root / "blueprint" / "roadmap" / "basics" / f"{name}.md"
    lines = [line for line in article.read_text(encoding="utf-8").splitlines(keepends=True)
             if not line.startswith("review_approved:")]
    if value is not None:
        lines.insert(lines.index("statement: formalized\n") + 1, f"review_approved: {value}\n")
    article.write_text("".join(lines), encoding="utf-8")


def _append(root: Path, text: str, path: str = _ARTICLE) -> None:
    article = root / path
    article.write_text(article.read_text(encoding="utf-8") + text, encoding="utf-8")


class FakeGitHub:
    """Answers GitHub's REST API for the test repository.

    Pull requests, the commits GitHub associates with them, reviews, and
    Actions runs live in memory; diffs and file contents come from Git. By
    default the repository is set up as the README asks: a ruleset this
    token cannot bypass requires code owner review on main, dismisses stale
    approvals, and requires approval of the last push, and everyone named has
    write permission.
    """

    api_url = "https://api.github.com"
    repository = {"id": 1, "full_name": "owner/project", "owner": {"login": "owner"}}

    def __init__(self, root: Path) -> None:
        self.root = root
        self.default_branch = "main"
        self.pulls: dict[int, dict] = {}
        self.associated: dict[str, list[int]] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.runs: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.file_edits: dict[int, object] = {}
        # path -> what GET answers instead, raised if an exception; such a request is not logged in calls.
        self.answers: dict[str, object] = {}
        # What GET /rate_limit says: the hour's limit and how many requests are left, or None for no answer.
        self.hourly: tuple[int, int] | None = (1000, 1000)
        self.rules: list[dict] = [
            {
                "type": "pull_request",
                "ruleset_source_type": "Repository",
                "ruleset_source": "owner/project",
                "ruleset_id": 1,
                "parameters": {
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": True,
                    "require_last_push_approval": True,
                    "required_approving_review_count": 1,
                    "required_review_thread_resolution": False,
                },
            }
        ]
        # ruleset id -> GET /rulesets/{id}; a missing id answers 404.
        self.rulesets: dict[int, dict] = {
            1: {
                "id": 1,
                "name": "main",
                "target": "branch",
                "source_type": "Repository",
                "source": "owner/project",
                "enforcement": "active",
                "current_user_can_bypass": "never",
            }
        }
        # What GET /codeowners/errors lists; None answers 404, as for a ref without CODEOWNERS.
        self.codeowners_errors: list[dict] | None = []
        # commit -> what it lists for that commit instead, since GitHub reads each ref's own CODEOWNERS.
        self.codeowners_errors_at: dict[str, list[dict] | None] = {}
        # branch -> what GET /git/ref/heads/{branch} answers when not the local branch's commit; None is 404.
        self.heads: dict[str, object] = {}
        # login -> permission; None answers 404, as for someone who is not a collaborator.
        self.permissions: dict[str, str | None] = {}
        self.events: dict[int, list[dict]] = {}
        # commit -> (author login, committer login); by default the pull request's author.
        self.commit_users: dict[str, tuple[str | None, str | None]] = {}

    def open_pull(
        self,
        number: int,
        author: str,
        *,
        head: str | None = None,
        base: str | None = None,
        base_ref: str = "main",
        ci: str | None = "success",
        ref: str | None = None,
    ) -> str:
        """Open pull request ``number`` from ``head`` (default HEAD) on branch ``ref``
        (default the current branch); return its head."""

        head = head or _git(self.root, "rev-parse", "HEAD")
        base = base or _git(self.root, "merge-base", self.default_branch, head)
        ref = ref or _git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        repository = {"id": self.repository["id"], "full_name": self.repository["full_name"]}
        self.pulls[number] = {
            "number": number,
            "state": "open",
            "user": {"login": author},
            "html_url": f"https://github.com/owner/project/pull/{number}",
            "head": {"sha": head, "ref": ref, "label": f"owner:{ref}", "repo": dict(repository)},
            "base": {"ref": base_ref, "sha": base, "repo": dict(repository)},
            "merged_at": None,
        }
        self.associate(number, *_git(self.root, "rev-list", f"{base}..{head}").split())
        if ci is not None:
            self.run(head, conclusion=ci, branch=ref)
        return head

    def push(self, number: int, *, ci: str | None = "success") -> str:
        """Move pull request ``number``'s head to HEAD."""

        pull = self.pulls[number]
        head = _git(self.root, "rev-parse", "HEAD")
        pull["head"]["sha"] = head
        self.associate(number, *_git(self.root, "rev-list", f"{pull['base']['sha']}..{head}").split())
        if ci is not None:
            self.run(head, conclusion=ci, branch=pull["head"]["ref"])
        return head

    def associate(self, number: int, *commits: str) -> None:
        for commit in commits:
            numbers = self.associated.setdefault(commit, [])
            if number not in numbers:
                numbers.append(number)

    def merged(self, number: int, *landed: str) -> None:
        self.pulls[number].update(state="closed", merged_at="2026-01-02T00:00:00Z")
        self.associate(number, *landed)

    def run(
        self,
        head: str,
        *,
        conclusion: str | None = "success",
        path: str = _VERIFY,
        event: str = "pull_request",
        status: str = "completed",
        branch: str | None = None,
    ) -> None:
        """Record an Actions run; ``branch`` defaults to that of the pull request with this head.

        GitHub lists a run's pull requests only while they are open, so the
        runs of merged pull requests list none.
        """

        if branch is None:
            branch = next(
                (pull["head"]["ref"] for pull in self.pulls.values() if pull["head"]["sha"] == head),
                _git(self.root, "rev-parse", "--abbrev-ref", "HEAD"),
            )
        self.runs.append(
            {
                "id": len(self.runs) + 1,
                "path": path,
                "event": event,
                "head_sha": head,
                "head_branch": branch,
                "head_repository": {"id": self.repository["id"], "full_name": self.repository["full_name"]},
                "pull_requests": [],
                "status": status,
                "conclusion": conclusion,
            }
        )

    def review(
        self,
        number: int,
        login: str,
        state: str,
        commit: str | None = None,
        *,
        association: str = "COLLABORATOR",
    ) -> None:
        reviews = self.reviews.setdefault(number, [])
        review_id = len(reviews) + 1
        reviews.append(
            {
                "id": review_id,
                "user": {"login": login},
                "state": state,
                "commit_id": commit or self.pulls[number]["head"]["sha"],
                "author_association": association,
                "submitted_at": f"2026-01-01T00:{review_id // 60:02d}:{review_id % 60:02d}Z",
                "html_url": f"https://github.com/owner/project/pull/{number}#pullrequestreview-{review_id}",
            }
        )

    def files(self, number: int) -> list[dict]:
        """The pull request's files as GitHub lists them: renames detected, patches without headers."""

        pull = self.pulls[number]
        base, head = pull["base"]["sha"], pull["head"]["sha"]
        entries = []
        for row in _git(self.root, "diff", "--name-status", "-M", base, head).splitlines():
            status, *names = row.split("\t")
            entry: dict[str, object] = {"filename": names[-1]}
            if status.startswith("R"):
                entry.update(status="renamed", previous_filename=names[0])
            else:
                entry["status"] = {"A": "added", "D": "removed"}.get(status, "modified")
            diff = _run_git(self.root, "diff", "-M", base, head, "--", *names).split("\n")
            hunks = next((index for index, line in enumerate(diff) if line.startswith("@@")), None)
            rows = [] if hunks is None else diff[hunks:]
            if rows and rows[-1] == "":
                rows.pop()
            entry["additions"] = sum(1 for line in rows if line.startswith("+"))
            entry["deletions"] = sum(1 for line in rows if line.startswith("-"))
            if rows:
                entry["patch"] = "\n".join(rows)
            entries.append(entry)
        edit = self.file_edits.get(number)
        return edit(entries) if callable(edit) else entries

    def commits(self, number: int) -> list[dict]:
        """The pull request's commits as GitHub lists them, oldest first, with the accounts GitHub links."""

        pull = self.pulls[number]
        listed = []
        for sha in _git(self.root, "rev-list", "--reverse", f"{pull['base']['sha']}..{pull['head']['sha']}").split():
            author, committer = self.commit_users.get(sha, (pull["user"]["login"], pull["user"]["login"]))
            listed.append(
                {
                    "sha": sha,
                    "author": None if author is None else {"login": author},
                    "committer": None if committer is None else {"login": committer},
                }
            )
        return listed

    def rate_limit(self) -> tuple[int, int] | None:
        return self.hourly

    def get(self, path: str, query: dict | None = None) -> object | None:
        if path in self.answers:
            answer = self.answers[path]
            if isinstance(answer, BaseException):
                raise answer
            return answer
        query = dict(query or {})
        self.calls.append((path, query))
        if path == "":
            return {**self.repository, "default_branch": self.default_branch}
        parts = path.split("/")
        if parts[1] == "commits" and parts[3:] == ["pulls"]:
            return self._page([self.pulls[number] for number in self.associated.get(parts[2], [])], query)
        if parts[1:3] == ["rules", "branches"]:
            # None answers 404, as for a branch no rule applies to.
            return None if self.rules is None else self._page(self.rules, query)
        if parts[1] == "rulesets" and len(parts) == 3:
            return self.rulesets.get(int(parts[2]))
        if parts[1:4] == ["git", "ref", "heads"]:
            branch = urllib.parse.unquote("/".join(parts[4:]))
            if branch in self.heads:
                return self.heads[branch]
            head = _git(self.root, "rev-parse", f"refs/heads/{branch}")
            return {"ref": f"refs/heads/{branch}", "object": {"sha": head, "type": "commit"}}
        if path == "/codeowners/errors":
            found = any(
                subprocess.run(
                    ["git", "cat-file", "-e", f"{query['ref']}:{location}"], cwd=self.root, capture_output=True
                ).returncode
                == 0
                for location in approvals.CODEOWNERS_LOCATIONS
            )
            if not found:
                return None
            errors = self.codeowners_errors_at.get(_git(self.root, "rev-parse", query["ref"]), self.codeowners_errors)
            return None if errors is None else {"errors": errors}
        if parts[1] == "collaborators" and parts[3:] == ["permission"]:
            login = urllib.parse.unquote(parts[2])
            permission = self.permissions.get(login.lower(), "write")
            return None if permission is None else {"permission": permission, "role_name": permission}
        if parts[1] == "issues" and parts[3:] == ["events"]:
            return self._page(self.events.get(int(parts[2]), []), query)
        if path == "/pulls":
            assert query.get("state") == "all"
            heads = [pull for pull in self.pulls.values() if pull["head"]["label"] == query["head"]]
            return self._page(heads, query)
        if parts[1] == "pulls":
            number = int(parts[2])
            if number not in self.pulls:
                return None
            if len(parts) == 3:
                return self.pulls[number]
            if parts[3] == "reviews":
                return self._page(self.reviews.get(number, []), query)
            if parts[3] == "files":
                return self._page(self.files(number), query)
            if parts[3] == "commits":
                return self._page(self.commits(number), query)
        if parts[1] == "contents":
            name = urllib.parse.unquote(path[len("/contents/"):])
            blob = subprocess.run(
                ["git", "cat-file", "blob", f"{query['ref']}:{name}"], cwd=self.root, capture_output=True
            )
            if blob.returncode != 0:
                return None
            # GitHub wraps base64 content at 60 columns.
            return {"type": "file", "encoding": "base64", "content": base64.encodebytes(blob.stdout).decode()}
        if parts[1:3] == ["actions", "runs"]:
            runs = [
                run for run in self.runs
                if run["head_sha"] == query.get("head_sha") and run["event"] == query.get("event", run["event"])
            ]
            return {"total_count": len(runs), "workflow_runs": self._page(runs, query)}
        raise AssertionError(f"unexpected GitHub API request {path}")

    @staticmethod
    def _page(items: list[dict], query: dict) -> list[dict]:
        size, page = int(query.get("per_page", 30)), int(query.get("page", 1))
        return items[(page - 1) * size : page * size]


def _branch(root: Path, name: str, start: str = "main") -> None:
    _git(root, "checkout", "--quiet", "-b", name, start)


def _land(root: Path, github: FakeGitHub, number: int, strategy: str = "squash") -> str:
    """Merge pull request ``number`` into main as GitHub would; return main's new head."""

    head = github.pulls[number]["head"]["sha"]
    _git(root, "checkout", "--quiet", "main")
    if strategy == "merge":
        _git(root, "merge", "--quiet", "--no-ff", "-m", f"Merge pull request #{number}", head)
        landed = [_git(root, "rev-parse", "HEAD")]
    elif strategy == "squash":
        _git(root, "merge", "--quiet", "--squash", head)
        landed = [_commit(root, f"Land pull request (#{number})")]
    elif strategy == "rebase":
        landed = []
        for commit in _git(root, "rev-list", "--reverse", f"HEAD..{head}").split():
            _git(root, "cherry-pick", "--allow-empty", commit, env=_GITHUB_COMMITTER)
            landed.append(_git(root, "rev-parse", "HEAD"))
    else:
        raise AssertionError(strategy)
    github.merged(number, *landed)
    return landed[-1]


def _verify(root: Path, github: FakeGitHub, *, trusted_ref: str = "HEAD", **kwargs: object):
    verifier = GitHubReviewVerifier(github, trusted_ref=trusted_ref, **kwargs)  # type: ignore[arg-type]
    graph = load_graph(root / "blueprint")
    approved = {node.id: node.review_approved for node in graph.nodes.values() if node.review_approved}
    return approval_statuses(graph, approved, verifier)


def _verified(root: Path, github: FakeGitHub, **kwargs: object) -> GitHubReviewVerifier:
    """A verifier of ``root`` at HEAD after it has checked every approval."""

    verifier = GitHubReviewVerifier(github, trusted_ref="HEAD", **kwargs)  # type: ignore[arg-type]
    graph = load_graph(root / "blueprint")
    approval_statuses(graph, {node.id: node.review_approved for node in graph.nodes.values() if node.review_approved}, verifier)
    assert verifier.unchecked.items() <= verifier.reasons.items()
    return verifier


def _pull_approving(
    root: Path, github: FakeGitHub, *, author: str = "bob", number: int = 7, strategy: str = "squash"
) -> str:
    """Pull request ``number`` records _HASH for the result and is merged; return its head."""

    _branch(root, f"approve-{number}")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    head = github.open_pull(number, author)
    _land(root, github, number, strategy)
    return head


def test_without_a_verifier_every_current_approval_is_self_approved(tmp_path: Path) -> None:
    graph = load_graph(_project(tmp_path) / "blueprint")

    [status] = approval_statuses(graph, {"basics/result": _HASH}).values()

    assert not status.authenticated
    assert status.label == "self-approved"


@pytest.mark.parametrize("strategy", ["squash", "merge", "rebase"])
def test_a_code_owner_approving_the_head_of_the_recording_pull_request_authenticates(
    tmp_path: Path, strategy: str
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    # main moves on first, so a rebase merge cannot fast-forward.
    _append(root, "Unrelated.\n", "blueprint/README.md")
    _commit(root, "Edit the blueprint README")
    _branch(root, "approve", "main~1")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    _append(root, "\nA later note.\n")
    head = _commit(root, "Add a note")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    _land(root, github, 7, strategy)

    status = _verify(root, github)["basics/result"]

    assert status.attestation == ApprovalAttestation(
        "basics/result",
        _HASH,
        "alice",
        "github-review",
        "https://github.com/owner/project/pull/7#pullrequestreview-1",
    ), status.reason
    assert status.label == "approved by @alice (github review)"


def test_the_pull_request_author_cannot_authenticate_their_own_approval(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github, author="Alice")
    github.review(7, "alice", "APPROVED", head)

    status = _verify(root, github)["basics/result"]

    assert status.label == "self-approved"
    assert "@alice approved #7 but is its author" in (status.reason or "")


def test_a_reviewer_who_is_not_a_code_owner_cannot_authenticate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "carol", "APPROVED", head)

    status = _verify(root, github)["basics/result"]

    assert not status.authenticated
    assert "@carol approved #7 but is not an individual code owner" in (status.reason or "")


def test_code_owners_must_hold_before_the_merge_and_at_the_trusted_ref(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    # The pull request adds its own reviewer to CODEOWNERS.
    _branch(root, "grab")
    (root / ".github" / "CODEOWNERS").write_text("* @owner\nblueprint/ @alice @carol\n", encoding="utf-8")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    head = github.open_pull(7, "bob")
    github.review(7, "carol", "APPROVED", head)
    landed = _land(root, github, 7)

    status = _verify(root, github)["basics/result"]
    assert not status.authenticated
    assert "#7 changes .github/CODEOWNERS, not only articles and read-back cards" in (status.reason or "")

    # Even were GitHub to list only the article, carol owned nothing before the merge.
    github.file_edits[7] = lambda entries: [entry for entry in entries if entry["filename"] == _ARTICLE]
    status = _verify(root, github)["basics/result"]
    assert not status.authenticated
    assert "@carol approved #7 but is not an individual code owner" in (status.reason or "")

    # A code owner then, who is no longer one at the trusted ref, does not count either.
    github.review(7, "alice", "APPROVED", head)
    assert _verify(root, github)["basics/result"].authenticated
    (root / ".github" / "CODEOWNERS").write_text("* @owner\nblueprint/ @carol\n", encoding="utf-8")
    _commit(root, "Hand the blueprint to carol")
    status = _verify(root, github)["basics/result"]
    assert not status.authenticated
    assert (
        f"no individual @user is a code owner of {_ARTICLE} both at {_git(root, 'rev-parse', landed + '^')[:12]} "
        "(before #7) and at HEAD"
    ) in (status.reason or "")
    # Nor does a build of the merge, which the default branch has moved past: it stops.
    with pytest.raises(SupersededBuildError, match="the head of main on GitHub; approvals are authenticated only"):
        _verify(root, github, trusted_ref=landed)


def test_an_approval_of_an_earlier_commit_does_not_count(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _OTHER_HASH)
    reviewed = _commit(root, "Approve an earlier review")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the current review")
    head = github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", reviewed)
    _land(root, github, 7)

    status = _verify(root, github)["basics/result"]

    assert not status.authenticated
    assert f"@alice approved an earlier commit of #7, not its head {head[:12]}" in (status.reason or "")


@pytest.mark.parametrize(
    ("later", "authenticated"),
    [("CHANGES_REQUESTED", False), ("DISMISSED", False), ("COMMENTED", True), ("PENDING", True)],
)
def test_a_later_verdict_voids_an_approval_but_a_comment_does_not(
    tmp_path: Path, later: str, authenticated: bool
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head)
    github.review(7, "alice", later, head)

    status = _verify(root, github)["basics/result"]

    assert status.authenticated is authenticated
    if not authenticated:
        assert f"@alice's latest review of #7 is {later}" in (status.reason or "")


def test_reviews_are_ordered_by_submission_not_by_listing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "CHANGES_REQUESTED", head)
    github.review(7, "alice", "APPROVED", head)
    github.reviews[7].reverse()

    assert _verify(root, github)["basics/result"].authenticated


def test_a_dismissed_approval_does_not_authenticate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    # GitHub rewrites a dismissed review's own state rather than adding one.
    github.review(7, "alice", "DISMISSED", head)

    assert not _verify(root, github)["basics/result"].authenticated


def test_team_and_email_owners_never_authenticate(tmp_path: Path) -> None:
    root = _project(tmp_path, "* @owner\nblueprint/ @owner/reviewers alice@example.com\n")
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head)

    status = _verify(root, github)["basics/result"]

    assert not status.authenticated
    assert "teams and email owners cannot be verified" in (status.reason or "")
    assert "name an individual @user" in (status.reason or "")
    # Only the repository's setup was read; nothing about the approval.
    assert [path for path, _ in github.calls] == _SETUP_CALLS


def test_no_codeowners_file_allows_nobody(tmp_path: Path) -> None:
    root = _project(tmp_path, codeowners=None)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head)

    status = _verify(root, github)["basics/result"]

    assert not status.authenticated
    assert "has no CODEOWNERS file" in (status.reason or "")


def test_code_owner_logins_match_without_case(tmp_path: Path) -> None:
    root = _project(tmp_path, "* @owner\nblueprint/ @Alice\n")
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "aLICE", "APPROVED", head)

    status = _verify(root, github)["basics/result"]

    assert status.authenticated, status.reason
    assert status.attestation is not None and status.attestation.reviewer == "aLICE"


def test_reviews_are_read_across_pages(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    for number in range(100):
        github.review(7, f"reader{number}", "COMMENTED", head)
    github.review(7, "alice", "APPROVED", head)

    assert _verify(root, github)["basics/result"].authenticated
    pages = [query for path, query in github.calls if path == "/pulls/7/reviews"]
    assert pages == [{"per_page": 100, "page": 1}, {"per_page": 100, "page": 2}]


def test_a_listing_longer_than_the_page_limit_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading only the first pages would miss alice's later verdict."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head)
    for number in range(99):
        github.review(7, f"reader{number}", "COMMENTED", head)
    github.review(7, "alice", "CHANGES_REQUESTED", head)
    monkeypatch.setattr(approvals, "_MAX_PAGES", 1)

    status = _verify(root, github)["basics/result"]

    assert not status.authenticated
    assert "GitHub API GET /pulls/7/reviews has more than 100 entries" in (status.reason or "")


def test_lookups_are_cached_and_the_request_budget_fails_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _approve(root, "other", _HASH)
    _commit(root, "Approve two articles")
    head = github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    landed = _land(root, github, 7)

    statuses = _verify(root, github)
    assert all(status.authenticated for status in statuses.values())
    requested = [path for path, _ in github.calls]
    # Once for the repository: its default branch, rules, and the permission of each code owner.
    setup = [*_SETUP_CALLS, "/collaborators/alice/permission"]
    assert requested[: len(setup)] == setup
    # Once for both approvals' pull request, whose approver's permission is already known.
    assert requested[len(setup) :] == [
        f"/commits/{landed}/pulls",
        f"/commits/{_git(root, 'rev-parse', landed + '^')}/pulls",
        "/pulls/7/files",
        "/contents/blueprint/roadmap/basics/other.md",
        "/pulls/7/reviews",
        "/pulls/7/commits",
        "/pulls",
        "/issues/7/events",
        "/actions/runs",
        f"/contents/{_ARTICLE}",
    ]

    github.calls.clear()
    # GitHub says 52 requests are left this hour, and a run leaves 50 of them.
    github.hourly = (1000, 52)
    statuses = _verify(root, github)
    assert len(github.calls) == 2
    assert not any(status.authenticated for status in statuses.values())
    assert all("budget of 2 GitHub API requests" in (status.reason or "") for status in statuses.values())


@pytest.mark.parametrize("gate", [False, True], ids=["precondition", "gate"])
def test_a_budget_spent_outside_a_candidate_is_refused_at_the_ceiling_and_unchecked_below_it(
    tmp_path: Path, gate: bool
) -> None:
    """Whether the budget runs out in the precondition, or in a gate's checks of its pull request, a run with
    the whole ceiling gets no further than any other, and a run with less may."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _approve(root, "other", _HASH)
    _commit(root, "Approve two articles")
    head = github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    if gate:
        assert all(status.authenticated for status in _verify(root, github, pull_request=7).values())
        # Out of requests at the last one, inside the checks of an approval.
        budget = len(github.calls) - 1
    else:
        _land(root, github, 7)
        # Out of requests in the precondition, after the head check.
        budget = 2
    kwargs = {"pull_request": 7} if gate else {}

    github.hourly = (budget + 200, budget + 200)
    verifier = _verified(root, github, **kwargs)
    assert verifier.reasons
    assert all("needs more than the" in reason for reason in verifier.reasons.values())
    assert verifier.unchecked == {}

    github.hourly = (1000, budget + 50)
    verifier = _verified(root, github, **kwargs)
    assert verifier.reasons
    assert verifier.unchecked == verifier.reasons


def test_a_failed_request_in_the_gate_refuses_only_the_approval_that_needs_it(tmp_path: Path) -> None:
    """Deliberate guard: the gate tries no candidate commits, so a failure reaches
    the per-approval handling that the tests of merged pull requests no longer exercise."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _approve(root, "other", _HASH)
    _commit(root, "Approve two articles")
    head = github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    other = "/contents/blueprint/roadmap/basics/other.md"
    github.answers[other] = GitHubUnavailable(f"GitHub API GET {other} failed with HTTP 502: Bad Gateway")
    statuses = _verify(root, github, trusted_ref="main", pull_request=7)

    assert statuses["basics/result"].authenticated
    assert not statuses["basics/other"].authenticated
    assert "HTTP 502" in (statuses["basics/other"].reason or "")


@pytest.mark.parametrize("failure", ["HTTP 502", "HTTP 404", "HTTP 422", "oversized"])
def test_only_an_approval_a_later_run_may_authenticate_is_unchecked(tmp_path: Path, failure: str) -> None:
    """A later run may get the answer a failed request did not, or a spent budget did not ask for. A 404, any
    other 4xx but a rate limit, and an oversized answer come back the same on every run: a verdict, not a gap."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head)
    _branch(root, "other")
    _approve(root, "other", _OTHER_HASH)
    _commit(root, "Approve the other article")
    other_head = github.open_pull(8, "bob")
    github.review(8, "alice", "APPROVED", other_head)
    _land(root, github, 8)
    github.answers["/pulls/8/files"] = {
        "HTTP 502": GitHubUnavailable("GitHub API GET /pulls/8/files failed with HTTP 502: Bad Gateway"),
        "HTTP 404": None,
        "HTTP 422": ApprovalError("GitHub API GET /pulls/8/files failed with HTTP 422: Unprocessable Entity"),
        "oversized": ApprovalError("GitHub API GET /pulls/8/files returned more than 8388608 bytes"),
    }[failure]
    verifier = _verified(root, github)

    assert list(verifier.reasons) == ["basics/other"]
    assert verifier.unchecked == (verifier.reasons if failure == "HTTP 502" else {})
    if failure == "HTTP 502":
        assert "HTTP 502" in verifier.reasons["basics/other"]

    del github.answers["/pulls/8/files"]
    # The setup with alice's permission, then the nine requests for #8, which "basics/other" sorts first to use.
    budget = len(_SETUP_CALLS) + 1 + 9
    github.hourly = (1000, budget + 50)
    github.calls.clear()
    verifier = _verified(root, github)
    assert len(github.calls) == budget
    assert list(verifier.reasons) == ["basics/result"]
    assert verifier.unchecked == verifier.reasons
    assert f"budget of {budget} GitHub API requests" in verifier.reasons["basics/result"]
    # Spent before the rules, past the head check, it leaves every approval for a later run.
    github.hourly = (1000, 52)
    assert sorted(_verified(root, github).unchecked) == ["basics/other", "basics/result"]


@pytest.mark.parametrize("failure", ["HTTP 502", "HTTP 404"])
def test_a_head_lookup_that_fails_outside_a_publishing_run_leaves_every_approval_unchecked(
    tmp_path: Path, failure: str
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head)
    github.answers["/git/ref/heads/main"] = (
        GitHubUnavailable("GitHub API GET /git/ref/heads/main failed with HTTP 502: Bad Gateway")
        if failure == "HTTP 502"
        else None
    )
    verifier = _verified(root, github, publishing=False)

    assert list(verifier.reasons) == ["basics/result"]
    assert verifier.reasons["basics/result"].startswith("cannot tell whether HEAD is the head of the default branch")
    assert verifier.unchecked == ({} if failure == "HTTP 404" else verifier.reasons)


@pytest.mark.parametrize(
    ("hourly", "budget"),
    [
        # A workflow's GITHUB_TOKEN may make 1000 requests an hour in one repository, and 15000 on Enterprise Cloud.
        ((1000, 1000), 800),
        ((1000, 850), 800),
        ((1000, 849), 799),
        ((1000, 30), 0),
        ((15000, 15000), 14800),
        # No answer: a workflow's GITHUB_TOKEN with the hour to itself, though a spent budget stays unchecked.
        (None, 800),
    ],
)
def test_the_request_budget_is_what_the_hour_has_left_and_leaves_some_of_it(
    tmp_path: Path, hourly: tuple[int, int] | None, budget: int
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    github.rules = []
    github.hourly = hourly
    # Not publishing, so a budget too small for the head check refuses rather than stops the build.
    verifier = GitHubReviewVerifier(github, trusted_ref="HEAD", publishing=False)  # type: ignore[arg-type]

    verifier.verify(load_graph(root / "blueprint"), {f"node{index}": _HASH for index in range(3)})

    assert verifier.budget == budget
    assert verifier._at_ceiling() is (hourly is not None and hourly[1] >= hourly[0] - 150)


def test_approvals_past_the_ceiling_of_a_run_with_the_hour_to_itself_are_refused_not_unchecked(
    tmp_path: Path,
) -> None:
    """No run gets further, so leaving them unchecked would fail every run, and retry each one in vain."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head)
    _branch(root, "other")
    _approve(root, "other", _OTHER_HASH)
    _commit(root, "Approve the other article")
    other_head = github.open_pull(8, "bob")
    github.review(8, "alice", "APPROVED", other_head)
    _land(root, github, 8)
    # The setup with alice's permission, then the nine requests for #8, which "basics/other" sorts first to use.
    budget = len(_SETUP_CALLS) + 1 + 9
    # A token whose hourly limit leaves exactly that budget, with the whole hour left.
    github.hourly = (budget + 200, budget + 200)

    verifier = _verified(root, github)

    assert list(verifier.reasons) == ["basics/result"]
    assert verifier.reasons["basics/result"] == (
        f"not checked: checking every approval needs more than the {budget} GitHub API requests a run may make "
        f"(GitHub's limit of {budget + 200} an hour for this repository's token, less 200 kept for the gate and "
        "later pushes), so the approvals past it stay self-approved; approvals recorded in one pull request "
        "share most of their requests"
    )
    assert verifier.unchecked == {}
    # Other runs of the hour, such as gate runs during the build, may have spent 150 before this one began,
    # and it still has the whole ceiling, so the same approval is refused.
    github.hourly = (budget + 200, budget + 50)
    verifier = _verified(root, github)
    assert list(verifier.reasons) == ["basics/result"]
    assert verifier.unchecked == {}
    # A run that began with 151 of the hour's requests spent keeps 50 back and has one request fewer, so a
    # later run might get further.
    github.hourly = (budget + 200, budget + 49)
    verifier = _verified(root, github)
    assert verifier.unchecked == verifier.reasons
    assert verifier.reasons["basics/result"].startswith(f"not checked: the budget of {budget - 1} GitHub API requests")
    # No answer from GET /rate_limit: the budget of a GITHUB_TOKEN, and anything it leaves is unchecked.
    github.hourly = None
    assert _verified(root, github).unchecked == {}


def test_an_attestation_for_another_hash_is_discarded(tmp_path: Path) -> None:
    graph = load_graph(_project(tmp_path) / "blueprint")

    class Careless:
        method = "careless"

        def verify(self, graph: object, approved: object) -> dict[str, ApprovalAttestation]:
            return {"basics/result": ApprovalAttestation("basics/result", _OTHER_HASH, "alice", "careless", "")}

    [status] = approval_statuses(graph, {"basics/result": _HASH}, Careless()).values()

    assert status.label == "self-approved"


@pytest.mark.parametrize(
    ("rules", "path", "owners"),
    [
        ("* @a\n", "blueprint/x.md", ("@a",)),
        ("* @a\nblueprint/ @b\n", "blueprint/x.md", ("@b",)),
        ("blueprint/ @b\n* @a\n", "blueprint/x.md", ("@a",)),
        ("blueprint/ @b\nblueprint/roadmap/\n", "blueprint/roadmap/x.md", ()),
        ("blueprint/ @b\n", "docs/blueprint/x.md", ("@b",)),
        ("/blueprint/ @b\n", "docs/blueprint/x.md", ()),
        ("/blueprint/ @b\n", "blueprint/x.md", ("@b",)),
        ("docs/blueprint/ @b\n", "x/docs/blueprint/y.md", ()),
        ("blueprint @b\n", "a/blueprint", ("@b",)),
        ("blueprint @b\n", "blueprint/x.md", ("@b",)),
        ("blueprint/ @b\n", "blueprint", ()),
        ("Blueprint/ @b\n", "blueprint/x.md", ()),
        ("*.md @b\n", "blueprint/roadmap/x.md", ("@b",)),
        ("blueprint/*.md @b\n", "blueprint/roadmap/x.md", ()),
        ("docs/* @b\n", "docs/a.md", ("@b",)),
        ("docs/* @b\n", "docs/x/a.md", ()),
        ("**/roadmap @b\n", "blueprint/roadmap/basics/x.md", ("@b",)),
        ("blueprint/**/result.md @b\n", "blueprint/roadmap/basics/result.md", ("@b",)),
        ("blueprint/**/result.md @b\n", "blueprint/result.md", ("@b",)),
        ("blueprint/** @b\n", "blueprint/a/b.md", ("@b",)),
        ("blueprint/r?sult.md @b\n", "blueprint/result.md", ("@b",)),
        ("blueprint/r?sult.md @b\n", "blueprint/r/sult.md", ()),
        ("# owners\n\nblueprint/ @b @org/team  # reviewers\n", "blueprint/x.md", ("@b", "@org/team")),
        ("blueprint/\t@b\t@c\r\n", "blueprint/x.md", ("@b", "@c")),
        ("docs/ @octocat_acme\nblueprint/ @alice\n", "docs/x.md", ("@octocat_acme",)),
        # An unreadable rule matters only for paths it could match.
        ("blueprint/ @b\n!docs/ @a\n", "blueprint/x.md", ("@b",)),
        ("blueprint/ @b\ndocs/[ab].md @a\n", "blueprint/x.md", ("@b",)),
        ("blueprint/ @b\ndocs/ a\n", "blueprint/x.md", ("@b",)),
    ],
)
def test_codeowners_patterns(rules: str, path: str, owners: tuple[str, ...]) -> None:
    assert code_owners(parse_codeowners(rules), path) == owners


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ("!blueprint/ @a\n", "unsupported CODEOWNERS pattern"),
        ("blueprint/[ab].md @a\n", "unsupported CODEOWNERS pattern"),
        ("blueprint/x\\ y.md @a\n", "unsupported CODEOWNERS pattern"),
        ("blueprint//x.md @a\n", "malformed CODEOWNERS pattern"),
        ("blueprint/ alice\n", "unsupported owner 'alice'"),
        ("blueprint/ @b\n# docs\u2028blueprint/ @c\n", "contains U+2028"),
    ],
)
def test_unreadable_codeowners_rules_leave_the_owners_undecided(rules: str, message: str) -> None:
    with pytest.raises(ApprovalError, match=re.escape(message)) as caught:
        code_owners(parse_codeowners(rules), "blueprint/x.md")
    assert "so the code owners of blueprint/x.md cannot be decided" in str(caught.value)


def test_an_unreadable_codeowners_rule_refuses_the_articles_it_could_own(tmp_path: Path) -> None:
    root = _project(tmp_path, "* @alice\n!blueprint/ @carol\n")
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head)

    status = _verify(root, github)["basics/result"]

    assert not status.authenticated
    assert ".github/CODEOWNERS:2: unsupported CODEOWNERS pattern '!blueprint/'" in (status.reason or "")


def test_github_reads_the_first_codeowners_location_that_exists(tmp_path: Path) -> None:
    root = _project(tmp_path, "blueprint/ @alice\n")
    (root / "CODEOWNERS").write_text("blueprint/ @carol\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "CODEOWNERS").write_text("blueprint/ @dave\n", encoding="utf-8")
    _commit(root, "Add CODEOWNERS everywhere")

    def owner() -> tuple[str, ...]:
        found = approvals.load_codeowners(root, "HEAD")
        assert found is not None
        return (found[0], *code_owners(found[1], _ARTICLE))

    assert owner() == (".github/CODEOWNERS", "@alice")
    (root / ".github" / "CODEOWNERS").unlink()
    _commit(root, "Drop .github/CODEOWNERS")
    assert owner() == ("CODEOWNERS", "@carol")
    (root / "CODEOWNERS").unlink()
    _commit(root, "Drop CODEOWNERS")
    assert owner() == ("docs/CODEOWNERS", "@dave")


def test_a_shallow_checkout_stops_authentication(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head)
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "--quiet", "--depth", "1", "--branch", "main", root.resolve().as_uri(), str(shallow))

    with pytest.raises(ApprovalError, match="needs full Git history"):
        _verify(shallow, FakeGitHub(shallow))


def test_missing_github_environment_names_what_is_missing() -> None:
    with pytest.raises(ApprovalError, match="needs GITHUB_TOKEN and GITHUB_REPOSITORY"):
        GitHubReviewVerifier.from_environment(environ={})
    with pytest.raises(ApprovalError, match="needs GITHUB_REPOSITORY in"):
        GitHubReviewVerifier.from_environment(environ={"GITHUB_TOKEN": "token"})
    with pytest.raises(ApprovalError, match="owner/name"):
        GitHubReviewVerifier.from_environment(environ={"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "project"})
    with pytest.raises(ApprovalError, match="GITHUB_SERVER_URL must be an https URL"):
        GitHubReviewVerifier.from_environment(
            environ={"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/p", "GITHUB_SERVER_URL": "http://github.com"}
        )


def test_the_environment_names_the_web_host_and_the_verify_workflow() -> None:
    verifier = GitHubReviewVerifier.from_environment(
        environ={
            "GITHUB_TOKEN": "t",
            "GITHUB_REPOSITORY": "o/p",
            "GITHUB_API_URL": "https://ghe.example/api/v3",
            "AUTOFORM_VERIFY_WORKFLOW": ".github/workflows/ci.yml",
        }
    )
    assert verifier.web_url == "https://ghe.example"
    assert verifier.verify_workflow == ".github/workflows/ci.yml"
    assert GitHubReviewVerifier.from_environment(environ={"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/p"}).web_url == (
        "https://github.com"
    )


def test_the_client_reads_404_as_no_evidence_and_fails_clearly_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[object] = []

    def urlopen(request: object, timeout: float) -> object:
        requests.append(request)
        status = 404 if "missing" in request.full_url else 403  # type: ignore[attr-defined]
        raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(b"rate limited"))  # type: ignore[attr-defined]

    monkeypatch.setattr(approvals.urllib.request, "urlopen", urlopen)
    client = GitHubClient("secret", "owner/project", api_url="https://github.example/api/v3/")

    assert client.get("/contents/missing", {"ref": "abc"}) is None
    with pytest.raises(ApprovalError, match="HTTP 403: rate limited"):
        client.get("/pulls/1/reviews")
    first = requests[0]
    assert first.full_url == "https://github.example/api/v3/repos/owner/project/contents/missing?ref=abc"  # type: ignore[attr-defined]
    # Kept off redirects, so the token never follows one to another host.
    assert first.unredirected_hdrs == {"Authorization": "Bearer secret"}  # type: ignore[attr-defined]
    with pytest.raises(ApprovalError, match="https"):
        GitHubClient("secret", "owner/project", api_url="http://github.example")


def test_the_client_refuses_an_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr(approvals.urllib.request, "urlopen", lambda request, timeout: Response(b"[" + b" " * 64 + b"]"))
    monkeypatch.setattr(approvals, "_MAX_RESPONSE_BYTES", 32)

    with pytest.raises(ApprovalError, match="returned more than 32 bytes"):
        GitHubClient("secret", "owner/project").get("/pulls/1/reviews")


@pytest.mark.parametrize(
    ("answer", "hourly"),
    [
        (b'{"resources": {"core": {"limit": 1000, "remaining": 987}}}', (1000, 987)),
        (b'{"resources": {"core": {"limit": 1000}}}', None),
        (b'{"resources": {"core": {"limit": 1000, "remaining": "987"}}}', None),
        (b'{"resources": {"core": {"limit": 1000, "remaining": true}}}', None),
        (b"[]", None),
        (b"not json", None),
        # GitHub Enterprise Server with rate limiting turned off.
        (urllib.error.HTTPError("", 404, "Not Found", {}, io.BytesIO(b"Rate limiting is not enabled.")), None),  # type: ignore[arg-type]
        (urllib.error.URLError("timed out"), None),
    ],
)
def test_the_client_reads_how_many_requests_are_left_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, answer: bytes | Exception, hourly: tuple[int, int] | None
) -> None:
    requests: list[object] = []

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def urlopen(request: object, timeout: float) -> object:
        requests.append(request)
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)

    monkeypatch.setattr(approvals.urllib.request, "urlopen", urlopen)

    assert GitHubClient("secret", "owner/project", api_url="https://github.example/api/v3/").rate_limit() == hourly
    assert requests[0].full_url == "https://github.example/api/v3/rate_limit"  # type: ignore[attr-defined]
    assert requests[0].unredirected_hdrs == {"Authorization": "Bearer secret"}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("failure", "unavailable"),
    [
        (urllib.error.HTTPError("", 500, "error", {}, io.BytesIO(b"")), True),  # type: ignore[arg-type]
        (urllib.error.HTTPError("", 502, "error", {}, io.BytesIO(b"")), True),  # type: ignore[arg-type]
        (urllib.error.HTTPError("", 408, "error", {}, io.BytesIO(b"")), True),  # type: ignore[arg-type]
        (urllib.error.HTTPError("", 429, "error", {}, io.BytesIO(b"")), True),  # type: ignore[arg-type]
        # GitHub marks its primary and secondary rate limits with these headers.
        (urllib.error.HTTPError("", 403, "error", {"x-ratelimit-remaining": "0"}, io.BytesIO(b"")), True),  # type: ignore[arg-type]
        (urllib.error.HTTPError("", 403, "error", {"retry-after": "60"}, io.BytesIO(b"")), True),  # type: ignore[arg-type]
        (urllib.error.HTTPError("", 403, "error", {"x-ratelimit-remaining": "12"}, io.BytesIO(b"")), False),  # type: ignore[arg-type]
        # A secondary limit may carry neither header, but GitHub's message says what it is.
        (
            urllib.error.HTTPError(
                "",
                403,
                "error",
                {"x-ratelimit-remaining": "812"},  # type: ignore[arg-type]
                io.BytesIO(b'{"message": "You have exceeded a secondary rate limit. Please wait a few minutes."}'),
            ),
            True,
        ),
        (
            urllib.error.HTTPError(
                "",
                403,
                "error",
                {"x-ratelimit-remaining": "12"},  # type: ignore[arg-type]
                io.BytesIO(b'{"message": "Resource not accessible by integration"}'),
            ),
            False,
        ),
        (urllib.error.HTTPError("", 401, "error", {}, io.BytesIO(b"")), False),  # type: ignore[arg-type]
        (urllib.error.HTTPError("", 410, "error", {}, io.BytesIO(b"")), False),  # type: ignore[arg-type]
        (urllib.error.HTTPError("", 422, "error", {}, io.BytesIO(b"")), False),  # type: ignore[arg-type]
        (urllib.error.URLError("timed out"), True),
        (TimeoutError("timed out"), True),
        (b"not json", True),
        (b"[" + b" " * 64 + b"]", False),
    ],
)
def test_the_client_tells_a_failure_a_later_run_may_not_repeat_from_a_verdict(
    monkeypatch: pytest.MonkeyPatch, failure: bytes | Exception, unavailable: bool
) -> None:
    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def urlopen(request: object, timeout: float) -> object:
        if isinstance(failure, Exception):
            raise failure
        return Response(failure)

    monkeypatch.setattr(approvals.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(approvals, "_MAX_RESPONSE_BYTES", 32)

    with pytest.raises(ApprovalError) as raised:
        GitHubClient("secret", "owner/project").get("/pulls/1/reviews")
    assert isinstance(raised.value, GitHubUnavailable) is unavailable


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (urllib.error.HTTPError("", 502, "error", {}, io.BytesIO(b"Bad Gateway")), "failed with HTTP 502: Bad Gateway"),  # type: ignore[arg-type]
        (urllib.error.URLError("timed out"), "failed: <urlopen error timed out>"),
        (b"not json", "returned malformed JSON"),
        (b"[" + b" " * 64 + b"]", "returned more than 32 bytes"),
    ],
    ids=["http", "network", "malformed", "oversized"],
)
def test_a_failed_request_for_the_repository_says_what_it_asked_for(
    monkeypatch: pytest.MonkeyPatch, failure: bytes | Exception, message: str
) -> None:
    """The repository's own path is empty, which left `GitHub API GET  failed` naming nothing."""

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def urlopen(request: object, timeout: float) -> object:
        if isinstance(failure, Exception):
            raise failure
        return Response(failure)

    monkeypatch.setattr(approvals.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(approvals, "_MAX_RESPONSE_BYTES", 32)

    with pytest.raises(ApprovalError) as raised:
        GitHubClient("secret", "owner/project").get("")
    assert str(raised.value) == f"GitHub API GET of the repository {message}"


# What every verification on the default branch reads first: the repository, the branch's head, its
# rules, the ruleset they come from, GitHub's errors in CODEOWNERS, and the permission of the catch-all owner.
_SETUP_CALLS = [
    "",
    "/git/ref/heads/main",
    "/rules/branches/main",
    "/rulesets/1",
    "/codeowners/errors",
    "/collaborators/owner/permission",
]


def _gate(root: Path, base: str, *, pr: int = 7, trusted_ref: str | None = None) -> int:
    return main(
        [
            "review", "authenticate", str(root / "blueprint"), "--github", "--pr", str(pr),
            "--since", base, "--trusted-ref", trusted_ref or base,
        ]
    )


def _use_fake_github(monkeypatch: pytest.MonkeyPatch, github: FakeGitHub) -> None:
    monkeypatch.setattr(approvals, "GitHubClient", lambda *args, **kwargs: github)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/project")
    for name in ("GITHUB_API_URL", "GITHUB_SERVER_URL", "AUTOFORM_VERIFY_WORKFLOW"):
        monkeypatch.delenv(name, raising=False)


def test_the_gate_requires_authentication_only_for_added_or_changed_approvals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _approve(root, "other", _OTHER_HASH)
    _approve(root, "gone", _OTHER_HASH)
    base = _commit(root, "Approve on the default branch")
    github = FakeGitHub(root)
    _use_fake_github(monkeypatch, github)

    _branch(root, "change")
    _approve(root, "gone", None)
    _commit(root, "Remove an approval")
    github.open_pull(7, "bob")
    assert _gate(root, base) == 0
    assert github.calls == []
    capsys.readouterr()

    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    head = github.push(7)
    assert _gate(root, base) == 1
    captured = capsys.readouterr()
    assert f"basics/result: self-approved · {_HASH} (#7 has no review)" in captured.out
    assert f"basics/other: unchanged since {base} · {_OTHER_HASH}" in captured.out
    assert "basics/gone" not in captured.out
    assert "1 approval added or changed since" in captured.err
    assert "must approve its final head commit" in captured.err
    assert all("other.md" not in path for path, _ in github.calls)

    github.review(7, "alice", "APPROVED", head)
    assert _gate(root, base) == 0
    output = capsys.readouterr().out
    assert f"basics/result: approved by @alice (github review) · {_HASH}" in output
    assert "OK: every approval added or changed since" in output
    # The gate asks nothing about merges or Actions runs.
    assert not any(path.startswith(("/commits/", "/actions/")) for path, _ in github.calls)

    # A review list GitHub did not answer needs a retry, not another review.
    github.answers["/pulls/7/reviews"] = GitHubUnavailable(
        "GitHub API GET /pulls/7/reviews failed with HTTP 502: Bad Gateway"
    )
    assert _gate(root, base) == 1
    captured = capsys.readouterr()
    assert f"error: 1 approval added or changed since {base} is self-approved; each line above says why\n" in captured.err

    # Nor does a review list too large to read, which every run is refused the same way.
    github.answers["/pulls/7/reviews"] = ApprovalError(
        "GitHub API GET /pulls/7/reviews returned more than 8388608 bytes"
    )
    assert _gate(root, base) == 1
    captured = capsys.readouterr()
    assert "returned more than 8388608 bytes" in captured.out
    assert f"error: 1 approval added or changed since {base} is self-approved; each line above says why\n" in captured.err


def test_the_gate_counts_only_reviews_of_its_own_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    github = FakeGitHub(root)
    _use_fake_github(monkeypatch, github)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    head = github.open_pull(7, "bob")
    # Another pull request with the same head, approved and closed unmerged.
    github.open_pull(8, "bob", head=head)
    github.review(8, "alice", "APPROVED", head)
    github.pulls[8]["state"] = "closed"

    assert _gate(root, base) == 1
    assert "(#7 has no review)" in capsys.readouterr().out
    assert _gate(root, base, pr=8) == 0


def test_the_gate_reads_code_owners_at_the_trusted_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    github = FakeGitHub(root)
    _use_fake_github(monkeypatch, github)
    (root / ".github" / "CODEOWNERS").write_text("* @owner\nblueprint/ @alice @carol\n", encoding="utf-8")
    _commit(root, "Make carol a code owner")
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    head = github.open_pull(7, "bob")
    github.review(7, "carol", "APPROVED", head)

    assert _gate(root, base) == 1
    assert "@carol approved #7 but is not an individual code owner" in capsys.readouterr().out
    # Trusting a later ref would have let its new owner approve.
    assert _gate(root, base, trusted_ref="HEAD") == 0


def test_the_gate_takes_its_base_from_the_merge_commit_it_checks_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate checks out refs/pull/N/merge, built on main as it is now; the event's base.sha can lag behind."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _use_fake_github(monkeypatch, github)
    _branch(root, "notes")
    _append(root, "A note.\n", "blueprint/README.md")
    head = _commit(root, "Add a note")
    github.open_pull(8, "bob")
    recorded = github.pulls[8]["base"]["sha"]
    # Meanwhile #7 records an approval and lands on main.
    _git(root, "checkout", "--quiet", "main")
    approving = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", approving)
    # GitHub's merge commit for #8, whose first parent is main now, ahead of #8's recorded base.
    _git(root, "checkout", "--quiet", "--detach", "main")
    _git(root, "merge", "--quiet", "--no-ff", "-m", "Merge #8", head)
    base = _git(root, "rev-parse", "HEAD^1")
    assert base != recorded

    # Diffing against the recorded base blames #8 for the approval #7 landed.
    assert _gate(root, recorded, pr=8) == 1
    assert f"basics/result: self-approved · {_HASH} (#8 changes blueprint/README.md" in capsys.readouterr().out
    assert _gate(root, base, pr=8) == 0
    output = capsys.readouterr().out
    assert f"basics/result: unchanged since {base} · {_HASH}" in output
    assert "OK: every approval added or changed since" in output


def test_the_gate_refuses_a_pull_request_into_another_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    github = FakeGitHub(root)
    _use_fake_github(monkeypatch, github)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    head = github.open_pull(7, "bob", base_ref="release")
    github.review(7, "alice", "APPROVED", head)

    assert _gate(root, base) == 1
    assert "#7 targets 'release', not the default branch 'main'" in capsys.readouterr().out


def test_the_gate_needs_the_base_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _use_fake_github(monkeypatch, github)

    assert main(["review", "authenticate", str(root / "blueprint"), "--github", "--pr", "7"]) == 2
    assert "--pr requires --since" in capsys.readouterr().err
    assert github.calls == []


def test_the_gate_names_missing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    assert _gate(root, "HEAD") == 2
    assert "needs GITHUB_TOKEN and GITHUB_REPOSITORY" in capsys.readouterr().err


def _authenticated_review_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, FakeGitHub]:
    """The review CLI fixture, its approvals merged from pull request #3, which a code owner approved."""

    blueprint = _approved_batch(tmp_path, monkeypatch, _Extraction())
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CODEOWNERS").write_text(_CODEOWNERS, encoding="utf-8")
    approved = {
        path: path.read_text(encoding="utf-8")
        for path in sorted((blueprint / "roadmap").rglob("*.md"))
        if "\nreview_approved:" in path.read_text(encoding="utf-8")
    }
    assert approved
    for path, text in approved.items():
        path.write_text(
            "".join(line for line in text.splitlines(keepends=True) if not line.startswith("review_approved:")),
            encoding="utf-8",
        )
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _commit(tmp_path, "Record the reviews")
    _branch(tmp_path, "approve")
    for path, text in approved.items():
        path.write_text(text, encoding="utf-8")
    _commit(tmp_path, "Approve the reviews")
    github = FakeGitHub(tmp_path)
    head = github.open_pull(3, "bob")
    github.review(3, "alice", "APPROVED", head)
    _land(tmp_path, github, 3)
    _use_fake_github(monkeypatch, github)
    return blueprint, github


def _render(tmp_path: Path, blueprint: Path) -> tuple[int, str]:
    site = tmp_path / "site"
    code = main(
        ["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--authenticate", "github",
         "--output", str(site)]
    )
    pages = "\n".join(path.read_text(encoding="utf-8") for path in site.rglob("*.md")) if site.exists() else ""
    return code, pages


def test_check_and_render_name_the_code_owner_who_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, _ = _authenticated_review_project(tmp_path, monkeypatch)
    check = ["review", "check", str(blueprint), "--lean-root", str(tmp_path)]

    assert main([*check, "--authenticate", "github"]) == 0
    output = capsys.readouterr().out
    assert "OK: statement reviews match" in output
    assert "basics/result: approved by @alice (github review) · sha256:" in output

    assert main([*check, "--authenticate", "github", "--json"]) == 0
    reported = json.loads(capsys.readouterr().out)["approvals"]
    assert [(item["node_id"], item["authenticated"], item["reviewer"]) for item in reported] == [
        ("basics/other", True, "alice"),
        ("basics/result", True, "alice"),
    ]

    code, pages = _render(tmp_path, blueprint)
    assert code == 0
    assert (
        '<span class="bp-review-approved">'
        '<a href="https://github.com/owner/project/pull/3#pullrequestreview-1">approved by @alice · sha256:'
    ) in pages
    assert "self-approved" not in pages


def test_check_and_render_say_why_an_approval_stayed_self_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)
    github.reviews.clear()

    assert main(["review", "check", str(blueprint), "--lean-root", str(tmp_path), "--authenticate", "github"]) == 0
    assert "basics/result: self-approved · sha256:" in (output := capsys.readouterr().out)
    assert "(#3 has no review)" in output

    code, pages = _render(tmp_path, blueprint)
    assert code == 0
    assert '<span class="bp-review-self-approved" title="#3 has no review">self-approved · sha256:' in pages
    # A refusal is a verdict, which a later build would only repeat.
    assert json.loads((tmp_path / "site/publication.json").read_text(encoding="utf-8"))["unchecked_approvals"] == {}
    # The build log says why as well, since the page shows it only on hover.
    output = capsys.readouterr().out
    assert "warning: basics/other is self-approved: #3 has no review" in output
    assert "warning: basics/result is self-approved: #3 has no review" in output


def test_a_failed_request_still_renders_the_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)
    answer = github.get

    def flaky(path: str, query: dict | None = None) -> object:
        # Past the head check, which a failed request stops instead.
        if path in ("", "/git/ref/heads/main"):
            return answer(path, query)
        raise GitHubUnavailable(f"GitHub API GET {path} failed with HTTP 502: Bad <Gateway>")

    github.get = flaky  # type: ignore[method-assign]
    code, pages = _render(tmp_path, blueprint)

    assert code == 0
    assert pages.count('class="bp-review-self-approved" title="GitHub API GET /rules/branches/main failed with HTTP 502: Bad &lt;Gateway&gt;"') == 2
    assert "bp-review-approved" not in pages
    assert "warning: basics/result is self-approved: GitHub API GET /rules/branches/main failed with HTTP 502" in capsys.readouterr().out
    # The site says what it understates, and why, for the run to fail on and a later one to retry.
    reason = "GitHub API GET /rules/branches/main failed with HTTP 502: Bad <Gateway>"
    manifest = json.loads((tmp_path / "site/publication.json").read_text(encoding="utf-8"))
    assert manifest["unchecked_approvals"] == {"basics/other": reason, "basics/result": reason}


def test_a_reason_never_starts_a_workflow_command_in_the_build_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file name or an answer from GitHub may hold a newline, after which GitHub Actions runs `::` as a command."""

    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)
    forged = "notes\n::error::forged\r\u2028"
    github.file_edits[3] = lambda entries: [*entries, {"filename": forged, "status": "added"}]
    check = ["review", "check", str(blueprint), "--lean-root", str(tmp_path), "--authenticate", "github"]

    assert main(check) == 0
    code, _ = _render(tmp_path, blueprint)
    assert code == 0
    output = capsys.readouterr().out
    assert output.count("#3 changes notes\\n::error::forged\\r\\u2028, not only articles") == 4
    assert all(not line.lstrip().startswith("::") for line in output.splitlines())

    github.answers[""] = GitHubUnavailable(f"GitHub API GET of the repository failed with HTTP 502: {forged}")
    assert _render(tmp_path, blueprint)[0] == 1
    output = capsys.readouterr().out
    assert "failed with HTTP 502: notes\\n::error::forged\\r\\u2028\n" in output
    assert all(not line.lstrip().startswith("::") for line in output.splitlines())


def test_a_reason_never_holds_an_older_workflow_command_anywhere_in_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The runner finds `##[` anywhere in a line and runs what follows, such as add-mask or stop-commands."""

    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)
    forged = "notes##[add-mask]secret###[error]forged"
    github.file_edits[3] = lambda entries: [*entries, {"filename": forged, "status": "added"}]
    check = ["review", "check", str(blueprint), "--lean-root", str(tmp_path), "--authenticate", "github"]

    assert main(check) == 0
    code, _ = _render(tmp_path, blueprint)
    assert code == 0
    output = capsys.readouterr().out
    assert output.count("#3 changes notes#\\x23[add-mask]secret##\\x23[error]forged, not only articles") == 4
    assert "##[" not in output


@pytest.mark.parametrize(
    ("char", "escaped"),
    [
        # Bidi overrides and isolates reorder a file name on screen; zero-width characters hide one.
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


def test_a_build_the_default_branch_has_moved_past_fails_before_writing_the_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Deploying it would replace the live site with one where every approval reads self-approved."""

    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)
    newer = "f" * 40
    github.heads["main"] = {"ref": "refs/heads/main", "object": {"sha": newer, "type": "commit"}}

    code, pages = _render(tmp_path, blueprint)

    assert code == 1
    assert not (tmp_path / "site").exists() and pages == ""
    output = capsys.readouterr().out
    assert (
        f"error: HEAD is {_git(tmp_path, 'rev-parse', 'HEAD')[:12]}, not {newer[:12]}, the head of main on GitHub; "
        "approvals are authenticated only at the head of the default branch, so this build stops rather than render "
        "every approval self-approved"
    ) in output
    assert "self-approved:" not in output


def test_check_and_authenticate_of_a_commit_main_has_moved_past_say_so_for_each_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Locally that is no build to stop: the findings stand, and each approval says why it is self-approved."""

    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)
    newer = "f" * 40
    github.heads["main"] = {"ref": "refs/heads/main", "object": {"sha": newer, "type": "commit"}}
    reason = (
        f"(HEAD is {_git(tmp_path, 'rev-parse', 'HEAD')[:12]}, not {newer[:12]}, the head of main on GitHub; "
        "approvals are authenticated only at the head of the default branch)"
    )

    check = ["review", "check", str(blueprint), "--lean-root", str(tmp_path), "--authenticate", "github"]
    assert main(check) == 0
    output = capsys.readouterr()
    assert "OK: statement reviews match" in output.out
    assert "basics/result: self-approved · sha256:" in output.out and output.out.count(reason) == 2
    assert "superseded" not in output.out + output.err and output.err == ""

    assert main(["review", "authenticate", str(blueprint), "--github"]) == 0
    output = capsys.readouterr()
    assert output.out.count(reason) == 2
    assert "superseded" not in output.out + output.err


def test_check_whose_head_cannot_be_read_still_reports_the_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)
    github.heads["main"] = None
    reason = (
        "(cannot tell whether HEAD is the head of the default branch on GitHub: GitHub finds no branch main, so "
        "HEAD cannot be shown to be its head)"
    )

    check = ["review", "check", str(blueprint), "--lean-root", str(tmp_path), "--authenticate", "github"]
    assert main(check) == 0
    output = capsys.readouterr().out
    assert "OK: statement reviews match" in output
    assert output.count(reason) == 2

    # A checkout that cannot be authenticated at all still reports what it found.
    monkeypatch.setattr(
        "autoform_cli.__main__._approval_verifier",
        lambda method, **kwargs: GitHubReviewVerifier(github, trusted_ref="no-such-ref", publishing=False),
    )
    assert main(check) == 2
    output = capsys.readouterr()
    assert "OK: statement reviews match" in output.out
    assert "basics/result: self-approved · sha256:" in output.out
    assert "error: " in output.err and "no-such-ref" in output.err


@pytest.mark.parametrize(
    ("failing", "reason"),
    [
        ("", "GitHub API GET of the repository failed with HTTP 502"),
        ("/git/ref/heads/main", "GitHub API GET /git/ref/heads/main failed with HTTP 502"),
        (None, "GitHub finds no branch main"),
    ],
    ids=["repository", "head", "no-branch"],
)
def test_a_build_whose_head_cannot_be_read_fails_before_writing_the_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failing: str | None,
    reason: str,
) -> None:
    """A failed lookup must not downgrade the live site to every approval self-approved either."""

    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)
    if failing is None:
        github.heads["main"] = None
    else:
        message = f"GitHub API GET {failing or 'of the repository'} failed with HTTP 502: Bad Gateway"
        github.answers[failing] = GitHubUnavailable(message)

    code, pages = _render(tmp_path, blueprint)

    assert code == 1
    assert not (tmp_path / "site").exists() and pages == ""
    output = capsys.readouterr().out
    assert f"error: cannot tell whether HEAD is the head of the default branch on GitHub: {reason}" in output
    assert "self-approved:" not in output


def _render_with_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rewrite: object
) -> str:
    """Render after replacing each status the verifier produced, as a faulty verifier might."""

    from autoform_cli import render as render_module

    blueprint, _ = _authenticated_review_project(tmp_path, monkeypatch)
    produced = render_module.approval_statuses

    def statuses(*args: object, **kwargs: object) -> dict:
        return {node_id: rewrite(status) for node_id, status in produced(*args, **kwargs).items()}  # type: ignore[operator]

    monkeypatch.setattr(render_module, "approval_statuses", statuses)
    code, pages = _render(tmp_path, blueprint)
    assert code == 0
    return pages


def test_render_ignores_a_status_for_another_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def elsewhere(status: approvals.ApprovalStatus) -> approvals.ApprovalStatus:
        attestation = ApprovalAttestation(status.node_id, _OTHER_HASH, "mallory", "github-review", "")
        return approvals.ApprovalStatus(status.node_id, _OTHER_HASH, attestation)

    pages = _render_with_statuses(tmp_path, monkeypatch, elsewhere)

    assert "@mallory" not in pages
    assert pages.count('<span class="bp-review-self-approved">self-approved · sha256:') == 2


@pytest.mark.parametrize(
    "reference",
    [
        "https://evil.example/owner/project/pull/3",
        "https://github.com.evil.example/pull/3",
        "javascript:alert(1)//https://github.com/",
        "http://github.com/owner/project/pull/3",
    ],
)
def test_render_links_reviews_only_on_the_github_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    def relinked(status: approvals.ApprovalStatus) -> approvals.ApprovalStatus:
        assert status.attestation is not None
        attestation = ApprovalAttestation(status.node_id, status.review_hash, "alice", "github-review", reference)
        return approvals.ApprovalStatus(status.node_id, status.review_hash, attestation)

    pages = _render_with_statuses(tmp_path, monkeypatch, relinked)

    assert pages.count('<span class="bp-review-approved">approved by @alice · sha256:') == 2
    assert "evil" not in pages and "javascript" not in pages and "http://" not in pages


def test_render_links_reviews_only_under_an_https_verifier_host() -> None:
    from autoform_cli.render import _linkable

    def status(reference: str) -> approvals.ApprovalStatus:
        attestation = ApprovalAttestation("basics/result", _HASH, "alice", "github-review", reference)
        return approvals.ApprovalStatus("basics/result", _HASH, attestation)

    kept = _linkable(status("https://github.com/owner/project/pull/3"), "https://github.com")
    assert kept.attestation is not None and kept.attestation.reference == "https://github.com/owner/project/pull/3"
    dropped = _linkable(status("http://github.com/owner/project/pull/3"), "http://github.com")
    assert dropped.attestation is not None and dropped.attestation.reference == ""
    assert dropped.attestation.reviewer == "alice"


def test_a_review_link_off_the_github_host_is_dropped(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head)
    github.reviews[7][0]["html_url"] = "https://evil.example/owner/project/pull/7"

    status = _verify(root, github)["basics/result"]
    assert status.attestation is not None
    assert status.attestation.reference == "https://github.com/owner/project/pull/7#pullrequestreview-1"

    github.pulls[7]["html_url"] = "javascript:alert(1)"
    status = _verify(root, github)["basics/result"]
    assert status.attestation is not None and status.attestation.reference == ""


def test_render_authenticate_needs_review_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)
    github.calls.clear()

    assert main(["render", str(blueprint), "--authenticate", "github", "--output", str(tmp_path / "site")]) == 2
    assert "--authenticate requires --review or --review-bundle" in capsys.readouterr().err
    assert github.calls == []


def test_check_and_render_say_why_no_approval_counts_without_a_code_owner_ruleset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)
    github.rules = []
    github.calls.clear()

    assert main(["review", "check", str(blueprint), "--lean-root", str(tmp_path), "--authenticate", "github"]) == 0
    output = capsys.readouterr().out
    reason = "(no active ruleset on main has a pull request rule requiring code owner review"
    assert "basics/result: self-approved · sha256:" in output
    assert output.count(reason) == 2
    # The repository is judged once for every approval, and no pull request is read.
    assert [path for path, _ in github.calls] == ["", "/git/ref/heads/main", "/rules/branches/main"]

    code, pages = _render(tmp_path, blueprint)
    assert code == 0
    assert pages.count('class="bp-review-self-approved" title="no active ruleset on main has a pull request') == 2


@pytest.mark.parametrize(("event", "hinted"), [("pull_request", True), ("pull_request_target", True), ("push", False)])
def test_authenticate_without_pr_in_a_pull_request_run_names_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], event: str, hinted: bool
) -> None:
    """The push case is a deliberate guard: only a pull request run gets the hint."""

    root = _project(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    github = FakeGitHub(root)
    _use_fake_github(monkeypatch, github)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    _branch(root, "change")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    github.open_pull(7, "bob")

    # The branch is not main's head, so the approval is refused as not at the head, after the hint.
    assert main(["review", "authenticate", str(root / "blueprint"), "--github", "--since", base]) == 1
    output = capsys.readouterr()
    assert ("hint: this is a pull request run; pass --pr with its number" in output.err) is hinted
    assert "the head of main on GitHub; approvals are authenticated only at the head of the default branch)" in output.out
    # No code owner approval would get past that, so the summary does not ask for one.
    assert f"error: 1 approval added or changed since {base} is self-approved; each line above says why\n" in output.err
