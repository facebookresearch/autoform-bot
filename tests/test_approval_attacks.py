"""Forged approvals from the adversarial review, each refused with its reason,
beside the legitimate merges that must still authenticate."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli import approvals
from autoform_cli.approvals import ApprovalError, code_owners, parse_codeowners
from tests.test_approvals import (
    _ARTICLE,
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
        f"no individual @user is a code owner of {_ARTICLE} both at {parent[:12]} (before {landed[:12]}) and at HEAD",
    )


@pytest.mark.parametrize("strategy", ["rebase", "merge", "squash"])
def test_a3_a_pull_request_cannot_name_its_own_code_owner_one_commit_earlier(tmp_path: Path, strategy: str) -> None:
    """Rebased, the commit recording the hash has the CODEOWNERS change as its
    parent, so carol owned the article just before it; only that the pull
    request changed CODEOWNERS refuses it. The merge and squash cases are
    deliberate guards: the owners before the landing commit already refuse them."""

    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "grab")
    (root / ".github" / "CODEOWNERS").write_text(_GRAB, encoding="utf-8")
    _commit(root, "Tidy CODEOWNERS")
    _approve(root, "result", _HASH)
    _commit(root, "Approve the result")
    github.open_pull(4, "mallory")
    github.review(4, "carol", "APPROVED")
    landed = _land(root, github, 4, strategy)

    if strategy == "rebase":
        _refused(root, github, "#4 changes .github/CODEOWNERS, not only articles and read-back cards; record approvals")
    else:
        _refused(root, github, f"code owner of {_ARTICLE} both at {_git(root, 'rev-parse', landed + '^')[:12]}")


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
    rules = f"blueprint/ @alice\n# reviewers, see docs{separator}blueprint/ @mallory\n"
    root = _project(tmp_path, rules)
    github = FakeGitHub(root)
    _pull_approving(root, github, author="bob")
    github.review(7, "mallory", "APPROVED")

    _refused(root, github, f".github/CODEOWNERS:2 contains U+{ord(separator):04X}")
    # The line could hold any rule, so it leaves every path undecided.
    with pytest.raises(ApprovalError, match="cannot be decided"):
        code_owners(parse_codeowners(f"docs/ @a\n/src/ @b{separator}@c\n"), "docs/x.md")


# B1b: a no-break space is not a token separator for GitHub.
def test_b1b_a_no_break_space_does_not_split_owner_tokens(tmp_path: Path) -> None:
    root = _project(tmp_path, "blueprint/ @alice\nblueprint/\u00a0@mallory\n")
    github = FakeGitHub(root)
    _pull_approving(root, github)
    github.review(7, "mallory", "APPROVED")

    _refused(root, github, ".github/CODEOWNERS:2 contains U+00A0")


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


@pytest.mark.parametrize("strategy", ["merge", "squash"])
def test_c2_codeowners_that_own_no_codeowners_file_authenticate_nothing(tmp_path: Path, strategy: str) -> None:
    root = _project(tmp_path, "blueprint/ @alice\n")
    github = FakeGitHub(root)
    _owners_grant(root, github, strategy)

    _refused(
        root,
        github,
        ".github/CODEOWNERS at HEAD gives 1 tracked file(s) other than articles and read-back cards no code "
        "owner with write access, so they can change without code owner review: .github/CODEOWNERS (no rule)",
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


@pytest.mark.parametrize(
    ("codeowners", "permissions", "uncovered"),
    [
        ("blueprint/ @alice\n", {}, ".github/CODEOWNERS (no rule)"),
        ("* @reader\nblueprint/ @alice\n", {"reader": "read"}, ".github/CODEOWNERS (line 1: @reader cannot write)"),
        ("* @triager\nblueprint/ @alice\n", {"triager": "triage"}, "(line 1: @triager cannot write)"),
        ("* @gone\nblueprint/ @alice\n", {"gone": None}, "(line 1: @gone cannot write)"),
        ("* owner@example.com\nblueprint/ @alice\n", {}, "(line 1: email owners cannot be verified)"),
        ("*\nblueprint/ @alice\n", {}, ".github/CODEOWNERS (line 1 names no owner)"),
        ("* @owner\n.github/ \u2028@owner\nblueprint/ @alice\n", {}, "contains U+2028"),
        ("* @owner\n!.github/CODEOWNERS @owner\nblueprint/ @alice\n", {}, ".github/CODEOWNERS ("),
        ("* @owner\n.github/[A-Z]* @owner\nblueprint/ @alice\n", {}, ".github/CODEOWNERS ("),
        ("* @owner\n.github/ @owner!\nblueprint/ @alice\n", {}, "unsupported owner '@owner!'"),
        (
            "* @owner\nblueprint/ @reader\nblueprint/roadmap/ @alice\n",
            {"reader": "read"},
            "blueprint/README.md (line 2: @reader cannot write)",
        ),
    ],
)
def test_every_file_but_articles_and_cards_needs_a_code_owner_who_can_write(
    tmp_path: Path, codeowners: str, permissions: dict, uncovered: str
) -> None:
    root = _project(tmp_path, codeowners)
    github = FakeGitHub(root)
    github.permissions.update(permissions)
    _approved(root, github)

    _refused(root, github, "no code owner with write access, so they can change without code owner review: ")
    _refused(root, github, uncovered)


@pytest.mark.parametrize(
    ("codeowners", "permissions"),
    [
        ("* @org/maintainers\nblueprint/ @alice\n", {}),
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


def test_the_uncovered_files_are_named_ten_at_a_time(tmp_path: Path) -> None:
    root = _project(tmp_path, "blueprint/ @alice\n")
    for index in range(12):
        (root / f"Lib{index:02d}.lean").write_text("-- empty\n", encoding="utf-8")
    _commit(root, "Add Lean sources")
    github = FakeGitHub(root)
    _approved(root, github)

    status = _verify(root, github)["basics/result"]
    assert status.label == "self-approved"
    assert "gives 13 tracked file(s) other than articles" in (status.reason or "")
    assert "Lib08.lean (no rule), and 3 more" in (status.reason or "")
    assert "Lib09.lean" not in (status.reason or "")


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

    _refused(root, github, "/pulls/7/files has more than 3000 entries")


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


def test_the_gate_refuses_a_file_list_shorter_than_github_counts(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "approve")
    _approve(root, "result", _HASH)
    head = _commit(root, "Approve the result")
    github.open_pull(7, "bob")
    github.review(7, "alice", "APPROVED", head)
    github.pulls[7]["changed_files"] = 2

    status = _verify(root, github, trusted_ref="main", pull_request=7)["basics/result"]
    assert status.label == "self-approved"
    assert "GitHub lists 1 of the 2 files #7 changes" in (status.reason or "")
    github.pulls[7]["changed_files"] = 1
    status = _verify(root, github, trusted_ref="main", pull_request=7)["basics/result"]
    assert status.authenticated, status.reason


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


@pytest.mark.parametrize(
    "listed",
    [
        [{"number": 7, "base": {"ref": "main", "repo": {"id": 1}}}],
        [],
    ],
)
def test_a_run_that_lists_only_its_own_pull_request_counts(tmp_path: Path, listed: list) -> None:
    """Deliberate guard: an open pull request's run lists it, a merged one's lists none."""

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
        {"pull_requests": [{"number": 7, "base": {"ref": "main", "repo": {"id": 2}}}]},
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


def test_a_pull_request_into_another_repository_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _approved(root, github)
    github.pulls[7]["base"]["repo"] = {"id": 2, "full_name": "mallory/project"}

    _refused(root, github, "#7 does not target owner/project")


# F5: write permission is read from GitHub, not inferred from the association.


@pytest.mark.parametrize("permission", ["read", "triage", "none", None])
def test_c5_an_organization_member_without_write_permission_does_not_count(
    tmp_path: Path, permission: str | None
) -> None:
    # alice owns only articles, so the coverage precondition holds without her.
    root = _project(tmp_path, "* @owner\nblueprint/roadmap/ @alice\n")
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

    _refused(root, github, "GitHub API GET /pulls/7/reviews found no page 2, so the list is incomplete")


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
