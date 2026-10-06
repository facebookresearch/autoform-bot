"""Forged approvals from the adversarial review, each refused with its reason,
beside the legitimate merges that must still authenticate."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli import approvals
from autoform_cli.approvals import (
    ApprovalError,
    HeadCheckError,
    SupersededBuildError,
    code_owners,
    parse_codeowners,
)
from tests.test_approvals import (
    _ARTICLE,
    _CODEOWNERS,
    _HASH,
    _OTHER_HASH,
    _SETUP_CALLS,
    _VERIFY,
    FakeGitHub,
    _append,
    _approve,
    _branch,
    _commit,
    _git,
    _land,
    _project,
    _pull_approving,
    _verified,
    _verify,
)


def _refused(root: Path, github: FakeGitHub, reason: str) -> None:
    status = _verify(root, github)["basics/result"]
    assert status.label == "self-approved", status.attestation
    assert reason in (status.reason or ""), status.reason


def _pasted_on_main(root: Path, github: FakeGitHub, *, extra: str = "") -> None:
    """Mallory's pull request #5 pastes the hash `review check` printed; only
    carol, who owns nothing, approves it, and it is merged anyway."""

    _branch(root, "paste")
    _approve(root, "result", _HASH)
    if extra:
        article = root / _ARTICLE
        text = article.read_text(encoding="utf-8")
        article.write_text(text.replace("statement: formalized\n", f"statement: formalized\n{extra}"), encoding="utf-8")
    _commit(root, "Paste the hash review check printed")
    github.open_pull(5, "mallory")
    github.review(5, "carol", "APPROVED")
    _land(root, github, 5, "merge")


# A1: an owner approving an unrelated pull request launders a pasted hash.
@pytest.mark.parametrize(
    ("strategy", "reason"),
    [
        ("merge", "@carol approved #5 but is not an individual code owner"),
        ("squash", "@carol approved #5 but is not an individual code owner"),
        ("rebase", "the diff of #6 does not add a review_approved line with this hash"),
    ],
)
def test_a1_an_owner_approving_an_unrelated_pull_request_launders_nothing(
    tmp_path: Path, strategy: str, reason: str
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _pasted_on_main(root, github)
    _refused(root, github, "@carol approved #5 but is not an individual code owner")

    # Two intermediate commits drop and restore the line; the net diff alice
    # reviews only fixes a typo below the hashed statement.
    _branch(root, "typo")
    _approve(root, "result", None)
    _commit(root, "wip")
    _approve(root, "result", _HASH)
    _commit(root, "wip 2")
    _append(root, "\n## Notes\n\nFix a typo in the notes.\n")
    _commit(root, "Fix a typo in the notes")
    github.open_pull(6, "mallory")
    assert "review_approved" not in _git(root, "diff", "main", "typo", "--", _ARTICLE)
    github.review(6, "alice", "APPROVED")
    _land(root, github, 6, strategy)

    _refused(root, github, reason)


def test_a1_a_decoy_line_in_the_body_does_not_count_as_recording_the_hash(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _pasted_on_main(root, github)
    _branch(root, "typo")
    _approve(root, "result", None)
    _commit(root, "wip")
    _approve(root, "result", _HASH)
    _commit(root, "wip 2")
    _append(root, f"\nreview_approved: {_HASH}\n")
    _commit(root, "Quote the approval in the notes")
    github.open_pull(6, "mallory")
    github.review(6, "alice", "APPROVED")
    _land(root, github, 6, "rebase")

    _refused(root, github, "the diff of #6 does not add a review_approved line with this hash")


def test_a1_one_diff_line_that_parses_as_two_does_not_record_the_hash(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "hide")
    article = root / _ARTICLE
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            "statement: formalized\n", f"statement: formalized\u2028review_approved: {_HASH}\n"
        ),
        encoding="utf-8",
    )
    _commit(root, "Reword the statement status")
    github.open_pull(7, "mallory")
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7)

    _refused(root, github, "the diff of #7 does not add a review_approved line with this hash")


# A1b: deleting a hidden second copy of the hash, a frontmatter comment the parser skips.
@pytest.mark.parametrize("strategy", ["merge", "rebase"])
def test_a1b_deleting_a_hidden_copy_of_the_hash_launders_nothing(tmp_path: Path, strategy: str) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _pasted_on_main(root, github, extra=f"# cache {_HASH}\n")
    github.reviews.clear()

    _branch(root, "cleanup")
    article = root / _ARTICLE
    article.write_text(article.read_text(encoding="utf-8").replace(f"# cache {_HASH}\n", ""), encoding="utf-8")
    _commit(root, "Drop a stale cache comment")
    github.open_pull(6, "mallory")
    github.review(6, "alice", "APPROVED")
    _land(root, github, 6, strategy)

    _refused(root, github, "#5 has no review")


# A2: an evil merge on a pull request branch replays a revoked approval.
def test_a2_an_evil_merge_cannot_replay_a_revoked_approval(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    approved = github.open_pull(1, "bob")
    github.review(1, "alice", "APPROVED")
    _land(root, github, 1, "merge")
    _branch(root, "revoke")
    _approve(root, "result", None)
    _commit(root, "Revoke the approval")
    github.open_pull(2, "alice")
    _land(root, github, 2, "merge")

    # Mallory branches from before the revocation, merges main back in, and
    # resolves the merge by keeping the approved article.
    _branch(root, "replay", approved)
    _append(root, "Unrelated.\n", "blueprint/roadmap/basics/other.md")
    _commit(root, "Unrelated change")
    _git(root, "merge", "--quiet", "--no-commit", "main")
    _git(root, "checkout", approved, "--", _ARTICLE)
    _commit(root, "Merge main into replay")
    github.open_pull(9, "mallory")
    _land(root, github, 9, "merge")

    _refused(root, github, "#9 has no review")


# A3: a pull request that edits CODEOWNERS labels itself approved once merged.
_GRAB = "* @owner\nblueprint/ @alice\nblueprint/roadmap/ @carol\n"


def test_a3_a_pull_request_cannot_name_its_own_code_owner(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "grab")
    (root / ".github" / "CODEOWNERS").write_text(_GRAB, encoding="utf-8")
    _approve(root, "result", _HASH)
    _commit(root, "Approve result; tidy CODEOWNERS")
    github.open_pull(4, "mallory")
    github.review(4, "carol", "APPROVED")
    landed = _land(root, github, 4, "merge")

    parent = _git(root, "rev-parse", f"{landed}^")
    _refused(
        root,
        github,
        f"no individual @user is a code owner of {_ARTICLE} both at {parent[:12]} (before #4) and at HEAD",
    )


@pytest.mark.parametrize("strategy", ["rebase", "merge", "squash"])
def test_a3_a_pull_request_cannot_name_its_own_code_owner_one_commit_earlier(tmp_path: Path, strategy: str) -> None:
    """Rebased, the commit recording the hash has the CODEOWNERS change as its
    first parent, so carol owned the article just before it, but not at #4's
    base, before both. The merge and squash cases are deliberate guards: the
    landing commit's first parent is that base."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    base = _git(root, "rev-parse", "main")
    _branch(root, "grab")
    (root / ".github" / "CODEOWNERS").write_text(_GRAB, encoding="utf-8")
    _commit(root, "Tidy CODEOWNERS")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    github.open_pull(4, "mallory")
    github.review(4, "carol", "APPROVED")
    _land(root, github, 4, strategy)

    _refused(root, github, f"code owner of {_ARTICLE} both at {base[:12]}")


