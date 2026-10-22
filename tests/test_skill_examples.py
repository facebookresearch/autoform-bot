from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from autoform_cli.audit import audit_blueprint
from autoform_cli.graph import load_graph
from autoform_cli.lean import declaration_names, index_project
from autoform_cli.project.create import _MINIMUM_LEAN
from autoform_cli.render import render_site
from autoform_cli.status import derive
from servers.repl.core import ALLOWED_IMPORTS
from tests.test_lean_sources import _git_commit


_HREF = re.compile(r'href="([^"]+)"')
_EXAMPLE = Path("skills/setup/assets/cabannes-thesis-project")
_LEAN_FLOOR = "v" + ".".join(str(part) for part in _MINIMUM_LEAN)
# Upstream tag v4.32.0 of leanprover-community/repl points at this commit:
# `git ls-remote --tags https://github.com/leanprover-community/repl.git`
# lists refs/tags/v4.32.0 at it.
_REPL_V4_32_0_COMMIT = "68a3b3a059787a7db44fb1e6281e4a657efee470"


def test_root_readme_uses_the_canonical_repository(repo_root: Path) -> None:
    readme = (repo_root / "README.md").read_text(encoding="utf-8")

    assert "claude plugin marketplace add facebookresearch/autoform-bot" in readme
    assert (
        "codex plugin marketplace add facebookresearch/autoform-bot --ref main"
        in readme
    )
    assert "git clone https://github.com/facebookresearch/autoform-bot.git" in readme
    assert "`execution` branch" not in readme
    assert "VivienCabannes/autoform-bot" not in readme


