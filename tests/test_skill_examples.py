from __future__ import annotations

import json
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from autoform_cli.audit import audit_blueprint
from autoform_cli.graph import load_graph
from autoform_cli.lean import build_linker, declaration_names
from autoform_cli.render import render_site
from autoform_cli.status import derive


_HREF = re.compile(r'href="([^"]+)"')
_EXAMPLE = Path("skills/setup/assets/cabannes-thesis-project")


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

    assert "`Scoped roadmap` percentage" in skill
    assert "formalizable leaf targets" in skill
    assert "fully proved, including every dependency" in skill
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
    assert "overview, progress, project graph" not in skill


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

    assert "Read the complete article, its `blueprint/.implementation-notes/<article_id>.md`" in formalize
    assert "The article keeps only frontmatter and mathematics." in formalize
    assert "Write notes only in the claimed article's `blueprint/.implementation-notes/<article_id>.md`" in formalize
    assert "one file per claimed article avoids cross-worker merge hotspots" in formalize
    assert "Delete the file once the article is proved" in formalize
    assert "never a secret or machine-local path" in formalize
    assert "Never link the hidden note from an article" in formalize
    assert "Articles hold only frontmatter and informal mathematics" in roadmap
    assert "`blueprint/.implementation-notes/<article_id>.md`" in roadmap
    assert "Delete it when the article is proved or marked `mathlib: true`" in roadmap
    assert "an orphaned note blocks every command that loads the graph" in roadmap
    assert "Versioned implementation notes live outside the mathematical roadmap" in reference
    assert "Record what happened in the revision commit and report" in reference
    assert "Use an article's implementation note only while that article remains unproved" in reference
    assert "Markdown under `blueprint/roadmap/` is the authored roadmap" in guidance
    assert "files under `blueprint/.implementation-notes/`" in guidance
    for text in (formalize, roadmap, reference):
        assert "Execution notes" not in text
        assert "`agents.md`" not in text