@pytest.mark.parametrize("strategy", ["rebase", "merge", "squash"])
def test_a3_a_pull_request_cannot_name_its_own_code_owner_and_take_it_back(tmp_path: Path, strategy: str) -> None:
    """#4 makes carol an owner, records the hash, and restores CODEOWNERS, so
    its file list shows only the article. Rebased, the recording commit's
    first parent is #4's own CODEOWNERS change; only #4's base, before all
    three, shows carol owned nothing. The merge and squash cases are
    deliberate guards: the landing commit's first parent is that base."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    base = _git(root, "rev-parse", "main")
    codeowners = root / ".github" / "CODEOWNERS"
    _branch(root, "grab")
    codeowners.write_text(_GRAB, encoding="utf-8")
    _commit(root, "Tidy CODEOWNERS")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    codeowners.write_text(_CODEOWNERS, encoding="utf-8")
    _commit(root, "Restore CODEOWNERS")
    github.open_pull(4, "mallory")
    github.review(4, "carol", "APPROVED")
    _land(root, github, 4, strategy)
    # Later, under review, carol is given the roadmap.
    codeowners.write_text(_GRAB, encoding="utf-8")
    _commit(root, "Hand the roadmap to carol")

    _refused(root, github, f"no individual @user is a code owner of {_ARTICLE} both at {base[:12]} (before #4)")


def test_a_rebased_pull_request_is_judged_by_the_owners_at_its_base(tmp_path: Path) -> None:
    """Deliberate guard: the walk passes the pull request's own commits and stops at its base."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    base = _git(root, "rev-parse", "main")
    other = "blueprint/roadmap/basics/other.md"
    _branch(root, "approve")
    _append(root, "\nA remark.\n", other)
    _commit(root, "Remark on the other article")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    _append(root, "\nAnother remark.\n", other)
    _commit(root, "Remark again")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7, "rebase")
    first, recording = _git(root, "rev-list", "--reverse", f"{base}..main").split()[:2]

    _authenticated(root, github)
    requested = [path for path, _ in github.calls]
    assert requested[requested.index(f"/commits/{recording}/pulls") :][:3] == [
        f"/commits/{recording}/pulls",
        f"/commits/{first}/pulls",
        f"/commits/{base}/pulls",
    ]


def test_a_pull_request_that_introduced_every_earlier_commit_shows_no_owners_before_it(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.associate(7, *_git(root, "rev-list", "main").split())

    _refused(root, github, "#7 introduced every first-parent ancestor of")


def test_a_base_further_back_than_a_pull_request_has_commits_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    landed = _git(root, "rev-parse", "main")
    github.associate(7, _git(root, "rev-parse", "main^"))
    monkeypatch.setattr(approvals, "_MAX_PULL_COMMITS", 1)

    _refused(root, github, f"#7 landed more than 1 commits before {landed[:12]}, so its base cannot be found")


def test_a_pull_request_listed_without_a_number_leaves_the_base_unknown(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    parent = _git(root, "rev-parse", "main^")
    github.answers[f"/commits/{parent}/pulls"] = [{"number": "7"}]

    _refused(root, github, f"GitHub listed a pull request without a number for commit {parent[:12]}")


# A4: a review by an account GitHub shows without write access.
@pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", None])
def test_a4_a_reviewer_without_write_access_does_not_count(tmp_path: Path, association: str | None) -> None:
    root = _project(tmp_path, "* @owner\nblueprint/ @alice-old\n")
    github = FakeGitHub(root)
    _pull_approving(root, github, author="mallory")
    github.review(7, "alice-old", "APPROVED", association=association)  # type: ignore[arg-type]

    _refused(root, github, f"@alice-old approved #7 but GitHub does not show them with write access ({association})")


# A5: nothing showed the hash was current where it was approved.
@pytest.mark.parametrize(
    "run",
    [
        None,
        {"conclusion": "failure"},
        {"conclusion": None, "status": "in_progress"},
        {"path": ".github/workflows/other.yml"},
        {"event": "push"},
        {"event": "pull_request_target"},
    ],
)
def test_a5_an_approval_needs_a_green_verify_run_on_the_approved_head(tmp_path: Path, run: dict | None) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _commit(root, "Record a hash for a surface that does not exist")
    head = github.open_pull(7, "mallory", ci=None)
    if run is not None:
        github.run(head, **run)
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7)

    _refused(root, github, f"{_VERIFY} has no successful pull_request run on the head {head[:12]} of #7")


def test_a5_a_green_run_on_an_earlier_commit_does_not_count(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    github.open_pull(7, "bob")
    _append(root, "\nA note.\n")
    _commit(root, "Add a note")
    head = github.push(7, ci="failure")
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7)

    _refused(root, github, f"has no successful pull_request run on the head {head[:12]} of #7")


def test_a5_a_pull_request_that_edits_the_verify_workflow_proves_nothing_by_its_run(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / _VERIFY).write_text("name: autoform verify\n", encoding="utf-8")
    _commit(root, "Add the verify workflow")
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    (root / _VERIFY).write_text("name: autoform verify\n# skip the review check\n", encoding="utf-8")
    _commit(root, "Approve the result")
    github.open_pull(7, "mallory")
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7)

    _refused(root, github, f"#7 changes {_VERIFY}, not only articles and read-back cards")


def test_a5_a_pull_request_that_moves_the_verify_workflow_away_proves_nothing_by_its_run(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / _VERIFY).write_text("name: autoform verify\n", encoding="utf-8")
    _commit(root, "Add the verify workflow")
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _git(root, "mv", _VERIFY, ".github/workflows/renamed.yml")
    _commit(root, "Approve the result")
    github.open_pull(7, "mallory")
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7)

    _refused(root, github, f"#7 changes {_VERIFY}, .github/workflows/renamed.yml, not only articles")


# A5: a run counts only when all of it matches, even if GitHub ignored the query filters.
_HEAD = "b" * 40
_GREEN = {
    "path": f"{_VERIFY}@refs/pull/7/merge",
    "event": "pull_request",
    "head_sha": _HEAD,
    "status": "completed",
    "conclusion": "success",
}


@pytest.mark.parametrize(
    "change",
    [
        {"path": ".github/workflows/other.yml"},
        {"event": "push"},
        {"head_sha": "c" * 40},
        {"status": "in_progress"},
        {"conclusion": "neutral"},
    ],
)
def test_a5_only_a_completed_green_pull_request_run_of_the_verify_workflow_on_the_head_counts(change: dict) -> None:
    from autoform_cli.approvals import _succeeded

    assert _succeeded(_GREEN, _VERIFY, _HEAD)
    assert not _succeeded({**_GREEN, **change}, _VERIFY, _HEAD)


# A6: an uppercase hash is the same approval, and a legitimate one authenticates.
def test_a6_an_uppercase_hash_authenticates(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", "sha256:" + "A" * 64)
    _commit(root, "Approve with an uppercase hash")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7)

    status = _verify(root, github)["basics/result"]
    assert status.review_hash == _HASH
    assert status.authenticated, status.reason


# A7: Enterprise Managed User logins contain an underscore.
def test_a7_an_enterprise_managed_user_can_own_and_approve(tmp_path: Path) -> None:
    root = _project(tmp_path, "* @owner\ndocs/ @docs_acme\nblueprint/ @octocat_acme\n")
    github = FakeGitHub(root)
    _pull_approving(root, github)
    github.review(7, "octocat_acme", "APPROVED")

    status = _verify(root, github)["basics/result"]
    assert status.label == "approved by @octocat_acme (github review)", status.reason


# A8: GitHub reads the first CODEOWNERS that exists, even one that is not UTF-8.
def test_a8_an_undecodable_first_codeowners_does_not_fall_through(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".github" / "CODEOWNERS").write_bytes(b"# Andr\xe9\nblueprint/ @alice\n")
    (root / "CODEOWNERS").write_text("blueprint/ @carol\n", encoding="utf-8")
    _commit(root, "Add CODEOWNERS")
    github = FakeGitHub(root)
    _pull_approving(root, github)
    github.review(7, "carol", "APPROVED")

    _refused(root, github, ".github/CODEOWNERS is not UTF-8, so the code owners it names cannot be decided")


# B1: characters Python splits on but GitHub may not hide an owner rule in a comment.
@pytest.mark.parametrize("separator", ["\u2028", "\x0b", "\x1c", "\x85", "\x0c", "\u00a0"])
def test_b1_a_hidden_separator_leaves_the_owners_undecided(tmp_path: Path, separator: str) -> None:
    rules = f"* @owner\nblueprint/ @alice\n# reviewers, see docs{separator}blueprint/ @mallory\n"
    root = _project(tmp_path, rules)
    github = FakeGitHub(root)
    _pull_approving(root, github, author="bob")
    github.review(7, "mallory", "APPROVED")

    _refused(root, github, f".github/CODEOWNERS:3 contains U+{ord(separator):04X}")
    # The line could hold any rule, so it leaves every path undecided.
    with pytest.raises(ApprovalError, match="cannot be decided"):
        code_owners(parse_codeowners(f"docs/ @a\n/src/ @b{separator}@c\n"), "docs/x.md")


