from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import autoform_cli.render as render_module
from autoform_cli.render import PUBLICATION_MANIFEST, PublicationError, render_site


@pytest.fixture(autouse=True)
def _clear_github_repository_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("GITHUB_REPOSITORY", "GITHUB_SERVER_URL", "GITHUB_SHA"):
        monkeypatch.delenv(variable, raising=False)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _project(tmp_path: Path, suffix: str) -> tuple[Path, Path, bytes]:
    project = tmp_path / "project"
    blueprint = project / "blueprint"
    roadmap = blueprint / "roadmap"
    roadmap.mkdir(parents=True)
    (roadmap / "README.md").write_text("# Book\n", encoding="utf-8")
    artifact = f"AUTOFORM-SECRET-SENTINEL-{suffix}\n".encode()
    relative = Path("sources") / "nested" / f"book{suffix}"
    source = blueprint / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(artifact)
    (roadmap / "result.md").write_text(
        "---\n"
        "declaration: theorem\n"
        "source_units: [result]\n"
        "---\n\n"
        "# Result\n\nThe result.\n\n"
        "## Sources\n\n"
        f"- [Textbook](../{relative.as_posix()})\n"
        f"- ![Scan](../{relative.as_posix()})\n"
        f"- <../{relative.as_posix()}>\n"
        "- [Reference][book-source]\n\n"
        f"[book-source]: ../{relative.as_posix()}\n",
        encoding="utf-8",
    )
    coverage = blueprint / "coverage" / "README.md"
    coverage.parent.mkdir(parents=True)
    coverage.write_text(
        "---\n"
        "schema: autoform-coverage/v2\n"
        f"artifact: {relative.as_posix()}\n"
        f"artifact_sha256: {_digest(artifact)}\n"
        "---\n\n"
        "# Coverage\n\n"
        "| Unit | Area | Lines | Locator | Unit SHA-256 | Coverage | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| result | Main result | 1-1 | theorem | {_digest(artifact)} | "
        "DECOMPOSED | [Result](../roadmap/result.md) |\n",
        encoding="utf-8",
    )
    return project, source, artifact


@pytest.mark.parametrize("suffix", [".txt", ".md"])
@pytest.mark.parametrize("with_coordinates", [False, True])
def test_named_artifact_never_enters_snapshot_or_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    with_coordinates: bool,
) -> None:
    project, source, artifact = _project(tmp_path, suffix)
    output = tmp_path / "site"
    original = render_module._render_snapshot

    def inspect_snapshot(snapshot, *args, **kwargs):
        relative = source.relative_to(project / "blueprint")
        assert not (Path(snapshot) / relative).exists()
        return original(snapshot, *args, **kwargs)

    monkeypatch.setattr(render_module, "_render_snapshot", inspect_snapshot)
    kwargs = (
        {"repository_url": "https://github.com/owner/repo", "ref": "abc123"}
        if with_coordinates
        else {"repository_url": "", "ref": ""}
    )

    render_site(project / "blueprint", output, lean_root=project, **kwargs)

    generated = b"\n".join(
        path.read_bytes() for path in output.rglob("*") if path.is_file()
    )
    assert artifact.rstrip(b"\n") not in generated
    assert not (output / source.relative_to(project / "blueprint")).exists()
    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    if with_coordinates:
        assert (
            f"https://github.com/owner/repo/blob/abc123/blueprint/"
            f"{source.relative_to(project / 'blueprint').as_posix()}"
        ) in chapter
    else:
        assert source.name not in chapter
        assert "[Textbook](" not in chapter


def test_stale_artifact_fails_without_replacing_previous_publication(tmp_path: Path) -> None:
    project, source, _ = _project(tmp_path, ".txt")
    output = tmp_path / "site"
    render_site(project / "blueprint", output, lean_root=project)
    before = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    source.write_text("changed source\n", encoding="utf-8")

    with pytest.raises(PublicationError, match="artifact_sha256 does not match"):
        render_site(project / "blueprint", output, lean_root=project)

    after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert (output / PUBLICATION_MANIFEST).is_file()


