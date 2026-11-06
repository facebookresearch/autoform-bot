"""The generated Pages workflow deploys only a commit `autoform verify` passed on main (#169).

The tests read the template and evaluate its conditions for each event GitHub
can send it, with a small evaluator for the part of GitHub's expression
language the workflow uses: property lookups, string literals, `==`, `!=`, `!`,
`&&`, `||`, parentheses, and `format`. As on GitHub, `==` compares strings
without regard to case, and `&&` and `||` return one of their operands.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from autoform_cli.scaffold import _destination

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "autoform_cli/templates/github/workflows"
_AUTOFORM = 'uvx --from "git+${AUTOFORM_SOURCE}@${AUTOFORM_REF}" autoform'
_VERIFY_PATH = ".github/workflows/autoform-verify.yml"
_MOVES_HEAD = re.compile(r"\bgit\s+(checkout|switch|reset|pull|fetch)\b")
_TOKEN = re.compile(r"\s*(?:(?P<string>'(?:[^']|'')*')|(?P<op>==|!=|&&|\|\||!|\(|\)|,)|(?P<name>[A-Za-z_][\w.-]*))")


def _evaluate(expression: str, context: dict) -> object:
    """``expression``, without ``${{ }}``, evaluated against ``context``."""

    tokens = [match for match in _TOKEN.finditer(expression.strip()) if match.group().strip()]
    assert "".join(token.group().strip() for token in tokens) == re.sub(r"\s+", "", expression), expression
    position = 0

    def peek() -> str | None:
        return tokens[position].group().strip() if position < len(tokens) else None

    def take() -> re.Match[str]:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def either() -> object:
        left = both()
        while peek() == "||":
            take()
            right = both()
            left = left if left else right
        return left

    def both() -> object:
        left = compare()
        while peek() == "&&":
            take()
            right = compare()
            left = right if left else left
        return left

    def compare() -> object:
        left = negation()
        if peek() in {"==", "!="}:
            operator = take().group().strip()
            right = negation()
            same = (left.casefold() == right.casefold()) if isinstance(left, str) and isinstance(right, str) else left == right
            return same if operator == "==" else not same
        return left

    def negation() -> object:
        if peek() == "!":
            take()
            return not negation()
        return primary()

    def primary() -> object:
        token = take()
        if token.group("string") is not None:
            return token.group("string")[1:-1].replace("''", "'")
        if token.group().strip() == "(":
            value = either()
            assert take().group().strip() == ")"
            return value
        name = token.group("name")
        assert name is not None, token.group()
        if peek() == "(":
            take()
            arguments = [either()]
            while peek() == ",":
                take()
                arguments.append(either())
            assert take().group().strip() == ")"
            assert name == "format", name
            template, *values = arguments
            return re.sub(r"\{(\d+)\}", lambda field: str(values[int(field.group(1))]), template)
        value: object = context
        for part in name.split("."):
            value = value.get(part, "") if isinstance(value, dict) else ""
        return value

    value = either()
    assert position == len(tokens), expression
    return value


def _unwrap(expression: str) -> str:
    return expression.strip().removeprefix("${{").removesuffix("}}").strip()


@pytest.fixture(scope="module")
def pages() -> dict:
    text = (_TEMPLATES / "blueprint-pages.yml").read_text(encoding="utf-8")
    text = text.replace("{{AUTOFORM_SOURCE_YAML}}", '"source"').replace("{{AUTOFORM_REF_YAML}}", '"ref"')
    return yaml.safe_load(text)


def _verify_run(**changes: object) -> dict:
    run = {
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "path": _VERIFY_PATH,
        "head_sha": "0" * 39 + "1",
        "head_repository": {"full_name": "owner/project"},
    }
    run.update(changes)
    return {"event_name": "workflow_run", "repository": "owner/project", "run_id": 7, "event": {"workflow_run": run}}


_EVENTS = {
    "verified push to main": _verify_run(),
    "same-named decoy workflow": _verify_run(path=".github/workflows/decoy.yml"),
    "failed verification": _verify_run(conclusion="failure"),
    "cancelled verification": _verify_run(conclusion="cancelled"),
    "verification of a pull request": _verify_run(event="pull_request"),
    "verification in a fork": _verify_run(head_repository={"full_name": "someone/fork"}),
    "verification of another branch": _verify_run(head_branch="feature"),
    "pull request": {"event_name": "pull_request", "repository": "owner/project", "run_id": 8, "sha": "pr"},
    "manual run": {"event_name": "workflow_dispatch", "repository": "owner/project", "run_id": 9, "sha": "main"},
}
_DEPLOYS = {"verified push to main"}
_BUILDS = _DEPLOYS | {"pull request", "manual run"}


@pytest.mark.parametrize("event", _EVENTS)
def test_only_a_verified_push_to_main_deploys(pages: dict, event: str) -> None:
    """A run builds when it is a pull request, a manual run, or a passing
    verify run of this project's own verify workflow on a push to main; it
    deploys only in the last case. The workflow is named by its file, so a
    workflow that copies its name cannot authorize a deploy."""

    context = {"github": _EVENTS[event]}
    build = pages["jobs"]["build"]
    builds = bool(_evaluate(build["if"], context))
    upload = next(step for step in build["steps"] if step["name"] == "Upload Pages artifact")
    uploads = bool(_evaluate(upload["if"], context))
    deploy_runs = bool(_evaluate(pages["jobs"]["deploy"]["if"], context))

    assert (builds, builds and uploads and deploy_runs) == (event in _BUILDS, event in _DEPLOYS)
    # Neither later gate leans on the other: each turns away every run that is not a verify run.
    assert uploads == deploy_runs == (_EVENTS[event]["event_name"] == "workflow_run")


@pytest.mark.parametrize("event", _EVENTS)
def test_only_deploys_share_the_deploy_concurrency_group(pages: dict, event: str) -> None:
    """Builds that cannot deploy each get a group of their own, so a pull
    request, a manual run, a failed verification or a decoy cannot cancel a
    verified deploy waiting in the deploy group."""

    group = _evaluate(_unwrap(pages["concurrency"]["group"]), {"github": _EVENTS[event]})

    if event in _DEPLOYS:
        assert group == "blueprint-pages-deploy"
    else:
        assert group == f"blueprint-pages-{_EVENTS[event]['run_id']}"
    assert pages["concurrency"]["cancel-in-progress"] is False


def test_the_deploy_group_and_the_build_share_one_condition(pages: dict) -> None:
    deploy_condition = re.search(r"\((github\.event_name == 'workflow_run'.*?)\)\s*&&\s*'blueprint-pages-deploy'",
                                 _unwrap(pages["concurrency"]["group"]), re.DOTALL)
    build_condition = re.fullmatch(r"github\.event_name != 'workflow_run'\s*\|\|\s*\((.*)\)", pages["jobs"]["build"]["if"],
                                   re.DOTALL)

    assert deploy_condition and build_condition
    normalized = [re.sub(r"\s+", " ", text) for text in (deploy_condition.group(1), build_condition.group(1))]
    assert normalized[0] == "github.event_name == 'workflow_run' && " + normalized[1]


def test_a_deploy_renders_and_checks_out_exactly_the_verified_commit(pages: dict) -> None:
    """A workflow_run's GITHUB_SHA is main's newest commit, not the verified
    one, so a deploy checks out the run's head_sha, fails unless that is what
    it checked out, and names it to render for the site's code links. Both
    checkouts come before that check, and no step's script moves HEAD."""

    in_order = pages["jobs"]["build"]["steps"]
    names = [step["name"] for step in in_order]
    checkouts = [at for at, step in enumerate(in_order) if step.get("uses", "").startswith("actions/checkout@")]
    moves_head = [step["name"] for step in in_order if _MOVES_HEAD.search(step.get("run", ""))]
    confirm_at = names.index("Confirm the checked-out commit is the verified one")
    assert len(checkouts) == 2 and max(checkouts) < confirm_at < names.index("Render the blueprint")
    assert not moves_head

    steps = dict(zip(names, in_order))
    deploy = {"github": _EVENTS["verified push to main"]}
    head_sha = deploy["github"]["event"]["workflow_run"]["head_sha"]

    verified = steps["Check out the verified commit"]
    assert _evaluate(verified["if"], deploy) and verified["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}"
    assert not _evaluate(steps["Check out project"]["if"], deploy)
    confirm = steps["Confirm the checked-out commit is the verified one"]
    assert _evaluate(confirm["if"], deploy)
    assert confirm["env"]["VERIFIED_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert confirm["run"] == 'test -n "$VERIFIED_SHA" && test "$(git rev-parse HEAD)" = "$VERIFIED_SHA"'

    render = steps["Render the blueprint"]
    assert '--ref "$PAGES_REF"' in render["run"]
    assert _evaluate(_unwrap(render["env"]["PAGES_REF"]), deploy) == head_sha
    assert _evaluate(_unwrap(render["env"]["PAGES_REF"]), {"github": _EVENTS["pull request"]}) == "pr"


def test_the_workflow_run_names_the_generated_verify_workflow(pages: dict) -> None:
    """The trigger names the verify workflow as GitHub lists it, by name; the
    build's condition binds it to the file init writes."""

    verify = yaml.safe_load((_TEMPLATES / "autoform-verify.yml").read_text(encoding="utf-8").replace("{{", "").replace("}}", ""))
    trigger = pages[True]["workflow_run"]

    assert trigger == {"workflows": [verify["name"]], "types": ["completed"], "branches": ["main"]}
    assert _destination("github/workflows/autoform-verify.yml") == _VERIFY_PATH
    assert f"github.event.workflow_run.path == '{_VERIFY_PATH}'" in pages["jobs"]["build"]["if"]
    assert "push" not in pages[True]
    assert {"pull_request", "workflow_dispatch"} <= set(pages[True])


def test_the_audit_runs_after_check_as_a_report(pages: dict) -> None:
    """Every build runs ``autoform audit`` right after ``check``, from the same
    pinned Autoform on the same roots. Audit exits 1 on any finding, so the step
    continues on error: a finding never fails the build or stops a deploy (#391)."""

    steps = pages["jobs"]["build"]["steps"]
    check_at = [step["name"] for step in steps].index("Validate the theorem DAG and its declarations")
    audit = steps[check_at + 1]

    assert audit["name"] == "Audit the roadmap (report only)"
    assert audit["run"] == f"{_AUTOFORM} audit blueprint --lean-root ."
    # Check runs the same pinned Autoform on the same roots.
    assert 'autoform="git+${AUTOFORM_SOURCE}@${AUTOFORM_REF}"' in steps[check_at]["run"]
    assert 'uvx --from "$autoform" autoform check blueprint --lean-root .' in steps[check_at]["run"]
    assert audit["continue-on-error"] is True and set(audit) == {"name", "continue-on-error", "run"}


def test_the_site_check_runs_after_the_build_as_a_report(pages: dict) -> None:
    """Every build runs ``autoform site check`` right after MkDocs, from the same
    pinned Autoform, on the site it uploads and the source render wrote. Like
    the audit, it continues on error, so it never fails the build or stops a
    deploy (#389)."""

    steps = pages["jobs"]["build"]["steps"]
    names = [step["name"] for step in steps]
    site_check = steps[names.index("Build the Markdown site") + 1]
    site = steps[names.index("Upload Pages artifact")]["with"]["path"]
    output = re.search(r"--output (\S+)", steps[names.index("Render the blueprint")]["run"]).group(1)

    assert site_check["name"] == "Check the built site (report only)"
    assert site_check["run"] == f"{_AUTOFORM} site check {site} --source {output}"
    assert site_check["continue-on-error"] is True and set(site_check) == {"name", "continue-on-error", "run"}


@pytest.mark.skipif(shutil.which("bash") is None, reason="the step runs in bash")
@pytest.mark.parametrize("pinned", ["flagged", "older", "unreachable"])
def test_check_leaves_the_site_warnings_to_render_when_its_autoform_can(
    pages: dict, tmp_path: Path, pinned: str
) -> None:
    """Pages runs ``check`` with ``--no-site-warnings`` when the pinned
    Autoform's check lists it, since render reports those warnings later in
    the same job. An older pin, which init and setup accept by
    ``--autoform-ref``, runs the full check, and a pin uvx cannot run stops
    the step. Verify keeps the warnings: setup can write a project with no
    Pages workflow, where check is the only step that reports them."""

    steps = pages["jobs"]["build"]["steps"]
    names = [step["name"] for step in steps]
    check = steps[names.index("Validate the theorem DAG and its declarations")]
    verify = (_TEMPLATES / "autoform-verify.yml").read_text(encoding="utf-8")
    calls = tmp_path / "calls"
    help_text = "usage: autoform check [-h] [--lean-root LEAN_ROOT]" + (" [--no-site-warnings]" if pinned == "flagged" else "")
    uvx = tmp_path / "bin" / "uvx"
    uvx.parent.mkdir()
    uvx.write_text(
        f'#!/bin/sh\necho "$*" >> "{calls}"\n'
        + ('exit 1\n' if pinned == "unreachable" else f'case "$*" in *--help*) echo "{help_text}" ;; esac\n'),
        encoding="utf-8",
    )
    uvx.chmod(0o755)
    ref = "0" * 40
    env = {"PATH": f"{uvx.parent}:/usr/bin:/bin", "AUTOFORM_SOURCE": "https://example.invalid/a.git", "AUTOFORM_REF": ref}

    # GitHub runs a step's script with bash -e -o pipefail.
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", check["run"]], env=env, cwd=tmp_path)
    autoform = f"--from git+https://example.invalid/a.git@{ref} autoform"
    if pinned == "unreachable":
        assert result.returncode != 0 and calls.read_text(encoding="utf-8").splitlines() == [f"{autoform} check --help"]
    else:
        flag = " --no-site-warnings" if pinned == "flagged" else ""
        assert result.returncode == 0
        assert calls.read_text(encoding="utf-8").splitlines() == [
            f"{autoform} check --help",
            f"{autoform} check blueprint --lean-root .{flag}",
        ]
    assert names.index("Render the blueprint") > names.index("Validate the theorem DAG and its declarations")
    assert "autoform check" in verify and "--no-site-warnings" not in verify


def _verify() -> dict:
    text = (_TEMPLATES / "autoform-verify.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text.replace("{{AUTOFORM_SOURCE_YAML}}", '"source"').replace("{{AUTOFORM_REF_YAML}}", '"ref"'))


def test_verify_keeps_the_builds_ileans_for_the_site_after_a_push(pages: dict) -> None:
    """Last, so only a run that passed keeps them, a push to main uploads the root package's .ilean files, which
    say what each name in the Lean sources means, for Pages to fetch by the same name. Hidden files are kept,
    since they sit under .lake, and the step can never fail verify."""

    keep = _verify()["jobs"]["build"]["steps"][-1]
    fetch = next(step for step in pages["jobs"]["build"]["steps"] if step["name"] == "Fetch the build's name references")

    assert keep["name"] == "Keep the build's name references for the site"
    assert keep["if"] == "github.event_name == 'push'" and keep["continue-on-error"] is True
    assert keep["uses"].startswith("actions/upload-artifact@")
    assert keep["with"] == {
        "name": fetch["with"]["name"],
        "path": ".lake/build/lib/lean/**/*.ilean",
        "include-hidden-files": True,
    }


def test_a_deploy_fetches_the_ileans_of_the_verify_run_it_renders(pages: dict) -> None:
    """Only a verify run's build fetches that run's .ilean files, and a failed fetch stops nothing. Render reads
    them only when the fetch succeeded, so a partial, rejected or expired download reads as none. Reading another
    run's artifacts needs actions: read, which the build job takes from the workflow; the deploy job keeps its own
    permissions."""

    steps = pages["jobs"]["build"]["steps"]
    names = [step["name"] for step in steps]
    fetch = steps[names.index("Fetch the build's name references")]
    render = steps[names.index("Render the blueprint")]

    assert pages["permissions"] == {"contents": "read", "actions": "read"} and "permissions" not in pages["jobs"]["build"]
    assert pages["jobs"]["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    for event, github in _EVENTS.items():
        assert bool(_evaluate(fetch["if"], {"github": github})) == (github["event_name"] == "workflow_run"), event
    assert fetch["id"] == "ileans" and fetch["continue-on-error"] is True
    assert fetch["uses"].startswith("actions/download-artifact@")
    assert fetch["with"]["run-id"] == "${{ github.event.workflow_run.id }}"
    assert fetch["with"]["github-token"] == "${{ github.token }}"
    assert fetch["with"]["path"] == "${{ runner.temp }}/ileans"
    assert names.index(fetch["name"]) < names.index(render["name"])
    for outcome, ileans in [("success", "/runner/ileans"), ("failure", ""), ("skipped", "")]:
        context = {"steps": {"ileans": {"outcome": outcome}}, "runner": {"temp": "/runner"}}
        assert _evaluate(_unwrap(render["env"]["ILEANS"]), context) == ileans, outcome


@pytest.mark.skipif(shutil.which("bash") is None, reason="the step runs in bash")
@pytest.mark.parametrize("case", ["fetched, flag listed", "fetched, older pin", "not fetched", "fetched, pin unreachable"])
def test_render_reads_the_ileans_when_they_came_and_its_autoform_can(pages: dict, tmp_path: Path, case: str) -> None:
    """Render gets --ileans only when the fetch brought the files and the pinned Autoform's render lists the
    option; otherwise it renders as before, and without the files it does not ask. As in check, a pin uvx cannot
    run stops the step."""

    steps = pages["jobs"]["build"]["steps"]
    render = next(step for step in steps if step["name"] == "Render the blueprint")
    calls = tmp_path / "calls"
    help_text = "usage: autoform render [-h] [--lean-root LEAN_ROOT]" + (" [--ileans DIR]" if "listed" in case else "")
    uvx = tmp_path / "bin" / "uvx"
    uvx.parent.mkdir()
    uvx.write_text(
        f'#!/bin/sh\necho "$*" >> "{calls}"\n'
        + ("exit 1\n" if "unreachable" in case else f'case "$*" in *--help*) echo "{help_text}" ;; esac\n'),
        encoding="utf-8",
    )
    uvx.chmod(0o755)
    ileans = "" if case == "not fetched" else str(tmp_path / "ileans")
    ref = "0" * 40
    env = {
        "PATH": f"{uvx.parent}:/usr/bin:/bin",
        "AUTOFORM_SOURCE": "https://example.invalid/a.git",
        "AUTOFORM_REF": ref,
        "PAGES_REF": "1" * 40,
        "ILEANS": ileans,
    }

    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", render["run"]], env=env, cwd=tmp_path)

    autoform = f"--from git+https://example.invalid/a.git@{ref} autoform"
    rendered = f"{autoform} render blueprint --output site-src --lean-root . --require-declarations --ref {'1' * 40}"
    expected = {
        "fetched, flag listed": [f"{autoform} render --help", f"{rendered} --ileans {ileans}"],
        "fetched, older pin": [f"{autoform} render --help", rendered],
        "not fetched": [rendered],
        "fetched, pin unreachable": [f"{autoform} render --help"],
    }
    assert (result.returncode == 0) == ("unreachable" not in case)
    assert calls.read_text(encoding="utf-8").splitlines() == expected[case]
