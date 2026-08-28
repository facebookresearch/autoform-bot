from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autoform_cli.__main__ import main
from autoform_cli.claims import CLAIM_REF_PREFIX, author_claim_key


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
    assert "--worker-id" in capsys.readouterr().out


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
