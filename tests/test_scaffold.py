"""The internal vault layout is fixed, so the tool writes it rather than describing it.

A real project came back from an agent-driven setup with chapter pages as
siblings of their directories instead of ``<chapter>/README.md``. That parses
clean and publishes a book with no chapters, so these tests pin the shape.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest

from autoform_cli import scaffold as scaffold_module
from autoform_cli.coverage import load_coverage
from autoform_cli.graph import load_graph
from autoform_cli.scaffold import ScaffoldError, scaffold_project

_EXPECTED = {
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


@pytest.fixture(autouse=True)
def _disable_network_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scaffold unit tests opt into a pin explicitly; verifier tests own I/O."""

    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda: ("", ""))


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
        autoform_ref="0" * 40,
    )

    assert ".github/autoform_audit.py" in result.written
    assert not (project / ".github/__pycache__").exists()
    assert all("__pycache__" not in path and not path.endswith(".pyc") for path in result.written)


def test_scaffold_writes_the_whole_vault(tmp_path: Path) -> None:
    result = scaffold_project(
        tmp_path,
        title="Finite Flat",
        repository_url="https://example.test/repo",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="0" * 40,
    )

    assert set(result.written) == _EXPECTED
    assert result.skipped == ()
    for relative in _EXPECTED:
        assert (tmp_path / relative).is_file(), relative


def test_init_does_not_create_a_lean_project_shell(tmp_path: Path) -> None:
    scaffold_project(tmp_path, title="Finite Flat")

    assert not (tmp_path / "lakefile.toml").exists()
    assert not (tmp_path / "lean-toolchain").exists()
    assert not (tmp_path / "src/FiniteFlat.lean").exists()


def test_legacy_init_refuses_a_manifest_managed_workspace(tmp_path: Path) -> None:
    (tmp_path / ".autoform.toml").write_text(
        'schema = "autoform-workspace/v1"\n', encoding="utf-8"
    )

    with pytest.raises(ScaffoldError, match="legacy single-vault"):
        scaffold_project(tmp_path, title="Finite Flat", force=True)

    assert not (tmp_path / "blueprint").exists()


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
    assert "python .github/autoform_audit.py" in verify
    assert '"$AUTOFORM_ROOT_PACKAGE" "$archive" blueprint . "$probe"' in verify
    assert "if [[ -f .autoform.toml ]]" in verify
    assert "autoform workspace check . --lean-root ." in verify
    assert "autoform check blueprint --lean-root ." in verify


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
        "autoform_ref": "0" * 40,
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

    assert main(
        [
            "init",
            str(tmp_path),
            "--title",
            "Finite Flat",
            "--autoform-source",
            "https://example.test/autoform.git",
            "--autoform-ref",
            "0" * 40,
            "--json",
        ]
    ) == 0
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


def test_generated_ci_uses_the_verified_plugin_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "https://example.test/autoform.git"
    ref = "4" * 40
    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda: (source, ref))
    scaffold_project(tmp_path, title="Finite Flat")
    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")

    assert f"AUTOFORM_SOURCE: {json.dumps(source)}" in verify
    assert f"AUTOFORM_REF: {json.dumps(ref)}" in verify
    assert '"git+${AUTOFORM_SOURCE}@${AUTOFORM_REF}"' in verify
    assert re.fullmatch(r"[0-9a-f]{40}", ref), "the pin must be an immutable commit"
    assert "@main" not in verify


def test_explicit_pin_skips_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> tuple[str, str]:
        raise AssertionError("explicit provenance invoked discovery")

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
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

    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda: ("", ""))
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


def test_a_ref_alone_is_refused_without_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden() -> tuple[str, str]:
        raise AssertionError("partial explicit provenance invoked discovery")

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    with pytest.raises(ScaffoldError, match="must be provided together"):
        scaffold_project(tmp_path, title="Finite Flat", autoform_ref="2" * 40)

    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("ref", ["main", "0f018613", "v1.0.0", "2" * 39, ("2" * 39) + "Z"])
