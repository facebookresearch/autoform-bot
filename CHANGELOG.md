# Changelog

Notable changes to Autoform, newest first. A release's version is the one in
`pyproject.toml`; `uv.lock`, the Claude, Muse and Codex manifests, and this
file's newest release heading carry the same one (`tests/test_release.py`
checks), and `autoform --version` prints it. Add a line under **Unreleased**
with each change worth knowing about, and give the section its version and
date when the release is tagged.

## Unreleased

### Changed

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
- **Pages.** The generated `blueprint-pages.yml` runs
  `autoform site check site --source site-src` after the MkDocs build. Its
  warnings show in the log and never stop a deploy, and `continue-on-error`
  keeps a check that cannot run from stopping one either. A scaffolded project
  keeps its workflow: delete it and rerun `autoform init`, or add the step by
  hand once its `AUTOFORM_REF` is from 0.6.0 or later (#389).

### Fixed

- **Book navigation.** The generated `SUMMARY.md` nests each book page under
  its chapter's README, written in book order. A landing page that linked a
  deep page before its chapters, or a folder with no README in the book, put a
  page a level too deep, and MkDocs refused the nav with a
  `LiterateNavParseError`. Where MkDocs accepted the nav, a page could sit
  under the wrong chapter (#481).
- **Claims.** `autoform dashboard` runs its claim listing's Git in the
  dashboard's process group again, so Ctrl+C or a signal to that group stops
  Git along with the dashboard (#447). On macOS and Windows, a listing shows
  both of two claims whose keys, or folders in them, differ only in case,
  including with Apple's Git, which stored one key's name holding the other's
  lease (#452). The claim contract now says what `--timeout` bounds and which
  listings run Git apart from their caller (#448).
- **Site search.** Each statement card is a search result of its own, titled
  by its statement and opening at its anchor, and its exact `lean:` names find
  it even when they show only as code icons. Notes headings such as Sources
  are no longer results of their own; their text stays in the card's (#390).

## 0.6.0 (2026-10-22)

What has landed since 0.5.0, across 236 merged pull requests, grouped by
theme. Each line names the pull requests to read for detail.

### Upgrading from 0.5.0

- **Pin.** CI at an older `AUTOFORM_REF` rejects `area:`, `navigation:`,
  `catalog:`, `open_statements:`, `published:` and `statement: retracted` in
  `check` and `INVENTORIED` in `render`, so move the pin before using them;
  until then, retract by removing `statement` and `proof` and keeping `lean:`.
  Older refs assign no areas from `blueprint/atlas.json` but publish it with
  every id it names, so a project with unpublished folders moves the pin before
  adding it (#90).
- **Site.** Render refuses to replace the `autoform-publication/v1` site 0.5.0
  wrote, and the dashboard reports it as stale: delete that output directory,
  such as a local `site-src`, once by hand (#165, #90). A v2 site is replaced
  by the first render and reported as stale until then.
- **Links.** 0.5.0's per-statement `dependencies/nodes/<id>.html` pages are
  gone, and links to them no longer resolve. A statement's dependency link is
  now `<scope>.html#node=<id>`, and a `dependencies/full.html#node=<id>` link
  redirects to the smallest scope holding the node (#90).
- **Check.** A repository that passed 0.5.0's `check` can fail this one's; see
  **Checks that fail** below. `check`'s status line counts only leaves that
  carry `declaration`, and with `--lean-root` the distinct `lean:` names it
  resolved. A `blueprint/atlas.json` that is not `autoform-atlas/v1` draws a
  warning from `check`, `render` and `audit`.
- **Checks that fail.** Since #325 a new check that a repository could fail
  warns and keeps the exit status (#335, #361, #394). These checks, older than
  that rule or exceptions to it, fail `check` where 0.5.0 passed, and so the
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
- **Universe binders.** `def f.{u}` is read as `f`, so a `lean:` name for it
  resolves and an alias of one is read through its target. A name declared
  that way in one file and plainly in another is `defined more than once` in
  `check` and `lean-target-ambiguous` in `audit` (#348, #90).
- **Coverage and audit.** `audit`'s coverage line always shows an inventoried
  count, and its text output adds `warning:` lines. A `DECOMPOSED` link that
  reaches only prose or `not_ready` leaves that declare nothing yet is a
  `decomposed-without-declaration` warning, which `render` also prints;
  neither fails (#90).
- **JSON consumers.** Runtime nodes and every `autoform search --json` hit carry
  a nullable `catalog` (`module` for a module catalog); search stays
  `autoform-search/v1`, so a consumer that rejects unknown keys must accept it.
  `audit --json` gains a top-level `warnings` list, which leaves `clean` and the
  exit status alone (#90).
- **Pages and files.** A page holding raw HTML is published as typed text, with
  a warning that says how to write it in Markdown. The site copies a blueprint's
  Markdown pages, raster images, PDFs and plain-text files such as `.lean`,
  `.tex`, `.bib` and `.json`, and leaves out other files under `blueprint/`,
  `.svg`, `.html`, scripts and stylesheets among them, with a warning (#215).
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
- **Diagnostics.** `lean_diagnostic_messages` runs one `lake --no-build lean`
  per call, so it reports unbuilt or stale imports instead of building them as
  0.5.0 did (#283, #364).
- **CI.** A project scaffolded by 0.5.0 keeps its workflows when it moves
  `AUTOFORM_REF`, since `init` writes only missing files. To deploy Pages only
  from verified commits, delete `.github/workflows/blueprint-pages.yml` and
  rerun `autoform init` (#399). On a manual run the new workflow builds the
  site without deploying it, as on a pull request; 0.5.0's deployed. For the
  new CI audit, delete `.github/workflows/autoform-verify.yml` and
  `.github/autoform_audit.py` together before rerunning `init`.

### Added

- **Formalization frontier.** A Markdown roadmap schedules the work.
  `autoform work` lists ready leaves, gives one leaf's context and measures the
  impact of a revision or an import, and the new Formalize skill proves the
  ready leaves (#92, #136, #150, #182, #183, #427).
- **Skeletons.** `autoform skeleton` extracts what a reader must trust for each
  formalized statement, from elaborated terms (#12, #213, #214).
- **Search and reuse.** `autoform search` finds articles before an agent adds
  one. `autoform library` lists the Lean libraries a project depends on and
  writes the index a library publishes, ATLAS among them, which
  `search --library` searches, and the skills reuse what they find (#171,
  #204, #208, #311, #343, #366, #373, #375, #376, #379, #392, #400, #408,
  #440).
- **Dependency explorer.** One explorer on the landing page and every project,
  chapter and scope page replaces the site's generated Mermaid maps, with
  regions from `area:` or `blueprint/atlas.json` and `navigation:` roles (#90,
  #411).
- **Module catalogs.** A `catalog: module` leaf inventories an existing Lean
  module, and coverage gains `INVENTORIED` (#90).
- **Site check.** `autoform site check` warns about a built site's dead
  internal links and assets, unmatched anchors and malformed links (#401,
  #438).
- **Projects.** `autoform project new`, `inspect` and `versions` work from a
  pinned release catalog, and `project new --library atlas` requires a shared
  library at a pinned commit (#14, #96, #211, #311).
- **Smaller additions.** `autoform migrate article-ids --write` (#191),
  `autoform dashboard` (#91), `autoform --version` (#397), hidden
  implementation notes (#172, #296), `published: false` folders (#381), open
  statements and conditional proofs (#138, #398), Agent Review's read-back
  rubric (#53), `autoform debrief` (#195, #414, #425), and README instructions
  for installing the plugin in Muse Code (#428).
- **CI audit.** The generated verify workflow checks `lean:` targets, non-root
  axioms and a kernel replay (#156, #190, #247, #395), and the Pages workflow
  logs `autoform audit`'s findings without failing (#437), among them unbounded
  and file-wide elaboration limits (#264).

### Changed

- **Claims.** A single Formalize run and builds need none (#298, #324, #420).
  `acquire`, `renew` and `release` take several targets, all or nothing
  (#135), `claim list` reads the board with one fetch (#427), and a stale lock
  file in the board is reported as one, not as a lost race (#291).
- **Publication.** Render publishes transactionally, scales linearly and links
  files by the spelling Git committed, and the homepage metric counts progress
  within the scoped roadmap (#45, #165, #196, #289, #301, #333, #339, #365).
- **Checks.** Like render, `check` and `audit` treat hidden files and folders
  under `roadmap/` as no part of the roadmap; only the article graph must be
  acyclic; `work` and `search` resolve Sources links as `check` does; and
  `audit` reports an unclosed code fence (#202, #314, #321, #325, #328, #358,
  #361, #369, #372, #380).
- **Vault dependency page.** `autoform-visualize` writes
  `blueprint/dependencies.md` as a Markdown summary, not a Mermaid graph, and
  refuses to replace a hand-written one (#378).
- **Skills.** Every skill that runs the CLI says how to launch it, a direct
  Roadmap run finishes one complete planning pass, Roadmap re-checks
  retraction before a revision lands, and Formalize and Agent Review run
  Batteries' `unusedArguments` linter (#44, #319, #352, #407).

### Removed

- 0.5.0's per-statement dependency pages (see **Links** above) and
  `autoform-visualize --link-extension` (#90, #378).
- The language server's diagnostics path (#306), the project routers and
  resident-era REPL pool (#316), and code nothing called (#114, #341, #344).

### Fixed and hardened

- **Lean runtime, REPL, LSP and process control:** bounded transport, a fresh
  Lean process per REPL call, verified cleanup of leftover processes, and real
  exit statuses when SIGCHLD is ignored (#13, #105, #106, #107, #111, #118,
  #168, #288, #315, #322, #331, #334, #338, #354, #360, #370, #382).
- **Lean source capture and indexing** (#95, #148, #231, #257, #278, #308, #312,
  #340, #345, #348, #357, #363, #364, #371).
- **Render and links:** statement bodies, malformed links and links that leave
  the blueprint, Windows and outside-the-repository renders (#163, #197, #235,
  #300, #318, #320, #326, #327, #337, #347, #355, #362, #406, #416).
- **Init and skeleton** (#189, #244).
- **Security:** credentials stripped from remote URLs, URL queries redacted,
  nonlocal dashboard requests rejected, and every published page treated as
  untrusted (see **Pages and files** above) (#98, #125, #215, #245).

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

- `markdown` is pinned to 3.10.3 and `pymdown-extensions` to 11.0.1 (up from
  10.x), as in the Pages workflow, which moves to mkdocs-material 9.7.7 (#212);
  `tomli` below 2.4 is a new runtime dependency (#12, #79). `cryptography` is
  bounded around the Intel Mac split (#56, #57), and `pyjwt`, `urllib3`, `mcp`
  and `certifi` are bumped (#50, #51, #353, #436). A `browser` extra
  (Playwright) runs the explorer's browser tests (#90).

## 0.5.0 (2026-08-28)

The release this changelog starts from: Autoform v2.0 (#5).
