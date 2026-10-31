"""Line budgets for the docs agents read, so they cannot quietly grow back.

An agent skips a file that is too long, and then the rules in it do nothing.
"""

from __future__ import annotations

from pathlib import Path

# Cut prose before raising a budget, and say why in the change that raises it.
BUDGETS = {
    "AGENTS.md": 59,
    "CONTRIBUTING.md": 87,
    "docs/explorer-design.md": 65,  # Internal architecture and failure contracts, not another user guide.
    "README.md": 120,
    "autoform_cli/README.md": 1188,
    "autoform_cli/project/README.md": 120,
    "autoform_cli/reference/claims-and-revisions.md": 164,
    "autoform_cli/reference/open-statements.md": 186,
    "autoform_cli/reference/runtime-and-publication.md": 123,
    "autoform_cli/reference/search.md": 171,
    "autoform_cli/reference/validation-and-audit.md": 244,  # +5 for #485's list of the commands render captions.
    "autoform_cli/reference/wiki-links.md": 20,  # Internal optional viewer transport; keep it on demand and concise.
    "servers/README.md": 122,
    "skills/agent-review/SKILL.md": 65,
    "skills/debrief-orchestrator/SKILL.md": 47,
    "skills/develop-plugin/SKILL.md": 34,
    "skills/formalize/SKILL.md": 254,
    "skills/human-review/SKILL.md": 113,
    "skills/roadmap/SKILL.md": 257,
    "skills/setup/SKILL.md": 170,
}
# All skills/*/references/*.md together, so a new reference file cannot dodge a budget.
REFERENCES_BUDGET = 932


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
