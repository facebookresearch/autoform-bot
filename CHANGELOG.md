# Changelog

Notable changes to Autoform, newest first. A release's version is the one in
`pyproject.toml`; `uv.lock`, the Claude, Muse and Codex manifests, and this
file's newest release heading carry the same one (`tests/test_release.py`
checks), and `autoform --version` prints it. Add a line under **Unreleased**
with each change worth knowing about, and give the section its version and
date when the release is tagged.

## Unreleased

### Removed

- **`autoform search --state` and `--declaration`**, deprecated in 0.7.0, are
  gone: filter each `--json` hit's `state` or `declaration` instead, with
  `--limit` at least `total_matches`. `filters` stays in `autoform-search/v1`,
  with both lists always empty (#765).
- **`autoform work list --lean-root` and `work context --lean-root`**,
  deprecated in 0.7.0, are gone: `search --lean-root` prints a Lean name's
  file and line. `lean_targets[].source_file` stays in `autoform-work/v2`,
  always null (#768).

### Fixed

- **Opening a statement leaves a focus the reader moves.** A statement opened
  from the map, search or Contents gives focus to its heading once the reading's
  formulas are set, or to the map where the reader is hidden. A keyboard reader
  who moved on during that wait, to the search box or the next control, had
  focus pulled back. Focus now stays where the reader put it, as Back to map
  already left it (#774).
- **Deeply nested markup no longer crashes `audit` and `render` on a coverage
  page.** Markup nested about a thousand elements deep anywhere on the page, a
  table caption included, raised a RecursionError when the coverage contract
  was read. The page's tables are now read with an explicit stack, as its text
  already was (#657).
- **Counts and readiness in the dependency summary.** The summary
  `autoform-visualize` writes called the chapters' items the roadmap's
  articles, and its status table counted an article marked not ready with no
  declaration, so neither matched `autoform check`. It now gives check's
  article count, with the formalization targets and module inventories apart
  from the rest, and a status table that counts what check counts; where a
  chapter also counts such deferred work, it says by how many. Each listed
  article names what it waits for, up to five prerequisites and then a count.
  The legends no longer call a `mathlib: true` article upstreamed, since it
  restates an existing Mathlib declaration, and "not ready" no longer reads as
  missing blueprint work (#762).

## 0.7.0 (2026-11-11)

What has landed since 0.6.0, across 282 merged pull requests, grouped by theme.
Each line names the pull requests to read for detail.

### Highlights

- Articles' facts live in `blueprint/links/` annotations (`migrate links`,
  `links set`), with frontmatter still read until 0.8.0 (#601).
- A whole-project map takes the explorer canvas's place, a click on a result
  opens its local dependency graph, and the Previous/Next links are gone (#517,
  #552, #574, #599, #759).
- Each statement shows its Lean under it, with its names linked;
  `render --ileans` links them exactly (#640, #641, #684).
- Statement review: blind read-back cards, `review check` and a Review
  disclosure on the site, behind an opt-in marker (#217, #284).
- Source notes become pages of the site, and `publication.json` says where every
  blueprint file went (#593, #658).
- Claims last seven days, and `project worktrees` says which worktrees are safe
  to remove (#714, #435).
- New options: `init --no-verify-workflow`, pinned name lists,
  `## Informally uses`, `tracks_lean: false` and `check --no-site-warnings`
  (#402, #626, #541, #554, #584).
- Python 3.11 to 3.14 (#607, #721).
- Nine deprecations, all gone in 0.8.0.

### Upgrading from 0.6.0

- **Pin.** Before `autoform migrate links blueprint --write`, move CI's
  `AUTOFORM_REF`, in `autoform-verify.yml` and `blueprint-pages.yml`, to 0.7.0
  or later: at an older pin the site shows no Lean progress, and CI can still
  pass. See **Migrating facts into annotations**.
- **Workflows.** `init` keeps existing files. Either delete both workflows and
  `.github/autoform_audit.py` and rerun `autoform init`, or copy the edits
  described under **Statement review**, **Exact names in a statement's Lean**,
  **Check without site warnings**, **Pages** and **Verify downloads try again**.
  A workflow written from `main` after #284 also needs
  `git grep -q '"review_approved"' -- blueprint/links` in its Detect statement
  review step.
- **Plugin.** A 0.6.0 install stays on its tag: remove the `autoform`
  marketplace and add it again with the README's install commands.
- **Python.** Autoform needs Python 3.11 to 3.14; see **Python 3.10**.
- **Audit.** A project with verification turned off can fail `audit --lean-root`
  where 0.6.0 passed; see **Audit refuses a placeholder behind a Mathlib
  article, as CI does**.
- **Deprecations.** Nine, all gone in 0.8.0: eight print a
  `warning[deprecated]:` line, and the `develop-plugin` skill leaves the
  consumer plugin. See **Deprecated**.
- **JSON.** The runtime projection moves from v4 to `autoform-runtime/v6`.
  `autoform search --json` hits gain keys but stay `autoform-search/v1`,
  `search --library --json` gains `refused` but stays `autoform-search/v2`, and
  `publication.json` gains `inventory` and `autoform` but stays
  `autoform-publication/v3`, so a consumer that rejects unknown keys must accept
  them. `autoform-links-plan/v1`, `autoform-pinned-names/v0`,
  `autoform-project-worktrees/v1` and `autoform-review-packets/v1` are new.
- **Imports.** Code that imports a display module, such as
  `autoform_cli.render`, needs `autoform_cli.ui.render`; see **Layout**.
- **Sites.** An existing `mkdocs.yml` stays dark until it takes **Theme**'s
  palette pair, and may drop its MathJax CDN line (see **MathJax no longer holds
  the page up**). Pages no longer end with Previous and Next links.
- **Claims.** A new claim lasts seven days and needs no renewing; see **Claims
  last seven days**.

### Added

- **Publication inventory.** `publication.json` says where each blueprint file
  went (`inventory`): for every file the verified commit holds below the
  blueprint, or else every file render read and each entry it skipped, its URL
  on the site, the repository URL the site links instead, or why the site leaves
  it out (`hidden`, `unpublished`, `mkdocs-excluded`, `generated`,
  `mathjax-config`, `review-testimony`, `graph-input`, `not-utf8`, `canvas`,
  `unsupported-type` or `not-captured`). A hidden or `published: false` folder,
  or a top-level `templates/`, is one entry, so nothing in it is named. It also
  names the Autoform that rendered it, best effort (`autoform`: its `version`,
  its `commit` when an install from Git or an unmodified checkout records one,
  and its repository as `source` only on github.com). The schema stays
  `autoform-publication/v3` (#658).
- **Annotations carry articles' facts.** In a project with `blueprint/links/`,
  an article's literature-links annotation carries what its frontmatter did: its
  Lean names, status, kind, article ID, Mathlib link and the `org.autoform` keys
  ([Annotations](autoform_cli/reference/links.md), #601). The settings stay in
  README frontmatter. Other frontmatter beside an annotation is ignored, with a
  warning, and an article without an annotation keeps its frontmatter.
  Loading never refuses a vault over the folder: every fact left out is warned
  about, counted, and `check` adds literature-links' own validation. Render
  never publishes the folder, whose files the inventory lists as `graph-input`;
  the runtime source revision and skeleton fingerprints cover it, and
  `migrate article-ids --write` writes no ID beside it. Autoform now depends on
  literature-links 1.0.2, pinned to its commit.
- **Migrating facts into annotations.**
  `autoform migrate links blueprint --write` gives every article its own
  annotation, `links/<folder>/<page>.jsonl`, holding its frontmatter facts and
  its article ID, the one `migrate article-ids` plans where it has none; Mathlib
  ends go in `links/mathlib/`, under folder defaults that name the project and
  Mathlib (#601). It loads the graph again and removes the frontmatter facts of
  each article that reads back the same: `proof: formalized` on a definition,
  which no annotation records, a false flag, and `lean:` or
  `mathlib_declaration` text with the same names count as the same. Any other
  article keeps its frontmatter and is listed, as is one whose frontmatter
  beside an annotation says otherwise; frontmatter whose every fact the
  annotation holds is removed. Without `--write` it plans (`--json`:
  `autoform-links-plan/v1`), and `--check` exits 1 until every article is
  migrated. The inventory keys the annotations it writes for an unpublished
  folder once in each of `links/` and `links/mathlib/`, and the generated verify
  workflow counts an approval in `blueprint/links/` as a review artifact. The
  skills record facts with `links set`, and Roadmap moves no article an
  annotation names, since the annotation names it by path. Run `--write` once
  every agent working on the project runs 0.7.0 or later and CI's
  `AUTOFORM_REF`, in `autoform-verify.yml` and `blueprint-pages.yml`, is 0.7.0
  or later: at an older pin the site shows no Lean progress, and CI can still
  pass.
- **Recording facts.** `autoform links set BLUEPRINT ARTICLE... [KEY=VALUE ...]
  [--unset KEY ...]` records facts about articles, by their frontmatter keys,
  for every article named or for none (#601). In a project with
  `blueprint/links/roadmap/_defaults.json`, which `migrate links` writes, it
  rewrites each article's own annotation files, moving an article none annotates
  out of its frontmatter; elsewhere it edits frontmatter in place, keeping every
  other line. It refuses an article another file annotates, or, without that
  template, any annotated article; a file of its own holding anything it would
  not write back; and facts frontmatter could not hold together. It gives an
  article without an `article_id` the one `migrate links` would, and loads the
  graph again, undoing the write and exiting 2 when any article reads otherwise.
  In a project with that template, for an article kept because its frontmatter
  adds keys its annotation lacks and gives none a different value, `migrate
  links` names the call that folds them in. The loader warns about an article
  without an annotation only once the project has that template.
- **Verify workflow off.** `init --no-verify-workflow` writes
  `.github/workflows/autoform-verify.yml` as a stub, for a project the
  generated workflow can't fit, such as one whose roadmap several Lake projects
  back. The stub keeps the workflow's name, triggers and `build` job and only
  reports that nothing was built or audited, so a required check passes and
  the Pages deploy goes on, publishing commits nothing verified. Later `init`
  runs, `--force` included, keep a file there with the stub's marker line;
  delete it to get the workflow back. With a ref, `.github/autoform_audit.py` is
  still written ([Project setup](autoform_cli/reference/project-setup.md#init),
  #402).
- **Pinned name lists.** A JSON Lines file in `blueprint/libraries/`, schema
  `autoform-pinned-names/v0`, names the declarations a library holds at one
  pinned revision; a wiki that cites Mathlib can generate one. Once a list holds
  a name, `check` warns about a `mathlib_declaration` name that no list holds,
  one the lists pin to different sources, a deprecated one, and a `mathlib_file`
  other than the first name's module path. A list whose file or header breaks
  the schema is left out, and a bad record is skipped; `check`, `render` and
  `audit` say which, and `audit` reports it as `invalid-pinned-list`. No exit
  status changes, render never publishes a list, and a project without the
  folder sees no change ([Pinned name
  lists](autoform_cli/reference/pinned-names.md), #626).
- **Worktrees.** `autoform project worktrees` reports which linked worktrees
  are safe to remove after a run and what protects the rest: changes, Git
  operations, unintegrated commits, processes, recent activity and live leases
  from this host. `--remove` takes a saved report, rechecks each entry, removes
  it with `git worktree remove`, never `--force`, and reports the free space
  recovered and how to re-create the checkout; ignored files such as `.lake`
  are not kept (#435).
- **Copied code.** With `AUTOFORM_SUBAGENT_DEBRIEF=1`, the subagent debrief
  asks for the project code a worker copied (`copied_code`), apart from its
  helper-lemma proposals. `autoform abstraction list|target|close` groups the
  copies into clusters and records the lead's decisions in the debrief store,
  with `abstractions/TODO.md` as the to-do list. At the end of a Formalize run
  the lead gives each open cluster to a refactor subagent, which acquires the
  claims it needs when the run uses claims (#491).
- **Import-closure scope.** `work import-impact --module-scope import-closure`
  finds the modules that may import `MODULE`, those whose source holds each
  component of a candidate's name, and has Lean load only the root and glob
  modules among them, with what they import; in a library with no globs it
  narrows nothing. `affected_modules` stays exact, while `affected_articles` may
  list more articles, `article_local` is always `false` and `module_scope` reads
  `import-closure`. An uncaptured `.lean` file that may import a candidate makes
  the command exit 2 and ask for the default scope. Formalize reruns
  `import-impact` with it when the default scope cannot build or load every root
  library, and reports a failure that remains
  ([Work](autoform_cli/reference/work.md#work-import-impact), #449).
- **Statement review.** `autoform review prepare` writes one blind packet per
  declaration of every Lean-mapped article, which must be declaration-sized
  (`autoform-review-packets/v1`). `review record` files a blind auditor's
  read-back of one as a card under `blueprint/readbacks/`, in the card format
  0.6.0 defined (#216). `review check` fails until every such article has
  current cards and a `review_approved`, set with `links set`, equal to the
  review hash it prints, and `render --review` puts a *Review* disclosure under
  every formalized statement. The gate is opt-in: commit
  `blueprint/.autoform-review` holding `autoform-review-policy/v1`, which `init`
  never writes, and the generated verify workflow runs `review check` after the
  Lean build; without the marker, cards or a `review_approved` fail it.
  `review_approved` is a drift checksum anyone can paste, labelled self-approved
  wherever it shows; authorization needs a CODEOWNERS rule for `/blueprint/` and
  a default-branch ruleset that requires code-owner review. A scaffolded project
  keeps its workflow: copy its Detect statement review and Verify statement
  reviews steps from the template, or delete it and rerun `autoform init`
  ([Statement
  review](autoform_cli/reference/skeleton-and-review.md#statement-review), #217,
  #284, #578).
- **Lean under each statement.** A statement whose `lean:` names resolve
  shows the first three declarations under it, as written from the attribute
  lines above the keyword, without the section's variables: a theorem or lemma
  without its proof, anything else whole, a definition's body included. A body
  written at the keyword's own indent is cut short. A declaration over 14
  lines folds behind a line that names it. Keywords, comments and strings take
  the page's code colors, in the map's reading pane too; site search skips the
  code. A catalog shows none, and a `--review` render leaves the Lean to each
  card's review, after its read-back. Render cuts the text from the Lean
  sources it captured, so a render without them shows none (#640).
- **Names in a statement's Lean.** A name in those declarations that the
  project declares shows that declaration in a card when the book's reader
  hovers or focuses it: a definition whole, its first 12 lines if longer, a
  theorem its statement, with its file and line. A name some statement
  formalizes links to that statement; another links to its source, or takes
  keyboard focus when the site has no source links. Render looks a name up
  among the project's own declarations, in the namespaces around the
  declaration and those its file opens, and takes only one the file can see:
  one above it in that file, or one not private in a file it imports.
  Binders, patterns, fields, attributes, tactics, one-letter names and names
  the project does not declare, such as Mathlib's, stay text (#641).
- **Exact names in a statement's Lean.** `render --ileans DIR` reads a Lean
  build's `.ilean` files, such as `.lake/build/lib/lean`'s, which record the
  declaration Lean resolved each name in the sources to. A name in a
  statement's Lean then links exactly that, whatever namespace or `open` it
  was written under: a project declaration as above, and a constant of a
  package Mathlib's documentation covers (Mathlib, Batteries, Init, Std, Lean
  and others) to its page there, found by its full name, with no card. Locals,
  binders, notation such as `+` or `ℕ`, field notation such as `h.le`, and
  other packages' constants stay text. A file whose `.ilean` file is missing,
  unreadable or of a format other than the one Lean writes since v4.27 links
  as without one, and so does a declaration the file shows to be older than
  the source, as after an edit since the build; render says which. An edit the
  file cannot show, such as to an `open` line, is not caught, so render a local
  site with a fresh build.
  Mathlib's documentation follows current Mathlib, so a constant renamed since
  the project's pin lands on its not-found page, as does a recursor such as
  `Nat.rec`, which the documentation leaves out. The generated
  `autoform-verify.yml` uploads the build's `.ilean` files at the end of a
  passing push to `main`, and `blueprint-pages.yml` passes that run's files to
  render when the download succeeds and its `AUTOFORM_REF`'s render lists
  `--ileans`; a failed upload or download fails nothing ([The published
  site](autoform_cli/reference/site.md#pages-deploys)). A scaffolded project
  keeps its workflows: delete them and rerun `autoform init`, as 0.6.0's CI
  note says, or copy three edits from the templates: verify's last step, the
  Pages fetch step with the render step that reads it, and `actions: read` in
  the Pages workflow's `permissions`. Pages takes the files only in a
  workflow that deploys verified commits, as 0.6.0's does (#684).
- **Source pages.** Markdown notes under `blueprint/sources/` are pages of the
  site, and links to them stay on it, where a render with repository links used
  to send every note to the repository (#593). Images under `sources/` are
  copied beside them. At a verified commit, a hidden note, the folder itself and
  its other files, such as scans, data or a type the site leaves out, are linked
  in the repository; a link to a file there that the commit lacks, such as a
  scan kept out of git, keeps only its text, with a warning, rather than failing
  the strict build. A note's image of a linked file keeps its text, with a
  warning, and `render --review` accepts a passage cut from one it reads, which
  a hidden one is not; `publication.json`'s inventory lists the notes and images
  as `published` and the other captured files there as `linked`. A reference
  definition in a quote or a list item moves as any other does. A folder below
  `sources/` whose README says `published: false` stays off the site, with or
  without a verified commit, as one below `roadmap/` does, and every link into
  it, coverage evidence included, keeps its text, with a warning;
  `render --review` refuses a passage from it. Check's graph does not read the
  setting there, so render and check warn about a README whose setting does not
  parse, and about a `published: false` where it takes no effect: in
  `sources/README.md`, another spelling of a README, or a note's frontmatter.
  The nav lists the notes after Coverage, headed by `sources/README.md` when
  there is one, and the landing page links that page beside the coverage view.
- **Check without site warnings.** `check --no-site-warnings` skips the warnings
  about the pages the site would publish, which `render` prints too. The
  generated `blueprint-pages.yml` passes it, since render runs two steps later,
  when its `AUTOFORM_REF`'s `check --help` lists it, so an older pin runs the
  full check. On one CI runner ATLAS's check took 58 s with the flag instead of
  149 s, and render printed every warning it skipped. A scaffolded project keeps
  its workflow: add the flag by hand once its `AUTOFORM_REF` includes it (#584).
- **Panes you can resize and shut.** In the book's workspace, Contents and the
  reading pane resize from splitters on their inner edges, by pointer or keys.
  A pane dragged nearly shut shuts while the drag goes on, and opens again if
  the drag comes back; Enter on its splitter shuts it too, and a tab at the
  map's edge brings it back. Dragged on across the map, the reading pane shuts
  the map once the map would be under 120px (until then it keeps 160px), and
  then Contents, so the article takes the whole width, its lines held to
  880px; End on the splitter shuts the map too, and a tab at the reader's edge
  brings it back, as Back to map does. An overview's collapsed reader keeps
  the map beside it. "Map only" in the trail row hides Contents and the
  reader. The choice holds on every page of the site, as the theme's own
  choices do; a double-click, Delete or a Mac's delete key on a splitter
  restores the usual width. While a pane is dragged, what the map shows stays
  where it is on the page, or the whole project, when that is the view, stays
  fitted, and nothing flies when the drag ends or when the reader's text
  reflows as it narrows or widens; the reader keeps its place. A drag that
  turned the map's stage between portrait and landscape repacks the map as it
  ends. The whole project, when that is the view, or a part's page the view
  still shows as the map framed it, with no statement selected and no other
  part picked, is fitted again. The selected statement's card, or else a part
  picked with one click, keeps its place and scale if the map drew it in view
  as the drag began or ended, moved only to show it whole; a part too large for
  that gives way to the next rule. Otherwise a card stays where it was: the
  statement let go of while the view showed it (until a flight, a pick or a
  move of the reader's), at its size where it shows its title, or else the
  card nearest the middle, with the part it was looked for from kept at its
  scale: a picked part that gave way, or else the part the view is in, which is
  the part it was in as the drag began where that holds the one it ends in.
  Then the parts that hold it move, card and all, only as far as it takes to
  show them whole, or to leave no side of the stage empty while they run off
  the other, and the statement let go of shows whole where the stage has room
  for it. Where touch is the main pointer, as on a tablet, the splitters are
  24px wide, and Windows' forced colours keep the splitter's line and Map
  only's pressed look. A phone keeps its sheet (#644, #717, #728).
- **Whole-project map.** Every rendered site gets `dependencies/map/`, one
  zoomable page for the whole project. Sections (authored areas, or the
  top-level chapters) open into their parts and parts into their results as the
  camera closes in, and a part opens once its parts can be named. Search reaches
  any result by title or Lean name; the reading pane names the result's kind,
  such as Theorem or Definition, and shows its statement and proof from its
  page, and what it uses and what uses it. One click picks a part, which the
  reading pane heads "Part", and shows the parts it builds on and the parts that
  use it, with links flowing between them; a second click goes inside, and Up or
  Escape goes back a level. The map counts its sections and lists how they build
  on each other, and a card names its status under its title only while its
  result is unfinished, since a finished one's fill says so. On a narrow screen
  the map takes the width, with the reading pane under it. The data loads one
  level at a time from `dependencies/map/data/` (#517, #545). The explorer's
  stage shows the same map beside the book: the page being read sets where it
  opens, a result picked on it opens in the reader and a part opens its chapter,
  the statement at the top of the reader is outlined on it, and the status
  filter dims the results it leaves out (#552). There it reads its data through
  the book's router, under the book's timeout and same-origin rules.
- **A result's local graph on the map.** A click on a result's card on the
  whole-project map, standalone or beside the book, opens its local dependency
  graph in place of the map: what it builds on, to its left and further left the
  more steps back once links implied by others are dropped, and what it feeds
  into, the eight most used first. Level 1 shows two steps back and everything
  it uses directly, level 2 three steps and level 3 all of it, at most 150
  results, the nearest first. Among them, one used by at least 10 others and by
  as many as the 98th percentile is a chip, not a card, and a dotted "via n"
  link stands for the n results between that are not drawn. A click inside moves
  the graph there; Escape and Back return to the map as it was. Render writes
  the relations it reads to `dependencies/map/data/relations.json` (#759).
- **Links at the end of an article.** In the book's reader, an article's links
  to other articles, and theirs to it, end the article as "See also" and "Linked
  from" lists that scroll with it, ten links each, then up to twenty more per
  click. On a chapter's page, the page's own lists end the page, and the
  selected statement's end its card, the statement's article. An article with
  neither kind of link shows no lists, and where some pages could not be read, a
  line says the lists may miss some (#703).
- **Informal dependencies.** Under `## Informally uses`, an article can list
  what its source's proof cites but its Lean proof need not use. The links
  resolve like dependencies and appear in the runtime projection as
  `informal_dependencies`, but derived status,
  readiness and `work assumptions` never count them, and the article's summary
  skips the section. `check` warns about a link that does not resolve, one whose
  target depends on the article (it stays listed), and the heading written below
  a dependency heading, where its links count (#541). The Roadmap skill puts
  under it what a source's proof cites but the planned Lean proof will not use,
  as when a library proof is reused (#556).
- **Projects that track no Lean.** `tracks_lean: false` in `roadmap/README.md`
  takes Lean progress out of the book's pages: the landing page's figures and
  Next up, the statement status marks, the status dots on dependency references
  and the chapter progress lines. The explorer drops its status filter and
  status words, and the whole-project map its tints, progress bars and status
  words; its cards name their kind instead. Neither search box mentions Lean
  names, though both still match them. In place of the figures, the landing hero
  shows `blueprint/README.md`'s opening paragraphs, not just its first sentence,
  up to the next heading or list. Check warns about the setting anywhere else
  under `roadmap/`, or with a value other than true or false, and ignores it
  (#554).

### Changed

- **Formalize's leaf steps.** What whoever works a leaf does, from its branch
  to its report, moved to `skills/formalize/references/leaf.md`, which links
  the import-edit and integration steps `SKILL.md` keeps; the lead gives each
  subagent its path. The run's report now names Human Review handoffs, and
  leaf.md lists what a worker reports (#734).
- **An article's revision covers its annotation.** Where an annotation in
  `blueprint/links/` counts for an article, the `article_revision` that `work`,
  `search` and `debrief` report hashes the facts it gives along with the
  article's bytes, so Formalize's revision check sees an edit to the article's
  Lean name, status or `not_ready`. A line about another article in the same
  file leaves it alone, and an article with no annotation keeps the hash of its
  bytes. An annotated article's revision changes once on upgrade, so a worker
  that read it before reads `work context` again. The runtime projection, now
  `autoform-runtime/v6`, carries it as `article_revision` (#707).
- **Roadmap's final audit reads the Lean.** Roadmap runs `autoform audit` with
  `--lean-root .`, as Formalize does, so a pass ends clean only when Formalize's
  first audit would too. It reports, rather than fixes, a finding or warning
  that only a Lean edit clears, such as a limit on existing code or an open
  statement's `lean-target-wanted`, and, as Formalize does, leaves a
  `lean-target-deprecated` the revision contract expects (#698).
- **Audit warns about an open statement a wanted placeholder states.** Under
  `open_statements: allowed`, `audit` with `--lean-root` warns under
  `lean-target-wanted` about a theorem that records only `statement: formalized`
  against a `theorem_wanted` or `proof_wanted` target, which no dependent can
  cite. `clean` does not change (#693).
- **Setup's library rule.** For a library outside the catalog, Setup writes the
  Lean version with its `v`, as `project new` requires, and asks when the
  library's revision is unknown (#682).
- **Every sentence in review.** Agent Review's roadmap rubric checks that every
  mathematical sentence, proof sketch included, is true as written, not only
  that planned main results preserve their source. A false sentence lowers
  source fidelity and caps nothing (#689).
- **Mathlib results in review.** Agent Review's roadmap rubric says when cited
  declarations state an article's result, and has the reviewer quote their
  hypotheses and conclusions before judging. A `mathlib: true` that fails this
  caps the rubric's usability score at 2, and Roadmap sets `mathlib: true` only
  when verified Mathlib declarations state the result (#668). Formalize's library
  search rules, which Roadmap follows, use the same definition: a library hit
  that needs only hypotheses the result gives or implies, or several that
  together state it, is reused like an exact one (#679). When a theorem holds
  without one of the source's hypotheses, Formalize proves the source's
  statement from a general declaration that already exists instead of re-proving
  it as a new lemma (#686).
- **The landing's coverage line** leads with the source areas split into
  statements: "5 source areas, all split into statements", or "3 of 5 source
  areas split into statements" before the other dispositions (#655).
- **How a library states and proves a result.** In a vault about a library,
  Roadmap lets an article say, in English and TeX, how that library states and
  proves each result, and Agent Review's roadmap rubric no longer counts that
  text as Lean-side (#691). Formalize keeps it in the article too (#702).
- **Search warns about a library it cannot verify.** `search --library` leaves
  out a library it cannot verify, with a `warning: library NAME:` line on
  standard error and, in `--json`, which stays `autoform-search/v2`, an entry
  under a new `refused` key, and still prints the blueprint's hits and the other
  libraries'. 0.6.0 exited 2 with an error instead (#664).
- **Search hits carry what an article records.** Each `autoform search --json`
  hit adds `statement` and `proof` as recorded, beside the derived `state`, and
  `origin`, `not_ready`, `review_approved`, `discussion`, `area` and
  `navigation`, read from the article's annotation or frontmatter as every
  command reads them. `not_ready` is a boolean, like `mathlib`, the others are
  null when unset, and `area` leaves out the one `atlas.json` assigns. Search
  stays `autoform-search/v1`, so a consumer that rejects unknown keys must
  accept them (#715).
- **Library index check.** `library index --check` counts an index as current
  when only the Autoform version that wrote it differs, so a new release no
  longer turns a publishing library's check red. An index another probe revision
  made is stale, said without running Lean, and a stale index names the records
  generation gives otherwise (#664).
- **Library index: imported libraries.** `library index --lib A` also indexes,
  transitively, each library of the package that A's modules import from, since
  a change there changes A's records. A note names each such library and the
  `lake build` command CI must run before `--check`, which can name modules as
  well as libraries; such a library must build completely. An index made earlier
  keeps its `--check` verdict, with a warning naming the files it leaves out,
  until it is regenerated (#673).
- **Vault pages on Windows.** `autoform-visualize` writes the dependency
  summary and structure pages with LF line endings on Windows too, as on every
  other system, so a vault regenerated on both no longer flips them (#629).
- **Helpers beside an article are its own.** `work impact` and
  `work import-impact` give a declaration no article names, and no named
  `parent` ancestor owns, to the one article that names declarations in its
  module, when exactly one does. A worker's own helpers no longer get a `lean/`
  claim key of their own, and in `work impact` that article also counts for
  `undeclared_dependencies` and `unused_statement_dependencies`. In a module
  several articles share, the helpers keep their own keys (#492).
- **Wrong import-impact module.** `work import-impact` refuses a `MODULE`
  outside the project before the freshness check and the probe; when the
  article's declarations are in one module, the error names it (#492).
- **Statements go by their names.** A statement's heading and every link to it,
  in Uses, Used by and Assumes, show its name instead of a generated number:
  "Theorem 55 (Bézout's theorem)" reads "Bézout's theorem", with the kind on a
  small line above the name. A title that is only the kind word, or empty, is
  named by the kind. Statements that share a title now read alike. Anchors and
  links into a page don't change (#612).
- **Coverage view.** The coverage page shows its contract's table as a view: a
  heading for each disposition with the number of rows under it, and each row
  led by its area, with its evidence as written. A `DECOMPOSED` row also lists
  the declarations its links reach, in book order and with their status, behind
  a line that counts them, with their states when the project tracks Lean; a
  list of one is shown open. The list replaces evidence that is only links, each
  straight to one of those declarations; beside evidence that stays, it shows
  the first 50, or as many as `coverage_list_limit` in `roadmap/README.md` says,
  and counts the rest. A page whose published table is not the contract's gets
  no view, and render warns. The rest of the page is unchanged. In the sidebar
  the page still follows the book's chapters, now ahead of any source notes
  (#631, #645).
- **One way to read a tree.** Lean sources, the blueprint and an output site
  are read by path in one walk that never follows a symbolic link, and a second
  walk must find the same folders, names and `stat` signatures; any difference
  is a change, which the Lean commands retry as before. This replaces the
  directory-descriptor capture and its Windows fallback (about 1,250 fewer
  lines). On native Windows, `check`, `render` and the other Lean commands now
  read Lean sources, so sites get code links there too, and `render
  --require-declarations` no longer fails for declarations it could not look
  up. A change that appears and is undone between the two walks is not seen,
  as with the old engine between its checks (#654).
- **Titles typeset their math.** Statement headings and Next up typeset a
  title's formulas, as the reader's dependency references already did, instead
  of showing its TeX as typed (#563).
- **Contents groups chapters by area.** When a project's chapters name two areas
  or more, Contents heads each area's chapters with its name, as the map groups
  them into sections. An area with a single chapter, and a chapter without an
  area, keep their place unheaded (#618, #703).
- **Lean index names.** The Lean index leaves out names Lean cannot declare,
  such as `$typeName.toBitVec_nsmul`, which it read from commands inside
  syntax quotations (14 in Mathlib). An `alias` name takes Lean's identifier
  characters: `alias ℘ := t` declares `℘`, and `alias Жук := t`, which Lean
  refuses, declares nothing (#623).
- **Landing.** The ready figure links the list of what it counts, each count
  shows once with thousands separators, and the brand gradient is off the
  figures, bar and header hairline. Source coverage reads in plain words
  (#503).
- **Declaration captions.** A `declaration` command with no caption, such as
  `isomorphism`, is captioned as written, first letter capitalized, in the book
  instead of "Node". Modifiers
  such as `noncomputable` and now `public` no longer hide a known command from
  the caption, from progress or from `audit`'s check of the Lean target's kind,
  which skipped `noncomputable def`; `public def` also counts as a definition,
  and `irreducible_def` and `opaque` read as definitions. `check` names each
  command with no caption once, with the articles that use it; `audit` does not
  check those articles' Lean target kind either (#485).
- **Integrated reading.** The book, theorem search and dependency map share one
  persistent workspace. The outline, headed "Contents", follows authored
  containment, includes root-level pages and labels collections as subheads;
  Home returns to the site. Search labels a topic's hit "Section in `<place>`",
  and Ctrl+K or Command+K focuses it, as / does from inside the workspace. Empty
  source choices are hidden. A page opens in the book's layout, its article in
  the reading pane while the map loads (a page that only lists its child topics
  shows its reader folded to its title instead, as the book does), though a
  phone keeps Material's page until the book opens, and when the explorer's data
  cannot load, its notice offers to try again. The site nav's top entries are
  Home and Mathematics, where 0.6.0's were Home, Book and Graph (#442, #444,
  #636, #703).
- **Explorer payloads.** Scope payloads carry only what the book reads: each
  node's title, kind, status, area, Lean names and summary, the palette and the
  page's trail. The x and y positions 0.6.0's Python layout computed are gone
  (#442), and so are card dimensions, region and member counts, and relations,
  which nothing read: a statement's page lists its own. `edges` stays, empty.
  The no-JavaScript fallback's graph-data link opens the search index (#597).
- **Old dependency pages.** `dependencies.html`, `dependencies/full.html` and
  the chapter and section pages under `dependencies/` redirect to the matching
  book page and keep the address's fragment, so `#node=` links and old view and
  filter links still open there; without JavaScript they refresh to it. They
  load no explorer, stylesheet or data of their own, and
  `dependencies/full.json` is no longer written (#597).
- **Book order.** The site nav, Next up and the explorer's outline and search
  follow one reading order, down the containment tree, so a landing-page
  shortcut no longer reorders the book. Pages their parent does not link now
  join the site nav, after the pages it links (#495).
- **Book copy.** The landing lead keeps link text, code, formulas and whole
  sentences. Next up says which Lean step is next. A chapter's progress is one
  line of counts whose status meanings show on hover, gone when the chapter has
  fewer than two countable statements. Unplaced statements sit under "Other
  statements", and the map's fallback stays out of search results (#494).
- **Listings.** The Codex and Muse listings name the plugin AutoformBot, and
  Codex shows the Roadmap, Setup and Develop Plugin skills under those short
  names (#463).
- **Messages.** The read and publish errors that `skeleton`, `library index`,
  `work impact`, `work import-impact`, `review prepare`, `review check` and
  `render --review` share no longer call the project files they read "skeleton
  input", or the files they write "skeleton output" or "packet output"; they
  name the path alone (#506).
- **Claims.** `claim list TARGET...` fetches and reads only those claims.
  `--timeout` (120 seconds by default) bounds a listing's reads of the board,
  though not the lock-file check after it. A slow step is named on stderr, and a
  failure to reach or read the board goes to stderr labelled `timeout:`,
  `contention:` or `transport:` with nothing on stdout; a timeout says
  `ran out of time after <N>s while <step>`. Git no longer waits at its own
  terminal password prompt in claim commands, though `ssh`, a credential helper
  or an askpass program can still prompt. A `claim list` or `claim cleanup`
  listing that runs out of time or is interrupted no longer leaves its transport
  running. Formalize treats a labelled `claim list` failure as an unreadable
  board, not as a board without claims, and an import-edit recheck whose
  `claim list` fails as a conflict (#432).
- **Claims last seven days.** A new claim's lease expires after 604800 seconds,
  not 1500, so nothing needs to renew it, and records the `target` that `claim
  list` shows and `claim release` takes. A claim nobody releases now blocks its
  target, and `project worktrees --remove` on that host, for up to a week; free
  a stopped worker's claim with `claim release --worker-id OWNER TARGET` (#714).
- **Sessions with claims.** With claims, each session leads from its own clone
  and checks `git worktree list` before dispatching, workers name their branch
  with `claim acquire --note`, now documented, and no message grants ownership;
  none completes a leaf (#434, #532).
- **Formalize without claims.** Workers run `work list` and `work context`
  without `--lean-root`, and the lead dispatches each leaf's `work list` item.
  Within a run, whoever works a leaf first creates its branch
  `work/<article_id>`, which Git refuses twice in one clone; the branch goes
  once the work lands or the leaf is abandoned. The skill suggests, without
  requiring, landing as a fast-forward that rebases and rechecks when the shared
  branch moved, then taking the next leaf, preferring one the landing unlocked.
  Claims are used only when the user asks for them or another session already
  does (#429).
- **Setup donor.** When the user names an existing Lake project or packages
  folder, Setup copies each package the new project locks that the donor holds
  clean at that revision, copy-on-write where the file system supports it and
  never as a symbolic link, before any Lake command fetches the rest (#275).
- **Formalize worktrees.** A new worktree gets a copy of the lead's
  `.lake/packages`, copy-on-write where the file system supports it and never
  a symbolic link, before its first build. `lake exe cache get` then downloads
  nothing; before, each worktree re-cloned about 0.5 GB of packages and
  unpacked another 7.5 GB Mathlib build (#275).
- **Generated import roots.** `work import-impact` reports `module_generated`
  when the repository's Git attributes set `linguist-generated` on MODULE's
  file, or make it `true`, normally through a committed `.gitattributes` line
  such as `/Lib.lean linguist-generated`, and leaves its module target out of
  `claim_targets`. The user's and the system's attribute files do not count.
  Formalize skips the claim only when CI regenerates and checks the root, and
  regenerates it from the merged tree, so independent leaves, other projects'
  included, publish without waiting on a claim on it. Any other root keeps the
  import-edit claims (#466, #496).
- **Render.** An article's page lists its `mathlib_declaration` names in a
  Mathlib row, as text without a link, so a `mathlib: true` article names the
  declaration it restates (#388).
- **Large skeletons.** Packets, the report, and the check that guards their
  publication are hashed and written in pieces, so writing a large report no
  longer holds every packet in memory, and shared closure material is no longer
  parsed and escaped again for every root; output bytes and hashes are unchanged.
  `skeleton` names each phase on stderr as it starts (`skeleton: probing <n>
  modules...`, then `assembling` once no probe runs, then `writing`). Agent
  Review and Formalize say what a user's pause of skeleton evidence during proof
  development still requires (#497).
- **Builds.** `project new` turns on Lake's artifact cache with
  `enableArtifactCache` and `restoreAllArtifacts`, so checkouts and worktrees
  on one toolchain reuse each other's builds; Setup offers the same two lines
  to an existing project. A probe that finds no `.olean` for a module Lake
  calls built now names the cache-without-restore cause (#431).
- **Process checks.** `autoform` and the Lean runtime share one process-table
  scan. A process whose environment cannot be read for any reason but
  permission now fails a probe run instead of being skipped, and a probe
  whose process group cannot be inspected fails with "could not inspect its
  process group" (#518).
- **Library results in Roadmap.** When a shared library already proves a
  result, Roadmap lists under the article's `## Proof depends on` only the
  articles that proof uses, and keeps the source's route in the proof sketch,
  so the article is not blocked on steps its proof never needs; Agent Review
  checks the same (#443).
- **Dependency explorer.** The rail and the details typeset the TeX in titles
  and summaries, with the articles' input and the project's macros, where the
  explorer's own MathJax calls had typeset nothing on a generated site
  (#455). Reading controls, breadcrumbs, search
  and supported inventory titles now typeset safe title parts; inventory math
  is deferred and unsupported inputs stay literal (#444).
- **Theme.** A new project's site opens in the reader's own light or dark
  setting, with Material's sun or moon to switch it, and the explorer's head
  carries the same switch (#444). Setup leaves an existing `mkdocs.yml` alone,
  so an existing site stays dark until the `palette:` and `scheme: slate` lines
  under `theme:` are replaced with the scaffolded pair:

  ```yaml
    palette:
      - media: "(prefers-color-scheme: light)"
        scheme: default
        toggle:
          icon: material/weather-night
          name: Use dark theme
      - media: "(prefers-color-scheme: dark)"
        scheme: slate
        toggle:
          icon: material/weather-sunny
          name: Use light theme
  ```
- **Pages.** The generated `blueprint-pages.yml` runs
  `autoform site check site --source site-src` after the MkDocs build. Its
  warnings show in the log and never stop a deploy, and `continue-on-error`
  keeps a check that cannot run from stopping one either. A scaffolded project
  keeps its workflow: delete it and rerun `autoform init`, or add the step by
  hand once its `AUTOFORM_REF` is from 0.6.0 or later (#389).
- **Check.** `check` also warns about a code fence that autoform reads as left
  open but the site ends sooner or reads as text: one indented one to three
  spaces, one inside a list item, or one whose options the site rejects.
  Coverage, the link checks and render's link rewriting read everything after
  it as code, so render shows such a page as typed until the fence is closed,
  at the indent of its opening line, with options the site accepts (#201).
- **Pull request reviews.** Formalize ends each pull request a run opens, or
  adopts and may edit, with an `Autoform project:` line. On any machine, the
  lead finds the open ones that carry it and come from the project's repository,
  by owner and name, reads their reviews and inline comments when a run starts
  or resumes and at each frontier re-read, decides each finding against the
  current head, and repairs the oldest first, after any it is stacked on. A run
  does not stop while an accepted finding awaits repair (#483, #525).
- **Duplicate credit.** `audit` warns `duplicate-lean-target` at each target
  that takes credit for a declaration another target also takes: the first
  `lean:` name of a target recording `statement: formalized`, or the first
  `mathlib_declaration` name of a `mathlib: true` target, compared as Lean
  names. Later names, catalogs, containers and placeholders recording neither
  take no credit (#193).
- **Node size.** `autoform audit` reports `node-too-large` as a warning, not
  a finding, so it no longer fails the audit or the doctor's `lean targets`
  check. Its limit is four times the project's median, so finished work
  landing elsewhere could fail the audit of a node nobody edited (#429).
- **Loader warnings.** A `navigation` role other than `topic` on an article that
  sets `declaration`, and an implementation note or notes folder no command can
  use, no longer stop every command. `check` warns about each, and about a notes
  folder that can be listed but not searched, which crashed every command.
  `audit` reports a note as `invalid-implementation-note` where it used to
  report it as `invalid-graph`, and no longer reports the role. The article
  ignores the role, nothing reads through a linked notes folder, and render no
  longer checks the notes: `graph.unpublished_content`, which gave it the
  unpublished articles' IDs, returns only the folders. CI at an older
  `AUTOFORM_REF` still refuses both, so move the pin before relying on the
  warnings (#604).
- **Library listing.** Formalize tells the user about the packages
  `library list` leaves out in one line that counts them by reason, not one line
  each (nine or ten on a Mathlib project), and searches the blueprint alone when
  the listing fails. Agent Review lists a module's declarations from the
  `--json` hits whose `module` is exactly that module, with `--limit` at least
  `total_matches`; searching the module's name also returned other modules'
  declarations, and at most 20 (#662).
- **Self-caused search refusals.** Formalize fixes a search refusal its own
  arguments caused, such as `--limit 0` or a query that folds to nothing, and
  searches again, as Roadmap does, where it reported the leaf as blocked (#676).

#### Performance

- **Lean root reads.** With `--lean-root` (deprecated for `work`), `search`,
  `check`, `work list` and `work context` scan only the Lean files that spell
  the last component of a name they look up, and `search` looks up only the hits
  it prints. Every file is still read, so the source revision, the errors and
  the output are unchanged; on a 13,698-file Lean root, indexing a 36-name
  blueprint's declarations took 4.7 s of CPU instead of 13.6 s. `check` reads
  the Lean root once where a shared library's citations made it read it twice,
  and again only when a name is declared nowhere, for the error about its
  parent. `audit` strips comments only from files that hold a resolved target
  (#429).
- **Lean scan.** Reading a Lean file's declarations tries its six command
  patterns only on a line that starts the way one of them can: with `#exit`,
  `namespace`, `section`, `mutual`, `end`, an attribute list, a modifier or a
  declaration keyword. It tried all six on every line, though almost every
  line is a proof. Each line's indent is measured only in a file with a
  structure or class, whose fields need it. On one CI runner, indexing
  ATLAS's 13,698 Lean files took 9.2 s instead of 13.0 s; the declarations
  found are the same (#576).
- **Search index size.** `dependencies/index.json` no longer repeats links the
  explorer doesn't read. A row whose route the explorer opens drops its context
  `url`, and only container rows keep `data_url`, since every page names its
  own graph data. The file is written as ASCII, so one symbol above U+00FF no
  longer doubles the memory of the decoded text. ATLAS's index goes from
  42.1 MB to 32.2 MB, and on one CI runner search and the outline were ready
  2.1 s after Home started loading instead of 2.8 s in WebKit, and 1.6 s
  instead of 1.9 s in Chrome (#550).
- **Explorer search.** A keystroke that extends the query ranks only the
  previous query's matches, not every row of the search index, and a one-word
  query no longer joins each row's fields. Results and their order are the
  same. On one CI runner, typing "zeckendorf" on ATLAS's Home took 110 ms of
  script in Chrome instead of 562 ms, and 220 ms instead of 1,272 ms in WebKit
  (#614).
- **Render reuse.** Render reuses one converter for statement and note bodies,
  where it built one for each (about 16,000 on ATLAS), walks the book once
  instead of twice, keeps every title's label from the search index for the
  map, and compares path parts where it called `relative_to`, which is slow
  from Python 3.12. On one CI runner ATLAS rendered in 312 s instead of 340 s,
  with the same output (#582).
- **Render workers.** With 4,000 or more node pages, `render` and `check` read
  them in up to four worker processes: on POSIX, from Python 3.11.5, run as the
  `autoform` command or `python -m autoform_cli`. A page a worker cannot read,
  or a pool that breaks or will not start, is read as before, so the site and
  the warnings are the same. On a 4-processor CI runner, ATLAS rendered in
  225 s instead of 337 s, and checked in 145 s instead of 255 s (#606).
- **Graph loading.** Loading a blueprint answers from what discovery already
  read, instead of asking the disk, for a dependency spelled as `..` steps and
  plain names and for the README that contains an article. It reads no page's
  links for ones into an unpublished folder when there is none, and path
  containment compares path parts. On one CI runner ATLAS's graph loaded in
  25.8 s instead of 52.3 s, with an equal result, and `check`, `audit` and
  `render`, which each load it, took 29, 28 and 24 s less (#568).
- **Check and render paths.** One reused converter reads every page, and a run
  resolves each page, node and linked path once, remembering it until the run
  ends instead of asking the disk again for every link to it. `check` uses about
  a fifth less CPU on a 1,806-article vault, and on one CI runner ATLAS rendered
  in 418 s instead of 441 s; neither command's output changes (#415, #569).

#### For contributors

- **Layout.** Display code moved to `autoform_cli/ui/` under the same module
  names: `render`, the explorer (`dag_viewer`, `graph_pages`, `graph_views`,
  `site_index`), the project map (`project_map`), `dashboard`, `site_check`,
  `visualize` (`autoform-visualize`), `publishable`, `mathjax`, `tex_plain` and
  `_page_pool`, with their JS and CSS (`autoform_cli/ui/assets/`); its tests
  moved to `tests/ui/`. Commands, flags, JSON and output files are unchanged.
  Only `render`, `site check`, `dashboard` and `check` load display code now, so
  a broken display file no longer stops the others, such as `audit`, `claim`,
  `work` and `search`. Code that imports `autoform_cli.<module>` for one of
  these needs `autoform_cli.ui.<module>`, and tools that hardcode
  `autoform_cli/assets/` need `autoform_cli/ui/assets/`. `project_macros`,
  `TEX_MACROS` and `NEUTRALISED` moved from `mathjax` to
  `autoform_cli/tex_macros.py`, `DECLARATION_LABELS` from `render` to
  `autoform_cli/status.py`, and `SOURCES_DIR` and `PublicationError` from
  `render` to `autoform_cli/graph.py`. `tests/test_ui_boundary.py` fails when an
  `autoform_cli` module outside `ui/` imports `autoform_cli.ui`, except the
  render, site check and dashboard handlers in `__main__.py` and check's call of
  `publication_issues` (#704).
- **Docs.** The CLI reference's contracts moved to pages of their own under
  `autoform_cli/reference/`: blueprint format, validation and audit, search,
  skeletons and statement review, open statements, claims and revisions, work,
  debriefs, project setup, the published site, and runtime and publication. The
  reference, skills and contributor docs keep only the rules users and agents
  act on; internals moved to module docstrings, and upgrade notes to this file.
  `tests/test_doc_budgets.py` fails when one of these docs grows past its line
  budget (#458, #600, #608, #610, #628, #643, #649).
- **Lean names.** Search, audit, elaboration limits, impact and the library
  modules read Lean names by the one grammar `lean.py` writes down, Lean's own,
  in place of seven copies that disagreed at the edges. A quoted component may
  be empty or hold spaces and dots, as in Lean, but not `«`. Every name the Lean
  index reads in Mathlib reads as before; a spelling no Mathlib name has, such
  as `Nat..add`, `a«b»c` or `«a«b»`, now splits into components as that grammar
  splits it (#581).
- **Explorer builds.** The explorer ships as one prebuilt script,
  `autoform_cli/ui/assets/blueprint-explorer.min.js`, which
  `npm run build:explorer` compiles from its readable sources with esbuild and
  Terser at pinned versions. Render refuses a bundle that is missing, 145,000
  bytes or more, or stale against a fingerprint of the sources' UTF-8 text with
  LF line ends, so a CRLF checkout matches too; a project that renders a site
  needs no Node (#442).

### Deprecated

Each command or flag below prints one `warning[deprecated]:` line on standard
error once its arguments parse, with or without `--json`, and changes nothing
else.

- **`autoform search --state`.** Filter each `--json` hit's `state` instead, with
  `--limit` at least `total_matches`, since the flag filters before the limit.
  Removed in 0.8.0 (#647).
- **`autoform search --declaration`.** Filter each `--json` hit's `declaration`
  the same way, comparing case-insensitively and allowing leading modifiers
  (`def` keeps `noncomputable def` and `public def`); a misspelled kind then
  matches nothing instead of exiting 2. Removed in 0.8.0 (#647).
- **`autoform work list --lean-root` and `work context --lean-root`.** From
  0.8.0, `lean_targets[].source_file` stays in `autoform-work/v2`, always null;
  `search --lean-root` prints a Lean name's file and line. Removed in 0.8.0
  (#647).
- **The dashboard's live claim overlay.** `autoform dashboard` keeps serving a
  built site on loopback, with its Host and Origin checks; its read-only overlay
  of author claims, the `autoform-live/v1` payload and the `--repo` and
  `--scratch` flags that feed it go. Nothing in the site replaces the overlay;
  `autoform claim list` prints the claims. Removed in 0.8.0 (#669).
- **`autoform debrief subagent render`.** Nothing replaces it; the records stay
  JSON under `records/` in the debrief store. Removed in 0.8.0 (#669).
- **`autoform claim renew`, `--ttl` and `claim cleanup`.** Re-run `claim
  acquire` on the same set, with the same `--note`, to confirm it, and free a
  stopped owner's claim with `claim release --worker-id OWNER TARGET`. Removed
  in 0.8.0, when a new claim stays held until it is released (#714).
- **`autoform migrate article-ids`.** `autoform links set BLUEPRINT ARTICLE`
  gives an article its `article_id`; nothing replaces its `--check` gate or
  `--json` plan. Removed in 0.8.0 (#601).
- **Articles' facts in frontmatter.** In a project without
  `blueprint/links/roadmap/_defaults.json`, a `check` that passes prints, after
  its results, a `warning[deprecated]:` line counting the articles whose facts
  are in frontmatter. Once every agent working on the project runs 0.7.0 or
  later and CI's `AUTOFORM_REF`, in `autoform-verify.yml` and
  `blueprint-pages.yml`, is 0.7.0 or later, move them into `blueprint/links/`:
  with `autoform migrate links blueprint --write` in a hand-authored vault, by
  its generator in a generated one. `project new` writes that file, so a project
  it creates keeps its facts in `blueprint/links/` from the start; `init` never
  writes it, so an existing vault's facts stay where they are. Reading them goes
  in 0.8.0 (#601). Given `--autoform-ref`, `project new` warns
  `project-autoform-ref-unchecked`, since CI reads `blueprint/links/` only at an
  Autoform commit from 3ea366ae on, and its hint for omitted workflows names the
  same floor (#771).
- **The `develop-plugin` skill**, which changes Autoform itself, leaves the
  consumer plugin in 0.8.0: Claude, Muse and Codex stop loading it, so
  formalization sessions no longer carry it. It stays in the repository for
  contributors (#701).

### Removed

None of these went through a deprecation release. The first two change the
generated site, not a CLI or JSON contract, and old explorer links still open.
Python 3.10 reached its end of life in October 2026 (PEP 619).

- **Previous/Next links.** Render no longer ends each book page, or the
  explorer's reader, with Previous and Next links. They followed the walk down
  the containment tree, so on a wiki such as ATLAS a folder's last page led into
  the next domain: Ultrafilter's Next was Theory of Computation. The sidebar and
  `SUMMARY.md` keep the book's order (#599).
- **Explorer canvas.** The explorer's own drawing of the graph is gone, now that
  its stage shows the whole-project map: its bounded maps, camera, Fit and zoom
  controls and Show unlinked filter. Old links' `direction=` and `isolates=`
  parameters are ignored. On a desktop, a page whose reader only lists its
  children folds the reader to its rail, and other pages open the reader beside
  the map; on a phone, landing on a chapter or Home, or picking one in search or
  on the map, opens the map, and statements open in the reader. Back to map
  focuses the selected result's card, or else the map. A status filter that
  leaves out the selected result's status no longer clears the selection, and
  "Show N filtered out" counts what it hides from the list. A map that cannot
  load, or stops before it is set up, leaves a notice on the stage, and the
  outline, search and lists still work (#574).
- **Python 3.10.** Autoform needs Python 3.11 to 3.14, and CI tests 3.11 and
  3.13. uv runs Autoform on a Python in that range, downloading one if none is
  installed; generated CI still runs `.github/autoform_audit.py` with the
  runner's `python3`, which can be 3.10. For the upper bound, see **MCP servers
  on Python 3.15**. The lock drops the packages only 3.10 needed and takes
  dnspython 2.9.1, which fixes GHSA-45c3-73f7-pv4m. Lakefiles are still read
  with `tomli` below 2.4 rather than the standard library's `tomllib`: Lake
  reads TOML 1.0, and from Python 3.15 `tomllib` parses TOML 1.1 (#607).
- **Agent Review's read-back rubric.** Agent Review no longer judges a read-back
  against its cited source passage, and the item and verdict JSON the rubric
  defined go with it; no Autoform command wrote or read them. Its faithfulness
  rubric still judges the Lean against the source, and in Human Review a person
  judges each read-back (#586).

### Fixed

- **Verify downloads try again.** The verify workflow `init` writes fetched elan
  once, so one stalled transfer or 504 failed a project's CI, and a failed
  download of the project's Lean toolchain in the cache step read as "no cache
  available": when Lake's next run got the toolchain, the build compiled Mathlib
  from source. The workflow now retries the elan download up to four times,
  giving up on a try that cannot connect in 20 s or moves less than 1 KB/s for
  30 s, and installs the toolchain in a step of its own that tries three times,
  30 s apart. A scaffolded project keeps its workflow: make the same edits by
  hand, or delete it and rerun `autoform init`, which pins the new copy to the
  Autoform that runs it, or to `--autoform-ref`, and writes none without a
  commit to pin (#742).
- **MathJax no longer holds the page up.** The `mkdocs.yml` that `autoform init`
  writes lists `javascripts/mathjax.js` but no longer the MathJax bundle, which
  ran as a blocking script before the explorer could start. The script loads
  MathJax itself once the page is parsed, as it already did when no bundle was
  listed (#636). A `mkdocs.yml` that still lists the bundle may drop that line
  once CI's `AUTOFORM_REF` is 0.6.0 or later; a render pinned before 0.6.0 needs
  it.
- **Audit refuses a placeholder behind a Mathlib article, as CI does.** With
  `--lean-root`, `audit` and `doctor` report `lean-target-wanted` for a `lean:`
  name that resolves to a wanted placeholder, directly or through an alias, on
  an article that records `mathlib: true`. The kernel-trust audit in the verify
  workflow `init` writes already refused one. Unlike other new checks, this one
  fails rather than warns: a project with verification turned off, or an older
  copy of that audit, can pass 0.6.0's `audit --lean-root` and fail this one's,
  and `doctor --lean-root` with it. To clear it, point `lean:` at a declaration
  that compiles, such as an alias of the `mathlib_declaration` name (#700).
- **MathJax's bundle and Mermaid run only their pinned bytes.** The scripts
  render writes load both from the CDN with their subresource-integrity
  hashes, so a CDN that serves other bytes for either leaves formulas as typed
  and diagrams as written, with a console error. `javascripts/mathjax.js`
  fetched the bundle unchecked before, even after a site's own pinned tag had
  failed its check. MathJax's own loader still fetches `ui/safe` and the TeX
  extensions boldsymbol, mathtools, braket, mhchem and amscd unchecked. A
  library a site still lists in `mkdocs.yml` loads from that tag, with
  whatever check the tag carries (#738).
- **A heading "Mermaid" no longer stops a page's diagrams.** The toc extension
  gives such a heading the id `mermaid`, which the browser also exposes as
  `window.mermaid`. The diagram script took that heading for the library, never
  loaded Mermaid, and drew none of the page's diagrams. It now takes only a
  global with Mermaid's API for the library (#749).
- **MCP servers on Python 3.15.** The LSP and REPL servers never started on
  CPython 3.15, which uv chooses when it is the newest Python installed, or,
  from uv 0.13, when none is: beartype 0.22.9, which fastmcp needs, imports a
  `typing` name 3.15 removed, and fastmcp reported that as "FastMCP server
  support is not installed". Autoform now requires Python 3.11 to 3.14, so uv
  runs it on 3.14 or older, downloading one if none is installed, and replaces
  an environment made with 3.15. Where uv may not download Python, a machine
  with only 3.15 needs 3.14 or older installed, for the CLI too (#721).
- **Library note from an enclosing project.** Search's note about libraries it
  did not read looks only at the searched project's own Lake root, so it no
  longer names the packages of a Lake project that merely contains it (#682).
- **Pages where render writes its own.** A blueprint page that MkDocs would
  publish at the URL of one of render's own pages, such as `SUMMARY.md`,
  `dependencies/full.markdown`, `dependencies/map/README.md`, or
  `roadmap/README.markdown` when render writes the roadmap's page, is left out
  of the site with a warning, which `check` prints too. So is one whose URL
  differs from one of render's only in letter case, such as
  `dependencies/Full.md`, which a disk that ignores case writes over. Capture
  already skipped the root `dependencies.md` and `structure.md`, and still does,
  without a warning. Render read its own page back as the author's: it stopped
  with a `JSONDecodeError` on a `dependencies/` page, and with a `SUMMARY.md`
  the site silently lost its navigation; two pages at one URL failed the strict
  MkDocs build (#671).
- **Pages as MkDocs names them.** A page whose name is a README or index in
  another spelling, such as `notes/readme.md`, `guides/Index.md` or a roadmap
  `Index.md`, was linked as its folder's `index.html`, from raw HTML, its
  explorer and `publication.json`. MkDocs serves a folder's index only from a
  page named exactly `README` or `index`, so each is now linked where MkDocs
  writes it. A top-level `templates/` folder, which MkDocs leaves out, is left
  out of the site too, as one inventory entry, and a link into it keeps only its
  text, with a warning (#670). `render --review` refuses a passage cut from a
  file there, as from any file the site leaves out.
- **Escapes in titles.** A title's Markdown escapes, such as the backslash in
  `foo\_bar`, showed as typed in the explorer's Contents, search, breadcrumbs
  and tab title, on the whole-project map and its tab, and in statement
  headings, dependency references, the coverage page's lists and the landing
  page's Next up. They now read as the page's heading reads them, `foo_bar`
  (#727).
- **Library index guarantees.** `library index` and `--check` refuse a toolchain
  the probe is not verified on before `lake translate-config` reads a
  `lakefile.lean`, and refuse a source file with a dot in its path, or two files
  that map to one module, which failed at the probe or dropped a file. Search
  leaves out, with a warning, a library whose index names a module other than
  the one its `source_file` holds, or a declaration whose name is not a Lean
  name, such as one holding a newline. `--check` reads the index as search does,
  so on POSIX a symbolic link, named pipe or directory there is stale rather
  than followed or waited on, and a root under an unknown `~user` exits 2 rather
  than 1 with a traceback. A refusal names a stale index before a merged or
  edited one, the missing-manifest refusal no longer suggests `lake update`, and
  the note naming an unsearched library quotes the flags it suggests (#664).
- **A later H1.** An article with more than one H1 loads: the first H1 is the
  title, a later one stays in its section, as on the site, and `check` warns,
  naming its line. Since #171 read a fence that never closes as text, a shell
  comment in one, such as `# install the toolchain`, made `check`, `audit` and
  `search` refuse the whole blueprint. The reader's See also and Linked from
  lists read a later H1 the same way, though one named as a provenance or
  dependency section, such as `# References`, keeps the links below it out of
  them. Unless the site shows the article as typed, `check` and `render` also
  warn about a heading with no space after its `#` marks, `##Proof`, which only
  the site reads as one, and, on an article published as its own page, about one
  indented up to three spaces, which only Autoform reads as one. Both come with
  the site's page warnings, so `check --no-site-warnings` leaves them to
  `render`. A missing title's error names a `#Title` line the site reads as the
  title.
- **TeX in page titles.** A formula in a page's title showed as TeX source,
  such as `\(L^p\) spaces`, in the browser tab, the MkDocs header and the
  sidebar, since MathJax typesets none of them. Render now writes a formula in
  the title of the home page, and of each page an author wrote, as the explorer
  reads it: `Lᵖ spaces`. The name a screen reader gives a statement's "#" link
  showed the title as typed, such as `Top on $\mathbb{R}^n$`, and now reads
  `Top on ℝⁿ` (#697).
- **Declarations split across lines.** A `class inductive` or `class abbrev`
  whose name, or whose `inductive` or `abbrev` with its name, opens the next
  line is indexed at the `class` line with its modifiers; it was missed, or
  indexed without its `private`. Its kind is now `class`, as on one line, so
  `audit` reports `lean-target-kind-mismatch` for an article that declares it
  `inductive` or `abbrev`. A split name followed by `deriving`, and a split
  declaration named `module`, are indexed as on one line. The site's Lean block
  and hover card for a declaration whose name opens the line after its keyword,
  at the keyword's column, now show that line (#675).
- **Library index coverage.** `library index` reads the directories of a
  library's globs outside its roots too, so their modules are no longer left out
  of the index and its digests; a Lean file there that no indexed library builds
  is refused, as under a root. A library's modules are those Lake can build for
  it, so one whose glob matches its root, such as `globs = ["Lib"]`, indexes
  every module under the root; before, all but the root were refused. A stale
  build's error gives the `lake build` command that builds every indexed module,
  naming as targets the modules `lake build` of the libraries leaves out (#673).
- **Unlistable project root.** `project inspect` no longer reports
  `project-changed-during-inspection` on every run for a project root that
  cannot be listed. A root that can still be searched (mode 0311) gets its
  report, and the library commands read it; one that cannot be searched
  reports `unreadable-file` (#619).
- **Pin files read mid-write.** `library index`, and every command that checks
  a library's index against its checkout, read the library's pin files until
  two reads agree, so a write that lands during a read is read again. Pins that
  keep changing are refused as changing, where they were reported as over 1 MiB
  or unreadable (#620).
- **Decision files on Windows.** `project inspect` and the library commands
  read `lean-toolchain`, the lakefiles and the manifests in text mode on
  Windows, which turned CRLF into LF and stopped at a 0x1A byte. They now read
  the bytes as they are, as elan and Lake do. Ordinary CRLF files read and
  digest as before. A decision file with a 0x1A byte or a CR CR LF line ending
  now parses and digests whole, so a 0x1A on `lean-toolchain`'s first line
  gets `invalid-lean-toolchain` instead of passing (#616).
- **Links whose text wraps.** A link whose text runs onto the next line of its
  paragraph or list item is read as the site reads it. Loading dropped one in a
  dependency section without a word, so Hartshorne's relative degree-zero
  Picard functor gains the proof dependency it was written with. Render
  published one as written, so a link to an article its chapter absorbs was
  dead on the site, and a chapter linked that way had no place in the book's
  order. Code spans are read as the site reads them too, so a link that an
  escaped backtick or a code span from the line above hid is read. Since these
  links went unread, a problem with one warns rather than fails: a dependency
  that does not resolve, names its own article, closes a cycle or leads into
  an unpublished folder is not counted, any link into an unpublished folder
  warns, and check and audit warn about a broken one under `## Sources`, audit
  about one in a coverage file. Every link read before still counts, even one
  the site shows as code, and only those give an article its cited passage
  (#635).
- **Escaped link destinations.** Render reads an inline link's or image's
  destination as the site does: the backslash before an escaped character goes,
  except in code or math, so `[Base](base\_lemma.md)` names the article
  `base_lemma.md` instead of pointing at a file its chapter absorbs, and a
  reference definition keeps its backslashes, as on the site. A valid link such
  as `[t](other\_ch.md)` in a statement or notes holding a `<` no longer fails
  the strict MkDocs build (#656).
- **Claims inside a Git hook.** The claim commands' Git ignores `GIT_DIR`,
  `GIT_WORK_TREE`, `GIT_INDEX_FILE` and the other variables that point Git at
  another repository, as a hook sets them. With only `GIT_DIR` set, the board's
  Git wrote objects, and on a refused acquire a claim ref, into the hook's
  repository; with `GIT_WORK_TREE` too, every claim command failed. `git -c`
  settings still reach the board (#573).
- **Other commands inside a Git hook.** In a hook, `init` and `project new`
  pin CI to the Autoform checkout they run from, and `claim` and `dashboard`
  without `--repo` use the origin of the checkout they run in. Before, the
  hook's `GIT_DIR` and `GIT_INDEX_FILE` made Git take any directory for the
  hook's repository, so CI was written without a pin, and the default board was
  the hook repository's origin. Autoform's local Git commands now share one
  runner that drops the caller's `GIT_*` variables, except
  `GIT_CEILING_DIRECTORIES`, which only stops Git's search upwards (#590).
- **`\pmod`, `\bmod` and `\mod` on the site.** Site formulas typeset `\pmod`,
  `\bmod`, `\mod` and amsmath's `\varliminf`, `\varlimsup`, `\varinjlim` and
  `\varprojlim`. MathJax builds them from `\mmlToken`, which the page's TeX input
  turns off, so each showed a red `\notallowed`; they are now written without
  it, and a project macro of the same name still replaces one (#544).
- **Summaries with formulas.** An article's summary, the line the explorer's
  lists show under its title, no longer ends inside a formula or code span,
  which showed it as raw TeX: a clip that would cut one stops before it. A
  display formula inside the summary's paragraph, `$$...$$`, shows inline
  there (#637).
- **Commented-out links in the book order.** A link inside an HTML comment no
  longer places its page in the book's order: a commented-out chapter link moved
  that chapter forward in `SUMMARY.md`, though the page showed it later (#535).
- **Fences in comments.** Render no longer reads a fence line inside an HTML
  comment as opening a code block. It had left every later line of the page
  unrewritten, so a link to a source note after it was published as written,
  to a page the site leaves out, and `check` said nothing (#527). `check`,
  `audit` and the explorer now read comments as the site does too, so a `<!--`
  in a code span no longer hides a fence that never closes from them, nor makes
  a closed code block after it read as unclosed.
- **Comments in code spans.** The graph, `audit` and the split of an article
  into its statement and sections read a `<!--` in a code span that opens and
  closes on its line as text, as the site does. It hid every heading and link
  after it up to a `-->`, so dependencies written after one were dropped
  without a word, and the sections after it read as part of the statement. On
  the site, a comment in an indented code block after such a line shows as
  typed, as other code does, where it was removed. A `<!--` still opens a
  comment, as before, in a code span that runs over a line break, or later in
  a paragraph after a backtick that pairs with nothing on its line. The site
  now leaves out a comment after a code span that an escaped backslash or part
  of a longer backtick run opens, or that crosses a fenced block or a comment
  taken out, where it showed the comment's text or the whole page as typed,
  and the graph reads such backslashes and runs as the site does. A span on
  the site ends where a new block starts, at a heading, a quote, a list item,
  a table row or a comment that opens a line, and the page now pairs spans so
  too, where it kept a comment after such a line and showed the page as typed.
  The graph and `audit` read no link the site shows in a code span on its line,
  as after two backticks with nothing between them or after an escaped backtick,
  where they read it as an edge or a link; one in a span the site pairs across
  lines still counts, as before (#657).
- **Output checks.** `skeleton`, `review prepare` and `library index` refuse
  to replace an output folder that holds a subfolder they cannot list, where
  they skipped it, so an edit inside it could be published over (#521).
- **No size caps on published data.** Render reads an `atlas.json` of any size,
  where 0.6.0 ignored one over 1,000,000 bytes, and writes and replaces a site
  whose `publication.json`, which lists every site file, passes 8 MiB;
  `autoform dashboard`'s claim overlay reads such a manifest too, where 0.6.0
  refused it as too large (#512).
- **Structure fields.** The Lean source index records the fields a
  `structure` or `class` declares and the parents it names, as
  `Structure.field`, so `check`, `render`, `audit` and `skeleton` resolve a
  `lean:` name for one; `audit` takes it as a `def` or theorem under its
  structure's limits. For a constructor or other name the index does not read
  directly under a project type, `check` names the type's `path:line` in a
  declaration article instead of pointing to `.lake` (#509).
- **`class abbrev` and `class inductive`.** The Lean source index reads
  `class abbrev C` and `class inductive D` as declarations of `C` and `D`, as
  Lean does, not as classes named `abbrev` and `inductive`. In Mathlib this
  indexes 10 classes it missed, such as `Finite`, `ExpChar` and `IsGCDMonoid`
  (#624).
- **Dotted namespaces.** The Lean source index and `audit`'s limit scan open
  a scope for each component of `namespace A.B` or `section A.B`, and `end`
  closes one per component of its name, as Lean does: `end B` after
  `namespace A.B` no longer drops the `A.` prefix from later names, and
  `end C.D` closes both scopes (#524).
- **Empty quoted name components.** `skeleton`, `review prepare`,
  `render --review` and the generated `.github/autoform_audit.py` read a Lean
  name with an empty quoted component, such as `Foo.«».bar`, as Lean does,
  instead of refusing it as an invalid declaration name (#622).
- **Lean commands.** A Lake or Lean command Autoform runs that fails just as
  Ctrl+C or a termination signal arrives is stopped along with Autoform
  instead of being left running (#510).
- **Lake configuration.** A `lakefile.toml` whose `lean_lib` is not an array
  of named tables, such as `[lean_lib]` written for `[[lean_lib]]`, is
  refused as Lake refuses it, instead of crashing, reading the package's
  default library, or skipping the entries that are not named tables (#505).
- **Import impact.** In `work import-impact`, a library whose `globs` name
  other modules but not its own root no longer claims the files under that
  root that no glob matches, which Lake does not build for it. A library
  nested there no longer makes the command refuse a project Lake builds with
  "maps to more than one module". A loaded local module that no library
  builds is now refused with exit 2, and under
  `--module-scope import-closure` an article naming a declaration in such a
  file is listed in `unresolved_articles` (#500).
- **Empty `globs`.** A `lean_lib` with `globs = []` builds no module, as in
  Lake, where commands that read the library targets took it to build every
  module under its roots. When `work import-impact` refuses a loaded module
  that no library builds, it names the module's source file instead of
  saying the module has none (#519).
- **Book navigation.** The generated `SUMMARY.md` nests each book page under
  its chapter's README, written in book order. A landing page that linked a
  deep page before its chapters, or a folder with no README in the book, put a
  page a level too deep, and MkDocs refused the nav with a
  `LiterateNavParseError`. Where MkDocs accepted the nav, a page could sit
  under the wrong chapter. A page beside its chapter's README now sits inside
  that chapter in the sidebar rather than next to it (#481).
- **Claim board location.** When `git remote get-url origin` cannot run, runs
  out of time or is killed, claim commands and `autoform dashboard` say they
  cannot read the origin remote (`error: transport:` in `claim list`), where
  they asked for `--repo` as outside a checkout, after which Formalize went on
  without claims. A board path naming a user who does not exist, such as
  `~nosuchuser/board.git`, is a one-line error in claim commands, the dashboard
  and `project worktrees`, not a traceback, and an origin URL holding a byte
  that is not UTF-8 is read as its path (#681).
- **Claim listings.** On macOS and Windows, a listing shows both of two claims
  whose keys, or folders in them, differ only in case, including with Apple's
  Git, which stored one key's name holding the other's lease (#452). A claim ref
  whose name is not UTF-8 no longer fails `claim list`, `claim cleanup`, the
  dashboard and `project worktrees`, and one whose name holds U+2028 no longer
  gives its lease to another claim in a listing, `claim acquire`, `renew` or
  `release`: a full listing leaves such names out, and `claim list` warns how
  many claim refs it left out. A board hook that prints a byte that is not UTF-8
  no longer fails a claim command whose push went through, and error messages
  show such a byte as `\xff`. A branch whose name ends in a claim's path, such
  as `refs/heads/refs/autoform-claims/x`, is no longer read as a claim; on macOS
  and Windows it could fail every full listing as `contention:`. On those
  systems, a claim key beside a folder that differs from it only in case, such
  as `lean/Foo` and `lean/foo/bar`, no longer fails every full listing, and with
  Apple's Git a key beside a name that folds into it, such as U+212A KELVIN SIGN
  beside `k`, keeps its own lease. Except on Windows, a listing deletes the
  private refs that a listing of this version on the same host left when a
  signal stopped it, unless a lock file that a killed Git command left in the
  scratch blocks that. On Linux, the same host also means the same boot and PID
  namespace, and a host whose `/proc` cannot give them deletes none; elsewhere
  it means the same host name, so two machines with one name must not share a
  scratch. Older versions' refs stay, as do those of a listing `/proc` could not
  name.
- **Claim keys ending in `.LOCK`.** `ClaimBoard` refuses a key ending in
  `.lock` in any case, such as `lease.LOCK`, as it refused `lease.lock`. Git
  reads such a ref on a case-insensitive file system as its sibling's lock
  file, so each full listing on such a scratch leaves private refs behind, and
  `renew` of the sibling on a macOS board fails. Claim commands never make such
  a key. A board that already holds one keeps both effects until the ref is
  deleted by hand, as with
  `git push <board> :refs/autoform-claims/<key>`; listings leave it out, with
  the left-out warning (#667).
- **Site search.** Each statement card is a search result of its own, titled
  by its statement and opening at its anchor, and its exact `lean:` names find
  it even when they show only as code icons. Notes headings such as Sources
  are no longer results of their own; their text stays in the card's (#390).
- **Links on chapter pages.** A definition-like line with more words after
  its first, such as `[^1]: Grounded in [Paper](../sources/paper.md)`, is
  prose, as Python-Markdown reads it. Render read it as a definition and left
  the links after its first word pointing at unpublished pages (#484).
- **Roadmap folder spelling.** A roadmap folder spelled with other capitals,
  such as `Roadmap/`, passed `check` on a case-insensitive filesystem, as on
  macOS, and then ended `render` in a `KeyError`. Loading the graph now refuses
  it on every filesystem as Linux did, "roadmap directory does not exist", and
  names the rename (#595).
- **Freshness check on large projects.** The commands that check a build's
  freshness (`skeleton`, `review prepare`, `review check`, `render --review`,
  `work impact`, `work import-impact` and `library index`) gave no verdict when
  the modules did not fit in one command line: the check named every module in
  one Lake command, which macOS refuses past about 21,000 modules and Windows
  past about 800. Lake now checks them in as many runs as the limit needs,
  within the same time limit. In `skeleton`, `review prepare`, `review check`
  and `render --review`, a stale build's advice names the modules of the run
  that found it, so its `lake build` fits too (#678).
- **Import closure and a path package inside the Lean root.**
  `work import-impact --module-scope import-closure` never read the files of a
  `path` dependency kept inside the Lean root, since the captured inventory
  holds them under their paths rather than the names Lake gives their modules. A
  root module that reached `MODULE` only through such a dependency was left out
  without a word. A file there that may import the closure now refuses the
  narrowed scope, as one outside the Lean root did (#678).
- **Import closure and import spellings.** A volume that ignores case or Unicode
  normalization, as macOS's default one does, builds `import Demo.basic` from
  `Demo/Basic.lean`. The default scope of `work import-impact` refuses such an
  import in a module it loads; `--module-scope import-closure` missed the
  importer, since its test compares bytes. It now refuses too, naming the import
  and the module, when a header import in a module the default scope loads, or
  in a file outside the captured sources that holds a closure module's first
  component, names a module in the closure only as such a volume compares names
  (#678).
- **`Ctrl+\` during a Lean probe.** SIGQUIT now stops the Lean and Lake
  commands a probe or a freshness check started, as SIGINT, SIGTERM and
  SIGHUP do. Those commands run in sessions of their own, so the terminal's
  SIGQUIT never reached them, and up to eight Lean processes kept running
  after Autoform quit (#711).
- **Import closure beside a private namesake.** Under
  `work import-impact --module-scope import-closure`, a private constant in a
  loaded module stood in for an article's `lean:` name when the name's public
  declaration sat in a root or glob module the narrowed probe did not load. The
  helpers the default scope reports there lost their claim keys, and two such
  private constants made the command exit 2. When the source index places the
  name by that declaration, the name now counts as it, which the edit cannot
  affect. A name declared more than once is placed by the declaration the index
  selects, so one it places outside the affected modules is no longer listed as
  unresolved (#678).

## 0.6.0 (2026-10-22)

What has landed since 0.5.0, across 236 merged pull requests, grouped by
theme. Each line names the pull requests to read for detail.

### Upgrading from 0.5.0

- **Pin.** CI at an older `AUTOFORM_REF` rejects `area:`, `navigation:`,
  `catalog:`, `open_statements:`, `published:` and `statement: retracted` in
  `check` and `INVENTORIED` in `render`, so move the pin before using them;
  until then, retract by removing `statement` and `proof` and keeping `lean:`.
  It also rejects a dependency cycle that only appears once chapters roll up
  (#358): leave out the edge that closes it until the pin moves. Older refs
  assign no areas from `blueprint/atlas.json` but publish it with every id it
  names, so a project with unpublished folders moves the pin before adding it
  (#90).
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