# B1b: a no-break space is not a token separator for GitHub.
def test_b1b_a_no_break_space_does_not_split_owner_tokens(tmp_path: Path) -> None:
    root = _project(tmp_path, "* @owner\nblueprint/ @alice\nblueprint/\u00a0@mallory\n")
    github = FakeGitHub(root)
    _pull_approving(root, github)
    github.review(7, "mallory", "APPROVED")

    _refused(root, github, ".github/CODEOWNERS:3 contains U+00A0")


# Finding 9 and its relatives: only a pull request merged into the default
# branch attributes the commit that recorded the hash.
def test_a_direct_push_is_attributed_to_no_pull_request(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approve(root, "result", _HASH)
    commit = _commit(root, "Approve on main directly")

    _refused(root, github, f"commit {commit[:12]}, which recorded this hash, came from no pull request merged into main")


def test_an_unmerged_pull_request_approved_by_an_owner_does_not_count(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    commit = _commit(root, "Approve the result")
    github.open_pull(8, "mallory")
    github.review(8, "alice", "APPROVED")
    github.pulls[8]["state"] = "closed"
    # The approved commit then reaches main by a push, not by merging #8.
    _git(root, "checkout", "--quiet", "main")
    _git(root, "merge", "--quiet", "--ff-only", "approve")

    _refused(root, github, f"commit {commit[:12]}, which recorded this hash, came from no pull request merged into main")


def test_a_pull_request_merged_into_another_branch_does_not_count(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    github.open_pull(7, "mallory", base_ref="release")
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7)

    _refused(root, github, "came from no pull request merged into main")


def test_a_commit_in_several_merged_pull_requests_is_ambiguous(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED")
    github.open_pull(8, "mallory", head=head, base=_git(root, "rev-parse", "HEAD~1"))
    github.merged(8, _git(root, "rev-parse", "HEAD"))

    _refused(root, github, "belongs to several pull requests: #7, #8")


def test_a_hash_recorded_in_the_first_commit_was_never_reviewed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _approve(root, "result", _HASH)
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "--amend", "--no-edit")
    github = FakeGitHub(root)

    _refused(root, github, "has recorded this hash since the first commit")
    assert [path for path, _ in github.calls] == [*_SETUP_CALLS, "/collaborators/alice/permission"]


def test_a_moved_article_needs_a_fresh_review(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _pull_approving(root, github)
    github.review(7, "alice", "APPROVED")
    assert _verify(root, github)["basics/result"].authenticated

    _branch(root, "move")
    chapter = root / "blueprint" / "roadmap" / "basics"
    (chapter / "result.md").rename(chapter / "moved.md")
    readme = chapter / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8").replace("(result.md)", "(moved.md)"), encoding="utf-8")
    _commit(root, "Move the result")
    github.open_pull(8, "mallory")
    github.review(8, "alice", "APPROVED")
    _land(root, github, 8)

    status = _verify(root, github)["basics/moved"]
    assert not status.authenticated
    assert "blueprint/roadmap/basics/moved.md in #8" in (status.reason or "")


@pytest.mark.parametrize(
    "edit",
    [
        lambda entry: entry.pop("patch"),
        lambda entry: entry.update(additions=entry["additions"] + 1),
        lambda entry: entry.update(patch=entry["patch"].replace("@@ -", "@@ garbled -", 1)),
    ],
)
def test_a_diff_github_cut_short_fails_closed(tmp_path: Path, edit: object) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _pull_approving(root, github)
    github.review(7, "alice", "APPROVED")

    def cut(entries: list[dict]) -> list[dict]:
        for entry in entries:
            if entry["filename"] == _ARTICLE:
                edit(entry)  # type: ignore[operator]
        return entries

    github.file_edits[7] = cut

    _refused(root, github, f"GitHub shows no complete diff of {_ARTICLE} in #7")


def test_the_recorded_hash_must_be_at_the_head_of_the_pull_request(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _OTHER_HASH)
    _commit(root, "Approve another hash")
    head = github.open_pull(7, "mallory")
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7)
    # The hash on main was changed after the reviewed head, by a direct push
    # GitHub still associates with #7.
    _approve(root, "result", _HASH)
    pushed = _commit(root, "Change the approval")
    github.associate(7, pushed)

    _refused(root, github, f"{_ARTICLE} does not record this hash at the head {head[:12]} of #7")


# Preconditions of A1 that already failed to launder; they must keep failing.
@pytest.mark.parametrize("strategy", ["merge", "squash"])
def test_a1_without_a_net_article_change_still_launders_nothing(tmp_path: Path, strategy: str) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _pasted_on_main(root, github)
    _branch(root, "typo")
    _approve(root, "result", None)
    _commit(root, "wip")
    _approve(root, "result", _HASH)
    _commit(root, "wip 2")
    _append(root, "Typo fixed.\n", "blueprint/README.md")
    _commit(root, "Fix a typo elsewhere")
    github.open_pull(6, "mallory")
    github.review(6, "alice", "APPROVED")
    _land(root, github, 6, strategy)

    _refused(root, github, "@carol approved #5 but is not an individual code owner")


# Round 3. Code owner review must guard everything an approval rests on, and
# an approving pull request may change nothing else.


def _approved(root: Path, github: FakeGitHub, *, strategy: str = "squash", reviewer: str = "alice") -> str:
    """The legitimate case: bob's #7 records the hash and alice approves its head; return that head."""

    head = _pull_approving(root, github, strategy=strategy)
    github.review(7, reviewer, "APPROVED", head)
    return head


def _authenticated(root: Path, github: FakeGitHub, reviewer: str = "alice") -> None:
    status = _verify(root, github)["basics/result"]
    assert status.authenticated, status.reason
    assert status.attestation is not None and status.attestation.reviewer == reviewer


_NO_RULESET = "no active ruleset on main has a pull request rule requiring code owner review"


def _owners_grant(root: Path, github: FakeGitHub, strategy: str) -> None:
    """Repro C2: mallory merges, unreviewed, a CODEOWNERS line naming herself;
    a sock puppet records the hash and mallory approves it."""

    _branch(root, "owners")
    codeowners = root / ".github" / "CODEOWNERS"
    codeowners.write_text(
        codeowners.read_text(encoding="utf-8") + f"{_ARTICLE} @mallory\n", encoding="utf-8"
    )
    _commit(root, "Add myself as a reviewer for result")
    github.open_pull(5, "mallory")
    _land(root, github, 5, strategy)
    _branch(root, "paste")
    _approve(root, "result", _HASH)
    _commit(root, "Record approval")
    github.open_pull(6, "sock-puppet")
    github.review(6, "mallory", "APPROVED", association="MEMBER")
    _land(root, github, 6, strategy)


@pytest.mark.parametrize("strategy", ["merge", "squash", "rebase"])
def test_c2_without_a_ruleset_requiring_code_owners_an_unreviewed_owner_change_authenticates_nothing(
    tmp_path: Path, strategy: str
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    github.rules = []
    _owners_grant(root, github, strategy)

    _refused(root, github, _NO_RULESET)


def test_rules_github_finds_nothing_for_read_as_no_ruleset(tmp_path: Path) -> None:
    """GitHub may answer 404 for the rules of a branch no rule applies to; that refuses, not as a failed request."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.rules = None  # type: ignore[assignment]

    _refused(root, github, _NO_RULESET)


@pytest.mark.parametrize("strategy", ["merge", "squash"])
def test_c2_codeowners_that_own_no_codeowners_file_authenticate_nothing(tmp_path: Path, strategy: str) -> None:
    root = _project(tmp_path, "blueprint/ @alice\n")
    github = FakeGitHub(root)
    _owners_grant(root, github, strategy)

    _refused(
        root,
        github,
        ".github/CODEOWNERS at HEAD has no `*` rule, the only pattern the verifier reads as matching every path, "
        "so it cannot show that a file no rule matches, such as a new workflow, needs code owner review",
    )


@pytest.mark.parametrize(
    "rules",
    [
        [],
        [{"type": "pull_request", "parameters": {"require_code_owner_review": False}}],
        [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}],
        [{"type": "pull_request"}],
        [{"type": "pull_request", "parameters": {"require_code_owner_review": "true"}}],
        [{"type": "required_status_checks", "parameters": {"require_code_owner_review": True}}],
        [{"type": "deletion"}, {"type": "non_fast_forward"}],
    ],
)
def test_no_approval_authenticates_unless_a_ruleset_requires_code_owner_review(tmp_path: Path, rules: list) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    github.rules = rules
    _approved(root, github)

    _refused(root, github, _NO_RULESET)
    assert "classic branch protection cannot be read with a workflow token" in (
        _verify(root, github)["basics/result"].reason or ""
    )


def test_a_code_owner_rule_on_a_later_page_of_rules_counts(tmp_path: Path) -> None:
    """Deliberate guard: the rules are read across pages, like every list."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    github.rules = [{"type": "deletion"}] * 100 + github.rules
    _approved(root, github)

    _authenticated(root, github)


_STALE = "Dismiss stale pull request approvals when new commits are pushed (dismiss_stale_reviews_on_push)"
_LAST_PUSH = "Require approval of the most recent reviewable push (require_last_push_approval)"
_CODE_OWNERS = "Require review from Code Owners (require_code_owner_review)"


@pytest.mark.parametrize(
    ("parameters", "missing", "consequences"),
    [
        (
            {"dismiss_stale_reviews_on_push": False},
            [_STALE],
            ["an approval still counts after pushes its reviewer never saw"],
        ),
        (
            {"require_last_push_approval": False},
            [_LAST_PUSH],
            ["a code owner can push to someone else's pull request and approve their own push"],
        ),
        ({"dismiss_stale_reviews_on_push": "true"}, [_STALE], []),
        ({"require_last_push_approval": None}, [_LAST_PUSH], []),
        (
            {"dismiss_stale_reviews_on_push": False, "require_last_push_approval": False},
            [f"{_STALE} or {_LAST_PUSH}"],
            ["pushes its reviewer never saw; and a code owner can push"],
        ),
    ],
)
def test_no_approval_authenticates_unless_stale_approvals_are_dismissed_and_the_last_push_is_approved(
    tmp_path: Path, parameters: dict, missing: list, consequences: list
) -> None:
    """Repro: without these settings, an owner's approval of a typo fix still
    counts after the author pushes a CODEOWNERS line naming themselves."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    github.rules[0]["parameters"].update(parameters)
    _approved(root, github)

    _refused(root, github, "no active ruleset on main has a pull request rule with ")
    for text in missing + consequences:
        _refused(root, github, text)


def test_the_review_settings_may_come_from_different_rulesets(tmp_path: Path) -> None:
    """Deliberate guard: GitHub enforces the strictest of every ruleset's rules, so they add up."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    github.rules[0]["parameters"].update(require_last_push_approval=False)
    github.rules.append(
        {
            "type": "pull_request",
            "ruleset_source_type": "Organization",
            "ruleset_source": "owner",
            "ruleset_id": 2,
            "parameters": {"require_code_owner_review": False, "require_last_push_approval": True},
        }
    )
    github.rulesets[2] = {**github.rulesets[1], "id": 2, "source_type": "Organization", "source": "owner"}
    _approved(root, github)

    _authenticated(root, github)


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
def test_a_ruleset_this_token_can_bypass_does_not_count(tmp_path: Path, ruleset: dict | None, reason: str) -> None:
    """A workflow whose token can bypass the ruleset can push to main unreviewed."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    if ruleset is None:
        del github.rulesets[1]
    else:
        github.rulesets[1].update(ruleset)
    _approved(root, github)

    _refused(
        root,
        github,
        f"no active ruleset on main that this verifier's token cannot bypass has a pull request rule with "
        f"{_CODE_OWNERS} or {_STALE} or {_LAST_PUSH}, so a pull request can merge without its code owners",
    )
    _refused(root, github, reason)


def test_a_pull_request_rule_that_names_no_ruleset_does_not_count(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    del github.rules[0]["ruleset_id"]
    _approved(root, github)

    _refused(root, github, "a pull request rule names no ruleset, so who can bypass it cannot be read")


def test_a_pull_request_rule_whose_ruleset_id_is_a_boolean_does_not_count(tmp_path: Path) -> None:
    """True == 1, so read as a number it would reuse ruleset 1's answer and count as held."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    held = github.rules[0]
    only_owners = {**held["parameters"], "dismiss_stale_reviews_on_push": False, "require_last_push_approval": False}
    github.rules = [{**held, "parameters": only_owners}, {**held, "ruleset_id": True}]
    _approved(root, github)

    _refused(root, github, "a pull request rule names no ruleset, so who can bypass it cannot be read")


def test_a_ruleset_the_token_cannot_bypass_counts_beside_one_it_can(tmp_path: Path) -> None:
    """Deliberate guard: a bypassable ruleset is left out, not held against the others."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    github.rules.insert(0, {**github.rules[0], "ruleset_id": 2})
    github.rulesets[2] = {**github.rulesets[1], "id": 2, "current_user_can_bypass": "always"}
    _approved(root, github)

    _authenticated(root, github)


# Only a build of the default branch's current head authenticates.


def test_a_build_of_an_older_commit_cannot_bring_back_a_withdrawn_approval(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    landed = _git(root, "rev-parse", "HEAD")
    (root / ".github" / "CODEOWNERS").write_text("* @owner\nblueprint/ @carol\n", encoding="utf-8")
    current = _commit(root, "Hand the blueprint to carol")
    _refused(root, github, f"no individual @user is a code owner of {_ARTICLE}")

    # Re-running the Pages run of the merge builds it again, with the CODEOWNERS that named alice.
    # It fails rather than publish a site where every approval reads self-approved.
    with pytest.raises(SupersededBuildError) as raised:
        _verify(root, github, trusted_ref=landed)
    assert str(raised.value) == (
        f"{landed} is {landed[:12]}, not {current[:12]}, the head of main on GitHub; approvals are authenticated "
        "only at the head of the default branch, so this build stops rather than render every approval self-approved"
    )


_UNNAMED_HEAD = "GitHub API GET of refs/heads/main did not name the commit it points to"


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (lambda found: None, "GitHub finds no branch main, so HEAD cannot be shown to be its head"),
        (lambda found: [found], _UNNAMED_HEAD),
        (lambda found: {**found, "ref": "refs/heads/main-old"}, _UNNAMED_HEAD),
        (lambda found: {**found, "object": {**found["object"], "type": "tag"}}, _UNNAMED_HEAD),
        (lambda found: {**found, "object": {"type": "commit"}}, _UNNAMED_HEAD),
    ],
)
def test_a_default_branch_head_github_does_not_name_stops_the_build(
    tmp_path: Path, edit: object, reason: str
) -> None:
    """Labelling every approval self-approved instead would let it downgrade the live site."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.heads["main"] = edit(github.get("/git/ref/heads/main"))  # type: ignore[operator]

    with pytest.raises(HeadCheckError) as raised:
        _verify(root, github)
    assert str(raised.value) == f"cannot tell whether HEAD is the head of the default branch on GitHub: {reason}"


@pytest.mark.parametrize("broken", ["base", "main"])
def test_the_gate_reads_codeowners_errors_at_its_base(tmp_path: Path, broken: str) -> None:
    """GitHub reads each commit's own CODEOWNERS, so the gate asks about its base, not main's head."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    base = _git(root, "rev-parse", "main")
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    head = _commit(root, "Approve the result")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    _git(root, "checkout", "--quiet", "main")
    _append(root, "Moved on.\n", "blueprint/README.md")
    moved = _commit(root, "Move main past the base")
    _git(root, "checkout", "--quiet", "approve")
    error = {"line": 2, "kind": "Unknown owner", "path": ".github/CODEOWNERS"}
    github.codeowners_errors_at[base if broken == "base" else moved] = [error]

    status = _verify(root, github, trusted_ref=base, pull_request=7)["basics/result"]
    if broken == "base":
        assert not status.authenticated
        assert f"GitHub reports 1 error(s) in CODEOWNERS at {base}" in (status.reason or "")
    else:
        assert status.authenticated, status.reason


def test_the_gate_reads_no_default_branch_head(tmp_path: Path) -> None:
    """Deliberate guard: the gate trusts its base commit, which the default branch may have moved past."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    base = _git(root, "rev-parse", "main")
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    head = _commit(root, "Approve the result")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    _git(root, "checkout", "--quiet", "main")
    _append(root, "Moved on.\n", "blueprint/README.md")
    _commit(root, "Move main past the base")
    _git(root, "checkout", "--quiet", "approve")

    status = _verify(root, github, trusted_ref=base, pull_request=7)["basics/result"]
    assert status.authenticated, status.reason
    assert "/git/ref/heads/main" not in [path for path, _ in github.calls]


@pytest.mark.parametrize(
    ("codeowners", "permissions", "uncovered"),
    [
        ("* @reader\nblueprint/ @alice\n", {"reader": "read"}, ": line 1: @reader cannot write"),
        ("* @triager\nblueprint/ @alice\n", {"triager": "triage"}, ": line 1: @triager cannot write"),
        ("* @gone\nblueprint/ @alice\n", {"gone": None}, ": line 1: @gone cannot write"),
        ("* owner@example.com\nblueprint/ @alice\n", {}, ": line 1: email owners cannot be verified"),
        ("*\nblueprint/ @alice\n", {}, ": line 1 names no owner"),
        ("* @owner\n.github/ \u2028@owner\nblueprint/ @alice\n", {}, "HEAD:.github/CODEOWNERS:2 contains U+2028"),
        (
            "* @owner\n!.github/CODEOWNERS @owner\nblueprint/ @alice\n",
            {},
            "HEAD:.github/CODEOWNERS:2: unsupported CODEOWNERS pattern '!.github/CODEOWNERS'",
        ),
        (
            "* @owner\n.github/[A-Z]* @owner\nblueprint/ @alice\n",
            {},
            "HEAD:.github/CODEOWNERS:2: unsupported CODEOWNERS pattern '.github/[A-Z]*'",
        ),
        ("* @owner\n.github/ @owner!\nblueprint/ @alice\n", {}, "unsupported owner '@owner!'"),
        ("* @owner\nblueprint/ @reader\nblueprint/roadmap/ @alice\n", {"reader": "read"}, ": line 2: @reader cannot"),
        ("* @owner\nblueprint/ @alice\n/blueprint/roadmap/*.html\n", {}, ": line 3 names no owner"),
        ("* @owner\nblueprint/ @alice\n/docs/ @reader\n", {"reader": "read"}, ": line 3: @reader cannot write"),
        (
            "* @owner\nblueprint/ @alice\n/docs/ @reader alice@example.com @org/docs\n",
            {"reader": "read"},
            ": line 3: @reader cannot write; @org/docs is not a team of owner; email owners cannot be verified",
        ),
    ],
)
def test_every_rule_from_the_last_catch_all_on_needs_a_code_owner_who_can_write(
    tmp_path: Path, codeowners: str, permissions: dict, uncovered: str
) -> None:
    """Every path is decided by the last `*` rule or a later one, tracked or not:
    an unowned /blueprint/roadmap/*.html rule would let a pull request publish
    HTML on the site without code owner review."""

    root = _project(tmp_path, codeowners)
    github = FakeGitHub(root)
    github.permissions.update(permissions)
    _approved(root, github)

    _refused(
        root,
        github,
        ".github/CODEOWNERS at HEAD leaves the paths of 1 rule(s) without a code owner GitHub enforces, so they can "
        "change without code owner review",
    )
    _refused(root, github, uncovered)


def test_owning_every_tracked_file_by_name_leaves_new_files_unowned(tmp_path: Path) -> None:
    """Repro: without a `*` rule, a pull request adding a workflow needs no code owner review."""

    root = _project(tmp_path, "/.github/CODEOWNERS @owner\n/blueprint/README.md @owner\n/blueprint/roadmap/ @alice\n")
    github = FakeGitHub(root)
    _approved(root, github)

    _refused(root, github, ".github/CODEOWNERS at HEAD has no `*` rule")
    _refused(root, github, "give every path an owner with a first line like `* @owner`")


@pytest.mark.parametrize("catch_all", ["**", "/**"])
def test_a_catch_all_other_than_a_star_is_refused_for_what_it_is(tmp_path: Path, catch_all: str) -> None:
    """Deliberate guard: only `*` is read as matching every path. The reason says so,
    rather than claim that a file no rule matches can be added unreviewed."""

    root = _project(tmp_path, f"{catch_all} @owner\nblueprint/ @alice\n")
    github = FakeGitHub(root)
    _approved(root, github)

    _refused(
        root,
        github,
        ".github/CODEOWNERS at HEAD has no `*` rule, the only pattern the verifier reads as matching every path, "
        "so it cannot show that a file no rule matches",
    )
    assert "can be added without code owner review" not in (_verify(root, github)["basics/result"].reason or "")


@pytest.mark.parametrize(
    "codeowners",
    ["/docs/ @reader\n* @owner\nblueprint/ @alice\n", "* @reader\n* @owner\nblueprint/ @alice\n"],
)
def test_rules_before_the_last_catch_all_decide_nothing(tmp_path: Path, codeowners: str) -> None:
    """Deliberate guard: the last `*` rule overrides every rule before it for every path."""

    root = _project(tmp_path, codeowners)
    github = FakeGitHub(root)
    github.permissions["reader"] = "read"
    _approved(root, github)

    _authenticated(root, github)


@pytest.mark.parametrize(
    ("codeowners", "line", "error"),
    [
        (_CODEOWNERS, 2, "Invalid pattern"),
        (_CODEOWNERS, 2, "Invalid owner"),
        # actions/checkout's CODEOWNERS is `* @actions/actions-runtime`, which GitHub reports this way.
        ("* @owner/actions-runtime\nblueprint/ @alice\n", 1, "Unknown owner"),
    ],
)
def test_any_error_github_reports_in_codeowners_authenticates_nothing(
    tmp_path: Path, codeowners: str, line: int, error: str
) -> None:
    """GitHub skips a line it cannot parse and ignores an owner it cannot use,
    such as a team without write access; the local parser cannot see either."""

    root = _project(tmp_path, codeowners)
    github = FakeGitHub(root)
    source = codeowners.split("\n")[line - 1]
    github.codeowners_errors = [
        {
            "line": line,
            "column": 1,
            "kind": error,
            "source": source,
            "suggestion": None,
            "message": f"{error} on line {line}:\n\n  {source}\n  ^",
            "path": ".github/CODEOWNERS",
        }
    ]
    _approved(root, github)

    _refused(
        root,
        github,
        f"GitHub reports 1 error(s) in CODEOWNERS at HEAD, so it does not enforce every line as written: "
        f".github/CODEOWNERS:{line} {error}",
    )


def test_errors_github_reports_are_named_ten_at_a_time(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    github.codeowners_errors = [{"line": line, "kind": "Unknown owner", "path": "CODEOWNERS"} for line in range(1, 13)]
    _approved(root, github)

    _refused(root, github, "GitHub reports 12 error(s) in CODEOWNERS at HEAD")
    _refused(root, github, "CODEOWNERS:10 Unknown owner; and 2 more")


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (None, "GitHub finds no CODEOWNERS file at HEAD, so it requires no code owner review"),
        ("not a list", "GitHub API GET /codeowners/errors did not return a list of errors"),
    ],
)
def test_codeowners_github_cannot_read_authenticate_nothing(tmp_path: Path, answer: object, reason: str) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    github.codeowners_errors = answer  # type: ignore[assignment]
    _approved(root, github)

    _refused(root, github, reason)


def test_a_team_of_another_organization_owns_nothing(tmp_path: Path) -> None:
    """Only the repository owner's teams can have access to its files."""

    root = _project(tmp_path, "* @org/maintainers\nblueprint/ @alice\n")
    github = FakeGitHub(root)
    _approved(root, github)

    _refused(root, github, ": line 1: @org/maintainers is not a team of owner")


@pytest.mark.parametrize(
    ("codeowners", "permissions"),
    [
        ("* @owner/maintainers\nblueprint/ @alice\n", {}),
        ("* @Owner/maintainers\nblueprint/ @alice\n", {}),
        ("* @org/maintainers @owner/maintainers\nblueprint/ @alice\n", {}),
        ("* @owner\nblueprint/ @alice\n", {"owner": "admin"}),
        ("* @owner\nblueprint/ @alice\n", {"owner": "maintain"}),
        ("* @reader @owner\nblueprint/ @alice\n", {"reader": "read"}),
        ("* owner@example.com @owner\nblueprint/ @alice\n", {}),
    ],
)
def test_a_team_or_one_owner_who_can_write_covers_a_file(tmp_path: Path, codeowners: str, permissions: dict) -> None:
    """Deliberate guard for the teams and permissions that do cover a file."""

    root = _project(tmp_path, codeowners)
    github = FakeGitHub(root)
    github.permissions.update(permissions)
    _approved(root, github)

    _authenticated(root, github)


@pytest.mark.parametrize("codeowners", ["* @owner/maintainers\nblueprint/ @alice\n", "* @Owner/maintainers\nblueprint/ @alice\n"])
def test_a_team_of_an_owner_whose_login_has_capitals_covers_a_file(tmp_path: Path, codeowners: str) -> None:
    """GitHub compares logins without case, and owners such as GoogleCloudPlatform are mixed case."""

    root = _project(tmp_path, codeowners)
    github = FakeGitHub(root)
    github.repository = {**FakeGitHub.repository, "owner": {"login": "Owner"}}
    _approved(root, github)

    _authenticated(root, github)


def test_a_repository_github_names_no_owner_of_authenticates_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    github.repository = {key: value for key, value in FakeGitHub.repository.items() if key != "owner"}
    _approved(root, github)

    _refused(root, github, "GitHub API GET of the repository did not name its owner")


def test_the_uncovered_rules_are_named_ten_at_a_time(tmp_path: Path) -> None:
    root = _project(tmp_path, _CODEOWNERS + "".join(f"/Lib{index:02d}.lean @reader{index:02d}\n" for index in range(12)))
    github = FakeGitHub(root)
    github.permissions.update({f"reader{index:02d}": "read" for index in range(12)})
    _approved(root, github)

    status = _verify(root, github)["basics/result"]
    assert status.label == "self-approved"
    assert "leaves the paths of 12 rule(s) without a code owner" in (status.reason or "")
    assert "line 12: @reader09 cannot write; and 2 more" in (status.reason or "")
    assert "@reader10" not in (status.reason or "")


def test_a_codeowners_file_github_would_not_load_owns_nothing(tmp_path: Path) -> None:
    rules = _GRAB[: -len("blueprint/roadmap/ @carol\n")]
    # Exactly 3 MB, which GitHub already does not load.
    root = _project(tmp_path, rules + "#" * (3_000_000 - len(rules.encode()) - 1) + "\n")
    assert (root / ".github" / "CODEOWNERS").stat().st_size == 3_000_000
    github = FakeGitHub(root)
    _approved(root, github)

    _refused(root, github, "GitHub does not load a CODEOWNERS file of 3 MB or more")


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("blueprint/roadmap/basics/result.md", True),
        ("blueprint/roadmap/README.md", True),
        ("blueprint/readbacks/af_0123456789abcdef01234567/Review.result.md", True),
        ("blueprint/README.md", False),
        ("blueprint/roadmap/basics/result.lean", False),
        ("blueprint/roadmap/basics/result.MD", False),
        ("blueprint/roadmap.md", False),
        ("blueprint/coverage/README.md", False),
        ("roadmap/basics/result.md", False),
        ("docs/blueprint/roadmap/result.md", False),
        ("other/roadmap/basics/result.md", False),
        (".github/CODEOWNERS", False),
    ],
)
def test_content_is_markdown_under_the_roadmap_or_the_read_back_cards(path: str, content: bool) -> None:
    from autoform_cli.approvals import _is_content

    assert _is_content(path, "blueprint") is content