def test_nonclean_upgrade_does_not_retain_artifact_from_v1_site(tmp_path: Path) -> None:
    project, source, artifact = _project(tmp_path, ".md")
    coverage = project / "blueprint/coverage/README.md"
    v2_contract = coverage.read_bytes()
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Book | OUT | Kept only to seed a legacy publication |\n",
        encoding="utf-8",
    )
    output = tmp_path / "site"
    render_site(project / "blueprint", output, lean_root=project)
    published_artifact = output / source.relative_to(project / "blueprint")
    assert published_artifact.is_file()

    coverage.write_bytes(v2_contract)
    render_site(project / "blueprint", output, lean_root=project, clean=False)

    assert not published_artifact.exists()
    generated = b"\n".join(
        path.read_bytes() for path in output.rglob("*") if path.is_file()
    )
    assert artifact.rstrip(b"\n") not in generated


def test_nonclean_v2_publication_purges_renamed_and_retired_source_artifacts(
    tmp_path: Path,
) -> None:
    project, source, artifact = _project(tmp_path, ".md")
    blueprint = project / "blueprint"
    coverage = blueprint / "coverage/README.md"
    v2_contract = coverage.read_text(encoding="utf-8")
    retired = blueprint / "sources/nested/retired-source.txt"
    retired_bytes = b"AUTOFORM-RETIRED-SOURCE-SENTINEL\n"
    retired.write_bytes(retired_bytes)
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Book | OUT | Kept only to seed a legacy publication |\n",
        encoding="utf-8",
    )
    output = tmp_path / "site"
    render_site(blueprint, output, lean_root=project)
    old_relative = source.relative_to(blueprint)
    assert (output / old_relative).is_file()
    assert (output / retired.relative_to(blueprint)).is_file()

    new_source = source.with_name("renamed-book.md")
    source.rename(new_source)
    retired.unlink()
    new_relative = new_source.relative_to(blueprint)
    coverage.write_text(
        v2_contract.replace(old_relative.as_posix(), new_relative.as_posix()),
        encoding="utf-8",
    )
    article = blueprint / "roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8").replace(
            old_relative.as_posix(), new_relative.as_posix()
        ),
        encoding="utf-8",
    )

    render_site(blueprint, output, lean_root=project, clean=False)

    assert not (output / "sources").exists()
    generated = b"\n".join(
        path.read_bytes() for path in output.rglob("*") if path.is_file()
    )
    assert artifact.rstrip(b"\n") not in generated
    assert retired_bytes.rstrip(b"\n") not in generated


@pytest.mark.parametrize(
    "raw_html",
    [
        '<a href="../sources/nested/book.txt">Textbook</a>',
        "<img src='../sources/nested/book.txt' alt='scan'>",
        '<a\n href="../sources/nested/book.txt">Textbook</a>',
        '<a href="/sources/nested/book.txt">Textbook</a>',
        '<svg><use xlink:href="..%2Fsources%2Fnested%2Fbook.txt"></use></svg>',
    ],
)
def test_raw_html_links_to_excluded_v2_sources_fail_before_publication(
    tmp_path: Path, raw_html: str
) -> None:
    project, _, _ = _project(tmp_path, ".txt")
    output = tmp_path / "site"
    render_site(project / "blueprint", output, lean_root=project)
    before = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8") + f"\n{raw_html}\n",
        encoding="utf-8",
    )

    with pytest.raises(PublicationError, match="raw HTML link targets an excluded source"):
        render_site(project / "blueprint", output, lean_root=project)

    after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_raw_html_source_link_examples_in_code_remain_publishable(tmp_path: Path) -> None:
    project, _, _ = _project(tmp_path, ".txt")
    article = project / "blueprint/roadmap/result.md"
    article.write_text(
        article.read_text(encoding="utf-8")
        + "\n`<a href=\"../sources/nested/book.txt\">example</a>`\n\n"
        "```html\n<a href=\"../sources/nested/book.txt\">example</a>\n```\n",
        encoding="utf-8",
    )

    render_site(project / "blueprint", tmp_path / "site", lean_root=project)