def test_human_review_distinguishes_roadmap_progress_from_source_scope(
    repo_root: Path,
) -> None:
    skill = (repo_root / "skills/human-review/SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(skill.split())

    assert "`Fully proved` percentage" in skill
    assert "formalizable leaf targets only" in normalized
    assert "Module catalogs are non-dispatchable source" in normalized
    assert "separate neutral metric" in normalized
    assert "Completion includes every dependency recursively" in normalized
    assert "bodies for definitions" in skill
    assert "`mathlib: true` follows the authored status contract" in skill
    assert "not audit verification" in skill
    assert "never as whole-source completion" in skill
    assert "linked coverage contract" in skill
    assert "statement-only theorem remains incomplete" in skill
    assert "landing-page progress summary" in skill
    assert "autoform dashboard <PROJECT> --site-dir site" in skill
    assert "same site deployed to\nGitHub Pages" in skill
    assert "local-only live claim badges" in skill
    assert "full-page route" in normalized
    assert "move from the project's topics to a chapter" in normalized
    assert "smallest useful scope" in normalized
    assert "mathematical knowledge map, not navigation chrome" in normalized
    assert "Regions come only from authored `area:` or `blueprint/atlas.json`" in normalized
    assert "folders that only file or paginate pages never become regions" in normalized
    assert "Areas show as regions, containment as topics you open, and dependency as arrows" in normalized
    assert "no authored dependency edges says so" in normalized
    assert "overview, progress, project graph" not in skill


def test_generated_site_docs_describe_one_multiscale_explorer(repo_root: Path) -> None:
    cli, review = (
        " ".join((repo_root / relative).read_text(encoding="utf-8").split())
        for relative in ("autoform_cli/README.md", "skills/human-review/SKILL.md")
    )

    assert "one full-viewport reading/map workspace" in cli
    assert "Authored Mermaid remains supported in the vault and book" in cli
    assert "Their JSON projections and layout-free `dependencies/index.json` use the same shared client" in cli
    assert "`autoform-dag-view/v2` and `autoform-dag-search/v1`" in cli
    assert "Every generated site route uses the same" in review


def test_integrated_landing_retains_authored_title_with_generated_hide_metadata(tmp_path: Path) -> None:
    from autoform_cli.render import _integrated_page

    page = _integrated_page(
        '---\ntitle: "Authored mathematical library"\nhide:\n  - navigation\n---\n'
        '<div><h1>Authored mathematical library</h1></div>\n',
        page=tmp_path / "README.md", destination=tmp_path,
        payload=tmp_path / "dependencies.json", scope="roadmap",
    )
    assert 'data-page-title="Authored mathematical library"' in page
    assert 'data-page-title="Index"' not in page


def test_the_example_uses_nothing_newer_than_its_autoform_pin(repo_root: Path) -> None:
    """The example's CI runs its pinned AUTOFORM_REF, 0288506b, which predates catalogs.

    That check rejects `catalog:` and `area:` and that render rejects
    INVENTORIED, while it ignores atlas.json, and its render draws Mermaid maps
    through the global Mermaid script, so mkdocs.yml keeps that line. Move the
    pin, here and in the workflow equivalence test, before relaxing this.
    """
    example = repo_root / _EXAMPLE
    for page in (example / "blueprint/roadmap").rglob("*.md"):
        text = page.read_text(encoding="utf-8")
        frontmatter = text[4:].split("\n---", 1)[0] if text.startswith("---\n") else ""
        assert not re.search(r"^(?:area|catalog):", frontmatter, re.MULTILINE), page
    assert "mermaid@11.4.1/dist/mermaid.min.js" in (example / "mkdocs.yml").read_text(encoding="utf-8")
    assert "INVENTORIED" not in (example / "blueprint/coverage/README.md").read_text(encoding="utf-8")
    assert "dependency maps of the project and each chapter" in " ".join(
        (example / "README.md").read_text(encoding="utf-8").split()
    )
    assert "derived statuses, dependency maps," in (example / ".github/workflows/blueprint-pages.yml").read_text(
        encoding="utf-8"
    )


def test_formalize_replaces_custom_orchestration_with_the_markdown_frontier(
    repo_root: Path,
) -> None:
    skill = (repo_root / "skills/formalize/SKILL.md").read_text(encoding="utf-8")
    metadata = (repo_root / "skills/formalize/agents/openai.yaml").read_text(encoding="utf-8")
    codex = (repo_root / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
    muse = (repo_root / ".muse-plugin/plugin.json").read_text(encoding="utf-8")
    normalized = " ".join(skill.split())

    for required in (
        "autoform work list",
        "autoform work context",
        "claim_target",
        "separate Git worktrees",
        "shared Lean LSP and REPL",
        "`blueprint/.implementation-notes/<article_id>.md`",
        "returns to Roadmap",
    ):
        assert required in skill
    assert "no custom scheduler or provider adapter" in normalized
    assert "ready, running, retrying, failed, or blocked scheduler states" in normalized
    assert "never a transcript or retry counter" in normalized
    assert (
        "require the same `phase`, `blockers`, `dependencies`, `article_revision`, "
        "`open_statements`, `assumes`, and `revision` as the first read"
    ) in normalized
    assert (
        "the same `phase`, `article_revision`, `open_statements`, `assumes`, and "
        "`revision` it was dispatched with"
    ) in normalized
    assert "$formalize" in metadata
    assert "Formalize the ready Markdown roadmap frontier" in codex
    assert '"id": "formalize"' in muse


def test_import_edits_use_reported_scope_instead_of_a_blanket_ban(
    repo_root: Path,
) -> None:
    paths = (
        "AGENTS.md",
        "skills/formalize/SKILL.md",
        "skills/develop-plugin/SKILL.md",
        "autoform_cli/README.md",
    )
    documents = {
        path: (repo_root / path).read_text(encoding="utf-8") for path in paths
    }

    for path, document in documents.items():
        assert "autoform work import-impact" in " ".join(document.split()), path

    for path in ("AGENTS.md", "skills/formalize/SKILL.md"):
        assert "blanket import" in " ".join(documents[path].split()), path

    cli = " ".join(documents["autoform_cli/README.md"].split())
    assert (
        "autoform work import-impact SELECTOR MODULE [TARGET] --lean-root PATH "
        "[--json] [--timeout SECONDS]"
    ) in cli
    assert "advisory and does not edit authored roadmap or Lean source" in cli
    assert "freshness check may refresh build hash sidecars" in cli
    assert "`autoform-module-import-impact/v1`" in cli
    assert "`module_scope` is `loaded-root-library-graph`" in cli
    assert "`module_claim_target`" in cli
    assert "`lean-module/<lowercase-slug>-<sha256[:16]>`" in cli
    assert "The import edit's `claim_targets` contain only the selected article" in cli
    assert "`affected_claim_targets`" in cli
    assert "downstream warnings, not pre-emptive ownership requirements" in cli
    assert "`claim_keys`" in cli
    assert "`affected_claim_keys`" in cli
    assert "`module_graph_revision`" in cli
    assert "captured root source and configured root-package artifact" in cli
    assert "complete over the root libraries' loaded Lake roots/globs" in cli
    assert "not dormant source files outside that build boundary" in cli
    assert "`unresolved_articles`" in cli
    assert "without globally blocking unrelated edits" in cli
    assert "`affected_unowned_helpers` entry includes its own claim target and key" in cli
    assert "`module_claim_key` is the claim key of `module_claim_target`" in cli
    assert "names, or owns through the parent chain, a declaration in an affected module" in cli
    assert "`selected_affected` says whether the selected article is one of them" in cli
    assert "`article_local` holds only when it is, `affected_claim_targets` is empty" in cli
    assert "`autoform claim list --worker-id WORKER`" in cli
    assert "Imports are allowed regardless of reported breadth" in cli
    assert "Formalize import workflow" in cli

    formalize = " ".join(documents["skills/formalize/SKILL.md"].split())
    assert "Lean imports are allowed; do not pre-block" in formalize
    assert "Do not acquire or edit them merely because they are listed" in formalize
    assert "Capture baseline skeleton evidence" in formalize
    assert "Disclosure alone is not a conflict or failed validation" in formalize
    assert "[import claims](references/claims.md#import-edits)" in formalize
    assert (
        "then repair each under the review gates of the "
        "[revision contract](../../autoform_cli/README.md#revision-contract)"
    ) in formalize
    assert "repairing another article's Lean that an [import edit](#import-edits) breaks" in formalize
    assert "a change another article needs beyond an import repair" in formalize

    claims = " ".join((repo_root / "skills/formalize/references/claims.md").read_text(encoding="utf-8").split())
    assert "already-held same-worker article claim is upgraded, not released first" in claims
    assert "best-effort coordination, not a lock" in claims
    assert "malformed evidence is unverifiable" in claims
    assert "An expired valid claim is not active" in claims
    assert "atomically acquire the full held set plus its targets" in claims
    assert "On a refused import-claim acquisition or a recheck conflict" in claims
    assert "release the complete currently held import set before waiting or reporting" in claims
    assert "unless handing off an existing verified candidate" in claims
    assert "Renew, hand off, and release the full import claim set together" in claims
    assert "Recheck affected keys after acquisition and before integration" in claims
    assert "Before repairing any article that actually needs changes, listed as affected or not" in claims

    agents = " ".join(documents["AGENTS.md"].split())
    assert "consumer formalization work" in agents
    assert "active conflicts" in agents

    development = " ".join(documents["skills/develop-plugin/SKILL.md"].split())
    assert "Keep imports permissive" in development
    assert "plugin changes preserve it" in development


def test_claims_are_optional_and_builds_take_none(repo_root: Path) -> None:
    """A run's lead keeps its workers apart, so claims are only for other people and sessions.

    A remote claim around `lake build` made unrelated machines wait on each other,
    while agents on one machine using different boards still downloaded into one
    Mathlib cache at once (#121). Builds take no claim, and the lead fills the cache
    first and asks the user if it cannot.
    """

    def read(relative: str) -> str:
        return " ".join((repo_root / relative).read_text(encoding="utf-8").split())

    formalize = read("skills/formalize/SKILL.md")
    cli = read("autoform_cli/README.md")
    assert "Claims are optional" in formalize
    assert "Within one run the lead keeps its workers apart" in formalize
    assert "no two workers, the lead included, hold the same target at once" in formalize
    assert (
        "A worker holds its article's `claim_target` and the `claim_targets` the lead confirmed until its work "
        "lands or it abandons the leaf without a candidate, so a handoff keeps them"
    ) in formalize
    assert "reports the new targets to the lead before editing" in formalize
    assert "goes ahead only once the lead confirms that no other worker holds them" in formalize
    assert "A subagent that cannot wait for the lead's answer commits its work, stops, and reports instead" in formalize
    assert "the lead answers when it dispatches the leaf again from that commit" in formalize
    assert "otherwise send the lead the report's `claim_targets` and `affected_claim_targets`" in formalize
    assert "In a run without claims, send the lead that report's `affected_claim_targets`" in formalize
    assert "land the edit only once the lead confirms that no other worker holds them" in formalize
    assert "without the lead's answer, or when integration fails, report the branch and commit" in formalize
    assert "so capture a fresh baseline of that article on the shared branch" in formalize
    assert "If the rerun fails too, ask the user before creating worktrees" in formalize
    assert "dispatch no more at once than its memory can build for" in formalize
    assert "Before choosing none, run `autoform claim list` once" in formalize
    assert "an unexpired claim on the board means another session uses them" in formalize
    assert "if the command fails, as it does in a checkout without an `origin`, tell the user" in formalize
    assert "the lead checks them when it confirms the edit's new targets and again before the edit lands" in formalize
    assert "If workers would wait on each other, the lead has one of them give up what it holds" in formalize
    assert "re-run `autoform work import-impact` on the result; if integration brought in a change" in formalize
    assert "A claim excludes only workers that also claim" in formalize
    assert "When the run uses claims, tell each subagent so" in formalize
    assert "Use claims only when other people or sessions may work on the same project at the same time" in formalize
    assert "[claim rules](references/claims.md)" in formalize
    assert "once in the lead's checkout before creating worktrees" in formalize
    assert "Claims are optional" in cli
    assert "builds take no claim" in cli
    assert "use them when other people or sessions may work on the same project at the same time" in cli
    assert "Without claims, a Formalize subagent has its lead confirm the set instead" in cli
    assert "or, without claims, after the lead confirms them" in cli
    assert "Claims are optional" in read("AGENTS.md")
    claims = (repo_root / "skills/formalize/references/claims.md").read_text(encoding="utf-8")
    assert "They replace asking the lead" in " ".join(claims.split())
    headings = {line[3:].lower().replace(" ", "-") for line in claims.splitlines() if line.startswith("## ")}
    anchors = re.findall(r"references/claims\.md#([a-z-]+)", formalize)
    assert {"acquire-and-renew", "import-edits", "integration-and-handoff"} <= set(anchors), anchors
    assert set(anchors) <= headings, set(anchors) - headings
    for relative in (
        "skills/formalize/SKILL.md",
        "skills/formalize/references/claims.md",
        "skills/formalize/references/library-search.md",
        "autoform_cli/README.md",
    ):
        assert "lake-build" not in read(relative), relative


def test_readme_tells_users_how_to_run_several_sessions(repo_root: Path) -> None:
    """A session's agent limit is not the project's: several sessions share one claim board (#421).

    Sessions started together each see an empty board and would choose no claims,
    so only the user's request can tell a session that others share the project.
    """
    readme = " ".join((repo_root / "README.md").read_text(encoding="utf-8").split())
    assert "## Run several sessions" in readme
    assert "own clone of the project, with the same `origin` and the same branch" in readme
    assert "Tell each session that other sessions share the project" in readme
    assert "push claims and verified results to `origin`" in readme
    assert "each one sees an empty claim board and would otherwise work without claims" in readme
    assert "raise `max_threads` under `[agents]` in `~/.codex/config.toml`" in readme
    assert "They also share one Lean runtime" in readme


def test_implementation_notes_are_hidden_and_keyed_by_article_id(repo_root: Path) -> None:
    """Notes written into articles crept into published books as Lean commentary.

    One hidden file per durable article keeps implementation work out of the
    mathematical wiki without creating a shared merge hotspot.
    """

    def read(relative: str) -> str:
        return " ".join((repo_root / relative).read_text(encoding="utf-8").split())

    formalize = read("skills/formalize/SKILL.md")
    roadmap = read("skills/roadmap/SKILL.md")
    reference = read("autoform_cli/README.md")
    guidance = read("AGENTS.md")
    rubric = read("skills/agent-review/references/roadmap-quality.md")

    assert "Read the complete article, its `blueprint/.implementation-notes/<article_id>.md`" in formalize
    assert "The article keeps only frontmatter and mathematics." in formalize
    assert "Write notes only in the selected article's `blueprint/.implementation-notes/<article_id>.md`" in formalize
    assert "one file per article avoids cross-worker merge hotspots" in formalize
    assert "Delete the file once the article is proved" in formalize
    assert "Delete every note the audit reports as `stale-implementation-note`" in formalize
    assert "even though it belongs to another article" in formalize
    assert "delete every note the audit now reports as stale in a follow-up commit" in formalize
    assert '"stale-implementation-note"' in read("autoform_cli/audit.py")
    assert "never a secret or machine-local path" in formalize
    assert "Never link the hidden note from an article" in formalize
    assert "Articles hold only frontmatter and informal mathematics" in roadmap
    assert "`blueprint/.implementation-notes/<article_id>.md`" in roadmap
    assert "Delete it when the article is proved or marked `mathlib: true`" in roadmap
    assert (
        "delete a container's note when every formalizable descendant, and every leaf marked `not_ready` "
        "beneath it, is proved and there is at least one; a module catalog never counts"
        in roadmap
    )
    assert "an orphaned note blocks every command that loads the graph" in roadmap
    assert "any worker may repair one the same way" in roadmap
    assert "never a transcript, a secret, or a machine-local path" in roadmap
    assert (
        "containers have no claims, and any worker may delete a note that `autoform audit` reports as stale"
        in reference
    )
    assert "When an article that still exists lost its `article_id` line, restore the line" in formalize
    assert "delete the orphan, and commit. Any worker may make this repair" in formalize
    assert "The exceptions are deleting stale implementation notes and repairing orphaned ones" in formalize
    assert "Parallel branches can orphan a note without a Git conflict" in reference
    assert "reports as stale or repair an orphaned one" in reference
    assert "When the article still exists but lost its `article_id` line, restore the line instead" in reference
    assert "Versioned implementation notes live outside the mathematical roadmap" in reference
    assert (
        "delete it once every formalizable descendant, and every leaf marked `not_ready` beneath it, "
        "is proved and there is at least one; a module catalog never counts" in reference
    )
    assert "Record what happened in the revision commit and report" in reference
    assert (
        "Use an implementation note only until [its deletion rule](#articles-and-containment) applies"
        in reference
    )
    assert "Markdown under `blueprint/roadmap/` is the authored roadmap" in guidance
    assert "files under `blueprint/.implementation-notes/`" in guidance
    assert "with its hidden `blueprint/.implementation-notes/<article_id>.md` when present" in rubric
    assert "Uncertainty, Mathlib candidates, and unresolved source questions are explicit" in rubric
    assert (
        "Mathlib candidates and other Lean-side findings are in the article's implementation note, "
        "never in the article"
    ) in rubric
    for text in (formalize, roadmap, reference, rubric):
        assert "Execution notes" not in text
        assert "`agents.md`" not in text


def test_development_guidance_requires_fail_closed_local_safety(repo_root: Path) -> None:
    development = (repo_root / "skills" / "develop-plugin" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    contracts = (
        repo_root / "skills" / "develop-plugin" / "references" / "repository-contracts.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(development.split())
    contract_text = " ".join(contracts.split())

    assert "private declaration safety as fail-closed evidence" in normalized
    assert "official user name" in normalized
    assert "by source coordinates" in normalized
    assert "regenerate `production_module_roots`" in normalized
    assert "from Lake package configs" in normalized
    assert "private creation bundle, catalog identity" in normalized
    assert "complete `lake update` manifest" in normalized
    assert "direct-Mathlib-only manifest is invalid" in normalized
    assert "repeated pathname reads are not a generation boundary" in normalized
    assert "marker schema in its owning feature" in normalized
    assert "match the blob at the stable detected commit" in normalized
    assert "Full-viewport knowledge maps" in contract_text
    assert "authored mathematical areas and prose to drive regions and reading panels" in contract_text
    assert "containment and dependency distinct" in contract_text
    assert "regions come only from authored areas, never from folders or paths" in contract_text
    assert "Never fit the whole repository" in contract_text
    assert "Run `make test-browser` for any explorer change" in contract_text


def test_development_guidance_uses_progressive_command_reference(repo_root: Path) -> None:
    development = (repo_root / "skills/develop-plugin/SKILL.md").read_text(
        encoding="utf-8"
    )
    cli_reference = (repo_root / "autoform_cli/README.md").read_text(encoding="utf-8")
    inspection_reference = (repo_root / "autoform_cli/project/README.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(development.split())

    assert "plugin and formalization roots distinct" in normalized
    assert "shared agent entrypoints concise" in normalized
    assert "on-demand references" in normalized
    assert "[project-inspection reference](project/README.md)" in cli_reference
    assert "| `target-unreadable` |" not in cli_reference
    assert "| `target-unreadable` |" in inspection_reference


def test_development_guidance_routes_repository_contracts(repo_root: Path) -> None:
    skill_path = repo_root / "skills/develop-plugin/SKILL.md"
    contracts_path = skill_path.parent / "references/repository-contracts.md"
    skill = skill_path.read_text(encoding="utf-8")

    assert "[repository contracts](references/repository-contracts.md)" in skill
    assert "pins/releases/explorers, read" in skill
    assert contracts_path.is_file()


def test_root_agent_guidance_routes_to_owning_documents(repo_root: Path) -> None:
    guidance = (repo_root / "AGENTS.md").read_text(encoding="utf-8")

    for relative in ("CONTRIBUTING.md", "skills/develop-plugin/SKILL.md"):
        assert f"]({relative})" in guidance
        assert (repo_root / relative).is_file()

    normalized_guidance = " ".join(guidance.split()).lower()
    assert "identify the concrete user outcome or maintenance burden" in normalized_guidance
    assert "review blockers does not prove that the proposal belongs" in normalized_guidance
    assert "validate their own planning state" in normalized_guidance
    assert "only when authorized" in normalized_guidance
    assert "repository quality" in normalized_guidance
    assert "line count" in normalized_guidance
    assert "pr count" in normalized_guidance
    assert all(word in normalized_guidance for word in ("closing", "deleting", "replacing"))
    assert "otherwise recommend" in normalized_guidance
    assert "success" in normalized_guidance


def test_agent_review_treats_skeleton_hashes_as_advisory(repo_root: Path) -> None:
    review = (repo_root / "skills" / "agent-review" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(review.split())

    assert "not reviewer authentication or an approval key" in normalized
    assert "report as advisory" in normalized


def test_quick_start_keeps_the_cli_agent_facing(repo_root: Path) -> None:
    readme = (repo_root / "README.md").read_text(encoding="utf-8")
    quick_start = readme.split("## Quick start", 1)[1].split("## Blueprint model", 1)[0]
    normalized = " ".join(quick_start.split())

    assert "agent window" in normalized
    assert "users do not need to learn or run its commands" in normalized
    assert "/autoform:setup" in quick_start
    assert "uv run autoform" not in quick_start


def test_public_repl_calls_are_documented_as_process_disposable(repo_root: Path) -> None:
    servers = " ".join((repo_root / "servers/README.md").read_text(encoding="utf-8").split())
    setup = " ".join((repo_root / "skills/setup/SKILL.md").read_text(encoding="utf-8").split())

    assert "one REPL slot pool" in servers
    assert "each public REPL call gets a fresh child" in servers
    assert "no process-owned environment or proof-state handle survives" in servers
    assert "toolchain runs Lean's own header" in servers
    assert "unrecognized parser response fails closed" in servers
    for root in sorted(ALLOWED_IMPORTS):
        assert f"`{root}`" in servers, root
    assert "(a plain `Init` import is ignored)" in servers
    assert "names that remain quoted or path-like after Lean parsing are refused" in servers
    assert "The allowlist is a policy check, not a sandbox" in servers
    assert "`Init` alone provides file and process IO" in servers
    assert "package-qualified `@repl/repl` target" in servers
    assert "Every REPL call starts Lean afresh" in servers
    assert "so it takes at least as long as `import Mathlib` on the node" in servers
    assert "run one at a time unless `AUTOFORM_REPL_WORKERS_PER_PROJECT` is raised" in servers
    assert "A REPL worker is one concurrent call slot; an idle slot holds no Lean process" in servers
    assert "finds the REPL only through a require named `repl`" in servers
    assert (
        "rename the require to `repl`, or restart the runtime with "
        '`LEAN_REPL_CMD="lake exe @<name>/repl"` for a require named `<name>`, '
        "which applies to every project the runtime serves"
    ) in servers
    assert (
        "A project with no REPL require, as `autoform project new` and `init` leave it, "
        "fails every REPL call until it adds one as the "
        "[setup skill](../skills/setup/SKILL.md) describes"
    ) in servers
    assert "When `LEAN_REPL_HEADER_CMD` is unset and `LEAN_REPL_CMD` starts with a `lake` executable" in servers
    assert "header validation reuses that executable without the command's other arguments" in servers
    assert "`LEAN_REPL_CMD` and `LEAN_REPL_HEADER_CMD` are node-wide and read only when the runtime starts" in servers
    assert "after changing either, run `uv run autoform-lean-runtime stop`" in servers
    assert (
        "The next process to start the runtime, an MCP adapter serving a tool call or "
        "`uv run autoform-lean-runtime start`, must have the new values in its environment"
    ) in servers
    assert "`uv run autoform-lean-runtime status` shows the resolved commands under `config`" in servers
    assert "one total post-admission budget" in servers
    assert "it and its cap default to 240 seconds" in servers
    assert "its REPL slot pool is cached" in servers
    assert "does not mean a REPL subprocess remains resident" in servers
    assert "`LEAN_REPL_HEADER_CMD`" in servers
    assert "is the lower of 240 seconds and that cap" in servers
    assert "If `LEAN_REPL_CMD` does not start with a `lake` executable" in servers
    assert (
        "`AUTOFORM_REPL_REQUEST_TIMEOUT` exceeds that cap, or if that cap does not fit "
        "the response budget below, the runtime refuses to start"
    ) in servers
    assert "`AUTOFORM_RUNTIME_RESPONSE_TIMEOUT` (default 15 minutes)" in servers
    assert "`leanprover-community/repl` at an immutable revision" in setup
    assert "the commit of the upstream tag that names the project's Lean version" in setup
    assert "failing that, its `vX.Y.0` release" in setup
    assert "git ls-remote --tags https://github.com/leanprover-community/repl.git" in setup
    assert "if neither exists, ask the user instead of guessing" in setup
    assert "Name the require `repl`" in setup
    assert "which finds the package only under its require name" in setup
    assert "run `lake update repl`" in setup
    assert "commit the manifest" in setup
    assert "lake build @repl/repl" in setup


def test_setup_example_pins_the_repl_that_the_real_repl_job_tests(repo_root: Path) -> None:
    # The real-REPL CI job builds this fixture, so the example's REPL pin is
    # tested only while both pin the same revision on the same toolchain.
    fixture = repo_root / "tests/fixtures/repl-smoke"
    example = repo_root / _EXAMPLE

    def repl_revs(project: Path) -> tuple[str, str]:
        lakefile = tomllib.loads((project / "lakefile.toml").read_text(encoding="utf-8"))
        manifest = json.loads((project / "lake-manifest.json").read_text(encoding="utf-8"))
        required = next(entry["rev"] for entry in lakefile["require"] if entry["name"] == "repl")
        locked = next(entry["rev"] for entry in manifest["packages"] if entry["name"] == "repl")
        return required, locked

    def toolchain(project: Path) -> str:
        return (project / "lean-toolchain").read_text(encoding="utf-8").strip()

    for project in (example, fixture):
        assert repl_revs(project) == (_REPL_V4_32_0_COMMIT, _REPL_V4_32_0_COMMIT), project
    assert toolchain(example) == toolchain(fixture)
    # The docs name the tag rather than the commit, so they hold only while
    # every pin above is that tag's commit.
    setup = " ".join((repo_root / "skills/setup/SKILL.md").read_text(encoding="utf-8").split())
    readme = " ".join((example / "README.md").read_text(encoding="utf-8").split())
    assert "the bundled example pins the commit of tag `v4.32.0`" in setup
    assert "the upstream REPL commit tagged `v4.32.0`" in readme


def test_minimum_lean_fixture_and_readme_name_the_lean_floor(repo_root: Path) -> None:
    # The real-REPL CI job checks the header parser on this fixture's
    # toolchain as the oldest Lean Autoform supports, which holds only while
    # it names the same release as _MINIMUM_LEAN and the README.
    fixture = repo_root / "tests/fixtures/minimum-lean/lean-toolchain"
    readme = " ".join((repo_root / "README.md").read_text(encoding="utf-8").split())

    assert fixture.read_text(encoding="utf-8").strip() == f"leanprover/lean4:{_LEAN_FLOOR}"
    assert f"Lean 4 {_LEAN_FLOOR} or newer" in readme


def test_setup_guidance_uses_the_offline_atomic_project_creator(repo_root: Path) -> None:
    setup_root = repo_root / "skills" / "setup"
    setup = (setup_root / "SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(setup.split())

    assert (
        'uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform project inspect '
        '"$PROJECT" --json'
    ) in setup
    assert (
        'uv run --project "$AUTOFORM_PLUGIN_ROOT" autoform project new '
        '"$TARGET" --package "$PACKAGE"'
    ) in setup
    assert "never overwrites an existing target" in normalized
    assert "without running Lake, Lean, or network operations" in normalized
    assert "complete Autoform vault, site, ignore rules, and pinnable CI" in normalized
    assert "no later `init` is needed" in normalized
    assert "Every component of the target parent must be a real directory, not a symlink" in normalized
    assert "on macOS use `/private/tmp`, not the `/tmp` alias" in normalized
    assert "pins generated workflows exactly as `init` does" in normalized
    assert "cached remote-tracking ref contains that commit" in normalized
    assert "prefers Autoform's canonical repository" in normalized
    assert "only when tracked files are clean" in normalized
    assert "bounded, regular, link-free required template snapshot" in normalized
    assert "executable-bit classification" in normalized
    assert "Git replacement objects" in normalized
    assert "installed copy must also match its marketplace checkout" in normalized
    assert "fails closed" in normalized and "including Windows" in normalized
    assert "Do not invent version pairs" not in normalized
    assert "--lean-toolchain" in normalized
    assert "lake update" in normalized
    assert f"Autoform needs Lean {_LEAN_FLOOR} or newer" in normalized
    assert "the project will depend on a Lean library outside the catalog, create the project on that library's Lean and Mathlib" in normalized
    # Lean alone does not pick the release: a library may lock a Mathlib commit that is not the tag.
    assert "the Mathlib `rev` its `lake-manifest.json` locks, at the revision the user plans to require" in normalized
    assert "Pass both, as `--lean-toolchain` and `--mathlib-rev`" in normalized
    assert "ask the user instead of guessing" in normalized
    assert "Say which pair you chose and why in the setup report" in normalized
    assert "Adding the `require` stays the user's decision" in normalized
    assert (
        "appending only missing Autoform rules through a retained bounded regular root"
        in normalized
    )
    assert "scripts/workspace_inspector.py" not in setup
    assert "scripts/make_project.sh" not in setup
    for document in sorted(setup_root.rglob("*.md")):
        text = document.read_text(encoding="utf-8")
        for reference in re.findall(r"(?<![A-Za-z0-9_])scripts/[A-Za-z0-9_./-]+", text):
            assert (repo_root / reference).is_file(), (
                f"{document.relative_to(repo_root)} references missing helper {reference}"
            )


def test_setup_offers_catalog_libraries_only_to_new_projects_on_a_yes(repo_root: Path) -> None:
    setup = (repo_root / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(setup.split())

    assert "autoform_cli/project/libraries.json" in normalized
    # Which libraries, and to which projects.
    assert "only when its entry has `offered: true`" in normalized
    assert "only when Setup creates a new project with `project new`" in normalized
    assert "never when inspecting, repairing, or running `init` on an existing repository" in normalized
    assert "a request that names a catalog library uses it even when the entry is not offered" in normalized
    # The cost, stated in the question.
    assert "Ask in the single round of questions, and state the cost in the question" in normalized
    assert "moves from the recommended release to the library's `release`" in normalized
    assert "needs network access and downloads the library and its dependencies (about 270 MB for ATLAS)" in normalized
    assert "built locally" in normalized
    # Where a yes may come from, and the default.
    assert "in the setup request itself" in normalized
    assert "Without a yes, add no library, including in a headless run" in normalized
    # What a yes runs and reports.
    assert "run the `project new` command above with `--library <NAME>` and no version flags" in normalized
    assert "Then run `autoform library list` on the project" in normalized
    assert "When it lists the library as usable, report the entry's `git` and pinned commit" in normalized
    assert "otherwise report its reason and do not claim the library works" in normalized
    assert "say that the project builds once the fetch succeeds, and give the retry command" in normalized
    assert "Never run a bare `lake update` in such a project" in normalized
    # A failed fetch resumes the validation sequence where it stopped, so a
    # failed `lake update repl` is retried as such rather than as a bare update.
    assert "resume the validation sequence from the step that failed" in normalized
    assert "`lake update repl` when the REPL require was added" in normalized
    # The catalog pin is used only when the request names no other source.
    assert "names no other repository or revision for it" in normalized
    assert "`<NAME>` is the entry's `name` exactly, such as `atlas`" in normalized


def test_setup_asset_is_a_repo_shaped_thesis_vault(repo_root: Path) -> None:
    example = repo_root / _EXAMPLE
    blueprint = example / "blueprint"
    graph = load_graph(blueprint)

    assert set(graph.nodes) == {
        "roadmap",
        "infimum-loss",
        "full-supervision",
        "infimum-loss/definitions/eligibility",
        "infimum-loss/definitions/non-ambiguity",
        "infimum-loss/theorems/infimum-loss",
        "infimum-loss/theorems/non-ambiguity-determinism",
        "infimum-loss/theorems/supervision-recovery",
        "full-supervision/definitions/full-supervision",
        "infimum-loss/theorems/supervision-non-ambiguous",
    }
    assert graph.edge_count == 9
    assert graph.nodes["roadmap"].parent is None
    assert graph.nodes["infimum-loss"].parent == "roadmap"
    assert graph.nodes["full-supervision"].parent == "roadmap"
    formalizable = {node.id for node in graph.nodes.values() if node.formalizable}
    assert len(formalizable) == 7
    assert all(graph.nodes[node_id].origin == "cited" for node_id in formalizable)
    eligibility = graph.nodes["infimum-loss/definitions/eligibility"]
    assert eligibility.declaration == "def"
    assert eligibility.statement_formalized
    assert eligibility.lean == "CabannesThesis.Eligible"
    recovery = graph.nodes["infimum-loss/theorems/supervision-recovery"]
    assert recovery.statement_dependencies == (
        "infimum-loss/theorems/infimum-loss",
        "full-supervision/definitions/full-supervision",
    )
    assert recovery.proof_dependencies == (
        "infimum-loss/theorems/non-ambiguity-determinism",
        "infimum-loss/theorems/supervision-non-ambiguous",
    )

    # The example exercises two real book chapters and an honest cross-chapter
    # boundary: the reusable support result is proved, while the stronger
    # source theorem remains planned and the infimum result is ready to state.
    statuses = derive(graph)
    assert statuses["infimum-loss/theorems/supervision-recovery"].key == "planned"
    assert statuses["infimum-loss/theorems/infimum-loss"].key == "can_state"
    assert statuses["infimum-loss/definitions/eligibility"].key == "fully_proved"
    assert statuses["infimum-loss/theorems/supervision-non-ambiguous"].key == (
        "fully_proved"
    )

    # Every declaration a node claims must exist in the project's Lean sources.
    index = index_project(example)
    for node in graph.nodes.values():
        for name in declaration_names(node.lean or ""):
            assert index.find(name) is not None, f"{node.id}: {name}"

    assert (blueprint / "roadmap" / "README.md").is_file()
    assert (blueprint / "roadmap" / "full-supervision" / "README.md").is_file()
    assert (blueprint / "coverage" / "README.md").is_file()
    source = (blueprint / "sources" / "thesis.md").read_text(encoding="utf-8")
    assert "arXiv:2209.11629" in source
    assert "infimum/core.tex" in source
    assert "il:thm:ambiguity" in source
    assert "il:thm:non-ambiguity" in source
    ignored = (blueprint / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".obsidian/" in ignored
    assert "dependencies.md" in ignored
    assert "structure.md" not in ignored

    overview = (blueprint / "README.md").read_text(encoding="utf-8")
    assert "kind: blueprint" in overview
    assert "status: active" in overview
    assert "[Thesis roadmap](roadmap/README.md)" in overview
    assert "[coverage notes](coverage/README.md)" in overview

    readme = (example / "README.md").read_text(encoding="utf-8")
    assert "[Browse the formalization blueprint](blueprint/README.md)" in readme
    assert (
        "Developed with "
        "[AutoformBot](https://github.com/facebookresearch/autoform-bot)."
    ) in readme
    assert ".claude/worktrees/" in (example / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines()
    assert (example / "src/CabannesThesis.lean").is_file()
    assert (example / "src/CabannesThesis/Basic.lean").is_file()
    toolchain = (example / "lean-toolchain").read_text(encoding="utf-8").strip()
    lakefile = tomllib.loads((example / "lakefile.toml").read_text(encoding="utf-8"))
    manifest = json.loads((example / "lake-manifest.json").read_text(encoding="utf-8"))
    assert toolchain == "leanprover/lean4:v4.32.2"
    requirements = {entry["name"]: entry for entry in lakefile["require"]}
    packages = {entry["name"]: entry for entry in manifest["packages"]}
    assert requirements["mathlib"]["rev"] == "v4.32.2"
    assert requirements["repl"]["rev"] == packages["repl"]["inputRev"]
    assert packages["repl"]["rev"] == requirements["repl"]["rev"]
    assert lakefile["lean_lib"][0]["srcDir"] == "src"


def test_roadmap_example_is_structural_not_a_completion_fixture(
    repo_root: Path,
) -> None:
    example = repo_root / _EXAMPLE
    audit = audit_blueprint(example / "blueprint", lean_root=example)
    assert audit.coverage is not None
    assert not audit.coverage.complete
    assert audit.coverage.counts["MAPPED"] > 0

    reference = (
        repo_root / "skills/roadmap/references/cabannes-thesis-roadmap.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(reference.split())
    assert "structural example, not a completion fixture" in normalized
    assert "not a valid finish state" in normalized
    assert "approved small slice" not in normalized
    assert "handing ready nodes to Orchestrate" not in normalized


def test_setup_asset_static_site_contract(repo_root: Path, tmp_path: Path) -> None:
    example = tmp_path / "consumer-project"  # links need a commit holding the rendered inputs
    shutil.copytree(repo_root / _EXAMPLE, example)
    subprocess.run(["git", "init", "-q"], cwd=example, check=True)
    ref = _git_commit(example, "consumer fixture")
    site = tmp_path / "site-src"

    report = render_site(
        example / "blueprint",
        site,
        lean_root=example,
        repository_url="https://github.com/owner/repo",
        ref=ref,
    )

    assert report.unresolved == []
    manifest = json.loads((site / "publication.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "autoform-publication/v3"
    assert manifest["nodes"] == 10
    assert manifest["dependencies"] == 9
    assert manifest["git_ref"] == ref
    assert manifest["coverage"]["complete"] is False
    assert manifest["coverage"]["counts"] == {
        "DECOMPOSED": 1,
        "DEFERRED": 0,
        "INVENTORIED": 0,
        "MAPPED": 5,
        "OUT": 1,
    }
    assert str(example) not in json.dumps(manifest)
    graph = load_graph(example / "blueprint")
    formalizable = {node.id for node in graph.nodes.values() if node.formalizable}

    # Statements are published as environments on their milestone chapter,
    # each anchored so every cross-reference still lands on the statement.
    chapter_path = site / "roadmap/infimum-loss/README.md"
    chapter = chapter_path.read_text(encoding="utf-8")
    infimum_nodes = [
        node_id for node_id in graph.nodes if node_id.startswith("infimum-loss/")
    ]
    for node_id in infimum_nodes:
        anchor = node_id.split("/", 1)[1].replace("/", "-")
        assert f'id="{anchor}"' in chapter, node_id
    assert not (site / "roadmap/infimum-loss/theorems").exists()

    # Both amsthm styles appear, and the status marks are derived.
    assert 'class="bp-thmwrapper theorem-style-definition bp-fully_proved"' in chapter
    assert 'class="bp-thmwrapper theorem-style-plain bp-planned"' in chapter
    assert '<a class="bp-code-link"' in chapter
    assert '<svg class="bp-code-icon"' in chapter
    assert '<a class="bp-context-link"' not in chapter
    assert 'class="bp-reading-source"' in chapter and 'class="bp-explorer-page"' in chapter
    assert 'data-graph-src="../../dependencies/chapters/infimum-loss.json"' in chapter
    assert re.search(r'<details class="bp-dependencies"><summary>Uses \d+ · Used by \d+</summary>', chapter)
    assert '<nav class="bp-book-nav" aria-label="Blueprint chapters">' in chapter
    assert (
        'class="bp-book-nav-link bp-book-nav-previous" '
        'href="../full-supervision/index.html"'
    ) in chapter
    assert 'bp-book-nav-next' not in chapter

    support_path = site / "roadmap/full-supervision/README.md"
    support = support_path.read_text(encoding="utf-8")
    for node_id in graph.nodes:
        if not node_id.startswith("full-supervision/"):
            continue
        anchor = node_id.split("/", 1)[1].replace("/", "-")
        assert f'id="{anchor}"' in support, node_id
    assert 'class="bp-thmwrapper theorem-style-definition bp-fully_proved"' in support
    assert 'class="bp-thmwrapper theorem-style-plain' not in support
    assert '<a class="bp-code-link"' in support
    assert 'class="bp-book-nav-link bp-book-nav-previous" href="../index.html"' in support
    assert (
        'class="bp-book-nav-link bp-book-nav-next" '
        'href="../infimum-loss/index.html"'
    ) in support

    for href in _HREF.findall(chapter):
        if href.startswith(("http", "#")):
            continue
        linked = (chapter_path.parent / href.split("#")[0]).resolve()
        # Raw HTML links already name the final MkDocs extension; the renderer
        # tree still contains the Markdown source at this stage.
        if not linked.is_file() and linked.name == "index.html":
            linked = linked.with_name("README.md")
        elif not linked.is_file() and linked.suffix == ".html":
            linked = linked.with_suffix(".md")
        assert linked.is_file(), href

    graph_page = (site / "dependencies.md").read_text(encoding="utf-8")
    assert "```mermaid" not in graph_page
    assert 'class="bp-dag-viewer"' in graph_page
    assert 'data-graph-src="dependencies.json"' in graph_page
    assert "graph_view: project" in graph_page
    project_payload = json.loads((site / "dependencies.json").read_text(encoding="utf-8"))
    assert project_payload["view"]["presentation"] == "atlas"
    assert {region["label"] for region in project_payload["regions"]} == {
        "Foundations",
        "Learning Theory",
    }
    assert all(node["summary"] for node in project_payload["nodes"])
    assert {node["url"] for node in project_payload["nodes"]} == {
        "roadmap/full-supervision/index.html",
        "roadmap/infimum-loss/index.html",
    }

    chapter_graph = (site / "dependencies/chapters/infimum-loss.md").read_text(
        encoding="utf-8"
    )
    support_graph = (site / "dependencies/chapters/full-supervision.md").read_text(
        encoding="utf-8"
    )
    assert "graph_view: chapter" in chapter_graph
    assert "graph_view: chapter" in support_graph
    chapter_payloads = {
        group: json.loads((site / f"dependencies/chapters/{group}.json").read_text(encoding="utf-8"))
        for group in ("infimum-loss", "full-supervision")
    }
    for node_id in formalizable:
        anchor = node_id.split("/", 1)[1].replace("/", "-")
        group = node_id.split("/", 1)[0]
        urls = {node["id"]: node["url"] for node in chapter_payloads[group]["nodes"]}
        assert urls[node_id] == f"../../roadmap/{group}/index.html#{anchor}"
    assert not (site / "dependencies/nodes").exists()

    generated_graph_pages = [site / "dependencies.md", *(site / "dependencies").rglob("*.md")]
    for generated_page in generated_graph_pages:
        document = generated_page.read_text(encoding="utf-8")
        assert "```mermaid" not in document
        assert 'class="bp-dag-viewer"' in document
        assert document.count("blueprint-dag.js") == 1

    full_graph = (site / "dependencies/full.md").read_text(encoding="utf-8")
    assert "graph_view: project" in full_graph
    assert 'class="bp-dag-viewer"' in full_graph
    assert "```mermaid" not in full_graph
    payload = json.loads((site / "dependencies/full.json").read_text(encoding="utf-8"))
    assert len(payload["nodes"]) < len(graph.nodes)
    search_index = json.loads((site / "dependencies/index.json").read_text(encoding="utf-8"))
    assert {node["id"] for node in search_index["nodes"]} == set(graph.nodes)
    statement_page = (site / "roadmap/infimum-loss/README.md").read_text(encoding="utf-8")
    assert 'data-graph-src="../../dependencies/chapters/infimum-loss.json"' in statement_page
    result = next(node for node in search_index["nodes"] if node["id"] == "infimum-loss/theorems/supervision-recovery")
    assert result["route"] == "../roadmap/infimum-loss/index.html#theorems-supervision-recovery"
    assert result["result"] is True

    # Progress stays in the integrated reading/map workspace; no separate page.
    assert not (site / "progress.md").exists()
    assert not (site / "book.md").exists()
    overview = (site / "README.md").read_text(encoding="utf-8")
    assert 'data-graph-src="dependencies.json"' in overview
    # The landing page states progress as figures; the chapters keep the strip.
    assert '<div class="bp-figure-label">Fully proved</div>' in overview
    assert '<div class="bp-figure-note">5 of 7 articles</div>' in overview
    assert ">71%<" in overview
    assert "Declared source coverage:" in overview
    assert "1 decomposed · 5 mapped · 1 out" in overview
    assert 'href="coverage/index.html"' in overview
    assert "items settled" not in overview
    assert "bp-progress-link" not in overview
    # A chapter strip counts that chapter, so this is 4 of the project's 5.
    milestone = (site / "roadmap/infimum-loss/README.md").read_text(encoding="utf-8")
    assert "<strong>4</strong> fully proved" in milestone
    coverage = (site / "coverage/README.md").read_text(encoding="utf-8")
    assert "Experiments and narrative material" in coverage
    graph_page = (site / "dependencies.md").read_text(encoding="utf-8")
    assert "bp-book-nav" not in graph_page

    mkdocs = (example / "mkdocs.yml").read_text(encoding="utf-8")
    assert "docs_dir: site-src" in mkdocs
    # Material renders repo_url as the header link, so it must be the project's
    # own repository. Pointing it at AutoformBot sends readers to the plugin.
    assert "repo_url: https://github.com/VivienCabannes/cabannes-thesis" in mkdocs
    assert "repo_url: https://github.com/facebookresearch/autoform-bot" not in mkdocs
    assert "use_directory_urls: false" in mkdocs
    assert "md_in_html" in mkdocs
    assert "pymdownx.superfences" in mkdocs
    assert "stylesheets/blueprint.css" in mkdocs
    assert "javascripts/blueprint-mermaid.js" in mkdocs
    assert "javascripts/blueprint-dag.js" not in mkdocs
    # The nav is generated from the vault into SUMMARY.md, so mkdocs.yml has
    # none: a hand-written chapter list would drift from the book.
    assert "\nnav:\n" not in mkdocs
    assert "literate-nav" in mkdocs
    assert "navigation.tabs" in mkdocs
    assert "  - search\n" in mkdocs
    summary = (site / "SUMMARY.md").read_text(encoding="utf-8")
    assert summary.startswith("---\nsearch:\n  exclude: true\n---\n")
    assert "- [Home](README.md)" in summary
    assert "- Mathematics" in summary
    assert "- Book" not in summary and "- Graph" not in summary
    assert "[Dependency explorer]" not in summary
    assert "[Infimum Loss milestone](roadmap/infimum-loss/README.md)" in summary
    theme = (example / "theme" / "main.html").read_text(encoding="utf-8")
    assert "{% block footer %}" in theme
    assert "AutoformBot" in theme
    assert "https://github.com/facebookresearch/autoform-bot" in theme
    assert '<a href="{{ config.repo_url }}">Formalization source</a>.' in theme
    workflow = (example / ".github/workflows/blueprint-pages.yml").read_text(encoding="utf-8")
    assert "autoform check blueprint --lean-root ." in workflow
    assert "autoform render blueprint" in workflow
    assert "--require-declarations" in workflow
    assert "actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128" in workflow
    assert "@main" not in workflow

    verify = (example / ".github/workflows/autoform-verify.yml").read_text(encoding="utf-8")
    assert "autoform check blueprint" in verify
    assert 'lake clean "$root_package"' in verify
    assert "lake build" in verify
    assert "Reject kernel-check bypass options" in verify
    assert "Audit every root-package declaration" in verify
    assert "python3 .github/autoform_audit.py" in verify
    assert "lake pack" in verify
    assert "lake-modules" not in verify
    assert "contains no ILean artifacts" in (
        example / ".github/autoform_audit.py"
    ).read_text(encoding="utf-8")
    assert 'forbidden="skip""KernelTC"' in verify
    assert 'git grep -n -I "$forbidden" -- .' in verify
    assert 'version: "0.12.1"' in verify
    assert "elan/releases/download/v4.2.3" in verify
    assert "df0b2b3a439961ffcbb3985214365ffe40f49bc871df04dff268c7d8e21ca8b2" in verify
    assert "github.event.workflow_run.path == '.github/workflows/autoform-verify.yml'" in workflow
    assert workflow.count('- "theme/**"') == 1
    assert 'version: "0.12.1"' in workflow
    assert "@main" not in verify

    for contents in (workflow, verify):
        action_refs = re.findall(r"uses:\s+[^@\s]+@([^\s]+)", contents)
        assert action_refs
        assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)


def test_each_skill_points_to_its_thesis_example(repo_root: Path) -> None:
    setup = (repo_root / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    setup_metadata = (repo_root / "skills/setup/agents/openai.yaml").read_text(encoding="utf-8")
    roadmap = (repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8")
    roadmap_metadata = (repo_root / "skills/roadmap/agents/openai.yaml").read_text(encoding="utf-8")
    agent_review = (repo_root / "skills/agent-review/SKILL.md").read_text(encoding="utf-8")
    agent_review_metadata = (repo_root / "skills/agent-review/agents/openai.yaml").read_text(
        encoding="utf-8"
    )
    human_review = (repo_root / "skills/human-review/SKILL.md").read_text(encoding="utf-8")
    human_review_metadata = (repo_root / "skills/human-review/agents/openai.yaml").read_text(
        encoding="utf-8"
    )
    develop_plugin = (repo_root / "skills/develop-plugin/SKILL.md").read_text(encoding="utf-8")
    develop_plugin_metadata = (repo_root / "skills/develop-plugin/agents/openai.yaml").read_text(
        encoding="utf-8"
    )

    for required in (
        "assets/cabannes-thesis-project/README.md",
        "lean-toolchain",
        "autoform-verify.yml",
        "Obsidian",
        "GitHub Pages",
        "root `README.md`",
        "verified canonical URL",
        "references/zulip.md",
        "separate opt-in outward-facing action",
    ):
        assert required in setup
    for required in (
        "references/cabannes-thesis-roadmap.md",
        "blueprint/roadmap/",
        "blueprint/coverage/README.md",
        "blueprint/sources/",
        "## Depends on",
        "## Proof depends on",
        "one unique main result",
        "../setup/references/zulip.md",
    ):
        assert required in roadmap
    assert "autoform init" in setup
    assert "references/thesis-review-case.md" in agent_review
    assert "references/roadmap-quality.md" in agent_review
    assert "stale-build refusal" in agent_review
    assert "drift checksum" in agent_review
    assert "autoform-visualize" in human_review
    assert "`approve`, `revise`, or\n`block`" in human_review
    for required in (
        "example-based plugin",
        "independent formalization",
        "Cabannes-specific",
        "make check-example",
        "plugin-creator",
        "new thread",
        "user nudges",
        "product evidence",
        "future agents need less steering",
        "not the transcript",
    ):
        assert required in develop_plugin
    assert re.search(r"consumer\s+scenario", develop_plugin)
    assert "Agents can infer routine details" in develop_plugin
    assert len(develop_plugin.split()) <= 220
    assert "$setup" in setup_metadata
    assert "$roadmap" in roadmap_metadata
    assert "one invocation" in roadmap_metadata
    assert "persistent Goal" in roadmap_metadata
    assert "$agent-review" in agent_review_metadata
    assert "$human-review" in human_review_metadata
    assert "$develop-plugin" in develop_plugin_metadata
    assert "stops before\nmathematical planning" in setup
    assert "When developing or adapting" not in roadmap
    assert (repo_root / "skills/roadmap/references/cabannes-thesis-roadmap.md").is_file()
    roadmap_example = (
        repo_root / "skills/roadmap/references/cabannes-thesis-roadmap.md"
    ).read_text(encoding="utf-8")
    assert "coherent pull\nrequest and review unit" in roadmap_example
    assert (repo_root / "skills/agent-review/references/thesis-review-case.md").is_file()
    assert (repo_root / "skills/agent-review/references/roadmap-quality.md").is_file()


def test_readback_faithfulness_contract_is_hash_bound_and_machine_readable(
    repo_root: Path,
) -> None:
    skill = (repo_root / "skills/agent-review/SKILL.md").read_text(encoding="utf-8")
    rubric = (
        repo_root / "skills/agent-review/references/readback-faithfulness.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(rubric.split())

    assert "isolated read-back-judge role" in skill
    assert "exact JSON output replaces this\ngeneral report layout" in skill
    for required in (
        "trusted coordinator",
        "blind auditor",
        "faithfulness judge",
        "packet hash",
        "article packet hash",
        "article review hash",
        "read-back hash is required",
    ):
        assert required in normalized

    def template(marker: str) -> dict[str, object]:
        fenced = rubric.split(f"<!-- {marker} -->\n```json\n", 1)[1]
        return json.loads(fenced.split("\n```", 1)[0])

    item = template("readback-faithfulness-item-template")
    verdict = template("readback-faithfulness-verdict-template")
    provenance = {
        "item",
        "declarations",
        "article_skeleton_hash",
        "article_packet_hash",
        "article_review_hash",
        "passage_hash",
    }
    assert item["schema"] == "autoform-readback-faithfulness-item/v1"
    assert verdict["schema"] == "autoform-readback-faithfulness-verdict/v1"
    assert provenance <= item.keys() and provenance <= verdict.keys()
    assert item["declarations"] == verdict["declarations"]
    declaration = item["declarations"][0]
    assert set(declaration) == {
        "id",
        "skeleton_hash",
        "packet_hash",
        "read_back_hash",
    }

    category_rows = {}
    table = rubric.split("## Categories and decisions", 1)[1].split("## Exact output", 1)[0]
    for line in table.splitlines():
        match = re.match(r"\| `([^`]+)` \| `([^`]+)` \|", line)
        if match:
            category_rows[match.group(1)] = match.group(2)
    assert category_rows["elaboration"] == "agrees"
    assert category_rows["equivalent-reformulation"] == "review"
    for category in (
        "hypothesis-missing",
        "hypothesis-added",
        "conclusion-weaker",
        "conclusion-stronger",
    ):
        assert category_rows[category] == "disagrees"
    assert category_rows["evidence-missing"] == "unknown"
    assert "hypothesis-missing: generalizes" not in rubric


def test_roadmap_skill_owns_a_complete_pass(repo_root: Path) -> None:
    """A direct Roadmap invocation is a full job, not one planning checkpoint."""

    roadmap = (repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(roadmap.split())

    for required in (
        "one complete planning pass",
        "model-callable Goal lifecycle",
        "If no compatible Goal can be used",
        "complete the same pass in the current run",
        "leave any unrelated Goal unchanged",
        "Do not ask the user to invoke another command",
        "internal checkpoints",
        "Do not pause for approval",
        "Treat every other `DEFERRED` row as queued work",
        "Do not stop after discovery",
        "Mark an active Goal complete only after these conditions hold",
    ):
        assert required in normalized

    for obsolete in (
        "internal/runbooks/planning.md",
        "Let the user choose whether",
        "for user approval before",
        "After approval",
        "Do not infer missing scope",
    ):
        assert obsolete not in roadmap


def test_roadmap_skill_commits_the_final_checked_pass(repo_root: Path) -> None:
    roadmap = (repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8")
    finish = roadmap.split("## Finish", 1)[1]
    normalized = " ".join(finish.split())

    for required in (
        "After the final edit",
        "`autoform check` with `--lean-root .`",
        "`autoform audit`",
        "Fix the other `warning:` lines inside the adopted boundary as you would a finding",
        "refresh the dependency summary with `autoform-visualize`",
        "Commit the vault, with the refreshed summary where the repository tracks it, only after this final validation",
        "no `MAPPED` rows",
        "latest commit contains every change from the pass",
    ):
        assert required in normalized


def test_setup_skill_offers_opt_in_zulip_project_sync(repo_root: Path) -> None:
    setup = (repo_root / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    roadmap = (repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8")
    zulip = (repo_root / "skills/setup/references/zulip.md").read_text(encoding="utf-8")

    for required in (
        "Treat reading and writing as separate permissions",
        "exact active Zulip identities",
        "Ground the wording in the actual project state",
        "Present ongoing work as",
        "people who have already thought about the design",
        "does not duplicate or scoop existing efforts",
        "After sending, fetch the",
        "stream, topic, links, and rendered mentions",
    ):
        assert required in zulip

    assert "references/zulip.md" in setup
    assert "Do not infer consent" in setup
    assert "../setup/references/zulip.md" in roadmap


def test_skills_teach_the_shipped_frontmatter_model(repo_root: Path) -> None:
    """Agent instructions must match what `autoform_cli.graph` actually parses.

    `kind` and `status` were removed from the frontmatter contract, so a skill
    that still teaches either makes agents author keys the parser rejects.
    """
    for skill in sorted((repo_root / "skills").glob("*/SKILL.md")):
        text = skill.read_text(encoding="utf-8")
        for stale in ("kind: node", "kind: article", "kind: roadmap", "status: active"):
            assert stale not in text, f"{skill.relative_to(repo_root)} still teaches `{stale}`"

    example = repo_root / _EXAMPLE / "blueprint/roadmap"
    for article in sorted(example.rglob("*.md")):
        assert "kind:" not in article.read_text(encoding="utf-8")


def _documented_invocations(reference: str) -> set[tuple[str, ...]]:
    """Every `autoform ...` command line inside the reference's bash fences."""
    invocations: set[tuple[str, ...]] = set()
    for block in re.findall(r"```bash\n(.*?)```", reference, re.DOTALL):
        for line in block.splitlines():
            line = line.strip().rstrip("\\").strip()
            words = line.split()
            if "autoform" not in words:
                continue
            rest = words[words.index("autoform") + 1 :]
            verbs = tuple(word for word in rest if word and not word.startswith("-"))
            if not verbs:
                continue
            if verbs[0] == "claim" and len(verbs) > 1:
                invocations.add(verbs[:2])
            else:
                invocations.add(verbs[:1])
    return invocations


def test_cli_reference_documents_only_commands_that_exist(repo_root: Path) -> None:
    """The CLI reference is the single source of truth, so it must be checkable.

    Skills link here instead of restating flags. That only stays safe if the
    reference cannot drift from the parser, so every command it shows must
    parse.
    """
    import pytest

    from autoform_cli.__main__ import main

    reference = (repo_root / "autoform_cli/README.md").read_text(encoding="utf-8")
    documented = _documented_invocations(reference)
    assert {("check",), ("audit",), ("render",), ("search",), ("claim", "acquire")} <= documented

    for invocation in sorted(documented):
        with pytest.raises(SystemExit) as exit_info:
            main([*invocation, "--help"])
        assert exit_info.value.code == 0, f"reference documents unknown command: {' '.join(invocation)}"


_LAUNCHER = 'uv run --project "<AUTOFORM_PLUGIN_ROOT>" autoform`'


def test_skills_delegate_the_command_line_to_the_reference(repo_root: Path) -> None:
    """Skills state intent and link to the reference; they do not restate flags.

    Duplicated invocations are what let the CLI move underneath the agent's
    instructions, so a skill that needs the command line must cite the
    reference instead of copying it. The bare launcher is not a flag, so a skill
    may state it once.
    """
    citing = 0
    for skill in sorted((repo_root / "skills").glob("*/SKILL.md")):
        text = " ".join(skill.read_text(encoding="utf-8").split())
        if skill.parent.name == "setup":
            assert text.count("uv run --project") == 3
        else:
            invocations = re.findall(r"uv run --project[^`]*`", text)
            assert invocations in ([], [_LAUNCHER]), (
                f"{skill.relative_to(repo_root)} restates a CLI invocation; "
                "link to autoform_cli/README.md#commands instead"
            )
        if "autoform_cli/README.md" in text:
            citing += 1
    assert citing >= 3


def test_skills_that_run_the_cli_state_how_to_launch_it(repo_root: Path) -> None:
    """A consumer project has no `autoform` on `PATH`.

    Agents that read a bare `autoform work list` run `which autoform` and then
    guess; in trials every fresh Roadmap and Formalize session did, and some
    never reached the CLI. So every skill whose instructions or references name
    a command states the launcher once, names the plugin root without relying
    on how a host presents the skill, and hands both to its subagents. Setup
    spells out its own full invocations; Develop Plugin works inside the plugin
    checkout, where `uv run autoform` already resolves.
    """
    running = {
        skill.name
        for skill in (repo_root / "skills").iterdir()
        if any(re.search(r"`autoform[ -][a-z]", path.read_text(encoding="utf-8")) for path in skill.rglob("*.md"))
    }
    assert {"roadmap", "formalize", "agent-review", "human-review"} <= running
    for name in sorted(running - {"setup", "develop-plugin"}):
        flat = " ".join((repo_root / "skills" / name / "SKILL.md").read_text(encoding="utf-8").split())
        assert f"`{_LAUNCHER}" in flat, f"{name} does not state the launcher"
        assert "no `autoform` on `PATH`" in flat
        assert "the directory that holds this skill's `skills/` folder, along with `pyproject.toml`" in flat
        assert "Give a subagent that root and this prefix with its task." in flat


def test_skill_links_into_the_cli_reference_land_on_a_section(repo_root: Path) -> None:
    """A skill that cites a reference section must not point at a renamed one.

    Skills hold no command line of their own, so a link whose fragment no
    longer matches a heading leaves the agent at the top of a long file with no
    contract to read.
    """
    reference_path = repo_root / "autoform_cli/README.md"
    sections: set[str] = set()
    fenced = False
    for line in reference_path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and (heading := re.fullmatch(r"#{1,6} +(.+?) *", line)):
            sections.add(re.sub(r"[^\w -]", "", heading[1].lower()).replace(" ", "-"))

    cited = 0
    for page in sorted((repo_root / "skills").rglob("*.md")):
        where = page.relative_to(repo_root)
        links = re.findall(r"\(([^)\s]*autoform_cli/README\.md)(?:#([^)\s]+))?\)", page.read_text(encoding="utf-8"))
        for target, fragment in links:
            assert (page.parent / target).resolve() == reference_path, f"{where} links past the reference: {target}"
            if fragment:
                assert fragment in sections, f"{where} links to a missing section: #{fragment}"
                cited += 1
    assert cited


def test_skills_search_the_blueprint_before_adding_a_result(repo_root: Path) -> None:
    """`autoform search` prevents a duplicate only if an agent runs it first.

    Matching is literal, so the rule that matters most is that an empty result
    is not yet an answer: without it an agent searches once with a full
    sentence, finds nothing, and adds the result a second time.
    """

    def read(relative: str) -> str:
        return " ".join((repo_root / relative).read_text(encoding="utf-8").split())

    def rule(relative: str) -> str:
        """The paragraph or list item that names the command."""

        blocks = (repo_root / relative).read_text(encoding="utf-8").split("\n\n")
        named = [" ".join(block.split()) for block in blocks if "`autoform search`" in block]
        assert len(named) == 1, f"{relative}: the search rule is spread over {len(named)} paragraphs"
        return named[0]

    def sentence(text: str, phrase: str) -> str:
        """The sentence of ``text`` that holds ``phrase``."""

        found = [part for part in re.split(r"(?<=[.:]) ", text) if phrase in part]
        assert len(found) <= 1, f"{phrase!r} is in {len(found)} sentences of the search rule"
        return found[0] if found else ""

    roadmap = rule("skills/roadmap/SKILL.md")
    formalize = rule("skills/formalize/SKILL.md")
    rubric = rule("skills/agent-review/references/roadmap-quality.md")

    for text in (roadmap, formalize, rubric):
        assert "README.md#search-contract)" in text
        # One query that finds nothing does not show the result is new.
        assert "fewer words" in text
    for skill in (roadmap, formalize):
        assert "refusal (exit 2) is not an empty result" in skill
    # What to do with a hit, which is the point of searching: these check that
    # each instruction is present, in one sentence, not how it is worded.
    assert "instead of adding" in sentence(roadmap, "## Depends on")
    assert "dependencies reach" in sentence(formalize, "Use the declaration")
    assert "never restate" in sentence(formalize, "missing prerequisite")
    assert "duplicate" in rubric and "not proof" in rubric


def test_agent_review_judges_a_catalog_as_an_inventory(repo_root: Path) -> None:
    """A catalog lists peer declarations by design, which the rubric scored as a defect."""
    rubric = " ".join(
        (repo_root / "skills/agent-review/references/roadmap-quality.md").read_text(encoding="utf-8").split()
    )

    assert "`DECOMPOSED`, `INVENTORIED`, `DEFERRED`, and `OUT`" in rubric
    assert "not a duplicate of the article that expounds it" in rubric
    assert "The pull-request-unit and unique-main-result rules do not apply to it" in rubric


def test_roadmap_states_the_library_search_without_the_link(repo_root: Path) -> None:
    """Roadmap carries the library flags itself, not only behind the link to the rules.

    In trials, Roadmap agents never opened the linked rules: one never learned
    `--library`, another searched once and, finding the new project's blueprint
    empty, wrote every article without searching the library that proved them.
    """

    blocks = (repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8").split("\n\n")
    search = next(" ".join(block.split()) for block in blocks if "`autoform search`" in block)
    flat = " ".join("\n\n".join(blocks).split())

    assert "`autoform library list .`" in search
    assert "`--library NAME --lean-root .`" in search
    assert "even while the blueprint is empty" in search
    assert "library search rules" in search  # the details still live once, behind the link
    assert "Search the pinned Mathlib checkout and every usable shared library before planning a replacement" in flat


def test_roadmap_searches_the_results_a_proof_uses_and_reads_all_of_the_output(repo_root: Path) -> None:
    """A library may prove a step of an article's proof, which only a search of that step finds.

    In a trial (#346), Roadmap searched in two broad batches before writing 36 articles, and piped every
    search through `head`, which cut off the statement of an ATLAS theorem proving a step of one proof.
    The plan proved that step again, and check and audit passed; the review rubric searched only main
    results, so a review would have missed it too.
    """

    def sentences(relative: str) -> list[str]:
        blocks = (repo_root / relative).read_text(encoding="utf-8").split("\n\n")
        search = next(" ".join(block.split()) for block in blocks if "`autoform search`" in block)
        return re.split(r"(?<=\.) ", search)

    roadmap = sentences("skills/roadmap/SKILL.md")
    # These check that each instruction is present, in one sentence of the search rule, not how it is worded.
    assert any("`autoform search`" in s and "proof" in s for s in roadmap), roadmap
    # A rule that names `head` must forbid it, and a batch search must still go article by article.
    assert any(re.search(r"\bhead\b", s) and re.search(r"\b(?:never|not)\b", s) for s in roadmap), roadmap
    assert any("batch" in s and "proof" in s and re.search(r"\b(?:each|every)\b", s) for s in roadmap), roadmap
    rubric = sentences("skills/agent-review/references/roadmap-quality.md")
    assert any("proof" in s and "missed reuse" in s for s in rubric), rubric


def test_roadmap_leaves_lean_unset_for_a_library_declaration(repo_root: Path) -> None:
    """A library declaration goes in the implementation note, and `lean:` waits for the project's declaration.

    `autoform check --lean-root` resolves `lean:` only in the project's own Lean files. In a trial (#349),
    Roadmap set `lean:` to the library declarations it found; check could not find them, so it imported the
    modules and rebuilt, then moved the names into the articles' prose instead of their implementation notes
    (a comment on #346 records this).
    """

    blocks = (repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8").split("\n\n")
    search = next(" ".join(block.split()) for block in blocks if "`autoform search`" in block)
    sentences = re.split(r"(?<=\.) ", search)
    # These check that each instruction is present, in one sentence of the search rule, not how it is worded.
    assert any("implementation note" in s and "unset" in s and "Formalize" in s for s in sentences), sentences
    assert any("`lean:`" in s and "project" in s and "--lean-root" in s for s in sentences), sentences
    # The rules Roadmap defers to have Formalize record `lean:` once the project's declaration exists.
    reference = (repo_root / "skills/formalize/references/library-search.md").read_text(encoding="utf-8")
    own = next(p for p in (" ".join(b.split()) for b in reference.split("\n\n")) if "own result" in p)
    assert "`lean:` records its name on acceptance" in own


def test_skills_search_shared_libraries_and_say_what_to_do_with_a_hit(repo_root: Path) -> None:
    """A library hit prevents a second proof only if the agent searches the library and knows what to do.

    The rules live once, in Formalize's reference page; the three search paragraphs link to it and carry
    only what changes how they read: search the libraries too, and a library refusal is not a blocker.
    Roadmap's also says how a hit is reused, since its agents did not open the link in trials.
    """

    reference_path = repo_root / "skills/formalize/references/library-search.md"
    reference = reference_path.read_text(encoding="utf-8")
    paragraphs = [" ".join(block.split()) for block in reference.split("\n\n")]

    def rule(*phrases: str) -> str:
        """The one paragraph of the reference holding every phrase."""

        found = [p for p in paragraphs if all(phrase in p for phrase in phrases)]
        assert len(found) == 1, f"{phrases!r} is in {len(found)} paragraphs of the library search rules"
        return found[0]

    # Which libraries, and a refusal that names one.
    listing = rule("`autoform library list`")
    assert "every package it reports as usable" in listing and "tell the user once" in listing
    assert "error: library NAME:" in rule("not a blocker")
    assert "rerun" in rule("not a blocker") and "tell the user" in rule("not a blocker")
    # What to do with each kind of hit.
    assert "`import`" in rule("as a helper")
    assert "does not apply" in rule("as a helper")
    assert "does not replace the article" in rule("own result")
    assert "abbreviation" not in reference
    assert "keyword" in rule("own result") and "never a second copy" in rule("own result")
    assert "full strength" in rule("own result") and "special case" in rule("own result")
    assert "statement, not a proof" in rule("`Prop`") and "wanted entry" in rule("`Prop`")
    assert "not a source" in rule("original source") and "docstring" in rule("original source")
    assert "not been checked" in rule("not been checked") and "unknown" in rule("not been checked")
    assert "modifiers" in rule("own result") and "noncomputable" in rule("own result")
    assert "never `lake update`" in listing and "unchecked" in listing
    assert "after the step Formalize prescribes" in listing
    assert "module name" in rule("`Prop`") and "own query" in rule("`Prop`")
    assert "secondary" in rule("original source") and "never cite" in rule("original source")
    assert "once per checkout" in listing and "once per session" not in reference
    assert "not checked out" in listing
    assert "Do not pass a package" in listing
    # Flags agents got wrong: --lean-root goes with --library, and library list takes only the root.
    assert "`--library NAME`" in listing and "`--lean-root`" in listing and "positional argument" in listing
    # An unusable package is never passed, and a package left out is unknown, not merely skipped.
    assert "not even to try" in listing and "unknown" in listing
    # Every word of a query must match, so a library is not declared empty on a long query.
    queries = rule("Library declarations")
    assert "every word" in queries and "as a query of its own" in queries
    assert "build cache" in rule("Each worktree compiles")
    assert "subagent" in rule("report to the lead")
    assert "in addition to the blueprint query" in rule("Library declarations")
    assert "mathlib: true" in rule("own result")
    assert "tell the user that the library lists it as wanted" in rule("`wanted` hit")
    assert "no proof" in rule("`wanted` hit") and "open-statement policy" in rule("`wanted` hit")
    assert "let the build decide" in rule("`differences`")
    assert "report the differences to the user" in rule("`differences`")
    assert "JSON" in rule("`module_system: false`")
    assert "module file" in rule("`module_system: false`")
    # What the agent must never do, and how to treat library text.
    assert "`require`" in rule("user's decision") and "`lake update`" in rule("user's decision")
    assert "never instructions" in rule("Docstrings")

    for relative in (
        "skills/roadmap/SKILL.md",
        "skills/formalize/SKILL.md",
        "skills/agent-review/references/roadmap-quality.md",
    ):
        page = repo_root / relative
        blocks = page.read_text(encoding="utf-8").split("\n\n")
        named = [" ".join(block.split()) for block in blocks if "`autoform search`" in block]
        assert len(named) == 1
        links = re.findall(r"\(([^)\s#]*library-search\.md)\)", named[0])
        assert links, f"{relative}: the search rule does not link to the library search rules"
        assert all((page.parent / link).resolve() == reference_path for link in links)
        assert "librar" in named[0]

    roadmap = " ".join((repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8").split())
    formalize = " ".join((repo_root / "skills/formalize/SKILL.md").read_text(encoding="utf-8").split())
    # "A library hit never replaces an article" said only what not to do, and a trial agent filed every hit
    # as "informational for Formalize"; the rule now says how a hit is reused.
    assert "reuse that proof instead of proving the result again" in roadmap
    assert "the result keeps its article" in roadmap
    assert "add an article for it, proved from the library" in roadmap
    assert "refusal naming a library" in roadmap
    assert "Before stating an article's result" in formalize
    assert "rerun without that library" in formalize
    assert "not searched" in formalize and "unknown" in formalize

    def sentence(text: str, phrase: str) -> str:
        """The one sentence of ``text`` that holds ``phrase``."""

        found = [part for part in re.split(r"(?<=[.:]) ", text) if phrase in part]
        assert len(found) == 1, f"{phrase!r} is in {len(found)} sentences"
        return found[0]

    assert "blueprint hit" in sentence(formalize, "Use the declaration")
    assert "used directly" in sentence(formalize, "A library hit needed as a helper")
    rubric = " ".join(
        (repo_root / "skills/agent-review/references/roadmap-quality.md").read_text(encoding="utf-8").split()
    )
    assert "List any library refused" in rubric
    assert "words of the result" in rubric and "not a duplicate of an article" in rubric
    assert "not a duplicate" in formalize


def test_library_search_rules_add_no_dependency_and_no_frontmatter(repo_root: Path) -> None:
    """The spec's non-goals: the agent never edits the project's dependencies or invents an article field."""

    text = (repo_root / "skills/formalize/references/library-search.md").read_text(encoding="utf-8")
    assert "uv run --project" not in text
    assert not re.search(r"^\s*(lean_library|library):", text, re.MULTILINE)
    flat = " ".join(text.split())
    assert "Do not add a `require`" in flat
    assert "change a locked revision" in flat and "run `lake update`" in flat
    sentence = next(part for part in re.split(r"(?<=\.) ", flat) if "`lake update` to make" in part)
    assert sentence.startswith("Do not add a `require`, change a locked revision, or run")


def test_roadmap_reconciles_the_pages_setup_wrote(repo_root: Path) -> None:
    """Setup declares the project empty; Roadmap must retract that.

    A live run decomposed a chapter into nine nodes and published a site whose
    front page still read "no chapters, statements, or dependencies exist
    below". Setup authored that sentence before any scope existed and Roadmap
    updated the chapter and the coverage contract but not the two landing pages,
    so the first thing a visitor read was that the project was empty.
    """

    roadmap = (repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8")

    for required in ("blueprint/README.md", "repository `README.md`"):
        assert required in roadmap, f"Roadmap never reconciles {required}"


def test_roadmap_repository_scope_inventories_existing_lean(repo_root: Path) -> None:
    """A global blueprint is an inventory, not merely a future-work backlog.

    A consumer asked for one wiki covering all mathematics in an established
    Lean repository. The old workflow treated local implementations as prior
    art, produced only prospective roadmaps, and therefore rendered 0% despite
    extensive existing code.
    """

    roadmap_path = repo_root / "skills" / "roadmap" / "SKILL.md"
    roadmap = roadmap_path.read_text(encoding="utf-8")
    normalized = " ".join(roadmap.split())
    metadata = (
        repo_root / "skills" / "roadmap" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")
    example = (
        repo_root / "skills" / "roadmap" / "references" / "cabannes-thesis-roadmap.md"
    ).read_text(encoding="utf-8")

    for required in (
        "repository-wide library, global wiki, or other existing codebase",
        "project-owned Lean is a primary mathematical source",
        "inventory every in-scope tracked module and public mathematical declaration",
        "future-work slice cannot stand in for a repository-wide inventory",
        # A docstring describes code; a paper-based pass still works from its source.
        "Work from exact source passages. When the boundary includes existing Lean",
        "never replace the adopted source",
        "a repository inventory also needs every in-scope declaration reconciled",
        "`DECOMPOSED` means represented by declaration articles, not unfinished",
        "record exact compiled names in `lean`",
        "`proof: formalized` for a theorem or lemma only after checking that its proof is complete",
        "An axiom or wanted placeholder does not justify a formalized proof",
        "a definition of a conjecture proposition formalizes its statement representation",
        "`mathlib: true` only for an exact verified upstream result",
    ):
        assert required in normalized

    assert "inventorying existing formalized Lean when the boundary is an existing repository or library" in metadata
    assert "Existing and planned mathematics coexist in one book" in example


def test_roadmap_and_formalize_leave_code_an_audit_warns_about_to_the_user(repo_root: Path) -> None:
    """A catalog's limit warning describes code that builds today; a guessed limit can break it."""
    for skill in ("roadmap", "formalize"):
        text = " ".join((repo_root / f"skills/{skill}/SKILL.md").read_text(encoding="utf-8").split())
        assert "`catalog-unscoped-limit` warning describes existing Lean code and never fails the audit" in text, skill
        assert "change that code only when the user asks" in text, skill


def test_roadmap_commits_so_the_published_site_can_catch_up(repo_root: Path) -> None:
    """CI publishes from the repository, not from a working tree.

    Roadmap was the only workflow skill silent on committing, which left the
    deploy waiting on a step nobody owned. Pushing stays the user's call.
    """

    roadmap = (repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8")

    assert "Commit the vault" in roadmap
    assert "outward-facing" in roadmap


def test_roadmap_retracts_against_the_branch_formalize_works_from(repo_root: Path) -> None:
    """Roadmap's base can predate a Formalize landing, with or without claims.

    If Roadmap looks for `lean:` only on its own base, an article Formalize
    formalized meanwhile keeps `statement: formalized` through a clean rebase
    or merge although the revision changed its statement, and check and audit
    both pass.
    """

    roadmap = " ".join((repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8").split())
    for required in (
        "Before the revision lands on that branch, and so before releasing any claim",
        "bring it up to date with that branch by rebase or merge, "
        "retract any revised article that gained `lean:` there",
        "report the revised articles that had no `lean:` on that branch at your last update",
    ):
        assert required in roadmap


def test_example_workflows_match_the_scaffold_templates(repo_root: Path) -> None:
    """The executable example differs only by its concrete immutable pin."""

    substitutions = {
        "{{AUTOFORM_SOURCE_YAML}}": '"https://github.com/facebookresearch/autoform-bot.git"',
        "{{AUTOFORM_REF_YAML}}": '"0288506b87e2e2f70b7446bf0cf7145006138d74"',
    }
    template_dir = repo_root / "autoform_cli/templates/github/workflows"
    example_dir = repo_root / _EXAMPLE / ".github/workflows"
    assert (
        repo_root / "autoform_cli/templates/github/autoform_audit.py"
    ).read_bytes() == (repo_root / _EXAMPLE / ".github/autoform_audit.py").read_bytes()
    assert (
        repo_root / "autoform_cli/templates/github/CODEOWNERS.autoform.example"
    ).read_bytes() == (
        repo_root / _EXAMPLE / ".github/CODEOWNERS.autoform.example"
    ).read_bytes()

    for name in ("autoform-verify.yml", "blueprint-pages.yml"):
        expected = (template_dir / name).read_text(encoding="utf-8")
        for placeholder, value in substitutions.items():
            expected = expected.replace(placeholder, value)
        actual = (example_dir / name).read_text(encoding="utf-8")
        assert actual == expected


def test_the_example_site_config_matches_what_setup_would_write(repo_root) -> None:
    """The bundled example is a second copy of the scaffold's mkdocs.yml.

    Only the two substituted lines may differ. Everything else -- theme, fonts,
    logo, features, plugins, extensions -- has to be what `autoform init`
    writes, or the example stops demonstrating the product. It already drifted
    once: the template moved to Plus Jakarta Sans and the example kept
    requesting Merriweather, so the built demo showed the old typeface.

    The example also keeps the Mermaid script its pinned render needs, which
    the template no longer lists because the site script loads Mermaid itself.
    """
    substituted = {"site_name", "repo_url"}
    pinned_mermaid = "  - https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"

    def significant(text: str) -> list[str]:
        return [
            line
            for line in text.splitlines()
            if line.strip()
            and not line.lstrip().startswith("#")
            and line.split(":", 1)[0].strip() not in substituted
        ]

    template = (repo_root / "autoform_cli/templates/mkdocs.yml").read_text(encoding="utf-8")
    example = (
        repo_root / "skills/setup/assets/cabannes-thesis-project/mkdocs.yml"
    ).read_text(encoding="utf-8")

    assert pinned_mermaid not in template.splitlines()
    assert [line for line in significant(example) if line != pinned_mermaid] == significant(template)
