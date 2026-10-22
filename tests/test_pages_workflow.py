"""The generated Pages workflow deploys only a commit `autoform verify` passed on main (#169).

The tests read the template and evaluate its conditions for each event GitHub
can send it, with a small evaluator for the part of GitHub's expression
language the workflow uses: property lookups, string literals, `==`, `!=`, `!`,
`&&`, `||`, parentheses, and `format`. As on GitHub, `==` compares strings
without regard to case, and `&&` and `||` return one of their operands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from autoform_cli.scaffold import _destination

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATES = _ROOT / "autoform_cli/templates/github/workflows"
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
    assert audit["run"] == steps[check_at]["run"].replace("autoform check", "autoform audit")
    assert audit["continue-on-error"] is True and "if" not in audit
