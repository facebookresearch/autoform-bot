from __future__ import annotations

import re
from pathlib import Path


EXPECTED_AGENTS = {
    "autoform-worker.md",
    "content-reviewer.md",
    "counterexample-hunter.md",
    "graph-reviewer.md",
    "holistic-reviewer.md",
    "mathlib-checker.md",
    "prior-art-scout.md",
    "proof-strategy-researcher.md",
    "source-searcher.md",
}


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    assert match is not None, f"{path} has no complete YAML frontmatter"

    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line or line[0].isspace():
            continue
        key, separator, value = line.partition(":")
        assert separator, f"{path} has malformed frontmatter line: {line}"
        fields[key] = value.strip()
    return fields, match.group(2)


def _overlay_text(repo_root: Path) -> dict[Path, str]:
    paths = [repo_root / "skills/orchestrate/SKILL.md"]
    paths.extend(sorted((repo_root / "skills/orchestrate/references").glob("*.md")))
    paths.extend(sorted((repo_root / "agents").glob("*.md")))
    return {path: path.read_text(encoding="utf-8") for path in paths}


def _prose(text: str) -> str:
    """Normalize Markdown wrapping without weakening token-level assertions."""
    return " ".join(text.split())


def test_orchestrate_skill_teaches_canonical_markdown_and_claim_protocol(
    repo_root: Path,
) -> None:
    skill_path = repo_root / "skills/orchestrate/SKILL.md"
    metadata_path = repo_root / "skills/orchestrate/agents/openai.yaml"
    fields, skill = _frontmatter(skill_path)
    metadata = metadata_path.read_text(encoding="utf-8")
    prose = _prose(skill)

    assert fields["name"] == "orchestrate"
    assert "$orchestrate" in metadata
    for required in (
        "blueprint/roadmap/**/*.md",
        "## Depends on",
        "## Proof depends on",
        "autoform-runtime/v1",
        "read-only projection",
        "dispatchable, formalizable leaf articles",
        "autoform claim acquire",
        "autoform claim renew",
        "autoform claim release",
        "live peer lease",
        "malformed lease",
        "ownership is unproven",
        "stop all edits before committing",
        "lake-build",
        "separate Git worktrees",
        "absolute Lean project directory",
        "shared Lean LSP",
        "shared REPL",
        "focused `lake build` target",
        "statement: formalized",
        "proof: formalized",
        "exact compiled declaration",
        "derived and must not be authored",
    ):
        assert required in prose

    acquire = skill.index("autoform claim acquire")
    renew = skill.index("autoform claim renew")
    release = skill.index("autoform claim release")
    assert acquire < renew < release
    assert "Release the claim on success, failure, or handoff" in prose
    assert "Roadmap owns initial decomposition" in prose


def test_orchestrate_agents_have_narrow_write_ownership(repo_root: Path) -> None:
    agent_dir = repo_root / "agents"
    paths = sorted(agent_dir.glob("*.md"))
    assert {path.name for path in paths} == EXPECTED_AGENTS

    writable: list[str] = []
    for path in paths:
        fields, body = _frontmatter(path)
        assert fields["name"] == path.stem
        assert fields["writes"] in {"none", "lean-and-article"}
        assert body.strip()
        if fields["writes"] != "none":
            writable.append(path.name)

    assert writable == ["autoform-worker.md"]


def test_proof_worker_requires_claim_and_kernel_backed_validation(
    repo_root: Path,
) -> None:
    worker = (repo_root / "agents/autoform-worker.md").read_text(encoding="utf-8")
    prose = _prose(worker)

    for required in (
        "exactly one formalizable leaf",
        "verified node claim owned by this worker",
        "Do not begin editing without that ownership confirmation",
        "renewal failure or uncertain ownership",
        "stop editing and do not commit",
        "pinned local Mathlib checkout",
        "absolute project directory",
        "focused `lake build` target",
        "no `sorry`, `admit`, new `axiom`, `unsafe`,",
        "`partial`, `native_decide`",
        "Do not change the public statement solely to make a proof easy",
        "Never author derived readiness or completion",
        "PROVED` or `FAILED",
    ):
        assert required in prose


def test_read_only_agents_return_evidence_instead_of_racing_edits(
    repo_root: Path,
) -> None:
    agent_dir = repo_root / "agents"
    for name in EXPECTED_AGENTS - {"autoform-worker.md"}:
        path = agent_dir / name
        fields, body = _frontmatter(path)
        assert fields["writes"] == "none"
        assert re.search(r"[Dd]o not edit", body), f"{path} does not prohibit edits"

    content = (agent_dir / "content-reviewer.md").read_text(encoding="utf-8")
    graph = (agent_dir / "graph-reviewer.md").read_text(encoding="utf-8")
    mathlib = (agent_dir / "mathlib-checker.md").read_text(encoding="utf-8")
    counterexample = (agent_dir / "counterexample-hunter.md").read_text(encoding="utf-8")
    strategy = (agent_dir / "proof-strategy-researcher.md").read_text(encoding="utf-8")

    assert "source faithfulness" in content
    assert "Statement edges come from `## Depends on`; proof-only edges come from `## Proof depends on`" in _prose(graph)
    assert all(classification in mathlib for classification in ("`EXACT`", "`PARTIAL`", "`MISSING`"))
    assert all(classification in counterexample for classification in ("`REFUTED`", "`SUSPECT`", "`NO REFUTATION FOUND`"))
    assert "VERDICT: VIABLE" in strategy
    assert "without an unsupported gap" in strategy


def test_orchestrate_overlay_has_no_legacy_or_unsafe_prompt_contracts(
    repo_root: Path,
) -> None:
    forbidden = {
        "second graph artifact": r"graph\.json",
        "split prose store": r"informal_content",
        "removed repository scripts": r"(?:^|[ `/])scripts/",
        "removed runbooks": r"internal/runbooks",
        "dashboard operations": r"dashboard",
        "detached dispatcher": r"dispatch_runner",
        "legacy queue": r"\bqueue(?:d|s)?\b",
        "pull-request tending": r"\bgh pr\b|auto-merge|scoreboard",
        "sandbox bypass": r"dangerously-skip-permissions|danger-full-access|bypassPermissions|sandbox bypass",
        "setup delegation": r"\bSetup\b|skills/setup|\.\./setup",
        "legacy tier model": r"\btier-[123]\b|\btier [123]\b",
    }

    for path, text in _overlay_text(repo_root).items():
        for label, pattern in forbidden.items():
            assert re.search(pattern, text, re.IGNORECASE | re.MULTILINE) is None, (
                f"{path.relative_to(repo_root)} retains {label}"
            )


def test_orchestrate_markdown_links_resolve(repo_root: Path) -> None:
    skill_path = repo_root / "skills/orchestrate/SKILL.md"
    skill = skill_path.read_text(encoding="utf-8")

    links = re.findall(r"\[[^]]+\]\(([^)#]+)(?:#[^)]+)?\)", skill)
    assert links
    for link in links:
        target = (skill_path.parent / link).resolve()
        assert target.is_file(), f"broken Orchestrate link: {link}"
