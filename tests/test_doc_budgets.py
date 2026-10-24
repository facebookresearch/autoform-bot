"""Line budgets for the docs agents read, so they cannot quietly grow back.

An agent skips a file that is too long, and then the rules in it do nothing.
"""

from __future__ import annotations

from pathlib import Path

# Cut prose before raising a budget, and say why in the change that raises it.
BUDGETS = {
    "AGENTS.md": 55,
    "CONTRIBUTING.md": 82,
    "README.md": 113,
    "autoform_cli/README.md": 1131,
    "autoform_cli/project/README.md": 114,
    "autoform_cli/reference/claims-and-revisions.md": 157,
    "autoform_cli/reference/open-statements.md": 177,
    "autoform_cli/reference/runtime-and-publication.md": 117,
    "autoform_cli/reference/search.md": 162,
    "autoform_cli/reference/validation-and-audit.md": 227,
    "servers/README.md": 114,
    "skills/agent-review/SKILL.md": 61,
    "skills/debrief-orchestrator/SKILL.md": 44,
    "skills/develop-plugin/SKILL.md": 32,
    "skills/formalize/SKILL.md": 241,
    "skills/human-review/SKILL.md": 107,
    "skills/roadmap/SKILL.md": 232,
    "skills/setup/SKILL.md": 161,
}
# All skills/*/references/*.md together, so a new reference file cannot dodge a budget.
REFERENCES_BUDGET = 887


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_docs_stay_within_their_line_budgets(repo_root: Path) -> None:
    over = [
        f"{relative}: {count} lines, budget {budget} (+{count - budget})"
        for relative, budget in sorted(BUDGETS.items())
        if (count := _lines(repo_root / relative)) > budget
    ]
    references = sum(_lines(path) for path in (repo_root / "skills").glob("*/references/*.md"))
    if references > REFERENCES_BUDGET:
        excess = references - REFERENCES_BUDGET
        over.append(f"skills/*/references/*.md: {references} lines, budget {REFERENCES_BUDGET} (+{excess})")
    assert not over, "cut prose, or raise BUDGETS with a reason:\n" + "\n".join(over)


def test_every_skill_and_reference_page_has_a_budget(repo_root: Path) -> None:
    pages = [*(repo_root / "skills").glob("*/SKILL.md"), *(repo_root / "autoform_cli/reference").glob("*.md")]
    missing = sorted(relative for path in pages if (relative := path.relative_to(repo_root).as_posix()) not in BUDGETS)
    assert not missing, f"no line budget for {missing}"
