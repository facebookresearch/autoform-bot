# Changelog

Notable changes to Autoform, newest first. A release's version is the one in
`pyproject.toml`; `uv.lock`, the Claude, Muse and Codex manifests, and this
file's newest release heading carry the same one (`tests/test_release.py`
checks), and `autoform --version` prints it. Add a line under **Unreleased**
with each change worth knowing about, and give the section its version and
date when the release is tagged.

## Unreleased

### Changed

- **Wiki map.** Authored concept links cluster intact formal developments,
  with distinct dotted associations and bounded local corridors. Proof edges,
  ranks, status and full reading pages remain unchanged (#442).
- **Docs.** The CLI reference's contracts moved to `autoform_cli/reference/`
  (validation and audit, search, open statements, claims and revisions,
  runtime and publication). The reference, skills and contributor docs keep
  only the rules users and agents act on; internals moved to module
  docstrings, and upgrade notes to this file. `tests/test_doc_budgets.py`
  fails when one of these docs grows past its line budget (#458).
- **Listings.** The Codex and Muse listings name the plugin AutoformBot, and
  Codex shows the Roadmap, Setup and Develop Plugin skills under those short
  names (#463).
- **Claims.** `claim list TARGET...` fetches and reads only those claims.
  `--timeout` (120 seconds by default) bounds a listing, a slow step is named on
  stderr, and a failure goes to stderr labelled `timeout:`, `contention:` or
  `transport:` with nothing on stdout. Git no longer waits for a terminal
  password prompt in claim commands, and a `claim list` or `claim cleanup`
  listing that runs out of time or is interrupted no longer leaves its
  transport running. Formalize treats a labelled `claim list` failure as an
  unreadable board, not as a board without claims (#432).
- **Render.** An article's page lists its `mathlib_declaration` names in a
  Mathlib row, as text without a link, so a `mathlib: true` article names the
  declaration it restates (#388).
- **Check and render.** One reused converter reads every page, and each linked
  path is resolved once per run, so `check` uses about a fifth less CPU on an
  1,806-article vault; neither command's output changes (#415).
- **Builds.** `project new` turns on Lake's artifact cache with
  `enableArtifactCache` and `restoreAllArtifacts`, so checkouts and worktrees
  on one toolchain reuse each other's builds; Setup offers the same two lines
  to an existing project. A probe that finds no `.olean` for a module Lake
  calls built now names the cache-without-restore cause (#431).
- **Library results in Roadmap.** When a shared library already proves a
  result, Roadmap lists under the article's `## Proof depends on` only the
  articles that proof uses, and keeps the source's route in the proof sketch,
  so the article is not blocked on steps its proof never needs; Agent Review
  checks the same (#443).
- **Dependency explorer.** The rail and the details typeset the TeX in titles
  and summaries, with the articles' input and the project's macros, where the
  explorer's own MathJax calls had typeset nothing on a generated site. Map
  cards, the Contents list, search and the explorer's head still show a title
  as typed (#455).

### Fixed

- **Wiki reading links.** The integrated explorer retains #455's title math
  in its outline and related-reading controls, using the site's safe MathJax
  service and project macros. Code stays literal, canonical links keep focus
  and native modifiers, and graph labels remain as typed (#442).
- **Claims.** `autoform dashboard` runs its claim listing's Git in the
  dashboard's process group again, so Ctrl+C or a signal to that group stops
  Git along with the dashboard (#447). On macOS and Windows, a listing shows
  both of two claims whose keys, or folders in them, differ only in case,
  including with Apple's Git, which stored one key's name holding the other's
  lease (#452). The claim contract now says what `--timeout` bounds and which
  listings run Git apart from their caller (#448).

## 0.6.0 (2026-10-22)

What has landed since 0.5.0, across 236 merged pull requests, grouped by
theme. Each group names the pull requests to read for detail.

### Upgrading from 0.5.0

- **Pin.** CI at an older `AUTOFORM_REF` rejects `area:`, `navigation:`,
  `catalog:` and `statement: retracted` in `check` and `INVENTORIED` in
  `render`, so move the pin before using them; until then, retract by removing
  `statement` and `proof` and keeping `lean:`. Older refs assign no areas from
  `blueprint/atlas.json` but publish it with every id it names, so a project
  with unpublished folders moves the pin before adding it (#90).
- **Site.** Render refuses to replace the `autoform-publication/v1` site 0.5.0
  wrote, and the dashboard reports it as stale: delete that output directory,
  such as a local `site-src`, once by hand (#165, #90). A v2 site is replaced
  by the first render and reported as stale until then.
- **Check.** A repository that passed 0.5.0's `check` can fail this one's; see
  **Checks that fail** below. `check`'s status line counts only leaves that
  carry `declaration`, and with `--lean-root` the distinct `lean:` names it
  resolved. A `blueprint/atlas.json` that is not `autoform-atlas/v1` draws a
  warning from `check`, `render` and `audit`.
- **Universe binders.** `def f.{u}` is read as `f`, so a `lean:` name for it
  resolves and an alias of one is read through its target. A name declared
  that way in one file and plainly in another is `defined more than once` in
  `check` and `lean-target-ambiguous` in `audit` (#348, #90).
- **Coverage and audit.** `audit`'s coverage line always shows an inventoried
  count, and its text output adds `warning:` lines. A `DECOMPOSED` link that
  reaches only prose or `not_ready` leaves that declare nothing yet is a
  `decomposed-without-declaration` warning, which `render` also prints;
  neither fails (#90).
- **JSON consumers.** Runtime nodes and every `autoform search --json` hit
  carry a nullable `catalog` (`module` for a module catalog); search stays
  `autoform-search/v1`, so a consumer that rejects unknown keys must accept it
  (#90).
- **TeX.** Formulas can no longer define notation: `\newcommand`, `\def`,
  `\DeclareMathOperator`, `\label`, `\color`, `\cancel`, `\href` and the like
  show in red, and `check` warns about a formula that defines a command. Put
  the project's macros in `blueprint/tex-macros.json`; render replaces an
  edited `blueprint/javascripts/mathjax.js`, with a warning, so move its macros
  to `tex-macros.json` and delete it (#215).
- **Mermaid.** Authored diagrams lose click callbacks, HTML labels and the
  status palette's `classDef` colours, such as `A:::fully_proved`. A
  `mkdocs.yml` that loads Mermaid from the CDN may drop that line once CI's
  `AUTOFORM_REF` includes #378; a render pinned before it needs the line.
- **MkDocs.** Setup leaves an existing `mkdocs.yml` alone; a large site can add
  Material's `navigation.prune` feature by hand, as the scaffold does.
- **Debrief.** Setup, Roadmap and Formalize now debrief their lead agent
  before the final report, on by default; `AUTOFORM_DEBRIEF=0` turns it off.
  Subagent debriefs run only with `AUTOFORM_SUBAGENT_DEBRIEF=1` (#195).
- **REPL.** `run_lean_code` starts a fresh Lean process for each call, so
  nothing carries over between calls and each pays for its imports. The
  default `LEAN_REPL_CMD` is now `lake exe @repl/repl`, which finds the REPL
  only through a require named `repl` (#105).
- **CI.** A project scaffolded by 0.5.0 keeps its workflows when it moves
  `AUTOFORM_REF`, since `init` writes only missing files. To deploy Pages only
  from verified commits, delete `.github/workflows/blueprint-pages.yml` and
  rerun `autoform init` (#399). For the new CI audit, delete
  `.github/workflows/autoform-verify.yml` and `.github/autoform_audit.py`
  together before that run.

### Added

- **Formalization frontier.** A Markdown roadmap now schedules the work:
  `autoform work` lists ready leaves, gives one leaf's context, and measures
  the impact of a revision or an import (#92, #136, #183). Proofs may import
  other articles' modules when that impact is measured. `work impact` also
  reports unused statement dependencies (#150), works beside Lean files whose
  names are not identifiers (#182), and on a stale build names the root
  libraries to build instead of printing Lake's whole output (#427).
- **Formalize skill.** New since 0.5.0: it proves a Markdown roadmap's ready
  leaves in Lean and records verified progress in the roadmap (#92).
- **Skeletons.** `autoform skeleton` extracts what a reader must trust for each
  formalized statement, from elaborated terms, one Lean probe per root module,
  run in parallel (#12, #213, #214).
- **Search and reuse.** `autoform search` finds articles by title, Lean name or
  statement before an agent adds one (#171, #208, #375, #376, #392).
  `autoform library` lists the Lean libraries a project depends on and writes
  the index a library publishes, ATLAS among them; `autoform search --library`
  searches that index, and the skills reuse its results: Roadmap searches each
  intermediate result a proof uses, and records a reused library declaration
  in the implementation note, leaving `lean:` unset (#204, #311, #343, #366,
  #373, #408). `autoform library index` runs its probe on Lean v4.34.1 and
  v4.35.0 (#440). `check --lean-root` warns about a library declaration whose
  docstring cites a paper that unproved articles cite, when nothing in the
  project names it (#379, #400).
- **Dependency explorer.** One explorer replaces the site's generated Mermaid
  dependency maps, the full theorem DAG page among them. It runs on the landing
  page and on every project, chapter and scope page. It opens on chapter cards
  and drills through authored scopes, with search, status filters, a details
  drawer and addressable `#node=` focus. 0.5.0's per-statement
  `dependencies/nodes/<id>.html` pages are gone, and links to them no longer
  resolve. A statement's dependency link is now `<scope>.html#node=<id>`, and a
  `dependencies/full.html#node=<id>` link redirects to the smallest scope
  holding the node. Authored `area:` values or `blueprint/atlas.json` group
  topic cards into regions, and any container can set a `navigation:` role.
  Authored Mermaid diagrams still render (#90). A design pass gave it one type
  scale and quieter chrome (#411).
- **Module catalogs.** A `catalog: module` leaf inventories an existing Lean
  module. It lists the module's public names in `lean:` and links a
  declaration ledger under `blueprint/sources/`. It is never dispatched, and it
  counts in its own inventory metric rather than in the roadmap's completion.
  Coverage gains `INVENTORIED`, the terminal disposition for a row that links
  module catalogs. `audit` reports catalog and coverage-role problems, and
  `audit --json` gains a `warnings` list that leaves `clean` and the exit
  status alone (#90).
- **Site check.** `autoform site check SITE_DIR [--source DOCS_DIR]` reads a
  built site and warns about dead internal links and assets, anchors that
  match no id and malformed links; the explorer's `#node=` routes are not
  anchors and are skipped. With `--source`, it also warns about statement
  cards the build dropped. It exits 0 whatever it finds (#401, #438).
- **Projects.** `autoform project new`, `inspect` and `versions` create and
  inspect Lean projects from a pinned release catalog (#14, #96, #211).
  `project new --library atlas` also requires a shared library at a pinned
  commit, and Setup offers it for a new project (#311).
- **Article ids.** `autoform migrate article-ids BLUEPRINT --write` writes each
  planned id into its article's frontmatter (#191).
- **Dashboard.** `autoform dashboard` serves the built site on a loopback port,
  with a read-only overlay of current claims (#91).
- **Implementation notes.** Each article's implementation handoff is a hidden
  file, `blueprint/.implementation-notes/<article_id>.md`, so articles keep to
  frontmatter and informal mathematics. Every command that loads the graph
  rejects a malformed, empty, symlinked or orphaned note, and `audit` reports
  one left behind after its article is proved (#172, #296).
- **Unpublished folders.** The `README.md` of a folder below `roadmap/` can
  say `published: false`: `check` and `audit` still validate the folder, and
  render leaves it out. On an article or `roadmap/README.md`, `check` rejects
  the key; outside `roadmap/`, as in `blueprint/sources/README.md`, it has no
  effect. A link from a published article into the folder fails `check`, so
  remove those links, the parent's included, first (#381).
- **CI audit.** The generated verify workflow checks `lean:` targets,
  non-root axioms and a kernel replay, without running project code in its
  probe (#156, #190, #247). `check --lean-root` and `audit` index Batteries'
  wanted commands (`theorem_wanted` and its kin) as unproved (#278), and the
  verify workflow accepts one as the target of an article not yet proved
  (#395).
- **Roadmap audit report.** The generated Pages workflow runs `autoform audit`
  after `check` and lists its findings in the step's log, among them unbounded
  and file-wide elaboration limits (#264), without failing the build or
  stopping a deploy (#437).
- **Statement status.** Open statements and conditional proofs (#138). Under
  `open_statements: allowed`, the skills state a cited open prerequisite as a
  `theorem` with a `sorry` proof, not with `theorem_wanted` (#398).
- **Read-back rubric.** Agent Review judges an auditor's read-back of blind
  skeleton packets against the source passage (#53).
- **Feedback.** `autoform debrief`: forms agents fill in about their work. The
  draft's issue link holds the draft as it was recorded, and `record` says when
  a draft is too long for a link to carry more than its title (#195, #414,
  #425).
- **Version.** `autoform --version` prints the installed version and, from a
  plugin checkout, its commit (#397).
- **Muse Code.** The README covers installing the plugin in Muse Code beside
  Claude Code and Codex (#428).

### Changed

- **Commands inform; they do not break a codebase.** Since #325, a new check
  that a repository could fail warns and keeps the exit status (#335, #361), a
  rule AGENTS.md now states (#394). The rule is not retroactive and has
  exceptions, which **Checks that fail** lists.
- **Claims are optional.** A single Formalize run keeps its own workers apart,
  and builds take no claim (#298, #324). The README says how to run several
  sessions on one project (#420). `claim acquire`, `renew` and `release` take
  several targets, all or nothing (#135). A stale lock file in the board is
  reported as one, not as a lost race (#291). `claim list` reads the board
  with one fetch, so a large or busy board lists quickly, and it no longer
  fails while peers renew (#427).
- **Diagnostics.** `lean_diagnostic_messages` runs one `lake --no-build lean`
  per call, so it reports unbuilt or stale imports instead of building them as
  0.5.0 did (#283, #364).
- **Publication.** Render publishes a site transactionally (#165), scales
  linearly (#289, #339), checks the site it replaces without reading it into
  memory (#365), and links files by the spelling Git committed (#301, #333).
  The generated Pages workflow deploys only the commit whose `autoform verify`
  run passed on `main` (#399). Pull requests build the site without deploying
  it, as before, and now so do manual runs, which deployed in 0.5.0. The
  homepage metric counts progress within the scoped roadmap from proof
  semantics, and a finished scope shows as complete (#45, #196).
- **Checks that fail.** Where 0.5.0 passed, these fail `check`, and so the
  generated workflows once CI's `AUTOFORM_REF` moves:
  - `proof: formalized` without `statement: formalized`, or either without a
    `lean:` name (#158);
  - a `## Sources` link that names no file (#287), a hidden path outside
    `sources/` (#321), or a generated view such as `../structure.md` (#380);
  - a dependency on a hidden page, or a dependency or Sources link that steps
    through a hidden folder and back out (#380, #409);
  - a symbolic link under `roadmap/` outside hidden paths, such as a linked
    image (#197);
  - with `--lean-root`, which only the Pages workflow runs, a `lean:` name
    declared in more than one file unless exactly one occurrence is public,
    such as `def f.{u}` in one file and plain `f` in another (#348, #90).
- **Checks.** `check` and `audit` now read the blueprint render publishes, so
  hidden files and folders under `roadmap/` are no part of the roadmap (#380).
  Only the article graph must be acyclic: a cycle that appears only when
  articles collapse into chapters or scopes passes, and views keep every
  authored dependency (#358, #372). `work` and `search` resolve Sources links
  as `check` does, and `audit` resolves the blueprint as the graph loader does
  (#369). A container article that sets a declaration, or a `## Sources` link
  to a hidden note under `sources/`, passes `check` with a warning (#321, #328,
  #361). So does a Sources link with a scheme other than http(s), or a
  `mathlib_file` that is not a portable relative path, which the runtime
  projection leaves out (#314, #325). `audit` reports an unclosed code fence
  (#202).
- **Vault dependency page.** `autoform-visualize` writes
  `blueprint/dependencies.md` as a Markdown summary of the explorer's project
  map instead of a Mermaid graph: statuses, one row per chapter with its
  counts, ready work and dependencies, and the articles while the roadmap has
  at most 120. It replaces only a missing page, its own, or the Mermaid page
  earlier versions wrote. Authored Mermaid diagrams draw locked down, and only
  pages that have one load Mermaid (#378).
- **Skills.** Every skill that runs the CLI says how to launch it (#319). A
  direct Roadmap run finishes one complete planning pass (#44). Before a
  revision lands, Roadmap re-checks retraction on the branch Formalize works
  from (#407). Formalize and Agent Review run Batteries' `unusedArguments`
  linter on finished proofs (#352).

### Removed

- 0.5.0's per-statement dependency pages, `dependencies/nodes/<id>.html`,
  which the explorer replaces (#90).
- `autoform-visualize --link-extension`, since the vault's dependency page is
  read in the vault (#378).
- The language server's diagnostics path (#306), the project routers and
  resident-era REPL pool (#316), and code nothing called (#114, #341, #344).

### Fixed and hardened

- **Lean runtime, REPL and LSP:** bounded transport, a fresh, import-checked
  Lean process for each public REPL call, and cleanup of leftover Lean
  processes (#13, #105, #106, #107, #111, #288, #322, #331, #338, #354, #360,
  #382).
- **Process control:** verified descendant cleanup and real exit statuses when
  SIGCHLD is ignored (#118, #168, #315, #334, #370).
- **Lean source capture and indexing:** filesystem-generation snapshots,
  aliases, multiline declarations, public declarations among duplicates,
  repositories of any size, raw strings that end at their first closer, and a
  foreign `publication.json` that no longer crashes capture (#95, #148, #231,
  #257, #308, #312, #340, #345, #348, #357, #363, #364, #371).
- **Render:** statement and proof bodies keep their compact inequalities,
  math, code fences and autolinks (#362). Render publishes without code links
  where Lean sources cannot be captured (native Windows), keeps code links
  when the blueprint lies outside the Lean repository or is uncommitted, and
  handles a link to the page's own folder (#163, #235, #300).
- **Links:** malformed, angle-bracket and query links, code-only labels, links
  that leave the blueprint, the structure page's links, and code links when
  the Lean root is a subdirectory of its repository (#318, #320, #326, #327,
  #355, #406). Malformed links are reported instead of crashing, an invalid
  URL gets its own `malformed-<label>-url` code, and a bad skeleton locator
  fails only its own article (#337, #347). A link from an ordinary page into a
  hidden path or an unpublished folder keeps only its text, with a warning
  (#416). A symlinked roadmap README no longer hangs `check` (#197).
- **Init and skeleton:** `init` refuses case variants of the paths it manages
  (#244), and skeleton reads `lakefile.lean` when both lakefiles exist, as
  Lake does (#189).
- **Security:** credentials stripped from remote URLs, URL queries redacted,
  nonlocal dashboard requests rejected, and every published page treated as
  untrusted: markup the site cannot trust is shown as typed, and a file a
  browser could run is left out, each with a warning (#98, #125, #215, #245).

### Contracts

Public CLI and JSON shapes are contracts. Since 0.5.0, `autoform-runtime` has
moved from v1 to v4 (#92, #138, #90), `autoform-publication` from v1 to v3
(#165, #90), and `autoform-coverage` from v1 to v2 (#90). Coverage, which
`publication.json` and `audit --json` carry, adds the `INVENTORIED`
disposition, and its `counts` always holds that key. New schema ids include
`autoform-skeleton/v5`, `autoform-work/v2`, `autoform-assumptions/v1` (which
the generated verify workflow reads), `autoform-search/v1` (and
`autoform-search/v2` for `search --library` output only), `autoform-impact/v1`
and `autoform-atlas/v1` for an authored `blueprint/atlas.json`.

### Dependencies

- `markdown` and `pymdown-extensions` are pinned to the Pages workflow's
  3.10.3 and 11.0.1, a major version up from 10.x, and that workflow moves to
  mkdocs-material 9.7.7 (#212). `tomli` is a new runtime dependency, pinned
  below 2.4 on every Python version (#12, #79).
- `cryptography` bounded on both sides of the Intel Mac split (#56, #57);
  `pyjwt`, `urllib3`, `mcp` and `certifi` bumped (#50, #51, #353, #436). An
  optional `browser` extra (Playwright) runs the explorer's browser tests
  (#90).

## 0.5.0 (2026-08-28)

The release this changelog starts from: Autoform v2.0 (#5).
