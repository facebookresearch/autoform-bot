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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoform_cli import approvals
from autoform_cli import scaffold as scaffold_module
from autoform_cli.coverage import load_coverage
from autoform_cli.graph import load_graph
from autoform_cli.scaffold import ScaffoldError, scaffold_project

_EXPECTED = {
    ".github/autoform_audit.py",
    ".github/workflows/autoform-review-gate.yml",
    ".github/workflows/autoform-verify.yml",
    ".github/workflows/blueprint-pages.yml",
    ".gitignore",
    "README.md",
    "blueprint/.gitignore",
    "blueprint/.autoform-review",
    "blueprint/README.md",
    "blueprint/coverage/README.md",
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
        ("theme/main.html", b'{% extends "base.html" %}'),
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
    # A read-back write killed by SIGTERM or SIGKILL leaves its staging file.
    assert ".autoform-readback-*.tmp" in ignored


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


def test_generated_ci_rebuilds_opted_in_statement_review_evidence(tmp_path: Path) -> None:
    scaffold_project(tmp_path, title="Finite Flat", autoform_ref="1" * 40)
    workflows = tmp_path / ".github/workflows"
    verify = (workflows / "autoform-verify.yml").read_text(encoding="utf-8")
    pages = (workflows / "blueprint-pages.yml").read_text(encoding="utf-8")

    for workflow in (verify, pages):
        assert 'marker="blueprint/.autoform-review"' in workflow
        assert "ad00ec55a215821b56f924782e25d7a77fff6696ee23cc465af20476b545b620" in workflow
        assert "review cards or approvals exist without $marker" in workflow
        assert "review_approved[[:space:]]*:' -- blueprint/roadmap" in workflow
        assert "AUTOFORM_REVIEW_ENABLED=true" in workflow
        assert "review prepare" not in workflow and "autoform-review.json" not in workflow
    # One extraction per command: the check derives its own bundle.
    assert "autoform review check blueprint --lean-root .\n" in verify
    # Pages extracts once, in the job that builds Lean, and checks the report.
    report = '"$RUNNER_TEMP/autoform-skeleton/skeleton-report.json"'
    assert f"autoform review check blueprint\n          --skeleton-report {report}\n" in pages
    assert f"review_args=(--review --skeleton-report {report})" in pages
    assert "--review-bundle" not in pages
    assert "Build Lean for statement review" in pages
    assert "--with markdown==3.10.3" in pages
    assert "--with pymdown-extensions==11.0.1" in pages
    assert (tmp_path / "blueprint/.autoform-review").read_text(encoding="utf-8") == (
        "autoform-review-policy/v1\n"
    )


# What a statement-review step mentions: a review or skeleton command, a
# --review flag, the skeleton report's directory or artifact, or Lean set up
# for the review.
_REVIEW_MARKERS = ("autoform review", "autoform skeleton", "--review", "autoform-skeleton", "for statement review")
# The same opt-in, tested inside a script that must run either way. Only the
# branch taken when it holds is exempt; an else or elif branch is read too.
_REVIEW_BRANCH = re.compile(
    r'^if \[\[ "\$\{AUTOFORM_REVIEW_ENABLED:-\}" == true \]\]; then\n.*?^(?=else$|elif |fi$)', re.MULTILINE | re.DOTALL
)


@pytest.mark.parametrize(
    ("name", "gated"),
    [
        ("autoform-verify.yml", "Verify statement reviews"),
        ("blueprint-pages.yml", "Verify statement reviews"),
        ("autoform-review-gate.yml", "Authenticate changed approvals"),
    ],
)
@pytest.mark.parametrize("copy", ["template", "example"])
def test_statement_review_steps_run_only_in_projects_that_opted_in(
    tmp_path: Path, repo_root: Path, copy: str, name: str, gated: str
) -> None:
    """A project without the review marker never extracts a skeleton report,
    and its Lean-mapped articles need no durable article_id, so a review step
    that ran there would fail its CI. Steps are found by what they mention, not
    by name, so a step added later is held to the gate too. Rendering the site
    must run either way, so its script sets its review arguments only in the
    branch that runs when the same test holds."""

    yaml = pytest.importorskip("yaml")
    if copy == "template":
        scaffold_project(tmp_path, title="Finite Flat", autoform_ref="1" * 40)
        workflows = tmp_path / ".github/workflows"
    else:
        workflows = repo_root / "skills/setup/assets/cabannes-thesis-project/.github/workflows"
    jobs = yaml.safe_load((workflows / name).read_text(encoding="utf-8"))["jobs"]
    review = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if any(marker in json.dumps(step) for marker in _REVIEW_MARKERS)
    ]

    assert gated in [step.get("name") for step in review]
    for step in review:
        if step.get("if") == "env.AUTOFORM_REVIEW_ENABLED == 'true'":
            continue
        rest = json.dumps(dict(step, run=_REVIEW_BRANCH.sub("", step.get("run", ""))))
        assert not any(marker in rest for marker in _REVIEW_MARKERS), f"{step.get('name')} runs without the opt-in"


def test_the_approval_gate_runs_apart_from_the_lean_build(tmp_path: Path) -> None:
    """A review event reruns only the gate, and the gate asks about its own
    pull request; the Lean build keeps one run per ref, so a review arriving
    cannot cancel it."""

    scaffold_project(tmp_path, title="Finite Flat", autoform_ref="1" * 40)
    workflows = tmp_path / ".github/workflows"
    verify = (workflows / "autoform-verify.yml").read_text(encoding="utf-8")
    gate = (workflows / "autoform-review-gate.yml").read_text(encoding="utf-8")
    pages = (workflows / "blueprint-pages.yml").read_text(encoding="utf-8")

    assert "pull_request_review" not in verify
    assert "review authenticate" not in verify
    assert "group: autoform-verify-${{ github.ref }}\n" in verify
    assert "github.event_name" not in verify
    assert "pull_request_review:\n    types: [submitted, dismissed]" in gate
    assert "group: autoform-review-gate-${{ github.event.pull_request.number }}" in gate
    assert '--github --pr "$PR_NUMBER"' in gate
    # The base is the first parent of the merge commit checked out, not the event's base.sha, which can lag.
    assert 'base="$(git rev-parse \'HEAD^1\')"' in gate
    assert '--since "$base" --trusted-ref "$base"' in gate
    assert "base.sha }}" not in gate and "BASE_SHA" not in gate
    assert "PR_NUMBER: ${{ github.event.pull_request.number }}" in gate
    assert "pull-requests: read" in gate
    # Pages reads the verify run and builds every push to the default branch, so a change to CODEOWNERS
    # relabels the site. A trigger cannot name the default branch, so every branch triggers.
    assert "actions: read" in pages
    assert '  push:\n    branches: ["**"]\n  pull_request:\n' in pages
    # A pull request's runs never cancel a pending main build, and neither does a newer run of main, such as
    # a re-run of an old one: the pending run may be the only build of the current head.
    assert "  group: blueprint-pages-${{ github.ref }}\n  cancel-in-progress: false\n  queue: max\n" in pages


