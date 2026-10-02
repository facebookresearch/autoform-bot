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
import subprocess
from pathlib import Path

import pytest

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
    result = scaffold_project(project, title="Cache-safe")

    assert ".github/autoform_audit.py" in result.written
    assert not (project / ".github/__pycache__").exists()
    assert all("__pycache__" not in path and not path.endswith(".pyc") for path in result.written)


def test_scaffold_writes_the_whole_vault(tmp_path: Path) -> None:
    result = scaffold_project(tmp_path, title="Finite Flat", repository_url="https://example.test/repo")

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

    assert main(["init", str(tmp_path), "--title", "Finite Flat", "--json"]) == 0
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


def test_generated_ci_pins_the_checkout_that_scaffolded_it(tmp_path: Path) -> None:
    """A floating ref installs an Autoform that may not have this CLI.

    `facebookresearch/autoform-bot@main` predates `autoform_cli` entirely, so
    defaulting to it meant every scaffolded project's first CI run installed a
    build with no `autoform` command. The pin now comes from the checkout doing
    the scaffolding, which is immutable and known-good by construction.
    """

    from autoform_cli.scaffold import plugin_pin

    scaffold_project(tmp_path, title="Finite Flat")
    source, ref = plugin_pin()
    verify = (tmp_path / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")

    assert f"AUTOFORM_SOURCE: {json.dumps(source)}" in verify
    assert f"AUTOFORM_REF: {json.dumps(ref)}" in verify
    assert '"git+${AUTOFORM_SOURCE}@${AUTOFORM_REF}"' in verify
    assert re.fullmatch(r"[0-9a-f]{40}", ref), "the pin must be an immutable commit"
    assert "@main" not in verify


def test_generated_ci_rebuilds_opted_in_statement_review_evidence(tmp_path: Path) -> None:
    scaffold_project(tmp_path, title="Finite Flat")
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


def test_the_approval_gate_runs_apart_from_the_lean_build(tmp_path: Path) -> None:
    """A review event reruns only the gate, and the gate asks about its own
    pull request; the Lean build keeps one run per ref, so a review arriving
    cannot cancel it."""

    scaffold_project(tmp_path, title="Finite Flat")
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
    # Pages reads the verify run and builds every push to main, so a change to CODEOWNERS relabels the site.
    assert "actions: read" in pages
    assert "  push:\n    branches: [main]\n  pull_request:\n" in pages
    # A pull request's runs never cancel a pending main build, and neither does a newer run of main, such as
    # a re-run of an old one: the pending run may be the only build of the current head.
    assert "  group: blueprint-pages-${{ github.ref }}\n  cancel-in-progress: false\n  queue: max\n" in pages


def test_pages_authenticates_approvals_in_a_job_that_never_builds_the_project(
    tmp_path: Path,
) -> None:
    """Building Lean runs the project's own build code, so the job that reads
    the token, labels approvals, and uploads the site takes only the skeleton
    report from the Lean job, and restores no cache that job could write."""

    scaffold_project(tmp_path, title="Finite Flat")
    pages = (tmp_path / ".github/workflows/blueprint-pages.yml").read_text(encoding="utf-8")
    jobs = pages.split("\njobs:\n")[1]
    lean = jobs.split("\n  lean:\n")[1].split("\n  build:\n")[0]
    build = jobs.split("\n  build:\n")[1].split("\n  deploy:\n")[0]

    assert "lake build" in lean and "autoform skeleton blueprint --lean-root ." in lean
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in lean
    assert "GITHUB_TOKEN" not in lean and "--authenticate" not in lean
    assert "    needs: lean\n" in build
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
    tmp_path: Path, script: str, answers: dict[str, object], **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str], dict[str, str]]:
    """Run a step's script with ``gh api`` answering from ``answers``.

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
        (stub / (re.sub(r"[^A-Za-z0-9]", "_", path) + ".json")).write_text(json.dumps(answer), encoding="utf-8")
    output = tmp_path / "github-output"
    output.touch()
    done = subprocess.run(
        ["bash", "-c", script],
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

    scaffold_project(tmp_path / "project", title="Finite Flat")
    workflow = tmp_path / "project/.github/workflows/blueprint-pages.yml"
    pages = workflow.read_text(encoding="utf-8")
    deploy = pages.split("\n  deploy:\n")[1]
    name = "Check that the build is of the default branch's head"
    assert deploy.index(f"- name: {name}\n") < deploy.index("uses: actions/deploy-pages@")
    assert "      contents: read\n" in deploy.split("    steps:\n")[0]

    done, _, _ = _run_step(tmp_path, _step(workflow, "deploy", name), answers, GITHUB_SHA="a" * 40)

    assert (done.returncode == 0) is deploys, done.stderr
    if not deploys:
        assert "::error::" in done.stdout or "HTTP 502" in done.stderr



@pytest.mark.parametrize("merged", [True, False], ids=["merge-commit", "linear"])
def test_the_gate_takes_its_base_only_from_the_merge_commit_of_the_pull_request(tmp_path: Path, merged: bool) -> None:
    """In any other checkout HEAD^1 is just the commit before, which would make the gate trust the wrong base."""

    scaffold_project(tmp_path / "project", title="Finite Flat")
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


def test_a_ref_alone_restores_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The commit is the unguessable half; the repository has a sane default.

    Setup tells the agent to pass `--autoform-ref`. If the source had to be
    supplied too, following that instruction would still yield no CI, and the
    fail-closed behaviour would be indistinguishable from a broken flag.
    """
    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(scaffold_module, "plugin_pin", lambda: ("", ""))
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