def test_a_mutable_ref_is_refused(ref: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand-supplying a branch is the bug this gate exists to prevent.

    Setup asks the agent to find the commit the plugin came from. An agent that
    answers `main` would pin CI to whatever that branch points at next week,
    which is how projects got a build with no `autoform` command in the first
    place. The scaffold refuses instead of writing CI that rots.
    """
    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda: ("", ""))
    with pytest.raises(scaffold_module.ScaffoldError) as caught:
        scaffold_module.scaffold_project(tmp_path, title="Finite Flat", autoform_ref=ref)

    assert "40-character commit sha" in str(caught.value)
    assert not (tmp_path / ".github").exists()
    assert not (tmp_path / "blueprint").exists()


def test_an_explicit_source_overrides_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda: ("", ""))
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
        "https://example.test/autoform.git?token=secret",
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
    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda: (secret_source, "2" * 40))

    result = scaffold_module.scaffold_project(tmp_path, title="Finite Flat")

    assert result.unpinned is True
    assert not (tmp_path / ".github").exists()
    assert secret_source not in "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


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


def test_blueprint_scaffold_never_replaces_a_concurrently_created_file(tmp_path: Path) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    descriptor = os.open(
        target,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        (target / "README.md").write_bytes(b"concurrent owner\n")
        with pytest.raises(ScaffoldError, match="already exists"):
            scaffold_module._exclusive_write_at(
                descriptor,
                "README.md",
                b"Autoform content\n",
                mode=0o644,
            )
    finally:
        os.close(descriptor)

    assert (target / "README.md").read_bytes() == b"concurrent owner\n"


def test_blueprint_scaffold_closes_a_nonempty_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    (target / "concurrent-owner").write_text("claimed\n", encoding="utf-8")
    original = scaffold_module._open_directory_chain
    opened: list[int] = []

    def record(path: Path) -> int:
        descriptor = original(path)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(scaffold_module, "_open_directory_chain", record)

    with pytest.raises(ScaffoldError, match="not empty"):
        scaffold_module.scaffold_blueprint(target, title="Probe")

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_blueprint_scaffold_requires_atomic_directory_publication_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    monkeypatch.setattr(scaffold_module, "_atomic_directory_publication_available", lambda: False)

    with pytest.raises(ScaffoldError, match="platform"):
        scaffold_module.scaffold_blueprint(target, title="Probe")

    assert list(target.iterdir()) == []


def test_blueprint_scaffold_rejects_a_racing_directory_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    original = scaffold_module._open_or_create_directory
    injected = False

    def race(parent_descriptor: int, name: str) -> int:
        nonlocal injected
        if not injected:
            injected = True
            os.symlink(outside, name, target_is_directory=True, dir_fd=parent_descriptor)
        return original(parent_descriptor, name)

    monkeypatch.setattr(scaffold_module, "_open_or_create_directory", race)

    with pytest.raises(ScaffoldError, match="cannot open blueprint directory safely"):
        scaffold_module.scaffold_blueprint(target, title="Probe")

    assert injected
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "event",
    ["identity-captured-before-bind", "bound-before-publication"],
)
def test_blueprint_scaffold_rejects_staged_child_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    held = target / "held-coverage-stage"
    staged_name = ""

    def replace_stage(
        current_event: str,
        _parent_descriptor: int,
        current_staging_name: str,
        target_name: str,
    ) -> None:
        nonlocal staged_name
        if current_event != event or target_name != "coverage" or staged_name:
            return
        staged_name = current_staging_name
        staged = target / staged_name
        staged.rename(held)
        (held / "owned").write_text("original\n", encoding="utf-8")
        staged.mkdir()
        (staged / "foreign").write_text("replacement\n", encoding="utf-8")

    monkeypatch.setattr(scaffold_module, "_scaffold_directory_checkpoint", replace_stage)

    with pytest.raises(ScaffoldError, match="blueprint directory changed"):
        scaffold_module.scaffold_blueprint(target, title="Probe")

    assert (held / "owned").read_text(encoding="utf-8") == "original\n"
    if event == "identity-captured-before-bind":
        assert not (target / "coverage").exists()
        foreign = target / staged_name
    else:
        foreign = target / "coverage"
    assert (foreign / "foreign").read_text(encoding="utf-8") == "replacement\n"


def test_blueprint_scaffold_preserves_a_directory_publication_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    staged_name = ""

    def claim_target(
        event: str,
        _parent_descriptor: int,
        current_staging_name: str,
        target_name: str,
    ) -> None:
        nonlocal staged_name
        if event != "bound-before-publication" or target_name != "coverage" or staged_name:
            return
        staged_name = current_staging_name
        (target / "coverage").mkdir()
        (target / "coverage/foreign").write_text("winner\n", encoding="utf-8")

    monkeypatch.setattr(scaffold_module, "_scaffold_directory_checkpoint", claim_target)

    with pytest.raises(ScaffoldError, match="blueprint directory changed"):
        scaffold_module.scaffold_blueprint(target, title="Probe")

    assert (target / "coverage/foreign").read_text(encoding="utf-8") == "winner\n"
    assert (target / staged_name).is_dir()
    assert list((target / staged_name).iterdir()) == []


def test_directory_stage_captures_identity_at_first_portable_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_descriptor = os.open(target, flags)
    original_mkdir = scaffold_module.os.mkdir
    original_stat = scaffold_module.os.stat
    staging_name = ""
    events: list[str] = []

    def record_mkdir(name, *args, **kwargs) -> None:
        nonlocal staging_name
        original_mkdir(name, *args, **kwargs)
        if isinstance(name, str) and name.startswith(scaffold_module._DIRECTORY_STAGE_PREFIX):
            staging_name = name
            events.append("mkdir")

    def record_stat(name, *args, **kwargs):
        if name == staging_name and "stat" not in events:
            events.append("stat")
        return original_stat(name, *args, **kwargs)

    def record_checkpoint(event: str, *_args) -> None:
        events.append(event)

    monkeypatch.setattr(scaffold_module.os, "mkdir", record_mkdir)
    monkeypatch.setattr(scaffold_module.os, "stat", record_stat)
    monkeypatch.setattr(scaffold_module, "_scaffold_directory_checkpoint", record_checkpoint)
    try:
        descriptor = scaffold_module._open_or_create_directory(parent_descriptor, "coverage")
        os.close(descriptor)
    finally:
        os.close(parent_descriptor)

    assert events[:3] == ["mkdir", "stat", "identity-captured-before-bind"]
    assert len(os.fsencode(staging_name)) == len(os.fsencode(scaffold_module._DIRECTORY_STAGE_PREFIX)) + 32


def test_directory_stage_rejects_an_exact_public_name_alias_before_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    first_token = "a" * 32
    second_token = "b" * 32
    public_name = f"{scaffold_module._DIRECTORY_STAGE_PREFIX}{first_token}"
    expected_stage = f"{scaffold_module._DIRECTORY_STAGE_PREFIX}{second_token}"
    tokens = iter((first_token, second_token))
    mkdir_names: list[str] = []
    original_mkdir = scaffold_module.os.mkdir

    def record_mkdir(name, *args, **kwargs) -> None:
        mkdir_names.append(name)
        original_mkdir(name, *args, **kwargs)

    monkeypatch.setattr(scaffold_module.secrets, "token_hex", lambda _size: next(tokens))
    monkeypatch.setattr(scaffold_module.os, "mkdir", record_mkdir)
    parent_descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = scaffold_module._open_or_create_directory(parent_descriptor, public_name)
        os.close(descriptor)
    finally:
        os.close(parent_descriptor)

    assert mkdir_names == [expected_stage]
    assert (target / public_name).is_dir()
    assert not (target / expected_stage).exists()


def test_directory_stage_rejects_a_darwin_casefold_public_alias_before_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    first_token = "a1" * 16
    second_token = "b2" * 16
    candidate = f"{scaffold_module._DIRECTORY_STAGE_PREFIX}{first_token}"
    public_name = candidate.upper()
    expected_stage = f"{scaffold_module._DIRECTORY_STAGE_PREFIX}{second_token}"
    tokens = iter((first_token, second_token))
    mkdir_names: list[str] = []
    original_mkdir = scaffold_module.os.mkdir

    def record_mkdir(name, *args, **kwargs) -> None:
        mkdir_names.append(name)
        original_mkdir(name, *args, **kwargs)

    monkeypatch.setattr(scaffold_module.secrets, "token_hex", lambda _size: next(tokens))
    monkeypatch.setattr(scaffold_module.os, "mkdir", record_mkdir)
    parent_descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = scaffold_module._open_or_create_directory(parent_descriptor, public_name)
        os.close(descriptor)
    finally:
        os.close(parent_descriptor)

    assert candidate.casefold() == public_name.casefold()
    assert mkdir_names == [expected_stage]
    assert (target / public_name).is_dir()
    assert not (target / expected_stage).exists()


def test_created_scaffold_directory_is_bound_before_parent_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    replacement = target / "coverage"
    displaced = target / "held-coverage"
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_descriptor = os.open(target, flags)
    original_sync = scaffold_module.os.fsync
    raced = False

    def replace_after_parent_fsync(descriptor: int) -> None:
        nonlocal raced
        original_sync(descriptor)
        if descriptor == parent_descriptor and not raced and replacement.is_dir():
            replacement.rename(displaced)
            replacement.mkdir()
            raced = True

    monkeypatch.setattr(scaffold_module.os, "fsync", replace_after_parent_fsync)
    try:
        with pytest.raises(ScaffoldError, match="blueprint directory changed"):
            scaffold_module._open_or_create_directory(parent_descriptor, "coverage")
    finally:
        os.close(parent_descriptor)

    assert raced
    assert list(replacement.iterdir()) == []
    assert list(displaced.iterdir()) == []


def test_blueprint_scaffold_retains_child_bindings_until_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    held = target / "held-coverage"
    original_write = scaffold_module._exclusive_write_at
    raced = False

    def replace_earlier_child(
        parent_descriptor: int,
        name: str,
        content: bytes,
        *,
        mode: int,
    ) -> None:
        nonlocal raced
        if name == ".gitignore" and not raced:
            (target / "coverage").rename(held)
            (target / "coverage").mkdir()
            raced = True
        original_write(parent_descriptor, name, content, mode=mode)

    monkeypatch.setattr(scaffold_module, "_exclusive_write_at", replace_earlier_child)

    with pytest.raises(ScaffoldError, match="coverage"):
        scaffold_module.scaffold_blueprint(target, title="Probe")

    assert raced
    assert (held / "README.md").is_file()
    assert list((target / "coverage").iterdir()) == []


def test_blueprint_scaffold_rejects_file_replacement_after_parent_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    displaced = target / "coverage/original-readme"
    replaced = False

    def replace_file(
        event: str,
        relative: str,
        _binding: scaffold_module._BlueprintScaffoldBinding,
    ) -> None:
        nonlocal replaced
        if event != "after-parent-fsync" or relative != "coverage" or replaced:
            return
        readme = target / "coverage/README.md"
        readme.rename(displaced)
        readme.write_text("foreign replacement\n", encoding="utf-8")
        replaced = True

    monkeypatch.setattr(scaffold_module, "_scaffold_binding_checkpoint", replace_file)

    with pytest.raises(ScaffoldError, match="blueprint file changed"):
        scaffold_module.scaffold_blueprint(target, title="Probe")

    assert replaced
    assert displaced.is_file()
    assert (target / "coverage/README.md").read_text(encoding="utf-8") == (
        "foreign replacement\n"
    )


def test_blueprint_scaffold_fsyncs_every_generated_parent_deepest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()
    events: list[tuple[str, str]] = []

    def record(
        event: str,
        relative: str,
        _binding: scaffold_module._BlueprintScaffoldBinding,
    ) -> None:
        events.append((event, relative))

    monkeypatch.setattr(scaffold_module, "_scaffold_binding_checkpoint", record)

    scaffold_module.scaffold_blueprint(target, title="Probe")

    parents = ["coverage", "javascripts", "roadmap", "sources", "."]
    assert events == [
        item
        for parent in parents
        for item in (
            ("before-parent-fsync", parent),
            ("after-parent-fsync", parent),
        )
    ]


def test_blueprint_scaffold_fails_closed_on_generated_parent_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "blueprint"
    target.mkdir()

    def fail(
        event: str,
        relative: str,
        _binding: scaffold_module._BlueprintScaffoldBinding,
    ) -> None:
        if event == "before-parent-fsync" and relative == "roadmap":
            raise OSError("injected parent fsync failure")

    monkeypatch.setattr(scaffold_module, "_scaffold_binding_checkpoint", fail)

    with pytest.raises(ScaffoldError, match="durably: roadmap"):
        scaffold_module.scaffold_blueprint(target, title="Probe")

    assert (target / "roadmap/README.md").is_file()


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


def test_a_source_without_a_ref_is_refused_without_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden() -> tuple[str, str]:
        raise AssertionError("partial explicit provenance invoked discovery")

    monkeypatch.setattr(scaffold_module, "plugin_pin", forbidden)
    with pytest.raises(ScaffoldError, match="must be provided together"):
        scaffold_project(
            tmp_path,
            title="Probe",
            autoform_source="https://example.test/other.git",
        )

    assert not list(tmp_path.iterdir())


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
    result = scaffold_project(
        tmp_path,
        title="Probe",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="A" * 40,
    )

    assert result.unpinned is False
    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert f'AUTOFORM_REF: "{"a" * 40}"' in verify
