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
from pathlib import Path

import pytest

from autoform_cli import scaffold as scaffold_module
from autoform_cli.coverage import load_coverage
from autoform_cli.graph import load_graph
from autoform_cli.scaffold import ScaffoldError, scaffold_project

_REAL_VERIFIED_TEMPLATE_SNAPSHOT = scaffold_module._verified_template_snapshot
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
def _disable_live_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable():
        raise scaffold_module.ProvenanceError("network provenance is disabled in unit tests")

    monkeypatch.setattr(scaffold_module, "_verified_template_snapshot", unavailable)


def test_unpinned_scaffolding_is_offline_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import provenance

    monkeypatch.setattr(
        provenance,
        "_fetch_source_layout",
        lambda *args, **kwargs: pytest.fail("unit scaffold attempted remote access"),
    )

    result = scaffold_project(tmp_path, title="Offline")

    assert result.unpinned is True


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
    result = scaffold_project(project, title="Cache-safe", autoform_ref="0" * 40)

    assert ".github/autoform_audit.py" in result.written
    assert not (project / ".github/__pycache__").exists()
    assert all("__pycache__" not in path and not path.endswith(".pyc") for path in result.written)


def test_scaffold_writes_the_whole_vault(tmp_path: Path) -> None:
    result = scaffold_project(
        tmp_path,
        title="Finite Flat",
        repository_url="https://example.test/repo",
        autoform_ref="0" * 40,
    )

    assert set(result.written) == _EXPECTED
    assert result.skipped == ()
    for relative in _EXPECTED:
        assert (tmp_path / relative).is_file(), relative


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
    assert (
        'uv sync --no-config --locked --no-install-project --no-build --no-cache '
        '--no-python-downloads --python "$host_python" --project "$AUTOFORM_DIR"'
        in verify
    )
    assert 'runpy.run_module("autoform_cli",run_name="__main__")' in verify
    assert '"$AUTOFORM_DIR" check blueprint' in verify
    assert "uv run" not in verify
    assert "uvx --from" not in verify
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
    scaffold_project(tmp_path, title="Finite Flat")
    (tmp_path / "blueprint/README.md").write_text("# Hand written\n", encoding="utf-8")

    again = scaffold_project(tmp_path, title="Finite Flat")

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


def test_generated_ci_uses_verified_plugin_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "https://example.test/autoform.git"
    ref = "1" * 40
    snapshot = scaffold_module._filesystem_template_snapshot(scaffold_module._TEMPLATES)
    monkeypatch.setattr(
        scaffold_module,
        "_verified_template_snapshot",
        lambda: (source, ref, snapshot),
    )

    scaffold_project(tmp_path, title="Finite Flat")
    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")

    assert f"AUTOFORM_SOURCE: {json.dumps(source)}" in verify
    assert f"AUTOFORM_REF: {json.dumps(ref)}" in verify
    assert (
        'uv sync --no-config --locked --no-install-project --no-build --no-cache '
        '--no-python-downloads --python "$host_python" --project "$AUTOFORM_DIR"'
        in verify
    )
    assert 'runpy.run_module("autoform_cli",run_name="__main__")' in verify
    assert '"$AUTOFORM_DIR" check blueprint' in verify
    assert "uv run" not in verify
    assert "uvx --from" not in verify
    assert "@main" not in verify


def test_generated_workflows_install_the_verified_lock(tmp_path: Path) -> None:
    scaffold_project(
        tmp_path,
        title="Finite Flat",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="1" * 40,
    )

    for name in ("autoform-verify.yml", "blueprint-pages.yml"):
        workflow = (tmp_path / ".github/workflows" / name).read_text(encoding="utf-8")
        assert 'fetch --depth=1 --no-tags --no-recurse-submodules origin "$AUTOFORM_REF"' in workflow
        assert 'test "$resolved" = "$AUTOFORM_REF"' in workflow
        assert "persist-credentials: false" in workflow
        assert (
            'uv sync --no-config --locked --no-install-project --no-build --no-cache '
            '--no-python-downloads --python "$host_python" --project "$AUTOFORM_DIR"'
            in workflow
        )
        assert 'runpy.run_module("autoform_cli",run_name="__main__")' in workflow
        assert '"$AUTOFORM_DIR/.venv/bin/python" -I -c' in workflow
        assert "uv run" not in workflow
        assert "uvx --from" not in workflow


