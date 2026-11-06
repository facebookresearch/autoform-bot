# Changelog

Notable changes to Autoform, newest first. A release's version is the one in
`pyproject.toml`; `uv.lock`, the Claude, Muse and Codex manifests, and this
file's newest release heading carry the same one (`tests/test_release.py`
checks), and `autoform --version` prints it. Add a line under **Unreleased**
with each change worth knowing about, and give the section its version and
date when the release is tagged.

## Unreleased

### Added

- **Publication inventory.** `publication.json` says where each blueprint file
  went (`inventory`): for every file the verified commit holds below the
  blueprint, or else every file render read and each entry it skipped, its URL
  on the site, the repository URL the site links instead, or why the site leaves
  it out (`hidden`, `unpublished`, `generated`, `mathjax-config`,
  `review-testimony`, `graph-input`, `not-utf8`, `canvas`, `unsupported-type` or
  `not-captured`). A hidden or `published: false` folder is one entry, so
  nothing in it is named. A file's role follows from its path: its first folder
  (`roadmap`, `sources`, `coverage`), else `hidden` for a dot name, else
  `other`. It also names the Autoform that rendered it, best effort (`autoform`:
  its version, its commit when an install from Git or an unmodified checkout
  records one, and its repository only on github.com). The schema stays
  `autoform-publication/v3` (#658).
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
- **Lean under each statement.** A statement whose `lean:` names resolve
  shows the first three declarations under it, as written from the attribute
  lines above the keyword, without the section's variables: a theorem or lemma
  without its proof, anything else whole, a definition's body included. A body
  written at the keyword's own indent is cut short. A declaration over 14
  lines folds behind a line that names it. Keywords, comments and strings take
  the page's code colors, in the map's reading pane too; site search skips the
  code. A catalog shows none, and a `--review` render leaves the Lean to each
  card's review, after its read-back. Render cuts the text from the Lean
  sources it captured, so a render without them shows none.
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
  the project does not declare, such as Mathlib's, stay text.
- **Check without site warnings.** `check --no-site-warnings` skips the
  warnings about the pages the site would publish, which `render` prints too,
  and still reads the notes under `blueprint/sources/`, which a render that
  links them in the repository never reads. The generated
  `blueprint-pages.yml` passes it, since render runs two steps later, when its
  `AUTOFORM_REF`'s `check --help` lists it, so an older pin runs the full check.
  On one CI runner ATLAS's check took 60 s with the flag instead of 255 s, and
  render printed every warning it skipped. A scaffolded project keeps its
  workflow: add the flag by hand once its `AUTOFORM_REF` includes it.
- **Panes you can resize and hide.** In the book's workspace, Topics and the
  reading pane resize from splitters on their inner edges, by pointer or
  keys, and the map always keeps at least 160px. A pane dragged nearly
  shut, or whose splitter takes Enter, hides, and a tab at the map's edge
  brings it back; "Map only" in the trail row hides both. The choice holds
  on every page of the site, as the theme's own choices do; a double-click,
  Delete or a Mac's delete key on a splitter restores the usual width.
  While a pane is dragged the map holds its camera, and it settles once the
  drag ends. Where touch is the main pointer, as on a tablet, the splitters
  are 24px wide, and Windows' forced colours keep the splitter's line and
  Map only's pressed look. A phone keeps its sheet.
- **Whole-project map.** Every rendered site gets `dependencies/map/`, one
  zoomable page for the whole project. Regions (authored areas, or the top-level
  chapters) open into their parts and parts into their results as the camera
  closes in, and a part opens once its parts can be named. Search reaches any
  result by title or Lean name; the reading pane names the result's kind, such
  as Theorem or Definition, and shows its statement and proof from its page, and
  what it uses and what uses it. One click picks a part and shows the parts it
  builds on and the parts that use it, with links flowing between them; a second
  click goes inside, and Up or Escape goes back a level. On a narrow screen the
  map takes the width, with the reading pane under it. The data loads one level
  at a time from `dependencies/map/data/` (#517, #545). The explorer's stage
  shows the same map beside the book: the page being read sets where it opens,
  a result picked on it opens in the reader and a part opens its chapter, the
  statement at the top of the reader is outlined on it, and the status filter
  dims the results it leaves out (#552). There it reads its data through the
  book's router, under the book's timeout and same-origin rules.
- **Informal dependencies.** Under `## Informally uses`, an article can list
  what its source's proof cites but its Lean proof need not use. The links
  resolve like dependencies and appear in the runtime projection, now
  `autoform-runtime/v5`, as `informal_dependencies`, but derived status,
  readiness and `work assumptions` never count them, and the article's summary
  skips the section. `check` warns about a link that does not resolve, one whose
  target depends on the article (it stays listed), and the heading written below
  a dependency heading, where its links count (#541). The Roadmap skill puts
  under it what a source's proof cites but the planned Lean proof will not use,
  as when a library proof is reused (#556).
- **Projects that track no Lean.** `tracks_lean: false` in
  `roadmap/README.md` takes Lean progress out of the book's pages: the landing
  page's figures and Next up, the statement status marks, the status dots on
  dependency references and the chapter progress lines. The explorer drops its
  status filter and status words, and the whole-project map its tints, progress
  bars and status words; its cards name their kind instead. Neither search box
  mentions Lean names, though both still match them. In place of the figures,
  the landing hero shows the README's opening paragraphs, up to its first
  heading or list. Check warns about the setting anywhere else under
  `roadmap/`, or with a value other than true or false, and ignores it.

### Changed

- **Mathlib results in review.** Agent Review's roadmap rubric says when cited
  declarations state an article's result, and has the reviewer quote their
  hypotheses and conclusions before judging. A `mathlib: true` that fails this
  caps the rubric's usability score at 2, and Roadmap sets `mathlib: true` only
  when verified Mathlib declarations state the result (#668). Formalize's library
  search rules, which Roadmap follows, use the same definition: a library hit
  that needs only hypotheses the result gives or implies, or several that
  together state it, is reused like an exact one (#679).
- **The landing's coverage line** leads with the source areas split into
  statements: "5 source areas, all split into statements", or "3 of 5 source
  areas split into statements" before the other dispositions.
- **Search warns about a library it cannot verify.** `search --library` leaves
  out a library it cannot verify, with a `warning: library NAME:` line on
  standard error and, in `--json`, an entry under the new `refused` key, and
  still prints the blueprint's hits and the other libraries'. It exited 2 and
  printed nothing (#325).
- **Library index check.** `library index --check` counts an index as current
  when only the Autoform version that wrote it differs, so a new release no
  longer turns a publishing library's check red. An index another probe
  revision made is stale, said without running Lean, and a stale index names
  the records generation gives otherwise.
- **Vault pages on Windows.** `autoform-visualize` writes the dependency
  summary and structure pages with LF line endings on Windows too, as on every
  other system, so a vault regenerated on both no longer flips them.
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
  the declarations its links reach, in book order and with their status,
  behind a line that counts them, with their states when the project tracks
  Lean; a list of one is shown open. The list replaces evidence that is only
  links, each straight to one of those declarations; beside evidence that
  stays, it shows the first 50, or as many as `coverage_list_limit` in
  `roadmap/README.md` says, and counts the rest. A page whose published
  table is not the contract's gets no view, and render warns. The rest of the
  page is unchanged. In the sidebar the page is the book's last entry, named by
  its title, and Mathematics no longer opens it (#631, #645).
- **One way to read a tree.** Lean sources, the blueprint and an output site
  are read by path in one walk that never follows a symbolic link, and a second
  walk must find the same folders, names and `stat` signatures; any difference
  is a change, which the Lean commands retry as before. This replaces the
  directory-descriptor capture and its Windows fallback (about 1,250 fewer
  lines). On native Windows, `check`, `render` and the other Lean commands now
  read Lean sources, so sites get code links there too, and `render
  --require-declarations` no longer fails for declarations it could not look
  up. A change that appears and is undone between the two walks is not seen,
  as with the old engine between its checks.
- **Titles typeset their math.** Statement headings, Next up and the
  Previous/Next links typeset a title's formulas, as the reader's dependency
  references already did, instead of showing its TeX as typed.
- **Topics groups chapters by area.** When a project's chapters name two areas
  or more, Topics heads each area's chapters with its name, as the map groups
  them into regions. An area with a single chapter, and a chapter without an
  area, keep their place unheaded.
- **Import closure.** `work import-impact --module-scope import-closure` tests
  each module only against the files that hold its last component, which one
  pass over each file finds, and reads a file that libraries sharing a `srcDir`
  all map once. It tested every file, once per such library, for each module it
  added: on a generated root whose libraries all share one `srcDir`, a hub
  module took 69 s at 1,491 files and is now 0.15 s, and 0.9 s at 7,000 files,
  where one test of each file per module took 105 s. The modules it finds are
  unchanged (#429).
- **Lean root reads.** With `--lean-root`, `search`, `check`, `work list` and
  `work context` scan only the Lean files that spell the last component of a
  name they look up, and `search` looks up only the hits it prints. Every file
  is still read, so the source revision, the errors and the output are
  unchanged; on a 13,698-file Lean root, indexing a 36-name blueprint's
  declarations took 4.7 s of CPU instead of 13.6 s. `check` reads the Lean
  root once where a shared library's citations made it read it twice, and
  again only when a name is declared nowhere, for the error about its parent.
  `audit` strips comments only from files that hold a resolved target (#429).
- **Lean scan.** Reading a Lean file's declarations tries its six command
  patterns only on a line that starts the way one of them can: with `#exit`,
  `namespace`, `section`, `mutual`, `end`, an attribute list, a modifier or a
  declaration keyword. It tried all six on every line, though almost every
  line is a proof. Each line's indent is measured only in a file with a
  structure or class, whose fields need it. On one CI runner, indexing
  ATLAS's 13,698 Lean files took 9.2 s instead of 13.0 s; the declarations
  found are the same.
- **Lean names.** Search, audit, elaboration limits, impact and the library
  modules read Lean names by the one grammar `lean.py` writes down, Lean's own,
  in place of seven copies that disagreed at the edges. A quoted component may
  be empty or hold spaces and dots, as in Lean, but not `«`. Every name the Lean
  index reads in Mathlib reads as before; a spelling no Mathlib name has, such
  as `Nat..add`, `a«b»c` or `«a«b»`, now splits into components as that grammar
  splits it.
- **Lean index names.** The Lean index leaves out names Lean cannot declare,
  such as `$typeName.toBitVec_nsmul`, which it read from commands inside
  syntax quotations (14 in Mathlib). An `alias` name takes Lean's identifier
  characters: `alias ℘ := t` declares `℘`, and `alias Жук := t`, which Lean
  refuses, declares nothing.
- **Search index size.** `dependencies/index.json` no longer repeats links the
  explorer doesn't read. A row whose route the explorer opens drops its context
  `url`, and only container rows keep `data_url`, since every page names its
  own graph data. The file is written as ASCII, so one symbol above U+00FF no
  longer doubles the memory of the decoded text. ATLAS's index goes from
  42.1 MB to 32.2 MB, and on one CI runner search and the outline were ready
  2.1 s after Home started loading instead of 2.8 s in WebKit, and 1.6 s
  instead of 1.9 s in Chrome.
- **Explorer search.** A keystroke that extends the query ranks only the
  previous query's matches, not every row of the search index, and a one-word
  query no longer joins each row's fields. Results and their order are the
  same. On one CI runner, typing "zeckendorf" on ATLAS's Home took 110 ms of
  script in Chrome instead of 562 ms, and 220 ms instead of 1,272 ms in WebKit.
- **Explorer first view.** The explorer draws a page's first view once. It drew
  the Topics outline, breadcrumb and reader as it took in the search index, and
  then again for the page itself. The outline also reads the index's rows by
  ID from the map the explorer already keeps, instead of building another. The
  first view is the same. With the index's 25 ms slices, on one CI runner,
  ATLAS's Home showed it 426 ms after the index arrived instead of 805 ms in
  Chrome, and 986 ms instead of 1,327 ms in WebKit. Other pages gained about as
  much, and the longest frame stayed within one frame of its old length.
- **Wiki links.** Render reads the links between articles without rendering
  an article whose links all sit under its Sources, References, Bibliography
  or Depends on headings, which holds for about nine in ten ATLAS articles.
  The links it finds are the same. On one CI runner, ATLAS rendered in 397 s
  instead of 456 s.
- **Render reuse.** Render reuses one converter for statement and note bodies,
  where it built one for each (about 16,000 on ATLAS), walks the book once
  instead of twice, keeps every title's label from the search index for the
  map, and compares path parts where it called `relative_to`, which is slow
  from Python 3.12. On one CI runner ATLAS rendered in 312 s instead of 340 s,
  with the same output.
- **Render workers.** With 4,000 or more node pages, `render` and `check` read
  them in up to four worker processes: on POSIX, from Python 3.11.5, run as the
  `autoform` command or `python -m autoform_cli`. A page a worker cannot read,
  or a pool that breaks or will not start, is read as before, so the site and
  the warnings are the same. On a 4-processor CI runner, ATLAS rendered in
  225 s instead of 337 s, and checked in 145 s instead of 255 s.
- **Landing.** The ready figure links the list of what it counts, each count
  shows once with thousands separators, and the brand gradient is off the
  figures, bar and header hairline. Source coverage reads in plain words
  (#503).
- **Formula staging.** A page reads every formula's font and width before it
  stages any formula for typesetting. It used to read each formula after
  appending the previous one's staging copy, so the browser laid out the whole
  page again for every formula. On one CI runner, Chrome prepared the 805
  formulas of an ATLAS page in 130 ms instead of 964 ms; the typeset output is
  unchanged.
- **Typeset once.** The explorer moves the page it opens on into its reader,
  with the formulas MathJax typeset as the page loaded, instead of a copy that
  it typeset again. On one CI runner, Chrome spent 561 ms in MathJax on an
  805-formula ATLAS page instead of 918 ms, and the typeset formulas are the
  same. The reader also shows that page's Mermaid diagrams, where it showed
  their source.
- **Map layout.** Laying out a part on the whole-project map no longer routes
  the links implied by others, which the map draws only around a selected
  result: they are routed when a selection first needs them. On one CI runner,
  laying out ATLAS's 3,635 parts that hold results took 945 ms instead of
  1,639 ms, and the slowest part 11.1 ms instead of 74.2 ms; its first
  selection pays the rest. Until a selection reads them, the laid-out parts
  also hold a third less memory.
- **Declaration captions.** A `declaration` command with no caption, such as
  `isomorphism`, is captioned as written, first letter capitalized, in the book
  instead of "Node". Modifiers
  such as `noncomputable` and now `public` no longer hide a known command from
  the caption, from progress or from `audit`'s check of the Lean target's kind,
  which skipped `noncomputable def`; `public def` also counts as a definition,
  and `irreducible_def` and `opaque` read as definitions. `check` names each
  command with no caption once, with the articles that use it; `audit` does not
  check those articles' Lean target kind either (#485).
- **Graph loading.** Loading a blueprint answers from what discovery already
  read, instead of asking the disk, for a dependency spelled as `..` steps and
  plain names and for the README that contains an article. It reads no page's
  links for ones into an unpublished folder when there is none, and path
  containment compares path parts. On one CI runner ATLAS's graph loaded in
  25.8 s instead of 52.3 s, with an equal result, and `check`, `audit` and
  `render`, which each load it, took 29, 28 and 24 s less.
- **Explorer delivery.** The approved integrated redesign ships below 145 KB,
  with the same JSON and frame budgets and pinned safe compiler. Reading
  handles retain page-owned mathematical titles.
- **Integrated reading.** The book, theorem search and dependency map share
  one persistent workspace. The outline follows authored containment, includes
  root-level pages and labels collections as subheads; Home returns to the site.
  Empty source choices are hidden (#442).
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
- **Book order.** Previous/Next, the site nav, Next up and the explorer's
  outline and search follow one reading order, down the containment tree, so a
  landing-page shortcut no longer reorders the book. Pages their parent does not
  link now join the site nav and Previous/Next, after the pages it links (#495).
- **Book copy.** The landing lead keeps link text, code, formulas and whole
  sentences. Next up says which Lean step is next. A chapter's progress is one
  line of counts whose status meanings show on hover, gone when the chapter has
  fewer than two countable statements. Unplaced statements sit under "Other
  statements", and the map's fallback stays out of search results (#494).
- **Docs.** The CLI reference's contracts moved to `autoform_cli/reference/`
  (validation and audit, search, open statements, claims and revisions,
  runtime and publication). The reference, skills and contributor docs keep
  only the rules users and agents act on; internals moved to module
  docstrings, and upgrade notes to this file. `tests/test_doc_budgets.py`
  fails when one of these docs grows past its line budget (#458).
- **Listings.** The Codex and Muse listings name the plugin AutoformBot, and
  Codex shows the Roadmap, Setup and Develop Plugin skills under those short
  names (#463).
- **Messages.** The read and publish errors that `skeleton`, `library index`,
  `work impact`, `work import-impact`, `review prepare`, `review check` and
  `render --review` share no longer call the project files they read "skeleton
  input", or the files they write "skeleton output" or "packet output"; they
  name the path alone (#506).
- **Claims.** `claim list TARGET...` fetches and reads only those claims.
  `--timeout` (120 seconds by default) bounds a listing, a slow step is named on
  stderr, and a failure goes to stderr labelled `timeout:`, `contention:` or
  `transport:` with nothing on stdout. Git no longer waits for a terminal
  password prompt in claim commands, and a `claim list` or `claim cleanup`
  listing that runs out of time or is interrupted no longer leaves its
  transport running. Formalize treats a labelled `claim list` failure as an
  unreadable board, not as a board without claims (#432).
- **Work packages and sessions.** A Formalize subagent may keep its worktree
  for a package of related leaves that the lead confirms together, holding only
  the leaf it has taken and taking the next once `work context` shows it ready.
  With claims, each session leads from its own clone and checks `git worktree
  list` before dispatching, and workers name their branch with `claim acquire
  --note`, now documented. A handoff to another session, when the request covers
  pushing, goes to the `origin` branch `handoff/<article_id>`, which the next
  worker to take the leaf continues. With claims, no message grants
  ownership; none completes a leaf (#434, #532).
- **Formalize without claims.** Workers run `work list` and `work context`
  without `--lean-root`, and the lead dispatches each leaf's `work list` item.
  Within a run, whoever works a leaf first creates its branch
  `work/<article_id>`, which Git refuses twice in one clone, in place of
  packages of leaves the lead confirmed; the branch goes once the work lands or
  the leaf is abandoned. The skill suggests, without requiring, landing as a
  fast-forward that rebases and rechecks when the shared branch moved, then
  taking the next leaf, preferring one the landing unlocked. Claims are used
  only when the user asks for them or another session already does (#429).
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
- **Check and render.** One reused converter reads every page, and each linked
  path is resolved once per run, so `check` uses about a fifth less CPU on an
  1,806-article vault; neither command's output changes (#415).
- **Render paths.** A render resolves each page and node path once and
  remembers it until the render ends, instead of asking the disk again for
  every link to it; `check` does the same while it reads the site's pages. On
  one CI runner ATLAS rendered in 418 s instead of 441 s, with the same output.
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
  warnings.
- **Library listing.** Formalize tells the user about the packages
  `library list` leaves out in one line that counts them by reason, not one line
  each (nine or ten on a Mathlib project), and searches the blueprint alone when
  the listing fails. Agent Review lists a module's declarations from the
  `--json` hits whose `module` is exactly that module, with `--limit` at least
  `total_matches`; searching the module's name also returned other modules'
  declarations, and at most 20 (#204).

### Deprecated

Each prints one `warning[deprecated]:` line on standard error once its arguments
parse, with or without `--json`, and changes nothing else.

- **`autoform search --state`.** Filter each `--json` hit's `state` instead, with
  `--limit` at least `total_matches`, since the flag filters before the limit.
  Removed in 0.8.0 (#647).
- **`autoform search --declaration`.** Filter each `--json` hit's `declaration`
  the same way, comparing case-insensitively and allowing leading modifiers
  (`def` keeps `noncomputable def` and `public def`); a misspelled kind then
  matches nothing instead of exiting 2. Removed in 0.8.0 (#647).
- **`autoform work list --lean-root` and `work context --lean-root`.**
  `lean_targets[].source_file` stays in `autoform-work/v2`, always null;
  `search --lean-root` prints a Lean name's file and line. Removed in 0.8.0
  (#647).

### Removed

- **Previous/Next links.** Render no longer ends each book page, or the
  explorer's reader, with Previous and Next links. They followed the walk down
  the containment tree, so on a wiki such as ATLAS a folder's last page led into
  the next domain: Ultrafilter's Next was Theory of Computation. The sidebar and
  `SUMMARY.md` keep the book's order (#599).
- **Explorer canvas.** The explorer's own drawing of the graph is gone, now
  that its stage shows the whole-project map: its bounded maps, camera, Fit and
  zoom controls, Show unlinked filter, topic shelves, concept-link clusters and
  connection backbone. Old links' `direction=` and `isolates=` parameters are
  ignored. On a desktop, a page whose reader only lists its children folds the
  reader to its rail, and other pages open the reader beside the map; on a
  phone, landing on a chapter or Home, or picking one in search or on the map,
  opens the map, and statements open in the reader. Back to map focuses the
  selected result's card, or else the map. A status filter that leaves out the
  selected result's status no longer clears the selection, and "Show N
  filtered out" counts what it hides from the list. A map that cannot load, or
  stops before it is set up, leaves a notice on the stage, and the outline,
  search and lists still work. The explorer bundle is 68,637 bytes instead of
  144,973 (#574).
- **Python 3.10.** Autoform needs Python 3.11 or later, and CI tests 3.11 and
  3.13. The lock drops the packages only 3.10 needed and takes dnspython
  2.9.1, which fixes GHSA-45c3-73f7-pv4m. Lakefiles are still read with
  `tomli` below 2.4 rather than the standard library's `tomllib`: Lake reads
  TOML 1.0, and from Python 3.15 `tomllib` parses TOML 1.1.

### Fixed

- **Library index guarantees.** `library index` and `--check` refuse a
  toolchain the probe is not verified on before `lake translate-config` reads a
  `lakefile.lean`, and refuse a source file with a dot in its path, or two files
  that map to one module, which failed at the probe or dropped a file. Search
  leaves out, with a warning, a library whose index names a module other than
  the one its `source_file` holds, or a declaration whose name is not a Lean
  name, such as one holding a newline. `--check` reads the index as search does,
  so on POSIX a symbolic link, named pipe or directory there is stale rather
  than followed or waited on, and a root under an unknown `~user` exits 2 rather
  than 1 with a traceback. A refusal names a stale index before a merged or
  edited one, the missing-manifest refusal no longer suggests `lake update`, and
  the note naming an unsearched library quotes the flags it suggests.
- **Reference links on chapters with a `<` in the title.** A `<` in a
  chapter's title alone left reference-style links in its prose shown as typed
  (#631).
- **Unlistable project root.** `project inspect` no longer reports
  `project-changed-during-inspection` on every run for a project root that
  cannot be listed. A root that can still be searched (mode 0311) gets its
  report, and the library commands read it; one that cannot be searched
  reports `unreadable-file`.
- **Pin files read mid-write.** `library index`, and every command that checks
  a library's index against its checkout, read the library's pin files until
  two reads agree, so a write that lands during a read is read again. Pins that
  keep changing are refused as changing, where they were reported as over 1 MiB
  or unreadable.
- **Decision files on Windows.** `project inspect` and the library commands
  read `lean-toolchain`, the lakefiles and the manifests in text mode on
  Windows, which turned CRLF into LF and stopped at a 0x1A byte. They now read
  the bytes as they are, as elan and Lake do. Ordinary CRLF files read and
  digest as before. A decision file with a 0x1A byte or a CR CR LF line ending
  now parses and digests whole, so a 0x1A on `lean-toolchain`'s first line
  gets `invalid-lean-toolchain` instead of passing.
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
  the site shows as code, and only those give an article its cited passage.
- **Claims inside a Git hook.** The claim commands' Git ignores `GIT_DIR`,
  `GIT_WORK_TREE`, `GIT_INDEX_FILE` and the other variables that point Git at
  another repository, as a hook sets them. With only `GIT_DIR` set, the board's
  Git wrote objects, and on a refused acquire a claim ref, into the hook's
  repository; with `GIT_WORK_TREE` too, every claim command failed. `git -c`
  settings still reach the board.
- **Other commands inside a Git hook.** In a hook, `init` and `project new`
  pin CI to the Autoform checkout they run from, and `claim` and `dashboard`
  without `--repo` use the origin of the checkout they run in. Before, the
  hook's `GIT_DIR` and `GIT_INDEX_FILE` made Git take any directory for the
  hook's repository, so CI was written without a pin, and the default board was
  the hook repository's origin. Autoform's local Git commands now share one
  runner that drops the caller's `GIT_*` variables, except
  `GIT_CEILING_DIRECTORIES`, which only stops Git's search upwards.
- **`\pmod`, `\bmod` and `\mod` on the site.** Site formulas typeset `\pmod`,
  `\bmod`, `\mod` and amsmath's `\varliminf`, `\varlimsup`, `\varinjlim` and
  `\varprojlim`. MathJax builds them from `\mmlToken`, which the page's TeX input
  turns off, so each showed a red `\notallowed`; they are now written without
  it, and a project macro of the same name still replaces one (#544).
- **Table headers in the reading pane.** A table too wide for the explorer's
  reading pane no longer breaks its headers mid-word ("Dependen t"): a cell
  keeps its words whole, and the table scrolls.
- **Import-closure scan.** `work import-impact --module-scope import-closure`
  reads uncaptured `.lean` files only in the root libraries' module trees and
  in dependency packages. It no longer reads other nested checkouts, such as
  worker worktrees with their own `.lake`, the root's build output or a
  package's `.lake`, which could stall the run before its probe (#429).
- **Search index load.** The explorer prepares its search index in 25 ms slices
  rather than 512-row batches, reading the clock every 128 rows, and resolves
  each page's links once rather than every row's. The first slice begins with
  the parse, so a long parse is not followed by a slice's work in the same task.
  On ATLAS (38,661 rows), search, the Topics outline and concept links were
  ready 5.3 s after the index was parsed, most of it spent waiting between
  batches; with 10 ms slices they were ready after 1.0 s in WebKit and 0.7 s in
  Chrome.
- **Search index memory.** The explorer no longer keeps the search index's
  text in its page cache after preparing the index. ATLAS's 42 MB index took
  40 of the cache's 48 MiB, so pages and graphs already read were dropped and
  fetched again. A body WebKit hands over in one piece is hashed and decoded
  without a copy, and when reading the index takes at least 25 ms, a frame
  paints before it is parsed. On ATLAS, on one CI runner, with 10 ms slices,
  the longest frame while the index was read and parsed went from 217 ms to
  142 ms in Chrome and from 199 ms to 164 ms in WebKit.
- **Summaries with formulas.** An article's summary, the line the explorer's
  lists show under its title, no longer ends inside a formula or code span,
  which showed it as raw TeX: a clip that would cut one stops before it. A
  display formula inside the summary's paragraph, `$$...$$`, shows inline
  there.
- **Book order.** A link inside an HTML comment no longer places its page in
  the book's order: a commented-out chapter link moved that chapter forward in
  `SUMMARY.md`, though the page showed it later (#535).
- **Fences in comments.** Render no longer reads a fence line inside an HTML
  comment as opening a code block. It had left every later line of the page
  unrewritten, so a link to a source note after it was published as written,
  to a page the site leaves out, and `check` said nothing (#527). `check`,
  `audit` and the explorer now read comments as the site does too, so a `<!--`
  in a code span no longer hides a fence that never closes from them, nor makes
  a closed code block after it read as unclosed.
- **Output checks.** `skeleton`, `review prepare` and `library index` refuse
  to replace an output folder that holds a subfolder they cannot list, where
  they skipped it, so an edit inside it could be published over (#521).
- **No size caps on published data.** The explorer loads its own site's
  search index, map data, pages and concept links whatever their size, and
  its timeout now measures a stalled transfer rather than a long one. Render
  keeps the whole concept-links layer, reads any `atlas.json`, and writes and
  replaces a site whose `publication.json` passes 8 MiB (it lists every site
  file). It notes a search index over 32 MiB, which every explorer page
  downloads. ATLAS's
  42 MB index had passed the old 32 MiB limit, so search and the Topics
  outline failed on every page.
- **Explorer reading defaults.** Desktop chapters open with the reader showing
  their text again, and arriving at a context chapter from search or the trail
  focuses its heading. On a phone, Back to a result whose reader was
  closed no longer fails with "Could not open this view" (#507, #520).
- **Explorer review follow-ups.** Deferred list math cancels with its view and
  falls back to visible text on
  unavailable typesetting. Preview formatting keeps currency and code literal;
  formal dependency links use converter-validated code and formula labels (#444).
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
  indexes 10 classes it missed, such as `Finite`, `ExpChar` and `IsGCDMonoid`.
- **Dotted namespaces.** The Lean source index and `audit`'s limit scan open
  a scope for each component of `namespace A.B` or `section A.B`, and `end`
  closes one per component of its name, as Lean does: `end B` after
  `namespace A.B` no longer drops the `A.` prefix from later names, and
  `end C.D` closes both scopes (#524).
- **Empty quoted name components.** `skeleton`, `review prepare`,
  `render --review` and the generated `.github/autoform_audit.py` read a Lean
  name with an empty quoted component, such as `Foo.«».bar`, as Lean does,
  instead of refusing it as an invalid declaration name.
- **Explorer controls.** The closed phone handle anchors to the bottom (#442).
- **Explorer resizing.** Observer notifications coalesce into one animation
  frame, and pending resize work is cancelled when the viewer is disposed (#442).
- **Explorer keyboard focus.** Result selection focuses its visible statement
  heading. Outline and filters keep native focus. Slash stays scoped; modified
  arrow keys stay native, and empty Escape no longer adds history entries (#442).
- **Reading feedback.** Valid HTML character references keep title math
  without admitting authored HTML. Directory aliases retain statement
  fragments (#442).
- **Wiki references.** Section boundaries come from the site's converter, so
  Setext and formatted Sources headings cannot turn provenance into concept
  links. Formal dependencies, statuses and captured article bytes are unchanged (#442).
- **Explorer navigation.** Contradictory canonical reading routes are refused
  before changing the reader. Failed rendering cannot poison history snapshots,
  and foreign-URL state is not restored. Failures offer a safe native article link (#442).
- **Explorer builds.** Compiler and publisher share a UTF-8/LF source fingerprint,
  including on CRLF checkouts, without requiring Node in consumer projects (#442).
- **Lean commands.** A Lake or Lean command Autoform runs that fails just as
  Ctrl+C or a termination signal arrives is stopped along with Autoform
  instead of being left running (#510).
- **Large wiki catalogues.** Validation yields to visible page frames, with
  a bounded fallback for hidden or stalled tabs, rather than relying only on
  timer tasks. Parent references use the same compact node offsets as relations;
  the client restores their exact IDs before publication. No content is dropped (#442).
- **Lake configuration.** A `lakefile.toml` whose `lean_lib` is not an array
  of named tables, such as `[lean_lib]` written for `[[lean_lib]]`, is
  refused as Lake refuses it, instead of crashing, reading the package's
  default library, or skipping the entries that are not named tables (#505).
- **Wiki reading links.** The integrated explorer retains #455's title math
  in its outline, breadcrumbs, paged inventory and related-reading controls, using safe MathJax
  service and project macros. Code stays literal, and canonical links keep
  focus and native modifiers (#442).
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
- **Links on chapter pages.** A definition-like line with more words after
  its first, such as `[^1]: Grounded in [Paper](../sources/paper.md)`, is
  prose, as Python-Markdown reads it. Render read it as a definition and left
  the links after its first word pointing at unpublished pages (#484).
- **Roadmap folder spelling.** A roadmap folder spelled with other capitals,
  such as `Roadmap/`, passed `check` on a case-insensitive filesystem, as on
  macOS, and then ended `render` in a `KeyError`. Loading the graph now refuses
  it on every filesystem as Linux did, "roadmap directory does not exist", and
  names the rename (#595).

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