def test_content_is_found_in_a_blueprint_at_the_repository_root() -> None:
    from autoform_cli.approvals import _is_content

    assert _is_content("roadmap/basics/result.md", "")
    assert _is_content("readbacks/af_x/Review.result.md", "")
    assert not _is_content("README.md", "")
    assert not _is_content("blueprint/roadmap/basics/result.md", "")


# D2: the approving pull request changes only articles and read-back cards.


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("blueprint/README.md", "Unrelated.\n"),
        ("Review.lean", "-- a proof\n"),
        ("mkdocs.yml", "site_name: x\n"),
        ("theme/main.html", "<p></p>\n"),
    ],
)
def test_an_approving_pull_request_that_changes_anything_else_authenticates_nothing(
    tmp_path: Path, path: str, text: str
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text, encoding="utf-8")
    _commit(root, "Approve the result")
    head = github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    _land(root, github, 7)

    _refused(
        root,
        github,
        f"#7 changes {path}, not only articles and read-back cards; record approvals in a pull request that "
        "changes only articles and read-back cards",
    )


def test_a_rename_into_the_roadmap_counts_by_its_previous_name(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.file_edits[7] = lambda entries: [
        *entries,
        {"filename": "blueprint/roadmap/basics/notes.md", "status": "renamed", "previous_filename": "lakefile.lean"},
    ]

    _refused(root, github, "#7 changes lakefile.lean, not only articles")


def test_an_approving_pull_request_may_change_other_articles_and_cards(tmp_path: Path) -> None:
    """Deliberate guard: content is every article and card, not only the approved one."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _append(root, "Another note.\n", "blueprint/roadmap/basics/other.md")
    card = root / "blueprint" / "readbacks" / "af_0123456789abcdef01234567" / "note.md"
    card.parent.mkdir(parents=True)
    card.write_text("not a real card\n", encoding="utf-8")
    head = _commit(root, "Approve the result")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    _land(root, github, 7)

    _authenticated(root, github)


def test_a_file_list_longer_than_github_lists_fails_closed(tmp_path: Path) -> None:
    """Deliberate guard: GitHub lists at most 3000 files, so a full list may be cut short."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.file_edits[7] = lambda entries: entries + [
        {"filename": f"blueprint/roadmap/basics/n{index}.md", "status": "added"}
        for index in range(3000 - len(entries))
    ]

    _refused(root, github, "#7 has 3000 files listed and GitHub lists at most 3000, so what it changes cannot be read")


