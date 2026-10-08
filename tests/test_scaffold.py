"""The vault layout is fixed, so the tool writes it rather than describing it.

A real project came back from an agent-driven setup with chapter pages as
siblings of their directories instead of ``<chapter>/README.md``. That parses
clean and publishes a book with no chapters, so these tests pin the shape.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoform_cli import scaffold as scaffold_module
from autoform_cli.coverage import load_coverage
from autoform_cli.graph import load_graph
from autoform_cli.scaffold import ScaffoldError, scaffold_project

_EXPECTED = {
    ".github/CODEOWNERS.autoform.example",
    ".github/autoform_audit.py",
    ".github/workflows/autoform-verify.yml",
    ".github/workflows/blueprint-pages.yml",
    ".gitignore",
    "README.md",
    "blueprint/.gitignore",
    "blueprint/README.md",
    "blueprint/coverage/README.md",
    "blueprint/javascripts/mathjax.js",
    "blueprint/roadmap/README.md",
    "blueprint/sources/README.md",
    "mkdocs.yml",
    "theme/main.html",
}


def test_scaffold_ignores_python_cache_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(scaffold_module._TEMPLATES, templates)
    cache = templates / "github/__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "autoform_audit.cpython-test.pyc").write_bytes(b"\x00binary cache")
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", templates)

    project = tmp_path / "project"
    result = scaffold_project(
        project,
        title="Cache-safe",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="1" * 40,
    )

    assert ".github/autoform_audit.py" in result.written
    assert not (project / ".github/__pycache__").exists()
    assert all("__pycache__" not in path and not path.endswith(".pyc") for path in result.written)


def test_template_snapshot_rejects_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(scaffold_module._TEMPLATES, templates)
    outside = tmp_path / "outside"
    outside.write_text("not a template\n", encoding="utf-8")
    try:
        (templates / "linked-template").symlink_to(outside)
    except OSError:
        pytest.skip("the test filesystem cannot create symbolic links")
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", templates)

    with pytest.raises(ScaffoldError, match="template tree contains a link"):
        scaffold_project(tmp_path / "project", title="Link-free")

    assert not (tmp_path / "project").exists()


def test_scaffold_rejects_an_incomplete_installed_template_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(scaffold_module._TEMPLATES, templates)
    (templates / "theme" / "main.html").unlink()
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", templates)

    with pytest.raises(ScaffoldError, match="template tree is incomplete"):
        scaffold_project(
            tmp_path / "project",
            title="Complete only",
            autoform_source="https://example.test/autoform.git",
            autoform_ref="1" * 40,
        )

    assert not (tmp_path / "project").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_template_snapshot_rejects_non_regular_files_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(scaffold_module._TEMPLATES, templates)
    os.mkfifo(templates / "blocking-template")
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", templates)

    with pytest.raises(ScaffoldError, match="non-regular file"):
        scaffold_project(tmp_path / "project", title="Regular files only")


def test_template_snapshot_bounds_file_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "oversized").write_bytes(b"12345")
    monkeypatch.setattr(scaffold_module, "_MAX_TEMPLATE_FILE_BYTES", 4)

    with pytest.raises(ScaffoldError, match="4-byte limit"):
        scaffold_module._read_templates(templates)


def test_template_snapshot_bounds_entry_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "one").write_bytes(b"1")
    (templates / "two").write_bytes(b"2")
    monkeypatch.setattr(scaffold_module, "_MAX_TEMPLATE_ENTRIES", 1)

    with pytest.raises(ScaffoldError, match="exceeds 1 entries"):
        scaffold_module._read_templates(templates)


def test_template_snapshot_detects_a_change_between_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(scaffold_module._TEMPLATES, templates)
    readme = templates / "README.md"
    original_capture = scaffold_module._capture_template_snapshot
    calls = 0

    def capture(root: Path):
        nonlocal calls
        snapshot = original_capture(root)
        calls += 1
        if calls == 1:
            readme.write_bytes(readme.read_bytes() + b"\nchanged\n")
        return snapshot

    monkeypatch.setattr(scaffold_module, "_capture_template_snapshot", capture)

    with pytest.raises(ScaffoldError, match="changed while it was being read"):
        scaffold_module._read_templates(templates)


def test_scaffold_uses_the_template_snapshot_validated_for_its_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(scaffold_module._TEMPLATES, templates)
    original_readme = (templates / "README.md").read_bytes()
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", templates)

    def pin(retained: tuple[tuple[str, bytes, int], ...]) -> tuple[str, str]:
        assert dict((relative, content) for relative, content, _mode in retained)[
            "README.md"
        ] == original_readme
        (templates / "README.md").write_text("replacement\n", encoding="utf-8")
        return "", ""

    monkeypatch.setattr(scaffold_module, "plugin_pin", pin)
    project = tmp_path / "project"

    scaffold_project(project, title="Stable snapshot")

    assert (project / "README.md").read_bytes() != b"replacement\n"
    assert b"Stable snapshot" in (project / "README.md").read_bytes()


def test_scaffold_writes_the_whole_vault(tmp_path: Path) -> None:
    result = scaffold_project(
        tmp_path,
        title="Finite Flat",
        repository_url="https://example.test/repo",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="1" * 40,
    )

    assert set(result.written) == _EXPECTED
    assert result.skipped == ()
    for relative in _EXPECTED:
        assert (tmp_path / relative).is_file(), relative


def test_scaffold_rejects_non_utf8_template_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(scaffold_module._TEMPLATES, templates)
    (templates / "README.md").write_bytes(b"\xff")
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", templates)

    with pytest.raises(ScaffoldError, match="template tree contains invalid text"):
        scaffold_project(tmp_path / "project", title="Invalid")


def test_init_does_not_create_a_lean_project_shell(tmp_path: Path) -> None:
    scaffold_project(tmp_path, title="Finite Flat")

    assert not (tmp_path / "lakefile.toml").exists()
    assert not (tmp_path / "lean-toolchain").exists()
    assert not (tmp_path / "src/FiniteFlat.lean").exists()


def test_scaffolded_vault_validates_immediately(tmp_path: Path) -> None:
    """A fresh project must pass `autoform check` before any mathematics."""

    scaffold_project(tmp_path, title="Finite Flat")
    graph = load_graph(tmp_path / "blueprint")

    assert set(graph.nodes) == {"roadmap"}
    assert graph.nodes["roadmap"].parent is None


def test_scaffolded_vault_has_a_valid_incomplete_coverage_contract(tmp_path: Path) -> None:
    scaffold_project(tmp_path, title="Finite Flat")

    coverage, issues = load_coverage(tmp_path / "blueprint")

    assert issues == ()
    assert coverage is not None
    assert coverage.counts == {"MAPPED": 1, "DECOMPOSED": 0, "DEFERRED": 0, "OUT": 0}
    assert not coverage.complete


def test_user_values_are_not_reinterpreted_as_template_tokens(tmp_path: Path) -> None:
    title = "Literal {{AUTOFORM_SOURCE_YAML}} token"

    scaffold_project(
        tmp_path,
        title=title,
        autoform_source="https://example.test/autoform.git",
        autoform_ref="0" * 40,
    )

    mkdocs = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert f'site_name: "{title}"' in mkdocs
    assert title in readme
    assert '""https://example.test/autoform.git""' not in mkdocs


def test_substitutions_reach_the_site_config(tmp_path: Path) -> None:
    scaffold_project(
        tmp_path,
        title="Finite Flat",
        repository_url="https://example.test/repo",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="0" * 40,
    )

    mkdocs = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
    # Quoted: a title is a YAML scalar, not bare text pasted after a colon.
    assert 'site_name: "Finite Flat"' in mkdocs
    assert 'repo_url: "https://example.test/repo"' in mkdocs

    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert 'AUTOFORM_SOURCE: "https://example.test/autoform.git"' in verify
    assert f'AUTOFORM_REF: "{"0" * 40}"' in verify
    assert '"git+${AUTOFORM_SOURCE}@${AUTOFORM_REF}"' in verify
    assert "python3 .github/autoform_audit.py" in verify


def test_no_placeholder_survives_anywhere(tmp_path: Path) -> None:
    """`${{ }}` is Actions syntax and `{{declName}}` is Lean interpolation.

    Only our own UPPER_SNAKE placeholders must be gone.
    """
    import re

    placeholder = re.compile(r"\{\{[A-Z_]+\}\}")
    scaffold_project(tmp_path, title="Finite Flat")

    for path in sorted(tmp_path.rglob("*")):
        if path.is_file():
            assert not placeholder.search(path.read_text(encoding="utf-8")), path


def test_rerun_is_idempotent_and_reports_what_it_left(tmp_path: Path) -> None:
    options = {
        "autoform_source": "https://example.test/autoform.git",
        "autoform_ref": "1" * 40,
    }
    scaffold_project(tmp_path, title="Finite Flat", **options)
    (tmp_path / "blueprint/README.md").write_text("# Hand written\n", encoding="utf-8")

    again = scaffold_project(tmp_path, title="Finite Flat", **options)

    assert again.written == ()
    assert set(again.skipped) == _EXPECTED
    assert (tmp_path / "blueprint/README.md").read_text(encoding="utf-8") == "# Hand written\n"


def test_force_overwrites(tmp_path: Path) -> None:
    scaffold_project(tmp_path, title="Finite Flat")
    (tmp_path / "mkdocs.yml").write_text("stale\n", encoding="utf-8")

    scaffold_project(tmp_path, title="Finite Flat", force=True)

    assert 'site_name: "Finite Flat"' in (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("mkdocs.yml", b'site_name: "Finite Flat"'),
        ("blueprint/javascripts/mathjax.js", b"window.MathJax"),
    ],
)
def test_force_atomically_breaks_hard_links_for_rendered_and_static_files(
    relative: str, expected: bytes, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    destination = project / relative
    destination.parent.mkdir(parents=True)
    linked = tmp_path / "authored-original"
    linked.write_bytes(b"authored\n")
    os.link(linked, destination)
    original_inode = linked.stat().st_ino

    scaffold_project(project, title="Finite Flat", force=True)

    assert expected in destination.read_bytes()
    assert destination.stat().st_ino != original_inode
    assert linked.stat().st_ino == original_inode
    assert linked.read_bytes() == b"authored\n"


@pytest.mark.skipif(os.name != "posix", reason="exact POSIX template modes are unavailable")
def test_scaffold_normalizes_template_modes_independently_of_installer_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(scaffold_module._TEMPLATES, templates)
    for template in templates.rglob("*"):
        if template.is_file():
            template.chmod(0o775 if template.stat().st_mode & stat.S_IXUSR else 0o664)
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", templates)
    project = tmp_path / "project"

    result = scaffold_project(
        project,
        title="Canonical modes",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="1" * 40,
    )

    modes = {relative: stat.S_IMODE((project / relative).stat().st_mode) for relative in result.written}
    assert modes.pop(".github/autoform_audit.py") == 0o755
    assert set(modes.values()) == {0o644}


def test_refuses_an_empty_title(tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError, match="title must not be empty"):
        scaffold_project(tmp_path, title="   ")


def test_refuses_a_symlinked_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ScaffoldError, match="symlink"):
        scaffold_project(link, title="Finite Flat")


def test_cli_reports_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from autoform_cli.__main__ import main

    assert (
        main(
            [
                "init",
                str(tmp_path),
                "--title",
                "Finite Flat",
                "--autoform-source",
                "https://example.test/autoform.git",
                "--autoform-ref",
                "1" * 40,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["project"] == "Finite Flat"
    assert set(payload["written"]) == _EXPECTED


def test_roadmap_readme_teaches_the_chapter_shape(tmp_path: Path) -> None:
    """The exact mistake this command exists to prevent must be named in it."""

    scaffold_project(tmp_path, title="Finite Flat")
    roadmap = (tmp_path / "blueprint/roadmap/README.md").read_text(encoding="utf-8")

    assert "<chapter>/README.md" in roadmap
    assert "WITHOUT a README.md is not a chapter" in roadmap


def test_authoring_guidance_never_reaches_the_published_site(tmp_path: Path) -> None:
    """Guidance is for the author in the vault, not for a reader on the site.

    The first live run published the scaffold's own instructions as the body of
    the roadmap page: an ASCII directory diagram and "run `autoform check`"
    where a reader expected the book. Guidance now lives in HTML comments, so
    the agent still reads it while the rendered page stays clean.
    """

    from autoform_cli.render import render_site

    scaffold_project(tmp_path / "project", title="Finite Flat")
    site = tmp_path / "site-src"
    render_site(tmp_path / "project/blueprint", site)

    for page in sorted(site.rglob("*.md")):
        visible = re.sub(r"<!--.*?-->", "", page.read_text(encoding="utf-8"), flags=re.DOTALL)
        for leaked in ("AUTHORING NOTES", "is not a chapter", "some-definition.md", "autoform check"):
            assert leaked not in visible, f"{page.name} publishes authoring guidance: {leaked}"


def test_an_empty_vault_reads_as_empty_not_as_a_tutorial(tmp_path: Path) -> None:
    scaffold_project(tmp_path, title="Finite Flat")

    for relative, expected in (
        ("blueprint/roadmap/README.md", "No chapters yet."),
        ("blueprint/coverage/README.md", "| Project scope | `MAPPED` |"),
        ("blueprint/sources/README.md", "No sources recorded yet."),
    ):
        text = (tmp_path / relative).read_text(encoding="utf-8")
        visible = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        assert expected in visible
        # An empty section publishes as an empty heading, so there must be none.
        assert not re.search(r"^## ", visible, flags=re.MULTILINE), relative


def test_scaffolded_gitignore_covers_agent_bootstrap_output(tmp_path: Path) -> None:
    """The first live run committed a stray bootstrap.log."""

    scaffold_project(tmp_path, title="Finite Flat")
    assert "*.log" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


def test_scaffold_merges_autoform_rules_into_lake_gitignore(tmp_path: Path) -> None:
    lake_ignore = b"/.lake\n# project-specific\n"
    (tmp_path / ".gitignore").write_bytes(lake_ignore)

    result = scaffold_project(tmp_path, title="Finite Flat")

    merged = (tmp_path / ".gitignore").read_bytes()
    assert merged.startswith(lake_ignore)
    for rule in (b".lake/", b"site/", b"site-src/", b"*.log"):
        assert merged.splitlines().count(rule) == 1
    assert ".gitignore" in result.written
    assert ".gitignore" not in result.skipped

    again = scaffold_project(tmp_path, title="Finite Flat")
    assert (tmp_path / ".gitignore").read_bytes() == merged
    assert ".gitignore" in again.skipped


@pytest.mark.parametrize(
    "lake_ignore",
    [b"/.lake\r\n# local\r\n", b"/.lake\n# no final newline"],
)
def test_gitignore_merge_preserves_existing_line_endings_and_content(
    lake_ignore: bytes, tmp_path: Path
) -> None:
    destination = tmp_path / ".gitignore"
    destination.write_bytes(lake_ignore)

    scaffold_project(tmp_path, title="Finite Flat")

    merged = destination.read_bytes()
    assert merged.startswith(lake_ignore)
    assert b"site/\n" in merged


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_gitignore_merge_rejects_a_fifo_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / ".gitignore")

    with pytest.raises(ScaffoldError, match="non-regular .gitignore"):
        scaffold_project(tmp_path, title="Finite Flat")


def test_gitignore_merge_rejects_oversized_input(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_bytes(
        b"x" * (scaffold_module._MAX_GITIGNORE_BYTES + 1)
    )

    with pytest.raises(ScaffoldError, match="larger than"):
        scaffold_project(tmp_path, title="Finite Flat")


def test_gitignore_append_never_overwrites_a_concurrent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ".gitignore"
    destination.write_text("/.lake\n", encoding="utf-8")
    replacement = tmp_path / "replacement"
    replacement.write_text("changed concurrently\n", encoding="utf-8")
    original_write = os.write
    replaced = False

    def replace_before_append(descriptor: int, content: bytes) -> int:
        nonlocal replaced
        if not replaced:
            os.replace(replacement, destination)
            replaced = True
        return original_write(descriptor, content)

    monkeypatch.setattr(scaffold_module.os, "write", replace_before_append)

    with pytest.raises(ScaffoldError, match="may have been partially appended"):
        scaffold_module._append_gitignore_rules(destination, b"/.lake\nsite/\n")
    assert destination.read_text(encoding="utf-8") == "changed concurrently\n"


def test_gitignore_append_reports_an_in_place_concurrent_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ".gitignore"
    destination.write_bytes(b"/.lake\n")
    original_write = os.write
    edited = False

    def edit_before_append(descriptor: int, content: bytes) -> int:
        nonlocal edited
        if not edited:
            with destination.open("ab") as authored:
                authored.write(b"author-concurrent\n")
                authored.flush()
                os.fsync(authored.fileno())
            edited = True
        return original_write(descriptor, content)

    monkeypatch.setattr(scaffold_module.os, "write", edit_before_append)

    with pytest.raises(ScaffoldError, match="may have been partially appended"):
        scaffold_module._append_gitignore_rules(destination, b"/.lake\nsite/\n")
    content = destination.read_bytes()
    assert content.startswith(b"/.lake\nauthor-concurrent\n")
    assert b"site/\n" in content


def test_gitignore_append_reports_a_partial_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ".gitignore"
    original = b"/.lake\n"
    destination.write_bytes(original)
    original_write = os.write
    calls = 0

    def fail_after_one_byte(descriptor: int, content: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, content[:1])
        raise OSError("injected append failure")

    monkeypatch.setattr(scaffold_module.os, "write", fail_after_one_byte)

    with pytest.raises(ScaffoldError, match="may have been partially appended"):
        scaffold_module._append_gitignore_rules(destination, b"/.lake\nsite/\n")
    assert destination.read_bytes().startswith(original)
    assert len(destination.read_bytes()) == len(original) + 1


def test_gitignore_merge_refuses_hard_links(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    authored = tmp_path / "authored-ignore"
    authored.write_text("/.lake\n", encoding="utf-8")
    destination = project / ".gitignore"
    os.link(authored, destination)
    original_inode = authored.stat().st_ino

    with pytest.raises(ScaffoldError, match="hard-linked .gitignore"):
        scaffold_module._append_gitignore_rules(destination, b"/.lake\nsite/\n")

    assert authored.read_text(encoding="utf-8") == "/.lake\n"
    assert authored.stat().st_ino == original_inode
    assert destination.stat().st_ino == original_inode
    assert destination.read_text(encoding="utf-8") == "/.lake\n"


def test_windows_cross_interface_gitignore_identity_ignores_ctime_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / ".gitignore"
    destination.write_bytes(b"/.lake\n")
    original_fstat = os.fstat
    original_stat = os.stat

    def view(metadata: os.stat_result, changed_ns: int) -> SimpleNamespace:
        return SimpleNamespace(
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_mode=metadata.st_mode,
            st_size=metadata.st_size,
            st_mtime_ns=metadata.st_mtime_ns,
            st_ctime_ns=changed_ns,
            st_nlink=metadata.st_nlink,
            st_file_attributes=0,
        )

    monkeypatch.setattr(scaffold_module.os, "fstat", lambda descriptor: view(original_fstat(descriptor), 10))
    monkeypatch.setattr(
        scaffold_module.os,
        "stat",
        lambda path, **kwargs: view(original_stat(path, **kwargs), 20),
    )
    monkeypatch.setattr(scaffold_module, "_WINDOWS_STAT_VIEWS", True)

    assert not scaffold_module._append_gitignore_rules(destination, b"/.lake\n")
    assert destination.read_bytes() == b"/.lake\n"
def test_scaffolded_gitignore_keeps_worker_worktrees_out_of_commits(tmp_path: Path) -> None:
    """`git add -A` would record a nested worktree as a dangling gitlink."""

    scaffold_project(tmp_path, title="Finite Flat")
    ignored = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".claude/worktrees/" in ignored


def test_scaffolded_blueprint_tracks_authored_structure(tmp_path: Path) -> None:
    scaffold_project(tmp_path, title="Finite Flat")

    ignored = (tmp_path / "blueprint/.gitignore").read_text(encoding="utf-8").splitlines()

    assert "dependencies.md" in ignored
    assert "structure.md" not in ignored


def test_scaffolded_theme_defers_navigation_to_the_book(tmp_path: Path) -> None:
    """Autoform derives reading order from the vault, so MkDocs must not.

    This used to be prose in the Setup skill telling an agent to strip the
    global previous/next controls. It is now a property of the file we write.
    """

    scaffold_project(tmp_path, title="Finite Flat")
    theme = (tmp_path / "theme/main.html").read_text(encoding="utf-8")
    mkdocs = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")

    # Material renders previous/next in its footer partial; overriding the
    # whole footer suppresses it, because Autoform derives reading order from
    # the vault and prints it at the bottom of book pages only.
    assert '{% block footer %}' in theme
    assert "md-footer" in theme
    assert "md-footer__link" not in theme
    assert "docs_dir: site-src" in mkdocs
    assert "md_in_html" in mkdocs
    assert "custom_dir: theme" in mkdocs
    assert "name: material" in mkdocs


def test_generated_ci_pins_the_checkout_that_scaffolded_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A floating ref installs an Autoform that may not have this CLI.

    `facebookresearch/autoform-bot@main` predates `autoform_cli` entirely, so
    defaulting to it meant every scaffolded project's first CI run installed a
    build with no `autoform` command. The pin now comes from the checkout doing
    the scaffolding, which is immutable and known-good by construction.
    """

    checkout = tmp_path / "checkout"
    (checkout / "autoform_cli").mkdir(parents=True)
    shutil.copy2(Path(scaffold_module.__file__), checkout / "autoform_cli" / "scaffold.py")
    shutil.copytree(scaffold_module._TEMPLATES, checkout / "autoform_cli" / "templates")
    head = _repository(checkout, "https://example.test/autoform.git")
    monkeypatch.setattr(scaffold_module, "_here", lambda: checkout)
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", checkout / "autoform_cli" / "templates")
    project = tmp_path / "project"

    scaffold_project(project, title="Finite Flat")
    source, ref = scaffold_module.plugin_pin()
    verify = (project / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")

    assert f"AUTOFORM_SOURCE: {json.dumps(source)}" in verify
    assert f"AUTOFORM_REF: {json.dumps(ref)}" in verify
    assert '"git+${AUTOFORM_SOURCE}@${AUTOFORM_REF}"' in verify
    assert re.fullmatch(r"[0-9a-f]{40}", ref), "the pin must be an immutable commit"
    assert ref == head
    assert "@main" not in verify


def test_explicit_pin_overrides_the_checkout(tmp_path: Path) -> None:
    scaffold_project(
        tmp_path,
        title="Finite Flat",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="1" * 40,
    )

    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert 'AUTOFORM_SOURCE: "https://example.test/autoform.git"' in verify
    assert f'AUTOFORM_REF: "{"1" * 40}"' in verify


def test_no_ci_rather_than_a_guessed_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong pin is worse than no pin, because it fails silently.

    Installed as a plugin, Autoform is a directory copy with no `.git`, so
    `plugin_pin` has nothing to read. It used to fall back to
    `facebookresearch/autoform-bot@main`, a commit predating the CLI, and every
    project scaffolded that way got CI that died at the first step.
    """
    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: ("", ""))
    result = scaffold_module.scaffold_project(tmp_path, title="Finite Flat")

    assert result.unpinned is True
    assert not (tmp_path / ".github/workflows/autoform-verify.yml").exists()
    assert not (tmp_path / ".github/workflows/blueprint-pages.yml").exists()
    assert not (tmp_path / ".github/autoform_audit.py").exists()
    assert (tmp_path / ".github/CODEOWNERS.autoform.example").is_file()
    assert ".github/autoform_audit.py" in result.skipped
    assert ".github/workflows/autoform-verify.yml" in result.skipped
    # Everything a project needs to be authored still lands.
    assert (tmp_path / "blueprint/roadmap/README.md").is_file()
    assert (tmp_path / "mkdocs.yml").is_file()


def test_unpinned_rerun_reports_the_inert_example_as_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from autoform_cli.__main__ import main

    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: ("", ""))
    args = ["init", str(tmp_path), "--title", "Finite Flat"]
    assert main(args) == 0
    capsys.readouterr()

    assert main(args) == 0
    output = capsys.readouterr().out

    assert "= .github/CODEOWNERS.autoform.example (exists, left alone)" in output
    assert "= .github/autoform_audit.py (no Autoform ref to pin)" in output


def test_a_ref_alone_restores_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit is the unguessable half; the repository has a sane default.

    Setup tells the agent to pass `--autoform-ref`. If the source had to be
    supplied too, following that instruction would still yield no CI, and the
    fail-closed behaviour would be indistinguishable from a broken flag.
    """
    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: ("", ""))
    result = scaffold_module.scaffold_project(tmp_path, title="Finite Flat", autoform_ref="2" * 40)

    assert result.unpinned is False
    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f"AUTOFORM_SOURCE: {json.dumps(scaffold_module.DEFAULT_AUTOFORM_SOURCE)}" in verify
    assert f'AUTOFORM_REF: "{"2" * 40}"' in verify


