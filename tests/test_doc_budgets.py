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
    "docs/wiki-links.md": 20,  # Internal optional viewer transport; keep it on demand and concise.
    "README.md": 120,
    "autoform_cli/README.md": 412,  # K5: command contracts moved to their reference pages.
    "autoform_cli/project/README.md": 120,
    "autoform_cli/reference/abstraction.md": 37,  # New in #491; other docs' budgets fell by as much.
    "autoform_cli/reference/blueprint-format.md": 251,  # K5: the README's format sections.
    "autoform_cli/reference/claims-and-revisions.md": 164,
    "autoform_cli/reference/debrief.md": 45,
    "autoform_cli/reference/links.md": 26,  # New in #660; other docs' budgets fell by more.
    "autoform_cli/reference/open-statements.md": 168,
    "autoform_cli/reference/pinned-names.md": 41,
    "autoform_cli/reference/project-setup.md": 97,
    "autoform_cli/reference/runtime-and-publication.md": 122,
    "autoform_cli/reference/search.md": 173,
    "autoform_cli/reference/site.md": 114,
    "autoform_cli/reference/skeleton-and-review.md": 191,
    "autoform_cli/reference/validation-and-audit.md": 246,  # #193's credit rule; #485's list of captioned commands.
    "autoform_cli/reference/work.md": 125,
    "servers/README.md": 121,
    "skills/agent-review/SKILL.md": 63,
    "skills/debrief-orchestrator/SKILL.md": 44,
    "skills/develop-plugin/SKILL.md": 34,
    "skills/formalize/SKILL.md": 128,  # The leaf steps moved to references/leaf.md.
    "skills/human-review/SKILL.md": 110,
    "skills/roadmap/SKILL.md": 234,
    "skills/setup/SKILL.md": 167,  # A named donor's packages (#275).
}
# All skills/*/references/*.md together, so a new reference file cannot dodge a budget.
REFERENCES_BUDGET = 935


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