def test_the_gate_refuses_a_pull_request_that_changes_anything_else(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    (root / ".github" / "CODEOWNERS").write_text(_GRAB, encoding="utf-8")
    head = _commit(root, "Approve the result")
    github.open_pull(7, "mallory")
    github.review(7, "alice", "APPROVED", head)

    status = _verify(root, github, trusted_ref="main", pull_request=7)["basics/result"]
    assert status.label == "self-approved"
    assert "#7 changes .github/CODEOWNERS, not only articles and read-back cards" in (status.reason or "")


@pytest.mark.parametrize("gate", [False, True])
def test_a_file_list_as_long_as_github_lists_fails_closed_however_many_pages_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: bool
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    head = _commit(root, "Approve the result")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    if not gate:
        _land(root, github, 7)
    github.file_edits[7] = lambda entries: entries + [
        {"filename": f"blueprint/roadmap/basics/n{index}.md", "status": "added"}
        for index in range(3000 - len(entries))
    ]
    monkeypatch.setattr(approvals, "_MAX_PAGES", 40)

    status = _verify(root, github, **({"trusted_ref": "main", "pull_request": 7} if gate else {}))["basics/result"]
    assert status.label == "self-approved"
    assert "#7 has 3000 files listed and GitHub lists at most 3000, so what it changes cannot be read" in (
        status.reason or ""
    )


def test_the_gate_still_needs_the_diff_to_record_the_hash(tmp_path: Path) -> None:
    """Deliberate guard: the gate skips the merge and the run, not the visible diff."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    head = _commit(root, "Approve the result")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    github.file_edits[7] = lambda entries: [{**entry, "patch": None} for entry in entries]

    status = _verify(root, github, trusted_ref="main", pull_request=7)["basics/result"]
    assert status.label == "self-approved"
    assert f"GitHub shows no complete diff of {_ARTICLE} in #7" in (status.reason or "")


# F2: the verify run belongs to the approving pull request.


def test_c3_a_run_for_a_pull_request_into_another_branch_does_not_count(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    head = github.open_pull(7, "mallory", ci=None)
    github.run(head)
    repository = {"id": 1, "full_name": "owner/project"}
    github.runs[-1]["pull_requests"] = [
        {"number": 7, "base": {"ref": "evil", "repo": repository}, "head": {"sha": head}}
    ]
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7, "squash")

    _refused(root, github, f"{_VERIFY} has no successful pull_request run on the head {head[:12]} of #7")


_FOREIGN = {"ref": "main", "repo": {"id": 2, "full_name": "mallory/project"}}


@pytest.mark.parametrize(
    "listed",
    [
        [{"number": 7, "base": {"ref": "main", "repo": {"id": 1}}}],
        [],
        [{"number": 7, "base": _FOREIGN}],
        [{"number": 1, "base": _FOREIGN}, {"number": 7, "base": {"ref": "main", "repo": {"id": 1}}}],
    ],
)
def test_a_run_that_lists_only_its_own_pull_request_counts(tmp_path: Path, listed: list) -> None:
    """Deliberate guard: an open pull request's run lists it, a merged one's lists none. Anyone can
    open a pull request in their fork from the branch, which GitHub lists too, but a run here
    belongs to a pull request into this repository, so that one is not the run's."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.runs[-1]["pull_requests"] = listed

    _authenticated(root, github)


@pytest.mark.parametrize(
    "change",
    [
        {"head_branch": "other"},
        {"head_repository": {"id": 2, "full_name": "mallory/project"}},
        {"head_repository": None},
        {"pull_requests": None},
        {"pull_requests": [{"number": 8, "base": {"ref": "main", "repo": {"id": 1}}}]},
        {"pull_requests": [{"number": 7, "base": {"ref": "main"}}]},
        {"pull_requests": [{"number": 7, "base": {"ref": "main", "repo": {"id": 1}}}, {"number": 8}]},
    ],
)
def test_a_run_from_elsewhere_does_not_count(tmp_path: Path, change: dict) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _approved(root, github)
    github.runs[-1].update(change)

    _refused(root, github, f"{_VERIFY} has no successful pull_request run on the head {head[:12]} of #7")


def test_a_branch_that_headed_another_pull_request_ties_no_run_to_this_one(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _approved(root, github)
    # A decoy pull request from the same branch, whose run is indistinguishable after both close.
    github.open_pull(8, "mallory", head=head, base_ref="evil", ref="approve-7", ci=None)

    _refused(
        root,
        github,
        "the branch 'approve-7' of #7 also headed other pull requests (#8), so a run on it cannot be tied to #7",
    )


def test_a_pull_request_that_changed_its_base_branch_proves_nothing_by_its_run(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.events[7] = [{"event": "labeled"}, {"event": "base_ref_changed"}]

    _refused(root, github, "#7 changed its base branch, so its verify run may have checked it against another branch")


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ({"id": 2, "full_name": "mallory/project"}, "#7 comes from 'mallory/project', not a branch of owner/project"),
        (None, "#7 comes from a deleted repository, not a branch of owner/project"),
    ],
)
def test_a_pull_request_from_a_fork_is_refused(tmp_path: Path, source: dict | None, reason: str) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.pulls[7]["head"]["repo"] = source

    _refused(root, github, reason)
    _refused(root, github, "record approvals from a branch of this repository")


@pytest.mark.parametrize("source", [{"id": 2, "full_name": "mallory/project"}, None])
def test_a_fork_is_refused_before_anything_about_it_is_read(tmp_path: Path, source: dict | None) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.pulls[7]["head"]["repo"] = source

    _refused(root, github, "not a branch of owner/project")
    requested = [path for path, _ in github.calls]
    assert not any(path.startswith(("/contents/", "/pulls/7/")) for path in requested), requested
    # Only the commit that recorded the hash is looked up, not the walk to the fork's base.
    assert [path for path in requested if path.startswith("/commits/")] == [
        f"/commits/{_git(root, 'rev-parse', 'HEAD')}/pulls"
    ], requested


@pytest.mark.parametrize(
    ("side", "reason"),
    [
        ("head", "#7 comes from 'mallory/project', not a branch of owner/project"),
        ("base", "#7 does not target owner/project"),
    ],
)
def test_the_gate_refuses_a_pull_request_between_repositories(tmp_path: Path, side: str, reason: str) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    head = _commit(root, "Approve the result")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    github.pulls[7][side]["repo"] = {"id": 2, "full_name": "mallory/project"}

    status = _verify(root, github, trusted_ref="main", pull_request=7)["basics/result"]
    assert status.label == "self-approved"
    assert reason in (status.reason or "")


def test_a_pull_request_into_another_repository_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.pulls[7]["base"]["repo"] = {"id": 2, "full_name": "mallory/project"}

    _refused(root, github, "which recorded this hash, came from no pull request merged into main")


@pytest.mark.parametrize("strategy", ["squash", "rebase"])
def test_a_pull_request_into_another_repository_beside_the_approving_one_is_left_out(
    tmp_path: Path, strategy: str
) -> None:
    """In a project that is a fork, GitHub also lists the upstream pull request that took its main,
    which may share a number with one of the project's own."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github, strategy=strategy)
    upstream = {"id": 2, "full_name": "upstream/project"}
    for number in (5, 7):
        github.pulls[-number] = {
            **github.pulls[7],
            "number": number,
            "head": {**github.pulls[7]["head"], "ref": "main", "label": "owner:main"},
            "base": {**github.pulls[7]["base"], "repo": upstream},
            "merged_at": "2026-01-03T00:00:00Z",
        }
    for commit in _git(root, "rev-list", "main").split():
        github.associate(-5, commit)
        github.associate(-7, commit)

    _authenticated(root, github)


@pytest.mark.parametrize("user", [None, {}, {"login": ""}, {"login": None}, {"login": "two words"}])
def test_a_pull_request_without_an_author_authenticates_nothing(tmp_path: Path, user: object) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _approved(root, github)
    pull = github.pulls[7]
    # GitHub still links the commits to bob, so only the missing author is in question.
    github.commit_users.update(
        dict.fromkeys(_git(root, "rev-list", f"{pull['base']['sha']}..{head}").split(), ("bob", "bob"))
    )
    pull["user"] = user

    _refused(root, github, "GitHub returned #7 without its author, so no reviewer can be shown not to be them")


def test_a_pull_request_an_app_opened_has_an_author(tmp_path: Path) -> None:
    """Deliberate guard: a bot login is an author, not a missing one."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.pulls[7]["user"] = {"login": "github-actions[bot]"}

    _authenticated(root, github)


# F5: write permission is read from GitHub, not inferred from the association.


@pytest.mark.parametrize("permission", ["read", "triage", "none", None])
def test_c5_an_organization_member_without_write_permission_does_not_count(
    tmp_path: Path, permission: str | None
) -> None:
    # owner also owns the articles, so the coverage precondition holds without alice.
    root = _project(tmp_path, "* @owner\nblueprint/roadmap/ @alice @owner\n")
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head, association="MEMBER")
    github.permissions["alice"] = permission

    _refused(
        root,
        github,
        f"@alice approved #7 but GitHub gives them {permission or 'no'} permission on the repository, not write",
    )