def test_codeowners_example_names_no_owner_until_a_maintainer_does(tmp_path: Path) -> None:
    """The tool cannot know who maintains a project, so its example is inert."""
    scaffold_project(
        tmp_path,
        title="Finite Flat",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="1" * 40,
    )
    example = tmp_path / ".github/CODEOWNERS.autoform.example"
    lines = example.read_text(encoding="utf-8").splitlines()

    assert all(not line.strip() or line.startswith("#") for line in lines)
    assert lines[-2:] == ["# /blueprint/roadmap/README.md @OWNER", "# /.github/ @OWNER"]
    assert not (tmp_path / ".github/CODEOWNERS").exists()


@pytest.mark.parametrize("existing", [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"])
def test_codeowners_example_never_touches_active_rules(existing: str, tmp_path: Path) -> None:
    (tmp_path / existing).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / existing).write_text("* @existing-owner\n", encoding="utf-8")

    result = scaffold_project(
        tmp_path,
        title="Finite Flat",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="1" * 40,
        force=True,
    )

    assert ".github/CODEOWNERS.autoform.example" in result.written
    assert (tmp_path / existing).read_text(encoding="utf-8") == "* @existing-owner\n"


@pytest.mark.parametrize("ref", ["main", "0f018613", "v1.0.0", "2" * 39, ("2" * 39) + "Z"])
def test_a_mutable_ref_is_refused(ref: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand-supplying a branch is the bug this gate exists to prevent.

    Setup asks the agent to find the commit the plugin came from. An agent that
    answers `main` would pin CI to whatever that branch points at next week,
    which is how projects got a build with no `autoform` command in the first
    place. The scaffold refuses instead of writing CI that rots.
    """
    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: ("", ""))
    with pytest.raises(scaffold_module.ScaffoldError) as caught:
        scaffold_module.scaffold_project(tmp_path, title="Finite Flat", autoform_ref=ref)

    assert "40-character commit sha" in str(caught.value)
    assert not (tmp_path / ".github").exists()
    assert not (tmp_path / "blueprint").exists()


def test_an_explicit_source_overrides_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: ("", ""))
    scaffold_module.scaffold_project(
        tmp_path,
        title="Finite Flat",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="2" * 40,
    )

    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert "AUTOFORM_SOURCE: \"https://example.test/autoform.git\"" in verify
    assert f"AUTOFORM_REF: \"{'2' * 40}\"" in verify
    assert '"git+${AUTOFORM_SOURCE}@${AUTOFORM_REF}"' in verify


@pytest.mark.parametrize(
    "source",
    [
        "https://user@example.test/autoform.git",
        "https://user:secret@example.test/autoform.git",
        "https://example.test:443/autoform.git",
        "https://example.test/autoform.git?",
        "https://example.test/autoform.git?token=secret",
        "https://example.test/autoform.git#",
        "https://example.test/autoform.git#fragment",
        "https://example.test/auto form.git",
        "https://example.test/autoform.git\nrun: pwned",
        "https://example.test/autoform.git\tother",
        "https://example.test/autoform.git\x00tail",
        "https://example.test/autoform.git\x7ftail",
        "https://example.test/%61utoform.git",
        "https://example.test/owner/../autoform.git",
        "https://example.test/${{secrets.TOKEN}}/autoform.git",
        "https://example.test/autoform",
        "https://example.test/autoform.git$(touch pwned)",
        "http://example.test/autoform.git",
        "file:///tmp/autoform.git",
        "git@example.test:owner/autoform.git",
    ],
)
def test_an_unsafe_explicit_source_is_refused_without_persisting_it(
    source: str, tmp_path: Path
) -> None:
    with pytest.raises(ScaffoldError) as caught:
        scaffold_project(
            tmp_path,
            title="Finite Flat",
            autoform_source=source,
            autoform_ref="2" * 40,
        )

    assert "safe credential-free HTTPS Git URL" in str(caught.value)
    assert source not in str(caught.value)
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_an_unsafe_plugin_pin_fails_closed_without_persisting_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_source = "https://token:secret@example.test/autoform.git"
    monkeypatch.setattr(
        scaffold_module,
        "plugin_pin",
        lambda _templates=None: (secret_source, "2" * 40),
    )

    result = scaffold_module.scaffold_project(tmp_path, title="Finite Flat")

    assert result.unpinned is True
    assert (tmp_path / ".github/CODEOWNERS.autoform.example").is_file()
    assert not (tmp_path / ".github/autoform_audit.py").exists()
    assert secret_source not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


def test_plugin_pin_is_empty_outside_a_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scaffold_module, "_git", lambda *args, **kwargs: None)
    monkeypatch.setattr(scaffold_module, "_marketplace_checkout", lambda: None)
    assert scaffold_module.plugin_pin() == ("", "")


def _repository(path: Path, remote: str, *, advertise: bool = True) -> str:
    """Make *path* a real one-commit checkout and return its HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    run = ["git", "-c", "user.email=t@test", "-c", "user.name=Test"]
    subprocess.run([*run, "init", "-q"], cwd=path, check=True)
    subprocess.run([*run, "remote", "add", "origin", remote], cwd=path, check=True)
    subprocess.run([*run, "add", "--all"], cwd=path, check=True)
    subprocess.run([*run, "commit", "-q", "--allow-empty", "-m", "first"], cwd=path, check=True)
    done = subprocess.run(
        [*run, "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    )
    head = done.stdout.strip()
    if advertise:
        subprocess.run(
            [*run, "update-ref", "refs/remotes/origin/main", head], cwd=path, check=True
        )
    return head


def _git_output(path: Path, *args: str) -> str:
    run = ["git", "-c", "user.email=t@test", "-c", "user.name=Test", *args]
    return subprocess.run(run, cwd=path, capture_output=True, text=True, check=True).stdout.strip()


def _autoform_checkout(path: Path, remote: str, *, advertise: bool = True) -> str:
    """Commit a copy of this scaffold and its templates at *path*; return HEAD."""
    (path / "autoform_cli").mkdir(parents=True)
    shutil.copy2(Path(scaffold_module.__file__), path / "autoform_cli" / "scaffold.py")
    shutil.copytree(scaffold_module._TEMPLATES, path / "autoform_cli" / "templates")
    return _repository(path, remote, advertise=advertise)


def _run_from(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(scaffold_module, "_here", lambda: root)
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", root / "autoform_cli" / "templates")


def _fake_plugin_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    """Lay out a plugin cache copy and the real checkout it was copied from."""
    checkout = tmp_path / "src" / "autoform-bot"
    head = _autoform_checkout(checkout, "git@github.com:owner/autoform-bot.git")
    copied = tmp_path / ".claude/plugins/cache/autoform/autoform/0.5.0"
    shutil.copytree(checkout / "autoform_cli", copied / "autoform_cli")
    _run_from(monkeypatch, copied)

    registry = tmp_path / "known_marketplaces.json"
    registry.write_text(
        json.dumps({"autoform": {"installLocation": str(checkout)}}), encoding="utf-8"
    )
    monkeypatch.setattr(scaffold_module, "_PLUGIN_REGISTRY", registry)
    return checkout, copied, head


def test_an_installed_plugin_pins_from_the_marketplace_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy has no `.git`, but the checkout it was copied from does.

    Without this, `init` under a plugin can only fail closed, and the operator
    is asked for a commit that nothing on their machine reports. That is a real
    provenance record, not the guess `plugin_pin` refuses to make.
    """
    _, _, head = _fake_plugin_install(tmp_path, monkeypatch)

    assert scaffold_module.plugin_pin() == (
        "https://github.com/owner/autoform-bot.git",
        head,
    )


def test_plugin_pin_is_empty_when_git_digests_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise ValueError("unsupported hash type")

    _fake_plugin_install(tmp_path, monkeypatch)
    digests = SimpleNamespace(new=unavailable, sha1=unavailable, sha256=unavailable)
    monkeypatch.setattr(scaffold_module, "hashlib", digests)

    assert scaffold_module.plugin_pin() == ("", "")


def test_plugin_pin_omits_a_local_commit_no_remote_tracking_ref_contains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _autoform_checkout(tmp_path / "checkout", "https://example.test/fork.git", advertise=False)
    _run_from(monkeypatch, tmp_path / "checkout")

    assert scaffold_module.plugin_pin() == ("", "")


def test_plugin_pin_ignores_git_replacement_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    original = _autoform_checkout(checkout, "https://example.test/autoform.git")
    renderer = checkout / "autoform_cli" / "scaffold.py"
    renderer.write_bytes(renderer.read_bytes() + b"\n# replacement behavior\n")
    _git_output(checkout, "commit", "-q", "-m", "replacement", "autoform_cli/scaffold.py")
    replacement = _git_output(checkout, "rev-parse", "HEAD")
    replacement_bytes = renderer.read_bytes()
    _git_output(checkout, "checkout", "-q", "--detach", original)
    renderer.write_bytes(replacement_bytes)
    _git_output(checkout, "add", "autoform_cli/scaffold.py")
    _git_output(checkout, "replace", original, replacement)
    for cached in ((), ("--cached",)):
        diff = subprocess.run(["git", "diff", *cached, "--quiet", "HEAD", "--"], cwd=checkout)
        assert diff.returncode == 0
    _run_from(monkeypatch, checkout)

    assert scaffold_module.plugin_pin() == ("", "")


def test_plugin_pin_prefers_canonical_upstream_over_a_containing_fork_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    _autoform_checkout(checkout, "https://example.test/fork.git")
    _git_output(checkout, "commit", "-q", "--allow-empty", "-m", "upstream head")
    head = _git_output(checkout, "rev-parse", "HEAD")
    _git_output(checkout, "update-ref", "refs/remotes/origin/main", head)
    canonical = "https://github.com/facebookresearch/autoform-bot.git"
    _git_output(checkout, "remote", "add", "upstream", canonical)
    _git_output(checkout, "update-ref", "refs/remotes/upstream/main", head)
    _run_from(monkeypatch, checkout)

    assert scaffold_module.plugin_pin() == (scaffold_module.DEFAULT_AUTOFORM_SOURCE, head)


def test_plugin_pin_skips_a_remote_whose_url_is_not_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    head = _autoform_checkout(checkout, "https://example.test/fork.git")
    _git_output(checkout, "remote", "add", "upstream", scaffold_module.DEFAULT_AUTOFORM_SOURCE)
    _git_output(checkout, "update-ref", "refs/remotes/upstream/main", head)
    config = checkout / ".git" / "config"
    config.write_bytes(config.read_bytes().replace(b"example.test/fork", b"example.test/\xff"))
    _run_from(monkeypatch, checkout)

    assert scaffold_module.plugin_pin() == (scaffold_module.DEFAULT_AUTOFORM_SOURCE, head)


@pytest.mark.parametrize(
    "changed",
    [
        "checkout-scaffold",
        "checkout-template",
        "copy-scaffold",
        "copy-template",
        "copy-extra-template",
        "copy-executable-mode",
    ],
)
def test_plugin_pin_fails_closed_when_checkout_or_copy_bytes_do_not_match_head(
    changed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, copied, _ = _fake_plugin_install(tmp_path, monkeypatch)
    selected = checkout if changed.startswith("checkout-") else copied
    if changed == "copy-executable-mode":
        if os.name != "posix":
            pytest.skip("exact POSIX template modes are unavailable")
        (selected / "autoform_cli" / "templates" / "README.md").chmod(0o755)
    if changed.endswith("scaffold"):
        path = selected / "autoform_cli" / "scaffold.py"
        path.write_bytes(path.read_bytes() + b"\n# changed\n")
    elif changed in {"checkout-template", "copy-template"}:
        path = selected / "autoform_cli" / "templates" / "README.md"
        path.write_bytes(path.read_bytes() + b"\nchanged\n")
    elif changed == "copy-extra-template":
        (selected / "autoform_cli" / "templates" / "extra").write_text(
            "different template surface\n", encoding="utf-8"
        )

    assert scaffold_module.plugin_pin() == ("", "")


@pytest.mark.parametrize(
    "change", ["tracked", "ignored-template", "template-executable-mode"]
)
def test_direct_checkout_pin_requires_a_matching_tracked_surface(
    change: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    head = _autoform_checkout(checkout, "https://example.test/autoform.git")
    _run_from(monkeypatch, checkout)
    assert scaffold_module.plugin_pin() == ("https://example.test/autoform.git", head)

    if change == "tracked":
        path = checkout / "autoform_cli" / "scaffold.py"
        path.write_bytes(path.read_bytes() + b"\n# dirty\n")
    elif change == "ignored-template":
        exclude = checkout / ".git" / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as output:
            output.write("\nautoform_cli/templates/ignored-extra\n")
        (checkout / "autoform_cli" / "templates" / "ignored-extra").write_text(
            "ignored by status, but used by the scaffold\n", encoding="utf-8"
        )
        assert scaffold_module._git_checkout_clean(checkout)
    else:
        if os.name != "posix":
            pytest.skip("exact POSIX template modes are unavailable")
        (checkout / "autoform_cli" / "templates" / "README.md").chmod(0o755)

    assert scaffold_module.plugin_pin() == ("", "")


def test_direct_checkout_pin_ignores_unrelated_untracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    head = _autoform_checkout(checkout, "https://example.test/autoform.git")
    (checkout / "large-unrelated-output").write_bytes(b"x" * (1024 * 1024))
    _run_from(monkeypatch, checkout)

    assert scaffold_module.plugin_pin() == ("https://example.test/autoform.git", head)


@pytest.mark.skipif(os.name != "posix", reason="exact POSIX template modes are unavailable")
def test_plugin_pin_accepts_umask_permission_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout, copied, head = _fake_plugin_install(tmp_path, monkeypatch)
    for root in (checkout, copied):
        (root / "autoform_cli" / "scaffold.py").chmod(0o664)
        for template in (root / "autoform_cli" / "templates").rglob("*"):
            if template.is_file():
                template.chmod(0o775 if template.stat().st_mode & stat.S_IXUSR else 0o664)

    assert scaffold_module._git_checkout_clean(checkout)
    assert scaffold_module.plugin_pin() == (
        "https://github.com/owner/autoform-bot.git",
        head,
    )


def test_an_unrelated_marketplace_checkout_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A location that is not Autoform would pin CI to somebody else's repo."""
    checkout, _, _ = _fake_plugin_install(tmp_path, monkeypatch)
    (checkout / "autoform_cli" / "scaffold.py").unlink()

    assert scaffold_module._marketplace_checkout() is None
    assert scaffold_module.plugin_pin() == ("", "")


def test_a_copy_inside_an_unrelated_repository_is_not_its_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git -C` searches upwards, and the answer it finds is confidently wrong.

    Installed into a project's own virtualenv, Autoform sits under that
    project's checkout. Asking for "its" origin and HEAD then describes the
    project, so its CI would be pinned to install the project instead of
    Autoform, at a sha that moves with every commit the author makes.
    """
    project = tmp_path / "their-project"
    _repository(project, "https://github.com/someone/their-project.git")
    installed = project / ".venv/lib/python3.12/site-packages"
    installed.mkdir(parents=True)
    monkeypatch.setattr(scaffold_module, "_here", lambda: installed)
    monkeypatch.setattr(scaffold_module, "_PLUGIN_REGISTRY", tmp_path / "absent.json")

    assert scaffold_module._checkout_root(installed) is None
    assert scaffold_module.plugin_pin() == ("", "")


def test_a_branch_in_the_marketplace_checkout_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the provenance says, only a full sha may reach the workflows."""
    checkout, _, _ = _fake_plugin_install(tmp_path, monkeypatch)

    def fake_git(*args: str, root: Path | None = None) -> str | None:
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return str(checkout)
        return "https://example.test/a.git" if args[0] == "remote" else "main"

    monkeypatch.setattr(scaffold_module, "_git", fake_git)

    assert scaffold_module.plugin_pin() == ("", "")


def test_a_symlinked_subdirectory_cannot_redirect_the_scaffold(tmp_path: Path) -> None:
    """Rejecting a symlinked root is not enough; any component can redirect.

    `project/blueprint` pointing elsewhere sent the whole vault outside the
    project, and --force would have overwritten whatever it found there.
    """
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "blueprint").symlink_to(outside)

    with pytest.raises(scaffold_module.ScaffoldError) as caught:
        scaffold_module.scaffold_project(project, title="Probe")

    assert "outside the project" in str(caught.value)
    assert list(outside.iterdir()) == []


def test_a_dangling_destination_symlink_cannot_redirect_the_scaffold(tmp_path: Path) -> None:
    """`Path.exists()` is false for a link whose outside target is absent."""
    project = tmp_path / "project"
    outside = tmp_path / "outside" / "mkdocs.yml"
    project.mkdir()
    (project / "mkdocs.yml").symlink_to(outside)

    with pytest.raises(scaffold_module.ScaffoldError, match="outside the project"):
        scaffold_module.scaffold_project(project, title="Probe")

    assert not outside.exists()


def test_a_title_with_a_colon_stays_one_yaml_key(tmp_path: Path) -> None:
    """`site_name: Algebra: Foundations` is a nested mapping, not a title."""
    scaffold_module.scaffold_project(tmp_path, title="Algebra: Foundations")

    config = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
    assert 'site_name: "Algebra: Foundations"' in config


def test_a_quoted_title_is_escaped_not_just_wrapped(tmp_path: Path) -> None:
    scaffold_module.scaffold_project(tmp_path, title='The "Hard" Case')

    config = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
    assert 'site_name: "The \\"Hard\\" Case"' in config


def test_a_source_without_a_ref_does_not_borrow_this_checkouts_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sha identifies a commit in one repository, not in any repository.

    Keeping the inferred ref while replacing the source emitted
    `git+other.git@our-sha`, which does not resolve in `other`.
    """
    monkeypatch.setattr(
        scaffold_module,
        "plugin_pin",
        lambda _templates=None: ("https://example.test/ours.git", "1" * 40),
    )
    result = scaffold_module.scaffold_project(
        tmp_path, title="Probe", autoform_source="https://example.test/other.git"
    )

    assert result.unpinned is True
    assert not (tmp_path / ".github/workflows/autoform-verify.yml").exists()


def test_a_source_with_its_own_ref_is_honoured(tmp_path: Path) -> None:
    scaffold_module.scaffold_project(
        tmp_path,
        title="Probe",
        autoform_source="https://example.test/other.git",
        autoform_ref="3" * 40,
    )

    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert 'AUTOFORM_SOURCE: "https://example.test/other.git"' in verify
    assert f'AUTOFORM_REF: "{"3" * 40}"' in verify


def test_control_characters_in_yaml_values_are_escaped(tmp_path: Path) -> None:
    """User text stays one scalar without silently changing its value."""
    title = "safe\n---\nsite_name: pwned\t\x00"
    repository_url = "https://example.test/repo\nextra: value"

    scaffold_project(tmp_path, title=title, repository_url=repository_url)

    config = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
    assert f"site_name: {json.dumps(title)}" in config
    assert f"repo_url: {json.dumps(repository_url)}" in config
    assert "\x00" not in config
    # One key, not two: the apparent keys remain escaped inside their values.
    keys = [line for line in config.splitlines() if line.startswith("site_name:")]
    assert len(keys) == 1
    assert not any(line.strip() == "---" for line in config.splitlines())


def test_an_uppercase_ref_is_accepted(tmp_path: Path) -> None:
    """Git prints shas lowercase but resolves them either way; a sha copied
    from a web UI is valid input rather than a mistake."""
    result = scaffold_project(tmp_path, title="Probe", autoform_ref="A" * 40)

    assert result.unpinned is False
    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f'AUTOFORM_REF: "{"a" * 40}"' in verify
