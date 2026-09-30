"""Authenticated approvals: who approved a review hash, not only that it is current."""

from __future__ import annotations

import base64
import io
import json
import os
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
_IDENTITY = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENTITY},
    ).stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "--no-verify", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _project(tmp_path: Path, codeowners: str | None = "blueprint/ @alice\n") -> Path:
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
    _git(root, "init", "--quiet")
    _commit(root, "Start the blueprint")
    return root


def _approve(root: Path, name: str, value: str | None) -> None:
    article = root / "blueprint" / "roadmap" / "basics" / f"{name}.md"
    lines = [line for line in article.read_text(encoding="utf-8").splitlines(keepends=True)
             if not line.startswith("review_approved:")]
    if value is not None:
        lines.insert(lines.index("statement: formalized\n") + 1, f"review_approved: {value}\n")
    article.write_text("".join(lines), encoding="utf-8")


class FakeGitHub:
    """Serves pull requests and reviews from memory and file contents from the test repository."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pulls: dict[str, list[dict]] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []

    def open_pull(self, number: int, author: str, *commits: str) -> None:
        pull = {
            "number": number,
            "user": {"login": author},
            "html_url": f"https://github.com/owner/project/pull/{number}",
        }
        for commit in commits:
            self.pulls.setdefault(commit, []).append(pull)

    def review(self, number: int, login: str, state: str, commit: str) -> None:
        reviews = self.reviews.setdefault(number, [])
        review_id = len(reviews) + 1
        reviews.append(
            {
                "id": review_id,
                "user": {"login": login},
                "state": state,
                "commit_id": commit,
                "html_url": f"https://github.com/owner/project/pull/{number}#pullrequestreview-{review_id}",
            }
        )

    def get(self, path: str, query: dict | None = None) -> object | None:
        query = dict(query or {})
        self.calls.append((path, query))
        parts = path.split("/")
        if parts[1] == "commits" and parts[3] == "pulls":
            return self._page(self.pulls.get(parts[2], []), query)
        if parts[1] == "pulls" and parts[3] == "reviews":
            return self._page(self.reviews.get(int(parts[2]), []), query)
        if parts[1] == "contents":
            name = urllib.parse.unquote(path[len("/contents/"):])
            blob = subprocess.run(
                ["git", "cat-file", "blob", f"{query['ref']}:{name}"], cwd=self.root, capture_output=True
            )
            if blob.returncode != 0:
                return None
            # GitHub wraps base64 content at 60 columns.
            return {"type": "file", "encoding": "base64", "content": base64.encodebytes(blob.stdout).decode()}
        return None

    @staticmethod
    def _page(items: list[dict], query: dict) -> list[dict]:
        size, page = int(query.get("per_page", 30)), int(query.get("page", 1))
        return items[(page - 1) * size : page * size]


def _verify(root: Path, github: FakeGitHub, *, trusted_ref: str = "HEAD", **kwargs: int):
    verifier = GitHubReviewVerifier(github, trusted_ref=trusted_ref, **kwargs)  # type: ignore[arg-type]
    graph = load_graph(root / "blueprint")
    approved = {node.id: node.review_approved for node in graph.nodes.values() if node.review_approved}
    return approval_statuses(graph, approved, verifier)


def _pull_approving(root: Path, github: FakeGitHub, *, author: str = "bob") -> str:
    _approve(root, "result", _HASH)
    commit = _commit(root, "Approve the result")
    github.open_pull(7, author, commit)
    return commit


def test_without_a_verifier_every_current_approval_is_self_approved(tmp_path: Path) -> None:
    graph = load_graph(_project(tmp_path) / "blueprint")

    [status] = approval_statuses(graph, {"basics/result": _HASH}).values()

    assert not status.authenticated
    assert status.label == "self-approved"


def test_a_code_owner_approving_the_recording_commit_authenticates(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    commit = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", commit)

    status = _verify(root, github)["basics/result"]

    assert status.attestation == ApprovalAttestation(
        "basics/result",
        _HASH,
        "alice",
        "github-review",
        "https://github.com/owner/project/pull/7#pullrequestreview-1",
    )
    assert status.label == "approved by @alice (github review)"


def test_the_pull_request_author_cannot_authenticate_their_own_approval(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    commit = _pull_approving(root, github, author="Alice")
    github.review(7, "alice", "APPROVED", commit)

    status = _verify(root, github)["basics/result"]

    assert status.label == "self-approved"
    assert "@alice approved #7 but is its author" in (status.reason or "")


def test_a_reviewer_who_is_not_a_code_owner_cannot_authenticate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    commit = _pull_approving(root, github)
    github.review(7, "carol", "APPROVED", commit)

    status = _verify(root, github)["basics/result"]

    assert not status.authenticated
    assert "@carol approved #7 but is not an individual code owner" in (status.reason or "")


def test_code_owners_come_from_the_trusted_ref_not_the_candidate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    github = FakeGitHub(root)
    (root / ".github" / "CODEOWNERS").write_text("blueprint/ @alice @carol\n", encoding="utf-8")
    commit = _pull_approving(root, github)
    github.review(7, "carol", "APPROVED", commit)

    assert not _verify(root, github, trusted_ref=base)["basics/result"].authenticated
    # The candidate's own CODEOWNERS would have let its new owner approve.
    assert _verify(root, github, trusted_ref="HEAD")["basics/result"].authenticated


def test_an_approval_of_a_commit_recording_another_hash_does_not_count(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approve(root, "result", _OTHER_HASH)
    reviewed = _commit(root, "Approve an earlier review")
    _approve(root, "result", _HASH)
    current = _commit(root, "Approve the current review")
    github.open_pull(7, "bob", reviewed, current)
    github.review(7, "alice", "APPROVED", reviewed)

    status = _verify(root, github)["basics/result"]

    assert not status.authenticated
    assert "does not record this hash" in (status.reason or "")


@pytest.mark.parametrize(
    ("later", "authenticated"),
    [("CHANGES_REQUESTED", False), ("DISMISSED", False), ("COMMENTED", True), ("PENDING", True)],
)
def test_a_later_verdict_voids_an_approval_but_a_comment_does_not(
    tmp_path: Path, later: str, authenticated: bool
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    commit = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", commit)
    github.review(7, "alice", later, commit)

    status = _verify(root, github)["basics/result"]

    assert status.authenticated is authenticated
    if not authenticated:
        assert f"@alice's latest review of #7 is {later}" in (status.reason or "")


def test_a_dismissed_approval_does_not_authenticate(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    commit = _pull_approving(root, github)
    # GitHub rewrites a dismissed review's own state rather than adding one.
    github.review(7, "alice", "DISMISSED", commit)

    assert not _verify(root, github)["basics/result"].authenticated


def test_team_and_email_owners_never_authenticate(tmp_path: Path) -> None:
    root = _project(tmp_path, "blueprint/ @owner/reviewers alice@example.com\n")
    github = FakeGitHub(root)
    commit = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", commit)

    status = _verify(root, github)["basics/result"]

    assert not status.authenticated
    assert "teams and email owners cannot be verified" in (status.reason or "")
    assert "name an individual @user" in (status.reason or "")
    assert github.calls == []


def test_no_codeowners_file_allows_nobody(tmp_path: Path) -> None:
    root = _project(tmp_path, codeowners=None)
    github = FakeGitHub(root)
    commit = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", commit)

    status = _verify(root, github)["basics/result"]

    assert not status.authenticated
    assert "has no CODEOWNERS file" in (status.reason or "")


def test_reviews_are_read_across_pages(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    commit = _pull_approving(root, github)
    for number in range(100):
        github.review(7, f"reader{number}", "COMMENTED", commit)
    github.review(7, "alice", "APPROVED", commit)

    assert _verify(root, github)["basics/result"].authenticated
    pages = [query for path, query in github.calls if path == "/pulls/7/reviews"]
    assert pages == [{"per_page": 100, "page": 1}, {"per_page": 100, "page": 2}]


def test_lookups_are_cached_and_the_request_budget_fails_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approve(root, "result", _HASH)
    _approve(root, "other", _HASH)
    commit = _commit(root, "Approve two articles")
    github.open_pull(7, "bob", commit)
    github.review(7, "alice", "APPROVED", commit)

    statuses = _verify(root, github)
    assert all(status.authenticated for status in statuses.values())
    assert [path for path, _ in github.calls].count("/pulls/7/reviews") == 1
    assert [path for path, _ in github.calls].count(f"/commits/{commit}/pulls") == 1

    github.calls.clear()
    statuses = _verify(root, github, max_requests=2)
    assert len(github.calls) == 2
    assert not any(status.authenticated for status in statuses.values())
    assert all("budget of 2 GitHub API requests" in (status.reason or "") for status in statuses.values())


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
        ("# owners\n\nblueprint/ @b @org/team  # reviewers\n", "blueprint/x.md", ("@b", "@org/team")),
    ],
)
def test_codeowners_patterns(rules: str, path: str, owners: tuple[str, ...]) -> None:
    assert code_owners(parse_codeowners(rules), path) == owners


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ("!blueprint/ @a\n", "unsupported CODEOWNERS pattern"),
        ("blueprint/[ab].md @a\n", "unsupported CODEOWNERS pattern"),
        ("blueprint\\ x @a\n", "unsupported CODEOWNERS pattern"),
        ("blueprint//x @a\n", "malformed CODEOWNERS pattern"),
        ("blueprint/ alice\n", "unsupported owner 'alice'"),
    ],
)
def test_unsupported_codeowners_syntax_fails_closed(rules: str, message: str) -> None:
    with pytest.raises(ApprovalError, match=message):
        parse_codeowners(rules)


def test_an_unreadable_codeowners_file_stops_authentication(tmp_path: Path) -> None:
    root = _project(tmp_path, "* @alice\n!blueprint/ @carol\n")
    github = FakeGitHub(root)
    _pull_approving(root, github)

    with pytest.raises(ApprovalError, match=r"CODEOWNERS:2: unsupported CODEOWNERS pattern"):
        _verify(root, github)


def test_missing_github_environment_names_what_is_missing() -> None:
    with pytest.raises(ApprovalError, match="needs GITHUB_TOKEN and GITHUB_REPOSITORY"):
        GitHubReviewVerifier.from_environment(environ={})
    with pytest.raises(ApprovalError, match="needs GITHUB_REPOSITORY in"):
        GitHubReviewVerifier.from_environment(environ={"GITHUB_TOKEN": "token"})
    with pytest.raises(ApprovalError, match="owner/name"):
        GitHubReviewVerifier.from_environment(environ={"GITHUB_TOKEN": "token", "GITHUB_REPOSITORY": "project"})


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


def _gate(root: Path, base: str) -> int:
    return main(["review", "authenticate", str(root / "blueprint"), "--github", "--since", base, "--trusted-ref", base])


def test_the_gate_requires_authentication_only_for_added_or_changed_approvals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    _approve(root, "other", _OTHER_HASH)
    _approve(root, "gone", _OTHER_HASH)
    base = _commit(root, "Approve on the default branch")
    github = FakeGitHub(root)
    monkeypatch.setattr(approvals, "GitHubClient", lambda *args, **kwargs: github)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/project")

    _approve(root, "gone", None)
    _commit(root, "Remove an approval")
    assert _gate(root, base) == 0
    assert github.calls == []
    capsys.readouterr()

    _approve(root, "result", _HASH)
    commit = _commit(root, "Approve the result")
    github.open_pull(7, "bob", commit)
    assert _gate(root, base) == 1
    captured = capsys.readouterr()
    assert f"basics/result: self-approved · {_HASH} (" in captured.out
    assert f"basics/other: unchanged since {base} · {_OTHER_HASH}" in captured.out
    assert "basics/gone" not in captured.out
    assert "1 approval added or changed since" in captured.err
    assert all("other.md" not in path for path, _ in github.calls)

    github.review(7, "alice", "APPROVED", commit)
    assert _gate(root, base) == 0
    output = capsys.readouterr().out
    assert f"basics/result: approved by @alice (github review) · {_HASH}" in output
    assert "OK: every approval added or changed since" in output


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
    """The review CLI fixture, committed with a code owner's approving review."""

    blueprint = _approved_batch(tmp_path, monkeypatch, _Extraction())
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CODEOWNERS").write_text("blueprint/ @alice\n", encoding="utf-8")
    _git(tmp_path, "init", "--quiet")
    commit = _commit(tmp_path, "Approve the reviews")
    github = FakeGitHub(tmp_path)
    github.open_pull(3, "bob", commit)
    github.review(3, "alice", "APPROVED", commit)
    monkeypatch.setattr(approvals, "GitHubClient", lambda *args, **kwargs: github)
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/project")
    return blueprint, github


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

    site = tmp_path / "site"
    assert main(
        ["render", str(blueprint), "--lean-root", str(tmp_path), "--review", "--authenticate", "github",
         "--output", str(site)]
    ) == 0
    pages = "\n".join(path.read_text(encoding="utf-8") for path in site.rglob("*.md"))
    assert (
        '<span class="bp-review-approved">'
        '<a href="https://github.com/owner/project/pull/3#pullrequestreview-1">approved by @alice · sha256:'
    ) in pages
    assert "self-approved" not in pages


def test_render_authenticate_needs_review_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint, github = _authenticated_review_project(tmp_path, monkeypatch)

    assert main(["render", str(blueprint), "--authenticate", "github", "--output", str(tmp_path / "site")]) == 2
    assert "--authenticate requires --review or --review-bundle" in capsys.readouterr().err
    assert github.calls == []