@pytest.mark.parametrize("association", ["MEMBER", "OWNER", "COLLABORATOR"])
@pytest.mark.parametrize("permission", ["write", "maintain", "admin"])
def test_a_writer_with_any_writing_association_authenticates(
    tmp_path: Path, association: str, permission: str
) -> None:
    """Deliberate guard: what the permission and association checks must still admit."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _pull_approving(root, github)
    github.review(7, "alice", "APPROVED", head, association=association)
    github.permissions["alice"] = permission

    _authenticated(root, github)


# F7: a reviewer who wrote any commit of the pull request, and lists cut short.


@pytest.mark.parametrize("users", [("alice", "bob"), ("bob", "Alice")])
def test_a_reviewer_who_wrote_a_commit_of_the_pull_request_does_not_count(
    tmp_path: Path, users: tuple[str, str]
) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    first = _commit(root, "Approve the result")
    _append(root, "\nA note.\n")
    head = _commit(root, "Add a note")
    github.open_pull(7, "bob")
    github.commit_users[first] = users
    github.review(7, "alice", "APPROVED", head)
    _land(root, github, 7)

    _refused(root, github, "@alice approved #7 but authored or committed one of its commits")


@pytest.mark.parametrize("users", [(None, "bob"), ("bob", None)])
def test_a_commit_github_links_to_no_account_refuses_the_approval(tmp_path: Path, users: tuple) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _approved(root, github)
    github.commit_users[head] = users

    _refused(root, github, "GitHub links to no account, so no reviewer can be shown not to have written it")


def test_a_pull_request_with_more_commits_than_github_lists_refuses_the_approval(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.commits = lambda number: [  # type: ignore[method-assign]
        {"sha": f"{index:040x}", "author": {"login": "bob"}, "committer": {"login": "bob"}} for index in range(250)
    ]

    _refused(root, github, "#7 has 250 commits listed and GitHub lists at most 250")


def test_a_missing_later_page_fails_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    head = _approved(root, github)
    for _ in range(99):
        github.review(7, "carol", "COMMENTED", head)
    # The approval leads the listing; the later page that could void it is missing.
    github.reviews[7].append({**github.reviews[7][0], "id": 101, "state": "CHANGES_REQUESTED",
                              "submitted_at": "2026-01-02T00:00:00Z"})
    listed = github.get

    def lost(path: str, query: dict | None = None) -> object | None:
        if path == "/pulls/7/reviews" and int((query or {}).get("page", 1)) == 2:
            return None
        return listed(path, query)

    github.get = lost  # type: ignore[method-assign]

    reason = "GitHub API GET /pulls/7/reviews found no page 2, so the list is incomplete"
    _refused(root, github, reason)
    # A list that changed between its pages is no answer: the approval is unchecked, so the run fails and is retried.
    unchecked = _verified(root, github).unchecked
    assert list(unchecked) == ["basics/result"] and reason in unchecked["basics/result"]


@pytest.mark.parametrize("listing", ["/pulls/7/commits", "/issues/7/events"])
def test_a_listing_github_finds_nothing_for_fails_closed(tmp_path: Path, listing: str) -> None:
    """A 404 for a list about a pull request GitHub just returned is no answer, not an empty
    list: no commits would clear every reviewer of writing one, and no events would show the
    base branch never changed."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.answers[listing] = None

    _refused(root, github, f"GitHub API GET {listing} found nothing (HTTP 404), so the list cannot be read")


