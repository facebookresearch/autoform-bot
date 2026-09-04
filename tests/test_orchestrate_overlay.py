from __future__ import annotations

import re
from pathlib import Path


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    assert match is not None
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line or line[0].isspace():
            continue
        key, separator, value = line.partition(":")
        assert separator
        fields[key] = value.strip()
    return fields, match.group(2)


def test_orchestrate_is_a_thin_cli_workflow(repo_root: Path) -> None:
    fields, body = _frontmatter(repo_root / "skills/orchestrate/SKILL.md")

    assert fields["name"] == "orchestrate"
    for command in (
        "autoform ready",
        "autoform claim acquire",
        "autoform check",
        "autoform audit",
    ):
        assert command in body
    assert "current host agent as the worker" in body
    assert "sole\norchestration and control interface" in body
    assert "autoform_worker" not in body
    assert "native specialist agents" not in body


def test_orchestrate_has_no_parallel_worker_implementation(repo_root: Path) -> None:
    assert not list((repo_root / "autoform_worker").glob("*.py"))
    assert not list((repo_root / "servers/prover").glob("*.py"))
    assert not list((repo_root / "agents").glob("*.md"))


def test_orchestrate_ui_metadata_invokes_the_skill(repo_root: Path) -> None:
    metadata = (repo_root / "skills/orchestrate/agents/openai.yaml").read_text(
        encoding="utf-8"
    )

    assert 'display_name: "Orchestrate"' in metadata
    assert "$orchestrate" in metadata


def test_orchestrate_integrates_before_releasing_the_claim(repo_root: Path) -> None:
    body = (repo_root / "skills/orchestrate/SKILL.md").read_text(encoding="utf-8")
    prose = " ".join(body.split())

    build = prose.index("Run a focused Lake build")
    review = prose.index("require an independent Agent Review")
    record = prose.index("After review acceptance")
    validate = prose.index("autoform audit")
    commit = prose.index("Commit the verified item")
    integrate = prose.index("verified commit has reached the authorized shared branch")
    release = prose.index("After integration, release the article claim")
    refresh = prose.index("rerun `autoform ready` from the updated shared base")
    assert build < review < record < validate < commit < integrate < release < refresh
    assert "repeat the focused build, independent Agent Review, metadata validation" in prose