@pytest.mark.parametrize("image", ["", "!"])
@pytest.mark.parametrize("with_coordinates", [False, True])
def test_angle_bracket_inline_source_destinations_with_spaces_are_whole(
    tmp_path: Path, image: str, with_coordinates: bool
) -> None:
    project, _, _ = _project(tmp_path, ".txt")
    source = project / "blueprint/sources/nested/spaced source.txt"
    source.write_text("A second source.\n", encoding="utf-8")
    article = project / "blueprint/roadmap/result.md"
    relative = source.relative_to(project / "blueprint")
    article.write_text(
        article.read_text(encoding="utf-8")
        + f'\n{image}[Spaced source](<../{relative.as_posix()}> "Source title")\n',
        encoding="utf-8",
    )
    output = tmp_path / "site"
    kwargs = (
        {"repository_url": "https://github.com/owner/repo", "ref": "abc123"}
        if with_coordinates
        else {"repository_url": "", "ref": ""}
    )

    render_site(project / "blueprint", output, lean_root=project, **kwargs)

    chapter = (output / "roadmap/README.md").read_text(encoding="utf-8")
    assert f"<../{relative.as_posix()}>" not in chapter
    if with_coordinates:
        expected = (
            "https://github.com/owner/repo/blob/abc123/blueprint/"
            + relative.as_posix().replace(" ", "%20")
        )
        assert f'{image}[Spaced source]({expected} "Source title")' in chapter
    else:
        assert "Spaced source" in chapter
        assert f"{image}[Spaced source](" not in chapter


def test_nonclean_v2_rejects_raw_source_link_carried_from_prior_site(
    tmp_path: Path,
) -> None:
    project, _, _ = _project(tmp_path, ".txt")
    blueprint = project / "blueprint"
    coverage = blueprint / "coverage/README.md"
    v2_contract = coverage.read_bytes()
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Book | OUT | Kept only to seed a legacy publication |\n",
        encoding="utf-8",
    )
    stale = blueprint / "retired.md"
    stale.write_text(
        '# Retired\n\n<a href="sources/nested/book.txt">Old source</a>\n',
        encoding="utf-8",
    )
    output = tmp_path / "site"
    render_site(blueprint, output, lean_root=project)
    before = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }

    stale.unlink()
    coverage.write_bytes(v2_contract)
    with pytest.raises(PublicationError, match="raw HTML link targets an excluded source"):
        render_site(blueprint, output, lean_root=project, clean=False)

    after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    "stale_link",
    [
        "[Old source](sources/nested/book.txt)",
        "![Old scan](sources/nested/book.txt)",
        "[Old source](<sources/nested/book note.txt>)",
        "[Old [nested] source](sources/nested/book.txt)",
        "[Old source][old-source]\n\n[old-source]: sources/nested/book.txt",
        "<./sources/nested/book.txt>",
    ],
)
def test_nonclean_v2_rejects_stale_markdown_source_links_from_prior_site(
    tmp_path: Path, stale_link: str
) -> None:
    project, _, _ = _project(tmp_path, ".txt")
    blueprint = project / "blueprint"
    coverage = blueprint / "coverage/README.md"
    v2_contract = coverage.read_bytes()
    coverage.write_text(
        "# Coverage\n\n"
        "| Area | Coverage | Evidence |\n"
        "| --- | --- | --- |\n"
        "| Book | OUT | Kept only to seed a legacy publication |\n",
        encoding="utf-8",
    )
    stale = blueprint / "retired.md"
    stale.write_text(f"# Retired\n\n{stale_link}\n", encoding="utf-8")
    output = tmp_path / "site"
    render_site(blueprint, output, lean_root=project)
    before = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }

    stale.unlink()
    coverage.write_bytes(v2_contract)
    with pytest.raises(PublicationError, match="Markdown link targets an excluded source"):
        render_site(blueprint, output, lean_root=project, clean=False)

    after = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert after == before