# Deviation 1: a later reviewed pull request can re-approve a hash.


def _move_approval_line(root: Path) -> None:
    article = root / _ARTICLE
    lines = article.read_text(encoding="utf-8").splitlines(keepends=True)
    line = next(row for row in lines if row.startswith("review_approved:"))
    lines.remove(line)
    lines.insert(1, line)
    article.write_text("".join(lines), encoding="utf-8")


def test_a_later_reviewed_pull_request_re_approves_a_hash_first_pushed_unreviewed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approve(root, "result", _HASH)
    pushed = _commit(root, "Paste the hash directly")
    _refused(root, github, f"commit {pushed[:12]}, which recorded this hash, came from no pull request merged")

    _branch(root, "reapprove")
    _move_approval_line(root)
    head = _commit(root, "Re-approve the result")
    github.open_pull(8, "bob")
    _land(root, github, 8)
    status = _verify(root, github)["basics/result"]
    assert status.label == "self-approved"
    landed = _git(root, "rev-parse", "HEAD")
    # Every candidate's reason is kept, newest first.
    assert (status.reason or "").startswith(f"{landed[:12]}: #8 has no review; {pushed[:12]}: commit {pushed[:12]}")

    github.review(8, "alice", "APPROVED", head)
    status = _verify(root, github)["basics/result"]
    assert status.attestation is not None, status.reason
    assert status.attestation.reviewer == "alice"
    assert status.attestation.reference == "https://github.com/owner/project/pull/8#pullrequestreview-1"


