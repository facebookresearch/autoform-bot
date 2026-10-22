# Changelog

Notable changes to Autoform, newest first. A release's version is the one in
`pyproject.toml`; `uv.lock`, the Claude, Muse and Codex manifests, and this
file's newest release heading carry the same one (`tests/test_release.py`
checks), and `autoform --version` prints it. Add a line under **Unreleased**
with each change worth knowing about, and give the section its version and
date when the release is tagged.

## Unreleased

What has landed since 0.5.0, across about 200 merged pull requests, grouped by
theme. Each group names the pull requests to read for detail.

### Added

- **Formalization frontier.** A Markdown roadmap now schedules the work:
  `autoform work` lists ready leaves, gives one leaf's context, and measures
  the impact of a revision or an import (#92, #136, #183). Proofs may import
  other articles' modules when that impact is measured.
- **Skeletons.** `autoform skeleton` extracts what a reader must trust for each
  formalized statement, from elaborated terms, one Lean probe per root module,
  run in parallel (#12, #213, #214).
- **Search and reuse.** `autoform search` finds articles by title, Lean name or
  statement before an agent adds one (#171, #208, #375, #376, #392).
  `autoform library` lists the Lean libraries a project depends on and writes
  the index a library publishes, ATLAS among them; `autoform search --library`
  searches that index, and the skills reuse its results (#204, #278, #311,
  #343, #379, #400, #408).
- **Projects.** `autoform project new`, `inspect` and `versions` create and
  inspect Lean projects from a pinned release catalog (#14, #96, #211).
- **Dashboard.** `autoform dashboard` serves the built site on a loopback port,
  with a read-only overlay of current claims (#91).
- **Implementation notes.** Each article's implementation handoff is a hidden
  file, `blueprint/.implementation-notes/<article_id>.md`, so articles keep to
  frontmatter and informal mathematics. Every command that loads the graph
  rejects a malformed, empty, symlinked or orphaned note, and `audit` reports
  one left behind after its article is proved (#172, #296).
- **Unpublished folders.** A folder's `README.md` can say `published: false`:
  `check` and `audit` still validate the folder, and render leaves it out
  (#381).
- **CI audit.** The generated verify workflow checks `lean:` targets,
  non-root axioms, a kernel replay and elaboration limits, without running
  project code in its probe (#156, #190, #247, #264).
- **Roadmap audit report.** The generated Pages workflow runs `autoform audit`
  after `check` and lists its findings in the step's log, without failing the
  build or stopping a deploy (#437).
- **Statement status.** Open statements and conditional proofs (#138).
- **Feedback.** `autoform debrief`: forms agents fill in about their work
  (#195, #414).
- **Version.** `autoform --version` prints the installed version and, from a
  plugin checkout, its commit (#397).

### Changed

- **Commands inform; they do not break a codebase.** A new check warns and
  keeps the exit status instead of failing a repository that passed before
  (#325, #335, #361), a rule AGENTS.md now states (#394).
- **Claims are optional.** A single Formalize run keeps its own workers apart,
  and builds take no claim (#298, #324, #420).
- **Publication.** Render publishes a site transactionally (#165), scales
  linearly (#289, #339), checks the site it replaces without reading it into
  memory (#365), and links files by the spelling Git committed (#301, #333).
- **Checks.** The graph loader rejects formalized work the Lean cannot back
  (#158). `check` rejects Sources links that name no file (#287) or a hidden
  path (#321), and container articles that set a declaration (#328); since
  #361, hidden source notes and declaring containers pass with a warning.
  `check` and `audit` now read the blueprint render publishes: hidden paths are
  no part of it, and a dependency on a hidden page, a link that steps through a
  hidden folder, or a link to a generated view fails (#380, #409).

### Removed

- `autoform unused-hypotheses`, in favor of Batteries' `unusedArguments`
  linter (#198, #352).
- The language server's diagnostics path (#306), the project routers and
  resident-era REPL pool (#316), and code nothing called (#114, #341, #344).

### Fixed and hardened

- **Lean runtime, REPL and LSP:** bounded transport, isolated public REPL
  calls, diagnostics barriers, and cleanup of leftover Lean processes (#13,
  #105, #106, #107, #111, #256, #283, #288, #322, #331, #338, #354, #360, #382).
- **Process control:** verified descendant cleanup and real exit statuses when
  SIGCHLD is ignored (#118, #168, #315, #334, #370).
- **Lean source capture and indexing:** filesystem-generation snapshots,
  aliases, multiline declarations, public declarations among duplicates, and
  repositories of any size (#95, #148, #231, #257, #308, #312, #345, #348,
  #363, #364, #371).
- **Render:** statement and proof bodies keep their compact inequalities,
  math, code fences, raw HTML and autolinks (#362).
- **Links:** malformed, angle-bracket and query links, code-only labels, links
  that leave the blueprint, the structure page's links, and code links when
  the Lean root is a subdirectory of its repository (#318, #320, #326, #327,
  #355, #406).
- **Security:** credentials stripped from remote URLs, URL queries redacted,
  nonlocal dashboard requests rejected, and every published page treated as
  untrusted: markup the site cannot trust is shown as typed, and a file a
  browser could run is left out, each with a warning (#98, #125, #215, #245).

### Contracts

Public CLI and JSON shapes are contracts. The JSON schema identifiers on main
include `autoform-skeleton/v5`, `autoform-work/v2`, `autoform-runtime/v3`,
`autoform-search/v2`, `autoform-publication/v2` and `autoform-impact/v1`.

### Dependencies

- `cryptography` bounded on both sides of the Intel Mac split (#56, #57);
  `pyjwt`, `urllib3` and `mcp` bumped (#50, #51, #353).

## 0.5.0 (2026-08-28)

The release this changelog starts from: Autoform v2.0 (#5).
