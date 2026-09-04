from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from autoform_cli import __main__ as cli
from autoform_cli.__main__ import main
from autoform_cli.claims import (
    CLAIM_REF_PREFIX,
    CLAIM_SCHEMA,
    LEGACY_BLOCK_SCHEMA,
    LEGACY_CLAIM_SCHEMA,
    ClaimBoard,
    MalformedLeaseError,
    author_claim_key,
    resource_claim_key,
    workspace_author_claim_key,
)
from autoform_cli.workspace_mutation import create_blueprint_project, initialize_workspace


def _bare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "claims.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True)
    return repo


def _plant_message(repo: Path, key: str, message: str) -> None:
    tree = subprocess.run(
        ["git", "mktree"], cwd=repo, input="", capture_output=True, text=True, check=True
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "commit-tree", tree, "-m", message],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    ).stdout.strip()
    subprocess.run(["git", "update-ref", CLAIM_REF_PREFIX + key, commit], cwd=repo, check=True)


def _article(path: Path, title: str, article_id: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = f"article_id: {article_id}\n" if article_id else ""
    path.write_text(f"---\n{metadata}---\n\n# {title}\n", encoding="utf-8")


def _blueprint(tmp_path: Path, *, article_id: str | None = "af_0123456789abcdef01234567") -> Path:
    blueprint = tmp_path / "blueprint"
    _article(blueprint / "roadmap/chapter/README.md", "Chapter", None)
    _article(blueprint / "roadmap/chapter/main-result.md", "Main result", article_id)
    return blueprint


def _args(repo: Path, scratch: Path, blueprint: Path, *command: str) -> list[str]:
    args = [
        "claim",
        *command,
        "--repo",
        str(repo),
        "--worker-id",
        "worker-a",
        "--session-id",
        "test-session",
        "--scratch",
        str(scratch),
    ]
    if command[0] in {"acquire", "renew", "release"}:
        args.extend(["--blueprint", str(blueprint)])
    return args


def test_claim_cli_acquire_renew_list_release_round_trip(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 1_000.0)
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"

    assert main(_args(repo, scratch, blueprint, "acquire", node_id, "--ttl", "600")) == 0
    assert "acquired chapter/main-result" in capsys.readouterr().out
    assert main(_args(repo, scratch, blueprint, "renew", node_id, "--ttl", "600")) == 0
    assert "renewed chapter/main-result" in capsys.readouterr().out
    assert main(_args(repo, scratch, blueprint, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    by_key = {lease["_key"]: lease for lease in leases}
    durable_key = author_claim_key("af_0123456789abcdef01234567")
    legacy_key = author_claim_key(node_id)
    assert by_key[durable_key]["schema"] == CLAIM_SCHEMA
    assert by_key[durable_key]["owner"] == "worker-a"
    assert by_key[legacy_key]["schema"] == LEGACY_BLOCK_SCHEMA
    assert main(_args(repo, scratch, blueprint, "release", node_id)) == 0
    assert "released chapter/main-result" in capsys.readouterr().out


def test_claim_cli_refuses_live_peer_and_list_needs_no_session_identity(
    tmp_path: Path, capsys
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert main(_args(repo, first, blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()

    peer = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--repo",
        str(repo),
        "--worker-id",
        "worker-b",
        "--session-id",
        "peer-session",
        "--scratch",
        str(second),
        "--blueprint",
        str(blueprint),
    ]
    assert main(peer) == 1
    assert "ownership is held or unverifiable" in capsys.readouterr().out

    assert main(["claim", "list", "--repo", str(repo), "--scratch", str(second)]) == 0
    assert json.loads(capsys.readouterr().out)


def test_workspace_projects_with_the_same_article_id_claim_independently(
    tmp_path: Path, capsys
) -> None:
    repo = _bare_repo(tmp_path)
    root = tmp_path / "repository"
    root.mkdir()
    initialize_workspace(root, blueprint_root="Plans")
    create_blueprint_project(root, project_id="one", title="One", path="One")
    create_blueprint_project(root, project_id="two", title="Two", path="Two")
    article_id = "af_0123456789abcdef01234567"
    for project in ("One", "Two"):
        _article(root / f"Plans/{project}/roadmap/result.md", "Result", article_id)

    def command(operation: str, project: str, worker: str, scratch: str) -> list[str]:
        args = [
            "claim",
            operation,
            "result",
            "--blueprint",
            str(root),
            "--project",
            project,
            "--repo",
            str(repo),
            "--worker-id",
            worker,
            "--session-id",
            worker,
            "--scratch",
            str(tmp_path / scratch),
        ]
        if operation in {"acquire", "renew"}:
            args.extend(["--ttl", "600"])
        return args

    assert main(command("acquire", "one", "worker-one", "scratch-one")) == 0
    capsys.readouterr()
    assert main(command("acquire", "two", "worker-two", "scratch-two")) == 0
    capsys.readouterr()

    inspector = ClaimBoard(repo, "inspector", tmp_path / "inspection")
    keys = {lease["_key"] for lease in inspector.list()}
    assert workspace_author_claim_key("one", article_id) in keys
    assert workspace_author_claim_key("two", article_id) in keys
    assert main(command("renew", "two", "worker-one", "scratch-one")) == 1
    assert "ownership is held" in capsys.readouterr().out
    assert main(command("release", "two", "worker-one", "scratch-one")) == 1
    assert "ownership is held" in capsys.readouterr().out

    assert main(command("release", "one", "worker-one", "scratch-one")) == 0
    capsys.readouterr()
    assert main(command("release", "two", "worker-two", "scratch-two")) == 0


def test_claim_cli_transport_failure_is_nonzero(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing" / "claims.git"
    blueprint = _blueprint(tmp_path)
    assert main(_args(missing, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "error:" in capsys.readouterr().out


def test_claim_cli_passes_explicit_object_format(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "claims-sha256.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "--object-format=sha256", str(repo)],
        check=True,
    )
    blueprint = _blueprint(tmp_path)
    args = _args(repo, tmp_path / "scratch", blueprint, "list")
    args.extend(["--object-format", "sha1"])

    assert main(args) == 1
    assert "does not match expected" in capsys.readouterr().out


def test_claim_cli_refuses_malformed_remote_lease(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"
    _plant_message(repo, author_claim_key(article_id), "not json")

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    assert "invalid lease JSON" in capsys.readouterr().out


def test_nonexistent_article_creates_no_claim_ref(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "missing")) == 1
    assert "does not exist" in capsys.readouterr().out
    assert subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""


def test_article_without_durable_id_is_actionable_and_creates_no_ref(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path, article_id=None)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")) == 1
    output = capsys.readouterr().out
    assert "has no durable article_id" in output
    assert "autoform migrate article-ids" in output
    assert subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""


def test_article_rename_with_unchanged_id_preserves_claim_key(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, scratch, blueprint, "acquire", "chapter/main-result")) == 0
    capsys.readouterr()
    old_path = blueprint / "roadmap/chapter/main-result.md"
    new_path = blueprint / "roadmap/chapter/renamed-result.md"
    old_path.rename(new_path)

    assert main(_args(repo, scratch, blueprint, "renew", "chapter/renamed-result")) == 0
    capsys.readouterr()
    assert main(_args(repo, scratch, blueprint, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    assert {lease["_key"] for lease in leases} == {
        author_claim_key("af_0123456789abcdef01234567"),
        author_claim_key("chapter/main-result"),
        author_claim_key("chapter/renamed-result"),
    }


def test_article_target_rejects_path_and_article_id_ambiguity(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    ambiguous = "af_aaaaaaaaaaaaaaaaaaaaaaaa"
    _article(blueprint / f"roadmap/{ambiguous}.md", "Path match", "af_bbbbbbbbbbbbbbbbbbbbbbbb")
    _article(blueprint / "roadmap/id-match.md", "ID match", ambiguous)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", ambiguous)) == 1
    assert "is ambiguous" in capsys.readouterr().out


def test_legacy_path_cannot_fence_another_articles_durable_key(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    second_id = "af_bbbbbbbbbbbbbbbbbbbbbbbb"
    _article(
        blueprint / "roadmap/af_0123456789abcdef01234567.md",
        "Colliding path",
        second_id,
    )

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", second_id)) == 1
    assert "collides with a durable canonical claim key" in capsys.readouterr().out
    assert subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""


def test_explicit_resource_uses_a_distinct_namespace_and_round_trips(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    scratch = tmp_path / "scratch"

    assert main(_args(repo, scratch, blueprint, "acquire", "--resource", "lake-build")) == 0
    capsys.readouterr()
    assert main(_args(repo, scratch, blueprint, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    by_key = {lease["_key"]: lease for lease in leases}
    assert by_key[resource_claim_key("lake-build")]["schema"] == CLAIM_SCHEMA
    assert by_key[author_claim_key("lake-build")]["schema"] == LEGACY_BLOCK_SCHEMA
    assert main(_args(repo, scratch, blueprint, "release", "--resource", "lake-build")) == 0


def test_resource_name_cannot_impersonate_a_durable_article_id(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(
        _args(
            repo,
            tmp_path / "scratch",
            blueprint,
            "acquire",
            "--resource",
            "af_0123456789abcdef01234567",
        )
    ) == 1
    assert "reserved article_id format" in capsys.readouterr().out


def test_positional_lake_build_is_resolved_as_an_article_not_a_resource(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_aaaaaaaaaaaaaaaaaaaaaaaa"
    _article(blueprint / "roadmap/lake-build.md", "Lake build article", article_id)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "lake-build")) == 0
    assert author_claim_key(article_id) in capsys.readouterr().out


def test_positional_lake_build_without_an_article_requires_explicit_resource(
    tmp_path: Path, capsys
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", "lake-build")) == 1
    assert "use --resource lake-build" in capsys.readouterr().out


def test_article_and_resource_targets_are_mutually_exclusive(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)

    assert main(
        _args(
            repo,
            tmp_path / "scratch",
            blueprint,
            "acquire",
            "chapter/main-result",
            "--resource",
            "lake-build",
        )
    ) == 1
    assert "mutually exclusive" in capsys.readouterr().out


def test_live_legacy_path_claim_blocks_new_article_key(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "renewed_at": 100.0,
        "expires_at": 200.0,
        "resource": author_claim_key(node_id),
    }
    _plant_message(repo, author_claim_key(node_id), json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 150.0)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 1
    assert "live legacy v1 claim" in capsys.readouterr().out


def test_live_legacy_resource_key_blocks_new_resource_namespace(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    legacy_key = author_claim_key("lake-build")
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": legacy_key,
    }
    _plant_message(repo, legacy_key, json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 150.0)

    assert main(
        _args(repo, tmp_path / "scratch", blueprint, "acquire", "--resource", "lake-build")
    ) == 1
    assert "live legacy v1 claim" in capsys.readouterr().out
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert refs == [CLAIM_REF_PREFIX + legacy_key]


def test_renamed_live_legacy_path_blocks_durable_article_claim(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    old_id = "chapter/main-result"
    new_id = "chapter/renamed-result"
    (blueprint / "roadmap/chapter/main-result.md").rename(
        blueprint / "roadmap/chapter/renamed-result.md"
    )
    legacy_key = author_claim_key(old_id)
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": legacy_key,
    }
    _plant_message(repo, legacy_key, json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 150.0)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", new_id)) == 1
    assert "live legacy v1 claim" in capsys.readouterr().out

    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 500.0)
    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", new_id)) == 0
    capsys.readouterr()
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert board.read(legacy_key)["schema"] == LEGACY_BLOCK_SCHEMA


def test_d9_client_cannot_acquire_path_after_v2_owns_durable_id(
    tmp_path: Path, capsys
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    path_id = "chapter/main-result"
    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", path_id)) == 0
    capsys.readouterr()

    class D9Client(ClaimBoard):
        @staticmethod
        def _lease_is_valid(lease: dict[str, object], key: str | None = None) -> bool:
            return bool(
                lease.get("schema") == LEGACY_CLAIM_SCHEMA
                and ClaimBoard._lease_is_valid(lease, key)
            )

    old_client = D9Client(repo, "worker-a", tmp_path / "old-client")
    with pytest.raises(MalformedLeaseError, match="invalid lease schema"):
        old_client.acquire(author_claim_key(path_id), ttl=600)


def test_expired_legacy_path_claim_does_not_block_new_article_key(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": author_claim_key(node_id),
    }
    _plant_message(repo, author_claim_key(node_id), json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 500.0)

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 0
    capsys.readouterr()
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert CLAIM_REF_PREFIX + author_claim_key(node_id) in refs
    assert CLAIM_REF_PREFIX + author_claim_key("af_0123456789abcdef01234567") in refs


def test_expired_v1_at_durable_key_is_upgraded_instead_of_permanently_blocked(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    article_id = "af_0123456789abcdef01234567"
    canonical_key = author_claim_key(article_id)
    lease = {
        "schema": LEGACY_CLAIM_SCHEMA,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "expires_at": 200.0,
        "resource": canonical_key,
    }
    _plant_message(repo, canonical_key, json.dumps(lease))
    monkeypatch.setattr("autoform_cli.claims.time.time", lambda: 500.0)

    assert main(
        _args(repo, tmp_path / "scratch", blueprint, "acquire", "chapter/main-result")
    ) == 0
    capsys.readouterr()
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert board.read(canonical_key)["schema"] == CLAIM_SCHEMA


def test_malformed_legacy_path_claim_blocks_new_article_key(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    node_id = "chapter/main-result"
    _plant_message(repo, author_claim_key(node_id), "not json")

    assert main(_args(repo, tmp_path / "scratch", blueprint, "acquire", node_id)) == 1
    assert "invalid lease JSON" in capsys.readouterr().out
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert refs == [CLAIM_REF_PREFIX + author_claim_key(node_id)]


def test_cli_session_environment_is_stable_across_worker_label_changes(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    scratch = tmp_path / "scratch"
    monkeypatch.setenv("AUTOFORM_CLAIM_SESSION_ID", "worktree-session")
    monkeypatch.setenv("AUTOFORM_WORKER_ID", "worker-a")
    acquire = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--repo",
        str(repo),
        "--scratch",
        str(scratch),
        "--blueprint",
        str(blueprint),
    ]
    assert main(acquire) == 0
    capsys.readouterr()

    monkeypatch.setenv("AUTOFORM_WORKER_ID", "worker-b")
    renew = acquire.copy()
    renew[1] = "renew"
    assert main(renew) == 0
    assert "renewed" in capsys.readouterr().out


def test_cli_derives_a_stable_session_from_the_target_worktree(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    blueprint = _blueprint(project)
    scratch = tmp_path / "scratch"
    monkeypatch.delenv("AUTOFORM_CLAIM_SESSION_ID", raising=False)
    args = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--repo",
        str(repo),
        "--worker-id",
        "worker-a",
        "--scratch",
        str(scratch),
        "--blueprint",
        str(blueprint),
    ]

    assert main(args) == 0
    capsys.readouterr()
    args[1] = "renew"
    assert main(args) == 0
    assert "renewed" in capsys.readouterr().out


def test_existing_worktree_claim_token_is_read_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    token_path = git_dir / "autoform-claim-session"
    token_path.write_text("a" * 64 + "\n")
    real_open = cli.os.open

    def reject_token_writes(path, flags, *args):
        if Path(path) == token_path and flags & (os.O_WRONLY | os.O_RDWR):
            raise AssertionError("an existing worktree token must not be rewritten")
        return real_open(path, flags, *args)

    monkeypatch.setattr(cli.os, "open", reject_token_writes)

    assert cli._worktree_claim_token(git_dir) == "a" * 64


def test_worktree_claim_token_rejects_non_regular_file(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    os.mkfifo(git_dir / "autoform-claim-session")

    with pytest.raises(ValueError, match="must be a regular file"):
        cli._worktree_claim_token(git_dir)


def test_blueprint_project_selects_that_projects_origin(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
    _blueprint(project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    args = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--worker-id",
        "worker-a",
        "--session-id",
        "session-a",
        "--scratch",
        str(tmp_path / "scratch"),
        "--blueprint",
        str(project),
    ]
    assert main(args) == 0
    assert "acquired" in capsys.readouterr().out


def test_nested_blueprint_resolves_relative_origin_from_worktree_root(
    tmp_path: Path, capsys
) -> None:
    repo = _bare_repo(tmp_path / "remote")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "../remote/claims.git"],
        cwd=project,
        check=True,
    )
    blueprint = _blueprint(project)

    assert main(
        [
            "claim",
            "acquire",
            "--resource",
            "lake-build",
            "--worker-id",
            "worker-a",
            "--scratch",
            str(tmp_path / "scratch"),
            "--blueprint",
            str(blueprint),
        ]
    ) == 0
    assert "acquired" in capsys.readouterr().out
    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert refs == sorted(
        [
            CLAIM_REF_PREFIX + author_claim_key("lake-build"),
            CLAIM_REF_PREFIX + resource_claim_key("lake-build"),
        ]
    )


def test_origin_url_preserves_scp_like_remote_without_user(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    origin = "git.example.test:team/claims.git"
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=project, check=True)

    assert cli._origin_url(project) == origin


def test_origin_url_ignores_inherited_and_local_url_rewrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intended = _bare_repo(tmp_path / "intended")
    redirected = _bare_repo(tmp_path / "redirected")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(intended)],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "config", f"url.{redirected}.insteadOf", str(intended)],
        cwd=project,
        check=True,
    )
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{redirected}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(intended))

    assert cli._origin_url(project) == str(intended)


def test_claim_target_pins_origin_before_blueprint_path_replacement(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    intended_repo = _bare_repo(tmp_path / "intended")
    redirected_repo = _bare_repo(tmp_path / "redirected")
    intended_project = tmp_path / "project"
    redirected_project = tmp_path / "redirected-project"
    for project, repo in (
        (intended_project, intended_repo),
        (redirected_project, redirected_repo),
    ):
        project.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
        _blueprint(project)

    original_resolve = cli._resolve_claim_target
    pinned_project = tmp_path / "pinned-project"

    def resolve_then_replace(args):
        target = original_resolve(args)
        intended_project.rename(pinned_project)
        intended_project.symlink_to(redirected_project, target_is_directory=True)
        return target

    monkeypatch.setattr(cli, "_resolve_claim_target", resolve_then_replace)
    args = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--worker-id",
        "worker-a",
        "--scratch",
        str(tmp_path / "scratch"),
        "--blueprint",
        str(intended_project),
    ]

    assert main(args) == 0
    assert "acquired" in capsys.readouterr().out
    intended_refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=intended_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    redirected_refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=redirected_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert len(intended_refs) == 2
    assert redirected_refs == []


def test_claim_target_rejects_aba_replacement_during_origin_resolution(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    intended_repo = _bare_repo(tmp_path / "intended")
    redirected_repo = _bare_repo(tmp_path / "redirected")
    intended_project = tmp_path / "project"
    redirected_project = tmp_path / "redirected-project"
    for project, repo in (
        (intended_project, intended_repo),
        (redirected_project, redirected_repo),
    ):
        project.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
        _blueprint(project)

    original_origin = cli._origin_url
    parked_project = tmp_path / "parked-project"

    def origin_during_aba(context):
        intended_project.rename(parked_project)
        intended_project.symlink_to(redirected_project, target_is_directory=True)
        try:
            return original_origin(context)
        finally:
            intended_project.unlink()
            parked_project.rename(intended_project)

    monkeypatch.setattr(cli, "_origin_url", origin_during_aba)

    assert main(
        [
            "claim",
            "acquire",
            "chapter/main-result",
            "--worker-id",
            "worker-a",
            "--scratch",
            str(tmp_path / "scratch"),
            "--blueprint",
            str(intended_project),
        ]
    ) == 1
    assert "was replaced while resolving the claim" in capsys.readouterr().out
    for repo in (intended_repo, redirected_repo):
        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert refs == []


def test_claim_target_rejects_ancestor_aba_during_origin_resolution(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    intended_repo = _bare_repo(tmp_path / "intended")
    redirected_repo = _bare_repo(tmp_path / "redirected")
    project_root = tmp_path / "projects"
    intended_scope = project_root / "scope"
    redirected_scope = project_root / "other"
    intended_project = intended_scope / "inner" / "project"
    redirected_project = redirected_scope / "inner" / "project"
    for project, repo in (
        (intended_project, intended_repo),
        (redirected_project, redirected_repo),
    ):
        project.mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
        _blueprint(project)

    original_origin = cli._origin_url
    parked = project_root / "parked"

    def origin_during_ancestor_aba(context):
        intended_scope.rename(parked)
        intended_scope.symlink_to(redirected_scope, target_is_directory=True)
        try:
            return original_origin(context)
        finally:
            intended_scope.unlink()
            parked.rename(intended_scope)

    monkeypatch.setattr(cli, "_origin_url", origin_during_ancestor_aba)

    assert main(
        [
            "claim",
            "acquire",
            "chapter/main-result",
            "--worker-id",
            "worker-a",
            "--scratch",
            str(tmp_path / "scratch"),
            "--blueprint",
            str(intended_project),
        ]
    ) == 1
    assert "was replaced while resolving the claim" in capsys.readouterr().out
    for repo in (intended_repo, redirected_repo):
        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert refs == []


def test_replacement_worktree_cannot_inherit_default_claim_session(
    tmp_path: Path, capsys
) -> None:
    repo = _bare_repo(tmp_path)
    project = tmp_path / "project"

    def initialize_worktree(path: Path) -> None:
        path.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=path, check=True)
        _blueprint(path)

    initialize_worktree(project)
    args = [
        "claim",
        "acquire",
        "chapter/main-result",
        "--worker-id",
        "worker-a",
        "--scratch",
        str(tmp_path / "scratch"),
        "--blueprint",
        str(project),
    ]
    assert main(args) == 0
    capsys.readouterr()
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    key = author_claim_key("af_0123456789abcdef01234567")
    board._ensure_scratch()
    original_oid = board._remote_oid(key)

    project.rename(tmp_path / "original-project")
    initialize_worktree(project)
    args[1] = "renew"

    assert main(args) == 1
    assert "ownership is held or unverifiable" in capsys.readouterr().out
    assert board._remote_oid(key) == original_oid


def test_cleanup_rejects_blueprint_replacement_before_selecting_origin(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    intended_repo = _bare_repo(tmp_path / "intended")
    redirected_repo = _bare_repo(tmp_path / "redirected")
    intended_project = tmp_path / "project"
    redirected_project = tmp_path / "redirected-project"
    for project, repo in (
        (intended_project, intended_repo),
        (redirected_project, redirected_repo),
    ):
        project.mkdir()
        subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=project, check=True)
        _blueprint(project)

    key = "expired"
    lease = {
        "schema": CLAIM_SCHEMA,
        "lease_id": "1" * 64,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "renewed_at": 100.0,
        "expires_at": 200.0,
        "resource": key,
    }
    _plant_message(intended_repo, key, json.dumps(lease))
    original_load = cli.load_bound_graph
    pinned_project = tmp_path / "pinned-project"

    def load_then_replace(paths):
        graph = original_load(paths)
        intended_project.rename(pinned_project)
        intended_project.symlink_to(redirected_project, target_is_directory=True)
        return graph

    monkeypatch.setattr(cli, "load_bound_graph", load_then_replace)

    assert main(["claim", "cleanup", "--blueprint", str(intended_project)]) == 1
    assert "was replaced while resolving the claim" in capsys.readouterr().out
    board = ClaimBoard(intended_repo, "inspector", tmp_path / "inspect-cleanup")
    assert board.read(key) is not None
    assert ClaimBoard(redirected_repo, "inspector", tmp_path / "inspect-redirected").list() == []


def test_cleanup_needs_no_worker_or_worktree_session(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = _bare_repo(tmp_path)
    key = "expired"
    lease = {
        "schema": CLAIM_SCHEMA,
        "lease_id": "1" * 64,
        "owner": "old-worker",
        "host": "old-host",
        "pid": 1,
        "acquired_at": 100.0,
        "renewed_at": 100.0,
        "expires_at": 200.0,
        "resource": key,
    }
    _plant_message(repo, key, json.dumps(lease))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    args = ["claim", "cleanup", "--repo", str(repo), "--scratch", str(tmp_path / "scratch")]

    assert main(args) == 0
    assert "recovered 1 expired or unsafe-timestamp claim(s)" in capsys.readouterr().out


def test_cleanup_with_blueprint_retires_old_paths_without_blocking_durable_ids(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    repo = _bare_repo(tmp_path)
    blueprint = _blueprint(tmp_path)
    old_path_key = author_claim_key("chapter/old-result")
    canonical_key = author_claim_key("af_0123456789abcdef01234567")
    for key in (old_path_key, canonical_key):
        lease = {
            "schema": LEGACY_CLAIM_SCHEMA,
            "owner": "old-worker",
            "host": "old-host",
            "pid": 1,
            "acquired_at": 100.0,
            "expires_at": 200.0,
            "resource": key,
        }
        _plant_message(repo, key, json.dumps(lease))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert main(
        [
            "claim",
            "cleanup",
            "--repo",
            str(repo),
            "--scratch",
            str(tmp_path / "scratch"),
            "--blueprint",
            str(blueprint),
        ]
    ) == 0
    assert "recovered 2" in capsys.readouterr().out
    board = ClaimBoard(repo, "inspector", tmp_path / "inspect")
    assert board.read(old_path_key)["schema"] == LEGACY_BLOCK_SCHEMA
    assert board.read(canonical_key) is None