def test_development_guidance_requires_fail_closed_local_safety(repo_root: Path) -> None:
    development = (repo_root / "skills" / "develop-plugin" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(development.split())

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
    assert "v4.27.0" in normalized
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
    linker = build_linker(example)
    for node in graph.nodes.values():
        for name in declaration_names(node.lean or ""):
            assert linker.location(name) is not None, f"{node.id}: {name}"

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
    manifest = tomllib.loads((example / "lakefile.toml").read_text(encoding="utf-8"))
    assert toolchain == "leanprover/lean4:v4.32.2"
    assert manifest["require"][0]["rev"] == "v4.32.2"
    assert manifest["lean_lib"][0]["srcDir"] == "src"


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
    example = repo_root / _EXAMPLE
    site = tmp_path / "site-src"

    report = render_site(
        example / "blueprint",
        site,
        lean_root=example,
        repository_url="https://github.com/owner/repo",
        ref="0" * 40,
    )

    assert report.unresolved == []
    manifest = json.loads((site / "publication.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "autoform-publication/v1"
    assert manifest["nodes"] == 10
    assert manifest["dependencies"] == 9
    assert manifest["git_ref"] == "0" * 40
    assert manifest["coverage"]["complete"] is False
    assert manifest["coverage"]["counts"] == {
        "DECOMPOSED": 1,
        "DEFERRED": 0,
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
    assert '<a class="bp-context-link"' in chapter
    assert "dependencies/nodes/infimum-loss/theorems/supervision-recovery.html" in chapter
    assert '<details class="bp-dependencies"><summary>Dependencies</summary>' in chapter
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
    assert "```mermaid" in graph_page
    assert "graph_view: project" in graph_page
    assert '"dependencies/chapters/infimum-loss.html"' in graph_page
    assert '"dependencies/chapters/full-supervision.html"' in graph_page

    chapter_graph = (site / "dependencies/chapters/infimum-loss.md").read_text(
        encoding="utf-8"
    )
    support_graph = (site / "dependencies/chapters/full-supervision.md").read_text(
        encoding="utf-8"
    )
    assert "graph_view: chapter" in chapter_graph
    assert "graph_view: chapter" in support_graph
    for node_id in formalizable:
        anchor = node_id.split("/", 1)[1].replace("/", "-")
        group = node_id.split("/", 1)[0]
        target_graph = chapter_graph if group == "infimum-loss" else support_graph
        assert f'"../../roadmap/{group}/index.html#{anchor}"' in target_graph
        assert (site / "dependencies/nodes" / f"{node_id}.md").is_file()

    full_graph = (site / "dependencies/full.md").read_text(encoding="utf-8")
    assert "graph_view: full" in full_graph
    focus_graph = (
        site / "dependencies/nodes/infimum-loss/theorems/supervision-recovery.md"
    ).read_text(encoding="utf-8")
    assert "graph_view: focus" in focus_graph
    assert re.search(r"class n\d+ focus", focus_graph)
    assert "one dependency hop" in focus_graph
    assert (
        "[Open textbook statement](../../../../roadmap/infimum-loss/README.md#"
        "theorems-supervision-recovery)"
    ) in focus_graph

    # Progress folded into the Book landing and the Graph; no separate page.
    assert not (site / "progress.md").exists()
    assert not (site / "book.md").exists()
    overview = (site / "README.md").read_text(encoding="utf-8")
    # The landing page states progress as figures; the chapters keep the strip.
    assert "Scoped roadmap" in overview
    assert "5 of 7 targets complete" in overview
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
    # The nav is generated from the vault into SUMMARY.md, so mkdocs.yml has
    # none: a hand-written chapter list would drift from the book.
    assert "\nnav:\n" not in mkdocs
    assert "literate-nav" in mkdocs
    assert "navigation.tabs" in mkdocs
    summary = (site / "SUMMARY.md").read_text(encoding="utf-8")
    assert summary.startswith("- [Home](README.md)")
    assert "- Book" in summary
    assert "- Graph" in summary
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
    assert "github.ref == 'refs/heads/main'" in workflow
    assert workflow.count('- "theme/**"') == 2
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
        "`autoform check`",
        "`autoform audit`",
        "Commit the vault and refreshed graph only after this final validation",
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
    assert {("check",), ("audit",), ("render",), ("claim", "acquire")} <= documented

    for invocation in sorted(documented):
        with pytest.raises(SystemExit) as exit_info:
            main([*invocation, "--help"])
        assert exit_info.value.code == 0, f"reference documents unknown command: {' '.join(invocation)}"


def test_skills_delegate_the_command_line_to_the_reference(repo_root: Path) -> None:
    """Skills state intent and link to the reference; they do not restate flags.

    Duplicated invocations are what let the CLI move underneath the agent's
    instructions, so a skill that needs the command line must cite the
    reference instead of copying it.
    """
    citing = 0
    for skill in sorted((repo_root / "skills").glob("*/SKILL.md")):
        text = skill.read_text(encoding="utf-8")
        if skill.parent.name == "setup":
            assert text.count("uv run --project") == 3
        else:
            assert "uv run --project" not in text, (
                f"{skill.relative_to(repo_root)} restates a CLI invocation; "
                "link to autoform_cli/README.md#commands instead"
            )
        if "autoform_cli/README.md" in text:
            citing += 1
    assert citing >= 3


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


def test_roadmap_commits_so_the_published_site_can_catch_up(repo_root: Path) -> None:
    """CI publishes from the repository, not from a working tree.

    Roadmap was the only workflow skill silent on committing, which left the
    deploy waiting on a step nobody owned. Pushing stays the user's call.
    """

    roadmap = (repo_root / "skills/roadmap/SKILL.md").read_text(encoding="utf-8")

    assert "Commit the vault" in roadmap
    assert "outward-facing" in roadmap


def test_example_workflows_match_the_scaffold_templates(repo_root: Path) -> None:
    """The executable example differs only by its concrete immutable pin."""

    substitutions = {
        "{{AUTOFORM_SOURCE_YAML}}": '"https://github.com/facebookresearch/autoform-bot.git"',
        "{{AUTOFORM_REF_YAML}}": '"c994d83f9fab1f40d69b2f279d4c62ca937a0186"',
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
    """
    substituted = {"site_name", "repo_url"}

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

    assert significant(example) == significant(template)
