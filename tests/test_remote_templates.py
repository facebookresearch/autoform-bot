from __future__ import annotations

from pathlib import Path

import pytest

from autoform_cli import remote_templates


SOURCE = "https://example.test/owner/autoform.git"
REVISION = "1" * 40
OBJECT_ID = "2" * 40
CONTENT = b"template\n"


def _listing(*, mode: str = "100644", omitted: str | None = None) -> bytes:
    return b"".join(
        f"{mode} blob {OBJECT_ID}\tautoform_cli/templates/{relative}".encode("ascii") + b"\0"
        for relative in sorted(remote_templates._REQUIRED_TEMPLATES)
        if relative != omitted
    )


def test_tree_parser_requires_the_complete_canonical_template_surface() -> None:
    parsed = remote_templates._parse_tree(_listing())

    assert set(parsed) == remote_templates._REQUIRED_TEMPLATES
    assert set(parsed.values()) == {(OBJECT_ID, 0o100644)}

    with pytest.raises(remote_templates.TemplateSnapshotError, match="expected surface"):
        remote_templates._parse_tree(_listing(omitted="github/workflows/autoform-verify.yml"))

    extra = (
        f"100644 blob {'3' * 40}\tautoform_cli/templates/unexpected.txt\0"
    ).encode("ascii")
    with pytest.raises(remote_templates.TemplateSnapshotError, match="expected surface"):
        remote_templates._parse_tree(_listing() + extra)


def test_tree_parser_rejects_links_and_case_aliases() -> None:
    with pytest.raises(remote_templates.TemplateSnapshotError, match="invalid"):
        remote_templates._parse_tree(_listing(mode="120000"))

    alias = (
        f"100644 blob {OBJECT_ID}\tautoform_cli/templates/README.md\0"
        f"100644 blob {'3' * 40}\tautoform_cli/templates/readme.md\0"
    ).encode("ascii")
    with pytest.raises(remote_templates.TemplateSnapshotError, match="invalid"):
        remote_templates._parse_tree(_listing() + alias)

    mapped_alias = (
        f"100644 blob {'3' * 40}\t"
        "autoform_cli/templates/.github/workflows/autoform-verify.yml\0"
    ).encode("ascii")
    with pytest.raises(remote_templates.TemplateSnapshotError, match="invalid"):
        remote_templates._parse_tree(_listing() + mapped_alias)


def test_blob_protocol_is_bounded_and_exact() -> None:
    sizes = remote_templates._parse_sizes(
        f"{OBJECT_ID} blob {len(CONTENT)}\n".encode("ascii"),
        (OBJECT_ID,),
    )
    payload = f"{OBJECT_ID} blob {len(CONTENT)}\n".encode("ascii") + CONTENT + b"\n"

    assert remote_templates._parse_blobs(payload, (OBJECT_ID,), sizes) == {
        OBJECT_ID: CONTENT
    }

    with pytest.raises(remote_templates.TemplateSnapshotError, match="too large"):
        remote_templates._parse_sizes(
            f"{OBJECT_ID} blob {remote_templates._MAX_FILE_BYTES + 1}\n".encode("ascii"),
            (OBJECT_ID,),
        )


def test_git_output_is_rejected_at_the_bound(tmp_path: Path) -> None:
    with pytest.raises(remote_templates.TemplateSnapshotError, match="too large"):
        remote_templates._run_git(
            ["version"],
            cwd=tmp_path,
            home=tmp_path,
            deadline=remote_templates.time.monotonic() + 10,
            max_output=1,
        )


def test_initial_fetch_must_honor_the_blob_filter() -> None:
    remote_templates._require_blobless_initial_fetch(b"commit\ntree\n")

    with pytest.raises(remote_templates.TemplateSnapshotError, match="ignored"):
        remote_templates._require_blobless_initial_fetch(b"commit\nblob\ntree\n")


def test_fetch_reads_only_the_template_subtree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run_git(
        arguments: list[str],
        *,
        cwd: Path,
        home: Path,
        deadline: float,
        input_bytes: bytes | None = None,
        max_output: int,
    ) -> bytes:
        assert cwd.is_dir()
        assert home.is_dir()
        assert deadline > 0
        assert max_output > 0
        calls.append(arguments)
        if arguments[0] == "init":
            Path(arguments[-1]).mkdir()
            return b""
        if arguments[:2] == ["rev-parse", "--verify"]:
            return f"{REVISION}\n".encode("ascii")
        if arguments[:2] == ["ls-tree", "-rz"]:
            assert arguments[-1] == "autoform_cli/templates"
            return _listing()
        if arguments[:2] == ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"]:
            assert input_bytes == f"{OBJECT_ID}\n".encode("ascii")
            return f"{OBJECT_ID} blob {len(CONTENT)}\n".encode("ascii")
        if arguments[:2] == ["cat-file", "--batch"]:
            assert input_bytes == f"{OBJECT_ID}\n".encode("ascii")
            return f"{OBJECT_ID} blob {len(CONTENT)}\n".encode("ascii") + CONTENT + b"\n"
        return b""

    monkeypatch.setattr(remote_templates, "_run_git", run_git)
    monkeypatch.setattr(remote_templates.tempfile, "TemporaryDirectory", lambda **kwargs: _Temp(tmp_path))

    snapshot = remote_templates.fetch_template_snapshot(SOURCE, REVISION)

    assert {relative for relative, _, _ in snapshot} == remote_templates._REQUIRED_TEMPLATES
    assert all(content == CONTENT and mode == 0o644 for _, content, mode in snapshot)
    assert any(arguments[0] == "fetch" and "--filter=blob:none" in arguments for arguments in calls)


class _Temp:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> str:
        return str(self.path)

    def __exit__(self, *args: object) -> None:
        return None