def test_a_budget_spent_on_an_older_candidate_keeps_the_newer_ones_reason(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _pull_approving(root, github)
    landed = _git(root, "rev-parse", "HEAD")
    _move_approval_line(root)
    pushed = _commit(root, "Re-approve the result directly")
    _verify(root, github)
    # Everything up to the older candidate's first request.
    budget = [path for path, _ in github.calls].index(f"/commits/{landed}/pulls")
    github.calls.clear()

    github.hourly = (1000, budget + 50)
    status = _verify(root, github)["basics/result"]

    assert status.label == "self-approved"
    reason = status.reason or ""
    assert reason.startswith(f"{pushed[:12]}: commit {pushed[:12]}, which recorded this hash, came from no "), reason
    assert f"; not checked: the budget of {budget} GitHub API requests was spent" in reason
    assert f"; {budget + 50} of the hour's 1000 were left when this run began, and it keeps 50" in reason
    # The older candidate may authenticate it once a run has the budget, so a later run checks again.
    assert _verified(root, github).unchecked == {"basics/result": reason}
    # With the budget for both, each candidate is refused, which a later run would only repeat.
    github.hourly = (1000, 1000)
    assert _verified(root, github).unchecked == {}


def test_the_newest_authenticated_re_approval_is_the_one_shown(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    _authenticated(root, github)

    _branch(root, "reapprove")
    _move_approval_line(root)
    head = _commit(root, "Re-approve the result")
    github.open_pull(8, "dave")
    github.review(8, "Alice", "APPROVED", head)
    _land(root, github, 8)

    status = _verify(root, github)["basics/result"]
    assert status.attestation is not None, status.reason
    assert status.attestation.reference.endswith("/pull/8#pullrequestreview-1")


def test_a_failed_re_approval_leaves_the_reviewed_one_standing(tmp_path: Path) -> None:
    """Deliberate guard: the newest-first walk falls back to the older reviewed commit."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    _move_approval_line(root)
    moved = _commit(root, "Move the approval line on main")

    status = _verify(root, github)["basics/result"]
    assert status.attestation is not None, status.reason
    assert status.attestation.reference.endswith("/pull/7#pullrequestreview-1")
    assert _git(root, "rev-parse", "HEAD") == moved


def test_a_later_edit_that_records_no_approval_costs_no_requests(tmp_path: Path) -> None:
    """Deliberate guard on the request budget: only a commit whose own diff adds
    the approval line is tried, though GitHub's diff would refuse the others."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    _branch(root, "edit")
    _append(root, "More truth.\n")
    _commit(root, "Edit the result")
    github.open_pull(8, "dave")
    edited = _land(root, github, 8)

    _authenticated(root, github)
    assert not any(edited in path or path.startswith(("/pulls/8/", "/issues/8/")) for path, _ in github.calls)


def test_the_walk_reads_the_article_path_literally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliberate guard against a false refusal: read as a glob, ``re[s]ult.md``
    would also match result.md, whose later edit would use up the walk's cap."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    chapter = root / "blueprint" / "roadmap" / "basics"
    text = (chapter / "result.md").read_text(encoding="utf-8")
    (chapter / "re[s]ult.md").write_text(
        text.replace("af_0123456789abcdef01234567", "af_" + "c" * 24).replace("Review.result", "Review.glob"),
        encoding="utf-8",
    )
    _commit(root, "Add an article whose name is a glob")
    _branch(root, "approve-7")
    _approve(root, "re[s]ult", _HASH)
    _commit(root, "Approve it")
    head = github.open_pull(7, "bob")
    _land(root, github, 7)
    github.review(7, "alice", "APPROVED", head)
    _branch(root, "edit")
    _append(root, "More truth.\n")
    _commit(root, "Edit result")
    github.open_pull(8, "bob")
    _land(root, github, 8)
    monkeypatch.setattr(approvals, "_MAX_HISTORY", 1)

    status = _verify(root, github)["basics/re[s]ult"]
    assert status.attestation is not None, status.reason
    assert status.attestation.reviewer == "alice"


def test_c4_a_replayed_approval_needs_someone_who_can_bypass_the_ruleset(tmp_path: Path) -> None:
    """Repro C4: a direct push fast-forwards main to a merge whose first parent
    is the old approving commit. With the ruleset that pushing needs a bypass
    actor, whom the verifier trusts; without it nothing authenticates."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve-7")
    _approve(root, "result", _HASH)
    approved = _commit(root, "Approve the result")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED")
    _land(root, github, 7, "merge")
    _branch(root, "revoke")
    _approve(root, "result", None)
    _commit(root, "Withdraw the approval")
    github.open_pull(8, "alice")
    _land(root, github, 8, "squash")
    _git(root, "checkout", "--quiet", "-b", "replay", approved)
    _git(root, "merge", "--no-ff", "--no-commit", "-s", "ours", "main")
    _git(root, "commit", "--quiet", "--no-verify", "-m", "Merge main")
    _git(root, "checkout", "--quiet", "main")
    _git(root, "merge", "--quiet", "--ff-only", "replay")
    github.rules = []

    _refused(root, github, _NO_RULESET)
