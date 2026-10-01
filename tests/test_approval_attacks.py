"""Forged approvals from the adversarial review, each refused with its reason,
beside the legitimate merges that must still authenticate."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli.approvals import ApprovalError, code_owners, parse_codeowners
from tests.test_approvals import (
    _ARTICLE,
    _HASH,
    _OTHER_HASH,
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
    _append(root, "Unrelated.\n", "blueprint/README.md")
    _commit(root, "Unrelated change")
    _git(root, "merge", "--quiet", "--no-commit", "main")
    _git(root, "checkout", approved, "--", _ARTICLE)
    _commit(root, "Merge main into replay")
    github.open_pull(9, "mallory")
    _land(root, github, 9, "merge")

    _refused(root, github, "#9 has no review")


# A3: a pull request that edits CODEOWNERS labels itself approved once merged.
def test_a3_a_pull_request_cannot_name_its_own_code_owner(tmp_path: Path) -> None:
    root = _project(tmp_path)
    github = FakeGitHub(root)
    _branch(root, "grab")
    (root / ".github" / "CODEOWNERS").write_text("blueprint/ @alice\nblueprint/roadmap/ @carol\n", encoding="utf-8")
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


# A4: a review by an account GitHub shows without write access.
@pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", None])
def test_a4_a_reviewer_without_write_access_does_not_count(tmp_path: Path, association: str | None) -> None:
    root = _project(tmp_path, "blueprint/ @alice-old\n")
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

    _refused(root, github, f"#7 changes {_VERIFY}, so its own run of it is no evidence")


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

    _refused(root, github, f"#7 changes {_VERIFY}, so its own run of it is no evidence")


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
    root = _project(tmp_path, "docs/ @docs_acme\nblueprint/ @octocat_acme\n")
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
    assert github.calls == []


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