def test_scaffold_writes_the_verified_template_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    shutil.copytree(scaffold_module._TEMPLATES, templates)
    static = templates / "blueprint/javascripts/mathjax.js"
    verified_content = static.read_bytes()
    source = "https://example.test/autoform.git"
    ref = "1" * 40

    def verified_snapshot():
        entries = tuple(
            (
                path.relative_to(templates).as_posix(),
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
            for path in sorted(templates.rglob("*"))
            if path.is_file()
        )
        static.write_bytes(b"mutated after verification\n")
        return source, ref, entries

    monkeypatch.setattr(scaffold_module, "_TEMPLATES", templates)
    monkeypatch.setattr(
        scaffold_module,
        "_verified_template_snapshot",
        verified_snapshot,
    )
    monkeypatch.setattr(
        scaffold_module,
        "plugin_pin",
        lambda: verified_snapshot()[:2],
    )

    scaffold_project(tmp_path / "project", title="Finite Flat")

    written = tmp_path / "project/blueprint/javascripts/mathjax.js"
    assert written.read_bytes() == verified_content


def test_verified_scaffolding_does_not_read_the_live_template_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "https://example.test/autoform.git"
    ref = "1" * 40
    snapshot = (("README.md", b"verified\n", 0o644),)
    monkeypatch.setattr(
        scaffold_module,
        "_verified_template_snapshot",
        lambda: (source, ref, snapshot),
    )
    monkeypatch.setattr(
        scaffold_module,
        "_filesystem_template_snapshot",
        lambda root: pytest.fail(f"read live template tree: {root}"),
    )

    scaffold_project(tmp_path, title="Finite Flat")

    assert (tmp_path / "README.md").read_bytes() == b"verified\n"


@pytest.mark.skipif(os.name != "posix", reason="safe local template reads are POSIX-only")
def test_local_template_snapshot_rejects_symlinks_without_reading_them(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"must not be copied\n")
    (templates / "README.md").symlink_to(secret)

    with pytest.raises(ScaffoldError, match="templates cannot be read safely"):
        scaffold_module._filesystem_template_snapshot(templates)


@pytest.mark.skipif(os.name != "posix", reason="safe local template reads are POSIX-only")
def test_local_template_snapshot_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "README.md").write_bytes(b"oversized")
    monkeypatch.setattr(scaffold_module, "_MAX_TEMPLATE_FILE_BYTES", 4)

    with pytest.raises(ScaffoldError, match="templates cannot be read safely"):
        scaffold_module._filesystem_template_snapshot(templates)


def test_verified_template_snapshot_comes_from_the_fetched_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import provenance

    verified = provenance.PluginProvenance(
        "https://example.test/autoform.git",
        "1" * 40,
    )
    layout = provenance._SourceLayout(
        files={
            "autoform_cli/__init__.py": provenance._ManifestEntry(0o100644, b"package\n"),
            "autoform_cli/templates/README.md": provenance._ManifestEntry(
                0o100755,
                b"verified template\n",
            ),
        },
        all_files=frozenset(),
        roots=("autoform_cli",),
        package_roots=("autoform_cli",),
    )
    monkeypatch.setattr(
        provenance,
        "_verify_plugin_layout",
        lambda: (verified, layout),
    )

    assert _REAL_VERIFIED_TEMPLATE_SNAPSHOT() == (
        verified.source,
        verified.revision,
        (("README.md", b"verified template\n", 0o755),),
    )


def test_scaffold_pin_delegates_to_verified_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autoform_cli import provenance

    expected = ("https://example.test/autoform.git", "1" * 40)
    monkeypatch.setattr(provenance, "plugin_pin", lambda: expected)

    assert scaffold_module.plugin_pin() == expected


def test_explicit_pin_overrides_without_discovering_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        scaffold_module,
        "_verified_template_snapshot",
        lambda: pytest.fail("explicit provenance must not trigger discovery"),
    )
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

    def unavailable():
        raise scaffold_module.ProvenanceError("unavailable")

    monkeypatch.setattr(scaffold_module, "_verified_template_snapshot", unavailable)
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


def test_a_ref_alone_restores_ci_without_discovering_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ref-only override explicitly targets the canonical repository."""
    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(
        scaffold_module,
        "_verified_template_snapshot",
        lambda: pytest.fail("an explicit ref must not trigger provenance discovery"),
    )
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

    monkeypatch.setattr(
        scaffold_module,
        "_verified_template_snapshot",
        lambda: pytest.fail("invalid input must not trigger provenance discovery"),
    )
    with pytest.raises(scaffold_module.ScaffoldError) as caught:
        scaffold_module.scaffold_project(tmp_path, title="Finite Flat", autoform_ref=ref)

    assert "40-character commit sha" in str(caught.value)
    assert not (tmp_path / ".github").exists()
    assert not (tmp_path / "blueprint").exists()


def test_an_explicit_source_overrides_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(
        scaffold_module,
        "_verified_template_snapshot",
        lambda: pytest.fail("an explicit source must not trigger provenance discovery"),
    )
    scaffold_module.scaffold_project(
        tmp_path,
        title="Finite Flat",
        autoform_source="https://example.test/autoform.git",
        autoform_ref="2" * 40,
    )

    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert "AUTOFORM_SOURCE: \"https://example.test/autoform.git\"" in verify
    assert f"AUTOFORM_REF: \"{'2' * 40}\"" in verify
    assert "--no-install-project --no-build --no-cache" in verify
    assert 'runpy.run_module("autoform_cli",run_name="__main__")' in verify
    assert '"$AUTOFORM_DIR" check blueprint' in verify
    assert "uvx --from" not in verify


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
    snapshot = scaffold_module._filesystem_template_snapshot(scaffold_module._TEMPLATES)
    monkeypatch.setattr(
        scaffold_module,
        "_verified_template_snapshot",
        lambda: (secret_source, "2" * 40, snapshot),
    )

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
        "_verified_template_snapshot",
        lambda: pytest.fail("an explicit source must not trigger provenance discovery"),
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
