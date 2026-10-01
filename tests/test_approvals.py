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
    default the repository is set up as the README asks: a ruleset requires
    code owner review on main, and everyone named has write permission.
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
        self.rules: list[dict] = [
            {
                "type": "pull_request",
                "ruleset_source_type": "Repository",
                "ruleset_source": "owner/project",
                "ruleset_id": 1,
                "parameters": {
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": True,
                    "require_last_push_approval": False,
                    "required_approving_review_count": 1,
                    "required_review_thread_resolution": False,
                },
            }
        ]
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

    def get(self, path: str, query: dict | None = None) -> object | None:
        query = dict(query or {})
        self.calls.append((path, query))
        if path == "":
            return {**self.repository, "default_branch": self.default_branch}
        parts = path.split("/")
        if parts[1] == "commits" and parts[3:] == ["pulls"]:
            return self._page([self.pulls[number] for number in self.associated.get(parts[2], [])], query)
        if parts[1:3] == ["rules", "branches"]:
            return self._page(self.rules, query)
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
        f"(before {landed[:12]}) and at HEAD"
    ) in (status.reason or "")
    assert _verify(root, github, trusted_ref=landed)["basics/result"].authenticated


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
    assert requested[:4] == [*_SETUP_CALLS, "/collaborators/alice/permission"]
    # Once for both approvals' pull request, whose approver's permission is already known.
    assert requested[4:] == [
        f"/commits/{landed}/pulls",
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
    statuses = _verify(root, github, max_requests=2)
    assert len(github.calls) == 2
    assert not any(status.authenticated for status in statuses.values())
    assert all("budget of 2 GitHub API requests" in (status.reason or "") for status in statuses.values())


def test_a_failed_request_refuses_only_the_approvals_that_need_it(tmp_path: Path) -> None:
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
    answer = github.get

    def flaky(path: str, query: dict | None = None) -> object | None:
        if path == "/pulls/8/files":
            raise ApprovalError(f"GitHub API GET {path} failed with HTTP 502: Bad Gateway")
        return answer(path, query)

    github.get = flaky  # type: ignore[method-assign]
    statuses = _verify(root, github)

    assert statuses["basics/result"].authenticated
    assert not statuses["basics/other"].authenticated
    assert "HTTP 502" in (statuses["basics/other"].reason or "")


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


# What every verification reads first: the repository, its rules, and the permission of the catch-all owner.
_SETUP_CALLS = ["", "/rules/branches/main", "/collaborators/owner/permission"]


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


def test_a_failed_request_still_renders_the_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)

    def flaky(path: str, query: dict | None = None) -> object:
        raise ApprovalError(f"GitHub API GET {path} failed with HTTP 502: Bad <Gateway>")

    github.get = flaky  # type: ignore[method-assign]
    code, pages = _render(tmp_path, blueprint)

    assert code == 0
    assert pages.count('class="bp-review-self-approved" title="GitHub API GET  failed with HTTP 502: Bad &lt;Gateway&gt;"') == 2
    assert "bp-review-approved" not in pages


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
    assert [path for path, _ in github.calls] == ["", "/rules/branches/main"]

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

    assert main(["review", "authenticate", str(root / "blueprint"), "--github", "--since", base]) == 1
    err = capsys.readouterr().err
    assert ("hint: this is a pull request run; pass --pr with its number" in err) is hinted