def test_pages_authenticates_approvals_in_a_job_that_never_builds_the_project(
    tmp_path: Path,
) -> None:
    """Building Lean runs the project's own build code, so the job that reads
    the token, labels approvals, and uploads the site takes only the skeleton
    report from the Lean job, and restores no cache that job could write."""

    scaffold_project(tmp_path, title="Finite Flat", autoform_ref="1" * 40)
    pages = (tmp_path / ".github/workflows/blueprint-pages.yml").read_text(encoding="utf-8")
    jobs = pages.split("\njobs:\n")[1]
    lean = jobs.split("\n  lean:\n")[1].split("\n  build:\n")[0]
    build = jobs.split("\n  build:\n")[1].split("\n  deploy:\n")[0]

    assert "lake build" in lean and "autoform skeleton blueprint --lean-root ." in lean
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in lean
    assert "GITHUB_TOKEN" not in lean and "--authenticate" not in lean
    assert "    needs: [decide, lean]\n" in build
    assert "lake" not in build and "elan" not in build
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in build
    assert "--authenticate github" in build and "fetch-depth: 0" in build
    for permission in ("pull-requests: read", "actions: read", "issues: read"):
        assert permission in build and permission not in lean
    assert pages.count("enable-cache: false") == 2


# Answers ``gh api PATH [--jq FILTER]`` from $GH_STUB/<PATH up to any query,
# with every other character than a letter or digit made _>.json, and records
# each PATH in $GH_STUB/calls. A path with no answer fails, as a failed request.
_GH_STUB = r"""#!/bin/bash
set -u
[ "$1" = api ] || { echo "unexpected: gh $*" >&2; exit 99; }
path=$2
shift 2
filter=.
while [ $# -gt 0 ]; do
  case $1 in
    --jq) filter=$2; shift 2 ;;
    *) echo "unexpected gh argument: $1" >&2; exit 99 ;;
  esac
done
printf '%s\n' "$path" >> "$GH_STUB/calls"
answer="$GH_STUB/$(printf '%s' "${path%%\?*}" | tr -c 'A-Za-z0-9' '_').json"
[ -f "$answer" ] || { echo "gh: HTTP 502 for $path" >&2; exit 1; }
exec jq -r "$filter" "$answer"
"""


def _step(workflow: Path, job: str, name: str) -> str:
    """The script of the step called ``name`` in ``job`` of ``workflow``."""

    yaml = pytest.importorskip("yaml")
    steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"][job]["steps"]
    scripts = [step["run"] for step in steps if step.get("name") == name]
    assert len(scripts) == 1, f"{job} has {len(scripts)} steps called {name!r}"
    return scripts[0]