def test_plugin_pin_is_empty_outside_a_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoform_cli import scaffold as scaffold_module

    monkeypatch.setattr(scaffold_module, "_git", lambda *args, **kwargs: None)
    monkeypatch.setattr(scaffold_module, "_marketplace_checkout", lambda: None)
    assert scaffold_module.plugin_pin() == ("", "")


def _repository(path: Path, remote: str) -> str:
    """Make *path* a real one-commit checkout and return its HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    run = ["git", "-c", "user.email=t@test", "-c", "user.name=Test"]
    subprocess.run([*run, "init", "-q"], cwd=path, check=True)
    subprocess.run([*run, "remote", "add", "origin", remote], cwd=path, check=True)
    subprocess.run([*run, "commit", "-q", "--allow-empty", "-m", "first"], cwd=path, check=True)
    done = subprocess.run(
        [*run, "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _fake_plugin_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Lay out a plugin cache copy and the real checkout it was copied from."""
    from autoform_cli import scaffold as scaffold_module

    checkout = tmp_path / "src" / "autoform-bot"
    (checkout / "autoform_cli").mkdir(parents=True)
    (checkout / "autoform_cli" / "scaffold.py").write_text("", encoding="utf-8")
    head = _repository(checkout, "git@github.com:owner/autoform-bot.git")

    copied = tmp_path / ".claude/plugins/cache/autoform/autoform/0.5.0/autoform_cli"
    copied.mkdir(parents=True)
    monkeypatch.setattr(scaffold_module, "_here", lambda: copied.parent)

    registry = tmp_path / "known_marketplaces.json"
    registry.write_text(
        json.dumps({"autoform": {"installLocation": str(checkout)}}), encoding="utf-8"
    )
    monkeypatch.setattr(scaffold_module, "_PLUGIN_REGISTRY", registry)
    return checkout, head


def test_an_installed_plugin_pins_from_the_marketplace_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy has no `.git`, but the checkout it was copied from does.

    Without this, `init` under a plugin can only fail closed, and the operator
    is asked for a commit that nothing on their machine reports. That is a real
    provenance record, not the guess `plugin_pin` refuses to make.
    """
    from autoform_cli import scaffold as scaffold_module

    _, head = _fake_plugin_install(tmp_path, monkeypatch)

    assert scaffold_module.plugin_pin() == (
        "https://github.com/owner/autoform-bot.git",
        head,
    )


def test_an_unrelated_marketplace_checkout_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A location that is not Autoform would pin CI to somebody else's repo."""
    from autoform_cli import scaffold as scaffold_module

    checkout, _ = _fake_plugin_install(tmp_path, monkeypatch)
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

    checkout, _ = _fake_plugin_install(tmp_path, monkeypatch)

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
        scaffold_module, "plugin_pin", lambda: ("https://example.test/ours.git", "1" * 40)
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
