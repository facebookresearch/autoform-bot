from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from autoform_cli import claims
from autoform_cli.__main__ import main
from autoform_cli.claims import CLAIM_REF_PREFIX, ClaimBoard, author_claim_key


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


def _args(repo: Path, scratch: Path, *command: str) -> list[str]:
    return [
        "claim",
        *command,
        "--repo",
        str(repo),
        "--worker-id",
        "worker-a",
        "--scratch",
        str(scratch),
    ]


def test_claim_cli_acquire_renew_list_release_round_trip(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    node_id = "chapter/main theorem"

    assert main(_args(repo, scratch, "acquire", node_id, "--ttl", "600")) == 0
    assert "acquired chapter/main theorem" in capsys.readouterr().out
    assert main(_args(repo, scratch, "renew", node_id, "--ttl", "600")) == 0
    assert "renewed chapter/main theorem" in capsys.readouterr().out
    assert main(_args(repo, scratch, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    assert leases[0]["_key"] == author_claim_key(node_id)
    assert leases[0]["owner"] == "worker-a"
    assert main(_args(repo, scratch, "release", node_id)) == 0
    assert "released chapter/main theorem" in capsys.readouterr().out


def test_claim_cli_refuses_live_peer_and_requires_identity(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert main(_args(repo, first, "acquire", "node")) == 0
    capsys.readouterr()

    peer = [
        "claim",
        "acquire",
        "node",
        "--repo",
        str(repo),
        "--worker-id",
        "worker-b",
        "--scratch",
        str(second),
    ]
    assert main(peer) == 1
    assert "ownership is held or unverifiable" in capsys.readouterr().out

    assert main(["claim", "list", "--repo", str(repo), "--scratch", str(second)]) == 1
    assert "--worker-id" in capsys.readouterr().err


def test_claim_cli_transport_failure_is_nonzero(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing" / "claims.git"
    assert main(_args(missing, tmp_path / "scratch", "acquire", "node")) == 1
    assert "error:" in capsys.readouterr().out


def test_claim_cli_refuses_malformed_remote_lease(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    node_id = "node"
    _plant_message(repo, author_claim_key(node_id), "not json")

    assert main(_args(repo, tmp_path / "scratch", "acquire", node_id)) == 1
    assert "invalid lease JSON" in capsys.readouterr().out


def _claim_refs(repo: Path) -> list[str]:
    listing = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", CLAIM_REF_PREFIX],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(listing.split())


def _peer_args(repo: Path, scratch: Path, *command: str) -> list[str]:
    return ["claim", *command, "--repo", str(repo), "--worker-id", "worker-b", "--scratch", str(scratch)]


def test_claim_cli_single_node_output_is_unchanged(tmp_path: Path, capsys, monkeypatch) -> None:
    def batch_path(*args: object, **kwargs: object) -> None:
        raise AssertionError("one target must take the single-key path")

    for name in ("acquire_many", "renew_many", "release_many"):
        monkeypatch.setattr(ClaimBoard, name, batch_path)
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    key = author_claim_key("node")

    assert main(_args(repo, scratch, "acquire", "node", "--ttl", "600")) == 0
    assert capsys.readouterr().out == f"acquired node ({key})\n"
    assert main(_args(repo, scratch, "renew", "node", "--ttl", "600")) == 0
    assert capsys.readouterr().out == f"renewed node ({key})\n"
    assert main(_peer_args(repo, tmp_path / "peer", "acquire", "node")) == 1
    assert capsys.readouterr().out == "error: could not acquire node; ownership is held or unverifiable\n"
    assert main(_args(repo, scratch, "release", "node")) == 0
    assert capsys.readouterr().out == f"released node ({key})\n"


def test_claim_cli_multi_node_round_trip_prints_one_line_per_target(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    nodes = ["chapter/main theorem", "chapter/helper"]

    def expected(past_tense: str) -> str:
        return "".join(f"{past_tense} {node} ({author_claim_key(node)})\n" for node in nodes)

    assert main(_args(repo, scratch, "acquire", *nodes, "--ttl", "600", "--note", "revision")) == 0
    assert capsys.readouterr().out == expected("acquired")
    assert main(_args(repo, scratch, "renew", *nodes, "--ttl", "600")) == 0
    assert capsys.readouterr().out == expected("renewed")
    assert main(_args(repo, scratch, "list")) == 0
    leases = json.loads(capsys.readouterr().out)
    assert sorted(lease["_key"] for lease in leases) == sorted(author_claim_key(node) for node in nodes)
    assert {(lease["owner"], lease["note"]) for lease in leases} == {("worker-a", "revision")}
    assert main(_args(repo, scratch, "release", *nodes)) == 0
    assert capsys.readouterr().out == expected("released")
    assert _claim_refs(repo) == []


def test_claim_cli_multi_node_failure_changes_no_claim(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    assert main(_peer_args(repo, tmp_path / "peer", "acquire", "b")) == 0
    capsys.readouterr()

    assert main(_args(repo, scratch, "acquire", "a", "b", "c")) == 1
    captured = capsys.readouterr()
    assert captured.out == "error: could not acquire a, b, c; no claim was acquired: held by another worker: b\n"
    assert captured.err == ""
    assert _claim_refs(repo) == [CLAIM_REF_PREFIX + author_claim_key("b")]

    assert main(_args(repo, scratch, "acquire", "a")) == 0
    capsys.readouterr()
    assert main(_args(repo, scratch, "renew", "a", "b")) == 1
    assert capsys.readouterr().out == (
        "error: could not renew a, b; no claim was renewed: not held by this worker: b\n"
    )
    assert main(_args(repo, scratch, "release", "a", "b")) == 1
    assert capsys.readouterr().out == (
        "error: could not release a, b; no claim was released: not held by this worker: b\n"
    )
    assert _claim_refs(repo) == sorted(CLAIM_REF_PREFIX + author_claim_key(node) for node in ("a", "b"))


def test_claim_cli_multi_node_names_a_malformed_claim(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    _plant_message(repo, author_claim_key("b"), "not json")

    assert main(_args(repo, tmp_path / "scratch", "acquire", "a", "b")) == 1
    assert capsys.readouterr().out == "error: could not acquire a, b; no claim was acquired: malformed lease: b\n"
    assert _claim_refs(repo) == [CLAIM_REF_PREFIX + author_claim_key("b")]


def test_claim_cli_rejects_duplicate_targets_before_touching_the_board(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"

    assert main(_args(repo, scratch, "acquire", "a", "b", "a")) == 2
    captured = capsys.readouterr()
    assert captured.err == "error: duplicate claim target: a\n"
    assert captured.out == ""
    assert _claim_refs(repo) == []
    assert not scratch.exists()


def test_claim_cli_multi_node_transport_failure_is_nonzero(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing" / "claims.git"
    assert main(_args(missing, tmp_path / "scratch", "acquire", "a", "b")) == 1
    assert capsys.readouterr().out.startswith("error: ")


def test_claim_cli_names_a_stale_ref_lock_and_list_and_cleanup_warn_about_it(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(claims, "_LOCK_GRACE_S", 0.1)
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    ref = CLAIM_REF_PREFIX + author_claim_key("a")
    lock = repo / f"{ref}.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    os.utime(lock, (1_700_000_000, 1_700_000_000))
    described = f"Git lock file {lock.resolve()} (modified 2023-11-14T22:13:20Z) blocks updates of {ref}"

    assert main(_args(repo, scratch, "acquire", "a")) == 1
    assert capsys.readouterr().out == (
        f"error: {described}. Git leaves a lock file behind when a process stops while updating a ref; "
        "if no Git process is using the claim board, delete it and retry\n"
    )

    for command, stdout in (("list", "[]\n"), ("cleanup", "removed 0 expired claim(s)\n")):
        assert main(_args(repo, scratch, command)) == 0
        captured = capsys.readouterr()
        assert captured.out == stdout
        assert captured.err == f"warning: {described}; if no Git process is using the claim board, delete it\n"
    assert lock.exists()


def test_claim_cli_lists_only_the_named_targets(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)
    scratch = tmp_path / "scratch"
    assert main(_args(repo, scratch, "acquire", "node-a", "node-b")) == 0
    capsys.readouterr()

    assert main(_args(repo, scratch, "list", "node-b", "node-absent")) == 0
    captured = capsys.readouterr()

    assert [lease["_key"] for lease in json.loads(captured.out)] == [author_claim_key("node-b")]
    assert captured.err == ""


def test_claim_cli_list_refuses_a_duplicate_target(tmp_path: Path, capsys) -> None:
    repo = _bare_repo(tmp_path)

    assert main(_args(repo, tmp_path / "scratch", "list", "node-a", "node-a")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: duplicate claim target: node-a\n"


def test_claim_cli_list_labels_a_timeout(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.ext.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    args = ["claim", "list", "--repo", "ext::sleep 30", "--worker-id", "worker-a"]

    assert main([*args, "--scratch", str(tmp_path / "scratch"), "--timeout", "1"]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == (
        "error: timeout: no answer from the claim board within 1s while fetching the claim board\n"
    )


def test_claim_cli_list_labels_a_transport_failure(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing" / "claims.git"

    assert main(_args(missing, tmp_path / "scratch", "list")) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err.startswith("error: transport: ")


def test_claim_cli_list_labels_contention(tmp_path: Path, capsys, monkeypatch) -> None:
    def contended(self: ClaimBoard, *args: object, **kwargs: object) -> list[dict[str, object]]:
        raise claims.ClaimContentionError("the claim board refused 3 fetches because claim refs kept moving")

    monkeypatch.setattr(ClaimBoard, "list", contended)

    assert main(_args(_bare_repo(tmp_path), tmp_path / "scratch", "list")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: contention: the claim board refused 3 fetches because claim refs kept moving\n"
    )


def test_claim_cli_list_names_a_slow_step_on_stderr(tmp_path: Path, capsys, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def slow(self: ClaimBoard, keys: object = None, *, timeout: object = None, progress=None) -> list[object]:
        seen.update(keys=keys, timeout=timeout)
        progress("fetching 1 claim refs")
        return []

    monkeypatch.setattr(ClaimBoard, "list", slow)

    assert main([*_args(_bare_repo(tmp_path), tmp_path / "scratch", "list", "node-a"), "--timeout", "7"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "[]\n"
    assert captured.err == "claim list: fetching 1 claim refs...\n"
    assert seen == {"keys": [author_claim_key("node-a")], "timeout": 7.0}


def test_claim_cli_list_without_targets_passes_none_and_the_default_timeout(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    seen: dict[str, object] = {}

    def record(self: ClaimBoard, keys: object = None, *, timeout: object = None, progress=None) -> list[object]:
        seen.update(keys=keys, timeout=timeout)
        return []

    monkeypatch.setattr(ClaimBoard, "list", record)

    assert main(_args(_bare_repo(tmp_path), tmp_path / "scratch", "list")) == 0
    assert seen == {"keys": None, "timeout": 120.0}