def _run_step(
    tmp_path: Path,
    script: str,
    answers: dict[str, object],
    *,
    tools: dict[str, str] | None = None,
    cwd: Path | None = None,
    **env: str,
) -> tuple[subprocess.CompletedProcess[str], list[str], dict[str, str]]:
    """Run a step's script with ``gh api`` answering from ``answers``.

    ``tools`` maps other commands the step runs to stand-in bash scripts.
    Returns the finished process, the paths it asked GitHub for, and what it
    wrote to $GITHUB_OUTPUT.
    """

    if shutil.which("jq") is None:
        pytest.skip("the workflow steps filter GitHub's answers with jq")
    stub = tmp_path / "gh-stub"
    stub.mkdir()
    gh = stub / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    for path, answer in answers.items():
        (stub / (re.sub(r"[^A-Za-z0-9]", "_", path) + ".json")).write_text(
            json.dumps(answer, default=_HoursAgo.timestamp), encoding="utf-8"
        )
    for name, body in (tools or {}).items():
        (stub / name).write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        (stub / name).chmod(0o755)
    output = tmp_path / "github-output"
    output.touch()
    done = subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env={
            "PATH": f"{stub}{os.pathsep}{os.path.dirname(shutil.which('jq') or '')}{os.pathsep}/usr/bin:/bin",
            "GH_STUB": str(stub),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REPOSITORY": "owner/project",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls_file = stub / "calls"
    calls = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []
    outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    return done, calls, outputs


_REPOSITORY = "repos/owner/project"
_MAIN = f"{_REPOSITORY}/git/ref/heads/main"


_HEAD_CHECK = "Check that this attempt built the default branch's head"


@pytest.mark.parametrize(
    ("answers", "deploys"),
    [
        ({_REPOSITORY: {"default_branch": "main"}, _MAIN: {"object": {"sha": "a" * 40}}}, True),
        # main has moved on, as when an old run is re-run.
        ({_REPOSITORY: {"default_branch": "main"}, _MAIN: {"object": {"sha": "b" * 40}}}, False),
        # The default branch is not the branch this run built.
        (
            {
                _REPOSITORY: {"default_branch": "trunk"},
                f"{_REPOSITORY}/git/ref/heads/trunk": {"object": {"sha": "b" * 40}},
            },
            False,
        ),
        # A default branch with another name deploys its own head.
        (
            {
                _REPOSITORY: {"default_branch": "trunk"},
                f"{_REPOSITORY}/git/ref/heads/trunk": {"object": {"sha": "a" * 40}},
            },
            True,
        ),
        # A failed lookup, or an answer that names no commit, deploys nothing.
        ({_MAIN: {"object": {"sha": "a" * 40}}}, False),
        ({_REPOSITORY: {"default_branch": "main"}}, False),
        ({_REPOSITORY: {"default_branch": "main"}, _MAIN: {"object": {}}}, False),
    ],
)
def test_pages_deploys_only_a_build_of_the_default_branch_head(
    tmp_path: Path, answers: dict[str, object], deploys: bool
) -> None:
    """The check runs before the deploy, whatever the review settings, so
    nothing but a build of the current head replaces the site."""

    scaffold_project(tmp_path / "project", title="Finite Flat", autoform_ref="1" * 40)
    workflow = tmp_path / "project/.github/workflows/blueprint-pages.yml"
    pages = workflow.read_text(encoding="utf-8")
    deploy = pages.split("\n  deploy:\n")[1]
    assert deploy.index(f"- name: {_HEAD_CHECK}\n") < deploy.index("uses: actions/deploy-pages@")
    assert "      contents: read\n" in deploy.split("    steps:\n")[0]
    # Nothing skips the check or lets the deploy go on after it fails, and the deploy depends on nothing else.
    yaml = pytest.importorskip("yaml")
    job = yaml.safe_load(pages)["jobs"]["deploy"]
    assert job["if"] == "needs.decide.outputs.publish == 'true'"
    steps = {step["name"]: step for step in job["steps"]}
    assert set(steps[_HEAD_CHECK]) == {"name", "env", "run"}
    assert set(steps["Configure GitHub Pages"]) == {"name", "uses"}
    assert set(steps["Deploy"]) == {"name", "id", "uses", "with"}

    done, _, _ = _run_step(
        tmp_path,
        _step(workflow, "deploy", _HEAD_CHECK),
        answers,
        GITHUB_SHA="a" * 40,
        GITHUB_RUN_ATTEMPT="1",
        BUILT_IN_ATTEMPT="1",
    )

    assert (done.returncode == 0) is deploys, done.stderr
    if not deploys:
        assert "::error::" in done.stdout or "HTTP 502" in done.stderr


@pytest.mark.parametrize("built_in", ["1", ""], ids=["earlier-attempt", "no-build-output"])
def test_pages_deploys_only_the_site_its_own_attempt_rendered(tmp_path: Path, built_in: str) -> None:
    """A re-run of the deploy job alone, or of failed jobs after a green build, keeps the earlier
    attempt's build; its artifact holds an older render of the head, which may show a withdrawn
    approval, so it never replaces the site, even while its commit is still the head."""

    yaml = pytest.importorskip("yaml")
    scaffold_project(tmp_path / "project", title="Finite Flat", autoform_ref="1" * 40)
    workflow = tmp_path / "project/.github/workflows/blueprint-pages.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    assert jobs["build"]["outputs"]["attempt"] == "${{ github.run_attempt }}"
    head_check = next(step for step in jobs["deploy"]["steps"] if step.get("name") == _HEAD_CHECK)
    assert head_check["env"]["BUILT_IN_ATTEMPT"] == "${{ needs.build.outputs.attempt }}"
    # Each attempt uploads and deploys only an artifact named for itself, so it never finds an earlier one.
    upload = next(step for step in jobs["build"]["steps"] if step.get("name") == "Upload Pages artifact")
    deploy = next(step for step in jobs["deploy"]["steps"] if step.get("name") == "Deploy")
    assert upload["with"]["name"] == deploy["with"]["artifact_name"] == "github-pages-${{ github.run_attempt }}"

    answers = {_REPOSITORY: {"default_branch": "main"}, _MAIN: {"object": {"sha": "a" * 40}}}
    done, calls, _ = _run_step(
        tmp_path,
        _step(workflow, "deploy", _HEAD_CHECK),
        answers,
        GITHUB_SHA="a" * 40,
        GITHUB_RUN_ATTEMPT="2",
        BUILT_IN_ATTEMPT=built_in,
    )

    assert done.returncode == 1
    assert done.stdout.startswith(f"::error::This is attempt 2, but the site was rendered in attempt {built_in or 'none'}")
    assert calls == []


@pytest.mark.parametrize(
    ("manifest", "unchecked"),
    [
        ({"unchecked_approvals": {"a/b": "HTTP 502", "a/c": "HTTP 502"}}, "2"),
        ({"unchecked_approvals": {}}, "0"),
        # A render without --authenticate checks nothing, so it leaves nothing unchecked.
        ({}, "0"),
    ],
)
def test_pages_fails_after_deploying_a_site_whose_approvals_could_not_be_checked(
    tmp_path: Path, manifest: dict[str, object], unchecked: str
) -> None:
    """The site still deploys, and says which approvals it understates, but the run is not green."""

    yaml = pytest.importorskip("yaml")
    scaffold_project(tmp_path / "project", title="Finite Flat", autoform_ref="1" * 40)
    workflow = tmp_path / "project/.github/workflows/blueprint-pages.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    assert [step["id"] for step in jobs["build"]["steps"] if step.get("name") == "Render the blueprint"] == ["render"]
    assert jobs["build"]["outputs"]["unchecked"] == "${{ steps.render.outputs.unchecked }}"
    name = "Fail when approvals could not be checked"
    names = [step.get("name") for step in jobs["deploy"]["steps"]]
    assert names.index(name) == len(names) - 1 and names[-2] == "Deploy"
    assert jobs["deploy"]["steps"][-1]["if"] == "needs.build.outputs.unchecked != '0'"

    site = tmp_path / "checkout"
    site.mkdir()
    uvx = 'mkdir -p site-src && printf \'%s\' "$MANIFEST" > site-src/publication.json'
    done, _, outputs = _run_step(
        tmp_path,
        _step(workflow, "build", "Render the blueprint"),
        {},
        tools={"uvx": uvx},
        cwd=site,
        MANIFEST=json.dumps(manifest),
        AUTOFORM_SOURCE="https://example.com/autoform.git",
        AUTOFORM_REF="main",
        AUTOFORM_REVIEW_ENABLED="true",
        PUBLISH="true",
        RUNNER_TEMP=str(tmp_path),
    )
    assert done.returncode == 0, done.stderr
    assert outputs == {"unchecked": unchecked}

    failed = subprocess.run(
        ["bash", "-c", _step(workflow, "deploy", name)],
        env={"PATH": "/usr/bin:/bin", "UNCHECKED": "2"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert failed.returncode == 1
    assert failed.stdout.startswith("::error::The site is deployed, but 2 approvals could not be checked")


_HOUR = 3600
_RUNS = f"{_REPOSITORY}/actions/workflows/blueprint-pages.yml/runs"
_DEPLOYMENTS = f"{_REPOSITORY}/deployments"


class _HoursAgo:
    """``hours`` before the step that reads it runs. Timestamps taken at
    collection aged as a slow suite ran: by the time it reached these tests,
    a failure meant to sit half an hour inside its backoff window had left it."""

    def __init__(self, hours: float) -> None:
        self.hours = hours

    def __repr__(self) -> str:
        return f"_HoursAgo({self.hours})"

    def timestamp(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(hours=self.hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _github_after(
    *,
    failed: tuple[tuple[str, float] | tuple[str, float, str], ...] = (),
    deployed: tuple[str, float] | None = None,
    earlier: tuple[tuple[str, float], ...] = (),
    remaining: int = 1000,
    limit: int = 1000,
    head: str = "a" * 40,
) -> dict[str, object]:
    """GitHub's answers when runs of the head failed ``failed`` = ((event, hours ago), ...)
    and its newest deployment ended ``deployed`` = (state, hours ago), the ones
    before it ``earlier``, newest first. A run may name another conclusion
    than failure as a third item."""

    def ago(hours: float) -> _HoursAgo:
        return _HoursAgo(hours)

    runs = [
        {"event": run[0], "conclusion": run[2] if len(run) > 2 else "failure", "updated_at": ago(run[1])}
        for run in failed
    ]
    runs.append({"event": "push", "conclusion": "success", "updated_at": ago(0.1)})
    answers: dict[str, object] = {
        _MAIN: {"object": {"sha": head}},
        _RUNS: {"workflow_runs": runs},
        "rate_limit": {"resources": {"core": {"limit": limit, "remaining": remaining}}},
    }
    deployments = ((deployed,) + earlier) if deployed is not None else ()
    answers[_DEPLOYMENTS] = [{"id": 7 - index} for index in range(len(deployments))]
    for index, (state, hours) in enumerate(deployments):
        answers[f"{_DEPLOYMENTS}/{7 - index}/statuses"] = [{"state": state, "created_at": ago(hours)}]
    return answers


def _decide(
    tmp_path: Path,
    answers: dict[str, object],
    event: str = "schedule",
    *,
    ref: str = "refs/heads/main",
    default_branch: str = "main",
    workflow: str = "blueprint-pages.yml",
):
    scaffold_project(tmp_path / "project", title="Finite Flat", autoform_ref="1" * 40)
    script = _step(tmp_path / "project/.github/workflows/blueprint-pages.yml", "decide", "Decide whether to build")
    # A scheduled run's payload names no repository.
    payload = tmp_path / "event.json"
    payload.write_text(
        json.dumps({} if event == "schedule" else {"repository": {"default_branch": default_branch}}),
        encoding="utf-8",
    )
    return _run_step(
        tmp_path,
        script,
        answers,
        GITHUB_EVENT_NAME=event,
        GITHUB_EVENT_PATH=str(payload),
        GITHUB_REF=ref,
        GITHUB_SHA="a" * 40,
        GITHUB_WORKFLOW_REF=f"owner/project/.github/workflows/{workflow}@{ref}",
    )


def test_pages_runs_every_hour_and_builds_only_what_decide_asks_for(tmp_path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    scaffold_project(tmp_path, title="Finite Flat", autoform_ref="1" * 40)
    pages = yaml.safe_load((tmp_path / ".github/workflows/blueprint-pages.yml").read_text(encoding="utf-8"))
    # YAML 1.1 reads the key "on" as true.
    assert pages[True]["schedule"] == [{"cron": "23 * * * *"}]
    decide = pages["jobs"]["decide"]
    assert decide["permissions"] == {"contents": "read", "actions": "read", "deployments": "read"}
    assert decide["outputs"] == {
        "build": "${{ steps.decide.outputs.build }}",
        "publish": "${{ steps.decide.outputs.publish }}",
    }
    assert pages["jobs"]["lean"]["needs"] == "decide"
    assert pages["jobs"]["lean"]["if"] == "needs.decide.outputs.build == 'true'"
    # The build and deploy jobs need the lean job, so they skip with it.
    assert pages["jobs"]["build"]["needs"] == ["decide", "lean"]
    assert pages["jobs"]["deploy"]["needs"] == ["decide", "build"]


def test_pages_publishes_only_builds_of_the_default_branch_whatever_its_name(tmp_path: Path) -> None:
    """A repository whose default branch is not main once built every hour and never deployed:
    the schedule runs on the default branch, but uploading and deploying asked for main."""

    yaml = pytest.importorskip("yaml")
    scaffold_project(tmp_path, title="Finite Flat", autoform_ref="1" * 40)
    text = (tmp_path / ".github/workflows/blueprint-pages.yml").read_text(encoding="utf-8")
    assert "refs/heads/main" not in text and "[main]" not in text
    jobs = yaml.safe_load(text)["jobs"]
    # A push to any other branch starts no runner: GitHub evaluates the condition before queuing the job.
    assert jobs["decide"]["if"] == (
        "github.event_name != 'push' || github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    )
    upload = next(step for step in jobs["build"]["steps"] if step.get("name") == "Upload Pages artifact")
    assert upload["if"] == jobs["deploy"]["if"] == "needs.decide.outputs.publish == 'true'"
    render = next(step for step in jobs["build"]["steps"] if step.get("name") == "Render the blueprint")
    assert render["env"]["PUBLISH"] == "${{ needs.decide.outputs.publish }}"


@pytest.mark.parametrize(
    ("event", "ref", "publish"),
    [
        ("push", "refs/heads/trunk", "true"),
        ("workflow_dispatch", "refs/heads/trunk", "true"),
        ("workflow_dispatch", "refs/heads/feature", "false"),
        ("workflow_dispatch", "refs/heads/main", "false"),
        ("pull_request", "refs/pull/3/merge", "false"),
    ],
)
def test_decide_publishes_a_build_only_of_the_default_branch(
    tmp_path: Path, event: str, ref: str, publish: str
) -> None:
    done, calls, outputs = _decide(tmp_path, {}, event, ref=ref, default_branch="trunk")

    assert done.returncode == 0, done.stderr
    assert (outputs, calls) == ({"publish": publish, "build": "true"}, [])


def test_a_scheduled_run_publishes_on_a_default_branch_not_called_main(tmp_path: Path) -> None:
    answers = {path.replace("heads/main", "heads/trunk"): answer for path, answer in _github_after().items()}

    done, calls, outputs = _decide(tmp_path, answers, ref="refs/heads/trunk")

    assert done.returncode == 0, done.stderr
    assert outputs == {"publish": "true", "build": "true"}
    assert calls[1] == f"{_REPOSITORY}/git/ref/heads/trunk"
    assert calls[-1].startswith(f"{_RUNS}?branch=trunk&")


def test_a_scheduled_run_asks_for_the_runs_of_its_own_workflow_file(tmp_path: Path) -> None:
    """A copy of the workflow under another name would ask for runs of a file
    that does not exist, and never build."""

    answers = {path.replace("blueprint-pages.yml", "pages.yml"): answer for path, answer in _github_after().items()}

    done, calls, outputs = _decide(tmp_path, answers, workflow="pages.yml")

    assert done.returncode == 0, done.stderr
    assert outputs == {"publish": "true", "build": "true"}
    assert calls[-1].startswith(f"{_REPOSITORY}/actions/workflows/pages.yml/runs?branch=main&")


@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
def test_every_event_but_the_schedule_builds_without_asking_github(tmp_path: Path, event: str) -> None:
    done, calls, outputs = _decide(tmp_path, {}, event)

    assert done.returncode == 0, done.stderr
    assert (outputs, calls) == ({"publish": "false" if event == "pull_request" else "true", "build": "true"}, [])


@pytest.mark.parametrize(
    ("answers", "build"),
    [
        pytest.param(_github_after(), True, id="never-built"),
        pytest.param(_github_after(deployed=("success", 1)), False, id="complete"),
        pytest.param(_github_after(deployed=("success", 25)), True, id="a-day-old"),
        pytest.param(_github_after(deployed=("success", 25), remaining=899), False, id="a-day-old-short-of-requests"),
        pytest.param(_github_after(deployed=("failure", 1)), True, id="deploy-failed"),
        pytest.param(_github_after(deployed=("success", 3), failed=(("push", 2),)), True, id="failed-after-deploying"),
        pytest.param(
            _github_after(deployed=("success", 3), failed=(("push", 2),), remaining=10),
            False,
            id="failed-after-deploying-short-of-requests",
        ),
        pytest.param(_github_after(deployed=("success", 1), failed=(("push", 2),)), False, id="failed-before"),
        pytest.param(_github_after(deployed=("success", 3), failed=(("pull_request", 2),)), False, id="pull-request"),
        # Three failures wait 4h after the last.
        pytest.param(_github_after(failed=(("push", 5), ("schedule", 4), ("schedule", 3))), False, id="backing-off"),
        pytest.param(_github_after(failed=(("push", 7), ("schedule", 6), ("schedule", 5))), True, id="backed-off"),
        # However many failures, a day at most.
        pytest.param(_github_after(failed=(("schedule", 23),) * 10), False, id="backing-off-a-day"),
        pytest.param(_github_after(failed=(("schedule", 25),) * 10), True, id="backed-off-a-day"),
        # Failures the site recovered from before its last deployment never lengthen the wait.
        pytest.param(
            _github_after(deployed=("success", 25), failed=(("push", 30),) * 5 + (("schedule", 2),)),
            True,
            id="recovered-failures-uncounted",
        ),
        pytest.param(
            _github_after(deployed=("success", 25), failed=(("push", 30),) * 5 + (("schedule", 0.5),)),
            False,
            id="backing-off-after-recovering",
        ),
        # A deploy job that deploys and then fails, say on unchecked approvals, leaves a failed deployment:
        # the failures before the last successful one still never count, and each red deploy since does.
        *(
            pytest.param(
                _github_after(
                    deployed=("failure", hours),
                    earlier=(("success", 25),),
                    failed=(("push", 30),) * 5 + (("schedule", hours),),
                ),
                True,
                id=f"red-deploy-after-recovering-{hours}h",
            )
            for hours in (1.1, 2, 12, 23)
        ),
        pytest.param(
            _github_after(
                deployed=("failure", 1.5),
                earlier=(("failure", 3.5), ("success", 25)),
                failed=(("push", 30),) * 5 + (("schedule", 3.5), ("schedule", 1.5)),
            ),
            False,
            id="red-deploys-backing-off",
        ),
        pytest.param(
            _github_after(
                deployed=("failure", 2.5),
                earlier=(("failure", 4.5), ("success", 25)),
                failed=(("push", 30),) * 5 + (("schedule", 4.5), ("schedule", 2.5)),
            ),
            True,
            id="red-deploys-backed-off",
        ),
        # Six failed deployments and no successful one among them: every failure counts.
        pytest.param(
            _github_after(deployed=("failure", 23), earlier=(("failure", 24),) * 5, failed=(("schedule", 23),) * 6),
            False,
            id="six-red-deploys",
        ),
        # A build starts only while at most 100 of the hour's requests are spent.
        pytest.param(_github_after(remaining=900), True, id="full-allowance"),
        pytest.param(_github_after(remaining=899), False, id="spent-hour"),
        pytest.param(_github_after(remaining=14900, limit=15000), True, id="enterprise-full-allowance"),
        pytest.param(_github_after(remaining=14899, limit=15000), False, id="enterprise-spent-hour"),
        pytest.param(_github_after(failed=(("schedule", 0.5, "timed_out"),)), False, id="timed-out"),
        pytest.param(_github_after(failed=(("push", 0.5, "startup_failure"),)), False, id="startup-failure"),
        pytest.param(_github_after(failed=(("push", 0.5, "cancelled"),)), True, id="cancelled"),
        pytest.param(_github_after(deployed=("error", 1)), True, id="deploy-error"),
        pytest.param(_github_after(deployed=("in_progress", 1)), True, id="deploy-in-progress"),
        pytest.param(_github_after(deployed=("inactive", 1)), True, id="deploy-inactive"),
        # 3600 << 52 is negative in bash, and 3600 << 63 is 0: the shift is capped, so the wait stays a day.
        pytest.param(_github_after(failed=(("schedule", 23),) * 53), False, id="backing-off-a-day-53"),
        pytest.param(_github_after(failed=(("schedule", 23),) * 64), False, id="backing-off-a-day-64"),
    ],
)
def test_a_scheduled_run_builds_the_head_until_it_has_a_complete_build(
    tmp_path: Path, answers: dict[str, object], build: bool
) -> None:
    """Without it, nothing builds the head again after a failed run, and a
    dismissed review, which starts no run, never reaches the site."""

    done, calls, outputs = _decide(tmp_path, answers)

    assert done.returncode == 0, done.stderr
    assert outputs == {"publish": "true", "build": "true" if build else "false"}
    head = "a" * 40
    # GET /rate_limit costs nothing, and is asked first, so a spent hour makes no request fail.
    assert calls[0] == "rate_limit"
    hour = answers["rate_limit"]["resources"]["core"]
    if hour["remaining"] < hour["limit"] - 100:
        assert calls == ["rate_limit"]
        assert done.stdout.startswith(f"::notice::Only {hour['remaining']} of the hour's {hour['limit']} GitHub API")
        return
    # Each deployment's newest status, newest first, up to the first that succeeded.
    statuses = []
    for deployment in answers[_DEPLOYMENTS]:  # type: ignore[attr-defined]
        statuses.append(f"{_DEPLOYMENTS}/{deployment['id']}/statuses?per_page=1")
        if answers[f"{_DEPLOYMENTS}/{deployment['id']}/statuses"][0]["state"] == "success":  # type: ignore[index]
            break
    assert calls[1:] == [
        _MAIN,
        f"{_DEPLOYMENTS}?environment=github-pages&sha={head}&per_page=6",
        *statuses,
        f"{_RUNS}?branch=main&head_sha={head}&status=completed&per_page=100",
    ]
    assert ("::notice::Building" in done.stdout) is build


def test_a_scheduled_run_builds_only_when_the_verifier_would_have_its_whole_allowance(tmp_path: Path) -> None:
    """The verifier has its whole ceiling while at most _RESERVED_REQUESTS - _LEFT_AFTER are spent when it
    begins. Decide starts a build only with 50 of those to spare, for its own requests and the other runs
    of the hour during the Lean build, so a request or two elsewhere never turns a run past the ceiling red."""

    scaffold_project(tmp_path, title="Finite Flat", autoform_ref="1" * 40)
    script = _step(tmp_path / ".github/workflows/blueprint-pages.yml", "decide", "Decide whether to build")

    assert "if (( remaining < limit - 100 )); then" in script
    assert approvals._RESERVED_REQUESTS - approvals._LEFT_AFTER - 100 == 50


def test_the_docs_state_how_few_spent_requests_hold_the_schedule_back(tmp_path: Path, repo_root: Path) -> None:
    """A reviewer reads in the skill and the README when a withdrawal can wait past a day."""

    scaffold_project(tmp_path, title="Finite Flat", autoform_ref="1" * 40)
    script = _step(tmp_path / ".github/workflows/blueprint-pages.yml", "decide", "Decide whether to build")
    spent = re.search(r"if \(\( remaining < limit - (\d+) \)\); then", script)
    assert spent is not None
    readme, skill = (
        " ".join((repo_root / path).read_text(encoding="utf-8").split())
        for path in ("autoform_cli/README.md", "skills/human-review/SKILL.md")
    )

    assert f"other runs keep more than {spent[1]} of each hour's API requests spent" in readme
    assert f"builds only while at most {spent[1]} of the hour's API requests are spent" in skill


def test_a_scheduled_run_of_a_head_main_has_moved_past_builds_nothing(tmp_path: Path) -> None:
    done, calls, outputs = _decide(tmp_path, _github_after(head="b" * 40))

    assert done.returncode == 0, done.stderr
    assert (outputs, calls) == ({"publish": "true", "build": "false"}, ["rate_limit", _MAIN])


@pytest.mark.parametrize(
    "broken",
    [
        {"rate_limit": None},
        {"rate_limit": {"resources": {"core": {}}}},
        {_MAIN: None},
        {_DEPLOYMENTS: None},
        # Never put into a path: the stub answers it, as GitHub might.
        {_DEPLOYMENTS: [{"id": "7 8"}], f"{_DEPLOYMENTS}/7 8/statuses": [{"state": "success"}]},
        {f"{_DEPLOYMENTS}/7/statuses": None},
        {_RUNS: None},
        {_RUNS: {"workflow_runs": [{"event": "push", "conclusion": "failure", "updated_at": "soon"}]}},
    ],
    ids=[
        "failed-rate-limit",
        "no-remaining-count",
        "failed-head",
        "failed-deployments",
        "malformed-deployment",
        "failed-statuses",
        "failed-runs",
        "malformed-runs",
    ],
)
def test_a_scheduled_run_that_cannot_decide_builds_nothing_and_warns(tmp_path: Path, broken: dict[str, object]) -> None:
    """A red run would count as a failed run of the head, and lengthen the wait
    for its next build, or end a complete build's day early."""

    answers = {**_github_after(deployed=("success", 1)), **broken}
    answers = {path: answer for path, answer in answers.items() if answer is not None}

    done, calls, outputs = _decide(tmp_path, answers)

    assert done.returncode == 0, done.stderr
    assert outputs == {"publish": "true", "build": "false"}
    assert done.stdout.startswith("::warning::GitHub did not say ")
    assert done.stdout.endswith(", so this run builds nothing; the next scheduled run asks again\n")
    assert not any("/7 8/" in call for call in calls)


@pytest.mark.parametrize("merged", [True, False], ids=["merge-commit", "linear"])
def test_the_gate_takes_its_base_only_from_the_merge_commit_of_the_pull_request(tmp_path: Path, merged: bool) -> None:
    """In any other checkout HEAD^1 is just the commit before, which would make the gate trust the wrong base."""

    scaffold_project(tmp_path / "project", title="Finite Flat", autoform_ref="1" * 40)
    script = _step(
        tmp_path / "project/.github/workflows/autoform-review-gate.yml", "authenticate", "Authenticate changed approvals"
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    identity = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com"}
    identity |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=checkout, env={**os.environ, **identity}, capture_output=True, text=True, check=True
        )
        return done.stdout.strip()

    git("init", "--quiet", "--initial-branch=main")
    git("commit", "--quiet", "--allow-empty", "-m", "Start")
    git("checkout", "--quiet", "-b", "topic")
    git("commit", "--quiet", "--allow-empty", "-m", "Change")
    if merged:
        git("checkout", "--quiet", "main")
        git("commit", "--quiet", "--allow-empty", "-m", "Move main")
        git("merge", "--quiet", "--no-ff", "--no-edit", "topic")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "uvx").write_text('#!/bin/bash\nprintf \'%s\\n\' "$@" > "$UVX_ARGS"\n', encoding="utf-8")
    (stub / "uvx").chmod(0o755)
    called = tmp_path / "uvx-args"

    done = subprocess.run(
        ["bash", "-c", script],
        cwd=checkout,
        env={
            "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "PR_NUMBER": "7",
            "AUTOFORM_SOURCE": "https://example.com/autoform.git",
            "AUTOFORM_REF": "0" * 40,
            "UVX_ARGS": str(called),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    if merged:
        base = git("rev-parse", "HEAD^1")
        assert done.returncode == 0, done.stderr
        assert called.read_text(encoding="utf-8").splitlines() == [
            "--from", f"git+https://example.com/autoform.git@{'0' * 40}", "autoform", "review", "authenticate",
            "blueprint", "--github", "--pr", "7", "--since", base, "--trusted-ref", base,
        ]
    else:
        assert done.returncode == 2
        assert "error: the checkout is not the merge commit of #7, so its base is unknown" in done.stderr
        assert not called.exists()

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
    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: ("", ""))
    result = scaffold_module.scaffold_project(tmp_path, title="Finite Flat")

    assert result.unpinned is True
    assert not (tmp_path / ".github/workflows/autoform-verify.yml").exists()
    assert not (tmp_path / ".github/workflows/blueprint-pages.yml").exists()
    assert not (tmp_path / ".github/autoform_audit.py").exists()
    assert ".github/autoform_audit.py" in result.skipped
    assert ".github/workflows/autoform-verify.yml" in result.skipped
    # Everything a project needs to be authored still lands.
    assert (tmp_path / "blueprint/roadmap/README.md").is_file()
    assert (tmp_path / "mkdocs.yml").is_file()


def test_a_ref_alone_restores_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit is the unguessable half; the repository has a sane default.

    Setup tells the agent to pass `--autoform-ref`. If the source had to be
    supplied too, following that instruction would still yield no CI, and the
    fail-closed behaviour would be indistinguishable from a broken flag.
    """
    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: ("", ""))
    result = scaffold_module.scaffold_project(tmp_path, title="Finite Flat", autoform_ref="2" * 40)

    assert result.unpinned is False
    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f"AUTOFORM_SOURCE: {json.dumps(scaffold_module.DEFAULT_AUTOFORM_SOURCE)}" in verify
    assert f'AUTOFORM_REF: "{"2" * 40}"' in verify


@pytest.mark.parametrize("ref", ["main", "0f018613", "v1.0.0", "2" * 39, ("2" * 39) + "Z"])
def test_a_mutable_ref_is_refused(ref: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand-supplying a branch is the bug this gate exists to prevent.

    Setup asks the agent to find the commit the plugin came from. An agent that
    answers `main` would pin CI to whatever that branch points at next week,
    which is how projects got a build with no `autoform` command in the first
    place. The scaffold refuses instead of writing CI that rots.
    """
    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda _templates=None: ("", ""))
    with pytest.raises(scaffold_module.ScaffoldError) as caught:
        scaffold_module.scaffold_project(tmp_path, title="Finite Flat", autoform_ref=ref)

    assert "40-character commit sha" in str(caught.value)
    assert not (tmp_path / ".github").exists()
    assert not (tmp_path / "blueprint").exists()


def test_an_explicit_source_overrides_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import scaffold as scaffold_module

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
    from autoform_cli import scaffold as scaffold_module

    secret_source = "https://token:secret@example.test/autoform.git"
    monkeypatch.setattr(
        scaffold_module,
        "plugin_pin",
        lambda _templates=None: (secret_source, "2" * 40),
    )

    result = scaffold_module.scaffold_project(tmp_path, title="Finite Flat")

    assert result.unpinned is True
    assert not (tmp_path / ".github").exists()
    assert secret_source not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


def test_plugin_pin_is_empty_outside_a_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoform_cli import scaffold as scaffold_module

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


def _fake_plugin_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, str]:
    """Lay out a plugin cache copy and the real checkout it was copied from."""
    from autoform_cli import scaffold as scaffold_module

    checkout = tmp_path / "src" / "autoform-bot"
    (checkout / "autoform_cli").mkdir(parents=True)
    shutil.copy2(Path(scaffold_module.__file__), checkout / "autoform_cli" / "scaffold.py")
    shutil.copytree(scaffold_module._TEMPLATES, checkout / "autoform_cli" / "templates")
    head = _repository(checkout, "git@github.com:owner/autoform-bot.git")

    copied = tmp_path / ".claude/plugins/cache/autoform/autoform/0.5.0"
    (copied / "autoform_cli").mkdir(parents=True)
    shutil.copy2(
        checkout / "autoform_cli" / "scaffold.py",
        copied / "autoform_cli" / "scaffold.py",
    )
    shutil.copytree(
        checkout / "autoform_cli" / "templates",
        copied / "autoform_cli" / "templates",
    )
    monkeypatch.setattr(scaffold_module, "_here", lambda: copied)
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", copied / "autoform_cli" / "templates")

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
    from autoform_cli import scaffold as scaffold_module

    _, _, head = _fake_plugin_install(tmp_path, monkeypatch)

    assert scaffold_module.plugin_pin() == (
        "https://github.com/owner/autoform-bot.git",
        head,
    )


def test_plugin_pin_omits_a_local_commit_no_remote_tracking_ref_contains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import scaffold as scaffold_module

    checkout = tmp_path / "checkout"
    (checkout / "autoform_cli").mkdir(parents=True)
    shutil.copy2(Path(scaffold_module.__file__), checkout / "autoform_cli" / "scaffold.py")
    shutil.copytree(scaffold_module._TEMPLATES, checkout / "autoform_cli" / "templates")
    _repository(checkout, "https://example.test/fork.git", advertise=False)
    monkeypatch.setattr(scaffold_module, "_here", lambda: checkout)
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", checkout / "autoform_cli" / "templates")

    assert scaffold_module.plugin_pin() == ("", "")


def test_plugin_pin_ignores_git_replacement_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import scaffold as scaffold_module

    checkout = tmp_path / "checkout"
    (checkout / "autoform_cli").mkdir(parents=True)
    shutil.copy2(Path(scaffold_module.__file__), checkout / "autoform_cli" / "scaffold.py")
    shutil.copytree(scaffold_module._TEMPLATES, checkout / "autoform_cli" / "templates")
    original = _repository(checkout, "https://example.test/autoform.git")
    run = ["git", "-c", "user.email=t@test", "-c", "user.name=Test"]
    renderer = checkout / "autoform_cli" / "scaffold.py"
    renderer.write_bytes(renderer.read_bytes() + b"\n# replacement behavior\n")
    subprocess.run([*run, "add", "autoform_cli/scaffold.py"], cwd=checkout, check=True)
    subprocess.run([*run, "commit", "-q", "-m", "replacement"], cwd=checkout, check=True)
    replacement = subprocess.run(
        [*run, "rev-parse", "HEAD"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    replacement_bytes = renderer.read_bytes()
    subprocess.run([*run, "checkout", "-q", "--detach", original], cwd=checkout, check=True)
    renderer.write_bytes(replacement_bytes)
    subprocess.run([*run, "add", "autoform_cli/scaffold.py"], cwd=checkout, check=True)
    subprocess.run([*run, "replace", original, replacement], cwd=checkout, check=True)
    assert subprocess.run([*run, "diff", "--quiet", "HEAD", "--"], cwd=checkout).returncode == 0
    assert (
        subprocess.run([*run, "diff", "--cached", "--quiet", "HEAD", "--"], cwd=checkout).returncode
        == 0
    )
    monkeypatch.setattr(scaffold_module, "_here", lambda: checkout)
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", checkout / "autoform_cli" / "templates")

    assert scaffold_module.plugin_pin() == ("", "")


def test_plugin_pin_prefers_canonical_upstream_over_a_containing_fork_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import scaffold as scaffold_module

    checkout = tmp_path / "checkout"
    (checkout / "autoform_cli").mkdir(parents=True)
    shutil.copy2(Path(scaffold_module.__file__), checkout / "autoform_cli" / "scaffold.py")
    shutil.copytree(scaffold_module._TEMPLATES, checkout / "autoform_cli" / "templates")
    _repository(checkout, "https://example.test/fork.git")
    run = ["git", "-c", "user.email=t@test", "-c", "user.name=Test"]
    subprocess.run(
        [*run, "commit", "-q", "--allow-empty", "-m", "upstream head"],
        cwd=checkout,
        check=True,
    )
    head = subprocess.run(
        [*run, "rev-parse", "HEAD"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [*run, "update-ref", "refs/remotes/origin/main", head], cwd=checkout, check=True
    )
    subprocess.run(
        [
            *run,
            "remote",
            "add",
            "upstream",
            "https://github.com/facebookresearch/autoform-bot.git",
        ],
        cwd=checkout,
        check=True,
    )
    subprocess.run(
        [*run, "update-ref", "refs/remotes/upstream/main", head], cwd=checkout, check=True
    )
    monkeypatch.setattr(scaffold_module, "_here", lambda: checkout)
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", checkout / "autoform_cli" / "templates")

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
    from autoform_cli import scaffold as scaffold_module

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
    from autoform_cli import scaffold as scaffold_module

    checkout = tmp_path / "checkout"
    (checkout / "autoform_cli").mkdir(parents=True)
    shutil.copy2(Path(scaffold_module.__file__), checkout / "autoform_cli" / "scaffold.py")
    shutil.copytree(scaffold_module._TEMPLATES, checkout / "autoform_cli" / "templates")
    head = _repository(checkout, "https://example.test/autoform.git")
    monkeypatch.setattr(scaffold_module, "_here", lambda: checkout)
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", checkout / "autoform_cli" / "templates")
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
    from autoform_cli import scaffold as scaffold_module

    checkout = tmp_path / "checkout"
    (checkout / "autoform_cli").mkdir(parents=True)
    shutil.copy2(Path(scaffold_module.__file__), checkout / "autoform_cli" / "scaffold.py")
    shutil.copytree(scaffold_module._TEMPLATES, checkout / "autoform_cli" / "templates")
    head = _repository(checkout, "https://example.test/autoform.git")
    (checkout / "large-unrelated-output").write_bytes(b"x" * (1024 * 1024))
    monkeypatch.setattr(scaffold_module, "_here", lambda: checkout)
    monkeypatch.setattr(scaffold_module, "_TEMPLATES", checkout / "autoform_cli" / "templates")

    assert scaffold_module.plugin_pin() == ("https://example.test/autoform.git", head)


@pytest.mark.skipif(os.name != "posix", reason="exact POSIX template modes are unavailable")
def test_plugin_pin_accepts_umask_permission_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import scaffold as scaffold_module

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
    from autoform_cli import scaffold as scaffold_module

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
    from autoform_cli import scaffold as scaffold_module

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
    from autoform_cli import scaffold as scaffold_module

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
    from autoform_cli import scaffold as scaffold_module

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
    from autoform_cli import scaffold as scaffold_module

    project = tmp_path / "project"
    outside = tmp_path / "outside" / "mkdocs.yml"
    project.mkdir()
    (project / "mkdocs.yml").symlink_to(outside)

    with pytest.raises(scaffold_module.ScaffoldError, match="outside the project"):
        scaffold_module.scaffold_project(project, title="Probe")

    assert not outside.exists()


def test_a_title_with_a_colon_stays_one_yaml_key(tmp_path: Path) -> None:
    """`site_name: Algebra: Foundations` is a nested mapping, not a title."""
    from autoform_cli import scaffold as scaffold_module

    scaffold_module.scaffold_project(tmp_path, title="Algebra: Foundations")

    config = (tmp_path / "mkdocs.yml").read_text(encoding="utf-8")
    assert 'site_name: "Algebra: Foundations"' in config


def test_a_quoted_title_is_escaped_not_just_wrapped(tmp_path: Path) -> None:
    from autoform_cli import scaffold as scaffold_module

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
    from autoform_cli import scaffold as scaffold_module

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
    from autoform_cli import scaffold as scaffold_module

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
